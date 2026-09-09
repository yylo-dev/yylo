#!/usr/bin/env python3
"""Helpers for multi-task Kanban + workflow task sets.

This is a guardrail/helper, not a new task store. It validates reusable task-set
contracts and can render a simple workflow YAML from a JSON manifest so task IDs,
tags, dependencies, and E2E exclusions come from one source.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from workflow_run_evidence import WorkflowRunEvidenceError, resolve_workflow_manifest

ROOT = Path(__file__).resolve().parents[2]
MAX_TAG_LEN = 50
TAG_RE = re.compile(r"^[A-Za-z0-9_-]+$")
REQUIRED_SECTIONS = [
    "Context:",
    "ASCII flow:",
    "MUST:",
    "MUST NOT:",
    "Failure mode prevented:",
    "Runtime contract enforced:",
    "Exact validation gate:",
    "Why tests/backing implementation matter:",
]
HIGH_COMPUTE_MARKERS = ("High-compute contract:", "HIGH_COMPUTE", "write-capable high-compute")
HIGH_COMPUTE_REQUIRED_TERMS = ["run_id", "attempt_id", "checkpoint", "resume", "telemetry", "early", "denominator"]
CONSERVATIVE_READ_FIRST = [
    ".juno_task/wiki/parallel_runner_task_creation_best_practices.md",
    "$(yy wiki --path)/controller/parallel_runner_and_spec_review.md",
]
ROLE_READ_FIRST = {
    "implementation": ["AGENTS.md"],
    "review": ["AGENTS.md", "$(yy wiki --path)/controller/parallel_runner_and_spec_review.md"],
    "planning": ["AGENTS.md", ".juno_task/wiki/parallel_runner_task_creation_best_practices.md"],
}


class FinalizeReviewError(RuntimeError):
    """Final review evidence could not be produced safely."""


class TaskSetCreationError(RuntimeError):
    """A symbolic task set could not be created transactionally."""


def fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    raise SystemExit(1)


def warn(msg: str) -> None:
    print(f"WARN: {msg}", file=sys.stderr)


def validate_tag(tag: str, *, e2e: bool = False) -> list[str]:
    errors: list[str] = []
    if not tag:
        errors.append("tag is empty")
    if len(tag) > MAX_TAG_LEN:
        errors.append(f"tag '{tag}' is {len(tag)} chars; max supported helper length is {MAX_TAG_LEN}")
    if not TAG_RE.match(tag):
        errors.append(f"tag '{tag}' must contain only letters, numbers, underscores, and hyphens")
    if e2e and not tag.endswith("_E2E_post_deploy"):
        errors.append(f"E2E tag '{tag}' must end with _E2E_post_deploy")
    if not e2e and tag.endswith("_E2E_post_deploy"):
        errors.append(f"implementation tag '{tag}' must not end with _E2E_post_deploy")
    return errors


def validate_body(path: Path) -> list[str]:
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    errors = [f"{path}: missing section {section}" for section in REQUIRED_SECTIONS if section not in text]
    if b"\x00" in raw:
        errors.append(f"{path}: contains a file-unsafe NUL byte")
    if b"\r\n" in raw or b"\r" in raw:
        errors.append(f"{path}: contains CRLF or carriage-return line endings; use LF")
    for line_number, line in enumerate(text.splitlines(), start=1):
        if line.endswith((" ", "\t")):
            errors.append(f"{path}:{line_number}: trailing whitespace is forbidden")
    if raw and not raw.endswith(b"\n"):
        errors.append(f"{path}: must end with a final newline")
    if "[blocked_by]" in text and "Blocked by:" not in text:
        errors.append(f"{path}: has [blocked_by] markup but no Blocked by section label")
    return errors


def validate_high_compute_body(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    if not any(marker in text for marker in HIGH_COMPUTE_MARKERS):
        return []
    lowered = text.lower()
    errors = [f"{path}: high-compute body missing term '{term}'" for term in HIGH_COMPUTE_REQUIRED_TERMS if term not in lowered]
    if "small" not in lowered or "fixture" not in lowered or "full" not in lowered:
        errors.append(f"{path}: high-compute body must distinguish small/fixture/canary evidence from full-denominator evidence")
    return errors


def load_manifest(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        fail("manifest must be a JSON object")
    return data


def manifest_errors(manifest: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    impl_tag = str(manifest.get("implementation_tag", ""))
    e2e_tag = str(manifest.get("e2e_tag", ""))
    errors.extend(validate_tag(impl_tag, e2e=False))
    errors.extend(validate_tag(e2e_tag, e2e=True))
    if impl_tag == e2e_tag:
        errors.append("implementation_tag and e2e_tag must differ")
    tasks = manifest.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        errors.append("manifest.tasks must be a non-empty list")
        return errors
    seen: set[str] = set()
    for i, task in enumerate(tasks):
        if not isinstance(task, dict):
            errors.append(f"tasks[{i}] must be an object")
            continue
        task_id = str(task.get("id", ""))
        role = str(task.get("role", ""))
        if not task_id:
            errors.append(f"tasks[{i}] missing id")
        if task_id in seen:
            errors.append(f"duplicate task id {task_id}")
        seen.add(task_id)
        if role == "post_deploy_e2e":
            errors.append(f"post-deploy E2E task {task_id} must be excluded from implementation workflow tasks")
        body_file = task.get("body_file")
        if body_file:
            body_path = (ROOT / body_file).resolve() if not Path(body_file).is_absolute() else Path(body_file)
            errors.extend(validate_body(body_path))
            errors.extend(validate_high_compute_body(body_path))
    for e2e_id in manifest.get("e2e_task_ids", []) or []:
        if e2e_id in seen:
            errors.append(f"E2E task {e2e_id} appears in implementation tasks list")
    return errors


SYMBOLIC_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
TASK_REFERENCE_RE = re.compile(r"\{\{task\.([A-Za-z][A-Za-z0-9_-]*)\}\}")
VALIDATOR_PLACEHOLDER_RE = re.compile(r"\{\{([a-z_]+)\}\}")
VALIDATOR_PLACEHOLDERS = {
    "body_file",
    "task_key",
    "role",
    "status",
    "tags_json",
    "dependencies_json",
}


def symbolic_task_set_errors(manifest: dict[str, Any], *, manifest_dir: Path | None = None, validate_files: bool = True) -> list[str]:
    errors: list[str] = []
    errors.extend(validate_tag(str(manifest.get("implementation_tag", "")), e2e=False))
    errors.extend(validate_tag(str(manifest.get("e2e_tag", "")), e2e=True))
    tasks = manifest.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return errors + ["manifest.tasks must be a non-empty list"]
    keys: list[str] = []
    validators = manifest.get("task_body_validators", []) or []
    if not isinstance(validators, list):
        errors.append("manifest.task_body_validators must be a list")
        validators = []
    for index, validator in enumerate(validators):
        if not isinstance(validator, dict):
            errors.append(f"task_body_validators[{index}] must be an object")
            continue
        name = str(validator.get("name", ""))
        command = validator.get("command")
        roles = validator.get("roles", []) or []
        if not name:
            errors.append(f"task_body_validators[{index}] missing name")
        if not isinstance(command, list) or not command or not all(isinstance(arg, str) and arg for arg in command):
            errors.append(f"task_body_validators[{index}] command must be a non-empty string list")
        else:
            unknown = sorted({match for arg in command for match in VALIDATOR_PLACEHOLDER_RE.findall(arg)} - VALIDATOR_PLACEHOLDERS)
            if unknown:
                errors.append(f"task_body_validators[{index}] command has unknown placeholders: {', '.join(unknown)}")
        if not isinstance(roles, list) or not all(isinstance(role, str) and role for role in roles):
            errors.append(f"task_body_validators[{index}] roles must be a string list")
        try:
            if float(validator.get("timeout_seconds", 30)) <= 0:
                raise ValueError
        except (TypeError, ValueError):
            errors.append(f"task_body_validators[{index}] timeout_seconds must be positive")
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            errors.append(f"tasks[{index}] must be an object")
            continue
        key = str(task.get("key", ""))
        if not SYMBOLIC_KEY_RE.fullmatch(key):
            errors.append(f"tasks[{index}] has invalid or missing symbolic key '{key}'")
        elif key in keys:
            errors.append(f"duplicate symbolic task key {key}")
        keys.append(key)
        role = str(task.get("role", ""))
        tags = [str(tag) for tag in task.get("tags", []) or []]
        edit_capable = task.get("edit_capable", False)
        if role == "implementation" and "edit_capable" not in task:
            errors.append(f"task {key or index} role implementation must explicitly declare edit_capable true or false")
        if not isinstance(edit_capable, bool):
            errors.append(f"task {key or index} edit_capable must be boolean")
        if role == "review" and (edit_capable is True or task.get("edit_admission") is not None):
            errors.append(f"task {key or index} role review must not declare edit_capable true or edit_admission")
        if generated_task_requires_admission(task):
            admission = task.get("edit_admission")
            required = {"repository", "target_ref", "approved_base", "task_worktree", "task_branch_ref",
                        "cleanup_owner", "manifest", "verify_receipt", "output", "next_receipt", "expected_paths"}
            if not isinstance(admission, dict) or set(admission) != required:
                errors.append(f"task {key or index} edit_admission must contain exactly: {', '.join(sorted(required))}")
            elif (not isinstance(admission.get("expected_paths"), list) or not admission["expected_paths"]
                  or not all(isinstance(value, str) and value for value in admission["expected_paths"])):
                errors.append(f"task {key or index} edit_admission.expected_paths must be a non-empty string list")
        if role == "post_deploy_e2e" and manifest.get("implementation_tag") in tags:
            errors.append(f"post-deploy E2E task {key} must not carry the implementation tag")
        if role != "post_deploy_e2e" and manifest.get("e2e_tag") in tags:
            errors.append(f"implementation task {key} must not carry the E2E tag")
        body_file = task.get("body_file")
        if not body_file:
            errors.append(f"task {key or index} missing body_file")
        elif validate_files:
            base = manifest_dir or ROOT
            body_path = Path(body_file)
            if not body_path.is_absolute():
                body_path = base / body_path
            if not body_path.is_file():
                errors.append(f"task {key} body file does not exist: {body_path}")
            else:
                errors.extend(validate_body(body_path))
                errors.extend(validate_high_compute_body(body_path))
        if role == "post_deploy_e2e":
            matching_validators = [
                validator
                for validator in validators
                if isinstance(validator, dict)
                and (not (validator.get("roles", []) or []) or role in validator.get("roles", []))
            ]
            if not matching_validators:
                errors.append(
                    f"task {key or index} role post_deploy_e2e requires a canonical task_body_validator"
                )
    key_set = set(keys)
    by_key = {str(task.get("key")): task for task in tasks if isinstance(task, dict)}

    def ancestors(key: str) -> set[str]:
        found: set[str] = set()
        pending = list(by_key.get(key, {}).get("blocked_by", []) or [])
        while pending:
            dependency = str(pending.pop())
            if dependency in found or dependency not in by_key:
                continue
            found.add(dependency)
            pending.extend(by_key[dependency].get("blocked_by", []) or [])
        return found

    for task in tasks:
        if not isinstance(task, dict):
            continue
        key = str(task.get("key"))
        for dependency in task.get("blocked_by", []) or []:
            if str(dependency) not in key_set:
                errors.append(f"task {key} has unknown symbolic dependency {dependency}")
        if validate_files and task.get("body_file"):
            body_path = Path(task["body_file"])
            if not body_path.is_absolute():
                body_path = (manifest_dir or ROOT) / body_path
            if body_path.is_file():
                for reference in TASK_REFERENCE_RE.findall(body_path.read_text(encoding="utf-8")):
                    if reference not in key_set:
                        errors.append(f"task {key} body has unknown symbolic task reference {reference}")
                    elif reference not in ancestors(key):
                        errors.append(f"task {key} body references {reference}, which is not a true dependency ancestor")
    if not errors:
        try:
            topological_task_keys(tasks)
        except ValueError as exc:
            errors.append(str(exc))
    return errors


def topological_task_keys(tasks: list[dict[str, Any]]) -> list[str]:
    by_key = {str(task["key"]): task for task in tasks}
    remaining = {key: set(map(str, task.get("blocked_by", []) or [])) for key, task in by_key.items()}
    result: list[str] = []
    while remaining:
        ready = [key for key in by_key if key in remaining and not remaining[key]]
        if not ready:
            raise ValueError("symbolic task dependency graph contains a cycle")
        for key in ready:
            result.append(key)
            remaining.pop(key)
            for dependencies in remaining.values():
                dependencies.discard(key)
    return result


def q(value: str) -> str:
    return value.replace("'", "''")


def generated_write_contract(task: dict[str, Any]) -> str:
    """Derive generated dispatch authority; semantic reviewers are always read-only."""
    if str(task.get("role", "")) == "review":
        return "read_only"
    if task.get("edit_capable") is True:
        return "product_edit"
    return "read_only"


def generated_task_requires_admission(task: dict[str, Any]) -> bool:
    return generated_write_contract(task) != "read_only"


def render_workflow(manifest: dict[str, Any]) -> str:
    workflow_id = manifest.get("workflow_id") or manifest.get("id") or "task_set_workflow"
    name = manifest.get("name") or workflow_id.replace("_", " ").title()
    description = manifest.get("description") or "Generated Kanban task-set workflow."
    impl_tag = manifest["implementation_tag"]
    e2e_tag = manifest["e2e_tag"]
    parent = manifest.get("parent_task", "")
    kanban_wrapper = str(manifest.get("kanban_wrapper", "./.juno_task/scripts/kanban.sh"))
    kanban_cmd = shlex.quote(kanban_wrapper)
    lines: list[str] = [
        "# Generated by .juno_task/scripts/task_workflow_helper.py render-workflow",
        f"# E2E tag excluded from implementation workflow: {e2e_tag}",
        "schema_version: 1",
        f"workflow_id: {workflow_id}",
        f"name: {name}",
        f"description: {description}",
        "vars:",
        f"  implementation_tag: {impl_tag}",
        f"  e2e_tag: {e2e_tag}",
        f"  parent_task: {parent}",
        "  shared_agent_contract: |",
        f"    Use the canonical controller Kanban wrapper `{kanban_wrapper}` for Kanban writes.",
        "    Fetch a particular related task separately only when its complete body is required.",
        "    Mark the selected task in_progress with a response-file before work.",
        "    Work only on the selected task and explicit TASK_ROOT.",
        "    Follow every MUST, MUST NOT, and exact validation gate in the task body.",
        "    Mark done only when validation evidence satisfies the task; otherwise leave a clear blocker response.",
        f"    Do not push, release, publish, deploy, mutate production, run excluded E2E, use tag `{e2e_tag}`, or clean lifecycle worktrees.",
        f"    Parent task: {parent}",
        f"    Shared implementation tag: {impl_tag}",
        "  review_contract: |",
        "    Inspect the exact lifecycle-bound diff, tests, and typed receipts independently.",
        "    This is a read-only review: do not edit files, create commits, update Kanban, launch reviewers, or repair findings.",
    ]
    edit_tasks = [task for task in manifest["tasks"] if generated_task_requires_admission(task)]
    if edit_tasks:
        lines.append("receipts:")
        for idx, task in enumerate(manifest["tasks"], start=1):
            if not generated_task_requires_admission(task): continue
            task_id = task["id"]; receipt_id = f"edit_admission_{idx}_{re.sub(r'[^a-z0-9_]', '_', task_id.lower())}"
            task_root = str(Path(task["edit_admission"]["task_worktree"]).expanduser().resolve())
            lines.extend([
                f"  - id: {receipt_id}", f"    producer: pre_edit_{idx}_{task_id}",
                f"    path: {task['edit_admission']['output']}", "    schema_version: juno_edit_preflight.v1",
                "    required_fields:", "      - producer_step_digest", "      - passed", "      - task_id",
                "      - current.root", "      - current.git_common_dir", "      - current.branch_ref",
                "      - current.head", "      - current.clean", "      - workspace.role",
                "      - workspace.current_root", "      - target.target_ref", "      - expected_paths",
                "    expected_fields:", "      passed: true", f"      task_id: {task_id}",
                f"      current.root: {task_root}", "      current.clean: true", "      workspace.role: task",
                f"      workspace.current_root: {task_root}",
            ])
    lines.extend([
        "steps:",
        "  - id: preflight",
        "    description: Resolve selected Kanban tasks and prove E2E tag isolation",
        "    capture_session: false",
        "    fail_workflow: true",
        "    command: |",
        "      set -eu",
    ])
    if parent:
        lines.append(f"      {kanban_cmd} get {parent} --compact >/dev/null")
    for task in manifest["tasks"]:
        lines.append(f"      {kanban_cmd} get {task['id']} --compact >/dev/null")
    for e2e_id in manifest.get("e2e_task_ids", []) or []:
        lines.append(f"      {kanban_cmd} get {e2e_id} --compact >/dev/null")
    lines.extend([
        "      printf '%s\\n' 'Implementation tag table:'",
        f"      {kanban_cmd} search --tag {impl_tag} --format table --limit 50",
        "      printf '%s\\n' 'Post-deploy E2E tag table:'",
        f"      {kanban_cmd} search --tag {e2e_tag} --format table --limit 50",
        f"      printf '%s\\n' 'Post-deploy E2E tag {e2e_tag} is excluded from implementation agent steps.'",
    ])
    for idx, task in enumerate(manifest["tasks"], start=1):
        task_id = task["id"]
        step_id = task.get("step_id") or f"task_{idx}_{task_id}"
        receipt_id = f"edit_admission_{idx}_{re.sub(r'[^a-z0-9_]', '_', task_id.lower())}"
        write_contract = generated_write_contract(task)
        write_capable = generated_task_requires_admission(task)
        if write_capable:
            raise ValueError(
                f"task {task_id} uses retired workflow edit admission; start it with `yy task start {task_id}`"
            )
        title = task.get("title") or f"Run Kanban task {task_id}"
        if "read_first" in task:
            read_first = task["read_first"]
        else:
            read_first = ROLE_READ_FIRST.get(str(task.get("role", "")), CONSERVATIVE_READ_FIRST)
        read_lines = "\n".join(f"        - `{path}`" for path in read_first) if read_first else "        (none explicitly supplied)"
        prior = task.get("prior_step")
        conversational_handoff = bool(task.get("conversational_handoff", False))
        if conversational_handoff and not prior:
            raise ValueError(f"task {task_id} conversational_handoff requires prior_step")
        prior_block = (
            f"\n\n        Deliberately declared conversational handoff:\n        {{{{ steps.{prior}.response }}}}"
            if conversational_handoff else ""
        )
        role_contract = []
        if str(task.get("role", "")) == "review":
            role_contract = ["", "        {{ vars.review_contract }}"]
        terminal_guard = []
        if str(task.get("role", "")) == "review":
            terminal_guard = [
                "        - MUST NOT run workflow doctor for the currently running workflow; its manifest is not terminal.",
                "        - The terminal owner runs `finalize-review` after the workflow has completed.",
            ]
        lines.extend([
            f"  - id: {step_id}",
            f"    description: {title}",
            "    capture_session: false",
            "    fail_workflow: true",
            "    generated_task_contract:",
            f"      role: {task.get('role', '')}",
            f"      write_contract: {write_contract}",
            *( [f"      task_root_receipt: {receipt_id}", "    edit_capable: true", "    requires_receipts:", f"      - {receipt_id}"] if write_capable else [] ),
            "    command:",
            "      - yy",
            "      - pi",
            "      - |",
            f"        You are a fresh-session agent for Kanban task {task_id}.",
            "",
            "        {{ vars.shared_agent_contract }}",
            *role_contract,
            "",
            "        Read first:",
            read_lines,
            "",
            "        Then run only this Kanban task:",
            f"        - Get task: `{kanban_wrapper} get {task_id} --compact`",
            *terminal_guard,
            prior_block,
        ])
    return "\n".join(lines) + "\n"


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(ROOT), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(str(tmp_path), str(path))


def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def preflight_task_set(manifest: dict[str, Any], manifest_path: Path) -> list[dict[str, Any]]:
    """Render the complete symbolic plan and run every project canonical validator.

    Preview IDs are stable non-Kanban tokens. They let validators inspect fully
    substituted bodies and dependency structure before any create call occurs.
    """
    preview_ids = {key: f"JUNO_PREFLIGHT_{key}" for key in topological_task_keys(manifest["tasks"])}
    by_key = {str(task["key"]): task for task in manifest["tasks"]}
    validators = manifest.get("task_body_validators", []) or []
    rows: list[dict[str, Any]] = []
    validation_errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="juno-task-set-preflight-") as temporary:
        body_dir = Path(temporary)
        for key in topological_task_keys(manifest["tasks"]):
            task = by_key[key]
            source_path = Path(task["body_file"])
            if not source_path.is_absolute():
                source_path = manifest_path.parent / source_path
            source_body = source_path.read_text(encoding="utf-8")
            resolved_body = TASK_REFERENCE_RE.sub(
                lambda match: preview_ids[match.group(1)], source_body
            )
            body_path = body_dir / f"{key}.md"
            body_path.write_text(resolved_body, encoding="utf-8")
            role = str(task.get("role", ""))
            required_tag = (
                manifest["e2e_tag"]
                if role == "post_deploy_e2e"
                else manifest["implementation_tag"]
            )
            tags = list(
                dict.fromkeys([required_tag] + [str(tag) for tag in task.get("tags", []) or []])
            )
            dependencies = [preview_ids[str(dep)] for dep in task.get("blocked_by", []) or []]
            status = str(task.get("status", "todo"))
            row = {
                "key": key,
                "role": role,
                "status": status,
                "tags": tags,
                "dependencies": dependencies,
                "body_sha256": hashlib.sha256(resolved_body.encode("utf-8")).hexdigest(),
            }
            rows.append(row)
            values = {
                "body_file": str(body_path),
                "task_key": key,
                "role": role,
                "status": status,
                "tags_json": json.dumps(tags, separators=(",", ":")),
                "dependencies_json": json.dumps(dependencies, separators=(",", ":")),
            }
            for validator in validators:
                roles = validator.get("roles", []) or []
                if roles and role not in roles:
                    continue
                command = [
                    VALIDATOR_PLACEHOLDER_RE.sub(lambda match: values[match.group(1)], arg)
                    for arg in validator["command"]
                ]
                timeout_seconds = float(validator.get("timeout_seconds", 30))
                env = os.environ.copy()
                env.update({f"JUNO_TASK_{name.upper()}": value for name, value in values.items()})
                env["JUNO_TASK_VALIDATION_ONLY"] = "1"
                try:
                    proc = subprocess.run(
                        command,
                        cwd=str(manifest_path.parent),
                        env=env,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        stdin=subprocess.DEVNULL,
                        timeout=timeout_seconds,
                    )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    validation_errors.append(
                        f"task {key} validator {validator['name']} body {source_path}: {exc}"
                    )
                    continue
                if proc.returncode != 0:
                    detail = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
                    for line in detail.splitlines():
                        validation_errors.append(
                            f"task {key} validator {validator['name']} body {source_path}: {line}"
                        )
    if validation_errors:
        raise TaskSetCreationError("canonical task-body preflight failed: " + "; ".join(validation_errors))
    return rows


def parse_kanban_task(stdout: str) -> dict[str, Any]:
    data = json.loads(stdout)
    if isinstance(data, list):
        if len(data) != 1:
            raise ValueError(f"expected one Kanban task, got {len(data)}")
        data = data[0]
    if not isinstance(data, dict) or not data.get("id"):
        raise ValueError("Kanban output does not contain one task object with an id")
    return data


def resolved_task_set(manifest: dict[str, Any], ids: dict[str, str] | None = None) -> dict[str, Any]:
    ids = ids or {}
    tasks = manifest["tasks"]
    by_key = {str(task["key"]): task for task in tasks}
    rows = []
    for key in topological_task_keys(tasks):
        task = by_key[key]
        key = str(task["key"])
        row = dict(task)
        row["id"] = ids.get(key)
        row["blocked_by_ids"] = [ids.get(str(dep)) for dep in task.get("blocked_by", []) or []]
        rows.append(row)
    implementation = [row for row in rows if row.get("role") != "post_deploy_e2e"]
    e2e = [row for row in rows if row.get("role") == "post_deploy_e2e"]
    mutation_claims = []
    for claim in manifest.get("mutation_claims", []) or []:
        resolved_claim = dict(claim)
        task_key = resolved_claim.pop("task_key", None)
        if task_key is not None:
            resolved_claim["task_id"] = ids.get(str(task_key))
        mutation_claims.append(resolved_claim)
    return {
        "workflow_id": manifest.get("workflow_id", "task_set_workflow"),
        "name": manifest.get("name"),
        "description": manifest.get("description"),
        "implementation_tag": manifest["implementation_tag"],
        "e2e_tag": manifest["e2e_tag"],
        "parent_task": manifest.get("parent_task", ""),
        "owned_paths": manifest.get("owned_paths", []) or [],
        "baseline_dirty_paths": manifest.get("baseline_dirty_paths", []) or [],
        "mutation_claims": mutation_claims,
        "kanban_wrapper": manifest.get("kanban_wrapper", "./.juno_task/scripts/kanban.sh"),
        "creation_order": [{"key": key, "id": ids.get(key)} for key in topological_task_keys(tasks)],
        "tasks": implementation,
        "e2e_tasks": e2e,
        "e2e_task_ids": [row["id"] for row in e2e if row.get("id")],
    }


def workflow_manifest_from_resolved(resolved: dict[str, Any]) -> dict[str, Any]:
    tasks = []
    for row in resolved["tasks"]:
        task = dict(row)
        task["id"] = task.get("id") or f"UNCREATED_{task['key']}"
        task.pop("key", None)
        task.pop("blocked_by_ids", None)
        tasks.append(task)
    return {
        "workflow_id": resolved["workflow_id"],
        "name": resolved.get("name"),
        "description": resolved.get("description"),
        "implementation_tag": resolved["implementation_tag"],
        "e2e_tag": resolved["e2e_tag"],
        "parent_task": resolved.get("parent_task", ""),
        "owned_paths": resolved.get("owned_paths", []),
        "baseline_dirty_paths": resolved.get("baseline_dirty_paths", []),
        "mutation_claims": resolved.get("mutation_claims", []),
        "kanban_wrapper": resolved.get("kanban_wrapper", "./.juno_task/scripts/kanban.sh"),
        "tasks": tasks,
        "e2e_task_ids": resolved.get("e2e_task_ids", []),
    }


def render_parent_response(resolved: dict[str, Any], *, mode: str) -> str:
    lines = [
        f"Task-set {mode} artifacts generated from the symbolic manifest.",
        "",
        f"- Implementation tag: `{resolved['implementation_tag']}`",
        f"- Post-deploy E2E tag: `{resolved['e2e_tag']}`",
        "- Human semantic acceptance remains required.",
        "",
        "Resolved tasks:",
    ]
    for row in resolved["creation_order"]:
        lines.append(f"- `{row['key']}` -> `{row.get('id') or 'DRY_RUN_UNCREATED'}`")
    return "\n".join(lines) + "\n"


def create_task_set(manifest_path: Path, output_dir: Path, *, execute: bool = False, kanban_wrapper: str = "./.juno_task/scripts/kanban.sh") -> dict[str, Any]:
    manifest_path = manifest_path.resolve()
    output_dir = output_dir.resolve()
    manifest = load_manifest(manifest_path)
    errors = symbolic_task_set_errors(manifest, manifest_dir=manifest_path.parent)
    if errors:
        raise TaskSetCreationError("invalid symbolic task set: " + "; ".join(errors))
    preflight_rows = preflight_task_set(manifest, manifest_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    mode = "execute" if execute else "dry_run"
    receipt: dict[str, Any] = {
        "schema": "juno_symbolic_task_set_receipt.v1",
        "mode": mode,
        "status": "running" if execute else "planned",
        "started_at": utc_now(),
        "manifest": str(manifest_path),
        "kanban_wrapper": kanban_wrapper,
        "created_tasks": {},
        "create_results": [],
        "archive_results": [],
        "preflight": {"status": "passed", "tasks": preflight_rows},
        "human_acceptance_required": True,
    }
    ids: dict[str, str] = {}
    atomic_write_json(output_dir / "receipt.json", receipt)
    if execute:
        by_key = {str(task["key"]): task for task in manifest["tasks"]}
        try:
            for key in topological_task_keys(manifest["tasks"]):
                task = by_key[key]
                body_path = Path(task["body_file"])
                if not body_path.is_absolute():
                    body_path = manifest_path.parent / body_path
                source_body = body_path.read_text(encoding="utf-8")
                resolved_body = TASK_REFERENCE_RE.sub(lambda match: ids[match.group(1)], source_body)
                body_path = output_dir / "resolved_bodies" / f"{key}.md"
                atomic_write_text(body_path, resolved_body)
                role = str(task.get("role", ""))
                required_tag = manifest["e2e_tag"] if role == "post_deploy_e2e" else manifest["implementation_tag"]
                tags = list(dict.fromkeys([required_tag] + [str(tag) for tag in task.get("tags", []) or []]))
                dependencies = [ids[str(dep)] for dep in task.get("blocked_by", []) or []]
                cmd = [kanban_wrapper, "-f", "json", "create", "--body-file", str(body_path), "--status", str(task.get("status", "todo")), "--tags", *tags]
                if dependencies:
                    cmd.extend(["--blocked-by", *dependencies])
                proc = run(cmd)
                if proc.returncode != 0:
                    raise TaskSetCreationError(f"create failed for {key}: {proc.stderr.strip() or proc.stdout.strip()}")
                created = parse_kanban_task(proc.stdout)
                task_id = str(created["id"])
                ids[key] = task_id
                receipt["created_tasks"][key] = task_id
                readback_proc = run([kanban_wrapper, "-f", "json", "get", task_id])
                readback = parse_kanban_task(readback_proc.stdout) if readback_proc.returncode == 0 else {}
                observed_tags = readback.get("feature_tags") or []
                observed_dependencies = [str(value) for value in (readback.get("blocked_by") or [])]
                forbidden_tag = manifest["implementation_tag"] if role == "post_deploy_e2e" else manifest["e2e_tag"]
                verified = (
                    readback_proc.returncode == 0
                    and str(readback.get("id")) == task_id
                    and str(readback.get("status")) == str(task.get("status", "todo"))
                    and all(tag in observed_tags for tag in tags)
                    and forbidden_tag not in observed_tags
                    and observed_dependencies == dependencies
                )
                receipt["create_results"].append({"key": key, "id": task_id, "verified": verified})
                atomic_write_json(output_dir / "receipt.json", receipt)
                if not verified:
                    raise TaskSetCreationError(f"read-after-write verification failed for {key}/{task_id}")
            receipt["status"] = "created"
        except Exception as exc:
            receipt["error"] = str(exc)
            for key, task_id in reversed(list(ids.items())):
                archive_proc = run([kanban_wrapper, "archive", task_id])
                readback_proc = run([kanban_wrapper, "-f", "json", "get", task_id])
                try:
                    archived = parse_kanban_task(readback_proc.stdout)
                except Exception:
                    archived = {}
                receipt["archive_results"].append({
                    "key": key,
                    "id": task_id,
                    "archive_exit_code": archive_proc.returncode,
                    "verified": archive_proc.returncode == 0 and archived.get("status") == "archive",
                })
            receipt["status"] = "partial_create_archived" if ids and all(row["verified"] for row in receipt["archive_results"]) else "partial_create_cleanup_failed" if ids else "create_failed"
            receipt["completed_at"] = utc_now()
            atomic_write_json(output_dir / "receipt.json", receipt)
            raise TaskSetCreationError(str(exc)) from exc
    manifest_for_resolution = json.loads(json.dumps(manifest))
    manifest_for_resolution["kanban_wrapper"] = kanban_wrapper
    for task in manifest_for_resolution["tasks"]:
        body_file = Path(task["body_file"])
        if not body_file.is_absolute():
            task["body_file"] = str((manifest_path.parent / body_file).resolve())
    resolved = resolved_task_set(manifest_for_resolution, ids)
    atomic_write_json(output_dir / "resolved_manifest.json", resolved)
    atomic_write_text(output_dir / "workflow.yaml", render_workflow(workflow_manifest_from_resolved(resolved)))
    atomic_write_text(output_dir / "parent_response.md", render_parent_response(resolved, mode=mode))
    receipt["artifacts"] = {
        "resolved_manifest": str(output_dir / "resolved_manifest.json"),
        "workflow": str(output_dir / "workflow.yaml"),
        "parent_response": str(output_dir / "parent_response.md"),
    }
    receipt["completed_at"] = utc_now()
    atomic_write_json(output_dir / "receipt.json", receipt)
    return receipt


def verify_mutation_claims(claims: dict[str, Any], output: Path, kanban_wrapper: str = "./.juno_task/scripts/kanban.sh") -> dict[str, Any]:
    rows = claims.get("claims")
    if not isinstance(rows, list) or not rows:
        raise ValueError("claims must contain a non-empty claims list")
    receipt: dict[str, Any] = {
        "schema": "juno_kanban_mutation_claim_receipt.v1",
        "checked_at": utc_now(),
        "status": "verified",
        "results": [],
        "human_acceptance_required": True,
    }
    for claim in rows:
        task_id = str(claim.get("task_id", ""))
        expected = claim.get("expected", {}) or {}
        proc = run([kanban_wrapper, "-f", "json", "get", task_id])
        mismatches: list[str] = []
        observed: dict[str, Any] = {}
        if proc.returncode != 0:
            mismatches.append(f"read failed with exit code {proc.returncode}")
        else:
            try:
                task = parse_kanban_task(proc.stdout)
                observed = {field: task.get(field) for field in ("id", "status", "commit_hash", "last_modified", "feature_tags")}
                for field in ("status", "commit_hash", "last_modified"):
                    if field in expected and task.get(field) != expected[field]:
                        mismatches.append(f"{field}: expected {expected[field]!r}, observed {task.get(field)!r}")
                for tag in expected.get("tags_include", []) or []:
                    if tag not in (task.get("feature_tags") or []):
                        mismatches.append(f"feature_tags missing {tag!r}")
                response = str(task.get("agent_response", ""))
                for needle in expected.get("response_contains", []) or []:
                    if str(needle) not in response:
                        mismatches.append(f"agent_response missing required text {needle!r}")
            except Exception as exc:
                mismatches.append(f"invalid readback: {exc}")
        receipt["results"].append({"task_id": task_id, "verified": not mismatches, "mismatches": mismatches, "observed": observed})
    if any(not row["verified"] for row in receipt["results"]):
        receipt["status"] = "failed"
    atomic_write_json(output, receipt)
    return receipt


def dirty_path(status_line: str) -> str:
    path = status_line[3:].strip()
    if " -> " in path:
        path = path.split(" -> ", 1)[1]
    return path.strip('"')


def path_matches(path: str, patterns: Iterable[str]) -> bool:
    return any(path == pattern.rstrip("/") or path.startswith(pattern.rstrip("/") + "/") or fnmatch.fnmatch(path, pattern) for pattern in patterns)


def classify_dirty_tree(status: str, owned_paths: Iterable[str], baseline_paths: Iterable[str]) -> dict[str, list[str]]:
    buckets: dict[str, list[str]] = {"owned": [], "baseline": [], "unexpected": []}
    for line in status.splitlines():
        path = dirty_path(line)
        if path_matches(path, owned_paths):
            buckets["owned"].append(path)
        elif path_matches(path, baseline_paths):
            buckets["baseline"].append(path)
        else:
            buckets["unexpected"].append(path)
    return {key: sorted(set(paths)) for key, paths in buckets.items()}


def workflow_packet(run_dir: Path, task_ids: Iterable[str], e2e_tag: str | None = None, task_set_manifest: Path | None = None) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    summary_path = run_dir / "summary.md"
    try:
        resolved = resolve_workflow_manifest(run_dir)
    except WorkflowRunEvidenceError as exc:
        raise FinalizeReviewError(f"cannot resolve workflow run manifest: {exc}") from exc
    data = resolved.payload
    packet: dict[str, Any] = {
        "run_dir": str(run_dir),
        "manifest_exists": True,
        "summary_exists": summary_path.exists(),
        "manifest_path": str(resolved.path),
        "manifest_sha256": resolved.sha256,
        "manifest_source": resolved.source,
        "workflow_id": data.get("workflow_id"),
        "run_id": data.get("run_id"),
        "steps": [
            {"id": s.get("id"), "status": s.get("status"), "exit_code": s.get("exit_code"), "response_path": s.get("response_path"), "child_steps": s.get("child_steps", [])}
            for s in data.get("steps", [])
        ],
    }
    owned_paths: list[str] = []
    baseline_paths: list[str] = []
    kanban_wrapper = "./.juno_task/scripts/kanban.sh"
    if task_set_manifest:
        task_set = load_manifest(task_set_manifest)
        owned_paths = [str(path) for path in task_set.get("owned_paths", []) or []]
        baseline_paths = [str(path) for path in task_set.get("baseline_dirty_paths", []) or []]
        kanban_wrapper = str(task_set.get("kanban_wrapper", kanban_wrapper))
    statuses = []
    for task_id in task_ids:
        proc = run([kanban_wrapper, "-f", "json", "get", task_id])
        statuses.append({"id": task_id, "ok": proc.returncode == 0, "raw": proc.stdout if proc.returncode == 0 else proc.stderr})
    packet["kanban_tasks"] = statuses
    if e2e_tag:
        proc = run([kanban_wrapper, "search", "--tag", e2e_tag, "--format", "table", "--limit", "50"])
        packet["e2e_tag_table"] = proc.stdout if proc.returncode == 0 else proc.stderr
    status = run(["git", "status", "--short"]).stdout
    packet["git_status_short"] = status
    packet["dirty_tree"] = classify_dirty_tree(status, owned_paths, baseline_paths)
    return packet


def packet_summary(packet: dict[str, Any], output: Path | None = None) -> str:
    steps = packet.get("steps", []) or []
    failed_steps = sum(1 for step in steps if step.get("status") not in {"done", "success", "succeeded", "completed"})
    lookups = packet.get("kanban_tasks", []) or []
    failed_lookups = sum(1 for row in lookups if not row.get("ok"))
    dirty = packet.get("dirty_tree", {}) or {}
    parts = [
        f"workflow={packet.get('workflow_id') or 'unknown'}",
        f"run={packet.get('run_id') or 'unknown'}",
        f"steps={len(steps)}",
        f"failed_steps={failed_steps}",
        f"kanban_lookup_failures={failed_lookups}",
        f"owned_paths={len(dirty.get('owned', []))}",
        f"baseline_paths={len(dirty.get('baseline', []))}",
        f"unexpected_paths={len(dirty.get('unexpected', []))}",
        "human_acceptance_required=true",
    ]
    if output:
        parts.append(f"full_packet={output}")
    return " ".join(parts)


def finalize_review(run_dir: Path, manifest_path: Path) -> dict[str, Path]:
    run_dir = run_dir.resolve()
    manifest_path = manifest_path.resolve()
    if not run_dir.is_dir():
        raise FinalizeReviewError(f"run directory does not exist: {run_dir}")
    try:
        resolved_run_manifest = resolve_workflow_manifest(run_dir)
    except WorkflowRunEvidenceError as exc:
        raise FinalizeReviewError(f"cannot resolve workflow run manifest: {exc}") from exc
    if not manifest_path.is_file():
        raise FinalizeReviewError(f"task-set manifest does not exist: {manifest_path}")
    try:
        manifest = load_manifest(manifest_path)
        errors = manifest_errors(manifest)
    except Exception as exc:
        raise FinalizeReviewError(f"cannot load task-set manifest: {exc}") from exc
    if errors:
        raise FinalizeReviewError("invalid task-set manifest: " + "; ".join(errors))

    task_ids = [str(task["id"]) for task in manifest["tasks"]]
    e2e_tag = str(manifest["e2e_tag"])
    doctor_cmd = ["./.juno_task/scripts/workflow_runner.sh", "doctor", "--json", str(run_dir)]
    doctor_proc = run(doctor_cmd)
    try:
        doctor = json.loads(doctor_proc.stdout)
    except Exception as exc:
        raise FinalizeReviewError(f"doctor output is not parseable JSON: {exc}") from exc
    doctor_errors = [finding for finding in doctor.get("findings", []) if finding.get("level") == "error"]
    if doctor_proc.returncode != 0 or doctor_errors:
        raise FinalizeReviewError("workflow doctor reported error findings or command failure")

    packet_output = run_dir / "final_review" / "workflow_review_packet.json"
    packet_cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "workflow-review-packet",
        str(run_dir),
        "--tasks",
        ",".join(task_ids),
        "--e2e-tag",
        e2e_tag,
        "--manifest",
        str(manifest_path),
        "--output",
        str(packet_output),
    ]
    packet_proc = run(packet_cmd)
    try:
        packet = json.loads(packet_output.read_text(encoding="utf-8")) if packet_output.is_file() else json.loads(packet_proc.stdout)
    except Exception as exc:
        raise FinalizeReviewError(f"workflow review packet is not parseable JSON: {exc}") from exc
    if packet_proc.returncode != 0:
        raise FinalizeReviewError("workflow review packet command failed")
    failed_lookups = [row["id"] for row in packet.get("kanban_tasks", []) if not row.get("ok")]
    if failed_lookups:
        raise FinalizeReviewError(f"Kanban task lookup failed: {', '.join(failed_lookups)}")
    mutation_receipt_path: Path | None = None
    mutation_claims = manifest.get("mutation_claims", []) or []
    if mutation_claims:
        mutation_receipt_path = run_dir / "final_review" / "mutation_claim_receipt.json"
        mutation_receipt = verify_mutation_claims(
            {"claims": mutation_claims},
            mutation_receipt_path,
            str(manifest.get("kanban_wrapper", "./.juno_task/scripts/kanban.sh")),
        )
        if mutation_receipt["status"] != "verified":
            raise FinalizeReviewError(f"mutation claims failed read-after-write verification; inspect {mutation_receipt_path}")
    command_manifest = {
        "evidence_only": True,
        "human_acceptance_required": True,
        "inputs": {
            "run_dir": str(run_dir),
            "workflow_manifest": str(resolved_run_manifest.path),
            "workflow_manifest_sha256": resolved_run_manifest.sha256,
            "workflow_manifest_source": resolved_run_manifest.source,
            "task_set_manifest": str(manifest_path),
            "task_ids": task_ids,
            "e2e_tag": e2e_tag,
        },
        "commands": [
            {"argv": doctor_cmd, "return_code": doctor_proc.returncode},
            {"argv": packet_cmd, "return_code": packet_proc.returncode},
        ],
        "mutation_claim_receipt": str(mutation_receipt_path) if mutation_receipt_path else None,
        "status": "evidence_only",
    }
    readme = """# Final workflow review evidence

