#!/usr/bin/env python3
"""Run ordered YAML workflows from a project root.

The workflow file is the source of truth. Steps are arbitrary shell commands,
rendered sequentially against builtins, workflow vars, and prior step results.
Artifacts make failures visible even though failed steps do not fail the overall
process unless a step opts into fail-fast behavior.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import errno
import hashlib
import json
import os
import re
import signal
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

# Package-managed runners execute from product worktrees; importing their local
# support modules must not create product output.
sys.dont_write_bytecode = True

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from workflow_run_evidence import WorkflowRunEvidenceError, resolve_workflow_manifest
from invocation_correlation import child_invocation_environment


JUNO_COMMANDS = {"yylo", "yy", "ypl"}
TEMPLATE_RE = re.compile(r"{{\s*([^}]+?)\s*}}")
SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")
ANSI_RESET = "\033[0m"
STEP_COLORS = [196, 39, 208, 35, 201, 220, 27, 118, 163, 45, 214, 99]

STALE_CHECK_ENV = "YYLO_SKIP_SCRIPT_STALE_CHECK"
TEMPLATE_DIR_ENV = "YYLO_SCRIPT_TEMPLATE_DIR"
SCOPED_CONTINUITY_KEY_PREFIXES = (
    "YYLO_LAST_SESSION_ID_SCOPE_",
    "YYLO_LAST_EXECUTION_SETTINGS_SCOPE_",
)
LEGACY_CONTINUITY_KEYS = {
    "YYLO_LAST_SESSION_ID",
    "YYLO_LAST_EXECUTION_SETTINGS",
}


def child_process_environment(base: dict[str, str]) -> dict[str, str]:
    """Preserve child config/routing while dropping historical continuity values."""
    return {
        name: value
        for name, value in base.items()
        if name not in LEGACY_CONTINUITY_KEYS and not name.startswith(SCOPED_CONTINUITY_KEY_PREFIXES)
    }


def sanitize_current_process_environment() -> None:
    environment = child_process_environment(dict(os.environ))
    # JUNO_PROJECT_PATH is a managed dispatch value. If a parent agent changed
    # cwd into another registered worktree, its role assertion describes the
    # parent boundary and must not override this worktree's persisted identity.
    dispatched = environment.get("JUNO_PROJECT_PATH", "").strip()
    if dispatched:
        try:
            stale_dispatch = Path(dispatched).expanduser().resolve() != Path.cwd().resolve()
        except OSError:
            stale_dispatch = True
        if stale_dispatch:
            environment.pop("JUNO_PROJECT_PATH", None)
            environment.pop("JUNO_WORKSPACE_ROLE", None)
    os.environ.clear()
    os.environ.update(environment)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _managed_site_package_paths(venv: Path) -> list[Path]:
    """Return active import paths that are actual site-package paths inside venv."""
    managed: list[Path] = []
    for value in sys.path:
        if not value:
            continue
        try:
            candidate = Path(value).resolve()
        except OSError:
            continue
        if candidate.name in {"site-packages", "dist-packages"} and _is_within(candidate, venv):
            managed.append(candidate)
    return managed


def _managed_yaml_module() -> tuple[Any | None, str]:
    """Use PyYAML only when it originates in the active managed environment."""
    try:
        active_venv = Path(sys.prefix).resolve()
        managed_sites = _managed_site_package_paths(active_venv)
        import yaml  # type: ignore

        origin_value = getattr(yaml, "__file__", None)
        if not origin_value:
            return None, "unavailable (module has no file origin)"
        origin = Path(origin_value).resolve()
        if not any(_is_within(origin, site) for site in managed_sites):
            return None, f"unavailable (unmanaged module origin {origin})"
        if not callable(getattr(yaml, "safe_load", None)):
            return None, f"unavailable (safe_load missing at {origin})"
        return yaml, str(origin)
    except Exception as exc:
        return None, f"unavailable ({exc})"


def ensure_controller_python_environment(controller_env: dict[str, str]) -> None:
    """Run under the controller's .venv_juno, matching the Kanban launcher contract."""
    root = Path(controller_env["JUNO_TASK_ROOT"]).resolve()
    venv = root / ".venv_juno"
    python = venv / "bin" / "python"
    installer = root / ".juno_task" / "scripts" / "install_requirements.sh"
    reexec_key = "JUNO_WORKFLOW_PYTHON_REEXEC"

    installer_env = dict(os.environ)
    installer_env.update(controller_env)
    inherited_pythonpath = bool(installer_env.get("PYTHONPATH", "").strip())
    inherited_venv = installer_env.pop("VIRTUAL_ENV", "").strip()
    inherited_conda = installer_env.pop("CONDA_PREFIX", "").strip()
    installer_env.pop("CONDA_DEFAULT_ENV", None)
    installer_env.pop("PYTHONHOME", None)
    installer_env.pop("PYTHONPATH", None)
    foreign_bins = {
        str(Path(value).expanduser().resolve() / "bin")
        for value in (inherited_venv, inherited_conda)
        if value
    }
    installer_env["PATH"] = os.pathsep.join(
        part
        for part in installer_env.get("PATH", "").split(os.pathsep)
        if not part or str(Path(part).expanduser().resolve()) not in foreign_bins
    )

    def provision() -> None:
        if not installer.is_file():
            raise WorkflowError(
                f"controller Python environment is incomplete ({python}) and installer was not found ({installer})"
            )
        try:
            completed = subprocess.run(
                ["bash", str(installer)],
                cwd=root,
                env=installer_env,
                stdin=subprocess.DEVNULL,
                check=False,
                timeout=300,
            )
        except subprocess.TimeoutExpired as exc:
            raise WorkflowError(f"timed out provisioning controller Python environment after 300s: {venv}") from exc
        if completed.returncode != 0 or not python.is_file():
            raise WorkflowError(f"failed to create controller Python environment: {venv}")

    if not python.is_file():
        provision()

    env = dict(os.environ)
    env.update(controller_env)
    env["VIRTUAL_ENV"] = str(venv)
    env["PATH"] = os.pathsep.join(
        [
            str(venv / "bin"),
            *[
                part
                for part in env.get("PATH", "").split(os.pathsep)
                if part != str(venv / "bin")
                and (not part or str(Path(part).expanduser().resolve()) not in foreign_bins)
            ],
        ]
    )
    env.pop("CONDA_PREFIX", None)
    env.pop("CONDA_DEFAULT_ENV", None)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)

    try:
        active_prefix = Path(sys.prefix).resolve()
        prefix_selected = active_prefix == venv.resolve()
    except OSError:
        active_prefix = Path(sys.prefix)
        prefix_selected = False
    managed_sites = _managed_site_package_paths(venv)
    # A selected prefix is insufficient when PYTHONPATH already changed this
    # process's sys.path; only a clean re-exec can remove those import entries.
    environment_selected = prefix_selected and bool(managed_sites) and not inherited_pythonpath
    reexec_marker = f"{os.getpid()}:{venv}"

    if not environment_selected:
        if env.get(reexec_key) == reexec_marker:
            raise WorkflowError(
                "controller Python re-exec did not establish the managed environment: "
                f"expected prefix {venv}, active prefix {active_prefix}, managed site-packages {managed_sites or 'missing'}"
            )
        env[reexec_key] = reexec_marker
        os.execve(str(python), [str(python), str(Path(__file__).resolve()), *sys.argv[1:]], env)

    _, yaml_origin = _managed_yaml_module()
    env.pop(reexec_key, None)
    os.environ.clear()
    os.environ.update(env)
    if os.environ.get("JUNO_DEBUG", "false") == "true":
        print(
            f"[DEBUG] workflow_runner.sh Python runtime: {python}; prefix: {active_prefix}; "
            f"site-packages: {managed_sites}; PyYAML: {yaml_origin}",
            file=sys.stderr,
        )


def _display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def _is_package_template_path(path: Path) -> bool:
    parts = path.parts
    template_suffixes = (("dist", "templates", "scripts"), ("src", "templates", "scripts"))
    for suffix in template_suffixes:
        suffix_len = len(suffix)
        if len(parts) >= suffix_len + 1 and tuple(parts[-suffix_len - 1 : -1]) == suffix:
            return True
    return False


def _installed_template_candidates(script_name: str) -> list[Path]:
    candidates: list[Path] = []
    env_template_dir = os.environ.get(TEMPLATE_DIR_ENV)
    if env_template_dir:
        candidates.append(Path(env_template_dir).expanduser() / script_name)

    for command_name in ("yy", "yylo", "ypl"):
        command_path = shutil.which(command_name)
        if not command_path:
            continue
        try:
            resolved = Path(command_path).resolve()
        except OSError:
            resolved = Path(command_path)
        for parent in (resolved.parent, *resolved.parents):
            candidates.append(parent / "dist" / "templates" / "scripts" / script_name)
            candidates.append(parent / "templates" / "scripts" / script_name)

    seen: set[str] = set()
    unique: list[Path] = []
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def warn_if_runtime_script_is_stale(script_name: str) -> None:
    if os.environ.get(STALE_CHECK_ENV) == "1":
        return
    try:
        runtime_path = Path(__file__).resolve()
        if not os.environ.get(TEMPLATE_DIR_ENV) and _is_package_template_path(runtime_path):
            return
        runtime_hash = hashlib.sha256(runtime_path.read_bytes()).hexdigest()
        for template_path in _installed_template_candidates(script_name):
            try:
                installed_path = template_path.resolve()
            except OSError:
                installed_path = template_path
            if installed_path == runtime_path or not template_path.is_file():
                continue
            installed_hash = hashlib.sha256(template_path.read_bytes()).hexdigest()
            if runtime_hash != installed_hash:
                print(
                    f"{script_name}: warning: this runtime script differs from the installed yylo template.\n"
                    f"  runtime: {_display_path(runtime_path)}\n"
                    f"  installed template: {installed_path}\n"
                    "  update with: yy scripts update --force",
                    file=sys.stderr,
                )
            return
    except Exception:
        return


class WorkflowError(Exception):
    pass


class StepLaunchOwnership:
    """Process-group ownership scoped to one rendered workflow step."""

    launched: bool
    process_group_id: int

    def __init__(self) -> None:
        self.launched = False
        self.process_group_id = 0

    def record(self, process_group_id: int) -> None:
        self.launched = True
        self.process_group_id = process_group_id


LOCAL_INTEGRATION_HARD_CUT = (
    "legacy local_integration execution is read-only; "
    "use `yy task start TASK_ID` and `yy merge land TASK_ID`, or inspect historical artifacts with workflow_runner.sh doctor"
)
RETIRED_LIFECYCLE_HELPERS = {
    "worktree_lifecycle.py", "integration_candidate.py", "integration_owner_preflight.py",
}


RUN_CONTRACT_SCHEMA = "juno_workflow_run_contract.v1"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return sha256_bytes(encoded)