**Evidence only.** These artifacts do not grant semantic acceptance.

- `evidence_only=true`
- `human_acceptance_required=true`
- `doctor.json` records workflow artifact diagnostics.
- `workflow_review_packet.json` records workflow, Kanban, E2E-isolation, and tree evidence.
- `command_manifest.json` records derived inputs and command return codes.
- `mutation_claim_receipt.json`, when declared, records read-after-write task verification.

A human reviewer must inspect the task contract, implementation, tests, and these artifacts before accepting or rejecting the work.
"""
    final_dir = run_dir / "final_review"
    artifacts = {
        "final_dir": final_dir,
        "doctor": final_dir / "doctor.json",
        "packet": final_dir / "workflow_review_packet.json",
        "command_manifest": final_dir / "command_manifest.json",
        "readme": final_dir / "README.md",
    }
    try:
        atomic_write_json(artifacts["doctor"], doctor)
        atomic_write_json(artifacts["packet"], packet)
        atomic_write_json(artifacts["command_manifest"], command_manifest)
        atomic_write_text(artifacts["readme"], readme)
    except Exception as exc:
        raise FinalizeReviewError(f"cannot write final review artifacts: {exc}") from exc
    missing = [str(path) for key, path in artifacts.items() if key != "final_dir" and not path.is_file()]
    if missing:
        raise FinalizeReviewError("missing expected final review artifacts: " + ", ".join(missing))
    return artifacts


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Validate/render Kanban task-set workflow helpers", allow_abbrev=False)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_tags = sub.add_parser("validate-tags")
    p_tags.add_argument("--implementation-tag", required=True)
    p_tags.add_argument("--e2e-tag", required=True)
    p_body = sub.add_parser("validate-task-body")
    p_body.add_argument("paths", nargs="+")
    p_high = sub.add_parser("validate-high-compute-body")
    p_high.add_argument("paths", nargs="+")
    p_manifest = sub.add_parser("validate-manifest")
    p_manifest.add_argument("manifest")
    p_render = sub.add_parser("render-workflow")
    p_render.add_argument("--manifest", required=True)
    p_render.add_argument("--output", required=True)
    p_create = sub.add_parser("create-task-set", allow_abbrev=False, help="plan by default; --execute creates and verifies Kanban tasks")
    p_create.add_argument("manifest")
    p_create.add_argument("--output-dir", required=True)
    p_create.add_argument("--execute", action="store_true", help="explicitly authorize Kanban task creation; does not push or deploy")
    p_create.add_argument("--kanban-wrapper", default="./.juno_task/scripts/kanban.sh")
    p_claims = sub.add_parser("verify-mutation-claims", allow_abbrev=False)
    p_claims.add_argument("claims")
    p_claims.add_argument("--output", required=True)
    p_claims.add_argument("--kanban-wrapper", default="./.juno_task/scripts/kanban.sh")
    p_packet = sub.add_parser("workflow-review-packet", allow_abbrev=False)
    p_packet.add_argument("run_dir")
    p_packet.add_argument("--tasks", default="", help="comma-separated Kanban task IDs")
    p_packet.add_argument("--e2e-tag", default="")
    p_packet.add_argument("--manifest", help="task-set manifest with optional owned_paths and baseline_dirty_paths")
    p_packet.add_argument("--output", help="persist full JSON and print only a concise evidence summary")
    p_finalize = sub.add_parser("finalize-review", allow_abbrev=False)
    p_finalize.add_argument("run_dir")
    p_finalize.add_argument("--manifest", required=True)
    args = parser.parse_args(argv)

    errors: list[str] = []
    if args.cmd == "validate-tags":
        errors.extend(validate_tag(args.implementation_tag, e2e=False))
        errors.extend(validate_tag(args.e2e_tag, e2e=True))
    elif args.cmd == "validate-task-body":
        for path in args.paths:
            errors.extend(validate_body(Path(path)))
            errors.extend(validate_high_compute_body(Path(path)))
    elif args.cmd == "validate-high-compute-body":
        for path in args.paths:
            errors.extend(validate_high_compute_body(Path(path)))
    elif args.cmd == "validate-manifest":
        errors.extend(manifest_errors(load_manifest(Path(args.manifest))))
    elif args.cmd == "render-workflow":
        manifest = load_manifest(Path(args.manifest))
        errors.extend(manifest_errors(manifest))
        if not errors:
            Path(args.output).write_text(render_workflow(manifest), encoding="utf-8")
            print(f"wrote {args.output}")
    elif args.cmd == "create-task-set":
        try:
            receipt = create_task_set(Path(args.manifest), Path(args.output_dir), execute=args.execute, kanban_wrapper=args.kanban_wrapper)
        except TaskSetCreationError as exc:
            print(f"FAIL: {exc}", file=sys.stderr)
            return 1
        print(f"mode={receipt['mode']} status={receipt['status']} receipt={Path(args.output_dir).resolve() / 'receipt.json'}")
    elif args.cmd == "verify-mutation-claims":
        receipt = verify_mutation_claims(load_manifest(Path(args.claims)), Path(args.output), args.kanban_wrapper)
        print(f"status={receipt['status']} claims={len(receipt['results'])} receipt={Path(args.output).resolve()}")
        if receipt["status"] != "verified":
            return 1
    elif args.cmd == "workflow-review-packet":
        task_ids = [x.strip() for x in args.tasks.split(",") if x.strip()]
        try:
            packet = workflow_packet(
                Path(args.run_dir), task_ids, args.e2e_tag or None,
                Path(args.manifest).resolve() if args.manifest else None,
            )
        except FinalizeReviewError as exc:
            print(f"FAIL: {exc}", file=sys.stderr)
            return 1
        if args.output:
            output = Path(args.output).resolve()
            atomic_write_json(output, packet)
            print(packet_summary(packet, output))
        else:
            print(json.dumps(packet, indent=2))
    elif args.cmd == "finalize-review":
        try:
            artifacts = finalize_review(Path(args.run_dir), Path(args.manifest))
        except FinalizeReviewError as exc:
            print(f"FAIL: {exc}", file=sys.stderr)
            return 1
        print("evidence_only=true human_acceptance_required=true")
        for key in ("doctor", "packet", "command_manifest", "readme"):
            print(f"{key}: {artifacts[key]}")
    if errors:
        for error in errors:
            print(f"FAIL: {error}", file=sys.stderr)
        return 1
    if args.cmd not in {"create-task-set", "verify-mutation-claims", "workflow-review-packet", "finalize-review"}:
        print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))


# --- Minimum-RC shared lifecycle contracts (typed engines remain task/merge owners) ---
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import shutil
import urllib.parse
import fcntl
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Optional

COMMAND_CLOSURE_SCHEMA = "juno_command_input_closure.v5"
COMMAND_CLOSURE_DECLARATION_SCHEMA = "juno_validation_input_declaration.v1"
COMMAND_OUTCOME_SCHEMA = "juno_canonical_validation_receipt.v1"
COMMAND_RESULT_SCHEMA = "juno_canonical_command_result.v1"
COMMAND_RESULT_INDEX_SCHEMA = "juno_canonical_command_result_index.v1"
COMMAND_RESULT_PRODUCER_SCHEMA = "juno_canonical_command_result_producer.v1"
CHANGED_INPUT_ATTRIBUTION_LIMIT = 64
COMPLETE_INPUT_IDENTITY_SCHEMA = "juno_complete_input_closure_identity.v1"
REPLAY_TRACE_SCHEMA = "juno_evidence_replay_trace.v1"
COMMAND_DECISION_SCHEMA = "juno_command_evidence_decision.v1"
DOCUMENTATION_ROUTE_SCHEMA = "juno_documentation_validation_route.v1"
COHERENCE_SCHEMA = "juno_grouped_coherence_report.v1"
COMPILED_PLAN_SCHEMA = "juno_compiled_lifecycle_plan.v1"
RUN_PROJECTION_SCHEMA = "juno_lifecycle_run_projection.v1"
RUN_SUMMARY_SCHEMA = "juno_lifecycle_run_summary.v1"
ACTIVE_DOC_COMMAND_ID = "active-documentation-audit"
ACTIVE_DOC_ARGV = ["@juno/active-documentation-audit"]
INTEGRITY_SCHEMA = "juno_parsed_test_result_integrity.v1"

TASK_OPERATIONS = (
    "task.admit", "task.require_implementation_ready", "task.hydrate_exact_lock",
    "task.implementation", "task.closure", "evidence.consume_or_execute",
    "task.attributable_repair", "task.finish",
)
MERGE_OPERATIONS = (
    "merge.freeze_fifo", "merge.compose_or_refresh", "evidence.consume_or_execute",
    "merge.grouped_coherence", "merge.classify_risk", "merge.review_and_suite",
    "merge.cas_and_finalize",
)


class LifecycleContractError(RuntimeError):
    pass


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def file_sha(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def atomic_json(path: Path, value: dict[str, Any], *, exclusive: bool = False) -> dict[str, str]:
    payload = canonical_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    if exclusive:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if path.read_bytes() != payload:
                raise LifecycleContractError(f"immutable lifecycle artifact collision: {path}")
            return {"path": str(path.resolve()), "sha256": hashlib.sha256(payload).hexdigest()}
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload); stream.flush(); os.fsync(stream.fileno())
    else:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(payload).hexdigest()}


@contextmanager
def lifecycle_claim(path: Path) -> Any:
    """Serialize one logical lifecycle identity for the complete invocation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


def lifecycle_journal_write(path: Path, journal: dict[str, Any]) -> None:
    journal["journal_revision"] = int(journal.get("journal_revision", 0)) + 1
    journal["updated_at_unix_ns"] = time.time_ns()
    atomic_json(path, journal)


def lifecycle_checkpoint(path: Path, journal: dict[str, Any], *, phase: str,
                         boundary: str, detail: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    if boundary not in {"PRE", "POST", "PAUSED", "ERROR", "RECOVERED"}:
        raise LifecycleContractError("invalid lifecycle checkpoint boundary")
    events = journal.setdefault("events", [])
    if not isinstance(events, list) or len(events) >= 512:
        raise LifecycleContractError("lifecycle journal event bound exhausted")
    event = {"schema_version": "juno_lifecycle_phase_checkpoint.v1",
             "sequence": len(events) + 1, "phase": phase, "boundary": boundary,
             "recorded_at_unix_ns": time.time_ns(), "detail": detail or {}}
    events.append(event)
    lifecycle_journal_write(path, journal)
    return event


def lifecycle_remaining_seconds(journal: dict[str, Any]) -> int:
    deadline = journal.get("deadline_unix_ns")
    if not isinstance(deadline, int):
        raise LifecycleContractError("lifecycle journal deadline is malformed")
    remaining_ns = deadline - time.time_ns()
    if remaining_ns <= 0:
        raise LifecycleContractError("lifecycle cumulative wall budget exhausted")
    return max(1, remaining_ns // 1_000_000_000)


def lifecycle_elapsed_started(journal: dict[str, Any]) -> float:
    started_ns = journal.get("started_at_unix_ns")
    elapsed = max(0.0, (time.time_ns() - int(started_ns)) / 1_000_000_000)
    return time.monotonic() - elapsed


def _git(root: Path, *args: str, binary: bool = False) -> Any:
    result = subprocess.run(["git", "-C", str(root), *args], cwd=root,
                            stdin=subprocess.DEVNULL, capture_output=True,
                            text=not binary)
    if result.returncode:
        detail = result.stderr if not binary else result.stderr.decode(errors="replace")
        raise LifecycleContractError(detail.strip() or f"git command failed: {args!r}")
    return result.stdout


def repository_identity(repository: Path) -> str:
    return str(Path(_git(repository, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()).resolve())


def _blob(repository: Path, head: str, relative: str) -> Optional[str]:
    result = subprocess.run(["git", "-C", str(repository), "rev-parse", f"{head}:{relative}"],
                            cwd=repository, stdin=subprocess.DEVNULL,
                            text=True, capture_output=True)
    value = result.stdout.strip()
    return value if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40,64}", value) else None


COMMAND_ENVIRONMENT_ALLOWLIST = (
    "PATH", "HOME", "LANG", "LC_ALL", "TZ", "CI", "NODE_ENV", "NODE_OPTIONS",
    "TMPDIR", "TMP", "TEMP", "XDG_CONFIG_HOME", "NPM_CONFIG_CACHE",
    "npm_config_cache", "NPM_CONFIG_REGISTRY", "npm_config_registry",
    "NPM_CONFIG_USERCONFIG", "npm_config_userconfig",
)


def admitted_command_environment(source: Optional[dict[str, str]] = None) -> dict[str, str]:
    """Return the small, explicit environment that validation may observe.

    Absence is represented by omission. Values are bounded so a hostile parent
    cannot turn a receipt into an environment dump.
    """
    source = os.environ if source is None else source
    admitted: dict[str, str] = {}
    for name in COMMAND_ENVIRONMENT_ALLOWLIST:
        value = source.get(name)
        if isinstance(value, str) and len(value.encode("utf-8", errors="replace")) <= 4096:
            admitted[name] = value
    return dict(sorted(admitted.items()))


def _stream_sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def _executable_identity(name: str, environment: dict[str, str]) -> dict[str, Any]:
    resolved = shutil.which(name, path=environment.get("PATH"))
    if not resolved:
        return {"name": name, "available": False, "_host_path": None}
    path = Path(resolved).resolve()
    try:
        stat_result = path.stat()
        return {"name": name, "available": path.is_file(),
                "mode": stat_result.st_mode & 0o7777, "bytes": stat_result.st_size,
                "sha256": _stream_sha256(path) if path.is_file() else None,
                "_host_path": str(path)}
    except OSError:
        return {"name": name, "available": False, "_host_path": str(path)}


def _bounded_version(executable: dict[str, Any], environment: dict[str, str]) -> Optional[str]:
    path = executable.get("_host_path")
    if not executable.get("available") or not isinstance(path, str):
        return None
    try:
        result = subprocess.run([path, "--version"], stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=5, env=environment)
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = (result.stdout or result.stderr)[:4096]
    return output.decode("utf-8", errors="replace").strip() if result.returncode == 0 else None


def command_execution_environment() -> dict[str, str]:
    """The exact allowlisted ambient environment used by validation children."""
    return admitted_command_environment()


def _within_declared(path: str, roots: list[str]) -> bool:
    return any(path == root or path.startswith(root + "/") for root in roots)


def _compile_declared_inputs(repository: Path, head: str, input_paths: list[str],
                             owned_roots: list[str]) -> tuple[dict[str, str], list[dict[str, str]], Optional[dict[str, str]]]:
    """Compile tracked declared roots; ambiguity is a typed whole-tree fallback."""
    local_inputs: dict[str, str] = {}
    gitlinks: list[dict[str, str]] = []
    for declared in input_paths:
        pure = PurePosixPath(declared)
        if (pure.is_absolute() or pure.as_posix() != declared or ".." in pure.parts
                or not _within_declared(declared, owned_roots)):
            return {}, [], {"code": "closure_unknown", "reason": "unsafe_or_unowned_input",
                            "path": declared}
        output = _git(repository, "ls-tree", "-r", "-z", head, "--", declared)
        entries = [entry for entry in output.split("\0") if entry]
        if not entries:
            return {}, [], {"code": "closure_unknown", "reason": "missing_declared_input",
                            "path": declared}
        for entry in entries:
            metadata, separator, relative = entry.partition("\t")
            fields = metadata.split()
            if not separator or len(fields) != 3:
                return {}, [], {"code": "closure_unknown", "reason": "malformed_git_input",
                                "path": declared}
            mode, kind, oid = fields
            if not _within_declared(relative, owned_roots):
                return {}, [], {"code": "closure_unknown", "reason": "input_escapes_owned_roots",
                                "path": relative}
            if mode == "160000" or kind == "commit":
                gitlinks.append({"path": relative, "commit": oid})
                local_inputs[relative] = oid
                continue
            if mode == "120000":
                target = _git(repository, "show", f"{head}:{relative}").rstrip("\n")
                resolved = (PurePosixPath(relative).parent / target)
                normalized_parts: list[str] = []
                escaped = resolved.is_absolute()
                for part in resolved.parts:
                    if part in {"", "."}: continue
                    if part == "..":
                        if not normalized_parts: escaped = True
                        else: normalized_parts.pop()
                    else: normalized_parts.append(part)
                normalized = "/".join(normalized_parts)
                if escaped or not _within_declared(normalized, owned_roots):
                    return {}, [], {"code": "closure_unknown",
                                    "reason": "symlink_escapes_owned_roots", "path": relative}
                if not any(normalized == item or normalized.startswith(item + "/")
                           for item in input_paths):
                    return {}, [], {"code": "closure_unknown",
                                    "reason": "symlink_target_not_declared", "path": relative}
                if _blob(repository, head, normalized) is None and not _git(
                        repository, "ls-tree", head, "--", normalized).strip():
                    return {}, [], {"code": "closure_unknown",
                                    "reason": "symlink_target_missing", "path": relative}
            local_inputs[relative] = oid
    return dict(sorted(local_inputs.items())), sorted(gitlinks, key=lambda row: row["path"]), None


def command_closure(repository: Path, head: str, row: dict[str, Any], *,
                    config_sha256: str, policy_sha256: Optional[str],
                    runtime_sha256: str, environment: Optional[dict[str, str]] = None,
                    producer: str = "task_workspace.run_validation.v3",
                    owned_roots: Optional[list[str]] = None) -> dict[str, Any]:
    """Build a deterministic command closure, narrowing only from declarations."""
    cwd = str(row.get("cwd", "")).strip("/")
    tree = _git(repository, "rev-parse", f"{head}^{{tree}}").strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", tree):
        raise RuntimeError("command closure cannot resolve the candidate tree")
    roots = sorted(set(owned_roots or []))
    declared = row.get("input_paths")
    closure_unknown: Optional[dict[str, str]] = None
    local_inputs: dict[str, str] = {}
    gitlinks: list[dict[str, str]] = []
    if roots and isinstance(declared, list) and declared:
        local_inputs, gitlinks, closure_unknown = _compile_declared_inputs(
            repository, head, declared, roots)
    else:
        closure_unknown = {"code": "closure_unknown", "reason": "inputs_not_declared"}
    observation_scope = "declared_inputs" if closure_unknown is None else "whole_tree"
    manifest_names = {
        "package.json", "package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml",
        "yarn.lock", "pyproject.toml", "uv.lock", "poetry.lock", "requirements.txt",
        ".npmrc", "tsconfig.json", "vitest.config.ts", "tsup.config.ts"}
    locks = {path: oid for path, oid in local_inputs.items()
             if PurePosixPath(path).name in manifest_names}
    if observation_scope == "whole_tree":
        for name in sorted(manifest_names):
            relative = f"{cwd}/{name}" if cwd else name
            identity = _blob(repository, head, relative)
            if identity: locks[relative] = identity
        for line in _git(repository, "ls-tree", "-r", head).splitlines():
            metadata, separator, relative = line.partition("\t")
            fields = metadata.split()
            if separator and len(fields) == 3 and fields[0] == "160000":
                gitlinks.append({"path": relative, "commit": fields[2]})
    command = {key: row.get(key) for key in
               ("id", "cwd", "argv", "timeout_seconds", "max_output_bytes", "resource")
               if key in row}
    admitted_environment = admitted_command_environment(environment)
    argv = row.get("argv") if isinstance(row.get("argv"), list) else []
    executable_names = [str(argv[0])] if argv else []
    if argv and Path(str(argv[0])).name.lower() in {"npm", "npm.cmd", "npx", "npx.cmd"}:
        executable_names.append("node")
    executable_facts = [_executable_identity(name, admitted_environment)
                        for name in dict.fromkeys(executable_names)]
    command_runtime = {item["name"]: {"version": _bounded_version(item, admitted_environment),
                                      "executable_sha256": item.get("sha256")}
                       for item in executable_facts}
    executables = [{key: value for key, value in item.items() if key != "_host_path"}
                   for item in executable_facts]
    body = {
        "schema_version": COMMAND_CLOSURE_SCHEMA,
        "declaration_schema": COMMAND_CLOSURE_DECLARATION_SCHEMA,
        "outcome_schema": COMMAND_OUTCOME_SCHEMA,
        "command": command,
        "command_sha256": digest(command),
        "observation_scope": observation_scope,
        "observable_tree": tree if observation_scope == "whole_tree" else None,
        "declared_owned_roots": roots,
        "declared_input_paths": list(declared) if isinstance(declared, list) else [],
        "local_inputs": local_inputs,
        "dependency_locks": locks,
        "gitlinks": gitlinks,
        "closure_unknown": closure_unknown,
        "routing_config_sha256": config_sha256,
        "risk_policy_sha256": policy_sha256,
        "runtime_sha256": runtime_sha256,
        "runner": {"class": producer, "python": list(__import__("sys").version_info[:3]),
                   "platform": __import__("sys").platform},
        "environment_allowlist": list(COMMAND_ENVIRONMENT_ALLOWLIST),
        "environment": admitted_environment,
        "executables": executables,
        "command_runtime": command_runtime,
    }
    return {**body, "input_closure_sha256": digest(body)}


def complete_input_identity(closure: Any) -> dict[str, str]:
    """Sign one complete input closure with a deterministic self-verifying digest.

    This is an integrity signature, not an authority grant.  The canonical
    controller still owns authority and receipt storage; the signature makes a
    copied, partial, or field-edited closure impossible to present as exact.
    """
    if not isinstance(closure, dict):
        raise LifecycleContractError("complete input closure is missing or malformed")
    claimed = closure.get("input_closure_sha256")
    body = {key: value for key, value in closure.items() if key != "input_closure_sha256"}
    computed = digest(body)
    if not isinstance(claimed, str) or claimed != computed:
        raise LifecycleContractError("complete input closure digest is forged or partial")
    schema = closure.get("schema_version")
    if schema != COMMAND_CLOSURE_SCHEMA:
        raise LifecycleContractError("complete input closure schema is unsupported")
    signature_body = {"schema_version": COMPLETE_INPUT_IDENTITY_SCHEMA,
                      "closure_schema": schema, "input_closure_sha256": computed}
    return {**signature_body, "signature_sha256": digest(signature_body)}


def canonical_command_result_identity(repository: Path,
                                      closure: dict[str, Any]) -> dict[str, str]:
    """Return the only command-result key used by task and queue phases."""
    complete = complete_input_identity(closure)
    body = {"schema_version": COMMAND_RESULT_INDEX_SCHEMA,
            "repository_identity": repository_identity(repository),
            "complete_input_identity": complete}
    return {**body, "index_sha256": digest(body)}


def canonical_command_result_path(root: Path, repository: Path,
                                  closure: dict[str, Any]) -> Path:
    identity = canonical_command_result_identity(repository, closure)
    repository_sha = hashlib.sha256(identity["repository_identity"].encode()).hexdigest()
    return root / repository_sha / identity["index_sha256"] / "result.json"


def _terminal_result_verdict(result: Any) -> str:
    if (not isinstance(result, dict) or not isinstance(result.get("exit_code"), int)
            or not isinstance(result.get("timed_out"), bool)
            or result.get("cancelled") is True):
        return "UNSETTLED"
    integrity = result.get("result_integrity")
    if isinstance(integrity, dict) and integrity.get("eligible_pass") is False:
        return "FAILED"
    return "PASSED" if result["exit_code"] == 0 and not result["timed_out"] else "FAILED"


def verify_canonical_command_result(receipt: Any, repository: Path,
                                    closure: dict[str, Any]) -> dict[str, Any]:
    """Strictly reopen one terminal result; a consumer never redefines its key."""
    if not isinstance(receipt, dict):
        raise LifecycleContractError("canonical command result is missing or malformed")
    expected_keys = {"schema_version", "producer", "index_identity", "index_sha256",
                     "command_id", "command", "input_closure", "complete_input_identity",
                     "outcome_identity", "result", "source", "recorded_at_unix_ns",
                     "receipt_body_sha256"}
    identity = canonical_command_result_identity(repository, closure)
    verification = verify_complete_input_closure(
        receipt.get("input_closure"), closure, receipt.get("complete_input_identity"))
    result = receipt.get("result")
    verdict = _terminal_result_verdict(result)
    outcome = {"schema_version": closure.get("outcome_schema"),
               "result_sha256": digest(result), "verdict": verdict}
    producer = receipt.get("producer")
    receipt_body = {key: value for key, value in receipt.items()
                    if key != "receipt_body_sha256"}
    if (set(receipt) != expected_keys
            or receipt.get("receipt_body_sha256") != digest(receipt_body)
            or receipt.get("schema_version") != COMMAND_RESULT_SCHEMA
            or not isinstance(producer, dict)
            or producer.get("schema_version") != COMMAND_RESULT_PRODUCER_SCHEMA
            or receipt.get("index_identity") != identity
            or receipt.get("index_sha256") != identity["index_sha256"]
            or receipt.get("command") != closure.get("command")
            or receipt.get("command_id") != closure.get("command", {}).get("id")
            or not verification["valid"]
            or receipt.get("outcome_identity") != outcome
            or verdict == "UNSETTLED"):
        raise LifecycleContractError("canonical command result identity or terminal outcome is invalid")
    return receipt


def consume_or_execute_command_result(
        root: Path, repository: Path, closure: dict[str, Any],
        execute: Callable[[], dict[str, Any]], *, phase: str, task_id: Optional[str] = None,
        legacy: Optional[tuple[dict[str, Any], dict[str, Any]]] = None) -> dict[str, Any]:
    """Consume or produce one immutable terminal result under one canonical index.

    The identity lock spans lookup and execution, so checkpoint, finish, queue,
    refresh, and resume cannot independently launch the same exact command.
    ``legacy`` is a finite read/import seam: the original bytes remain untouched
    and their exact reference is retained in the canonical producer record.
    """
    path = canonical_command_result_path(root, repository, closure)
    lock_path = path.with_name(".claim.lock")
    with lifecycle_claim(lock_path):
        if path.exists():
            try:
                raw = path.read_bytes(); receipt = json.loads(raw)
            except (OSError, json.JSONDecodeError) as exc:
                raise LifecycleContractError("canonical command result is unreadable") from exc
            verify_canonical_command_result(receipt, repository, closure)
            decision = ("failure_stands" if receipt.get("outcome_identity", {}).get("verdict") == "FAILED"
                        else "reused")
            return {"decision": decision, "receipt": receipt,
                    "reference": {"path": str(path.resolve()),
                                  "sha256": hashlib.sha256(raw).hexdigest(),
                                  "command_id": receipt["command_id"]}}
        source: Optional[dict[str, Any]] = None
        if legacy is not None:
            legacy_receipt, legacy_reference = legacy
            admitted = {"juno_standing_validation_evidence.v1",
                        "juno_canonical_validation_receipt.v1"}
            verification = verify_complete_input_closure(
                legacy_receipt.get("input_closure"), closure,
                legacy_receipt.get("complete_input_identity"))
            legacy_result = legacy_receipt.get("result")
            source_path = Path(str(legacy_reference.get("path", "")))
            try: source_raw = source_path.read_bytes()
            except OSError as exc:
                raise LifecycleContractError("legacy command result source is unavailable") from exc
            try:
                preserved_legacy = json.loads(source_raw)
            except json.JSONDecodeError as exc:
                raise LifecycleContractError("legacy command result source is malformed") from exc
            if (legacy_receipt.get("schema_version") not in admitted
                    or not verification["valid"]
                    or hashlib.sha256(source_raw).hexdigest() != legacy_reference.get("sha256")
                    or preserved_legacy != legacy_receipt
                    or _terminal_result_verdict(legacy_result) == "UNSETTLED"):
                raise LifecycleContractError("legacy command result cannot be verified for import")
            result = legacy_result
            source = {"kind": "legacy_import", "schema_version": legacy_receipt["schema_version"],
                      "path": str(source_path.resolve()),
                      "sha256": legacy_reference["sha256"]}
            decision = ("failure_stands" if _terminal_result_verdict(result) == "FAILED"
                        else "reused")
        else:
            result = execute()
            if _terminal_result_verdict(result) == "UNSETTLED":
                raise LifecycleContractError("validation command did not produce a settled terminal result")
            decision = "executed"
        identity = canonical_command_result_identity(repository, closure)
        outcome = {"schema_version": closure.get("outcome_schema"),
                   "result_sha256": digest(result),
                   "verdict": _terminal_result_verdict(result)}
        receipt_body = {"schema_version": COMMAND_RESULT_SCHEMA,
                        "producer": {"schema_version": COMMAND_RESULT_PRODUCER_SCHEMA,
                                     "implementation": "task_workflow_helper.consume_or_execute_command_result.v1",
                                     "phase": phase, "task_id": task_id},
                        "index_identity": identity, "index_sha256": identity["index_sha256"],
                        "command_id": closure.get("command", {}).get("id"),
                        "command": closure.get("command"), "input_closure": closure,
                        "complete_input_identity": complete_input_identity(closure),
                        "outcome_identity": outcome, "result": result, "source": source,
                        "recorded_at_unix_ns": time.time_ns()}
        receipt = {**receipt_body, "receipt_body_sha256": digest(receipt_body)}
        reference = atomic_json(path, receipt, exclusive=True)
        return {"decision": decision, "receipt": receipt,
                "reference": {"path": reference["path"], "sha256": reference["sha256"],
                              "command_id": receipt["command_id"]}}


def verify_complete_input_closure(previous: Any, current: Any,
                                  identity: Any) -> dict[str, Any]:
    """One fail-closed verifier shared by task, queue, refresh, and train stages."""
    reasons: list[dict[str, Any]] = []
    try:
        expected_identity = complete_input_identity(previous)
    except LifecycleContractError as exc:
        reasons.append({"code": "CLOSURE_MISSING_FORGED_OR_PARTIAL", "field": "closure",
                        "reason": str(exc)})
        expected_identity = None
    if not isinstance(identity, dict):
        reasons.append({"code": "CLOSURE_SIGNATURE_MISSING", "field": "signature"})
    elif expected_identity is not None and identity != expected_identity:
        code = ("CLOSURE_SCHEMA_UNSUPPORTED" if identity.get("closure_schema") != COMMAND_CLOSURE_SCHEMA
                else "CLOSURE_SIGNATURE_MISMATCH")
        reasons.append({"code": code, "field": "signature",
                        "old_sha256": digest(identity), "new_sha256": digest(expected_identity)})
    reasons.extend(closure_invalidation(previous, current))
    return {"valid": not reasons, "reasons": reasons,
            "identity": expected_identity,
            "input_closure_sha256": (current.get("input_closure_sha256")
                                      if isinstance(current, dict) else None)}


def closure_invalidation(previous: Any, current: Any) -> list[dict[str, Any]]:
    if not isinstance(previous, dict) or not isinstance(current, dict):
        return [{"code": "CLOSURE_MISSING_OR_MALFORMED", "field": "closure",
                 "old": None, "new": None, "reason": "missing_or_malformed"}]
    ignored = {"input_closure_sha256"}
    rows = []
    for key in sorted((set(previous) | set(current)) - ignored):
        if previous.get(key) != current.get(key):
            row = {"code": "CLOSURE_DRIFT_" + key.upper(), "field": key,
                   "old_sha256": digest(previous.get(key)),
                   "new_sha256": digest(current.get(key))}
            if key == "local_inputs" and isinstance(previous.get(key), dict) and isinstance(current.get(key), dict):
                changed = sorted(path for path in set(previous[key]) | set(current[key])
                                 if previous[key].get(path) != current[key].get(path))
                row["changed_inputs"] = changed[:CHANGED_INPUT_ATTRIBUTION_LIMIT]
                row["changed_input_count"] = len(changed)
                row["changed_inputs_truncated"] = len(changed) > CHANGED_INPUT_ATTRIBUTION_LIMIT
            rows.append(row)
    if not rows and previous.get("input_closure_sha256") != current.get("input_closure_sha256"):
        rows.append({"code": "CLOSURE_DIGEST_MISMATCH", "field": "input_closure_sha256",
                     "old": previous.get("input_closure_sha256"),
                     "new": current.get("input_closure_sha256")})
    return rows


_RESTART_STAGE_BY_FIELD = {
    "composition": "COMPOSING", "candidate_order": "COMPOSING",
    "review": "REVIEWING", "review_policy": "REVIEWING",
    "closure": "VALIDATING", "signature": "VALIDATING",
    "schema_version": "VALIDATING", "observable_tree": "VALIDATING",
    "dependency_locks": "VALIDATING", "gitlinks": "VALIDATING",
    "local_inputs": "VALIDATING", "declared_input_paths": "VALIDATING",
    "declared_owned_roots": "VALIDATING", "closure_unknown": "VALIDATING",
    "routing_config_sha256": "VALIDATING", "risk_policy_sha256": "VALIDATING",
    "runtime_sha256": "VALIDATING", "runner": "VALIDATING",
    "command": "VALIDATING", "command_sha256": "VALIDATING",
    "environment": "VALIDATING", "executables": "VALIDATING",
    "command_runtime": "VALIDATING", "selection": "VALIDATING",
}


def earliest_safe_restart_stage(reasons: list[dict[str, Any]]) -> str:
    """Map invalidation/corruption to the smallest deterministic restart stage."""
    order = {"COMPOSING": 0, "VALIDATING": 1, "REVIEWING": 2, "READY_CAS": 3}
    stages = [_RESTART_STAGE_BY_FIELD.get(str(row.get("field")), "VALIDATING")
              for row in reasons if isinstance(row, dict)]
    return min(stages or ["READY_CAS"], key=order.__getitem__)


def evidence_replay_trace(decisions: list[dict[str, Any]], *, phase: str) -> dict[str, Any]:
    invalidation = [reason for row in decisions if isinstance(row, dict)
                    for reason in row.get("invalidation", []) if isinstance(reason, dict)]
    return {"schema_version": REPLAY_TRACE_SCHEMA, "phase": phase,
            "restart_stage": earliest_safe_restart_stage(invalidation),
            "decisions": decisions, "counters": evidence_counters(decisions),
            "model_wakeups": sum(1 for row in decisions
                                 if isinstance(row, dict) and row.get("decision") == "model_wakeup")}


def evidence_decision(command_id: str, decision: str, *, closure: dict[str, Any],
                      source: Optional[dict[str, Any]] = None,
                      invalidation: Optional[list[dict[str, Any]]] = None,
                      reason: Optional[str] = None) -> dict[str, Any]:
    if decision not in {"executed", "reused", "failure_stands", "invalidated", "unknown", "skipped", "not_applicable"}:
        raise LifecycleContractError(f"invalid command evidence decision: {decision}")
    return {"schema_version": COMMAND_DECISION_SCHEMA, "command_id": command_id,
            "decision": decision, "input_closure_sha256": closure.get("input_closure_sha256"),
            "source": source, "invalidation": invalidation or [], "reason": reason}


def evidence_counters(decisions: list[dict[str, Any]]) -> dict[str, int]:
    result = {name: 0 for name in ("executed", "reused", "invalidated", "unknown", "skipped", "not_applicable")}
    for row in decisions:
        decision = row.get("decision")
        if decision == "failure_stands":
            result["reused"] += 1
        elif decision in result:
            result[decision] += 1
    return result


def changed_path_status(repository: Path, base: str, head: str) -> list[dict[str, str]]:
    raw = _git(repository, "diff", "--name-status", "--no-renames", "-z", f"{base}..{head}", binary=True)
    parts = raw.split(b"\0")
    rows = []
    index = 0
    while index + 1 < len(parts) and parts[index]:
        status = parts[index].decode("ascii", errors="replace")
        path = parts[index + 1].decode("utf-8", errors="strict")
        index += 2
        rows.append({"status": status, "path": path})
    return rows


def default_documentation_policy() -> dict[str, Any]:
    return {
        "schema_version": "juno_documentation_validation_policy.v1",
        "inert_exact_files": ["AGENTS.md", "CLAUDE.md"],
        "inert_roots": [".juno_task/wiki"],
        "active_exact_files": ["README.md", "juno-code/README.md", "juno-benchmark/README.md"],
        "active_roots": ["docs", "juno-code/docs", "juno-benchmark/docs"],
        "active_name_patterns": [r"(^|/)(CHANGELOG|RELEASE_NOTES)(\.[^/]*)?$", r"(^|/)MIGRATION[^/]*\.md$"],
        "public_identities": [
            "https://github.com/askbudi/juno-mono", "https://github.com/yylo-dev/yylo",
            "https://github.com/yylo-dev/yylo-benchmark", "https://github.com/yylo-dev/yylo-ledger",
            "https://www.npmjs.com/package/%40yylo%2Fcli",
            "https://www.npmjs.com/package/%40yylo%2Fbenchmark",
            "https://pypi.org/project/yylo-ledger",
        ],
        "cli_top_level": [
            "auth", "benchmark", "branches", "cc", "clone", "completion", "continue",
            "continue-scope", "continuity", "doctor", "evidence", "feedback", "help", "info",
            "init", "integration", "kanban", "ledger", "logs", "loop", "merge", "migrate", "pi",
            "release", "scripts", "services", "session", "setup-git", "skills", "start",
            "switch", "task", "test", "view-log", "watch", "where", "wiki",
        ],
    }


def documentation_route(path_rows: list[dict[str, str]], policy: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    policy = policy or default_documentation_policy()
    inert: list[str] = []
    active: list[str] = []
    unsafe: list[dict[str, str]] = []
    exact_inert = set(policy.get("inert_exact_files", []))
    exact_active = set(policy.get("active_exact_files", []))
    inert_roots = tuple(str(root).rstrip("/") + "/" for root in policy.get("inert_roots", []))
    active_roots = tuple(str(root).rstrip("/") + "/" for root in policy.get("active_roots", []))
    patterns = [re.compile(value) for value in policy.get("active_name_patterns", [])]
    for row in path_rows:
        path, status = row.get("path", ""), row.get("status", "")
        posix = PurePosixPath(path)
        if (not path or posix.is_absolute() or ".." in posix.parts or status not in {"A", "M"}
                or path.endswith((".json", ".yaml", ".yml", ".toml", ".lock", ".sh", ".py", ".js", ".ts"))):
            unsafe.append({"path": path, "reason": "unsafe_status_or_surface"})
            continue
        is_inert = path in exact_inert or path.startswith(inert_roots)
        is_active = (path in exact_active or path.startswith(active_roots)
                     or any(pattern.search(path) for pattern in patterns))
        if is_inert and is_active:
            unsafe.append({"path": path, "reason": "overlapping_documentation_classes"})
        elif is_inert:
            inert.append(path)
        elif is_active:
            active.append(path)
        else:
            unsafe.append({"path": path, "reason": "unreviewed_path"})
    if not path_rows or unsafe or (inert and active):
        mode = "fallback"
    elif active:
        mode = "active_audit"
    else:
        mode = "inert_zero_command"
    body = {"schema_version": DOCUMENTATION_ROUTE_SCHEMA, "mode": mode,
            "inert_paths": sorted(inert), "active_paths": sorted(active),
            "fallback_reasons": unsafe, "policy_sha256": digest(policy),
            "authored_path_count": len(path_rows)}
    return {**body, "route_sha256": digest(body)}


def active_documentation_row() -> dict[str, Any]:
    return {"id": ACTIVE_DOC_COMMAND_ID, "cwd": "juno-code", "argv": ACTIVE_DOC_ARGV,
            "timeout_seconds": 30, "max_output_bytes": 32768}


def _git_file(repository: Path, head: str, path: str) -> Optional[bytes]:
    result = subprocess.run(["git", "-C", str(repository), "show", f"{head}:{path}"],
                            cwd=repository, stdin=subprocess.DEVNULL, capture_output=True)
    return result.stdout if result.returncode == 0 else None


def _documentation_cli_identity(repository: Path, head: str) -> dict[str, Any]:
    tree = _git(repository, "ls-tree", "-r", "--name-only", head).splitlines()
    sources = sorted(path for path in tree if (path == "juno-code/src/bin/cli.ts"
                                               or (path.startswith("juno-code/src/cli/commands/")
                                                   and path.endswith(".ts"))))
    source_bytes = {path: _git_file(repository, head, path) or b"" for path in sources}
    all_text = b"\n".join(source_bytes.values()).decode("utf-8", errors="replace")
    options = sorted(set(re.findall(r"(?:requiredOption|option)\(\s*['\"](--[a-z0-9-]+)",
                                    all_text, re.I)))
    namespaces: dict[str, list[str]] = {}
    for namespace in ("task", "merge", "evidence", "integration", "migrate", "watch"):
        raw = source_bytes.get(f"juno-code/src/cli/commands/{namespace}.ts", b"")
        names = re.findall(rb"\.command\(\s*['\"]([a-z][a-z0-9-]*)", raw)
        # Include operation literals used by compact typed loops/unions; values
        # containing prose, spaces, paths, or option syntax are excluded.
        names.extend(re.findall(rb"['\"]([a-z][a-z0-9-]*)['\"]", raw))
        namespaces[namespace] = sorted({item.decode() for item in names if item.decode() != namespace})
    body = {"sources": sources,
            "source_sha256": hashlib.sha256(b"\0".join(
                path.encode() + b"\0" + source_bytes[path] for path in sources)).hexdigest(),
            "options": options, "namespaces": namespaces}
    return body


def _documented_commands(text: str) -> list[list[str]]:
    snippets = re.findall(r"(?ms)^```(?:bash|sh|shell)[ \t]*\n(.*?)^```[ \t]*$", text)
    snippets.extend(re.findall(r"(?<!`)`([^`\n]+)`(?!`)", text))
    commands: list[list[str]] = []
    for snippet in snippets:
        for line in snippet.splitlines():
            line = line.strip().removeprefix("$ ").strip()
            if not re.match(r"^(?:yy|yylo|ypl)(?:\s|$)", line):
                continue
            try:
                argv = shlex.split(line, comments=True)
            except ValueError:
                # Shell examples may deliberately continue quoted argv onto the
                # next line; only fully tokenized command identities are audited.
                continue
            if argv:
                commands.append(argv[:64])
    return commands


def _known_schema_identities(repository: Path, head: str) -> set[str]:
    result = subprocess.run(
        ["git", "-C", str(repository), "grep", "-h", "-o", "-E",
         r"juno_[a-zA-Z0-9_.-]+\.v[0-9]+", head, "--", "*.py", "*.ts", "*.json", "*.yaml", "*.yml"],
        cwd=repository, stdin=subprocess.DEVNULL, capture_output=True)
    if result.returncode not in {0, 1} or len(result.stdout) > 4 * 1024 * 1024:
        return set()
    return set(result.stdout.decode("utf-8", errors="replace").splitlines())


def active_documentation_audit(repository: Path, head: str, paths: list[str],
                               policy: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Cheap offline audit of executable product guidance and public identities."""
    policy = policy or default_documentation_policy()
    findings: list[dict[str, Any]] = []
    link_re = re.compile(r"(?<!!)\[[^\]]+\]\(([^)\s]+)(?:\s+['\"][^'\"]*['\"])?\)")
    package_names: set[str] = set()
    package_versions: dict[str, str] = {}
    tree_paths = set(_git(repository, "ls-tree", "-r", "--name-only", head).splitlines())
    for manifest_path in ("juno-code/package.json", "juno-benchmark/package.json", "package.json"):
        raw = _git_file(repository, head, manifest_path)
        if raw:
            try:
                package = json.loads(raw)
                if isinstance(package, dict) and isinstance(package.get("name"), str):
                    package_names.add(package["name"])
                    if isinstance(package.get("version"), str):
                        package_versions[package["name"]] = package["version"]
            except (UnicodeDecodeError, json.JSONDecodeError):
                findings.append({"code": "active_doc.package_manifest_malformed", "path": manifest_path})
    cli_identity = _documentation_cli_identity(repository, head)
    configured_top = set(policy.get("cli_top_level", []))
    public_identities = tuple(str(value).rstrip("/") for value in policy.get("public_identities", []))
    known_schemas = _known_schema_identities(repository, head)
    for path in sorted(paths):
        raw = _git_file(repository, head, path)
        if raw is None:
            findings.append({"code": "active_doc.missing", "path": path}); continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            findings.append({"code": "active_doc.non_utf8", "path": path}); continue
        for link in link_re.findall(text):
            target = link.split("#", 1)[0]
            if not target or target.startswith("mailto:"):
                continue
            if re.match(r"https?://", target):
                parsed = urllib.parse.urlsplit(target)
                if (parsed.scheme not in {"http", "https"} or not parsed.netloc
                        or parsed.username is not None or parsed.password is not None
                        or any(ord(char) < 32 for char in target)):
                    findings.append({"code": "active_doc.invalid_public_link", "path": path,
                                     "target": link})
                elif (public_identities and parsed.netloc.lower() in
                      {"github.com", "www.npmjs.com", "pypi.org"}
                      and not any(target.rstrip("/").startswith(value) for value in public_identities)):
                    findings.append({"code": "active_doc.unknown_public_identity", "path": path,
                                     "target": link})
                continue
            target_path = (PurePosixPath(path).parent / target).as_posix()
            normalized = os.path.normpath(target_path).replace(os.sep, "/")
            exists = (normalized in tree_paths
                      or any(item.startswith(normalized.rstrip("/") + "/")
                             for item in tree_paths))
            if normalized.startswith("../") or not exists:
                findings.append({"code": "active_doc.broken_relative_link", "path": path,
                                 "target": link})
        for coordinate in sorted(set(re.findall(r"@[a-z0-9_.-]+/[a-z0-9_.-]+", text, re.I))):
            if coordinate.startswith("@yylo/") and coordinate not in package_names:
                findings.append({"code": "active_doc.unknown_package_coordinate", "path": path,
                                 "coordinate": coordinate})
        for coordinate, version in re.findall(
                r"npm\s+(?:install|i)(?:\s+-g)?\s+(@[a-z0-9_.-]+/[a-z0-9_.-]+)(?:@([^\s`]+))?",
                text, re.I):
            expected = package_versions.get(coordinate)
            if ((coordinate in package_names and version and expected and version != expected)
                    or (coordinate.startswith("@yylo/") and coordinate not in package_names)):
                findings.append({"code": "active_doc.install_identity_mismatch", "path": path,
                                 "coordinate": coordinate, "documented_version": version or None,
                                 "expected_version": expected})
        for argv in _documented_commands(text):
            top = argv[1] if len(argv) > 1 and not argv[1].startswith("-") else None
            if argv[0] == "yy" and configured_top and top and top not in configured_top:
                findings.append({"code": "active_doc.unknown_cli_command", "path": path,
                                 "command": " ".join(argv[:3])})
            namespace = top if top in cli_identity["namespaces"] else None
            if namespace and len(argv) > 2 and not argv[2].startswith("-"):
                subcommand = argv[2]
                if (re.fullmatch(r"[a-z][a-z0-9-]*", subcommand)
                        and subcommand not in cli_identity["namespaces"][namespace]):
                    findings.append({"code": "active_doc.unknown_cli_subcommand", "path": path,
                                     "command": " ".join(argv[:3])})
            for option in (arg.split("=", 1)[0] for arg in argv if arg.startswith("--")):
                if namespace and option not in cli_identity["options"]:
                    findings.append({"code": "active_doc.unknown_cli_option", "path": path,
                                     "option": option, "command": " ".join(argv[:4])})
        for schema in sorted(set(re.findall(r"juno_[a-zA-Z0-9_.-]+\.v[0-9]+", text))):
            if known_schemas and schema not in known_schemas:
                findings.append({"code": "active_doc.unknown_schema_identity", "path": path,
                                 "schema": schema})
        if re.search(r"(^|/)MIGRATION[^/]*\.md$", path, re.I):
            lowered = text.lower()
            missing = [term for term in ("from", "to", "rollback") if term not in lowered]
            if missing:
                findings.append({"code": "active_doc.incoherent_migration_guidance", "path": path,
                                 "missing": missing})
        if re.search(r"(^|/)(CHANGELOG|RELEASE_NOTES)(\.[^/]*)?$", path, re.I):
            current_versions = set(package_versions.values())
            documented = set(re.findall(r"(?m)^#{1,3}\s+\[?v?([0-9]+\.[0-9]+\.[0-9][0-9A-Za-z.-]*)", text))
            if current_versions and not current_versions.intersection(documented):
                findings.append({"code": "active_doc.release_identity_mismatch", "path": path,
                                 "expected_versions": sorted(current_versions)})
        if "```" in text and text.count("```") % 2:
            findings.append({"code": "active_doc.unclosed_fence", "path": path})
    findings = sorted(findings, key=lambda row: (row["code"], digest(row)))
    body = {"schema_version": "juno_active_documentation_audit.v2", "head": head,
            "paths": sorted(paths), "findings": findings,
            "checks": ["utf8", "relative_links", "public_identities", "install_package_identity",
                       "cli_help_schema_identity", "release_identity", "schema_references",
                       "migration_coherence", "markdown_fences"],
            "policy_sha256": digest(policy), "cli_schema_sha256": cli_identity["source_sha256"]}
    return {**body, "outcome": "PASSED" if not findings else "FAILED",
            "audit_sha256": digest(body)}


_FAILURE_PATTERNS = [
    ("vitest_test_files", re.compile(r"Test Files\s+(\d+)\s+failed", re.I)),
    ("vitest_tests", re.compile(r"Tests\s+(\d+)\s+failed", re.I)),
    ("pytest", re.compile(r"(?:^|\s)(\d+)\s+failed(?:,|\s|$)", re.I)),
    ("jest", re.compile(r"Test Suites:\s+(\d+)\s+failed", re.I)),
]


def parsed_test_result_integrity(argv: list[str], output: Any,
                                 process_exit_code: int) -> dict[str, Any]:
    """Parse recognized summaries across the complete bounded stream.

    Validation logs are disk-backed. Reading only a tail can turn an early test
    failure into PASS after a large diagnostic epilogue, while reading the whole
    log into memory is itself unsafe. This scanner keeps a small overlap, a
    bounded finding list, and a streaming digest.
    """
    stream = output.open("rb") if isinstance(output, Path) else None
    chunks = iter(lambda: stream.read(64 * 1024), b"") if stream is not None else iter((bytes(output),))
    failures: list[dict[str, Any]] = []
    failure_keys: set[tuple[str, int, int]] = set()
    carry = ""
    consumed_chars = 0
    output_bytes = 0
    output_hash = hashlib.sha256()
    try:
        for chunk in chunks:
            output_bytes += len(chunk); output_hash.update(chunk)
            text = carry + chunk.decode("utf-8", errors="replace")
            base = max(0, consumed_chars - len(carry))
            for parser, pattern in _FAILURE_PATTERNS:
                for match in pattern.finditer(text):
                    count = int(match.group(1))
                    key = (parser, count, base + match.start())
                    if count and key not in failure_keys:
                        failure_keys.add(key)
                        if len(failures) < 256:
                            failures.append({"parser": parser, "failed": count,
                                             "stream_offset": key[2]})
            consumed_chars += max(0, len(text) - len(carry))
            carry = text[-4096:]
    finally:
        if stream is not None:
            stream.close()
    contradiction = process_exit_code == 0 and bool(failure_keys)
    body = {"schema_version": INTEGRITY_SCHEMA, "argv_sha256": digest(argv),
            "process_exit_code": process_exit_code, "parsed_failures": failures,
            "parsed_failure_count": len(failure_keys),
            "findings_truncated": len(failure_keys) > len(failures),
            "contradiction": contradiction, "output_bytes": output_bytes,
            "output_sha256": output_hash.hexdigest()}
    return {**body, "eligible_pass": process_exit_code == 0 and not contradiction,
            "integrity_sha256": digest(body)}


def _normalized_bin_delegate(package_root: str, executable: Any) -> str:
    """Canonical repo-relative path for one package bin target (C5EC9p).

    npm bin entries commonly carry a leading "./"; normalize exactly one such
    prefix so committed delegates compare exactly against tree paths.
    """
    if not isinstance(executable, str):
        return ""
    value = executable[2:] if executable.startswith("./") else executable
    value = value.strip()
    return f"{package_root}/{value}" if value else ""


def _gitignore_proves_ignored(lines: list[str], relative: str) -> bool:
    """Strict gitignore subset evaluated against one exact commit (01EUMc).

    Negations fail closed and only the shapes that provably build outputs use
    (blank/comment lines skipped, trailing-slash directory patterns, anchored
    root patterns, unanchored name patterns at any depth) may prove ignoring.
    """
    parts = relative.split("/")
    directory_prefixes = ["/".join(parts[:index]) for index in range(1, len(parts))]
    for raw in lines:
        pattern = raw.strip()
        if not pattern or pattern.startswith("#") or pattern.startswith("!"):
            continue
        directory_only = pattern.endswith("/")
        pattern = pattern.rstrip("/").lstrip("/")
        if not pattern:
            continue
        anchored = "/" in pattern
        if anchored:
            candidates = directory_prefixes if directory_only else directory_prefixes + [relative]
            if any(fnmatch.fnmatch(candidate, pattern) for candidate in candidates):
                return True
        else:
            names = parts[:-1] if directory_only else parts
            if any(fnmatch.fnmatch(name, pattern) for name in names):
                return True
    return False


def _commit_ignored_delegate(repository: Path, head: str, relative: str) -> bool:
    """True when .gitignore bytes at the exact commit prove a build output.

    The proof reads blobs from the task tip commit, never mutable worktree
    state, so a lean sparse controller cannot flip the verdict (01EUMc).
    """
    if not relative:
        return False
    parts = relative.split("/")
    for depth in range(len(parts)):
        prefix = "/".join(parts[:depth])
        ignore_path = f"{prefix}/.gitignore" if prefix else ".gitignore"
        raw = _git_file(repository, head, ignore_path)
        if raw is None:
            continue
        if _gitignore_proves_ignored(raw.decode("utf-8", errors="replace").splitlines(), relative):
            return True
    return False


def grouped_coherence(controller: Path, repository: Path, head: str,
                      changed_paths: list[str], *, active_doc_paths: Optional[list[str]] = None,
                      documentation_policy: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Collect every cheap deterministic finding in one stable report."""
    findings: list[dict[str, Any]] = []
    checked: list[str] = []
    tree_paths = set(_git(repository, "ls-tree", "-r", "--name-only", head).splitlines())
    runtime_prefix = ".juno_task/scripts/"
    template_prefix = "juno-code/src/templates/scripts/"
    managed_runtime_paths: set[str] = set()
    managed_definition_raw = _git_file(
        repository, head, "juno-code/src/templates/managed-assets.json")
    try:
        managed_definition = json.loads(managed_definition_raw or b"null")
        assets = managed_definition.get("assets") if isinstance(managed_definition, dict) else None
        if isinstance(assets, list):
            managed_runtime_paths = {
                asset["destination"] for asset in assets
                if isinstance(asset, dict) and asset.get("installClass") == "script"
                and isinstance(asset.get("destination"), str)
            }
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass
    for path in sorted(changed_paths):
        twin = None
        if path.startswith(runtime_prefix):
            twin = template_prefix + path[len(runtime_prefix):]
        elif path.startswith(template_prefix):
            twin = runtime_prefix + path[len(template_prefix):]
        if twin:
            checked.append("runtime_template")
            left, right = _git_file(repository, head, path), _git_file(repository, head, twin)
            retired_unmanaged_template = (
                path.startswith(template_prefix) and left is None
                and right is not None and twin not in managed_runtime_paths)
            if not retired_unmanaged_template and (left is None or right is None or left != right):
                findings.append({"code": "coherence.runtime_template_mismatch", "path": path,
                                 "twin": twin})
    for root in ("juno-code", "juno-benchmark"):
        manifest_path, lock_path = f"{root}/package.json", f"{root}/package-lock.json"
        if manifest_path not in changed_paths and lock_path not in changed_paths:
            continue
        manifest_raw, lock_raw = _git_file(repository, head, manifest_path), _git_file(repository, head, lock_path)
        if manifest_raw is None and lock_raw is None:
            continue
        checked.append("package_lock")
        try:
            manifest, lock = json.loads(manifest_raw or b"null"), json.loads(lock_raw or b"null")
            package_root = lock.get("packages", {}).get("") if isinstance(lock, dict) else None
            if (not isinstance(manifest, dict) or not isinstance(package_root, dict)
                    or any(manifest.get(key) != package_root.get(key) for key in ("name", "version"))):
                findings.append({"code": "coherence.package_lock_identity", "path": root})
            bins = manifest.get("bin") if isinstance(manifest, dict) else None
            executable_paths = ([bins] if isinstance(bins, str) else
                                list(bins.values()) if isinstance(bins, dict) else [])
            for executable in executable_paths:
                normalized = _normalized_bin_delegate(root, executable)
                if (normalized not in tree_paths
                        and not _commit_ignored_delegate(repository, head, normalized)):
                    findings.append({"code": "coherence.executable_delegate_missing",
                                     "path": normalized or manifest_path})
        except (UnicodeDecodeError, json.JSONDecodeError):
            findings.append({"code": "coherence.package_json_malformed", "path": root})
    for config_relative in sorted(path for path in changed_paths
                                  if path.endswith(".json") and ("/config/" in path
                                                                 or path.startswith(".juno_task/config/"))):
        checked.append("config")
        raw = _git_file(repository, head, config_relative)
        try:
            value = json.loads(raw) if raw is not None else None
            if not isinstance(value, (dict, list)):
                raise ValueError("shape")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            findings.append({"code": "coherence.config_malformed", "path": config_relative})

    generated_raw = _git_file(repository, head, "juno-code/scripts/implementation-contract.json")
    if generated_raw is not None:
        checked.append("generated")
        try:
            generated = json.loads(generated_raw)
            source = generated.get("source") if isinstance(generated, dict) else None
            destinations = generated.get("destinations") if isinstance(generated, dict) else None
            if not isinstance(source, str) or not isinstance(destinations, list):
                raise ValueError("shape")
            if source in changed_paths or any(path in changed_paths for path in destinations):
                source_bytes = _git_file(repository, head, source)
                for destination in destinations:
                    if (not isinstance(destination, str)
                            or source_bytes is None
                            or _git_file(repository, head, destination) != source_bytes):
                        findings.append({"code": "coherence.generated_output_mismatch",
                                         "source": source, "path": destination})
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            findings.append({"code": "coherence.generated_contract_malformed",
                             "path": "juno-code/scripts/implementation-contract.json"})

    if managed_definition_raw is not None:
        checked.append("managed_declaration")
        try:
            managed_definition = json.loads(managed_definition_raw)
            outputs = managed_definition.get("admissionOutputs") \
                if isinstance(managed_definition, dict) else None
            if not isinstance(outputs, list):
                raise ValueError("shape")
            for pair in outputs:
                source = ("juno-code/src/templates/" + str(pair.get("source"))
                          if isinstance(pair, dict) else "")
                destination = str(pair.get("destination")) if isinstance(pair, dict) else ""
                if source in changed_paths or destination in changed_paths:
                    if (not source or not destination
                            or _git_file(repository, head, source) !=
                               _git_file(repository, head, destination)):
                        findings.append({"code": "coherence.managed_output_mismatch",
                                         "source": source, "path": destination})
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            findings.append({"code": "coherence.managed_declaration_malformed",
                             "path": "juno-code/src/templates/managed-assets.json"})

    inventory_raw = _git_file(repository, head, ".juno_task/managed-assets.json")
    if inventory_raw is not None:
        checked.append("managed_inventory")
        try:
            inventory = json.loads(inventory_raw)
            assets = inventory.get("assets") if isinstance(inventory, dict) else None
            if not isinstance(assets, dict):
                raise ValueError("assets")
            for path, binding in assets.items():
                raw = _git_file(repository, head, path)
                actual = hashlib.sha256(raw).hexdigest() if raw is not None else None
                if not isinstance(binding, dict) or actual != binding.get("installedSha256"):
                    findings.append({"code": "coherence.managed_inventory_hash", "path": path})
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            findings.append({"code": "coherence.managed_inventory_malformed",
                             "path": ".juno_task/managed-assets.json"})
    config_path = controller / ".juno_task/config/task-workspace.json"
    checked.append("controller_readiness")
    if not config_path.is_file():
        findings.append({"code": "coherence.controller_policy_missing", "path": str(config_path)})
    controller_status = subprocess.run(["git", "-C", str(controller), "status", "--porcelain=v1"],
                                       cwd=controller, stdin=subprocess.DEVNULL,
                                       text=True, capture_output=True)
    controller_dirt = [line[3:] for line in controller_status.stdout.splitlines()
                       if line[3:] and line[3:].startswith((
                           ".juno_task/prompts/lifecycle/", ".juno_task/workflows/"))]
    if controller_status.returncode or controller_dirt:
        findings.append({"code": "coherence.controller_not_clean",
                         "paths": controller_dirt[:32]})
    if active_doc_paths:
        checked.append("active_documentation")
        audit = active_documentation_audit(
            repository, head, active_doc_paths, documentation_policy)
        findings.extend(audit["findings"])
    findings = sorted(findings, key=lambda row: (str(row.get("code")), digest(row)))
    # Stable exact deduplication while retaining a complete grouped result.
    unique = []
    seen = set()
    for finding in findings:
        identity = digest(finding)
        if identity not in seen:
            seen.add(identity); unique.append({**finding, "finding_sha256": identity})
    body = {"schema_version": COHERENCE_SCHEMA, "head": head,
            "tree": _git(repository, "rev-parse", f"{head}^{{tree}}").strip(),
            "changed_paths": sorted(changed_paths), "checks": sorted(set(checked)),
            "findings": unique}
    return {**body, "outcome": "PASSED" if not unique else "FAILED",
            "report_sha256": digest(body)}


def _tracked_committed_blob(controller: Path, path: Path) -> tuple[str, str]:
    root = Path(_git(controller, "rev-parse", "--show-toplevel").strip()).resolve()
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise LifecycleContractError("lifecycle template/prompt escaped controller Git") from exc
    head = _git(controller, "rev-parse", "HEAD^{commit}").strip()
    blob = subprocess.run(["git", "-C", str(controller), "show", f"{head}:{relative}"],
                          cwd=controller, stdin=subprocess.DEVNULL, capture_output=True)
    if blob.returncode or blob.stdout != resolved.read_bytes():
        raise LifecycleContractError(f"lifecycle asset is absent, uncommitted, or drifted: {relative}")
    status = subprocess.run(["git", "-C", str(controller), "status", "--porcelain=v1", "--", relative],
                            cwd=controller, stdin=subprocess.DEVNULL, text=True, capture_output=True)
    if status.returncode or status.stdout:
        raise LifecycleContractError(f"lifecycle asset has uncommitted mutation: {relative}")
    return head, relative


def compile_lifecycle_template(controller: Path, kind: str, task_id: Optional[str], *,
                               model_identity: Optional[str] = None) -> dict[str, Any]:
    if kind not in {"task-run", "merge-drive"}:
        raise LifecycleContractError("unknown lifecycle template kind")
    name = "yy-task-run.yaml" if kind == "task-run" else "yy-merge-drive.yaml"
    template = controller / ".juno_task/workflows" / name
    try:
        raw = template.read_bytes(); value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LifecycleContractError(f"invalid controller lifecycle template: {exc}") from exc
    controller_head, relative = _tracked_committed_blob(controller, template)
    required = {"schema_version", "template_id", "revision", "kind", "budgets", "steps", "prompts"}
    expected_operations = TASK_OPERATIONS if kind == "task-run" else MERGE_OPERATIONS
    if (not isinstance(value, dict) or set(value) != required or value.get("kind") != kind
            or value.get("schema_version") != "juno_lifecycle_template.v1"
            or not isinstance(value.get("revision"), int) or value["revision"] < 1
            or not isinstance(value.get("budgets"), dict)
            or not isinstance(value.get("steps"), list)
            or [row.get("operation") for row in value["steps"] if isinstance(row, dict)] != list(expected_operations)
            or any(set(row) != {"id", "owner", "operation"} for row in value["steps"])
            or not isinstance(value.get("prompts"), list)):
        raise LifecycleContractError("controller lifecycle template violates typed operation invariants")
    prompt_evidence = []
    for relative_prompt in value["prompts"]:
        if not isinstance(relative_prompt, str) or not relative_prompt.startswith(".juno_task/prompts/lifecycle/"):
            raise LifecycleContractError("lifecycle prompt path is not controller-owned")
        prompt = controller / relative_prompt
        prompt_head, prompt_relative = _tracked_committed_blob(controller, prompt)
        if prompt_head != controller_head:
            raise LifecycleContractError("lifecycle prompt/template controller revisions differ")
        prompt_evidence.append({"path": prompt_relative, "sha256": file_sha(prompt)})
    normalized = {**value, "steps": value["steps"], "prompts": value["prompts"]}
    body = {"schema_version": COMPILED_PLAN_SCHEMA, "kind": kind,
            "task_id": task_id, "controller_commit": controller_head,
            "template": {"path": relative, "raw_sha256": hashlib.sha256(raw).hexdigest(),
                         "semantic_sha256": digest(normalized), "id": value["template_id"],
                         "revision": value["revision"]},
            "prompts": prompt_evidence, "compiler_version": "minimum-rc.1",
            "runtime_sha256": file_sha(Path(__file__).resolve()),
            "model_identity": model_identity, "budgets": value["budgets"],
            "steps": value["steps"]}
    return {**body, "compiled_plan_sha256": digest(body)}


def readiness_questions(task_bytes: bytes) -> list[str]:
    text = task_bytes.decode("utf-8", errors="replace")
    body = re.search(r"<!-- juno:body:start -->\n(.*?)<!-- juno:body:end -->", text, re.S)
    content = body.group(1) if body else re.sub(r"\A---\n.*?\n---\n", "", text,
                                                count=1, flags=re.S)
    questions = []
    if not re.search(r"(?im)^##\s+Goal\s*$", content):
        questions.append("What exact product goal must this task deliver?")
    if not re.search(r"(?im)^##\s+Acceptance(?: criteria)?\s*$", content):
        questions.append("What durable acceptance criteria prove completion?")
    unresolved = re.findall(r"(?im)^.*\b(?:TBD|TODO|NEEDS_DECISION|owner decision required)\b.*$", content)
    if unresolved:
        questions.append("Resolve the material open decisions: " + "; ".join(row.strip()[:160] for row in unresolved[:4]))
    return questions


def run_overlap(suite: Callable[[threading.Event], Any],
                reviewers: list[Callable[[], dict[str, Any]]]) -> dict[str, Any]:
    """Run suite with Reviewer A; preserve A-before-B and cancel suite on A block."""
    cancellation = threading.Event()
    events: list[dict[str, Any]] = []
    lock = threading.Lock()
    started = time.monotonic()
    def event(name: str, **extra: Any) -> None:
        with lock:
            events.append({"event": name, "elapsed_ms": int((time.monotonic() - started) * 1000), **extra})
    suite_started_event = threading.Event()
    def suite_run() -> Any:
        event("suite_started")
        suite_started_event.set()
        try:
            result = suite(cancellation)
            event("suite_completed")
            return result
        except BaseException as exc:
            event("suite_stopped", error=type(exc).__name__)
            raise
    suite_result = suite_error = None
    reviews = []
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="juno-review-suite") as pool:
        future = pool.submit(suite_run)
        if not suite_started_event.wait(5):
            raise LifecycleContractError("suite producer did not start before Reviewer A")
        for index, reviewer in enumerate(reviewers, 1):
            event("reviewer_started", sequence=index)
            result = reviewer()
            reviews.append(result)
            event("reviewer_completed", sequence=index,
                  blocking=bool(result.get("blocking_count")))
            if result.get("blocking_count"):
                cancellation.set(); event("suite_cancellation_requested", sequence=index)
                break
        try:
            suite_result = future.result()
        except BaseException as exc:
            suite_error = exc
    return {"events": events, "reviews": reviews, "suite_result": suite_result,
            "suite_error": suite_error, "cancelled": cancellation.is_set(),
            "elapsed_ms": int((time.monotonic() - started) * 1000)}


PROJECTION_VOLATILE_FIELDS = frozenset({"elapsed_ms", "critical_path_ms",
                                         "projection_sha256"})


def adopt_interrupted_projection(run_dir: Path, journal: dict[str, Any], index: int,
                                 state_name: str, kind: str,
                                 expected: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Adopt an exact preexisting projection left by an interrupted publication.

    A crash after the numbered projection write but before the journal append
    must not strand the run. The artifact never self-attests: every nonvolatile
    field must equal the projection reconstructed from authoritative journal
    and controller state, and only explicitly volatile wall-time fields may
    differ. The journal then records the recovered reference exactly once.
    """
    path = run_dir / "projections" / f"{index:04d}-{state_name.lower()}.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise LifecycleContractError(
            f"immutable lifecycle artifact collision: {path}") from exc
    mismatch = None
    if not isinstance(value, dict):
        mismatch = "not an object"
    elif value.get("schema_version") != RUN_PROJECTION_SCHEMA:
        mismatch = f"schema={value.get('schema_version')}"
    elif value.get("kind") != kind:
        mismatch = f"kind={value.get('kind')}"
    elif value.get("run_id") != journal.get("run_id"):
        mismatch = f"run_id={value.get('run_id')}"
    elif value.get("state") != state_name:
        mismatch = f"state={value.get('state')}"
    elif set(value) != set(expected):
        mismatch = "field set differs from the reconstructed projection"
    else:
        for key, item in value.items():
            if key not in PROJECTION_VOLATILE_FIELDS and item != expected.get(key):
                mismatch = f"field {key} differs from the reconstructed projection"
                break
    if mismatch is None:
        # The artifact's own embedded digest must also hold for its complete
        # body: a field-bound artifact with a stale internal digest is refused.
        body = {key: item for key, item in value.items() if key != "projection_sha256"}
        if value.get("projection_sha256") != digest(body):
            mismatch = "embedded digest does not cover the artifact body"
    if mismatch is not None:
        raise LifecycleContractError(
            f"immutable lifecycle artifact collision ({mismatch}): {path}")
    return value


def verified_projection_bytes(path: Path, *, expected_sha256: Optional[str] = None,
                              kind: Optional[str] = None,
                              run_id: Optional[str] = None) -> dict[str, Any]:
    """Return one persisted projection after exact byte and identity checks."""
    raw = path.read_bytes()
    if expected_sha256 is not None and hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise LifecycleContractError(
            f"lifecycle projection bytes do not match their journal reference: {path}")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LifecycleContractError(f"lifecycle projection is malformed: {path}") from exc
    if (not isinstance(value, dict)
            or value.get("schema_version") != RUN_PROJECTION_SCHEMA
            or (kind is not None and value.get("kind") != kind)
            or (run_id is not None and value.get("run_id") != run_id)):
        raise LifecycleContractError(f"lifecycle projection identity is invalid: {path}")
    body = {key: item for key, item in value.items() if key != "projection_sha256"}
    if value.get("projection_sha256") != digest(body):
        raise LifecycleContractError(f"lifecycle projection digest is invalid: {path}")
    return value


def compact_projection(*, kind: str, run_id: str, task_id: Optional[str], state: str,
                       plan: dict[str, Any], started: float, counters: dict[str, int],
                       attempts: dict[str, int], blocker: Optional[dict[str, Any]],
                       next_action: str, artifacts: list[dict[str, Any]],
                       identities: dict[str, Any], critical_path_ms: int = 0) -> dict[str, Any]:
    body = {"schema_version": RUN_PROJECTION_SCHEMA, "kind": kind,
            "run_id": run_id, "task_id": task_id, "state": state,
            "template_id": plan.get("template", {}).get("id"),
            "template_revision": plan.get("template", {}).get("revision"),
            "compiled_plan_sha256": plan.get("compiled_plan_sha256"),
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "critical_path_ms": critical_path_ms, "commands": counters,
            "attempts": attempts, "blocker": blocker, "next_legal_action": next_action,
            "identities": identities, "artifacts": artifacts[:32]}
    return {**body, "projection_sha256": digest(body)}


def deterministic_summary(projection: dict[str, Any]) -> dict[str, Any]:
    body = {"schema_version": RUN_SUMMARY_SCHEMA,
            "kind": projection.get("kind"), "run_id": projection.get("run_id"),
            "task_id": projection.get("task_id"), "state": projection.get("state"),
            "elapsed_ms": projection.get("elapsed_ms"),
            "critical_path_ms": projection.get("critical_path_ms"),
            "commands": projection.get("commands"), "attempts": projection.get("attempts"),
            "blocker": projection.get("blocker"),
            "compiled_plan_sha256": projection.get("compiled_plan_sha256"),
            "artifact_digests": [row.get("sha256") for row in projection.get("artifacts", [])]}
    return {**body, "summary_sha256": digest(body)}