def file_sha256(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def dotted_get(value: Any, field: str) -> tuple[bool, Any]:
    current = value
    for part in field.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return False, None
    return True, current


def normalize_receipt_contracts(workflow: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = workflow.get("receipts") or []
    if isinstance(raw, dict):
        items = [{"id": key, **(value if isinstance(value, dict) else {})} for key, value in raw.items()]
    elif isinstance(raw, list):
        items = raw
    else:
        raise WorkflowError("receipts must be a list or mapping")
    contracts: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            raise WorkflowError("each receipt contract must be a mapping")
        receipt_id = str(item.get("id") or "").strip()
        if not receipt_id or not re.match(r"^[a-z][a-z0-9_]*$", receipt_id):
            raise WorkflowError(
                f"invalid receipt id: {receipt_id!r}; use lowercase letters, numbers, and underscores, "
                "starting with a letter, so template and environment lookups remain unambiguous"
            )
        if receipt_id in contracts:
            raise WorkflowError(f"duplicate receipt id: {receipt_id}")
        producer = str(item.get("producer") or "").strip()
        path = str(item.get("path") or "").strip()
        schema_version = str(item.get("schema_version") or "").strip()
        required_fields = item.get("required_fields") or []
        expected_fields = item.get("expected_fields") or {}
        if not producer or not path or not schema_version:
            raise WorkflowError(f"receipt {receipt_id} requires producer, path, and schema_version")
        if not isinstance(required_fields, list) or not all(isinstance(field, str) and field for field in required_fields):
            raise WorkflowError(f"receipt {receipt_id} required_fields must be a list of dotted field names")
        if "producer_step_digest" not in required_fields:
            raise WorkflowError(
                f"receipt {receipt_id} required_fields must include producer_step_digest so lint and runtime bind the same producer"
            )
        if not isinstance(expected_fields, dict) or not all(
            isinstance(field, str) and field for field in expected_fields
        ):
            raise WorkflowError(f"receipt {receipt_id} expected_fields must be a mapping of dotted fields to values")
        contracts[receipt_id] = {
            "id": receipt_id,
            "producer": producer,
            "path": path,
            "schema_version": schema_version,
            "required_fields": required_fields,
            "expected_fields": expected_fields,
        }
    return contracts


def validate_receipt_payload(
    contract: dict[str, Any], payload: Any, producer_step_digest: str, *, location: str
) -> None:
    receipt_id = contract["id"]
    if not isinstance(payload, dict):
        raise WorkflowError(f"receipt[{receipt_id}] {location}: expected JSON object")
    actual_schema = payload.get("schema_version")
    if actual_schema != contract["schema_version"]:
        raise WorkflowError(
            f"receipt[{receipt_id}].schema_version: expected={contract['schema_version']!r} actual={actual_schema!r}"
        )
    actual_digest = payload.get("producer_step_digest")
    if actual_digest != producer_step_digest:
        raise WorkflowError(
            f"receipt[{receipt_id}].producer_step_digest: expected={producer_step_digest!r} actual={actual_digest!r}"
        )
    for field in contract["required_fields"]:
        present, _ = dotted_get(payload, field)
        if not present:
            raise WorkflowError(f"receipt[{receipt_id}].required_field[{field}]: missing at {location}")
    for field, expected in contract.get("expected_fields", {}).items():
        present, actual = dotted_get(payload, field)
        if not present:
            raise WorkflowError(f"receipt[{receipt_id}].expected_field[{field}]: missing at {location}")
        if actual != expected:
            raise WorkflowError(
                f"receipt[{receipt_id}].expected_field[{field}]: expected={expected!r} actual={actual!r}"
            )


def normalize_candidate_composition(step: dict[str, Any]) -> dict[str, str] | None:
    raw = step.get("candidate_composition")
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != {"manifest_path", "receipt_path_field", "changed_paths_field"}:
        raise WorkflowError(
            f"step {step.get('id')} candidate_composition must contain exactly manifest_path, "
            "receipt_path_field, and changed_paths_field"
        )
    if not all(isinstance(raw.get(key), str) and raw[key].strip() for key in raw):
        raise WorkflowError(f"step {step.get('id')} candidate_composition fields must be non-empty strings")
    normalized = {key: raw[key].strip() for key in raw}
    if normalized["changed_paths_field"] != "changed_paths":
        raise WorkflowError(
            f"step {step.get('id')} candidate_composition changed_paths_field must be changed_paths"
        )
    return normalized


def load_json_object(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise WorkflowError(f"{description} not found: {path}") from exc
    except Exception as exc:
        raise WorkflowError(f"invalid {description} at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise WorkflowError(f"{description} must be a JSON object: {path}")
    return value


def color_enabled() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def step_color(index: int) -> str:
    return f"\033[38;5;{STEP_COLORS[(index - 1) % len(STEP_COLORS)]}m"


def colorize(text: str, index: int) -> str:
    if not color_enabled():
        return text
    return f"{step_color(index)}{text}{ANSI_RESET}"


def step_separator(label: str, index: int, step_id: str, details: str = "") -> str:
    suffix = f" {details}" if details else ""
    return colorize(f"{'=' * 18} {label}: step {index} [{step_id}]{suffix} {'=' * 18}", index)


def parse_scalar(value: str) -> Any:
    value = value.strip()
    if value == "":
        return ""
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    lowered = value.lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if lowered in {"null", "none", "~"}:
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def count_indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def read_literal_block(lines: list[str], start: int, parent_indent: int) -> tuple[str, int]:
    i = start
    block_lines: list[str] = []
    block_indent: int | None = None
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            block_lines.append("")
            i += 1
            continue
        indent = count_indent(line)
        if indent <= parent_indent:
            break
        if block_indent is None:
            block_indent = indent
        block_lines.append(line[min(block_indent, len(line)) :])
        i += 1
    return "\n".join(block_lines).rstrip("\n"), i


def read_scalar_list(lines: list[str], start: int, parent_indent: int) -> tuple[list[Any], int]:
    values: list[Any] = []
    i = start
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        indent = count_indent(line)
        if indent <= parent_indent:
            break
        if not stripped.startswith("-"):
            raise WorkflowError(f"expected scalar list item near line {i + 1}: {line}")
        item = stripped[1:].strip()
        if item == "|":
            literal, i = read_literal_block(lines, i + 1, indent)
            values.append(literal)
            continue
        values.append(parse_scalar(item))
        i += 1
    return values, i


def parse_yaml_like(text: str) -> dict[str, Any]:
    """Parse workflow YAML without making PyYAML a hard runtime dependency.

    JSON is accepted. When PyYAML is installed, it is used for full YAML support.
    The fallback supports the documented workflow shape: root scalars/mappings,
    literal blocks, and a list of step mappings.
    """
    try:
        loaded = json.loads(text)
        if isinstance(loaded, dict):
            return loaded
    except json.JSONDecodeError:
        pass

    yaml, _ = _managed_yaml_module()
    if yaml is not None:
        try:
            loaded = yaml.safe_load(text)
            if not isinstance(loaded, dict):
                raise WorkflowError("workflow must be a YAML mapping")
            return loaded
        except Exception as exc:  # pragma: no cover - exact PyYAML error varies
            raise WorkflowError(f"failed to parse workflow YAML: {exc}") from exc

    advanced_yaml_keys = sorted(
        set(
            re.findall(
                r"(?m)^(?:receipts|frozen_inputs|validation_ownership):\s*(?:#.*)?$",
                text,
            )
        )
    )
    if advanced_yaml_keys:
        raise WorkflowError(
            "advanced workflow contracts require Python PyYAML or JSON input; "
            "install PyYAML in the runner environment or encode the workflow as JSON"
        )

    lines = text.splitlines()
    root: dict[str, Any] = {}
    i = 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        if count_indent(raw) != 0 or ":" not in stripped:
            raise WorkflowError(f"unsupported YAML near line {i + 1}: {raw}")
        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value == "|":
            root[key], i = read_literal_block(lines, i + 1, count_indent(raw))
            continue
        if value:
            root[key] = parse_scalar(value)
            i += 1
            continue

        i += 1
        if key == "steps":
            steps: list[dict[str, Any]] = []
            while i < len(lines):
                line = lines[i]
                stripped_line = line.strip()
                if not stripped_line or stripped_line.startswith("#"):
                    i += 1
                    continue
                indent = count_indent(line)
                if indent == 0:
                    break
                if not stripped_line.startswith("-"):
                    raise WorkflowError(f"expected step list item near line {i + 1}: {line}")
                item: dict[str, Any] = {}
                rest = stripped_line[1:].strip()
                if rest:
                    if ":" not in rest:
                        raise WorkflowError(f"expected key/value after '-' near line {i + 1}")
                    k, v = rest.split(":", 1)
                    item[k.strip()] = parse_scalar(v.strip())
                i += 1
                while i < len(lines):
                    child = lines[i]
                    child_stripped = child.strip()
                    child_indent = count_indent(child)
                    if not child_stripped or child_stripped.startswith("#"):
                        i += 1
                        continue
                    if child_indent <= indent:
                        break
                    if ":" not in child_stripped:
                        raise WorkflowError(f"expected step field near line {i + 1}: {child}")
                    k, v = child_stripped.split(":", 1)
                    k = k.strip()
                    v = v.strip()
                    if v == "|":
                        item[k], i = read_literal_block(lines, i + 1, child_indent)
                        continue
                    if v == "" and k == "command":
                        item[k], i = read_scalar_list(lines, i + 1, child_indent)
                        continue
                    item[k] = parse_scalar(v)
                    i += 1
                steps.append(item)
            root[key] = steps
            continue

        nested: dict[str, Any] = {}
        while i < len(lines):
            line = lines[i]
            stripped_line = line.strip()
            if not stripped_line or stripped_line.startswith("#"):
                i += 1
                continue
            indent = count_indent(line)
            if indent == 0:
                break
            if ":" not in stripped_line:
                raise WorkflowError(f"expected mapping field near line {i + 1}: {line}")
            k, v = stripped_line.split(":", 1)
            k = k.strip()
            v = v.strip()
            if v == "|":
                nested[k], i = read_literal_block(lines, i + 1, indent)
                continue
            if v == "" and k == "command":
                nested[k], i = read_scalar_list(lines, i + 1, indent)
                continue
            nested[k] = parse_scalar(v)
            i += 1
        root[key] = nested
    return root


def get_path(context: dict[str, Any], expr: str) -> Any:
    current: Any = context
    for part in expr.split("."):
        part = part.strip()
        if part == "":
            continue
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return ""
    return current


def render(value: Any, context: dict[str, Any]) -> Any:
    if isinstance(value, list):
        return [render(item, context) for item in value]
    if isinstance(value, dict):
        return {key: render(item, context) for key, item in value.items()}
    if not isinstance(value, str):
        return value

    def repl(match: re.Match[str]) -> str:
        resolved = get_path(context, match.group(1))
        if resolved is None:
            return ""
        if isinstance(resolved, (dict, list)):
            return json.dumps(resolved, ensure_ascii=False)
        return str(resolved)

    return TEMPLATE_RE.sub(repl, value)


def safe_id(value: Any, fallback: str) -> str:
    text = str(value or fallback).strip() or fallback
    text = SAFE_ID_RE.sub("-", text).strip("-._")
    return text or fallback


def validate_workflow(workflow: dict[str, Any], policy: dict[str, Any] | None = None) -> None:
    if str(workflow.get("workflow_class") or "").strip() == "local_integration":
        raise WorkflowError(LOCAL_INTEGRATION_HARD_CUT)
    workflow_schema = str(workflow.get("schema_version") or "").strip()
    if workflow_schema and workflow_schema not in {"1", "1.0", "v1", "2", "2.0", "v2"}:
        raise WorkflowError(f"unsupported schema_version: {workflow['schema_version']}")
    steps = workflow.get("steps")
    if not isinstance(steps, list) or not steps:
        raise WorkflowError("workflow must define a non-empty steps list")
    if "summary" in workflow and not isinstance(workflow["summary"], (str, dict, type(None))):
        raise WorkflowError("summary must be a string, mapping, or null")
    continue_from_step = str(workflow.get("continue_from_step") or "").strip()
    managed_policy = workflow.get("managed_agent_policy")
    managed_steps_present = any(isinstance(step, dict) and step.get("managed_agent") is not None for step in steps)
    if managed_steps_present:
        expected_policy = {"external_side_effects": "forbidden", "lifecycle_hooks": "disabled"}
        if managed_policy != expected_policy:
            raise WorkflowError(
                "managed-agent workflows require exact managed_agent_policy: "
                "{external_side_effects: forbidden, lifecycle_hooks: disabled}"
            )
    elif managed_policy is not None:
        raise WorkflowError("managed_agent_policy is valid only when managed-agent steps are present")
    seen: set[str] = set()
    step_names: set[str] = set()
    step_indexes: dict[str, int] = {}
    for idx, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            raise WorkflowError(f"step {idx} must be a mapping")
        step_id = str(step.get("id") or "").strip()
        if not step_id:
            raise WorkflowError(f"step {idx} is missing required id")
        if not re.match(r"^[A-Za-z0-9_.-]+$", step_id):
            raise WorkflowError(f"step id contains unsupported characters: {step_id}")
        if step_id in seen:
            raise WorkflowError(f"duplicate step id: {step_id}")
        seen.add(step_id)
        step_indexes[step_id] = idx
        step_name = str(step.get("name") or "").strip()
        if step_name:
            step_names.add(step_name)
        managed = step.get("managed_agent")
        if managed is not None:
            if not isinstance(managed, dict):
                raise WorkflowError(f"step {step_id} managed_agent must be a mapping")
            required = {"mode", "controller_root", "controller_branch", "agent_root", "prompt_file", "out_dir"}
            if not required.issubset(managed):
                raise WorkflowError(f"step {step_id} managed_agent is missing required identity fields")
            if managed.get("mode") not in {"worker", "reviewer"}:
                raise WorkflowError(f"step {step_id} managed_agent.mode must be worker or reviewer")
            if step.get("capture_session") not in {None, False} or step.get("capture") not in {None, False}:
                raise WorkflowError(f"step {step_id} managed_agent outer capture must be disabled")
            if "command" in step:
                raise WorkflowError(f"step {step_id} managed_agent cannot also declare command")
            boundary = step.get("stage_boundary")
            if not isinstance(boundary, dict) or set(boundary) != {"root", "admitted_paths"}:
                raise WorkflowError(
                    f"step {step_id} managed_agent requires exact stage_boundary root/admitted_paths"
                )
            if not isinstance(boundary.get("root"), str) or not boundary["root"].strip():
                raise WorkflowError(f"step {step_id} stage_boundary.root must be a non-empty path")
            admitted = boundary.get("admitted_paths")
            if (not isinstance(admitted, list) or not admitted
                    or not all(isinstance(item, str) and item.strip() for item in admitted)):
                raise WorkflowError(f"step {step_id} stage_boundary.admitted_paths must be a non-empty path list")
            normalized: list[str] = []
            for item in admitted:
                candidate = Path(item)
                if candidate.is_absolute() or item != candidate.as_posix() or ".." in candidate.parts or ".git" in candidate.parts:
                    raise WorkflowError(f"step {step_id} stage_boundary admitted path is unsafe: {item}")
                clean = item.rstrip("/")
                if clean in {"", "."}:
                    raise WorkflowError(f"step {step_id} stage_boundary cannot admit the whole worktree")
                normalized.append(clean)
            if len(set(normalized)) != len(normalized):
                raise WorkflowError(f"step {step_id} stage_boundary admitted paths must be unique")
        elif "command" not in step:
            raise WorkflowError(f"step {step_id} is missing required command")
        requires_receipts = step.get("requires_receipts") or []
        if not isinstance(requires_receipts, list) or not all(isinstance(item, str) for item in requires_receipts):
            raise WorkflowError(f"step {step_id} requires_receipts must be a list of receipt ids")
        normalize_candidate_composition(step)
        expected_outcomes = step.get("expected_outcomes")
        if expected_outcomes is not None and (
            not isinstance(expected_outcomes, list)
            or not expected_outcomes
            or not all(isinstance(item, str) and item for item in expected_outcomes)
        ):
            raise WorkflowError(f"step {step_id} expected_outcomes must be a non-empty string list")
    summary = workflow.get("summary")
    summary_has_command = isinstance(summary, dict) and "command" in summary
    if continue_from_step and continue_from_step != "summary" and continue_from_step not in seen and continue_from_step not in step_names:
        raise WorkflowError(f"continue_from_step references unknown step: {continue_from_step}")
    if continue_from_step == "summary" and not summary_has_command:
        raise WorkflowError("continue_from_step references summary, but summary.command is not configured")

    selection = workflow.get("hydration_selection")
    if selection is not None and (
            selection != "admitted_scope_v1" or workflow.get("workflow_class") != "task_hydration"):
        raise WorkflowError("hydration_selection requires task_hydration and admitted_scope_v1")
    if any("dependency_root" in step for step in steps) and selection is None:
        raise WorkflowError("dependency_root requires an explicit hydration_selection policy")
    if str(workflow.get("workflow_class") or "").strip() == "task_hydration":
        if summary_has_command or workflow.get("continue_from_step"):
            raise WorkflowError("task_hydration workflows cannot launch summary commands or continuation")
        forbidden = re.compile(
            r"(?:^|\s)(?:yy|yylo|ypl)\s+(?:task|merge|ledger|kanban|release|pi|codex|claude|gemini)|"
            r"JUNO_TASK_ROOT|\.juno_task/(?:state|tasks|ledger|receipts|runtime/merge)", re.IGNORECASE)
        for hydration_step in steps:
            step_id = str(hydration_step["id"])
            root = hydration_step.get("dependency_root")
            if root is not None:
                if not isinstance(root, str) or not root or "{{" in root:
                    raise WorkflowError(f"task_hydration step {step_id} dependency_root must be a literal path")
                candidate = Path(root)
                if (candidate.is_absolute() or ".." in candidate.parts or ".git" in candidate.parts
                        or candidate.as_posix() != root or root == "."):
                    raise WorkflowError(f"task_hydration step {step_id} dependency_root must be worktree-relative")
            command = hydration_step.get("command")
            probe = hydration_step.get("probe")
            if hydration_step.get("managed_agent") is not None:
                raise WorkflowError(f"task_hydration step {step_id} cannot launch an agent")
            if (not isinstance(command, list) or not command
                    or not all(isinstance(part, str) and part for part in command)):
                raise WorkflowError(f"task_hydration step {step_id} command must be a non-empty argv list")
            if (not isinstance(probe, list) or not probe
                    or not all(isinstance(part, str) and part for part in probe)):
                raise WorkflowError(f"task_hydration step {step_id} requires a non-empty idempotency probe argv")
            timeout = hydration_step.get("timeout_seconds")
            if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 3600:
                raise WorkflowError(f"task_hydration step {step_id} timeout_seconds must be 1..3600")
            if hydration_step.get("fail_workflow") is not True or hydration_step.get("non_interactive") is not True:
                raise WorkflowError(f"task_hydration step {step_id} must be non-interactive and workflow-fatal")
            if not isinstance(hydration_step.get("network"), bool) or not isinstance(hydration_step.get("sensitive"), bool):
                raise WorkflowError(f"task_hydration step {step_id} must declare network and sensitive booleans")
            outputs = hydration_step.get("outputs")
            if not isinstance(outputs, list) or not all(isinstance(item, str) and item for item in outputs):
                raise WorkflowError(f"task_hydration step {step_id} outputs must be a path list")
            for output in outputs:
                candidate = Path(output)
                if candidate.is_absolute() or ".." in candidate.parts or ".git" in candidate.parts:
                    raise WorkflowError(f"task_hydration step {step_id} output escapes the worktree: {output}")
            policy_text = " ".join([*probe, *command])
            if forbidden.search(policy_text):
                raise WorkflowError(f"task_hydration step {step_id} invokes a forbidden controller/lifecycle surface")
            if any(token in {"-i", "--interactive", "--live"} for token in command):
                raise WorkflowError(f"task_hydration step {step_id} is interactive")
            if hydration_step.get("sensitive") and "worktree_hydration.py" not in policy_text:
                raise WorkflowError(f"task_hydration sensitive step {step_id} must use the non-echoing helper")

    for step in steps:
        if step.get("managed_agent") is not None:
            continue
        helper_tokens = effective_command_argv(step.get("command"))
        helper_index = 1 if helper_tokens and Path(helper_tokens[0]).name in {"python", "python3"} else 0
        helper_executable = Path(helper_tokens[helper_index]).name if len(helper_tokens) > helper_index else ""
        if helper_executable in RETIRED_LIFECYCLE_HELPERS:
            raise WorkflowError(LOCAL_INTEGRATION_HARD_CUT)
        validate_pi_launch_policy(step, context=f"step {step['id']}", policy=policy)
    if summary_has_command:
        validate_pi_launch_policy(summary, context="summary", policy=policy)

    receipts = normalize_receipt_contracts(workflow)
    for receipt_id, contract in receipts.items():
        producer = contract["producer"]
        if producer not in step_indexes:
            raise WorkflowError(f"receipt {receipt_id} references unknown producer step: {producer}")
    for step in steps:
        step_id = str(step["id"])
        for receipt_id in step.get("requires_receipts") or []:
            if receipt_id not in receipts:
                raise WorkflowError(f"step {step_id} requires unknown receipt: {receipt_id}")
            producer = receipts[receipt_id]["producer"]
            if step_indexes[producer] >= step_indexes[step_id]:
                raise WorkflowError(f"step {step_id} requires receipt {receipt_id} before its producer {producer}")
        admission = [receipts[receipt_id] for receipt_id in step.get("requires_receipts") or []
                     if receipts[receipt_id]["schema_version"] == "juno_edit_preflight.v1"
                     and receipts[receipt_id]["expected_fields"].get("passed") is True]
        if step.get("edit_capable") is True and len(admission) != 1:
            raise WorkflowError(
                f"edit-capable step {step_id} requires exactly one successful juno_edit_preflight.v1 receipt"
            )
        elif step.get("edit_capable") not in {None, False, True}:
            raise WorkflowError(f"step {step_id} edit_capable must be boolean")
        generated = step.get("generated_task_contract")
        if generated is not None:
            if not isinstance(generated, dict):
                raise WorkflowError(f"generated step {step_id} contract must be a mapping")
            role = str(generated.get("role") or "")
            write_contract = str(generated.get("write_contract") or "")
            if role == "review" and write_contract != "read_only":
                raise WorkflowError(f"generated review step {step_id} must use write_contract read_only")
            if write_contract not in {"read_only", "product_edit"}:
                raise WorkflowError(f"generated step {step_id} has invalid write_contract: {write_contract}")
            if write_contract == "read_only":
                if generated.get("task_root_receipt") or step.get("edit_capable") is True:
                    raise WorkflowError(f"generated read-only step {step_id} cannot declare edit admission")
            else:
                receipt_id = str(generated.get("task_root_receipt") or "")
                if len(admission) != 1 or not receipt_id or admission[0]["id"] != receipt_id:
                    raise WorkflowError(
                        f"generated write-capable step {step_id} requires one matching task_root_receipt"
                    )
                if step.get("edit_capable") is not True:
                    raise WorkflowError(f"generated write-capable step {step_id} must declare edit_capable true")

    terminal_gate = str(workflow.get("terminal_gate") or "").strip()
    if terminal_gate and terminal_gate not in step_indexes:
        raise WorkflowError(f"terminal_gate references unknown step: {terminal_gate}")
    final_worktree = workflow.get("final_worktree")
    if final_worktree is not None:
        if (not isinstance(final_worktree, dict) or set(final_worktree) != {"path", "head"}
                or not isinstance(final_worktree.get("path"), str)
                or not Path(final_worktree["path"]).is_absolute()
                or not re.fullmatch(r"[0-9a-f]{40}", str(final_worktree.get("head") or ""))):
            raise WorkflowError("final_worktree must bind an absolute path and exact 40-hex head")
    validation_ownership = workflow.get("validation_ownership")
    if validation_ownership is not None and not isinstance(validation_ownership, dict):
        raise WorkflowError("validation_ownership must be a mapping")
    for role, step_id_value in (validation_ownership or {}).items():
        if str(step_id_value) not in step_indexes:
            raise WorkflowError(f"validation_ownership.{role} references unknown step: {step_id_value}")

    frozen_inputs = workflow.get("frozen_inputs") or []
    if not isinstance(frozen_inputs, list):
        raise WorkflowError("frozen_inputs must be a list")
    frozen_ids: set[str] = set()
    for item in frozen_inputs:
        if not isinstance(item, dict):
            raise WorkflowError("each frozen_inputs entry must be a mapping")
        input_id = str(item.get("id") or "").strip()
        path = str(item.get("path") or "").strip()
        if not input_id or not path:
            raise WorkflowError("each frozen_inputs entry requires id and path")
        if input_id in frozen_ids:
            raise WorkflowError(f"duplicate frozen input id: {input_id}")
        frozen_ids.add(input_id)


def workflow_to_yaml(data: Any, indent: int = 0) -> str:
    pad = " " * indent
    if isinstance(data, dict):
        lines: list[str] = []
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                lines.append(f"{pad}{key}:")
                nested = workflow_to_yaml(value, indent + 2)
                if nested:
                    lines.append(nested)
            else:
                lines.append(f"{pad}{key}: {json.dumps(value) if isinstance(value, str) else value}")
        return "\n".join(lines)
    if isinstance(data, list):
        lines = []
        for item in data:
            if isinstance(item, dict):
                item_lines = workflow_to_yaml(item, indent + 2).splitlines()
                if item_lines:
                    lines.append(f"{pad}- {item_lines[0].lstrip()}")
                    lines.extend(item_lines[1:])
                else:
                    lines.append(f"{pad}- {{}}")
            else:
                lines.append(f"{pad}- {item}")
        return "\n".join(lines)
    return f"{pad}{data}"


def command_argv(command: Any) -> list[str]:
    if isinstance(command, list):
        return [str(part) for part in command]
    if not isinstance(command, str):
        return [str(command)]
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.strip().split()


def command_preview(command: Any) -> str:
    if isinstance(command, list):
        return " ".join(shlex.quote(str(part)) for part in command)
    return str(command)


def detect_juno_command(command: Any) -> bool:
    parts = command_argv(command)
    if not parts:
        return False
    executable = Path(parts[0]).name
    return executable in JUNO_COMMANDS


DIRECT_AGENT_EXECUTABLES = {"pi", "codex", "claude", "gemini", "cursor"}


def active_shell_syntax(command: str) -> tuple[bool, bool]:
    quote: str | None = None
    escaped = False
    has_control = False
    has_expansion = False
    for index, char in enumerate(command):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote != "'":
            escaped = True
            continue
        if char == "'" and quote != '"':
            quote = None if quote == "'" else "'"
            continue
        if char == '"' and quote != "'":
            quote = None if quote == '"' else '"'
            continue
        if quote == "'":
            continue
        if char == "`" or (char == "$" and command[index + 1:index + 2] == "("):
            has_expansion = True
        if quote is None and (char in ";&|<>()" or char == "\n"):
            has_control = True
    return has_control, has_expansion


def compound_agent_shell_command(command: Any, *, _depth: int = 0) -> bool:
    if _depth >= 8:
        return True
    if isinstance(command, list):
        parts = effective_command_argv(command)
        if not parts:
            return False
        executable = Path(parts[0]).name
        if executable in JUNO_COMMANDS | DIRECT_AGENT_EXECUTABLES:
            return False

        def wrapped_launch(value: Any) -> bool:
            nested = effective_command_argv(value)
            if not nested:
                return False
            nested_executable = Path(nested[0]).name
            return (
                nested_executable in JUNO_COMMANDS | DIRECT_AGENT_EXECUTABLES
                or compound_agent_shell_command(value, _depth=_depth + 1)
            )

        if executable in {"bash", "dash", "ksh", "sh", "zsh"}:
            for index, part in enumerate(parts[1:], start=1):
                if part.startswith("-") and "c" in part[1:] and index + 1 < len(parts):
                    return wrapped_launch(parts[index + 1])
            return False
        if executable == "eval":
            return len(parts) > 1 and wrapped_launch(" ".join(parts[1:]))
        if executable in {"builtin", "command", "exec"}:
            index = 1
            while index < len(parts) and (parts[index] == "--" or parts[index].startswith("-")):
                option = parts[index]
                index += 1
                if executable == "exec" and option != "--" and "a" in option[1:] and not option.split("a", 1)[1]:
                    # Bash exec's -a name option consumes the following argv
                    # entry before the wrapped executable.
                    index += 1
            return index < len(parts) and wrapped_launch(parts[index:])
        return False
    if not isinstance(command, str):
        return False
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return True
    agent_names = JUNO_COMMANDS | DIRECT_AGENT_EXECUTABLES
    has_control, active_expansion = active_shell_syntax(command)
    has_agent = any(Path(token.strip("`$")).name in agent_names for token in tokens)
    if active_expansion and not has_agent:
        agent_pattern = re.compile(
            rf"(?<![A-Za-z0-9_-])(?:{'|'.join(re.escape(name) for name in sorted(agent_names))})(?![A-Za-z0-9_-])"
        )
        has_agent = bool(agent_pattern.search(command))
    if has_agent and (has_control or active_expansion):
        return True

    # Shell/eval/command wrappers hide their executable in a quoted argument.
    # Inspect that argument as shell source instead of searching every quoted
    # token: prompt-generation commands may legitimately write text such as
    # "yy pi ..." without executing it.
    parts = effective_command_argv(command)
    if not parts:
        return False
    executable = Path(parts[0]).name

    def launches_agent(value: str) -> bool:
        nested = effective_command_argv(value)
        if not nested:
            return False
        nested_executable = Path(nested[0]).name
        return (
            nested_executable in DIRECT_AGENT_EXECUTABLES
            or (nested_executable in JUNO_COMMANDS and juno_subagent_name(nested) in DIRECT_AGENT_EXECUTABLES)
            or compound_agent_shell_command(value, _depth=_depth + 1)
        )

    if executable in {"bash", "dash", "ksh", "sh", "zsh"}:
        for index, part in enumerate(parts[1:], start=1):
            if part.startswith("-") and "c" in part[1:] and index + 1 < len(parts):
                return launches_agent(parts[index + 1])
        return False
    if executable == "eval":
        return len(parts) > 1 and launches_agent(" ".join(parts[1:]))
    if executable in {"builtin", "command", "exec"}:
        index = 1
        while index < len(parts) and (parts[index] == "--" or parts[index].startswith("-")):
            option = parts[index]
            index += 1
            if executable == "exec" and option != "--" and "a" in option[1:] and not option.split("a", 1)[1]:
                index += 1
        return index < len(parts) and launches_agent(" ".join(parts[index:]))
    return False


def juno_command_name(command: Any) -> str | None:
    parts = command_argv(command)
    if not parts:
        return None
    executable = Path(parts[0]).name
    return executable if executable in JUNO_COMMANDS else None


def juno_subagent_name(command: Any) -> str | None:
    parts = command_argv(command)
    if not parts:
        return None
    executable = Path(parts[0]).name
    if executable == "ypl":
        return "pi"
    if executable not in {"yylo", "yy"}:
        return None
    boolean_options = {
        "--quiet", "--silent", "-q", "--live", "--no-color", "--enable-feedback",
        "--continue", "--til-completion", "--until-completion", "--run-until-completion",
        "--till-complete", "--no-stale-check", "--force-update", "--no-hooks", "--no-hook",
    }
    value_options = {
        "-b", "--backend", "-m", "--model", "-c", "--config", "-l", "--log-file",
        "--log-level", "--agents", "--mcp-timeout", "--stale-threshold",
        "--on-hourly-limit", "--thinking",
    }
    variadic_options = {"--tools", "--allowed-tools", "--disallowed-tools", "--append-allowed-tools", "--pre-run-hook"}
    idx = 1
    while idx < len(parts):
        part = parts[idx]
        if part in boolean_options:
            idx += 1
            continue
        if part in {"--verbose", "-v"}:
            idx += 1
            if idx < len(parts) and not parts[idx].startswith("-"):
                idx += 1
            continue
        if part.startswith("--verbose="):
            idx += 1
            continue
        if part in variadic_options:
            idx += 1
            while idx < len(parts) and not parts[idx].startswith("-"):
                idx += 1
            continue
        if any(part.startswith(f"{option}=") for option in variadic_options):
            idx += 1
            continue
        if part in {"-s", "--subagent"}:
            return parts[idx + 1] if idx + 1 < len(parts) and parts[idx + 1] else None
        if part.startswith("--subagent="):
            return part.split("=", 1)[1] or None
        if part in value_options:
            idx += 2
            continue
        if any(part.startswith(f"{option}=") for option in value_options if option.startswith("--")):
            idx += 1
            continue
        if len(part) > 2 and part.startswith(("-b", "-m", "-c", "-l")):
            idx += 1
            continue
        return part if part in {"pi", "claude", "codex", "gemini", "cursor"} else None
    return None


ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$", re.S)


def effective_command_argv(command: Any) -> list[str]:
    parts = command_argv(command)
    while True:
        while parts and ENV_ASSIGNMENT_RE.fullmatch(parts[0]):
            parts = parts[1:]
        if not parts or Path(parts[0]).name != "env":
            return parts
        index = 1
        while index < len(parts):
            part = parts[index]
            if part == "--":
                parts = parts[index + 1:]
                break
            if part in {"-i", "--ignore-environment"} or ENV_ASSIGNMENT_RE.fullmatch(part):
                index += 1
                continue
            if part in {"-u", "--unset", "-P", "-C", "--chdir"}:
                index += 2
                continue
            if part.startswith(("--unset=", "--chdir=")):
                index += 1
                continue
            if part in {"-S", "--split-string"} and index + 1 < len(parts):
                try:
                    parts = shlex.split(parts[index + 1], posix=True) + parts[index + 2:]
                except ValueError:
                    return []
                break
            if part.startswith("--split-string=") or (part.startswith("-S") and part != "-S"):
                value = part.split("=", 1)[1] if part.startswith("--") else part[2:]
                try:
                    parts = shlex.split(value, posix=True) + parts[index + 1:]
                except ValueError:
                    return []
                break
            if part.startswith("-"):
                return []
            parts = parts[index:]
            break
        else:
            return []
    return []


def direct_agent_executable(command: Any) -> str | None:
    parts = effective_command_argv(command)
    executable = Path(parts[0]).name if parts else ""
    return executable if executable in DIRECT_AGENT_EXECUTABLES else None


def is_bare_pi_command(command: Any) -> bool:
    return direct_agent_executable(command) == "pi"


def is_canonical_yy_pi_command(command: Any) -> bool:
    parts = effective_command_argv(command)
    return bool(parts) and Path(parts[0]).name == "yy" and juno_subagent_name(parts) == "pi"


def has_resume_or_continue(command: Any) -> bool:
    for token in effective_command_argv(command):
        if token == "--":
            return False
        if (
            token in {"--resume", "--continue", "continue", "cc", "-r"}
            or token.startswith(("--resume=", "--continue="))
            or (len(token) > 2 and token.startswith("-r"))
        ):
            return True
    return False


MODEL_PROVIDER_ENV_RE = re.compile(r"(?:^|_)(?:MODEL|PROVIDER)$", re.I)


def command_environment_assignments(command: Any) -> list[str]:
    parts = command_argv(command)
    assignments: list[str] = []
    while True:
        while parts and ENV_ASSIGNMENT_RE.fullmatch(parts[0]):
            assignments.append(parts.pop(0))
        if not parts or Path(parts[0]).name != "env":
            return assignments
        index = 1
        while index < len(parts):
            part = parts[index]
            if part == "--":
                parts = parts[index + 1:]
                break
            if ENV_ASSIGNMENT_RE.fullmatch(part):
                assignments.append(part)
                index += 1
                continue
            if part in {"-i", "--ignore-environment"}:
                index += 1
                continue
            if part in {"-u", "--unset", "-P", "-C", "--chdir"}:
                index += 2
                continue
            if part.startswith(("--unset=", "--chdir=")):
                index += 1
                continue
            if part in {"-S", "--split-string"} and index + 1 < len(parts):
                try:
                    parts = shlex.split(parts[index + 1], posix=True) + parts[index + 2:]
                except ValueError:
                    return assignments
                break
            if part.startswith("--split-string=") or (part.startswith("-S") and part != "-S"):
                value = part.split("=", 1)[1] if part.startswith("--") else part[2:]
                try:
                    parts = shlex.split(value, posix=True) + parts[index + 1:]
                except ValueError:
                    return assignments
                break
            if part.startswith("-"):
                return assignments
            parts = parts[index:]
            break
        else:
            return assignments
    return assignments


def load_workflow_model_policy(project_root: Path) -> dict[str, Any]:
    config_path = (project_root / ".juno_task" / "config.json").resolve()
    if not config_path.is_file():
        allowlist: list[str] = []
        config_sha256: str | None = None
    else:
        raw = config_path.read_bytes()
        config_sha256 = sha256_bytes(raw)
        try:
            config = json.loads(raw)
        except Exception as error:
            raise WorkflowError(f"workflow model policy config is invalid JSON: {config_path}") from error
        if not isinstance(config, dict):
            raise WorkflowError(f"workflow model policy config must be a JSON object: {config_path}")
        value = config.get("workflowModels", [])
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise WorkflowError("project config workflowModels must be an array of strings")
        allowlist = list(value)
        if any(not item or item != item.strip() for item in allowlist):
            raise WorkflowError("project config workflowModels entries must be non-empty and already trimmed")
        if len(set(allowlist)) != len(allowlist):
            raise WorkflowError("project config workflowModels entries must be unique")
    return {
        "config_path": str(config_path),
        "config_sha256": config_sha256,
        "workflow_models": allowlist,
        "workflow_models_sha256": canonical_sha256(allowlist),
    }


def parse_pi_model_selection(command: Any, *, context: str) -> dict[str, Any]:
    parts = effective_command_argv(command)
    assignments = command_environment_assignments(command)
    assignment_names = [item.split("=", 1)[0] for item in assignments]
    hidden = [name for name in assignment_names if MODEL_PROVIDER_ENV_RE.search(name)]
    if hidden:
        raise WorkflowError(f"{context} must not select model/provider through environment assignment {hidden[0]}")
    alternate_config = [name for name in assignment_names if re.search(r"(?:^|_)CONFIG(?:_FILE|_PATH)?$", name, re.I)]
    if alternate_config:
        raise WorkflowError(f"{context} must not select an alternate config through environment assignment {alternate_config[0]}")
    hidden_args = [name for name in assignment_names if "ADDITIONAL_ARGS" in name.upper()]
    if hidden_args:
        raise WorkflowError(f"{context} must not inject additional args through environment assignment {hidden_args[0]}")
    for part in parts:
        if part == "--":
            break
        if part == "--additional-args" or part.startswith("--additional-args="):
            raise WorkflowError(f"{context} must not use --additional-args in a managed yy pi launch")
        if part in {"-c", "--config"} or part.startswith("--config=") or (len(part) > 2 and part.startswith("-c")):
            raise WorkflowError(f"{context} must not select an alternate config")

    values: dict[str, list[str]] = {"provider": [], "model": []}
    index = 0
    while index < len(parts):
        part = parts[index]
        if part == "--":
            break
        kind = "provider" if part == "--provider" else "model" if part in {"-m", "--model"} else ""
        if kind:
            if index + 1 >= len(parts) or not parts[index + 1] or parts[index + 1].startswith("-"):
                raise WorkflowError(f"{context} has a missing value for {part}")
            values[kind].append(parts[index + 1])
            index += 2
            continue
        if part.startswith("--provider="):
            values["provider"].append(part.split("=", 1)[1])
        elif part.startswith("--model="):
            values["model"].append(part.split("=", 1)[1])
        elif len(part) > 2 and part.startswith("-m"):
            raise WorkflowError(f"{context} has malformed model selector flag {part}")
        index += 1
    if len(values["provider"]) > 1 or len(values["model"]) > 1:
        raise WorkflowError(f"{context} has duplicate model/provider selector arguments")
    provider = values["provider"][0] if values["provider"] else None
    model = values["model"][0] if values["model"] else None
    for kind, value in (("provider", provider), ("model", model)):
        if value is not None and (not value or value != value.strip() or any(ch.isspace() for ch in value)):
            raise WorkflowError(f"{context} has malformed {kind} selector")
    if provider is not None and (provider.startswith(":") or "/" in provider):
        raise WorkflowError(f"{context} has malformed provider selector")
    if model == ":":
        raise WorkflowError(f"{context} has malformed model selector")
    if provider and not model:
        raise WorkflowError(f"{context} explicit --provider requires explicit --model")
    if provider and model and (model.startswith(":") or "/" in model):
        raise WorkflowError(f"{context} provider plus shorthand or qualified model is ambiguous")
    if model and "/" in model:
        provider_part, separator, model_part = model.partition("/")
        if not separator or not provider_part or not model_part or "/" in model_part:
            raise WorkflowError(f"{context} has malformed model selector")
    normalized = f"{provider}/{model}" if provider and model else model
    return {
        "explicit": normalized is not None,
        "provider": provider,
        "model": model,
        "normalized_selector": normalized,
    }


def validate_pi_launch_policy(
    step: dict[str, Any], *, context: str, policy: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    command = step.get("command")
    if compound_agent_shell_command(command):
        raise WorkflowError(f"{context} must use one direct agent command or an argv list, not compound shell syntax")
    raw_parts = command_argv(command)
    effective_parts = effective_command_argv(command)
    raw_prefix = list(raw_parts)
    while raw_prefix and ENV_ASSIGNMENT_RE.fullmatch(raw_prefix[0]):
        raw_prefix.pop(0)
    if raw_prefix and Path(raw_prefix[0]).name == "env" and not effective_parts:
        raise WorkflowError(f"{context} uses an unsupported env wrapper; launch directly through yy pi")
    direct = direct_agent_executable(command)
    if direct == "pi":
        raise WorkflowError(f"{context} must launch through yy pi, not bare pi")
    if direct:
        raise WorkflowError(f"{context} must launch through yy pi, not direct agent CLI {direct}")
    if not is_canonical_yy_pi_command(command):
        return None
    selection = parse_pi_model_selection(command, context=context)
    if selection["explicit"]:
        allowed = (policy or {}).get("workflow_models", [])
        if selection["normalized_selector"] not in allowed:
            raise WorkflowError(
                f"{context} explicit selector {selection['normalized_selector']!r} is not exactly allowlisted by workflowModels"
            )
    return selection


def extract_model_from_command(command: Any) -> str | None:
    parts = command_argv(command)
    for idx, part in enumerate(parts):
        if part in {"-m", "--model"} and idx + 1 < len(parts):
            return parts[idx + 1]
        if part.startswith("--model="):
            return part.split("=", 1)[1]
    return None


def resolve_continue_scope_from_juno(
    project_root: Path,
    parent_pid: int,
    command: Any,
) -> dict[str, str]:
    """Ask the selected Juno executable for its TypeScript-owned scope identity."""
    parts = command_argv(command)
    executable = parts[0] if parts and Path(parts[0]).name in {"yy", "yylo"} else None
    if not executable:
        # ypl is a prompt shortcut (`yylo pi --live`), not a control-command API.
        executable = next((path for name in ("yy", "yylo") if (path := shutil.which(name))), None)
    if not executable:
        raise WorkflowError("cannot resolve continue scope: yy or yylo was not found")

    completed = subprocess.run(
        [
            executable,
            "continue-scope",
            "--json",
            "--cwd",
            str(project_root),
            "--parent-pid",
            str(parent_pid),
        ],
        cwd=project_root,
        env=child_process_environment(dict(os.environ)),
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown error").strip()
        raise WorkflowError(f"yylo continue-scope failed: {detail}")
    try:
        payload = json.loads(completed.stdout)
    except Exception as error:
        raise WorkflowError("yylo continue-scope returned invalid JSON") from error

    scope_hash = str(payload.get("fullHash") or "") if isinstance(payload, dict) else ""
    session_key = str(payload.get("sessionEnvKey") or "") if isinstance(payload, dict) else ""
    settings_key = str(payload.get("settingsEnvKey") or "") if isinstance(payload, dict) else ""
    if not re.fullmatch(r"SCOPE_[A-F0-9]{16}", scope_hash):
        raise WorkflowError("yylo continue-scope returned an invalid fullHash")
    if not re.fullmatch(r"YYLO_LAST_SESSION_ID_SCOPE_[A-F0-9]{16}", session_key):
        raise WorkflowError("yylo continue-scope returned an invalid sessionEnvKey")
    if not re.fullmatch(r"YYLO_LAST_EXECUTION_SETTINGS_SCOPE_[A-F0-9]{16}", settings_key):
        raise WorkflowError("yylo continue-scope returned an invalid settingsEnvKey")
    return {
        "scope_hash": scope_hash,
        "session_env_key": session_key,
        "settings_env_key": settings_key,
        "session_id": str(payload.get("sessionId") or ""),
        "executable": executable,
    }


def build_continue_settings(command: Any) -> dict[str, Any] | None:
    subagent = juno_subagent_name(command)
    if not subagent:
        return None
    settings: dict[str, Any] = {"version": 1, "subagent": subagent}
    model = extract_model_from_command(command)
    if model:
        settings["model"] = model
    return settings


def read_child_continue_session(project_root: Path, command: Any) -> str | None:
    # Top-level yy/yylo commands persist their own continue snapshot, but when
    # launched by this runner without terminal markers their PPID fallback is the
    # workflow_runner process. Adopt that child snapshot, then persist it to the
    # caller's shell scope so `workflow_runner.sh ... ; yy cc` works.
    child_context = resolve_continue_scope_from_juno(project_root, os.getpid(), command)
    return child_context["session_id"] or None


def persist_continue_context(project_root: Path, session_id: str, command: Any) -> dict[str, str] | None:
    settings = build_continue_settings(command)
    if not settings:
        return None
    context = resolve_continue_scope_from_juno(project_root, os.getppid(), command)
    serialized_settings = json.dumps(settings, separators=(",", ":"))
    completed = subprocess.run(
        [
            context["executable"], "continue-scope", "--json", "--cwd", str(project_root),
            "--parent-pid", str(os.getppid()), "--handoff-session", session_id,
            "--handoff-settings", serialized_settings,
        ],
        cwd=project_root,
        env=child_process_environment(dict(os.environ)),
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown error").strip()
        raise WorkflowError(f"yylo continuity handoff failed: {detail}")
    return {**context, "metadata_file": "session_continuity.v2.json", "settings": serialized_settings}


def select_continue_step(workflow: dict[str, Any], session_candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Choose the yy cc handoff from executed Juno invocations in workflow order.

    Without an explicit override, the last successful candidate that captured a
    session_id wins. With continue_from_step, the named step/summary must exist
    in the candidate stream and must have produced a session_id.
    """
    selected = str(workflow.get("continue_from_step") or "").strip()
    if not selected:
        for item in reversed(session_candidates):
            if item.get("status") == "success" and str(item.get("session_id") or "").strip():
                return item
        return None
    for item in session_candidates:
        if item.get("id") == selected or item.get("name") == selected:
            if str(item.get("session_id") or "").strip():
                return item
            raise WorkflowError(f"continue_from_step '{selected}' selected {session_label(item)}, but it did not produce a session_id")
    raise WorkflowError(f"continue_from_step '{selected}' did not match an executed Juno invocation with a session_id")


def session_label(item: dict[str, Any]) -> str:
    if item.get("kind") == "summary":
        return "summary [summary]"
    return f"step {item['index']} [{item['id']}]"


def print_session_summary(session_steps: list[dict[str, Any]], persisted: dict[str, str] | None) -> None:
    if not session_steps:
        return
    print("\nSession ID(s):")
    for item in session_steps:
        print(f"  {session_label(item)}: {item['session_id']}")
    if persisted:
        selected_label = persisted.get("selected_label")
        if selected_label:
            print(f"  handoff: {selected_label} persisted for yy cc ({persisted['scope_hash']})")
        else:
            print(f"  handoff: last session persisted for yy cc ({persisted['scope_hash']})")
        print(f"  metadata_file: {persisted['metadata_file']}")


SESSION_FOOTER_TOKEN_RE = re.compile(
    r"\b(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|session-[A-Za-z0-9_.:-]+)\b"
)


def extract_footer_session_id(text: str) -> str | None:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not re.search(r"\bsession\s+id\(s\)\s*:\s*$", line.strip(), re.I):
            continue
        for candidate_line in lines[index + 1 :]:
            stripped = candidate_line.strip()
            if not stripped:
                break
            match = SESSION_FOOTER_TOKEN_RE.search(stripped)
            if match:
                return match.group(0)
    return None


def extract_session_id(stdout: str, stderr: str) -> str | None:
    for text in (stdout, stderr):
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                for key in ("session_id", "sessionId", "session"):
                    val = payload.get(key)
                    if isinstance(val, str) and val:
                        return val
            match = re.search(r"session[_ -]?id[=:]\s*([A-Za-z0-9_.:-]+)", stripped, re.I)
            if match:
                return match.group(1)
        footer_session_id = extract_footer_session_id(text)
        if footer_session_id:
            return footer_session_id
    return None


def write_text(path: Path, text: str) -> None:
    """Atomically persist one artifact in its destination directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            # Some filesystems do not support directory fsync; storage exhaustion
            # and other persistence failures must still fail closed.
            if exc.errno not in {errno.EINVAL, errno.ENOTSUP, errno.EBADF}:
                raise
    finally:
        temporary.unlink(missing_ok=True)


def remove_persisted_marker(path: Path) -> None:
    path.unlink(missing_ok=True)
    try:
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        if exc.errno not in {errno.EINVAL, errno.ENOTSUP, errno.EBADF}:
            raise


def inject_interruption(point: str) -> None:
    """Deterministic failure injection used by the adversarial persistence tests."""
    requested = os.environ.get("JUNO_WORKFLOW_TEST_INTERRUPT_AT", "")
    if requested != point:
        return
    mode = os.environ.get("JUNO_WORKFLOW_TEST_INTERRUPT_MODE", "exit")
    if mode == "enospc":
        raise OSError(errno.ENOSPC, f"injected ENOSPC at {point}")
    os._exit(86)


def parse_vars(values: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in values:
        if "=" not in item:
            raise WorkflowError(f"--var must use key=value form: {item}")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise WorkflowError("--var key cannot be empty")
        parsed[key] = value
    return parsed


def step_should_fail_process(step: dict[str, Any]) -> bool:
    if step.get("managed_agent") is not None:
        return True
    for key in ("fail_workflow", "fail_on_error", "exit_on_failure", "fail_fast"):
        if bool(step.get(key, False)):
            return True
    return False


def step_capture_enabled(step: dict[str, Any], command: Any) -> bool:
    if step.get("managed_agent") is not None:
        return False
    if "capture_session" in step and not bool(step.get("capture_session")):
        return False
    if "capture" in step and not bool(step.get("capture")):
        return False
    if "capture_session" in step:
        return bool(step.get("capture_session"))
    if "capture" in step:
        return bool(step.get("capture"))
    return detect_juno_command(command)


def read_capture_payload(capture_path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not capture_path.exists():
        return None, None
    try:
        payload = json.loads(capture_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, f"invalid capture JSON at {capture_path}: {exc}"
    if not isinstance(payload, dict):
        return None, f"capture JSON at {capture_path} must be an object"
    return payload, None


def load_child_step_evidence(
    child_dir: Path, workflow_id: str, run_id: str, parent_step_id: str, parent_digest: str,
) -> list[dict[str, Any]]:
    if not child_dir.exists():
        return []
    events: list[dict[str, Any]] = []
    for event_path in sorted(child_dir.glob("*.event.json")):
        try:
            event = json.loads(event_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise WorkflowError(f"child_step[{parent_step_id}]: invalid event {event_path}: {exc}") from exc
        if not isinstance(event, dict) or event.get("schema_version") != "juno_workflow_child_step.v1":
            raise WorkflowError(f"child_step[{parent_step_id}]: unsupported event schema")
        child_id = str(event.get("child_id") or "")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", child_id):
            raise WorkflowError(f"child_step[{parent_step_id}]: invalid child_id")
        expected = {
            "role": "actual_target_review", "parent_workflow_id": workflow_id,
            "parent_run_id": run_id, "parent_step_id": parent_step_id,
            "parent_step_digest": parent_digest, "invocation_mode": "fresh_session",
        }
        for field, value in expected.items():
            if event.get(field) != value:
                raise WorkflowError(f"child_step[{parent_step_id}/{child_id}].{field}: expected={value!r} actual={event.get(field)!r}")
        for hash_field in ("rendered_command_sha256", "rendered_argv_sha256"):
            if not re.fullmatch(r"[0-9a-f]{64}", str(event.get(hash_field) or "")):
                raise WorkflowError(f"child_step[{parent_step_id}/{child_id}]: {hash_field} missing")
        prompt_hash = event.get("rendered_prompt_sha256")
        if prompt_hash is not None and not re.fullmatch(r"[0-9a-f]{64}", str(prompt_hash)):
            raise WorkflowError(f"child_step[{parent_step_id}/{child_id}]: rendered prompt hash invalid")
        if event.get("transport_status") not in {"success", "failed"} or event.get("semantic_outcome") not in {"accepted", "rejected", "failed"}:
            raise WorkflowError(f"child_step[{parent_step_id}/{child_id}]: invalid transport or semantic outcome")
        if not isinstance(event.get("exit_code"), int) or not isinstance(event.get("duration_seconds"), (int, float)):
            raise WorkflowError(f"child_step[{parent_step_id}/{child_id}]: timing/exit evidence missing")
        if not re.fullmatch(r"[0-9a-f]{40}", str(event.get("reviewed_target_sha") or "")):
            raise WorkflowError(f"child_step[{parent_step_id}/{child_id}]: reviewed target SHA missing")
        artifacts = event.get("artifacts")
        required_artifacts = {"stdout", "stderr", "response", "capture", "review_receipt"}
        if not isinstance(artifacts, dict) or set(artifacts) != required_artifacts:
            raise WorkflowError(f"child_step[{parent_step_id}/{child_id}]: exact artifact set required")
        for artifact_id, evidence in artifacts.items():
            if not isinstance(evidence, dict):
                raise WorkflowError(f"child_step[{parent_step_id}/{child_id}].artifact[{artifact_id}]: evidence missing")
            path = Path(str(evidence.get("path") or ""))
            try:
                path.resolve().relative_to(child_dir.resolve())
            except ValueError:
                raise WorkflowError(f"child_step[{parent_step_id}/{child_id}].artifact[{artifact_id}]: cross-step path")
            if not path.is_file() or file_sha256(path) != evidence.get("sha256"):
                raise WorkflowError(f"child_step[{parent_step_id}/{child_id}].artifact[{artifact_id}]: hash mismatch")
        event["event"] = {"path": str(event_path.resolve()), "sha256": file_sha256(event_path)}
        events.append(event)
    ids = [str(event["child_id"]) for event in events]
    if len(ids) != len(set(ids)):
        raise WorkflowError(f"child_step[{parent_step_id}]: duplicate child ID")
    return events


def verify_persisted_child_steps(child_steps: Any, parent_step_id: str, run_dir: Path) -> None:
    if child_steps is None:
        return
    if not isinstance(child_steps, list):
        raise WorkflowError(f"child_step[{parent_step_id}]: checkpoint evidence must be a list")
    for child in child_steps:
        child_id = str(child.get("child_id") or "") if isinstance(child, dict) else ""
        if not isinstance(child, dict) or child.get("schema_version") != "juno_workflow_child_step.v1":
            raise WorkflowError(f"child_step[{parent_step_id}/{child_id}]: unsupported checkpoint evidence")
        for artifact_id, evidence in (child.get("artifacts") or {}).items():
            path = Path(str(evidence.get("path") or ""))
            try:
                path.resolve().relative_to(run_dir.resolve())
            except ValueError:
                raise WorkflowError(f"child_step[{parent_step_id}/{child_id}].artifact[{artifact_id}]: cross-run path")
            if not path.is_file() or file_sha256(path) != evidence.get("sha256"):
                raise WorkflowError(f"child_step[{parent_step_id}/{child_id}].artifact[{artifact_id}]: hash mismatch")
        event_evidence = child.get("event") or {}
        event_path = Path(str(event_evidence.get("path") or ""))
        if not event_path.is_file() or file_sha256(event_path) != event_evidence.get("sha256"):
            raise WorkflowError(f"child_step[{parent_step_id}/{child_id}].event: hash mismatch")


def make_summary(workflow: dict[str, Any], context: dict[str, Any], failed_steps: list[str], dry_run: bool) -> str:
    explicit = workflow.get("summary")
    if isinstance(explicit, str) and explicit.strip():
        rendered = render(explicit, context)
        return str(rendered).rstrip() + "\n"
    if isinstance(explicit, dict) and explicit.get("template"):
        rendered = render(str(explicit["template"]), context)
        return str(rendered).rstrip() + "\n"
    lines = ["# Workflow Summary", ""]
    lines.append(f"Workflow: {context.get('workflow_id', workflow.get('name', 'unnamed'))}")
    lines.append(f"Run ID: {context.get('run_id')}")
    lines.append(f"Mode: {'dry-run' if dry_run else 'execute'}")
    lines.append(f"Failed steps: {len(failed_steps)}")
    lines.append("")
    lines.append("| Step | Status | Exit | Duration (s) |")
    lines.append("| --- | --- | ---: | ---: |")
    for step_id, result in context["steps"].items():
        lines.append(
            f"| {step_id} | {result.get('status')} | {result.get('exit_code')} | {result.get('duration_seconds')} |"
        )
    lines.append("")
    return "\n".join(lines)


def append_live_log(path: Path | None, text: str) -> None:
    if path is None:
        return
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()


def _candidate_git(candidate: Path, *args: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(candidate), *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )
    if completed.returncode:
        raise WorkflowError(
            f"candidate_read_only git {' '.join(args)} failed: "
            f"{completed.stderr.decode(errors='replace').strip()}"
        )
    return completed.stdout


def _bounded_digest(value: bytes) -> dict[str, Any]:
    """Persist equality evidence without retaining private or unbounded Git payloads."""
    return {"sha256": hashlib.sha256(value).hexdigest(), "bytes": len(value)}


def canonical_candidate_root(declared_path: str) -> Path:
    candidate = Path(declared_path).expanduser()
    if not candidate.is_absolute():
        raise WorkflowError("candidate_read_only path must be an absolute canonical Git worktree root")
    try:
        canonical = candidate.resolve(strict=True)
    except OSError as exc:
        raise WorkflowError(f"candidate_read_only path is unavailable: {declared_path}") from exc
    if str(candidate) != str(canonical):
        raise WorkflowError(
            f"candidate_read_only path must be exact canonical path (no alias or traversal): {declared_path}"
        )
    top_level = Path(_candidate_git(canonical, "rev-parse", "--path-format=absolute", "--show-toplevel").decode().strip())
    try:
        top_level = top_level.resolve(strict=True)
    except OSError as exc:
        raise WorkflowError("candidate_read_only Git worktree top-level is unavailable") from exc
    if canonical != top_level:
        raise WorkflowError(
            f"candidate_read_only path must equal exact Git worktree top-level: declared={canonical} top={top_level}"
        )
    return canonical


def ensure_external_orchestration(candidate: Path, orchestration_cwd: Path) -> None:
    orchestration = orchestration_cwd.resolve(strict=True)
    try:
        orchestration.relative_to(candidate)
    except ValueError:
        return
    raise WorkflowError(
        f"candidate_read_only orchestration cwd must be outside candidate root: {orchestration}"
    )


def snapshot_candidate_identity(candidate: Path, expected_sha: str, *, require_expected: bool = True) -> dict[str, Any]:
    candidate = canonical_candidate_root(str(candidate))
    if not re.fullmatch(r"[0-9a-f]{40}", expected_sha):
        raise WorkflowError(f"candidate_read_only SHA is not a full commit identity: {expected_sha}")
    head = _candidate_git(candidate, "rev-parse", "HEAD").decode().strip()
    if require_expected and head != expected_sha:
        raise WorkflowError(f"candidate_read_only SHA mismatch: expected {expected_sha}, observed {head}")
    git_common_dir = Path(
        _candidate_git(candidate, "rev-parse", "--path-format=absolute", "--git-common-dir").decode().strip()
    ).resolve(strict=True)
    git_dir = Path(
        _candidate_git(candidate, "rev-parse", "--path-format=absolute", "--git-dir").decode().strip()
    ).resolve(strict=True)
    index_path = Path(
        _candidate_git(candidate, "rev-parse", "--path-format=absolute", "--git-path", "index").decode().strip()
    ).resolve()
    status = _candidate_git(candidate, "status", "--porcelain=v2", "-z", "--untracked-files=all")
    index_bytes = index_path.read_bytes() if index_path.is_file() else b""
    logical_index = _candidate_git(candidate, "ls-files", "--stage", "-z")
    root_stat = candidate.stat()
    git_dir_stat = git_dir.stat()
    topology = canonical_sha256({
        "candidate_device": root_stat.st_dev,
        "candidate_inode": root_stat.st_ino,
        "git_dir_device": git_dir_stat.st_dev,
        "git_dir_inode": git_dir_stat.st_ino,
        "git_common_dir": str(git_common_dir),
        "git_dir": str(git_dir),
    })
    return {
        "path": str(candidate),
        "head": head,
        "worktree_identity_sha256": topology,
        "git_common_dir_sha256": hashlib.sha256(str(git_common_dir).encode()).hexdigest(),
        "git_dir_sha256": hashlib.sha256(str(git_dir).encode()).hexdigest(),
        "raw_index": _bounded_digest(index_bytes),
        "logical_index": _bounded_digest(logical_index),
        "status_porcelain_v2_z": _bounded_digest(status),
    }


def candidate_identity_changes(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    fields = (
        "path", "head", "worktree_identity_sha256", "git_common_dir_sha256", "git_dir_sha256",
        "raw_index", "logical_index", "status_porcelain_v2_z",
    )
    return [field for field in fields if before.get(field) != after.get(field)]


def canonical_stage_root(declared_path: str) -> Path:
    candidate = Path(declared_path).expanduser()
    if not candidate.is_absolute():
        raise WorkflowError(f"stage_boundary.root must be absolute after rendering: {declared_path}")
    try:
        canonical = candidate.resolve(strict=True)
        top = Path(_candidate_git(canonical, "rev-parse", "--path-format=absolute", "--show-toplevel").decode().strip()).resolve(strict=True)
    except OSError as exc:
        raise WorkflowError(f"stage_boundary.root is unavailable: {declared_path}") from exc
    if canonical != top:
        raise WorkflowError(f"stage_boundary.root must equal the exact Git worktree top-level: {declared_path}")
    return canonical


def normalize_stage_admitted_paths(step_id: str, values: Any) -> list[str]:
    if not isinstance(values, list) or not values:
        raise WorkflowError(f"step {step_id} stage_boundary.admitted_paths must be a non-empty path list")
    normalized: list[str] = []
    for item in values:
        if not isinstance(item, str):
            raise WorkflowError(f"step {step_id} stage_boundary admitted paths must be strings")
        candidate = Path(item)
        clean = item.rstrip("/")
        if (not clean or clean == "." or candidate.is_absolute() or item != candidate.as_posix()
                or ".." in candidate.parts or ".git" in candidate.parts):
            raise WorkflowError(f"step {step_id} stage_boundary admitted path is unsafe: {item}")
        normalized.append(clean)
    if len(set(normalized)) != len(normalized):
        raise WorkflowError(f"step {step_id} stage_boundary admitted paths must be unique")
    return normalized


def stage_content_snapshot(root: Path) -> tuple[dict[str, Any], dict[str, str]]:
    names = _candidate_git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    paths = sorted({item.decode("utf-8") for item in names.split(b"\0") if item})
    content: dict[str, str] = {}
    for relative in paths:
        path = root / relative
        if path.is_symlink():
            content[relative] = "symlink:" + hashlib.sha256(os.readlink(path).encode()).hexdigest()
        elif path.is_file():
            content[relative] = f"file:{path.stat().st_mode:o}:" + file_sha256(path)
        elif path.exists():
            content[relative] = "other"
        else:
            content[relative] = "missing"
    status = _candidate_git(root, "status", "--porcelain=v2", "-z", "--untracked-files=all")
    evidence = {
        "root": str(root),
        "head": _candidate_git(root, "rev-parse", "HEAD").decode().strip(),
        "branch_ref": _candidate_git(root, "rev-parse", "--abbrev-ref", "HEAD").decode().strip(),
        "status_sha256": hashlib.sha256(status).hexdigest(),
        "content_sha256": canonical_sha256(content),
        "path_count": len(content),
    }
    return evidence, content


def stage_boundary_evidence(
    root: Path, admitted_paths: list[str], before: tuple[dict[str, Any], dict[str, str]]
) -> dict[str, Any]:
    after = stage_content_snapshot(root)
    changed = sorted(
        path for path in set(before[1]) | set(after[1]) if before[1].get(path) != after[1].get(path)
    )
    admitted = [item.rstrip("/") for item in admitted_paths]
    unexpected = [
        path for path in changed
        if not any(path == allowed or path.startswith(allowed + "/") for allowed in admitted)
    ]
    return {
        "schema_version": "juno_workflow_stage_boundary.v1",
        "root": str(root),
        "admitted_paths": admitted,
        "before": before[0],
        "after": after[0],
        "changed_paths": changed,
        "unexpected_paths": unexpected,
        "passed": not unexpected,
    }


def verify_candidate_guard_evidence(
    evidence: dict[str, Any], expected_sha256: str, *, orchestration_cwd: Path,
) -> dict[str, Any]:
    if evidence.get("schema_version") != "juno_candidate_read_only.v2" or evidence.get("passed") is not True:
        raise WorkflowError("candidate_read_only guard evidence is incomplete or unsuccessful")
    if canonical_sha256(evidence) != expected_sha256:
        raise WorkflowError("candidate_read_only guard evidence hash mismatch")
    candidate = canonical_candidate_root(str(evidence.get("candidate_path") or ""))
    ensure_external_orchestration(candidate, orchestration_cwd)
    current = snapshot_candidate_identity(candidate, str(evidence.get("expected_sha") or ""))
    if candidate_identity_changes(evidence.get("after") or {}, current):
        raise WorkflowError("candidate_read_only guard evidence no longer matches exact candidate identity")
    return evidence


def verify_candidate_guard_artifact(
    checkpoint: dict[str, Any], step_id: str, orchestration_cwd: Path,
) -> dict[str, Any] | None:
    anchor = checkpoint.get("candidate_read_only")
    if anchor is None:
        return None
    if not isinstance(anchor, dict):
        raise WorkflowError(f"candidate_read_only checkpoint[{step_id}] evidence missing")
    path = Path(str(anchor.get("path") or ""))
    if not path.is_file() or file_sha256(path) != anchor.get("sha256"):
        raise WorkflowError(f"candidate_read_only checkpoint[{step_id}] artifact hash mismatch")
    evidence = load_json_object(path, f"candidate_read_only checkpoint[{step_id}]")
    return verify_candidate_guard_evidence(evidence, str(anchor.get("evidence_sha256") or ""), orchestration_cwd=orchestration_cwd)


def canonical_admitted_task_root(declared_path: str) -> Path:
    root = Path(declared_path).expanduser()
    if not root.is_absolute():
        raise WorkflowError("generated edit admission task root must be absolute")
    try:
        canonical = root.resolve(strict=True)
    except OSError as exc:
        raise WorkflowError(f"generated edit admission task root is unavailable: {declared_path}") from exc
    if str(root) != str(canonical):
        raise WorkflowError("generated edit admission task root must be an exact canonical path")
    top = subprocess.run(
        ["git", "-C", str(canonical), "rev-parse", "--path-format=absolute", "--show-toplevel"],
        stdin=subprocess.DEVNULL, text=True, capture_output=True, check=False,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )
    if top.returncode != 0:
        raise WorkflowError("generated edit admission task root is not a Git worktree")
    if Path(top.stdout.strip()).resolve(strict=True) != canonical:
        raise WorkflowError("generated edit admission path is not the exact Git worktree root")
    return canonical


def current_persisted_task_authority(root: Path, admitted: dict[str, Any]) -> dict[str, Any]:
    """Re-resolve checkout authority at the final generated-write boundary."""
    resolver = Path(__file__).resolve().with_name("controller_resolver.py")
    if not resolver.is_file():
        raise WorkflowError("generated edit dispatch authority resolver is unavailable")
    resolver_env = dict(os.environ)
    # The runner exports its orchestration workspace role. It is assertion-only
    # and must neither reject nor confer the admitted checkout's current role.
    resolver_env.pop("JUNO_WORKSPACE_ROLE", None)
    try:
        completed = subprocess.run(
            [sys.executable, str(resolver), "--cwd", str(root), "--operation", "product-edit", "--format", "json"],
            stdin=subprocess.DEVNULL, text=True, capture_output=True, check=False,
            timeout=10, env=resolver_env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkflowError("generated edit dispatch persisted task authority recheck failed") from exc
    try:
        current = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise WorkflowError("generated edit dispatch persisted task authority recheck returned invalid evidence") from exc
    if completed.returncode != 0 or not isinstance(current, dict) or current.get("valid") is not True:
        raise WorkflowError("generated edit dispatch persisted task authority is invalid or unregistered")
    authority_fields = (
        "current_root", "role", "role_source", "role_base", "task_id", "manifest_identity",
        "create_receipt_sha256", "expected_paths_sha256", "role_authority",
    )
    if current.get("role") != "task" or any(current.get(key) != admitted.get(key) for key in authority_fields):
        raise WorkflowError("generated edit dispatch persisted task role/manifest authority changed")
    return current


def generated_dispatch_root(
    step: dict[str, Any], receipts: dict[str, dict[str, Any]], context: dict[str, Any],
    project_root: Path, completed_steps: dict[str, Any],
) -> Path:
    generated = step.get("generated_task_contract")
    if not isinstance(generated, dict) or generated.get("write_contract") == "read_only":
        return project_root
    if generated.get("role") == "review":
        raise WorkflowError(f"generated review step {step['id']} cannot obtain edit dispatch authority")
    receipt_id = str(generated.get("task_root_receipt") or "")
    contract = receipts[receipt_id]
    producer = contract["producer"]
    evidence = completed_steps.get(producer, {})
    receipt_evidence = (evidence.get("receipts") or {}).get(receipt_id)
    if not receipt_evidence:
        raise WorkflowError(f"step[{step['id']}].task_root_receipt[{receipt_id}]: producer evidence missing")
    path = receipt_path(contract, context, project_root)
    payload = load_json_object(path, f"receipt[{receipt_id}]")
    validate_receipt_payload(contract, payload, str(evidence["command_sha256"]), location=str(path))
    if file_sha256(path) != str(receipt_evidence.get("sha256") or ""):
        raise WorkflowError(f"step[{step['id']}].task_root_receipt[{receipt_id}]: artifact hash mismatch")
    current = payload.get("current") or {}
    workspace = payload.get("workspace") or {}
    root = canonical_admitted_task_root(str(current.get("root") or ""))
    if workspace.get("role") != "task" or workspace.get("current_root") != str(root):
        raise WorkflowError(f"step[{step['id']}].task_root_receipt[{receipt_id}]: workspace/path binding mismatch")
    authority = current_persisted_task_authority(root, workspace)
    expected_paths_hash = hashlib.sha256(json.dumps(
        payload.get("expected_paths") or [], sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    manifest = payload.get("manifest")
    if not isinstance(manifest, dict):
        manifest = {}
    try:
        manifest_path = Path(str(manifest["path"])).resolve(strict=True)
        manifest_hash = file_sha256(manifest_path) if manifest_path.is_file() else ""
    except (KeyError, OSError):
        manifest_hash = ""
    if (authority.get("expected_paths_sha256") != expected_paths_hash
            or manifest_hash != str(manifest.get("sha256") or "")
            or authority.get("create_receipt_sha256") != manifest_hash):
        raise WorkflowError(f"step[{step['id']}].task_root_receipt[{receipt_id}]: persisted task manifest evidence changed")
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], stdin=subprocess.DEVNULL,
        text=True, capture_output=True, check=False, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )
    branch = subprocess.run(
        ["git", "-C", str(root), "symbolic-ref", "-q", "HEAD"], stdin=subprocess.DEVNULL,
        text=True, capture_output=True, check=False, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )
    common = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        stdin=subprocess.DEVNULL, text=True, capture_output=True, check=False,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )
    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain=v2", "--untracked-files=all"],
        stdin=subprocess.DEVNULL, text=True, capture_output=True, check=False,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )
    actual_common = str(Path(common.stdout.strip()).resolve()) if common.returncode == 0 else ""
    if (head.returncode or common.returncode or status.returncode or status.stdout
            or head.stdout.strip() != current.get("head")
            or branch.stdout.strip() != current.get("branch_ref")
            or actual_common != current.get("git_common_dir") or current.get("clean") is not True):
        raise WorkflowError(f"step[{step['id']}].task_root_receipt[{receipt_id}]: exact task identity changed")
    return root


def execute_rendered_command(
    command: Any,
    project_root: Path,
    env: dict[str, str],
    live_log_path: Path | None = None,
    activity: dict[str, Any] | None = None,
    active_marker: Path | None = None,
    timeout_seconds: int | None = None,
    launch_ownership: StepLaunchOwnership | None = None,
) -> subprocess.CompletedProcess[str]:
    argv: Any = [str(part) for part in command] if isinstance(command, list) else str(command)
    shell = not isinstance(command, list)
    proc = subprocess.Popen(
        argv,
        shell=shell,
        cwd=str(project_root),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        bufsize=1,
        start_new_session=True,
    )
    if launch_ownership is not None:
        launch_ownership.record(proc.pid)
    if activity is not None and active_marker is not None:
        activity.update({"child_pid": proc.pid, "process_group_id": proc.pid})
        write_text(active_marker, json.dumps(activity, indent=2, sort_keys=True) + "\n")
    if live_log_path is None:
        try:
            stdout, stderr = proc.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            stdout, stderr = proc.communicate()
            raise subprocess.TimeoutExpired(argv, timeout_seconds or 0, output=stdout, stderr=stderr)
        return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    log_lock = threading.Lock()

    def relay(stream: Any, label: str, chunks: list[str]) -> None:
        if stream is None:
            return
        for chunk in iter(stream.readline, ""):
            chunks.append(chunk)
            with log_lock:
                append_live_log(live_log_path, f"[{label}] {chunk}")
        stream.close()

    threads = [
        threading.Thread(target=relay, args=(proc.stdout, "stdout", stdout_chunks), daemon=True),
        threading.Thread(target=relay, args=(proc.stderr, "stderr", stderr_chunks), daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        return_code = proc.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        for thread in threads:
            thread.join()
        raise subprocess.TimeoutExpired(
            argv, timeout_seconds or 0, output="".join(stdout_chunks), stderr="".join(stderr_chunks))
    for thread in threads:
        thread.join()
    return subprocess.CompletedProcess(argv, return_code, "".join(stdout_chunks), "".join(stderr_chunks))


def start_tmux_observer(out_dir: Path, workflow_id: str, run_id: str, requested_session: str | None) -> dict[str, Any]:
    tmux = shutil.which("tmux")
    if not tmux:
        raise WorkflowError("--tmux requires tmux to be installed and available on PATH")
    session = requested_session or safe_id(f"wf-{workflow_id}-{run_id[-14:-1]}", "workflow-observer")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", session):
        raise WorkflowError("--tmux-session must contain only letters, numbers, '.', '_', or '-'")
    existing = subprocess.run([tmux, "has-session", "-t", session], capture_output=True, text=True)
    if existing.returncode == 0:
        raise WorkflowError(f"tmux session already exists: {session}")

    live_log = out_dir / "workflow.live.log"
    observer_script = out_dir / "tmux_observer.sh"
    append_live_log(
        live_log,
        f"Workflow observer started\nworkflow={workflow_id}\nrun_id={run_id}\nout_dir={out_dir}\n\n",
    )
    observer_script.write_text(
        "#!/usr/bin/env bash\n"
        "set -u\n"
        f"printf '%s\\n' 'Juno workflow observer: {workflow_id}' 'Artifacts: {out_dir}' "
        "'This session remains available after workflow completion. Press Ctrl-C to stop following.'\n"
        f"exec tail -n +1 -F {shlex.quote(str(live_log))}\n",
        encoding="utf-8",
    )
    observer_script.chmod(0o755)
    launched = subprocess.run(
        [tmux, "new-session", "-d", "-s", session, str(observer_script)],
        capture_output=True,
        text=True,
    )
    if launched.returncode != 0:
        detail = (launched.stderr or launched.stdout or "unknown tmux error").strip()
        raise WorkflowError(f"could not create tmux observer session {session}: {detail}")
    print(f"Workflow observer tmux session: {session}")
    print(f"Attach: tmux attach -t {shlex.quote(session)}")
    print(f"Live log: {live_log}")
    return {
        "enabled": True,
        "session": session,
        "live_log": str(live_log),
        "observer_script": str(observer_script),
        "attach_command": f"tmux attach -t {shlex.quote(session)}",
    }


def build_command_env(
    project_root: Path,
    command: Any,
    capture_enabled: bool,
    capture_path: Path,
    tool_id: str,
    dry_run: bool,
) -> tuple[dict[str, str], str | None]:
    env = child_process_environment(dict(os.environ))
    is_juno_command = detect_juno_command(command)
    if is_juno_command:
        metadata_dir = capture_path.parent / "session_metadata"
        metadata_dir.mkdir(parents=True, exist_ok=True)
        env["YYLO_SESSION_METADATA_DIRECTORY"] = str(metadata_dir)
    if capture_enabled:
        env["JUNO_TOOL_ID"] = tool_id
        env["JUNO_SUBAGENT_CAPTURE_PATH"] = str(capture_path)
    else:
        env.pop("JUNO_TOOL_ID", None)
        env.pop("JUNO_SUBAGENT_CAPTURE_PATH", None)
    child_continue_session_before = (
        read_child_continue_session(project_root, command)
        if is_juno_command and capture_enabled and not dry_run
        else None
    )
    return env, child_continue_session_before


def apply_semantic_outcome_contract(step: dict[str, Any], result: dict[str, Any], dry_run: bool) -> None:
    expected = step.get("expected_outcomes")
    if expected is None:
        return
    if not isinstance(expected, list) or not expected or not all(isinstance(item, str) and item for item in expected):
        raise WorkflowError(f"step {step.get('id', '<unknown>')} expected_outcomes must be a non-empty string list")
    if dry_run or result.get("status") != "success":
        return
    matches = re.findall(
        r"(?m)^JUNO_WORKFLOW_OUTCOME:\s*([A-Za-z0-9_.-]+)\s*$",
        str(result.get("response") or ""),
    )
    if not matches:
        result["status"] = "failed"
        result["failure_reason"] = "missing JUNO_WORKFLOW_OUTCOME footer"
        return
    outcome = matches[-1]
    result["semantic_outcome"] = outcome
    if outcome not in expected:
        result["status"] = "failed"
        result["failure_reason"] = f"semantic outcome {outcome!r} is not one of {expected!r}"


def apply_agent_session_capture(
    result: dict[str, Any],
    project_root: Path,
    stdout: str,
    stderr: str,
    capture_path: Path,
    child_continue_session_before: str | None,
    dry_run: bool,
    *,
    use_capture_result_as_response: bool,
) -> None:
    if result.get("capture_enabled") and not dry_run:
        capture_payload, capture_warning = read_capture_payload(capture_path)
        if capture_warning:
            print(f"workflow_runner.sh: warning: {capture_warning}", file=sys.stderr)
            result["capture_warning"] = capture_warning
        if capture_payload is not None:
            result["capture"] = capture_payload
            session_id = capture_payload.get("session_id")
            capture_result = capture_payload.get("result")
            if isinstance(session_id, str):
                result["session_id"] = session_id
            if isinstance(capture_result, str):
                result["capture_result"] = capture_result
                if use_capture_result_as_response:
                    result["response"] = capture_result
        elif not capture_path.exists():
            session_id = extract_session_id(stdout, stderr)
            if session_id:
                result["session_id"] = session_id
    if not result.get("session_id"):
        fallback_session_id = extract_session_id(stdout, stderr)
        if not fallback_session_id and not dry_run:
            child_continue_session_after = read_child_continue_session(project_root, result.get("command"))
            if child_continue_session_after and child_continue_session_after != child_continue_session_before:
                fallback_session_id = child_continue_session_after
        if fallback_session_id:
            result["session_id"] = fallback_session_id


def resolve_workflow_vars(workflow_vars: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Resolve workflow vars against builtins and other vars before command rendering."""
    resolved = dict(workflow_vars)
    for _ in range(10):
        changed = False
        render_context = {**context, "vars": resolved}
        for key, value in list(resolved.items()):
            rendered = render(value, render_context)
            if rendered != value:
                resolved[key] = rendered
                changed = True
        if not changed:
            break
    return resolved


def maybe_run_summary_command(
    workflow: dict[str, Any],
    context: dict[str, Any],
    project_root: Path,
    out_dir: Path,
    dry_run: bool,
    live_log_path: Path | None = None,
    model_policy: dict[str, Any] | None = None,
) -> tuple[str, str, int, Any | None, dict[str, Any] | None]:
    explicit = workflow.get("summary")
    if not isinstance(explicit, dict) or "command" not in explicit:
        write_text(out_dir / "summary.stdout.txt", "")
        write_text(out_dir / "summary.stderr.txt", "")
        return "", "", 0, None, None
    command = render(explicit["command"], context)
    model_selection = validate_pi_launch_policy(
        {"command": command}, context="summary", policy=model_policy
    )
    write_text(out_dir / "summary.command.sh", command_preview(command) + "\n")
    is_juno_command = detect_juno_command(command)
    capture_enabled = bool(explicit.get("capture_session", explicit.get("capture", is_juno_command)))
    capture_path = out_dir / "summary.capture.json"
    env, child_continue_session_before = build_command_env(
        project_root, command, capture_enabled, capture_path, "workflow_summary", dry_run
    )
    if dry_run:
        stdout = ""
        stderr = ""
        exit_code = 0
        probe_satisfied = False
    else:
        append_live_log(live_log_path, "\n=== SUMMARY COMMAND ===\n")
        proc = execute_rendered_command(command, project_root, env, live_log_path)
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        exit_code = int(proc.returncode)
    write_text(out_dir / "summary.stdout.txt", stdout)
    write_text(out_dir / "summary.stderr.txt", stderr)
    result: dict[str, Any] | None = None
    if is_juno_command or capture_enabled:
        result = {
            "id": "summary",
            "kind": "summary",
            "index": "summary",
            "name": "summary",
            "command": command,
            "command_preview": command_preview(command),
            "status": "dry_run" if dry_run else ("success" if exit_code == 0 else "failed"),
            "exit_code": exit_code,
            "stdout_path": str(out_dir / "summary.stdout.txt"),
            "stderr_path": str(out_dir / "summary.stderr.txt"),
            "capture_enabled": capture_enabled,
            "capture_json": str(capture_path) if capture_enabled else "",
            "capture_json_path": str(capture_path) if capture_enabled else "",
            "capture_result": "",
            "session_id": "",
            "workflow_model_selection": model_selection,
        }
        if capture_enabled:
            apply_agent_session_capture(
                result,
                project_root,
                stdout,
                stderr,
                capture_path,
                child_continue_session_before,
                dry_run,
                use_capture_result_as_response=False,
            )
    return stdout, stderr, exit_code, command, result


def resolve_from_step(steps: list[dict[str, Any]], selector: str | None) -> int:
    """Return zero-based start index for --from-step selector."""
    if selector is None or str(selector).strip() == "":
        return 0
    raw = str(selector).strip()
    try:
        value = int(raw)
    except ValueError:
        for idx, step in enumerate(steps):
            if str(step.get("id")) == raw or str(step.get("name", "")) == raw:
                return idx
        raise WorkflowError(f"--from-step target not found: {raw}")
    if value == -1:
        return len(steps) - 1
    if value < 0 or value >= len(steps):
        raise WorkflowError(f"--from-step index out of range: {value} (steps: 0..{len(steps) - 1}, or -1)")
    return value


def workflow_model_bindings(
    workflow: dict[str, Any], context: dict[str, Any], policy: dict[str, Any]
) -> dict[str, Any]:
    steps: dict[str, Any] = {}
    for step in workflow["steps"]:
        if step.get("managed_agent") is not None:
            steps[str(step["id"])] = {"managed_agent": True, "configured_defaults": True}
        else:
            command = render(step["command"], context)
            steps[str(step["id"])] = validate_pi_launch_policy(
                {"command": command}, context=f"step {step['id']}", policy=policy
            )
    summary = workflow.get("summary")
    summary_selection = None
    if isinstance(summary, dict) and "command" in summary:
        summary_selection = validate_pi_launch_policy(
            {"command": render(summary["command"], context)}, context="summary", policy=policy
        )
    return {"steps": steps, "summary": summary_selection}


def build_run_contract(
    workflow_text: str,
    workflow: dict[str, Any],
    context: dict[str, Any],
    project_root: Path,
    model_policy: dict[str, Any],
) -> dict[str, Any]:
    frozen_inputs: list[dict[str, Any]] = []
    for item in workflow.get("frozen_inputs") or []:
        rendered_path = Path(str(render(item["path"], context))).expanduser()
        if not rendered_path.is_absolute():
            rendered_path = project_root / rendered_path
        required = bool(item.get("required", True))
        if required and not rendered_path.is_file():
            raise WorkflowError(f"frozen_input[{item['id']}]: required file not found: {rendered_path}")
        frozen_inputs.append(
            {
                "id": item["id"],
                "path": str(rendered_path.resolve()),
                "required": required,
                "sha256": file_sha256(rendered_path) if rendered_path.is_file() else None,
            }
        )
    receipts = normalize_receipt_contracts(workflow)
    return {
        "schema_version": RUN_CONTRACT_SCHEMA,
        "workflow_id": context["workflow_id"],
        "workflow_source_sha256": sha256_bytes(workflow_text.encode("utf-8")),
        "workflow_source_path": str(context.get("workflow_source_path") or ""),
        "project_root": str(project_root.resolve()),
        "workflow_model_policy": model_policy,
        "workflow_model_bindings": workflow_model_bindings(workflow, context, model_policy),
        "resolved_vars": context["vars"],
        "resolved_vars_sha256": canonical_sha256(context["vars"]),
        "step_order": [str(step["id"]) for step in workflow["steps"]],
        "steps": {
            str(step["id"]): {
                "template_sha256": canonical_sha256(step.get("managed_agent", step.get("command"))),
                "initial_rendered_sha256": canonical_sha256(render(step.get("managed_agent", step.get("command")), context)),
                "stage_boundary_template_sha256": (
                    canonical_sha256(step["stage_boundary"])
                    if step.get("stage_boundary") is not None else None
                ),
                "stage_boundary_rendered_sha256": (
                    canonical_sha256(render(step["stage_boundary"], context))
                    if step.get("stage_boundary") is not None else None
                ),
                "candidate_read_only_template_sha256": (
                    canonical_sha256(step["candidate_read_only"])
                    if step.get("candidate_read_only") is not None else None
                ),
                "candidate_read_only_rendered_sha256": (
                    canonical_sha256(render(step["candidate_read_only"], context))
                    if step.get("candidate_read_only") is not None else None
                ),
                "candidate_composition_template_sha256": (
                    canonical_sha256(step["candidate_composition"])
                    if step.get("candidate_composition") is not None else None
                ),
                "candidate_composition_rendered_sha256": (
                    canonical_sha256(render(step["candidate_composition"], context))
                    if step.get("candidate_composition") is not None else None
                ),
            }
            for step in workflow["steps"]
        },
        "frozen_inputs": frozen_inputs,
        "receipt_contracts": receipts,
        "receipt_contracts_sha256": canonical_sha256(receipts),
        "resolved_receipt_paths": {
            receipt_id: str(receipt_path(contract, context, project_root).resolve())
            for receipt_id, contract in receipts.items()
        },
        "terminal_gate": str(workflow.get("terminal_gate") or ""),
        "validation_ownership": workflow.get("validation_ownership") or {},
        "workflow_class": str(workflow.get("workflow_class") or ""),
        "managed_agent_policy": workflow.get("managed_agent_policy"),
        "managed_agent_policy_sha256": canonical_sha256(workflow.get("managed_agent_policy")),
        "integration_step": str(workflow.get("integration_step") or ""),
        "controller_disposition": str(workflow.get("controller_disposition") or ""),
        "completed_steps": {},
        "attempts": [],
    }


def verify_run_contract(frozen: dict[str, Any], current: dict[str, Any]) -> None:
    checks = {
        "schema_version": (RUN_CONTRACT_SCHEMA, frozen.get("schema_version")),
        "workflow_id": (frozen.get("workflow_id"), current.get("workflow_id")),
        "workflow_source_sha256": (frozen.get("workflow_source_sha256"), current.get("workflow_source_sha256")),
        "resolved_vars_sha256": (frozen.get("resolved_vars_sha256"), current.get("resolved_vars_sha256")),
        "steps": (frozen.get("steps"), current.get("steps")),
        "frozen_inputs": (frozen.get("frozen_inputs"), current.get("frozen_inputs")),
        "receipt_contracts_sha256": (
            frozen.get("receipt_contracts_sha256"),
            current.get("receipt_contracts_sha256"),
        ),
        "terminal_gate": (frozen.get("terminal_gate"), current.get("terminal_gate")),
        "validation_ownership": (frozen.get("validation_ownership"), current.get("validation_ownership")),
        "workflow_class": (frozen.get("workflow_class"), current.get("workflow_class")),
        "integration_step": (frozen.get("integration_step"), current.get("integration_step")),
        "controller_disposition": (frozen.get("controller_disposition"), current.get("controller_disposition")),
        "workflow_model_policy": (frozen.get("workflow_model_policy"), current.get("workflow_model_policy")),
        "workflow_model_bindings": (frozen.get("workflow_model_bindings"), current.get("workflow_model_bindings")),
    }
    for name, (expected, actual) in checks.items():
        if expected != actual:
            raise WorkflowError(f"resume_contract[{name}]: expected={expected!r} actual={actual!r}")


def reconcile_candidate_composition(
    step: dict[str, Any], context: dict[str, Any], project_root: Path
) -> dict[str, Any] | None:
    """Replace a composed receipt's changed_paths after the receipt exists.

    Git is the source of truth, so an in-worktree receipt necessarily includes
    its own path and cannot be omitted by a pre-write child-path union.
    """
    contract = normalize_candidate_composition(step)
    if contract is None:
        return None
    rendered = render(contract, context)
    manifest_path = Path(str(rendered["manifest_path"])).expanduser()
    if not manifest_path.is_absolute():
        manifest_path = project_root / manifest_path
    manifest = load_json_object(manifest_path, f"step[{step['id']}] candidate composition manifest")
    present, receipt_path_value = dotted_get(manifest, str(rendered["receipt_path_field"]))
    if not present or not isinstance(receipt_path_value, str) or not receipt_path_value.strip():
        raise WorkflowError(
            f"step[{step['id']}].candidate_composition receipt path field "
            f"{rendered['receipt_path_field']!r} is missing or invalid"
        )
    candidate_path = Path(receipt_path_value).expanduser()
    if not candidate_path.is_absolute():
        candidate_path = project_root / candidate_path
    payload = load_json_object(candidate_path, f"step[{step['id']}] candidate receipt")
    tracked = subprocess.run(
        ["git", "-C", str(project_root), "diff", "--name-only", "--relative", "HEAD", "--"],
        text=True, capture_output=True, check=False,
    )
    untracked = subprocess.run(
        ["git", "-C", str(project_root), "ls-files", "--others", "--exclude-standard"],
        text=True, capture_output=True, check=False,
    )
    if tracked.returncode != 0 or untracked.returncode != 0:
        detail = (tracked.stderr or untracked.stderr or "git changed-path discovery failed").strip()
        raise WorkflowError(f"step[{step['id']}].candidate_composition: {detail}")
    changed = sorted(set(tracked.stdout.splitlines() + untracked.stdout.splitlines()))
    previous = payload.get("changed_paths")
    payload["changed_paths"] = changed
    write_text(candidate_path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    try:
        relative_receipt = candidate_path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        relative_receipt = None
    if relative_receipt is not None and relative_receipt not in changed:
        raise WorkflowError(
            f"step[{step['id']}].candidate_composition: in-worktree receipt was absent from Git changed paths: "
            f"{relative_receipt}"
        )
    return {
        "schema_version": "juno_candidate_composition.v1",
        "manifest_path": str(manifest_path.resolve()),
        "receipt_path": str(candidate_path.resolve()),
        "receipt_worktree_path": relative_receipt,
        "receipt_included": relative_receipt is None or relative_receipt in changed,
        "changed_paths": changed,
        "replaced_changed_paths": previous,
    }


def receipt_path(contract: dict[str, Any], context: dict[str, Any], project_root: Path) -> Path:
    path = Path(str(render(contract["path"], context))).expanduser()
    return path if path.is_absolute() else project_root / path


def populate_receipt_context(
    context: dict[str, Any], workflow: dict[str, Any], project_root: Path
) -> dict[str, dict[str, Any]]:
    """Expose each declared receipt as the only canonical rendered path source."""
    resolved: dict[str, dict[str, Any]] = {}
    for receipt_id, contract in normalize_receipt_contracts(workflow).items():
        path = receipt_path(contract, context, project_root)
        resolved[receipt_id] = {**contract, "path": str(path.resolve())}
    context["receipts"] = resolved
    return resolved


def context_for_out_dir(
    context: dict[str, Any], out_dir: Path, workflow: dict[str, Any], project_root: Path
) -> dict[str, Any]:
    inherited = dict(context)
    inherited["out_dir"] = str(out_dir)
    inherited["workflow"] = {**context["workflow"], "out_dir": str(out_dir)}
    inherited["steps"] = {}
    populate_receipt_context(inherited, workflow, project_root)
    return inherited


def validate_receipt_file(
    contract: dict[str, Any],
    context: dict[str, Any],
    project_root: Path,
    producer_step_digest: str,
    expected_artifact_sha256: str | None = None,
) -> dict[str, Any]:
    path = receipt_path(contract, context, project_root)
    payload = load_json_object(path, f"receipt[{contract['id']}]")
    validate_receipt_payload(contract, payload, producer_step_digest, location=str(path))
    digest = file_sha256(path)
    if expected_artifact_sha256 is not None and digest != expected_artifact_sha256:
        raise WorkflowError(
            f"receipt[{contract['id']}].artifact_sha256: expected={expected_artifact_sha256!r} actual={digest!r}"
        )
    return {"path": str(path.resolve()), "sha256": digest, "schema_version": payload["schema_version"]}


def latest_contract_manifest(
    parent_contract: dict[str, Any], parent_dir: Path, require_hash: bool = False
) -> Path:
    del parent_contract  # The shared resolver reloads the on-disk contract as the evidence source of truth.
    try:
        resolved = resolve_workflow_manifest(parent_dir)
    except WorkflowRunEvidenceError as exc:
        raise WorkflowError(str(exc)) from exc
    if require_hash and resolved.source != "run_contract_latest_attempt":
        raise WorkflowError(f"amendment parent has no hash-bound readable manifest: {parent_dir}")
    return resolved.path


def materialize_inherited_receipt(
    contract: dict[str, Any],
    source_context: dict[str, Any],
    destination_context: dict[str, Any],
    project_root: Path,
    producer_step_digest: str,
    expected_artifact_sha256: str | None = None,
    inherited_source_path: str | None = None,
) -> dict[str, Any]:
    source = (
        Path(inherited_source_path).expanduser()
        if inherited_source_path
        else receipt_path(contract, source_context, project_root)
    )
    payload = load_json_object(source, f"amendment receipt[{contract['id']}]")
    validate_receipt_payload(contract, payload, producer_step_digest, location=str(source))
    digest = file_sha256(source)
    if expected_artifact_sha256 is not None and digest != expected_artifact_sha256:
        raise WorkflowError(
            f"amendment receipt[{contract['id']}].artifact_sha256: "
            f"expected={expected_artifact_sha256!r} actual={digest!r}"
        )
    destination = receipt_path(contract, destination_context, project_root)
    if source.resolve() != destination.resolve():
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and file_sha256(destination) != digest:
            raise WorkflowError(f"amendment receipt[{contract['id']}]: destination already differs: {destination}")
        if not destination.exists():
            shutil.copy2(source, destination)
    inherited = validate_receipt_file(
        contract, destination_context, project_root, producer_step_digest, digest
    )
    inherited["inherited_from"] = str(source.resolve())
    inherited["lineage"] = "amendment_revalidated"
    return inherited


def prepare_selective_amendment(
    workflow: dict[str, Any],
    current_contract: dict[str, Any],
    parent_contract: dict[str, Any],
    parent_manifest: dict[str, Any],
    context: dict[str, Any],
    project_root: Path,
    parent_dir: Path,
    parent_manifest_path: Path,
    start_index: int,
) -> dict[str, dict[str, Any]]:
    """Revalidate a parent prefix before any amendment suffix command is dispatched."""
    for field in ("schema_version", "workflow_id", "resolved_vars_sha256", "frozen_inputs", "workflow_class"):
        if parent_contract.get(field) != current_contract.get(field):
            raise WorkflowError(
                f"amendment_contract[{field}]: expected={parent_contract.get(field)!r} "
                f"actual={current_contract.get(field)!r}"
            )
    parent_steps = parent_contract.get("steps") or {}
    current_steps = current_contract.get("steps") or {}
    manifest_steps = {
        str(item.get("id")): item
        for item in parent_manifest.get("steps") or []
        if isinstance(item, dict)
    }
    parent_completed = parent_contract.get("completed_steps") or {}
    receipts_by_producer: dict[str, list[dict[str, Any]]] = {}
    for contract in normalize_receipt_contracts(workflow).values():
        receipts_by_producer.setdefault(contract["producer"], []).append(contract)
    parent_receipts_by_producer: dict[str, list[dict[str, Any]]] = {}
    for contract in (parent_contract.get("receipt_contracts") or {}).values():
        parent_receipts_by_producer.setdefault(str(contract.get("producer") or ""), []).append(contract)
    parent_context = context_for_out_dir(context, parent_dir, workflow, project_root)
    reused: dict[str, dict[str, Any]] = {}

    for step in workflow["steps"][:start_index]:
        step_id = str(step["id"])
        if (parent_steps.get(step_id) or {}).get("template_sha256") != (current_steps.get(step_id) or {}).get("template_sha256"):
            raise WorkflowError(f"amendment_prerequisite[{step_id}]: command template changed")
        previous = manifest_steps.get(step_id)
        completed = parent_completed.get(step_id) or {}
        if not previous:
            raise WorkflowError(f"amendment_prerequisite[{step_id}]: parent manifest evidence missing")
        command_digest = str(completed.get("command_sha256") or "")
        previous_digest = str(previous.get("command_sha256") or "")
        if not command_digest or previous_digest != command_digest:
            raise WorkflowError(f"amendment_prerequisite[{step_id}]: producer command digest mismatch")
        if "command" not in previous or canonical_sha256(previous["command"]) != command_digest:
            raise WorkflowError(f"amendment_prerequisite[{step_id}]: manifest command does not match producer digest")

        produced_contracts = receipts_by_producer.get(step_id, [])
        previous_contracts = parent_receipts_by_producer.get(step_id, [])
        produced_by_id = {str(contract["id"]): contract for contract in produced_contracts}
        previous_by_id = {str(contract["id"]): contract for contract in previous_contracts}
        if set(produced_by_id) != set(previous_by_id):
            raise WorkflowError(f"amendment_prerequisite[{step_id}]: producer receipt set changed")
        for receipt_id, contract in produced_by_id.items():
            previous_contract = previous_by_id[receipt_id]
            for field in ("producer", "schema_version", "required_fields", "expected_fields"):
                if contract.get(field) != previous_contract.get(field):
                    raise WorkflowError(
                        f"amendment_prerequisite[{step_id}].receipt[{receipt_id}]: contract {field} changed"
                    )
        status = str(previous.get("status") or "")
        normally_reusable = status in {"success", "reused_verified", "amendment_revalidated"} and bool(completed)
        if not normally_reusable:
            raise WorkflowError(f"amendment_prerequisite[{step_id}]: no reusable successful predecessor evidence")
        if step.get("candidate_read_only") is not None:
            verify_candidate_guard_artifact(completed, step_id, project_root)
        manifest_attempt = (
            previous.get("reused_from_attempt")
            if status in {"reused_verified", "amendment_revalidated"}
            else parent_manifest.get("run_id")
        )
        if str(manifest_attempt or "") != str(completed.get("attempt_id") or ""):
            raise WorkflowError(f"amendment_prerequisite[{step_id}]: completed evidence attempt mismatch")

        inherited_receipts: dict[str, Any] = {}
        for contract in produced_contracts:
            old_evidence = (completed.get("receipts") or {}).get(contract["id"]) or {}
            if not old_evidence.get("path") or not old_evidence.get("sha256"):
                raise WorkflowError(
                    f"amendment_prerequisite[{step_id}].receipt[{contract['id']}]: "
                    "parent hash anchor missing"
                )
            inherited_receipts[contract["id"]] = materialize_inherited_receipt(
                contract,
                parent_context,
                context,
                project_root,
                command_digest,
                str(old_evidence.get("sha256")) if old_evidence.get("sha256") else None,
                str(old_evidence.get("path")) if old_evidence.get("path") else None,
            )
        reused[step_id] = {
            "previous": previous,
            "command_sha256": command_digest,
            "receipts": inherited_receipts,
            "parent_attempt": completed.get("attempt_id"),
            "parent_manifest": str(parent_manifest_path.resolve()),
        }
    return reused


def archive_attempt(out_dir: Path, manifest: dict[str, Any]) -> None:
    attempt_id = str(manifest["run_id"])
    attempt_dir = out_dir / "attempts" / attempt_id
    if attempt_dir.exists():
        raise WorkflowError(f"attempt archive already exists: {attempt_dir}")
    attempt_dir.mkdir(parents=True)
    archived_manifest = json.loads(json.dumps(manifest))
    for step in archived_manifest.get("steps", []):
        for key in ("stdout_path", "stderr_path", "response_path"):
            raw_path = str(step.get(key) or "")
            if not raw_path:
                continue
            source = Path(raw_path)
            if source.is_file():
                destination = attempt_dir / source.name
                shutil.copy2(source, destination)
                step[key] = str(destination.resolve())
        guard = step.get("candidate_read_only_evidence")
        if isinstance(guard, dict) and guard.get("path"):
            source = Path(str(guard["path"]))
            destination = attempt_dir / "steps" / safe_id(str(step.get("id") or "step"), "step") / "candidate_read_only.json"
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not source.is_file() or file_sha256(source) != guard.get("sha256"):
                raise WorkflowError(f"attempt candidate_read_only[{step.get('id')}]: artifact hash mismatch")
            shutil.copy2(source, destination)
            guard["path"] = str(destination.resolve())
    write_text(attempt_dir / "manifest.json", json.dumps(archived_manifest, indent=2, ensure_ascii=False) + "\n")
    for name in ("summary.md", "summary.stdout.txt", "summary.stderr.txt", "summary.command.sh"):
        source = out_dir / name
        if source.is_file():
            shutil.copy2(source, attempt_dir / name)


def validate_print_output(print_output: str, workflow: dict[str, Any]) -> None:
    if print_output in {"summary", "none", "all"}:
        return
    selected = print_output.split(":", 1)[1] if print_output.startswith("step:") else print_output
    known = {str(step["id"]) for step in workflow["steps"]}
    if not selected or selected not in known:
        raise WorkflowError(f"unknown --print-output step: {selected}")


def selected_final_output(print_output: str, context: dict[str, Any], summary: str) -> str:
    if print_output == "summary":
        return summary
    if print_output == "none":
        return ""
    if print_output == "all":
        outputs = [str(result.get("response", result.get("stdout", ""))) for result in context["steps"].values()]
        return "\n".join(output.rstrip("\n") for output in outputs if output)
    selected = print_output.split(":", 1)[1] if print_output.startswith("step:") else print_output
    result = context["steps"].get(selected)
    return str(result.get("response", result.get("stdout", ""))) if result is not None else ""


def run_workflow(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root or os.getcwd()).resolve()
    if args.init_example:
        target = init_example(args.init_example, bool(args.force))
        print(f"Wrote example workflow to {target}")
        return 0

    if not args.workflow:
        raise WorkflowError("--workflow is required unless --init-example is used")
    if args.workflow == "-":
        workflow_text = sys.stdin.read()
        workflow_dir = project_root
        workflow_source_path = ""
    else:
        workflow_path = Path(args.workflow).resolve()
        workflow_text = workflow_path.read_text(encoding="utf-8")
        workflow_dir = workflow_path.parent
        workflow_source_path = str(workflow_path)
    workflow = parse_yaml_like(workflow_text)
    model_policy = load_workflow_model_policy(project_root)
    validate_workflow(workflow, model_policy)
    validate_print_output(args.print_output, workflow)

    now = _dt.datetime.now(_dt.timezone.utc)
    run_id = now.strftime("%Y%m%d_%H%M%S_%fZ")
    workflow_id = safe_id(workflow.get("workflow_id") or workflow.get("id") or workflow.get("name"), "workflow")
    out_dir = (
        Path(args.out_dir).resolve()
        if args.out_dir
        else project_root / ".juno_task" / "specs" / "workflows" / workflow_id / run_id
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    workflow_vars = workflow.get("vars") if isinstance(workflow.get("vars"), dict) else {}
    workflow_vars = {**workflow_vars, **parse_vars(args.vars or [])}
    context: dict[str, Any] = {
        "now_utc": now.isoformat().replace("+00:00", "Z"),
        "today_utc": now.date().isoformat(),
        "yesterday_utc": (now.date() - _dt.timedelta(days=1)).isoformat(),
        "run_id": run_id,
        "workflow_id": workflow_id,
        "workflow_dir": str(workflow_dir),
        "workflow_source_path": workflow_source_path,
        "repo_root": str(project_root),
        "project_root": str(project_root),
        "out_dir": str(out_dir),
        "workflow": {
            "id": workflow_id,
            "name": workflow.get("name", workflow_id),
            "out_dir": str(out_dir),
            "project_root": str(project_root),
            "dir": str(workflow_dir),
        },
        "vars": workflow_vars,
        "steps": {},
    }
    workflow_vars = resolve_workflow_vars(workflow_vars, context)
    context["vars"] = workflow_vars
    for key, value in workflow_vars.items():
        if isinstance(key, str) and key not in context:
            context[key] = value
    populate_receipt_context(context, workflow, project_root)

    start_index = resolve_from_step(workflow["steps"], args.from_step)
    requested_from_step = args.from_step is not None and str(args.from_step).strip() != ""
    run_contract_path = out_dir / "run_contract.json"
    is_resume = requested_from_step and run_contract_path.exists() and not args.amends_run
    is_selective_amendment = bool(args.amends_run and requested_from_step)
    current_contract = build_run_contract(workflow_text, workflow, context, project_root, model_policy)
    parent_contract: dict[str, Any] | None = None
    parent_manifest: dict[str, Any] | None = None
    parent_manifest_path: Path | None = None
    parent_dir: Path | None = None
    if args.amends_run:
        if str(workflow.get("amendment_mode") or "") != "harness_only_validation":
            raise WorkflowError("--amends-run requires amendment_mode: harness_only_validation")
        parent = Path(args.amends_run).expanduser().resolve()
        parent_contract_path = parent / "run_contract.json" if parent.is_dir() else parent
        parent_dir = parent_contract_path.parent
        parent_contract = load_json_object(parent_contract_path, "amended run contract")
        if parent_contract.get("schema_version") != RUN_CONTRACT_SCHEMA:
            raise WorkflowError(f"amended run contract has unsupported schema: {parent_contract.get('schema_version')!r}")
        if parent_contract.get("workflow_id") != workflow_id:
            raise WorkflowError(
                f"amended run contract workflow mismatch: expected={workflow_id!r} actual={parent_contract.get('workflow_id')!r}"
            )
        current_contract["amendment_of"] = {
            "run_contract": str(parent_contract_path),
            "sha256": file_sha256(parent_contract_path),
            "workflow_id": parent_contract.get("workflow_id"),
            "mode": "harness_only_validation",
        }
        if is_selective_amendment:
            parent_manifest_path = latest_contract_manifest(parent_contract, parent_dir, require_hash=True)
            parent_manifest = load_json_object(parent_manifest_path, "amended run manifest")
            newest_attempt_id = str((parent_contract.get("attempts") or [])[-1].get("attempt_id") or "")
            if str(parent_manifest.get("run_id") or "") != newest_attempt_id:
                raise WorkflowError(
                    "amendment newest attempt identity does not match its hash-bound manifest: "
                    f"attempt={newest_attempt_id!r} manifest={parent_manifest.get('run_id')!r}"
                )
            current_contract["amendment_of"]["manifest"] = str(parent_manifest_path.resolve())
            current_contract["amendment_of"]["manifest_sha256"] = file_sha256(parent_manifest_path)
    previous_manifest: dict[str, Any] | None = None
    amendment_reuse: dict[str, dict[str, Any]] = {}
    if is_resume:
        frozen_contract = load_json_object(run_contract_path, "run contract")
        verify_run_contract(frozen_contract, current_contract)
        previous_manifest_path = latest_contract_manifest(frozen_contract, out_dir)
        previous_manifest = load_json_object(previous_manifest_path, "previous workflow manifest")
        run_contract = frozen_contract
        recovery = previous_manifest.get("recovery") or {}
        if recovery:
            required_index = int(recovery.get("first_invalid_step_index", -1))
            if start_index != required_index:
                raise WorkflowError(
                    f"recovered_attempt requires --from-step index {required_index}; requested {start_index}"
                )
    else:
        if run_contract_path.exists():
            raise WorkflowError(
                f"run directory already has an immutable contract: {run_contract_path}; use a fresh --out-dir or --from-step"
            )
        run_contract = current_contract
        if is_selective_amendment:
            assert (
                parent_contract is not None
                and parent_manifest is not None
                and parent_manifest_path is not None
                and parent_dir is not None
            )
            amendment_reuse = prepare_selective_amendment(
                workflow,
                current_contract,
                parent_contract,
                parent_manifest,
                context,
                project_root,
                parent_dir,
                parent_manifest_path,
                start_index,
            )
            previous_manifest = parent_manifest
            for step_id, evidence in amendment_reuse.items():
                run_contract.setdefault("completed_steps", {})[step_id] = {
                    "attempt_id": evidence["parent_attempt"],
                    "command_sha256": evidence["command_sha256"],
                    "status": "amendment_revalidated",
                    "receipts": evidence["receipts"],
                    "inherited_from_manifest": evidence["parent_manifest"],
                }
            run_contract["amendment_reused_steps"] = list(amendment_reuse)
        write_text(run_contract_path, json.dumps(run_contract, indent=2, ensure_ascii=False) + "\n")

    if args.tmux_session and not args.tmux:
        raise WorkflowError("--tmux-session requires --tmux")
    tmux_observer = (
        start_tmux_observer(out_dir, workflow_id, run_id, args.tmux_session)
        if args.tmux and not args.dry_run
        else {"enabled": False}
    )
    live_log_path = Path(tmux_observer["live_log"]) if tmux_observer.get("enabled") else None

    manifest: dict[str, Any] = {
        "schema_version": "1.0",
        "workflow_id": workflow_id,
        "workflow": context["workflow"],
        "run_id": run_id,
        "dry_run": bool(args.dry_run),
        "repo_root": str(project_root),
        "workflow_dir": str(workflow_dir),
        "out_dir": str(out_dir),
        "steps": [],
        "failed_steps": [],
        "status": "success",
        "tmux_observer": tmux_observer,
        "workflow_model_policy": model_policy,
        "workflow_model_bindings": current_contract["workflow_model_bindings"],
    }

    final_exit = 0
    session_candidates: list[dict[str, Any]] = []
    explicit_continue_from_step = str(workflow.get("continue_from_step") or "").strip()
    persisted_continue: dict[str, str] | None = None
    manifest["from_step"] = args.from_step
    manifest["from_step_index"] = start_index
    manifest["attempt_number"] = len(run_contract.get("attempts") or []) + 1
    manifest["run_contract_path"] = str(run_contract_path)
    if is_selective_amendment:
        reused_ids = [str(step["id"]) for step in workflow["steps"][:start_index]]
        executed_ids = [str(step["id"]) for step in workflow["steps"][start_index:]]
        manifest["amendment_plan"] = {
            "parent_manifest": current_contract["amendment_of"]["manifest"],
            "reused_steps": reused_ids,
            "executed_steps": executed_ids,
        }
        print("Amendment execution plan")
        print("  Revalidate/reuse: " + (", ".join(reused_ids) if reused_ids else "none"))
        print("  Execute: " + (", ".join(executed_ids) if executed_ids else "none"))
    previous_steps = {
        str(item.get("id")): item for item in (previous_manifest or {}).get("steps", []) if isinstance(item, dict)
    }
    receipts = normalize_receipt_contracts(workflow)
    receipts_by_producer: dict[str, list[dict[str, Any]]] = {}
    for contract in receipts.values():
        receipts_by_producer.setdefault(contract["producer"], []).append(contract)
    for index, step in enumerate(workflow["steps"], start=1):
        step_id = str(step["id"])
        if index - 1 < start_index:
            if not is_resume and not is_selective_amendment:
                command = render(step.get("command", ["managed_agent"]), context)
                skipped_result: dict[str, Any] = {
                    "id": step_id,
                    "command": command,
                    "command_preview": command_preview(command),
                    "status": "skipped",
                    "exit_code": 0,
                    "transport_status": "skipped",
                    "transport_exit_code": 0,
                    "duration_seconds": 0,
                    "stdout": "",
                    "stderr": "",
                    "stdout_path": "",
                    "stderr_path": "",
                    "response": "",
                    "response_path": "",
                    "capture_enabled": False,
                    "capture_json": "",
                    "capture_json_path": "",
                    "capture_result": "",
                    "session_id": "",
                }
                context["steps"][step_id] = skipped_result
                manifest["steps"].append(
                    {k: v for k, v in skipped_result.items() if k not in {"stdout", "stderr"}}
                )
                continue
            previous = previous_steps.get(step_id)
            completed = (run_contract.get("completed_steps") or {}).get(step_id)
            reusable_statuses = {"success", "reused_verified", "amendment_revalidated"}
            if is_selective_amendment:
                evidence = amendment_reuse.get(step_id)
                if not evidence:
                    raise WorkflowError(f"amendment_prerequisite[{step_id}]: prepared evidence missing")
                previous = evidence["previous"]
            if not previous or not completed or (
                not is_selective_amendment and previous.get("status") not in reusable_statuses
            ):
                raise WorkflowError(f"resume_prerequisite[{step_id}]: no reusable successful predecessor evidence")
            if step.get("candidate_read_only") is not None:
                verify_candidate_guard_artifact(completed, step_id, project_root)
            for contract in receipts_by_producer.get(step_id, []):
                receipt_evidence = (completed.get("receipts") or {}).get(contract["id"])
                if not receipt_evidence:
                    raise WorkflowError(f"resume_prerequisite[{step_id}].receipt[{contract['id']}]: evidence missing")
                validate_receipt_file(
                    contract,
                    context,
                    project_root,
                    str(completed["command_sha256"]),
                    str(receipt_evidence["sha256"]),
                )
            reused_status = "amendment_revalidated" if is_selective_amendment else "reused_verified"
            skipped_result: dict[str, Any] = {
                "id": step_id,
                "command": previous.get("command", render(step.get("command", ["managed_agent"]), context)),
                "command_preview": previous.get("command_preview", command_preview(render(step.get("command", ["managed_agent"]), context))),
                "status": reused_status,
                "exit_code": previous.get("exit_code", 0),
                "transport_status": previous.get("transport_status", previous.get("status")),
                "transport_exit_code": previous.get("transport_exit_code", previous.get("exit_code", 0)),
                "duration_seconds": 0,
                "stdout": "",
                "stderr": "",
                "stdout_path": "",
                "stderr_path": "",
                "response": previous.get("response", ""),
                "response_path": previous.get("response_path", ""),
                "capture_enabled": False,
                "capture_json": "",
                "capture_json_path": "",
                "capture_result": "",
                "session_id": "",
                "reused_from_attempt": completed.get("attempt_id") or previous_manifest.get("run_id"),
                "reused_from_manifest": completed.get("inherited_from_manifest", ""),
                "command_sha256": completed.get("command_sha256"),
                "producer_command_sha256": completed.get("command_sha256"),
                "semantic_outcome": previous.get("semantic_outcome", ""),
            }
            context["steps"][step_id] = skipped_result
            manifest["steps"].append({k: v for k, v in skipped_result.items() if k not in {"stdout", "stderr"}})
            continue
        managed_spec = render(step.get("managed_agent"), context) if step.get("managed_agent") is not None else None
        if managed_spec is not None:
            runner = project_root / ".juno_task/scripts/managed_agent_runner.py"
            if not runner.is_file():
                raise WorkflowError(f"step[{step_id}]: canonical managed-agent runner is missing")
            allowed = {"mode", "controller_root", "controller_branch", "agent_root", "prompt_file", "out_dir", "tool_id",
                       "task_id", "create_receipt", "task_root_receipt", "verify_receipt", "edit_preflight_receipt",
                       "authority_map", "candidate_sha", "candidate_root", "require_terminal_result"}
            unknown = sorted(set(managed_spec) - allowed)
            if unknown:
                raise WorkflowError(f"step[{step_id}]: unsupported managed_agent fields: {unknown}")
            command = [sys.executable, str(runner), "run"]
            for key, value in managed_spec.items():
                if key == "require_terminal_result":
                    if value is not True:
                        raise WorkflowError(f"step[{step_id}]: require_terminal_result must be true when declared")
                    command.append("--require-terminal-result")
                elif value is not None and str(value) != "":
                    command.extend(["--" + key.replace("_", "-"), str(value)])
            command.extend(["--external-side-effects", "forbidden", "--lifecycle-hooks", "disabled"])
            model_selection = {"managed_agent": True, "configured_defaults": True}
        else:
            command = render(step["command"], context)
            model_selection = validate_pi_launch_policy(
                {"command": command}, context=f"step {step_id}", policy=model_policy
            )
        preview = command_preview(command)
        command_digest = canonical_sha256(command)
        is_juno_command = detect_juno_command(command)
        capture_enabled = step_capture_enabled(step, command)
        is_managed_agent = managed_spec is not None
        rendered_stage_boundary = render(step["stage_boundary"], context) if is_managed_agent else None
        stage_root = canonical_stage_root(str(rendered_stage_boundary["root"])) if rendered_stage_boundary else None
        stage_admitted_paths = (
            normalize_stage_admitted_paths(step_id, rendered_stage_boundary["admitted_paths"])
            if rendered_stage_boundary else []
        )
        step_slug = safe_id(step_id, f"step-{index}")
        stdout_path = out_dir / f"{index:03d}_{step_slug}.stdout.txt"
        stderr_path = out_dir / f"{index:03d}_{step_slug}.stderr.txt"
        capture_path = out_dir / f"{index:03d}_{step_slug}.capture.json"
        response_path = out_dir / f"{index:03d}_{step_slug}.response.txt"
        legacy_step_dir = out_dir / "steps" / step_id
        write_text(legacy_step_dir / "command.sh", preview + "\n")
        print("\n" + step_separator("START", index, step_id))
        print(preview)
        append_live_log(live_log_path, f"\n=== START step {index} [{step_id}] ===\n{preview}\n")
        started = time.monotonic()
        stdout = ""
        stderr = ""
        exit_code = 0
        probe_satisfied = False
        launch_ownership = StepLaunchOwnership()
        dispatch_root = project_root
        dispatch_root_error = ""
        if not args.dry_run:
            try:
                dispatch_root = generated_dispatch_root(
                    step, receipts, context, project_root, run_contract.get("completed_steps") or {}
                )
            except (WorkflowError, KeyError, TypeError, OSError) as exc:
                dispatch_root_error = str(exc)
        if dispatch_root_error:
            env = child_process_environment(dict(os.environ))
            child_continue_session_before = None
        else:
            env, child_continue_session_before = build_command_env(
                dispatch_root, command, capture_enabled, capture_path, f"workflow_{step_slug}", bool(args.dry_run)
            )
        if dispatch_root != project_root:
            env["TASK_ROOT"] = str(dispatch_root)
        env = child_invocation_environment(
            env, launch_surface="workflow_runner", workflow_run_id=run_id,
            workflow_step_id=step_id, source=os.environ,
        )
        env["JUNO_WORKFLOW_ID"] = workflow_id
        env["JUNO_WORKFLOW_RUN_ID"] = run_id
        env["JUNO_WORKFLOW_STEP_ID"] = step_id
        env["JUNO_WORKFLOW_STEP_DIGEST"] = command_digest
        child_evidence_dir = out_dir / "child_steps" / step_slug
        env.pop("JUNO_WORKFLOW_CHILD_EVIDENCE_DIR", None)
        env.pop("JUNO_WORKFLOW_DIRECT_OWNER", None)
        if live_log_path is not None:
            env["JUNO_WORKFLOW_LIVE_LOG_PATH"] = str(live_log_path)
        for receipt_id, receipt in context.get("receipts", {}).items():
            env_key = "JUNO_WORKFLOW_RECEIPT_" + re.sub(r"[^A-Za-z0-9]", "_", receipt_id).upper()
            env[env_key] = str(receipt["path"])
        precondition_error = dispatch_root_error
        candidate_guard: dict[str, Any] | None = None
        candidate_guard_path = legacy_step_dir / "candidate_read_only.json"
        stage_before: tuple[dict[str, Any], dict[str, str]] | None = None
        stage_evidence: dict[str, Any] | None = None
        stage_evidence_path = legacy_step_dir / "stage_boundary.json"
        if not args.dry_run:
            try:
                for receipt_id in step.get("requires_receipts") or []:
                    producer = receipts[receipt_id]["producer"]
                    evidence = (run_contract.get("completed_steps") or {}).get(producer, {})
                    receipt_evidence = (evidence.get("receipts") or {}).get(receipt_id)
                    if not receipt_evidence:
                        raise WorkflowError(f"step[{step_id}].requires_receipt[{receipt_id}]: producer evidence missing")
                    validate_receipt_file(
                        receipts[receipt_id],
                        context,
                        project_root,
                        str(evidence["command_sha256"]),
                        str(receipt_evidence["sha256"]),
                    )
                if stage_root is not None:
                    stage_before = stage_content_snapshot(stage_root)
                if step.get("candidate_read_only") is not None:
                    rendered_identity = render(step["candidate_read_only"], context)
                    candidate_path = canonical_candidate_root(str(rendered_identity["path"]))
                    expected_candidate_sha = str(rendered_identity["sha"])
                    ensure_external_orchestration(candidate_path, project_root)
                    before_identity = snapshot_candidate_identity(candidate_path, expected_candidate_sha)
                    candidate_guard = {
                        "schema_version": "juno_candidate_read_only.v2",
                        "candidate_path": str(candidate_path),
                        "expected_sha": expected_candidate_sha,
                        "orchestration_cwd": str(project_root),
                        "before": before_identity,
                        "after": None,
                        "changed_fields": [],
                        "passed": None,
                    }
                    write_text(candidate_guard_path, json.dumps(candidate_guard, indent=2, sort_keys=True) + "\n")
            except (WorkflowError, KeyError, TypeError, OSError) as exc:
                precondition_error = str(exc)
                if step.get("candidate_read_only") is not None:
                    failed_guard = candidate_guard or {
                        "schema_version": "juno_candidate_read_only.v2",
                        "orchestration_cwd": str(project_root),
                        "passed": False,
                    }
                    failed_guard["preflight_error"] = precondition_error[:512]
                    write_text(candidate_guard_path, json.dumps(failed_guard, indent=2, sort_keys=True) + "\n")
        if args.dry_run:
            status = "dry_run"
        elif precondition_error:
            stderr = precondition_error + "\n"
            exit_code = 1
            status = "failed"
            if step.get("candidate_read_only") is not None:
                final_exit = 1
        else:
            active_marker = out_dir / "active_step.json"
            activity = {
                "schema_version": "juno_workflow_active_step.v1",
                "workflow_id": workflow_id,
                "attempt_id": run_id,
                "step_id": step_id,
                "step_index": index - 1,
                "command_sha256": command_digest,
                "runner_pid": os.getpid(),
                "started_at": _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z"),
            }
            write_text(active_marker, json.dumps(activity, indent=2, sort_keys=True) + "\n")
            inject_interruption("command_started")
            try:
                timeout_seconds = step.get("timeout_seconds")
                timeout_seconds = int(timeout_seconds) if timeout_seconds is not None else None
                probe_command = render(step.get("probe"), context) if step.get("probe") is not None else None
                if probe_command is not None:
                    probe_result = execute_rendered_command(
                        probe_command, dispatch_root, env, live_log_path,
                        timeout_seconds=min(timeout_seconds or 30, 30))
                    probe_satisfied = probe_result.returncode == 0
                if probe_satisfied:
                    stdout = "Hydration/idempotency probe already satisfied; command skipped.\n"
                    status = "success"
                else:
                    proc = execute_rendered_command(
                        command, dispatch_root, env, live_log_path, activity, active_marker,
                        timeout_seconds=timeout_seconds, launch_ownership=launch_ownership)
                    stdout = proc.stdout or ""
                    stderr = proc.stderr or ""
                    exit_code = int(proc.returncode)
                    status = "success" if exit_code == 0 else "failed"
            except subprocess.TimeoutExpired as exc:
                stdout = str(exc.output or "")
                stderr = str(exc.stderr or "") + f"\ncommand timed out after {exc.timeout}s\n"
                exit_code = 124
                status = "failed"
            except OSError as exc:
                stderr = f"command dispatch failed: {exc}\n"
                exit_code = 1
                status = "failed"
            if stage_root is not None and stage_before is not None:
                try:
                    stage_evidence = stage_boundary_evidence(stage_root, stage_admitted_paths, stage_before)
                except (WorkflowError, OSError, UnicodeError) as exc:
                    stage_evidence = {
                        "schema_version": "juno_workflow_stage_boundary.v1",
                        "root": str(stage_root), "admitted_paths": stage_admitted_paths,
                        "passed": False, "snapshot_error": str(exc)[:512],
                    }
                write_text(stage_evidence_path, json.dumps(stage_evidence, indent=2, sort_keys=True) + "\n")
                if stage_evidence.get("passed") is not True:
                    mutation_error = (
                        "managed-agent stage boundary violation; no cleanup performed; "
                        f"unexpected={','.join(stage_evidence.get('unexpected_paths') or ['snapshot_unavailable'])}; "
                        f"evidence={stage_evidence_path}"
                    )
                    stderr = stderr + ("\n" if stderr and not stderr.endswith("\n") else "") + mutation_error + "\n"
                    exit_code = 1
                    status = "failed"
                    final_exit = 1
            if candidate_guard is not None:
                try:
                    after_identity = snapshot_candidate_identity(
                        Path(candidate_guard["candidate_path"]),
                        str(candidate_guard["expected_sha"]),
                        require_expected=False,
                    )
                    changed_fields = candidate_identity_changes(candidate_guard["before"], after_identity)
                    candidate_guard["after"] = after_identity
                    candidate_guard["changed_fields"] = changed_fields
                    candidate_guard["passed"] = not changed_fields
                except (WorkflowError, OSError) as exc:
                    changed_fields = ["snapshot_unavailable"]
                    candidate_guard["after_error"] = str(exc)[:512]
                    candidate_guard["changed_fields"] = changed_fields
                    candidate_guard["passed"] = False
                write_text(candidate_guard_path, json.dumps(candidate_guard, indent=2, sort_keys=True) + "\n")
                if changed_fields:
                    mutation_error = (
                        "candidate_read_only mutation detected; no cleanup performed; "
                        f"changed={','.join(changed_fields)}; evidence={candidate_guard_path}"
                    )
                    stderr = stderr + ("\n" if stderr and not stderr.endswith("\n") else "") + mutation_error + "\n"
                    exit_code = 1
                    status = "failed"
                    final_exit = 1
            inject_interruption("command_success_before_artifacts")
        duration = round(time.monotonic() - started, 3)
        write_text(stdout_path, stdout)
        write_text(stderr_path, stderr)
        write_text(legacy_step_dir / "stdout.txt", stdout)
        write_text(legacy_step_dir / "stderr.txt", stderr)
        inject_interruption("artifacts_before_checkpoint")
        response = stdout
        result: dict[str, Any] = {
            "id": step_id,
            "command": command,
            "command_preview": preview,
            "status": status,
            "exit_code": exit_code,
            "transport_status": status,
            "transport_exit_code": exit_code,
            "duration_seconds": duration,
            "stdout": stdout,
            "stderr": stderr,
            "response": response,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "response_path": str(response_path),
            "capture_enabled": capture_enabled,
            "capture_json": str(capture_path) if capture_enabled else "",
            "capture_json_path": str(capture_path) if capture_enabled else "",
            "capture_result": "",
            "session_id": "",
            "command_sha256": command_digest,
            "workflow_model_selection": model_selection,
            "probe_satisfied": probe_satisfied,
        }
        if stage_evidence_path.is_file():
            result["stage_boundary_evidence"] = {
                "path": str(stage_evidence_path.resolve()),
                "sha256": file_sha256(stage_evidence_path),
                "passed": bool(stage_evidence and stage_evidence.get("passed") is True),
            }
        if step.get("candidate_read_only") is not None and candidate_guard_path.is_file():
            result["candidate_read_only_evidence"] = {
                "path": str(candidate_guard_path.resolve()),
                "sha256": file_sha256(candidate_guard_path),
                "evidence_sha256": canonical_sha256(load_json_object(candidate_guard_path, "candidate_read_only evidence")),
            }
        child_steps: list[dict[str, Any]] = []
        if not args.dry_run:
            try:
                child_steps = load_child_step_evidence(
                    child_evidence_dir, workflow_id, run_id, step_id, command_digest
                )
            except WorkflowError as exc:
                result["status"] = "failed"
                result["failure_reason"] = str(exc)
                result["child_evidence_error"] = str(exc)
                stderr = stderr + ("\n" if stderr else "") + str(exc) + "\n"
                result["stderr"] = stderr
        if child_steps:
            result["child_steps"] = child_steps
        if capture_enabled:
            apply_agent_session_capture(
                result,
                project_root,
                stdout,
                stderr,
                capture_path,
                child_continue_session_before,
                bool(args.dry_run),
                use_capture_result_as_response=True,
            )
            inject_interruption("capture_before_checkpoint")
        managed_receipt_path: Path | None = None
        managed_receipt: dict[str, Any] | None = None
        if is_managed_agent and not args.dry_run:
            try:
                managed_root = Path(str(managed_spec["out_dir"])).resolve()
                managed_root.relative_to(out_dir.resolve())
                managed_receipt_path = managed_root / "receipt.json"
                managed_receipt = load_json_object(managed_receipt_path, f"step[{step_id}] managed-agent receipt")
                if managed_receipt.get("state") != "succeeded" or managed_receipt.get("exit_code") != 0:
                    raise WorkflowError(f"step[{step_id}]: managed-agent receipt is not successful")
                expected_hook_policy = {
                    "schema_version": "juno_managed_hook_policy.v1",
                    "external_side_effects": "forbidden",
                    "lifecycle_hooks": "disabled",
                    "enforcement": "yy_pi_no_hooks",
                }
                if managed_receipt.get("effective_hook_policy") != expected_hook_policy:
                    raise WorkflowError(f"step[{step_id}]: managed-agent effective hook policy is missing or incompatible")
                artifact_map = managed_receipt.get("artifacts")
                if not isinstance(artifact_map, dict):
                    raise WorkflowError(f"step[{step_id}]: managed-agent artifacts are missing")
                for artifact_id in ("prompt", "launch", "stdout", "stderr", "combined", "capture", "response"):
                    item = artifact_map.get(artifact_id)
                    path = Path(str(item.get("path") if isinstance(item, dict) else ""))
                    path.resolve().relative_to(managed_root)
                    if not path.is_file() or file_sha256(path) != item.get("sha256"):
                        raise WorkflowError(f"step[{step_id}]: managed-agent artifact {artifact_id} drifted")
                result["response"] = Path(artifact_map["response"]["path"]).read_text(encoding="utf-8")
                result["session_id"] = str(managed_receipt.get("session_id") or "")
                if not result["session_id"] or not str(result["response"]).strip():
                    raise WorkflowError(f"step[{step_id}]: managed-agent session/response is empty")
                terminal_result = managed_receipt.get("terminal_result")
                if managed_spec.get("require_terminal_result") is True:
                    expected_terminal = {
                        "schema_version": "juno_managed_agent_terminal_result.v1",
                        "workflow_step_digest": command_digest,
                        "session_id": result["session_id"],
                        "identity_sha256": canonical_sha256(managed_receipt.get("identity")),
                        "response_sha256": artifact_map["response"]["sha256"],
                    }
                    if (not isinstance(terminal_result, dict)
                            or set(terminal_result) != {*expected_terminal, "state"}
                            or any(terminal_result.get(key) != value for key, value in expected_terminal.items())
                            or terminal_result.get("state") not in {"completed", "blocked", "incomplete", "failed"}):
                        raise WorkflowError(
                            f"step[{step_id}]: managed-agent terminal result is missing or unbound: "
                            f"expected={expected_terminal!r} observed={terminal_result!r}")
                    result["semantic_outcome"] = terminal_result["state"]
                    result["terminal_result"] = terminal_result
                    if terminal_result["state"] != "completed":
                        raise WorkflowError(
                            f"step[{step_id}]: managed-agent terminal outcome is {terminal_result['state']}")
                result["managed_agent"] = {"receipt": str(managed_receipt_path), "sha256": file_sha256(managed_receipt_path)}
            except (WorkflowError, OSError, ValueError, KeyError) as exc:
                result["status"] = "failed"; result["failure_reason"] = str(exc)
        apply_semantic_outcome_contract(step, result, bool(args.dry_run))
        status = str(result.get("status", status))
        if is_juno_command and not args.dry_run and status == "success" and not str(result.get("response", "")).strip():
            status = "failed"
            result["status"] = status
            result["failure_reason"] = "empty response from detected agent command"
        if status == "failed" and exit_code == 0:
            exit_code = 1
            result["exit_code"] = 1
            failure_reason = str(result.get("failure_reason") or "semantic contract failed")
            if failure_reason not in stderr:
                stderr = (stderr + ("\n" if stderr else "") + failure_reason + "\n")
                result["stderr"] = stderr
        write_text(stderr_path, stderr)
        write_text(legacy_step_dir / "stderr.txt", stderr)
        if stderr and status == "failed":
            print(stderr, file=sys.stderr, end="" if stderr.endswith("\n") else "\n")
        if result.get("session_id") or explicit_continue_from_step in {step_id, str(step.get("name") or "")}:
            session_candidates.append({
                "index": index,
                "id": step_id,
                "name": str(step.get("name") or ""),
                "session_id": str(result.get("session_id") or ""),
                "status": result.get("status", status),
                "command": command,
            })
        write_text(response_path, str(result.get("response", "")))
        write_text(legacy_step_dir / "response.txt", str(result.get("response", "")))
        if status == "success" and not args.dry_run:
            try:
                if (launch_ownership.launched
                        and launch_ownership.process_group_id > 0
                        and process_group_is_active(launch_ownership.process_group_id)):
                    raise WorkflowError(f"step[{step_id}]: command process group remains active after command exit")
                composition_evidence = reconcile_candidate_composition(step, context, project_root)
                produced_receipts: dict[str, Any] = {}
                for contract in receipts_by_producer.get(step_id, []):
                    produced_receipts[contract["id"]] = validate_receipt_file(
                        contract, context, project_root, command_digest
                    )
                artifacts = {
                    "stdout": {"path": str(stdout_path.resolve()), "sha256": file_sha256(stdout_path)},
                    "stderr": {"path": str(stderr_path.resolve()), "sha256": file_sha256(stderr_path)},
                    "response": {"path": str(response_path.resolve()), "sha256": file_sha256(response_path)},
                }
                if stage_evidence_path.is_file():
                    if stage_evidence is None or stage_evidence.get("passed") is not True:
                        raise WorkflowError(f"step[{step_id}]: successful managed-agent step lacks passing stage boundary")
                    artifacts["stage_boundary"] = {
                        "path": str(stage_evidence_path.resolve()), "sha256": file_sha256(stage_evidence_path)
                    }
                if composition_evidence is not None:
                    composition_path = legacy_step_dir / "candidate_composition.json"
                    write_text(composition_path, json.dumps(composition_evidence, indent=2, ensure_ascii=False) + "\n")
                    artifacts["candidate_composition"] = {
                        "path": str(composition_path.resolve()), "sha256": file_sha256(composition_path)
                    }
                    result["candidate_composition"] = composition_evidence
                candidate_anchor: dict[str, Any] | None = None
                if step.get("candidate_read_only") is not None:
                    if candidate_guard is None or candidate_guard.get("passed") is not True:
                        raise WorkflowError(f"step[{step_id}]: successful candidate review lacks passing guard evidence")
                    candidate_anchor = {
                        "path": str(candidate_guard_path.resolve()),
                        "sha256": file_sha256(candidate_guard_path),
                        "evidence_sha256": canonical_sha256(candidate_guard),
                    }
                    artifacts["candidate_read_only"] = {
                        "path": candidate_anchor["path"], "sha256": candidate_anchor["sha256"]
                    }
                if capture_enabled:
                    if not capture_path.is_file():
                        raise WorkflowError(f"step[{step_id}].capture: required capture artifact missing")
                    artifacts["capture"] = {
                        "path": str(capture_path.resolve()), "sha256": file_sha256(capture_path)
                    }
                managed_anchor: dict[str, Any] | None = None
                if is_managed_agent:
                    if managed_receipt_path is None or managed_receipt is None:
                        raise WorkflowError(f"step[{step_id}]: successful managed-agent step lacks receipt")
                    managed_anchor = {"path": str(managed_receipt_path), "sha256": file_sha256(managed_receipt_path),
                                      "run_root": str(managed_receipt_path.parent), "artifacts": managed_receipt["artifacts"],
                                      "session_id": managed_receipt["session_id"],
                                      "effective_hook_policy": managed_receipt["effective_hook_policy"]}
                    artifacts["managed_agent_receipt"] = {"path": str(managed_receipt_path), "sha256": managed_anchor["sha256"]}
                checkpoint = {
                    "checkpoint_schema": "juno_workflow_step_checkpoint.v1",
                    "checkpoint_complete": True,
                    "workflow_id": workflow_id,
                    "run_directory": str(out_dir.resolve()),
                    "attempt_id": run_id,
                    "step_id": step_id,
                    "step_index": index - 1,
                    "command": command,
                    "command_sha256": command_digest,
                    "status": "success",
                    "semantic_outcome": str(result.get("semantic_outcome") or "completed"),
                    "transport_status": str(result.get("transport_status") or "success"),
                    "exit_code": int(result.get("exit_code") or 0),
                    "started_at": activity.get("started_at") if 'activity' in locals() else "",
                    "completed_at": _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z"),
                    "duration_seconds": duration,
                    "artifacts": artifacts,
                    "receipts": produced_receipts,
                    "child_steps": child_steps,
                    "candidate_read_only": candidate_anchor,
                    "managed_agent": managed_anchor,
                    "stage_boundary": (
                        {"path": str(stage_evidence_path.resolve()), "sha256": file_sha256(stage_evidence_path)}
                        if stage_evidence_path.is_file() else None
                    ),
                    "receipt_contracts_sha256": run_contract["receipt_contracts_sha256"],
                    "workflow_model_policy_sha256": model_policy["workflow_models_sha256"],
                    "workflow_model_selection": model_selection,
                }
                run_contract.setdefault("completed_steps", {})[step_id] = checkpoint
                write_text(run_contract_path, json.dumps(run_contract, indent=2, ensure_ascii=False) + "\n")
                inject_interruption("checkpoint_before_terminal_manifest")
                remove_persisted_marker(out_dir / "active_step.json")
            except (WorkflowError, OSError) as exc:
                status = "failed"
                result["status"] = "failed"
                result["failure_reason"] = str(exc)
                stderr = (stderr + ("\n" if stderr else "") + str(exc) + "\n")
                result["stderr"] = stderr
                write_text(stderr_path, stderr)
                write_text(legacy_step_dir / "stderr.txt", stderr)
        elif not args.dry_run:
            remove_persisted_marker(out_dir / "active_step.json")
        if args.print_step_stdout:
            response_text = str(result.get("response", ""))
            print(step_separator("RESPONSE", index, step_id))
            print(response_text, end="" if response_text.endswith("\n") or not response_text else "\n")
            if not response_text:
                print("(response is empty)")
        context["steps"][step_id] = result
        manifest["steps"].append({k: v for k, v in result.items() if k not in {"stdout", "stderr"}})
        print(step_separator("END", index, step_id, f"status={status} duration={duration:.3f}s exit={exit_code}"))
        append_live_log(
            live_log_path,
            f"=== END step {index} [{step_id}] status={status} duration={duration:.3f}s exit={exit_code} ===\n",
        )
        if status == "failed":
            manifest["failed_steps"].append(step_id)
            manifest["status"] = "failed"
            if step_should_fail_process(step):
                final_exit = exit_code or 1
                break

    final_worktree_evidence = None
    final_worktree = workflow.get("final_worktree")
    if final_worktree is not None and not args.dry_run:
        final_root = Path(str(final_worktree["path"])).resolve()
        top = subprocess.run(["git", "-C", str(final_root), "rev-parse", "--show-toplevel"],
                             text=True, capture_output=True, check=False)
        head = subprocess.run(["git", "-C", str(final_root), "rev-parse", "HEAD^{commit}"],
                              text=True, capture_output=True, check=False)
        status_check = subprocess.run(
            ["git", "-C", str(final_root), "status", "--porcelain=v1", "--untracked-files=all"],
            text=True, capture_output=True, check=False)
        passed = (top.returncode == 0 and head.returncode == 0 and status_check.returncode == 0
                  and Path(top.stdout.strip()).resolve() == final_root
                  and head.stdout.strip() == final_worktree["head"] and not status_check.stdout)
        final_worktree_evidence = {"path": str(final_root), "expected_head": final_worktree["head"],
                                   "observed_head": head.stdout.strip(), "clean": not status_check.stdout,
                                   "passed": passed}
        manifest["final_worktree"] = final_worktree_evidence
        if not passed:
            manifest["status"] = "failed"
            manifest["failed_steps"].append("final_worktree")
            final_exit = final_exit or 1

    terminal_gate = str(workflow.get("terminal_gate") or "").strip()
    terminal_result = context["steps"].get(terminal_gate) if terminal_gate else None
    if args.dry_run:
        semantic_status = "dry_run"
    elif terminal_result is None:
        semantic_status = "failed" if manifest["failed_steps"] else "completed"
    elif terminal_result.get("status") in {"success", "reused_verified", "amendment_revalidated"}:
        semantic_status = str(terminal_result.get("semantic_outcome") or "completed")
    else:
        semantic_status = "failed"
    successful_terminal_outcomes = workflow.get("terminal_success_outcomes") or ["completed"]
    if terminal_gate and not args.dry_run and semantic_status not in successful_terminal_outcomes:
        manifest["status"] = "failed"
        if terminal_gate not in manifest["failed_steps"]:
            manifest["failed_steps"].append(terminal_gate)
        final_exit = final_exit or 1
    manifest["terminal_gate"] = terminal_gate or None
    manifest["semantic_status"] = semantic_status
    context["workflow_semantic"] = {"terminal_gate": terminal_gate or "none", "status": semantic_status}

    summary_stdout, summary_stderr, summary_exit, summary_command, summary_session = maybe_run_summary_command(
        workflow, context, project_root, out_dir, bool(args.dry_run), live_log_path, model_policy
    )
    if summary_session:
        session_candidates.append(summary_session)

    selected_continue_step = select_continue_step(workflow, session_candidates)
    if selected_continue_step:
        persisted_continue = persist_continue_context(
            project_root, str(selected_continue_step["session_id"]), selected_continue_step["command"]
        )
        if persisted_continue:
            persisted_continue["step_index"] = str(selected_continue_step["index"])
            persisted_continue["step_id"] = str(selected_continue_step["id"])
            persisted_continue["selected_label"] = session_label(selected_continue_step)
        manifest["continue"] = {
            "step_index": selected_continue_step["index"],
            "step_id": selected_continue_step["id"],
            "session_id": selected_continue_step["session_id"],
            "scope_hash": persisted_continue.get("scope_hash") if persisted_continue else "",
        }
    elif str(workflow.get("continue_from_step") or "").strip():
        raise WorkflowError(f"continue_from_step '{workflow.get('continue_from_step')}' did not produce a session_id")
    summary_capture_result = str(summary_session.get("capture_result", "")) if summary_session else ""
    summary = (
        summary_capture_result.rstrip() + "\n"
        if summary_capture_result
        else summary_stdout.rstrip() + "\n"
        if summary_stdout
        else make_summary(workflow, context, manifest["failed_steps"], bool(args.dry_run))
    )
    semantic_header = (
        f"Controlling gate: {terminal_gate or 'none'}\n"
        f"Semantic outcome: {semantic_status}\n\n"
    )
    summary = semantic_header + summary
    write_text(out_dir / "summary.md", summary)
    manifest["summary_path"] = str(out_dir / "summary.md")
    manifest["summary"] = {
        "stdout_path": str(out_dir / "summary.stdout.txt"),
        "stderr_path": str(out_dir / "summary.stderr.txt"),
        "exit_code": summary_exit,
        "command": summary_command,
        "workflow_model_selection": summary_session.get("workflow_model_selection") if summary_session else None,
    }
    if summary_session:
        manifest["summary"]["session_id"] = summary_session.get("session_id", "")
        manifest["summary"]["capture_enabled"] = summary_session.get("capture_enabled", False)
        manifest["summary"]["capture_json"] = summary_session.get("capture_json", "")
        manifest["summary"]["capture_json_path"] = summary_session.get("capture_json_path", "")
        manifest["summary"]["capture_result"] = summary_session.get("capture_result", "")
    if summary_stdout:
        manifest["summary"]["stdout"] = summary_stdout
    if summary_stderr:
        manifest["summary"]["stderr"] = summary_stderr
    inject_interruption("summary_before_attempt_manifest")
    archive_attempt(out_dir, manifest)
    archived_manifest = out_dir / "attempts" / run_id / "manifest.json"
    inject_interruption("attempt_manifest_before_record")
    run_contract.setdefault("attempts", []).append(
        {
            "attempt_id": run_id,
            "from_step": args.from_step,
            "status": manifest["status"],
            "semantic_status": semantic_status,
            "manifest": str(archived_manifest.resolve()),
            "manifest_sha256": file_sha256(archived_manifest),
        }
    )
    write_text(run_contract_path, json.dumps(run_contract, indent=2, ensure_ascii=False) + "\n")
    inject_interruption("attempt_record_before_root_manifest")
    write_text(out_dir / "manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    write_text(out_dir / "manifest.yaml", workflow_to_yaml(manifest) + "\n")

    output = selected_final_output(args.print_output, context, summary)
    if output:
        print("\n" + output, end="" if output.endswith("\n") else "\n")
    print_session_summary([item for item in session_candidates if item.get("session_id")], persisted_continue)
    if tmux_observer.get("enabled"):
        append_live_log(
            live_log_path,
            f"\n=== WORKFLOW COMPLETE status={manifest['status']} exit={final_exit} ===\n"
            f"Manifest: {out_dir / 'manifest.json'}\n"
            "The tmux observer remains available for review.\n",
        )
        print(f"Observer remains available: {tmux_observer['attach_command']}")
    return final_exit


EXAMPLE_WORKFLOWS = {
    "agent-chain": """schema_version: 1
workflow_id: example_agent_chain
vars:
  run_date: "{{ yesterday_utc }}"
steps:
  - id: first_agent
    command:
      - yy
      - pi
      - |
        %ralph-loop Do a small readonly investigation for {{ run_date }}.
        Finish with: AGENT_RESPONSE_ONE_LINE: <one sentence>
  - id: continue_agent
    command:
      - yy
      - pi
      - --resume
      - "{{ steps.first_agent.session_id }}"
      - |
        Continue the previous session and summarize next actions.
summary:
  command:
    - yy
    - pi
    - |
      Summarize workflow status. First session: {{ steps.first_agent.session_id }}
      First response: {{ steps.first_agent.response }}
""",
    "command-pipeline": """schema_version: 1
workflow_id: example_command_pipeline
vars:
  subject: juno workflow runner
steps:
  - id: collect
    command: |
      printf 'Subject: {{ subject }}\\nDate: {{ today_utc }}\\n'
  - id: summarize
    command: |
      printf 'Summary input:\\n{{ steps.collect.stdout }}\\n'
summary: |
  # Command pipeline summary
  Collect status: {{ steps.collect.status }}
  Summary output: {{ steps.summarize.stdout }}
""",
    "daily-ops": """schema_version: 1
workflow_id: example_daily_ops
vars:
  run_date: "{{ yesterday_utc }}"
steps:
  - id: preflight
    command: |
      printf 'Daily ops preflight for {{ run_date }} in {{ repo_root }}\\n'
  - id: operator_check
    command:
      - yy
      - pi
      - |
        Review the daily workflow context for {{ run_date }}.
        Preflight output: {{ steps.preflight.response }}
        Return one concise operator note.
    fail_workflow: false
  - id: archive_note
    capture_session: false
    command: |
      printf 'operator_session={{ steps.operator_check.session_id }}\\n'
summary: |
  # Daily ops summary
  Run date: {{ run_date }}
  Preflight: {{ steps.preflight.status }}
  Operator session: {{ steps.operator_check.session_id }}
""",
    "production-triage-handoff": """schema_version: 1
workflow_id: production_triage_handoff
vars:
  triage_name: prod-triage-{{ run_id }}
steps:
  - id: discover_issues
    capture_session: false
    command: |
      set -eu
      mkdir -p "{{ out_dir }}"
      # Replace this stub with your production detector, but keep the JSONL contract:
      #   ./scripts/discover-production-issues --jsonl > "{{ out_dir }}/issues.jsonl"
      python3 - <<'PY'
      import json
      from pathlib import Path
      out_dir = Path("{{ out_dir }}")
      issues = [
          {"id": "checkout-5xx-spike", "service": "checkout-api", "severity": "P1", "signal": "5xx rate above 4% for 15 minutes", "dashboard": "https://observability.example/checkout-api", "runbook": "docs/runbooks/checkout-api.md"},
          {"id": "worker-lag", "service": "billing-worker", "severity": "P2", "signal": "queue lag above 10000 jobs", "dashboard": "https://observability.example/billing-worker", "runbook": "docs/runbooks/billing-worker.md"},
      ]
      with (out_dir / "issues.jsonl").open("w", encoding="utf-8") as fh:
          for issue in issues:
              fh.write(json.dumps(issue, ensure_ascii=False) + "\\n")
      item_placeholder = f"{chr(123) * 2}item{chr(125) * 2}"
      (out_dir / "triage_prompt.md").write_text(
          "You are taking over one production issue in a dedicated tmux pane.\\n"
          "Keep this pane available for later session continuation; do not collapse history.\\n"
          f"Issue JSON: {item_placeholder}\\n\\n"
          "Investigate the service, runbook, likely blast radius, immediate mitigations, and follow-up owners. "
          "Finish with a concise HANDOFF_SUMMARY and preserve any session id/artifact paths you create.\\n",
          encoding="utf-8",
      )
      print(out_dir / "issues.jsonl")
      PY
  - id: start_tmux_handoff
    capture_session: false
    command: |
      set -eu
      ./.juno_task/scripts/parallel_runner.sh \\
        --items-file "{{ out_dir }}/issues.jsonl" \\
        --format jsonl \\
        --prompt-file "{{ out_dir }}/triage_prompt.md" \\
        --tmux panes \\
        --tmux-handoff \\
        --max-panes-per-session 4 \\
        --parallel 4 \\
        --name "{{ triage_name }}" \\
        --output-dir "{{ out_dir }}/parallel"
  - id: handoff_summary
    capture_session: false
    command: |
      set -eu
      summary="{{ out_dir }}/handoff_summary.md"
      {
        printf '# Production triage handoff\\n\\n'
        printf 'Issues: `%s`\\n\\n' "{{ out_dir }}/issues.jsonl"
        printf 'Parallel artifacts: `%s`\\n\\n' "{{ out_dir }}/parallel"
        printf 'Attach with `tmux ls | grep pc-{{ triage_name }}` then `tmux attach -t <session>`.\\n\\n'
        printf 'Latest aggregation files preserve each final agent response, commit metadata, cost, and session id so later continuation does not need to reconstruct history from scrollback.\\n\\n'
        find "{{ out_dir }}/parallel" -name 'aggregation_*.json' -print 2>/dev/null | sort || true
      } | tee "$summary"
summary: |
  # Production triage handoff
  Discovery status: {{ steps.discover_issues.status }}
  Handoff status: {{ steps.start_tmux_handoff.status }}
  Summary artifact: {{ out_dir }}/handoff_summary.md
  Parallel artifacts: {{ out_dir }}/parallel
  Attach: tmux ls | grep pc-{{ triage_name }}
""",
    "parallel-kanban-review": """schema_version: 1
workflow_id: parallel_kanban_review
vars:
  review_topic: "Implement the next safe increment"
steps:
  - id: plan_kanban_tasks
    command:
      - yy
      - pi
      - "Plan mode: create the concrete kanban tasks needed for this topic, then print TASK_IDS=<comma-separated-kanban-task-ids>. Topic: {{ review_topic }}. Use ./.juno_task/scripts/kanban.sh as the source of truth. Keep task bodies complete enough for parallel agents."
  - id: resolve_task_ids
    capture_session: false
    command: |
      set -eu
      mkdir -p "{{ out_dir }}"
      cp "{{ steps.plan_kanban_tasks.response_path }}" "{{ out_dir }}/plan_response.txt"
      task_ids=$(python3 - <<'PY'
      import re
      from pathlib import Path
      text = Path("{{ out_dir }}/plan_response.txt").read_text(encoding="utf-8")
      match = re.search(r"^TASK_IDS=([^\\n]+)", text, re.MULTILINE)
      print(match.group(1).strip() if match else "")
      PY
      )
      if [ -z "$task_ids" ]; then
        echo "plan_kanban_tasks must print TASK_IDS=<comma-separated-kanban-task-ids>" >&2
        exit 2
      fi
      printf '%s\\n' "$task_ids" | tee "{{ out_dir }}/kanban_task_ids.txt"
  - id: run_parallel_kanban
    capture_session: false
    command: |
      set -eu
      task_ids=$(cat "{{ out_dir }}/kanban_task_ids.txt")
      python3 - <<'PY'
      from pathlib import Path
      out_dir = Path("{{ out_dir }}")
      task_placeholder = f"{chr(123) * 2}task_id{chr(125) * 2}"
      (out_dir / "kanban_worker_prompt.md").write_text(
          f"Implement exactly kanban task ##{task_placeholder}.\\n"
          "Keep the kanban response current, run focused validation, and ensure final output includes commit hash, changed files, validation commands, and any preserved session id.\\n",
          encoding="utf-8",
      )
      PY
      ./.juno_task/scripts/parallel_runner.sh \\
        --kanban "$task_ids" \\
        --parallel 3 \\
        --prompt-file "{{ out_dir }}/kanban_worker_prompt.md" \\
        --output-dir "{{ out_dir }}/parallel"
  - id: prepare_master_review
    capture_session: false
    command: |
      set -eu
      latest=$(find "{{ out_dir }}/parallel" -name 'aggregation_*.json' -print 2>/dev/null | sort | tail -n 1)
      if [ -z "$latest" ]; then
        echo "No aggregation_*.json found under {{ out_dir }}/parallel" >&2
        exit 2
      fi
      {
        printf 'Review the completed parallel kanban batch for topic: %s.\\n\\n' "{{ review_topic }}"
        printf 'Read the latest aggregation artifact at: %s\\n' "$latest"
        printf 'It preserves each worker final response, session id, commit hash, status, and cost so this master review does not need to reconstruct history from raw logs.\\n\\n'
        printf 'Aggregation JSON:\\n'
        cat "$latest"
        printf '\\n\\nProduce a concise merge/review plan with: completed tasks, failures needing follow-up, commits to inspect, validation gaps, and recommended next kanban updates.\\n'
      } > "{{ out_dir }}/master_review_prompt.md"
  - id: master_review
    capture_session: true
    command:
      - yy
      - pi
      - --prompt-file
      - "{{ out_dir }}/master_review_prompt.md"
summary: |
  # Parallel kanban review
  Plan session: {{ steps.plan_kanban_tasks.session_id }}
  Task ids: {{ steps.resolve_task_ids.response }}
  Parallel artifacts: {{ out_dir }}/parallel
  Master review session: {{ steps.master_review.session_id }}
  Master review response: {{ steps.master_review.response }}
""",
}


def iter_template_strings(value: Any, path: str = "") -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if isinstance(value, str):
        found.append((path or "$", value))
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            found.extend(iter_template_strings(item, f"{path}[{idx}]" if path else f"[{idx}]"))
    elif isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            found.extend(iter_template_strings(item, child))
    return found


def workflow_lint_findings(
    workflow: dict[str, Any], policy: dict[str, Any] | None = None
) -> list[dict[str, str]]:
    validate_workflow(workflow, policy)
    agent_steps = {
        str(step["id"])
        for step in workflow.get("steps", [])
        if detect_juno_command(step.get("command"))
    }
    findings: list[dict[str, str]] = []
    steps_by_id = {str(step["id"]): step for step in workflow.get("steps", [])}
    for receipt_id, contract in normalize_receipt_contracts(workflow).items():
        declared_path = str(contract["path"])
        filename = Path(declared_path.replace("{{ out_dir }}", "out")).name
        stem = re.sub(r"[^a-z0-9]+", "_", Path(filename).stem.lower()).strip("_")
        normalized_id = re.sub(r"[^a-z0-9]+", "_", receipt_id.lower()).strip("_")
        if not stem or not normalized_id.endswith(stem):
            continue
        producer = steps_by_id.get(str(contract["producer"]))
        producer_command = (producer or {}).get("command")
        canonical_expression = "receipts." + receipt_id + ".path"
        if any(
            match.group(1).strip() == canonical_expression
            for _, text in iter_template_strings(producer_command)
            for match in TEMPLATE_RE.finditer(text)
        ):
            continue
        for location, text in iter_template_strings(producer_command, f"steps.{contract['producer']}.command"):
            for literal_path in re.findall(r"\{\{\s*out_dir\s*\}\}/[^\s'\"`]+\.json", text):
                normalized_path = re.sub(r"\{\{\s*out_dir\s*\}\}", "{{ out_dir }}", literal_path)
                if Path(normalized_path.replace("{{ out_dir }}", "out")).name == filename and normalized_path != declared_path:
                    findings.append({
                        "level": "error",
                        "code": "CONTRADICTORY_RECEIPT_PATH",
                        "location": location,
                        "message": (
                            f"Producer hardcodes receipt path {normalized_path!r}, but receipt {receipt_id} "
                            f"declares {declared_path!r}; reference {{{{ receipts.{receipt_id}.path }}}} instead."
                        ),
                    })
    for location, text in iter_template_strings(workflow):
        for match in re.finditer(r"steps\.([A-Za-z_][A-Za-z0-9_-]*)\.stderr\b", text):
            findings.append({
                "level": "warn",
                "code": "NOISY_STEP_STDERR_TEMPLATE",
                "location": location,
                "message": f"Template references steps.{match.group(1)}.stderr; keep stderr as an artifact and include it only for failure debugging.",
            })
        for match in re.finditer(r"steps\.([A-Za-z_][A-Za-z0-9_-]*)\.stdout\b", text):
            step_id = match.group(1)
            if step_id in agent_steps:
                findings.append({
                    "level": "warn",
                    "code": "AGENT_STDOUT_TEMPLATE",
                    "location": location,
                    "message": f"Template references steps.{step_id}.stdout for an agent step; use steps.{step_id}.response for the final answer.",
                })
    return findings


def print_findings(title: str, findings: list[dict[str, str]]) -> None:
    print(title)
    if not findings:
        print("OK: no issues found")
        return
    for item in findings:
        print(f"{item['level'].upper()} {item['code']} at {item['location']}: {item['message']}")


def run_lint_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="workflow_runner.sh lint",
        description="Lint workflow YAML for response/log anti-patterns before running it.",
        epilog="""Checks:
  - summary/step templates should use steps.<id>.response for agent final answers
  - steps.<id>.stderr should not be injected into prompts/summaries by default
  - producer paths that contradict an identifiable declared receipt are rejected
  - YAML/schema validation is performed using the same parser as workflow execution

Examples:
  workflow_runner.sh lint --workflow .juno_task/workflows/daily_product_ops.yaml
  cat workflow.yaml | workflow_runner.sh lint --workflow -
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--workflow", "-w", required=True, help="Workflow YAML path, or '-' to read from stdin")
    parser.add_argument("--project-root", help="Canonical project config root (defaults to cwd; local integration uses its orchestration workspace)")
    parser.add_argument("--json", action="store_true", help="Emit findings as JSON")
    args = parser.parse_args(argv)
    workflow_text = sys.stdin.read() if args.workflow == "-" else Path(args.workflow).read_text(encoding="utf-8")
    workflow = parse_yaml_like(workflow_text)
    project_root = Path(args.project_root or os.getcwd()).resolve()
    policy = load_workflow_model_policy(project_root)
    findings = workflow_lint_findings(workflow, policy)
    if args.json:
        print(json.dumps({"status": "ok" if not findings else "issues", "findings": findings}, indent=2))
    else:
        print_findings("Workflow lint", findings)
    return 0 if not findings else 1


def process_group_is_active(process_group_id: int) -> bool:
    if process_group_id <= 0:
        return True
    try:
        os.killpg(process_group_id, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True


def recovery_context(contract: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    variables = contract.get("resolved_vars")
    if not isinstance(variables, dict) or canonical_sha256(variables) != contract.get("resolved_vars_sha256"):
        raise WorkflowError("recovery_contract[resolved_vars]: snapshot missing or hash mismatch")
    context: dict[str, Any] = {
        "workflow_id": contract.get("workflow_id"),
        "out_dir": str(run_dir),
        "repo_root": str(contract.get("project_root") or ""),
        "project_root": str(contract.get("project_root") or ""),
        "vars": variables,
        "steps": {},
        "workflow": {"id": contract.get("workflow_id"), "out_dir": str(run_dir)},
    }
    context.update({key: value for key, value in variables.items() if isinstance(key, str)})
    return context


def verify_recovery_contract(contract: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    if str(contract.get("workflow_class") or "").strip() == "local_integration":
        raise WorkflowError(LOCAL_INTEGRATION_HARD_CUT)
    if contract.get("schema_version") != RUN_CONTRACT_SCHEMA:
        raise WorkflowError(f"recovery contract has unsupported schema: {contract.get('schema_version')!r}")
    project_root_text = str(contract.get("project_root") or "").strip()
    if not project_root_text or not Path(project_root_text).is_absolute():
        raise WorkflowError("recovery_contract[project_root]: missing or non-absolute")
    source_text = str(contract.get("workflow_source_path") or "")
    if not source_text:
        raise WorkflowError("recovery requires a path-backed workflow source; stdin-only legacy runs are not recoverable")
    source = Path(source_text)
    if not source.is_file() or file_sha256(source) != contract.get("workflow_source_sha256"):
        raise WorkflowError("recovery_contract[workflow_source_sha256]: workflow source missing or drifted")
    if canonical_sha256(contract.get("receipt_contracts") or {}) != contract.get("receipt_contracts_sha256"):
        raise WorkflowError("recovery_contract[receipt_contracts_sha256]: receipt declarations drifted")
    current_policy = load_workflow_model_policy(Path(project_root_text))
    if current_policy != contract.get("workflow_model_policy"):
        raise WorkflowError("recovery_contract[workflow_model_policy]: project config or workflowModels policy drifted")
    for frozen in contract.get("frozen_inputs") or []:
        path = Path(str(frozen.get("path") or ""))
        expected = frozen.get("sha256")
        if bool(frozen.get("required", True)) and not path.is_file():
            raise WorkflowError(f"recovery frozen_input[{frozen.get('id')}]: required file missing")
        actual = file_sha256(path) if path.is_file() else None
        if actual != expected:
            raise WorkflowError(f"recovery frozen_input[{frozen.get('id')}]: artifact hash mismatch")
    marker = run_dir / "active_step.json"
    if marker.exists():
        active = load_json_object(marker, "active step marker")
        process_group = int(active.get("process_group_id") or 0)
        if not process_group or process_group_is_active(process_group):
            raise WorkflowError(
                f"recovery refused: step {active.get('step_id')!r} is active or its process state is ambiguous"
            )
    return recovery_context(contract, run_dir)


def verify_checkpoint(
    checkpoint: dict[str, Any], step_id: str, index: int, contract: dict[str, Any],
    context: dict[str, Any], run_dir: Path,
) -> dict[str, Any]:
    if checkpoint.get("checkpoint_schema") != "juno_workflow_step_checkpoint.v1" or checkpoint.get("checkpoint_complete") is not True:
        raise WorkflowError(f"recovery checkpoint[{step_id}]: incomplete or unsupported")
    expected = {
        "workflow_id": contract.get("workflow_id"), "run_directory": str(run_dir.resolve()),
        "step_id": step_id, "step_index": index, "status": "success", "exit_code": 0,
        "receipt_contracts_sha256": contract.get("receipt_contracts_sha256"),
    }
    for field, value in expected.items():
        if checkpoint.get(field) != value:
            raise WorkflowError(
                f"recovery checkpoint[{step_id}].{field}: expected={value!r} actual={checkpoint.get(field)!r}"
            )
    command_digest = str(checkpoint.get("command_sha256") or "")
    if not command_digest or canonical_sha256(checkpoint.get("command")) != command_digest:
        raise WorkflowError(f"recovery checkpoint[{step_id}]: rendered command digest mismatch")
    policy = contract.get("workflow_model_policy")
    if not isinstance(policy, dict) or checkpoint.get("workflow_model_policy_sha256") != policy.get("workflow_models_sha256"):
        raise WorkflowError(f"recovery checkpoint[{step_id}]: workflow model policy evidence mismatch")
    if checkpoint.get("managed_agent") is not None:
        selection = {"managed_agent": True, "configured_defaults": True}
    else:
        selection = validate_pi_launch_policy(
            {"command": checkpoint.get("command")}, context=f"recovery checkpoint[{step_id}]", policy=policy
        )
    if checkpoint.get("workflow_model_selection") != selection:
        raise WorkflowError(f"recovery checkpoint[{step_id}]: workflow model selection evidence mismatch")
    artifacts = checkpoint.get("artifacts")
    if not isinstance(artifacts, dict):
        raise WorkflowError(f"recovery checkpoint[{step_id}]: artifact evidence missing")
    required_artifact_ids = ["stdout", "stderr", "response"]
    step_contract = (contract.get("steps") or {}).get(step_id) or {}
    if step_contract.get("candidate_composition_template_sha256") is not None:
        required_artifact_ids.append("candidate_composition")
    if checkpoint.get("managed_agent") is not None:
        required_artifact_ids.extend(["managed_agent_receipt", "stage_boundary"])
    for artifact_id in required_artifact_ids:
        evidence = artifacts.get(artifact_id)
        if not isinstance(evidence, dict):
            raise WorkflowError(f"recovery checkpoint[{step_id}].artifact[{artifact_id}]: evidence missing")
        path = Path(str(evidence.get("path") or ""))
        try:
            path.resolve().relative_to(run_dir.resolve())
        except ValueError:
            raise WorkflowError(f"recovery checkpoint[{step_id}].artifact[{artifact_id}]: cross-run path")
        if not path.is_file() or file_sha256(path) != evidence.get("sha256"):
            raise WorkflowError(f"recovery checkpoint[{step_id}].artifact[{artifact_id}]: hash mismatch")
    managed_anchor = checkpoint.get("managed_agent")
    if managed_anchor is not None:
        if not isinstance(managed_anchor, dict):
            raise WorkflowError(f"recovery checkpoint[{step_id}]: managed-agent evidence malformed")
        managed_root = Path(str(managed_anchor.get("run_root") or "")).resolve()
        try: managed_root.relative_to(run_dir.resolve())
        except ValueError: raise WorkflowError(f"recovery checkpoint[{step_id}]: managed-agent evidence is cross-run")
        receipt_path = Path(str(managed_anchor.get("path") or ""))
        if not receipt_path.is_file() or file_sha256(receipt_path) != managed_anchor.get("sha256"):
            raise WorkflowError(f"recovery checkpoint[{step_id}]: managed-agent receipt drifted")
        managed_receipt = load_json_object(receipt_path, f"recovery checkpoint[{step_id}] managed-agent receipt")
        if managed_receipt.get("state") != "succeeded" or managed_receipt.get("session_id") != managed_anchor.get("session_id"):
            raise WorkflowError(f"recovery checkpoint[{step_id}]: managed-agent terminal/session evidence invalid")
        expected_hook_policy = {
            "schema_version": "juno_managed_hook_policy.v1",
            "external_side_effects": "forbidden", "lifecycle_hooks": "disabled",
            "enforcement": "yy_pi_no_hooks",
        }
        if (managed_receipt.get("effective_hook_policy") != expected_hook_policy
                or managed_anchor.get("effective_hook_policy") != expected_hook_policy):
            raise WorkflowError(f"recovery checkpoint[{step_id}]: managed-agent hook policy evidence invalid")
        stage_anchor = checkpoint.get("stage_boundary")
        if not isinstance(stage_anchor, dict) or stage_anchor != artifacts.get("stage_boundary"):
            raise WorkflowError(f"recovery checkpoint[{step_id}]: stage boundary anchor is missing or inconsistent")
        stage_receipt = load_json_object(Path(str(stage_anchor.get("path") or "")), f"recovery checkpoint[{step_id}] stage boundary")
        if stage_receipt.get("schema_version") != "juno_workflow_stage_boundary.v1" or stage_receipt.get("passed") is not True:
            raise WorkflowError(f"recovery checkpoint[{step_id}]: stage boundary evidence is not successful")
        if (managed_root / "active.json").exists():
            raise WorkflowError(f"recovery checkpoint[{step_id}]: managed-agent child remains active")
        if managed_receipt.get("artifacts") != managed_anchor.get("artifacts"):
            raise WorkflowError(f"recovery checkpoint[{step_id}]: managed-agent artifact binding drifted")
        for artifact_id, item in managed_anchor["artifacts"].items():
            path = Path(str(item.get("path") if isinstance(item, dict) else ""))
            try: path.resolve().relative_to(managed_root)
            except ValueError: raise WorkflowError(f"recovery checkpoint[{step_id}]: managed-agent artifact is cross-run")
            if not path.is_file() or file_sha256(path) != item.get("sha256"):
                raise WorkflowError(f"recovery checkpoint[{step_id}]: managed-agent artifact {artifact_id} drifted")
    declared = contract.get("receipt_contracts") or {}
    produced = {rid: value for rid, value in declared.items() if value.get("producer") == step_id}
    evidence_map = checkpoint.get("receipts") or {}
    if set(evidence_map) != set(produced):
        raise WorkflowError(f"recovery checkpoint[{step_id}]: declared receipt set mismatch")
    for receipt_id, receipt_contract in produced.items():
        evidence = evidence_map[receipt_id]
        path = Path(str(evidence.get("path") or ""))
        expected_path = Path(str((contract.get("resolved_receipt_paths") or {}).get(receipt_id) or "")).resolve()
        if not str((contract.get("resolved_receipt_paths") or {}).get(receipt_id) or "") or path.resolve() != expected_path:
            raise WorkflowError(f"recovery checkpoint[{step_id}].receipt[{receipt_id}]: path mismatch")
        validate_receipt_file(
            receipt_contract, context, Path(str(contract["project_root"])), command_digest,
            str(evidence.get("sha256") or ""),
        )
    verify_persisted_child_steps(checkpoint.get("child_steps", []), step_id, run_dir)
    candidate_evidence = verify_candidate_guard_artifact(
        checkpoint, step_id, Path(str(contract["project_root"]))
    )
    response_path = Path(str(artifacts["response"]["path"]))
    recovered = {
        "id": step_id,
        "command": checkpoint["command"],
        "command_preview": command_preview(checkpoint["command"]),
        "command_sha256": command_digest,
        "status": "success",
        "exit_code": 0,
        "transport_status": checkpoint.get("transport_status", "success"),
        "transport_exit_code": 0,
        "duration_seconds": checkpoint.get("duration_seconds", 0),
        "semantic_outcome": checkpoint.get("semantic_outcome", "completed"),
        "stdout_path": artifacts["stdout"]["path"],
        "stderr_path": artifacts["stderr"]["path"],
        "response_path": artifacts["response"]["path"],
        "response": response_path.read_text(encoding="utf-8"),
        "recovered_from_checkpoint": True,
        "reused_from_attempt": checkpoint.get("attempt_id"),
        "child_steps": checkpoint.get("child_steps", []),
        "workflow_model_selection": checkpoint.get("workflow_model_selection"),
    }
    if candidate_evidence is not None:
        recovered["candidate_read_only_evidence"] = checkpoint["candidate_read_only"]
    return recovered


def recover_attempt(run_dir: Path, dry_run: bool) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    contract_path = run_dir / "run_contract.json"
    contract = load_json_object(contract_path, "run contract")
    context = verify_recovery_contract(contract, run_dir)
    order = contract.get("step_order")
    if not isinstance(order, list) or not order or len(set(order)) != len(order):
        raise WorkflowError("recovery_contract[step_order]: missing, empty, or duplicate")
    completed = contract.get("completed_steps") or {}
    unknown = sorted(set(completed) - set(order))
    if unknown:
        raise WorkflowError(f"recovery completed_steps contains unknown steps: {unknown}")
    verified: list[dict[str, Any]] = []
    first_invalid = len(order)
    missing_seen = False
    for index, raw_id in enumerate(order):
        step_id = str(raw_id)
        checkpoint = completed.get(step_id)
        if checkpoint is None:
            missing_seen = True
            first_invalid = min(first_invalid, index)
            continue
        if missing_seen:
            raise WorkflowError(f"recovery checkpoint[{step_id}]: non-contiguous successful evidence")
        verified.append(verify_checkpoint(checkpoint, step_id, index, contract, context, run_dir))
    latest_attempts = contract.get("attempts") or []
    if latest_attempts and str(latest_attempts[-1].get("status")) in {"success", "failed"}:
        raise WorkflowError("recovery refused: latest attempt already has terminal hash-bound metadata")
    recovery_id = _dt.datetime.now(_dt.timezone.utc).strftime("recovery_%Y%m%d_%H%M%S_%fZ")
    first_invalid_id = str(order[first_invalid]) if first_invalid < len(order) else None
    manifest = {
        "schema_version": "1.0",
        "workflow_id": contract.get("workflow_id"),
        "run_id": recovery_id,
        "out_dir": str(run_dir),
        "repo_root": contract.get("project_root"),
        "steps": verified,
        "failed_steps": [],
        "status": "interrupted",
        "semantic_status": "interrupted",
        "terminal_gate": contract.get("terminal_gate") or None,
        "recovery": {
            "reason": "recovered_from_step_checkpoints",
            "verified_prefix_steps": [step["id"] for step in verified],
            "first_invalid_step": first_invalid_id,
            "first_invalid_step_index": first_invalid,
            "semantic_completion_inferred": False,
        },
    }
    result = {
        "status": "recoverable",
        "run_dir": str(run_dir),
        "verified_prefix_steps": [step["id"] for step in verified],
        "first_invalid_step": first_invalid_id,
        "first_invalid_step_index": first_invalid,
        "dry_run": dry_run,
    }
    if dry_run:
        return result
    archive_attempt(run_dir, manifest)
    archived = run_dir / "attempts" / recovery_id / "manifest.json"
    contract.setdefault("attempts", []).append({
        "attempt_id": recovery_id,
        "from_step": None,
        "status": "interrupted",
        "semantic_status": "interrupted",
        "recovery_reason": "recovered_from_step_checkpoints",
        "first_invalid_step": first_invalid_id,
        "first_invalid_step_index": first_invalid,
        "manifest": str(archived.resolve()),
        "manifest_sha256": file_sha256(archived),
    })
    write_text(contract_path, json.dumps(contract, indent=2, ensure_ascii=False) + "\n")
    result.update({"status": "recovered", "manifest": str(archived.resolve())})
    return result


def run_recover_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="workflow_runner.sh recover-attempt",
        description="Recover a non-active interrupted attempt from complete hash-bound step checkpoints.",
    )
    parser.add_argument("run_dir", help="Workflow run directory containing run_contract.json")
    parser.add_argument("--dry-run", action="store_true", help="Verify and report without appending recovery evidence")
    args = parser.parse_args(argv)
    result = recover_attempt(Path(args.run_dir), bool(args.dry_run))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def file_size(path_text: str | None) -> int | None:
    if not path_text:
        return None
    try:
        return Path(path_text).stat().st_size
    except OSError:
        return None


def command_has_quiet(command: Any) -> bool:
    parts = command_argv(command)
    return any(part in {"--quiet", "--silent", "-q"} for part in parts[1:])


def doctor_findings(run_dir: Path) -> list[dict[str, str]]:
    manifest_path = run_dir / "manifest.json"
    findings: list[dict[str, str]] = []
    try:
        resolved = resolve_workflow_manifest(run_dir)
        manifest_path = resolved.path
        manifest = resolved.payload
    except WorkflowRunEvidenceError as exc:
        message = str(exc)
        code = "INVALID_MANIFEST" if message.startswith("cannot parse") or "must be a JSON object" in message else "MISSING_MANIFEST"
        return [{"level": "error", "code": code, "location": str(manifest_path), "message": message}]
    steps = manifest.get("steps")
    if not isinstance(steps, list):
        return [{"level": "error", "code": "INVALID_MANIFEST_STEPS", "location": str(manifest_path), "message": "manifest.steps must be a list."}]
    for idx, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            continue
        step_id = str(step.get("id") or f"step-{idx}")
        location = f"steps[{idx}].{step_id}"
        command = step.get("command")
        is_agent = detect_juno_command(command)
        status = str(step.get("status") or "")
        response_size = file_size(step.get("response_path"))
        stdout_size = file_size(step.get("stdout_path"))
        stderr_size = file_size(step.get("stderr_path"))
        for field in ("stdout_path", "stderr_path", "response_path"):
            path_text = step.get(field)
            if path_text and not Path(path_text).exists():
                findings.append({"level": "error", "code": "MISSING_ARTIFACT", "location": f"{location}.{field}", "message": f"Artifact path does not exist: {path_text}"})
        if is_agent and command_has_quiet(command):
            findings.append({"level": "warn", "code": "AGENT_QUIET_ARG", "location": location, "message": "Detected agent command includes --quiet/--silent/-q; this can suppress final response in workflow contexts."})
        if is_agent and status == "success" and (response_size == 0 or response_size is None):
            findings.append({"level": "error", "code": "EMPTY_SUCCESS_AGENT_RESPONSE", "location": location, "message": "Agent step is marked success but response artifact is empty/missing; this should be a failure."})
        if status == "success" and stderr_size and stderr_size > 0:
            findings.append({"level": "info", "code": "SUCCESS_STDERR_ARTIFACT", "location": location, "message": f"Successful step has stderr artifact ({stderr_size} bytes); keep it out of summaries unless debugging failures."})
        if is_agent and stdout_size == 0 and response_size == 0:
            findings.append({"level": "warn", "code": "EMPTY_AGENT_STDOUT_RESPONSE", "location": location, "message": "Agent stdout and response are empty; inspect command flags, provider output mode, and stderr artifact."})
        for child in step.get("child_steps") or []:
            child_id = str(child.get("child_id") or "unknown") if isinstance(child, dict) else "unknown"
            child_artifacts = (child.get("artifacts") or {}).items() if isinstance(child, dict) else []
            for artifact_id, evidence in child_artifacts:
                child_path = Path(str(evidence.get("path") or ""))
                if not child_path.is_file() or file_sha256(child_path) != evidence.get("sha256"):
                    findings.append({"level": "error", "code": "CHILD_ARTIFACT_HASH_MISMATCH", "location": f"{location}.child_steps.{child_id}.{artifact_id}", "message": "Child-step artifact is missing or hash-drifted."})
            event = (child.get("event") or {}) if isinstance(child, dict) else {}
            event_path = Path(str(event.get("path") or ""))
            if not event_path.is_file() or file_sha256(event_path) != event.get("sha256"):
                findings.append({"level": "error", "code": "CHILD_EVENT_HASH_MISMATCH", "location": f"{location}.child_steps.{child_id}.event", "message": "Child-step event is missing or hash-drifted."})
    return findings


def run_doctor_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="workflow_runner.sh doctor",
        description="Inspect a workflow run directory and diagnose response/output artifact problems.",
        epilog="""Checks:
  - manifest and artifact paths exist
  - detected agent steps do not have successful empty responses
  - agent commands are not accidentally quieted
  - successful stderr is identified as log/audit noise, not summary input

Aliases:
  workflow_runner.sh dr ...

Examples:
  workflow_runner.sh doctor .juno_task/specs/workflows/daily_product_ops/20260706_064333_251873Z
  workflow_runner.sh dr --json /tmp/workflow-run
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("run_dir", help="Workflow run artifact directory containing manifest.json")
    parser.add_argument("--json", action="store_true", help="Emit findings as JSON")
    args = parser.parse_args(argv)
    findings = doctor_findings(Path(args.run_dir).resolve())
    if args.json:
        print(json.dumps({"status": "ok" if not any(f["level"] == "error" for f in findings) else "issues", "findings": findings}, indent=2))
    else:
        print_findings("Workflow doctor", findings)
    return 0 if not any(f["level"] == "error" for f in findings) else 1


def init_example(example_args: list[str], force: bool) -> Path:
    if len(example_args) != 2:
        names = ", ".join(sorted(EXAMPLE_WORKFLOWS))
        raise WorkflowError(f"--init-example requires <name> <path>; available examples: {names}")
    name, target_text = example_args
    if name not in EXAMPLE_WORKFLOWS:
        names = ", ".join(sorted(EXAMPLE_WORKFLOWS))
        raise WorkflowError(f"unknown example '{name}'. Available examples: {names}")
    target = Path(target_text).resolve()
    if target.exists() and not force:
        raise WorkflowError(f"refusing to overwrite existing workflow: {target} (pass --force to replace)")
    write_text(target, EXAMPLE_WORKFLOWS[name])
    return target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run an ordered YAML workflow from the project root",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Workflow behavior:
  Commands execute from --run-root/--project-root (default: current directory).
  Step failures continue and final process exit is 0 by default.
  Set fail_workflow: true on a step to make that failed command fail the workflow process.
  yylo, yy, and ypl commands automatically receive JUNO_TOOL_ID and
  JUNO_SUBAGENT_CAPTURE_PATH so steps.<id>.session_id can be used by later steps.
  The runner does not inject --quiet; agent stdout is the canonical response while
  stderr is kept as an artifact and printed only when the step fails.
  Detected agent commands that exit 0 with an empty response are marked failed.
  At the end, yylo/yy/ypl step and summary.command session IDs are listed;
  the final successful agent command is persisted so `yy cc` continues it in the same shell scope.
  Set top-level continue_from_step: <step-id-or-name-or-summary> to persist a specific agent command;
  explicit continue_from_step is strict and fails if that command has no session id.
  Disable capture env per step/summary command with capture_session: false (or capture: false).
  Each run directory owns the checkpoint and attempt index in run_contract.json.
  Successful steps become reusable only after all declared artifacts and receipts are
  atomically hash-bound. recover-attempt refuses active, partial, non-contiguous, or
  drifted evidence and never infers semantic success. --from-step verifies the original
  workflow, variables, commands, frozen_inputs, typed receipts, and reused artifacts. A harness-only correction uses a fresh out-dir,
  amendment_mode: harness_only_validation, --amends-run PRIOR_RUN, and optionally
  --from-step STEP to revalidate/import the successful prefix before executing only
  the requested suffix. Failed or changed predecessor steps are never reused.
  Receipt paths are available as {{ receipts.<id>.path }} and as
  JUNO_WORKFLOW_RECEIPT_<ID>. Receipt producers receive JUNO_WORKFLOW_STEP_ID and JUNO_WORKFLOW_STEP_DIGEST;
  every receipt required_fields list explicitly includes producer_step_digest.
  Legacy workflow_class: local_integration execution is hard-rejected. Use
  `yy task start TASK_ID` and `yy merge land TASK_ID`; doctor remains available for historical artifacts.

Helper commands:
  workflow_runner.sh lint --workflow WORKFLOW.yaml     # flag response/log template anti-patterns
  workflow_runner.sh recover-attempt RUN_DIR [--dry-run] # append fail-closed interrupted-attempt evidence
  workflow_runner.sh doctor RUN_DIR                    # inspect latest hash-bound manifest/artifacts
  workflow_runner.sh dr RUN_DIR                        # short alias for doctor

Example boilerplates (written only when explicitly requested):
  workflow_runner.sh --init-example agent-chain .juno_task/workflows/agent_chain.yaml
  workflow_runner.sh --init-example command-pipeline .juno_task/workflows/command_pipeline.yaml
  workflow_runner.sh --init-example daily-ops .juno_task/workflows/daily_ops.yaml
  workflow_runner.sh --init-example production-triage-handoff .juno_task/workflows/production_triage_handoff.yaml
  workflow_runner.sh --init-example parallel-kanban-review .juno_task/workflows/parallel_kanban_review.yaml

  production-triage-handoff writes safe sample JSONL, then invokes parallel_runner.sh
  with --tmux panes --tmux-handoff --max-panes-per-session 4 and a fixed
  {{ out_dir }}/parallel artifact root. parallel-kanban-review shows plan-created
  kanban tasks flowing through fixed-output parallel execution into a master review
  step. Both preserve final responses and session ids in artifacts so review and
  yy continue handoff do not depend on tmux scrollback.
""",
    )
    parser.add_argument("--workflow", "-w", help="Workflow YAML path, or '-' to read from stdin")
    parser.add_argument(
        "--run-root",
        "--project-root",
        dest="project_root",
        default=os.getcwd(),
        help="Directory where commands execute (default: current directory)",
    )
    parser.add_argument("--out-dir", help="Artifact directory (default: .juno_task/specs/workflows/<workflow_id>/<run_id>)")
    parser.add_argument("--var", dest="vars", action="append", default=[], metavar="NAME=VALUE", help="Template variable override in NAME=VALUE form")
    parser.add_argument("--dry-run", action="store_true", help="Render commands and write artifacts without executing steps")
    parser.add_argument("--from-step", help="Start at zero-based step index, step id/name, or -1 for the last step")
    parser.add_argument("--amends-run", help="Start a fresh harness-only run linked to a prior run; combine with --from-step to revalidate/import its successful prefix")
    parser.add_argument("--print-step-stdout", dest="print_step_stdout", action="store_true", default=True, help="Print each step response/stdout as it completes (default); successful stderr stays in artifacts")
    parser.add_argument("--no-print-step-stdout", dest="print_step_stdout", action="store_false", help="Do not echo per-step response/stdout to the console; artifacts are still written")
    parser.add_argument("--tmux", action="store_true", help="Create a detached observer only; the producer remains attached to this foreground command")
    parser.add_argument("--tmux-session", help="Observer session name (requires --tmux; default is derived from workflow/run id)")
    parser.add_argument(
        "--print-output",
        "--final-output",
        dest="print_output",
        default="summary",
        help="Final console output: summary, none, all, <step_id>, or step:<step_id>",
    )
    parser.add_argument(
        "--init-example",
        nargs=2,
        metavar=("NAME", "PATH"),
        help="Write a built-in example workflow YAML (agent-chain, command-pipeline, daily-ops, production-triage-handoff, parallel-kanban-review) to PATH and exit",
    )
    parser.add_argument("--force", action="store_true", help="Allow --init-example to overwrite an existing file")
    return parser


def resolve_session_metadata_directory(controller_root: str) -> str:
    override = os.environ.get("YYLO_SESSION_METADATA_DIRECTORY", "").strip()
    if override:
        candidate = Path(override)
        return str(candidate if candidate.is_absolute() else (Path(controller_root) / candidate).resolve())
    completed = subprocess.run(
        ["git", "-C", controller_root, "rev-parse", "--path-format=absolute", "--git-common-dir"],
        text=True, capture_output=True,
    )
    if completed.returncode == 0 and completed.stdout.strip():
        return str(Path(completed.stdout.strip()).resolve() / "juno" / "session_metadata")
    identity = hashlib.sha256(str(Path(controller_root).resolve()).encode()).hexdigest()[:16]
    state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return str(state_home / "@yylo/cli" / "session_metadata" / identity)


def resolve_controller_environment() -> dict[str, str]:
    resolver = Path(__file__).resolve().with_name("controller_resolver.py")
    if not resolver.is_file():
        resolver = next(
            (parent / ".juno_task/scripts/controller_resolver.py" for parent in (Path.cwd(), *Path.cwd().parents)
             if (parent / ".juno_task/scripts/controller_resolver.py").is_file()),
            resolver,
        )
    completed = subprocess.run(
        [sys.executable, str(resolver), "--cwd", os.getcwd(), "--operation", "orchestration"],
        text=True, capture_output=True, check=True,
    )
    resolution = json.loads(completed.stdout)
    controller_root = str(resolution["path"])
    return {
        "JUNO_TASK_ROOT": controller_root,
        "JUNO_CONTROLLER_SOURCE": resolution["source"],
        "JUNO_WORKSPACE_ROLE": resolution["role"],
        "YYLO_SESSION_METADATA_DIRECTORY": resolve_session_metadata_directory(controller_root),
    }


def checkpoint_after_finalization(exit_code: int, owner: str) -> None:
    """Best effort only; never replace the owning runner's status."""
    if os.environ.get("JUNO_CONTROLLER_CHECKPOINT_ACTIVE") == "1":
        return
    root = Path(os.environ["JUNO_TASK_ROOT"]).resolve()
    script = root / ".juno_task/scripts/controller_checkpoint.py"
    if not script.is_file():
        return
    message = f"chore(controller): checkpoint finalized {owner} state"
    if exit_code:
        message = f"chore(controller): checkpoint failed {owner} state (exit {exit_code})"
    env = child_process_environment(dict(os.environ))
    env["JUNO_CONTROLLER_CHECKPOINT_ACTIVE"] = "1"
    try:
        completed = subprocess.run(
            [sys.executable, str(script), "--root", str(root), "commit", "--message", message],
            cwd=root, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=30, check=False,
        )
        if completed.returncode:
            detail = (completed.stderr or completed.stdout or f"exit {completed.returncode}").strip()
            detail = re.sub(r"(?im)^(\s*(?:authorization|token|password|secret)\s*[:=]\s*).*$", r"\1[REDACTED]", detail)
            detail = detail[-2000:]
            raise WorkflowError(f"checkpoint exit {completed.returncode}: {detail}")
    except (OSError, subprocess.SubprocessError, WorkflowError) as exc:
        detail = str(exc)
        retryable = bool(re.search(r"lease busy|lock timeout|changed during checkpoint", detail, re.I))
        blocker = "retryable_lock_or_race" if retryable else "deterministic_or_unsafe_state"
        action = ("wait for the owning writer, then rerun the owning command" if retryable else
                  "preserve bytes and run `yy doctor workspace` for exact recovery")
        print(
            f"workflow_runner.sh: WARNING: controller checkpoint failed after finalization; "
            f"blocker={blocker}; safe_next_action={action}; detail={detail}", file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> int:
    sanitize_current_process_environment()
    controller_env = resolve_controller_environment()
    try:
        ensure_controller_python_environment(controller_env)
    except WorkflowError as exc:
        print(f"workflow_runner.sh: error: {exc}", file=sys.stderr)
        return 2
    warn_if_runtime_script_is_stale("workflow_runner.sh")
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "lint":
        try:
            return run_lint_command(argv[1:])
        except WorkflowError as exc:
            print(f"workflow_runner.sh lint: error: {exc}", file=sys.stderr)
            return 2
    if argv and argv[0] in {"doctor", "dr"}:
        try:
            return run_doctor_command(argv[1:])
        except WorkflowError as exc:
            print(f"workflow_runner.sh doctor: error: {exc}", file=sys.stderr)
            return 2
    if argv and argv[0] == "recover-attempt":
        try:
            return run_recover_command(argv[1:])
        except (WorkflowError, OSError) as exc:
            print(f"workflow_runner.sh recover-attempt: error: {exc}", file=sys.stderr)
            return 2
    parser = build_parser()
    args = parser.parse_args(argv)
    exit_code = 0
    try:
        exit_code = run_workflow(args)
    except WorkflowError as exc:
        print(f"workflow_runner.sh: error: {exc}", file=sys.stderr)
        exit_code = 2
    except BaseException:
        exit_code = 1
        raise
    finally:
        # run_workflow has returned or failed only after its terminal manifest,
        # receipts, summary, and continuation metadata writes.
        checkpoint_after_finalization(exit_code, "workflow")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
