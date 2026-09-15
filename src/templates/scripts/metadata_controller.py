#!/usr/bin/env python3
"""Plan and prepare a metadata-only controller without moving live refs.

The helper creates a new, unrelated root commit and linked worktree.  It never
changes controller registration, the product target, or the rollback worktree.
Cutover and rollback are receipts only and require a separate owner-authorized
registrar.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import fcntl
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

os.environ.setdefault("GIT_OPTIONAL_LOCKS", "0")
SCHEMA = "juno_metadata_controller_policy.v1"
PLAN_SCHEMA = "juno_metadata_controller_plan.v1"
RECEIPT_SCHEMA = "juno_metadata_controller_receipt.v1"
CONFIG_REPAIR_SCHEMA = "juno_metadata_controller_config_repair.v1"
AGENT_SURFACE_REPAIR_SCHEMA = "juno_metadata_controller_agent_surface_repair.v1"

def agent_surface_repair_tolerances(root: Path) -> set[str]:
    """Boundary checks a living controller may carry into an agent-surface repair.

    An activated controller (juno.workspace.role=controller), a controller
    whose frozen root boundary receipt predates the integration-workspace
    policy key, and a controller whose reviewed policies legitimately evolved
    after cutover can never satisfy the role, root_preservation, or
    generated_contract checks at repair time. None of them participates in
    evacuation safety: the plan binds its own exact head, tree, entries, and
    policy digest, and the apply path re-verifies that frozen state before and
    after one hermetic evacuation commit.
    """
    tolerated = {"agent_surface_untracked", "tracked_boundary",
                 "root_preservation", "generated_contract"}
    role = git(root, "config", "--worktree", "--get", "juno.workspace.role", check=False)
    if role == "controller":
        tolerated.add("role")
    return tolerated
POLICY_MIGRATION_SCHEMA = "juno_metadata_policy_migration.v1"
POLICY_PATH = ".juno_task/config/metadata-controller.json"
INTEGRATION_POLICY_PATH = ".juno_task/config/integration-workspace.json"
TASK_POLICY_PATH = ".juno_task/config/task-workspace.json"
RISK_POLICY_PATH = ".juno_task/config/risk-policy.json"
POLICY_MIGRATION_PATHS = (INTEGRATION_POLICY_PATH, POLICY_PATH)
AGENT_SURFACE_ROOTS = ("AGENTS.md", "CLAUDE.md", ".agents", ".claude", ".pi")
LEGACY_OPERATIONAL_METADATA = (
    ".juno_task/config/umbrella-admissions",
    ".juno_task/task-scopes",
)
LEGACY_MANAGED_CONTROLLER_METADATA = (
    ".juno_task/USER_FEEDBACK.md",
    ".juno_task/managed-assets.json",
    ".juno_task/plan.md",
    ".juno_task/prompts",
    ".juno_task/wiki",
    ".juno_task/workflows",
)
CORE_CONTROLLER_WIKI = (
    "git_worktree_lifecycle.md",
    "metadata_controller_boundary.md",
    "parallel_runner_and_spec_review.md",
    "runtime_migration_and_replacement_contract.md",
    "task_dependency_hydration.md",
    "tmux_best_practices.md",
    "wiki_maintenance.md",
    "yy_pi_progress.md",
)
REQUIRED_ROOT_IGNORES = ("/AGENTS.md", "/CLAUDE.md", "/.agents/", "/.claude/", "/.pi/")
CANONICAL_CONTROLLER_WORKSPACE = {
    "mode": "metadata-only", "policy": ".juno_task/config/metadata-controller.json"}
RETIRED_CONTROLLER_WORKSPACE = {
    "enabled": True, "policy": ".juno_task/config/controller-workspace.json"}
CANONICAL_CONTROLLER_CONFIG = {"controllerWorkspace": CANONICAL_CONTROLLER_WORKSPACE}
CONTROLLER_SAFE_CONFIG_FIELDS = {
    "configVersion", "defaultSubagent", "defaultBackend", "defaultMaxIterations",
    "defaultModel", "defaultModels", "workflowModels", "mainTask", "logLevel",
    "logFile", "verbose", "quiet", "mcpTimeout", "mcpRetries", "mcpServerPath",
    "mcpServerName", "hookCommandTimeout", "onHourlyLimit", "interactive",
    "headlessMode", "kanbanRegistry", "gitCheckpoint",
}
CONTROLLER_PRODUCT_CONFIG_FIELDS = {
    "workingDirectory", "sessionDirectory", "gitFlow", "autoDependencyUpdate", "hooks", "skipHooks",
}
CONTROLLER_PROFILE_CONFIG_FIELDS = CONTROLLER_SAFE_CONFIG_FIELDS | {
    "agentProfile", "promptMacros", "modelShortcuts", "headlessUi",
}
AGENT_MIGRATION_DISPOSITIONS = {"migrate", "transform", "product-only", "secret", "retire"}
CONFIG_PATH = ".juno_task/config.json"
SHA_RE = re.compile(r"[0-9a-f]{40,64}\Z")
MAX_LOCAL_RUNTIME_ARTIFACT_BYTES = 256 * 1024 * 1024
MAX_RUNTIME_ARCHIVE_MEMBERS = 100_000
MAX_RUNTIME_ARCHIVE_EXPANDED_BYTES = 1024 * 1024 * 1024
MAX_RUNTIME_MANIFEST_BYTES = 1024 * 1024


class BoundaryError(RuntimeError):
    pass


def run(argv: list[str], cwd: Path, check: bool = True, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, cwd=cwd, text=True, capture_output=True, env={**os.environ, **(env or {})})
    if check and result.returncode:
        raise BoundaryError(result.stderr.strip() or result.stdout.strip() or f"command failed: {argv}")
    return result


def git(root: Path, *args: str, check: bool = True, env: dict[str, str] | None = None) -> str:
    return run(["git", "-C", str(root), *args], root, check, env=env).stdout.strip()


def canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def bytes_digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_controller_config_bytes() -> bytes:
    return canonical(CANONICAL_CONTROLLER_CONFIG)


def committed_bytes(root: Path, head: str, relative: str) -> bytes:
    result = run(["git", "-C", str(root), "show", f"{head}:{relative}"], root, False)
    if result.returncode:
        raise BoundaryError(f"controller is missing committed {relative}")
    return result.stdout.encode()


def controller_config(root: Path, head: str) -> tuple[bytes, dict[str, Any]]:
    data = committed_bytes(root, head, CONFIG_PATH)
    try:
        value = json.loads(data)
    except json.JSONDecodeError as exc:
        raise BoundaryError(f"controller config is invalid: {exc}") from exc
    if not isinstance(value, dict):
        raise BoundaryError("controller config must be a JSON object")
    return data, value


def require_canonical_controller_config(root: Path, head: str) -> dict[str, Any]:
    _, value = controller_config(root, head)
    if ("lifecycle" in value
            or value.get("controllerWorkspace") != CANONICAL_CONTROLLER_WORKSPACE):
        raise BoundaryError("controller config does not contain the exact canonical metadata-only workspace shape")
    return value


def reviewed_path(path: Path, label: str) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise BoundaryError(f"{label} does not resolve to a reviewed file: {exc}") from exc
    if not resolved.is_file():
        raise BoundaryError(f"{label} is not a regular file: {resolved}")
    return resolved


def read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BoundaryError(f"invalid {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise BoundaryError(f"{label} must be a JSON object")
    return value


def load_sibling(name: str) -> Any:
    path = Path(__file__).resolve().with_name(name)
    spec = importlib.util.spec_from_file_location(f"juno_metadata_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise BoundaryError(f"cannot load packaged policy validator: {name}")
    module = importlib.util.module_from_spec(spec)
    scripts = str(path.parent)
    sys.path.insert(0, scripts)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(scripts)
    return module


def validate_task_policy(value: dict[str, Any]) -> dict[str, Any]:
    validator = load_sibling("task_workspace.py")
    with tempfile.TemporaryDirectory(prefix="juno-task-policy-") as temporary:
        root = Path(temporary)
        path = root / ".juno_task/config/task-workspace.json"
        path.parent.mkdir(parents=True)
        path.write_bytes(canonical(value))
        try:
            return validator.load_config(root)
        except Exception as exc:
            raise BoundaryError(f"invalid reviewed task workspace policy: {exc}") from exc


def validate_risk_policy(value: dict[str, Any]) -> dict[str, Any]:
    validator = load_sibling("risk_policy.py")
    with tempfile.TemporaryDirectory(prefix="juno-risk-policy-") as temporary:
        path = Path(temporary) / "risk-policy.json"
        path.write_bytes(canonical(value))
        try:
            return validator.load_policy(path)
        except Exception as exc:
            raise BoundaryError(f"invalid reviewed risk policy: {exc}") from exc


def validate_integration_policy(value: dict[str, Any], task_policy: dict[str, Any]) -> dict[str, Any]:
    validator = load_sibling("integration_workspace.py")
    with tempfile.TemporaryDirectory(prefix="juno-integration-policy-") as temporary:
        root = Path(temporary); config = root / ".juno_task/config"; config.mkdir(parents=True)
        (config / "integration-workspace.json").write_bytes(canonical(value))
        (config / "task-workspace.json").write_bytes(canonical(task_policy))
        try:
            policy, _, _ = validator.load_policy(root)
            return policy
        except Exception as exc:
            raise BoundaryError(f"invalid reviewed integration workspace policy: {exc}") from exc


def reviewed_policies_from_sources(
        *, metadata_policy: dict[str, Any], policy_bundle: Path | None = None,
        task_policy: Path | None = None, integration_policy: Path | None = None,
        risk_policy: Path | None = None) -> dict[str, Any]:
    if policy_bundle is not None:
        if task_policy is not None or integration_policy is not None or risk_policy is not None:
            raise BoundaryError("use either --policy-bundle or all explicit policy paths, not both")
        bundle_path = reviewed_path(policy_bundle, "policy bundle")
        bundle = read_json(bundle_path, "policy bundle")
        if (bundle.get("schema_version") != "juno_migration_policy_bundle.v1"
                or bundle.get("operation") != "generate-policy"
                or bundle.get("outcome") != "generated_from_reviewed_answers"
                or not isinstance(bundle.get("policies"), dict)):
            raise BoundaryError("policy bundle is not a reviewed Juno migration policy bundle")
        policies = bundle["policies"]
        if set(policies) != {"metadata_controller", "task_workspace", "integration_workspace", "risk"}:
            raise BoundaryError("policy bundle must contain exactly metadata_controller, task_workspace, integration_workspace, and risk")
        if digest(metadata_policy_from_bundle(bundle_path)) != digest(metadata_policy):
            raise BoundaryError("policy bundle metadata controller policy differs from --policy")
        task_value = validate_task_policy(load_policy_value(policies["task_workspace"]))
        integration_value = validate_integration_policy(
            load_policy_value(policies["integration_workspace"]), task_value)
        risk_value = validate_risk_policy(load_policy_value(policies["risk"]))
        agent_migration = bundle.get("agent_migration")
        if not isinstance(agent_migration, dict):
            raise BoundaryError("policy bundle is missing reviewed agent migration dispositions")
        source = {"kind": "policy_bundle", "path": str(bundle_path), "sha256": file_digest(bundle_path),
                  "agent_migration_sha256": digest(agent_migration)}
    else:
        if task_policy is None or integration_policy is None or risk_policy is None:
            raise BoundaryError("migration-plan requires --policy-bundle or all three workspace/risk policy paths")
        task_path = reviewed_path(task_policy, "task workspace policy")
        integration_path = reviewed_path(integration_policy, "integration workspace policy")
        risk_path = reviewed_path(risk_policy, "risk policy")
        task_value = validate_task_policy(read_json(task_path, "task workspace policy"))
        integration_value = validate_integration_policy(
            read_json(integration_path, "integration workspace policy"), task_value)
        risk_value = validate_risk_policy(read_json(risk_path, "risk policy"))
        source = {"kind": "explicit_paths", "task_workspace_path": str(task_path),
                  "task_workspace_file_sha256": file_digest(task_path),
                  "integration_workspace_path": str(integration_path),
                  "integration_workspace_file_sha256": file_digest(integration_path),
                  "risk_path": str(risk_path),
                  "risk_file_sha256": file_digest(risk_path)}
    return {"source": source, "metadata_controller_sha256": digest(metadata_policy),
            "task_workspace": {"sha256": digest(task_value), "content": task_value},
            "integration_workspace": {"sha256": digest(integration_value), "content": integration_value},
            "risk": {"sha256": digest(risk_value), "content": risk_value}}


def load_policy_value(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BoundaryError("reviewed policy content must be a JSON object")
    return json.loads(json.dumps(value))


def revalidate_planned_policies(plan: dict[str, Any], metadata_policy: dict[str, Any]) -> dict[str, Any]:
    expected = plan.get("reviewed_policies")
    if not isinstance(expected, dict) or not isinstance(expected.get("source"), dict):
        raise BoundaryError("migration plan does not bind reviewed task and risk policies")
    source = expected["source"]
    if source.get("kind") == "policy_bundle":
        actual = reviewed_policies_from_sources(metadata_policy=metadata_policy,
                                                policy_bundle=Path(source["path"]))
    elif source.get("kind") == "explicit_paths":
        actual = reviewed_policies_from_sources(
            metadata_policy=metadata_policy, task_policy=Path(source["task_workspace_path"]),
            integration_policy=Path(source["integration_workspace_path"]),
            risk_policy=Path(source["risk_path"]))
    else:
        raise BoundaryError("migration plan has an unsupported reviewed policy source")
    if digest(actual) != digest(expected):
        raise BoundaryError("reviewed task workspace or risk policy changed after planning")
    return expected


def atomic_receipt(path: Path, value: dict[str, Any]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = canonical(value)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{hashlib.sha256(data).hexdigest()[:16]}")
    with temporary.open("xb") as stream:
        stream.write(data); stream.flush(); os.fsync(stream.fileno())
    try:
        # link() is an atomic no-clobber publication. A concurrent creator is
        # never overwritten; identical immutable evidence is idempotent.
        try: os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != data:
                raise BoundaryError(f"immutable receipt collision: {path}")
    finally:
        temporary.unlink(missing_ok=True)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try: os.fsync(directory_fd)
    finally: os.close(directory_fd)


def preflight_receipt(path: Path, value: dict[str, Any]) -> None:
    path = path.expanduser().resolve()
    if path.exists() and path.read_bytes() != canonical(value):
        raise BoundaryError(f"immutable receipt collision: {path}")


def safe_relative(value: Any) -> str:
    if not isinstance(value, str) or value != value.strip() or not value:
        raise BoundaryError("policy paths must be non-empty normalized strings")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or value in {".", ".git"} or ".." in path.parts or ".git" in path.parts:
        raise BoundaryError(f"unsafe policy path: {value!r}")
    return value.rstrip("/")


def safe_ref(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.startswith("refs/heads/"):
        raise BoundaryError(f"{label} must be a full local branch ref")
    if run(["git", "check-ref-format", value], Path.cwd(), False).returncode:
        raise BoundaryError(f"unsafe {label}: {value!r}")
    return value


def load_policy(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise BoundaryError(f"invalid metadata controller policy: {exc}") from exc
    required = {"schema_version", "controller_branch", "product_ref", "spec_copy_mode", "copied_metadata", "generated_metadata", "product_forbidden", "tracked_exact", "tracked_recursive", "tracked_top_level_files", "runtime"}
    if not isinstance(value, dict) or set(value) != required or value.get("schema_version") != SCHEMA:
        raise BoundaryError(f"policy must contain exactly the {SCHEMA} fields")
    value["controller_branch"] = safe_ref(value["controller_branch"], "controller_branch")
    value["product_ref"] = safe_ref(value["product_ref"], "product_ref")
    if value["spec_copy_mode"] != "top_level_files_only":
        raise BoundaryError("spec_copy_mode must exclude nested workflow and lifecycle evidence")
    for field in ("copied_metadata", "generated_metadata", "product_forbidden", "tracked_exact", "tracked_recursive", "tracked_top_level_files"):
        items = value[field]
        if not isinstance(items, list) or not items:
            raise BoundaryError(f"{field} must be a non-empty array")
        normalized = sorted(safe_relative(item) for item in items)
        if len(normalized) != len(set(normalized)):
            raise BoundaryError(f"{field} contains duplicates")
        value[field] = normalized
    runtime = value["runtime"]
    if not isinstance(runtime, dict) or set(runtime) != {"package", "identity_file", "ignored_roots"} or runtime.get("package") != "@yylo/cli":
        raise BoundaryError("invalid runtime policy")
    runtime["identity_file"] = safe_relative(runtime["identity_file"])
    runtime["ignored_roots"] = sorted(safe_relative(item) for item in runtime["ignored_roots"])
    if ".gitignore" not in value["generated_metadata"] or ".gitignore" not in value["tracked_exact"]:
        raise BoundaryError("metadata policy must generate and track .gitignore")
    missing_ignored = sorted(set(AGENT_SURFACE_ROOTS) - set(runtime["ignored_roots"]))
    if missing_ignored:
        raise BoundaryError("runtime policy must ignore the complete controller agent surface: " + ", ".join(missing_ignored))
    for item in value["copied_metadata"] + value["generated_metadata"]:
        if not policy_path_allowed(item, value, container=True):
            raise BoundaryError(f"metadata path is outside tracked roots: {item}")
    return value


def metadata_policy_from_bundle(path: Path) -> dict[str, Any]:
    bundle_path = reviewed_path(path, "policy bundle")
    bundle = read_json(bundle_path, "policy bundle")
    policies = bundle.get("policies")
    if not isinstance(policies, dict) or not isinstance(policies.get("metadata_controller"), dict):
        raise BoundaryError("policy bundle does not contain a metadata controller policy")
    with tempfile.TemporaryDirectory(prefix="juno-metadata-policy-") as temporary:
        policy_path = Path(temporary) / "metadata-controller.json"
        policy_path.write_bytes(canonical(policies["metadata_controller"]))
        return load_policy(policy_path)


def policy_from_plan_bundle(path: Path) -> dict[str, Any] | None:
    plan = read_json(path.expanduser().resolve(), "migration plan")
    reviewed = plan.get("reviewed_policies")
    source = reviewed.get("source") if isinstance(reviewed, dict) else None
    if not isinstance(source, dict) or source.get("kind") != "policy_bundle":
        return None
    bundle_path = source.get("path")
    if not isinstance(bundle_path, str) or not bundle_path:
        raise BoundaryError("migration plan has an invalid reviewed policy bundle path")
    return metadata_policy_from_bundle(Path(bundle_path))


def ref_exists(root: Path, ref: str) -> bool:
    return run(["git", "-C", str(root), "show-ref", "--verify", "--quiet", ref], root, False).returncode == 0


def common_dir(root: Path) -> str:
    return str(Path(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve())


def exact_worktree(root: Path) -> Path:
    root = root.expanduser().resolve()
    actual = git(root, "rev-parse", "--show-toplevel", check=False)
    if not actual or Path(actual).resolve() != root:
        raise BoundaryError(f"not an exact Git worktree: {root}")
    return root


def resolve_commit(root: Path, ref: str, expected: str, label: str) -> str:
    actual = git(root, "rev-parse", f"{ref}^{{commit}}", check=False)
    if actual != expected or not SHA_RE.fullmatch(expected):
        raise BoundaryError(f"{label} changed: expected {expected}, found {actual or '<missing>'}")
    return actual


def valid_semver(value: Any) -> bool:
    """Use the packaged task-runtime's single SemVer 2.0.0 validator."""
    return bool(load_sibling("task_workspace.py").is_valid_semver(value))


def runtime_identity(executable: Path, expected_version: str, repository: Path) -> dict[str, str]:
    executable = executable.expanduser().resolve()
    if not valid_semver(expected_version):
        raise BoundaryError("runtime version must be an exact released semantic version")
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise BoundaryError(f"runtime executable is not executable: {executable}")
    # A released installation must not resolve into this repository, any of its
    # linked worktrees, its administration directory, or another Git checkout.
    repo = repository.resolve()
    prohibited = {repo, Path(common_dir(repo)).resolve()}
    listing = run(["git", "-C", str(repo), "worktree", "list", "--porcelain", "-z"], repo)
    for record in listing.stdout.split("\0"):
        if record.startswith("worktree "):
            prohibited.add(Path(record.removeprefix("worktree ")).resolve())
    for root in prohibited:
        try:
            executable.relative_to(root)
        except ValueError:
            continue
        raise BoundaryError("runtime executable must be an installed distribution outside every linked worktree and Git administration directory")
    containing_repo = git(executable.parent, "rev-parse", "--show-toplevel", check=False)
    if containing_repo:
        raise BoundaryError(
            "runtime executable must not come from a mutable Git worktree (including an unrelated Git ancestor such as ~/.nvm); "
            "install and rebind an exact release into a fresh non-Git prefix with `yy migrate runtime-install-rebind --help`"
        )
    result = run([str(executable), "--version"], executable.parent, False)
    if (result.returncode
            or not load_sibling("task_workspace.py").cli_version_output_valid(
                result, expected_version, executable.parent)):
        raise BoundaryError(f"runtime identity mismatch: expected yylo {expected_version}")
    return {"package": "@yylo/cli", "version": expected_version, "executable": str(executable),
            "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest()}


def install_runtime_scripts(executable: Path, controller: Path) -> dict[str, Any]:
    source = executable.expanduser().resolve().parent.parent / "templates/scripts"
    target = controller / ".juno_task/scripts"
    if not source.is_dir():
        raise BoundaryError(f"installed runtime is missing packaged controller scripts: {source}")
    if target.exists() or target.is_symlink():
        raise BoundaryError("fresh controller runtime script directory must not already exist")
    entries: list[dict[str, str]] = []
    for item in sorted(source.rglob("*")):
        if item.is_symlink():
            raise BoundaryError(f"packaged runtime scripts may not contain symlinks: {item}")
        if item.is_dir():
            continue
        if not item.is_file():
            raise BoundaryError(f"packaged runtime contains an unsafe script entry: {item}")
        entries.append({"path": item.relative_to(source).as_posix(),
                        "sha256": hashlib.sha256(item.read_bytes()).hexdigest()})
    if not entries or not any(entry["path"] == "controller_resolver.py" for entry in entries):
        raise BoundaryError("installed runtime script set is incomplete")
    shutil.copytree(source, target, copy_function=shutil.copy2)
    return {"source": str(source), "file_count": len(entries), "sha256": digest(entries)}


def packaged_controller_wiki(executable: Path) -> tuple[dict[str, bytes], dict[str, Any]]:
    source = executable.expanduser().resolve().parent.parent / "templates/wiki/controller"
    if not source.is_dir() or source.is_symlink():
        raise BoundaryError(f"installed runtime is missing packaged controller wiki: {source}")
    generated: dict[str, bytes] = {}
    entries: list[dict[str, str]] = []
    for name in CORE_CONTROLLER_WIKI:
        item = source / name
        if item.is_symlink() or not item.is_file():
            raise BoundaryError(f"packaged controller wiki is incomplete or unsafe: {item}")
        data = item.read_bytes()
        destination = f".juno_task/wiki/controller/{name}"
        generated[destination] = data
        entries.append({"path": destination, "sha256": hashlib.sha256(data).hexdigest()})
    return generated, {"source": str(source), "file_count": len(entries), "sha256": digest(entries)}


def listed_tree(root: Path, head: str) -> list[tuple[str, str, str]]:
    raw = git(root, "ls-tree", "-r", "-z", head)
    entries: list[tuple[str, str, str]] = []
    for record in raw.split("\0"):
        if not record:
            continue
        metadata, name = record.split("\t", 1)
        mode, kind, oid = metadata.split()
        if kind != "blob":
            continue
        entries.append((mode, oid, name))
    return entries


def copied_allowed(name: str, policy: dict[str, Any]) -> bool:
    if not any(name == prefix or name.startswith(prefix + "/") for prefix in policy["copied_metadata"]):
        return False
    if name.startswith(".juno_task/specs/") and policy["spec_copy_mode"] == "top_level_files_only":
        return "/" not in name.removeprefix(".juno_task/specs/")
    return True


def selected_entries(root: Path, head: str, policy: dict[str, Any]) -> list[tuple[str, str, str]]:
    selected = []
    for mode, oid, name in listed_tree(root, head):
        if copied_allowed(name, policy):
            if mode not in {"100644", "100755"}:
                raise BoundaryError(f"metadata may not contain symlinks or gitlinks: {name}")
            if name.startswith(".juno_task/wiki/"):
                relative = PurePosixPath(name.removeprefix(".juno_task/wiki/"))
                if relative.suffix.lower() != ".md":
                    raise BoundaryError(f"controller wiki migration accepts Markdown files only: {name}")
                if relative.parts and relative.parts[0] == "controller":
                    raise BoundaryError(
                        f"legacy wiki collides with the package-owned controller namespace: {name}"
                    )
            selected.append((mode, oid, name))
    required = (".juno_task/tasks", ".juno_task/ledger", ".juno_task/specs")
    for prefix in required:
        if not any(name == prefix or name.startswith(prefix + "/") for _, _, name in selected):
            raise BoundaryError(f"source controller is missing canonical metadata: {prefix}")
    return selected


def inventory(old: Path, old_head: str, product_head: str, policy: dict[str, Any], runtime: dict[str, str]) -> dict[str, Any]:
    entries = listed_tree(old, old_head)
    names = [name for _, _, name in entries]
    selected = [name for name in names if copied_allowed(name, policy)]
    categories = {
        "controller_data": [name for name in selected if name.startswith((".juno_task/tasks", ".juno_task/ledger"))],
        "task_specs": [name for name in selected if name.startswith(".juno_task/specs")],
        "minimal_project_configuration": [name for name in names if name in {".juno_task/config.json", ".gitignore"} or name.startswith(".juno_task/config/")],
        "product_documentation": [name for name in names if name == "README.md" or name.startswith(("docs/", "yylo/docs/", "frontend/"))],
        "historical_evidence": [name for name in names if name.startswith((".juno_task/workflows", ".juno_task/artifacts", ".juno_task/logs"))
                                or (name.startswith(".juno_task/specs/") and not copied_allowed(name, policy))],
    }
    ignored = run(["git", "-C", str(old), "status", "--ignored", "--porcelain=v1", "-z"], old, False).stdout.split("\0")
    ignored_names = sorted(item[3:] for item in ignored if item.startswith("!! "))
    return {"old_controller_head": old_head, "product_head": product_head, "tracked_count": len(names),
            "copied_count": len(selected), "categories": {key: {"count": len(paths), "paths": paths} for key, paths in categories.items()},
            "installed_runtime": runtime, "local_ignored": ignored_names,
            "excluded_from_metadata_branch_count": len(set(names) - set(selected))}


def normalize_migrated_prompt_macros(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BoundaryError("reviewed promptMacros migration requires an object")
    known = {"enabled", "order", "maxDepth", "global", "local"}
    profile = json.loads(json.dumps(value)) if set(value).issubset(known) else {"global": json.loads(json.dumps(value))}
    for dictionary_name in ("global", "local"):
        dictionary = profile.get(dictionary_name)
        if dictionary is None:
            continue
        if not isinstance(dictionary, dict):
            raise BoundaryError(f"promptMacros.{dictionary_name} must be an object")
        for name, raw in list(dictionary.items()):
            if isinstance(raw, dict) and isinstance(raw.get("path"), str):
                prompt_path = PurePosixPath(raw["path"])
                prefix = PurePosixPath(".juno_task/prompts")
                try:
                    relative = prompt_path.relative_to(prefix)
                except ValueError:
                    relative = prompt_path
                if relative.is_absolute() or ".." in relative.parts:
                    raise BoundaryError(f"prompt macro {name} escapes the reviewed prompt asset root")
                dictionary[name] = {"path": relative.as_posix()}
            elif isinstance(raw, str) and raw.startswith(".juno_task/prompts/"):
                dictionary[name] = {"path": raw.removeprefix(".juno_task/prompts/")}
    return profile


def reviewed_agent_migration(old: Path, old_head: str, policy_bundle: Path | None) -> dict[str, Any]:
    if policy_bundle is None:
        return {"configured": False, "controller_config": CANONICAL_CONTROLLER_CONFIG,
                "historical_plan": None, "compact_plan": None, "prompt_assets": []}
    bundle_path = reviewed_path(policy_bundle, "policy bundle")
    bundle = read_json(bundle_path, "policy bundle")
    migration = bundle.get("agent_migration")
    if not isinstance(migration, dict) or not isinstance(migration.get("source"), dict):
        raise BoundaryError("reviewed policy bundle lacks agent migration source identity")
    source = migration["source"]
    if source.get("source_head") != old_head:
        raise BoundaryError("agent migration inventory does not bind the frozen old controller head")
    config_record = source.get("config")
    dispositions = migration.get("field_dispositions")
    if not isinstance(config_record, dict) or not isinstance(dispositions, dict):
        raise BoundaryError("agent migration config identity or dispositions are missing")
    if config_record.get("present") is not True:
        if dispositions:
            raise BoundaryError("absent legacy config cannot have field dispositions")
        config_bytes = canonical({})
        legacy: dict[str, Any] = {}
    else:
        config_bytes = committed_bytes(old, old_head, CONFIG_PATH)
        if (config_record.get("sha256") != bytes_digest(config_bytes)
                or config_record.get("size") != len(config_bytes)):
            raise BoundaryError("legacy config bytes differ from the reviewed inventory")
        try:
            parsed_legacy = json.loads(config_bytes)
        except json.JSONDecodeError as exc:
            raise BoundaryError(f"legacy config is invalid: {exc}") from exc
        if not isinstance(parsed_legacy, dict):
            raise BoundaryError("legacy config must be an object")
        legacy = parsed_legacy
    if set(dispositions) != set(legacy):
        raise BoundaryError("every legacy config field requires exactly one reviewed disposition")
    if any(value not in AGENT_MIGRATION_DISPOSITIONS for value in dispositions.values()):
        raise BoundaryError("agent migration contains an unsupported field disposition")
    migrated: dict[str, Any] = {"controllerWorkspace": CANONICAL_CONTROLLER_WORKSPACE}
    for name, disposition in sorted(dispositions.items()):
        if disposition == "migrate":
            if name not in CONTROLLER_SAFE_CONFIG_FIELDS:
                raise BoundaryError(f"unsafe config field cannot be migrated into controller: {name}")
            migrated[name] = legacy[name]
        elif name == "promptMacros" and disposition == "transform":
            migrated[name] = normalize_migrated_prompt_macros(legacy[name])
        elif name == "hooks" and disposition == "product-only":
            if not isinstance(legacy[name], dict):
                raise BoundaryError("legacy product hooks must be an object")
            migrated.setdefault("agentProfile", {
                "version": 1, "promptAssetRoot": ".juno_task/prompts"})
            migrated["agentProfile"]["roleHooks"] = {"product": legacy[name]}
        elif name == "controllerWorkspace" and disposition != "transform":
            raise BoundaryError("controllerWorkspace must be transformed to metadata-only")
    prompt_assets = source.get("prompt_assets", [])
    if prompt_assets:
        migrated.setdefault("agentProfile", {"version": 1, "promptAssetRoot": ".juno_task/prompts"})
    plan_record = source.get("plan", {})
    historical_plan = compact_plan = None
    if plan_record.get("present"):
        plan_bytes = committed_bytes(old, old_head, ".juno_task/plan.md")
        if (plan_record.get("sha256") != bytes_digest(plan_bytes)
                or plan_record.get("size") != len(plan_bytes)):
            raise BoundaryError("legacy plan.md bytes differ from the reviewed inventory")
        evidence_name = f".juno_task/specs/legacy-plan-{bytes_digest(plan_bytes)[:16]}.md"
        historical_plan = {"path": evidence_name,
                           "bytes_base64": base64.b64encode(plan_bytes).decode(),
                           "sha256": bytes_digest(plan_bytes)}
        compact_bytes = ("# Current work\n\nThe immutable Juno v2 plan is preserved at "
                         f"`{evidence_name}` (SHA-256 `{bytes_digest(plan_bytes)}`).\n"
                         "Use Kanban and current task specs for active execution.\n").encode()
        compact_plan = {"bytes_base64": base64.b64encode(compact_bytes).decode(),
                        "sha256": bytes_digest(compact_bytes)}
    if source.get("environment_sources") and migration.get("environment_binding_authorized") is not True:
        # Presence is preserved diagnostically, but secret bytes are never copied or activated.
        pass
    return {"configured": True, "controller_config": migrated,
            "controller_config_sha256": bytes_digest(canonical(migrated)),
            "field_dispositions": dispositions, "source": source,
            "historical_plan": historical_plan, "compact_plan": compact_plan,
            "prompt_assets": prompt_assets,
            "environment_binding_authorized": migration.get("environment_binding_authorized") is True}


def migration_plan(args: argparse.Namespace, policy: dict[str, Any]) -> dict[str, Any]:
    old = exact_worktree(args.old_controller)
    if git(old, "status", "--porcelain=v2", "--untracked-files=all"):
        raise BoundaryError("old controller must be clean before inventory freeze")
    if git(old, "symbolic-ref", "-q", "HEAD", check=False) != args.old_branch:
        raise BoundaryError("old controller worktree is not attached to the exact rollback branch")
    if args.new_branch != policy["controller_branch"] or args.product_ref != policy["product_ref"]:
        raise BoundaryError("requested controller/product refs do not match the reviewed policy")
    old_head = resolve_commit(old, args.old_branch, args.expected_old_head, "old controller ref")
    product_head = resolve_commit(old, args.product_ref, args.expected_product_head, "product target")
    if ref_exists(old, args.new_branch):
        raise BoundaryError("fresh metadata controller branch already exists")
    if args.new_controller.expanduser().resolve().exists():
        raise BoundaryError("fresh metadata controller path already exists")
    runtime = runtime_identity(args.runtime, args.runtime_version, old)
    reviewed_policies = reviewed_policies_from_sources(
        metadata_policy=policy, policy_bundle=args.policy_bundle,
        task_policy=args.task_workspace_policy, integration_policy=args.integration_workspace_policy,
        risk_policy=args.risk_policy)
    agent_migration = reviewed_agent_migration(old, old_head, args.policy_bundle)
    selected_entries(old, old_head, policy)
    payload = {"schema_version": PLAN_SCHEMA, "operation": "migration-plan", "outcome": "planned_no_mutation",
               "old_controller": str(old), "old_branch": args.old_branch, "old_head": old_head,
               "new_controller": str(args.new_controller.expanduser().resolve()), "new_branch": args.new_branch,
               "product_ref": args.product_ref, "product_head": product_head, "git_common_dir": common_dir(old),
               "runtime": runtime, "policy_sha256": digest(policy), "reviewed_policies": reviewed_policies,
               "inventory": inventory(old, old_head, product_head, policy, runtime),
               "agent_migration": agent_migration,
               "cutover_authorized": False, "rollback": {"controller": str(old), "branch": args.old_branch, "head": old_head},
               "steps": ["freeze old controller identity", "prepare unrelated metadata-only root and worktree",
                         "install generated local runtime", "run canaries", "request owner-authorized registration cutover"],
               "forbidden": ["move product target", "switch live registration", "delete rollback controller", "push", "release"]}
    atomic_receipt(args.output, payload)
    return payload


def read_plan(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise BoundaryError(f"invalid migration plan: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != PLAN_SCHEMA or value.get("outcome") != "planned_no_mutation":
        raise BoundaryError("prepare requires a canonical no-mutation migration plan")
    return value


def add_blob(root: Path, index_env: dict[str, str], path: str, data: bytes, mode: str = "100644") -> None:
    result = subprocess.run(["git", "-C", str(root), "hash-object", "-w", "--stdin"], input=data,
                            capture_output=True, env={**os.environ, **index_env})
    if result.returncode:
        raise BoundaryError(result.stderr.decode().strip())
    oid = result.stdout.decode().strip()
    git(root, "update-index", "--add", "--cacheinfo", f"{mode},{oid},{path}", env=index_env)


def prepare(args: argparse.Namespace, policy: dict[str, Any]) -> dict[str, Any]:
    plan_path = args.plan.resolve()
    plan = read_plan(plan_path)
    output_path = args.output.expanduser().resolve()
    if output_path.exists():
        prior = read_json(output_path, "prepare receipt")
        destination = Path(plan["new_controller"])
        if (prior.get("schema_version") != RECEIPT_SCHEMA or prior.get("operation") != "prepare"
                or prior.get("outcome") != "prepared_not_registered"
                or prior.get("plan_sha256") != file_digest(plan_path)
                or prior.get("new_controller") != str(destination)
                or not destination.is_dir()
                or git(destination, "rev-parse", "HEAD", check=False) != prior.get("new_head")):
            raise BoundaryError("existing prepare receipt is not an exact completed retry")
        evidence = inspect(destination, policy, expected_branch=plan["new_branch"], require_active=False)
        if not evidence["passed"]:
            raise BoundaryError("existing prepare receipt destination no longer verifies")
        return prior
    old = exact_worktree(Path(plan["old_controller"]))
    resolve_commit(old, plan["old_branch"], plan["old_head"], "old controller ref")
    resolve_commit(old, plan["product_ref"], plan["product_head"], "product target")
    if plan["policy_sha256"] != digest(policy):
        raise BoundaryError("metadata policy changed after planning")
    reviewed_policies = revalidate_planned_policies(plan, policy)
    policy_source = reviewed_policies.get("source", {})
    planned_agent = reviewed_agent_migration(
        old, plan["old_head"],
        Path(policy_source["path"]) if policy_source.get("kind") == "policy_bundle" else None)
    if digest(planned_agent) != digest(plan.get("agent_migration")):
        raise BoundaryError("derived agent migration changed after planning")
    runtime_identity(Path(plan["runtime"]["executable"]), plan["runtime"]["version"], old)
    destination = Path(plan["new_controller"])
    if plan["new_branch"] != policy["controller_branch"] or plan["product_ref"] != policy["product_ref"]:
        raise BoundaryError("migration plan refs no longer match the reviewed policy")
    if destination.exists() or ref_exists(old, plan["new_branch"]):
        raise BoundaryError("metadata destination or branch is no longer fresh")
    entries = selected_entries(old, plan["old_head"], policy)
    transformed_paths = {CONFIG_PATH}
    if plan["agent_migration"].get("compact_plan"):
        transformed_paths.add(".juno_task/plan.md")
    preserved_entries = [{"mode": mode, "oid": oid, "path": name}
                         for mode, oid, name in entries if name not in transformed_paths]
    boundary = {"schema_version": RECEIPT_SCHEMA, "operation": "controller-boundary", "source_head": plan["old_head"],
                "product_ref": plan["product_ref"], "product_head_at_plan": plan["product_head"],
                "runtime": {"package": "@yylo/cli", "version": plan["runtime"]["version"]},
                "policy_sha256": plan["policy_sha256"],
                "reviewed_policy_sha256": {
                    "task_workspace": reviewed_policies["task_workspace"]["sha256"],
                    "integration_workspace": reviewed_policies["integration_workspace"]["sha256"],
                    "risk": reviewed_policies["risk"]["sha256"],
                }, "controller_commits_integrate_to_product": False,
                "preserved_metadata": {"entries": preserved_entries, "sha256": digest(preserved_entries)},
                "agent_migration": {
                    "source_head": plan["agent_migration"].get("source", {}).get("source_head"),
                    "config_sha256": plan["agent_migration"].get("source", {}).get("config", {}).get("sha256"),
                    "plan_sha256": plan["agent_migration"].get("source", {}).get("plan", {}).get("sha256"),
                    "field_dispositions_sha256": digest(plan["agent_migration"].get("field_dispositions", {})),
                }}
    task_policy_bytes = canonical(reviewed_policies["task_workspace"]["content"])
    integration_policy_bytes = canonical(reviewed_policies["integration_workspace"]["content"])
    risk_policy_bytes = canonical(reviewed_policies["risk"]["content"])
    generated = {
        ".gitignore": b".env.yylo\n.venv_juno/\n.juno_task/runtime/\n.juno_task/scripts/\n.juno_task/tmp/\n.juno_task/cache/\n.juno_task/locks/\n.juno_task/transactions/\n/AGENTS.md\n/CLAUDE.md\n/.agents/\n/.claude/\n/.pi/\n*.log\n__pycache__/\n",
        CONFIG_PATH: canonical(plan["agent_migration"]["controller_config"]),
        ".juno_task/config/metadata-controller.json": canonical(policy),
        ".juno_task/config/task-workspace.json": task_policy_bytes,
        ".juno_task/config/integration-workspace.json": integration_policy_bytes,
        ".juno_task/config/risk-policy.json": risk_policy_bytes,
        ".juno_task/receipts/controller-boundary.json": canonical(boundary),
        ".juno_task/state/tasks.json": canonical({"schema_version": "juno_task_workspace_state.v1", "tasks": {}, "queues": {}}),
    }
    historical_plan = plan["agent_migration"].get("historical_plan")
    compact_plan = plan["agent_migration"].get("compact_plan")
    if historical_plan:
        historical_bytes = base64.b64decode(historical_plan["bytes_base64"], validate=True)
        if bytes_digest(historical_bytes) != historical_plan["sha256"]:
            raise BoundaryError("historical plan evidence bytes do not match the migration plan")
        generated[historical_plan["path"]] = historical_bytes
    if compact_plan:
        compact_bytes = base64.b64decode(compact_plan["bytes_base64"], validate=True)
        if bytes_digest(compact_bytes) != compact_plan["sha256"]:
            raise BoundaryError("compact plan bytes do not match the migration plan")
        generated[".juno_task/plan.md"] = compact_bytes
    controller_wiki, controller_wiki_evidence = packaged_controller_wiki(
        Path(plan["runtime"]["executable"]))
    generated.update(controller_wiki)
    with tempfile.TemporaryDirectory(prefix="juno-metadata-index-") as temporary:
        index_env = {"GIT_INDEX_FILE": str(Path(temporary) / "index")}
        git(old, "read-tree", "--empty", env=index_env)
        for mode, oid, name in entries:
            git(old, "update-index", "--add", "--cacheinfo", f"{mode},{oid},{name}", env=index_env)
        for name, data in generated.items():
            add_blob(old, index_env, name, data)
        tree = git(old, "write-tree", env=index_env)
    identity = {"package": "@yylo/cli", "version": plan["runtime"]["version"], "executable": plan["runtime"]["executable"],
                "executable_sha256": plan["runtime"]["executable_sha256"], "source": "installed-release", "tracked": False}
    commit_env = {"GIT_AUTHOR_NAME": "Juno Controller Migration", "GIT_AUTHOR_EMAIL": "juno-controller@local.invalid",
                  "GIT_COMMITTER_NAME": "Juno Controller Migration", "GIT_COMMITTER_EMAIL": "juno-controller@local.invalid"}
    commit = run(["git", "-C", str(old), "commit-tree", tree, "-m", "Initialize metadata-only controller"],
                 old, env=commit_env).stdout.strip()
    created = False
    try:
        branch_name = plan["new_branch"].removeprefix("refs/heads/")
        run(["git", "-C", str(old), "worktree", "add", "-b", branch_name, str(destination), commit], old)
        created = True
        # The repository may still carry the retired full-tree controller's
        # sparse-checkout setting.  This branch is already metadata-only, so
        # materialize its complete small tree and remove that hidden state.
        git(destination, "sparse-checkout", "disable")
        git(destination, "config", "extensions.worktreeConfig", "true")
        git(destination, "config", "--worktree", "juno.workspace.role", "controller-pending")
        git(destination, "config", "--worktree", "juno.controller.mode", "metadata-only")
        git(destination, "config", "--worktree", "juno.controller.runtimeVersion", identity["version"])
        git(destination, "config", "--worktree", "juno.controller.runtimeExecutable", identity["executable"])
        runtime_file = destination / policy["runtime"]["identity_file"]
        runtime_file.parent.mkdir(parents=True, exist_ok=True)
        runtime_file.write_bytes(canonical(identity))
        runtime_scripts = install_runtime_scripts(Path(identity["executable"]), destination)
        evidence = inspect(destination, policy, expected_branch=plan["new_branch"], require_active=False)
        if not evidence["passed"]:
            raise BoundaryError(f"prepared metadata controller failed verification: {evidence['checks']}")
    except Exception:
        if created:
            run(["git", "-C", str(old), "worktree", "remove", "--force", str(destination)], old, False)
            run(["git", "-C", str(old), "branch", "-D", plan["new_branch"].removeprefix("refs/heads/")], old, False)
        raise
    payload = {"schema_version": RECEIPT_SCHEMA, "operation": "prepare", "outcome": "prepared_not_registered",
               "plan_sha256": hashlib.sha256(args.plan.resolve().read_bytes()).hexdigest(), "new_controller": str(destination),
               "new_branch": plan["new_branch"], "new_head": commit, "new_tree": tree, "root_commit": True,
               "old_controller_preserved": True, "product_head": plan["product_head"], "evidence": evidence,
               "runtime_scripts": runtime_scripts,
               "controller_wiki": controller_wiki_evidence,
               "cutover_authorized": False}
    atomic_receipt(args.output, payload)
    return payload


def policy_path_decision(name: str, policy: dict[str, Any], *, container: bool = False,
                         historical_attribution: dict[str, str] | None = None) -> dict[str, Any]:
    """Authoritative controller/product classifier used by every policy consumer."""
    if name in policy["tracked_exact"]:
        return {"allowed": True, "reason": "tracked_exact", "rule": f"tracked_exact:{name}"}
    for root in AGENT_SURFACE_ROOTS:
        if root in policy["runtime"]["ignored_roots"] and (name == root or name.startswith(root + "/")):
            return {"allowed": True, "reason": "managed_agent_surface",
                    "rule": f"runtime:ignored_managed_agent_surface:{root}"}
    for root in LEGACY_MANAGED_CONTROLLER_METADATA:
        if name == root or name.startswith(root + "/"):
            return {"allowed": True, "reason": "legacy_managed_controller_metadata",
                    "rule": f"compatibility:legacy_managed_controller_metadata:{root}"}
    if container:
        tracked_roots = (policy["tracked_exact"] + policy["tracked_recursive"]
                         + policy["tracked_top_level_files"])
        descendants = sorted(root for root in tracked_roots if root.startswith(name + "/"))
        if descendants:
            return {"allowed": True, "reason": "tracked_container",
                    "rule": f"metadata_controller:tracked_descendant_container:{descendants[0]}"}
    for root in policy["tracked_recursive"]:
        if name == root or name.startswith(root + "/"):
            return {"allowed": True, "reason": "tracked_recursive", "rule": f"tracked_recursive:{root}"}
    for root in policy["tracked_top_level_files"]:
        if name == root:
            return {"allowed": container, "reason": "container" if container else "container_not_file",
                    "rule": f"tracked_top_level_files:{root}:direct_children_only"}
        if name.startswith(root + "/"):
            nested = "/" in name.removeprefix(root + "/")
            if not nested:
                return {"allowed": True, "reason": "tracked_top_level_file",
                        "rule": f"tracked_top_level_files:{root}:direct_children_only"}
            if historical_attribution is not None:
                return {"allowed": True, "reason": "historical_reference_bound_artifact",
                        "rule": f"tracked_top_level_files:{root}:historical_reference_bound_artifact",
                        **historical_attribution}
            return {"allowed": False, "reason": "unattributed_nested_path",
                    "rule": f"tracked_top_level_files:{root}:direct_children_only"}
    for root in LEGACY_OPERATIONAL_METADATA:
        if name == root or name.startswith(root + "/"):
            return {"allowed": True, "reason": "legacy_operational_metadata",
                    "rule": f"legacy_operational_metadata:{root}"}
    return {"allowed": False, "reason": "outside_controller_boundary",
            "rule": "metadata_controller:tracked_path_classes"}


def policy_path_allowed(name: str, policy: dict[str, Any], *, container: bool = False) -> bool:
    return bool(policy_path_decision(name, policy, container=container)["allowed"])


def tracked_allowed(name: str, policy: dict[str, Any]) -> bool:
    return bool(policy_path_decision(name, policy)["allowed"])


def exact_text_reference(text: str, value: str) -> bool:
    path_character = r"A-Za-z0-9._/-"
    return re.search(rf"(?<![{path_character}]){re.escape(value)}(?![{path_character}])", text) is not None


def historical_nested_attributions(root: Path, head: str,
                                   entries: list[tuple[str, str, str]],
                                   policy: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Authenticate legacy nested evidence without moving or rewriting it.

    A nested specs artifact is grandfathered only when its current blob was
    committed atomically with a canonical task/ledger reference. A sibling
    report may bind another same-directory artifact by exact filename. This is
    intentionally history-only: dirty nested files cannot self-attest.
    """
    candidates = {(mode, oid, name) for mode, oid, name in entries
                  if name.startswith(".juno_task/specs/")
                  and "/artifacts/" in name
                  and not policy_path_allowed(name, policy)
                  and mode in {"100644", "100755"}}
    by_commit: dict[str, list[tuple[str, str, str]]] = {}
    for entry in candidates:
        commit = git(root, "log", "-1", "--format=%H", head, "--", entry[2], check=False)
        if commit and git(root, "rev-parse", f"{commit}:{entry[2]}", check=False) == entry[1]:
            by_commit.setdefault(commit, []).append(entry)
    attributed: dict[str, dict[str, str]] = {}
    for commit, group in by_commit.items():
        changed = set(filter(None, git(root, "diff-tree", "--root", "--no-commit-id",
                                      "--name-only", "-r", commit, check=False).splitlines()))
        if any(name not in changed for _, _, name in group):
            continue
        canonical = sorted(name for name in changed
                           if name.startswith((".juno_task/tasks/", ".juno_task/ledger/")))
        canonical_text: dict[str, str] = {
            name: run(["git", "-C", str(root), "show", f"{commit}:{name}"], root, False).stdout
            for name in canonical
        }
        pending = {name: (mode, oid) for mode, oid, name in group}
        references: dict[str, str] = {}
        for name in sorted(pending):
            owner = next((path for path, text in canonical_text.items()
                          if exact_text_reference(text, name)), None)
            if owner:
                references[name] = owner
        progress = True
        while progress:
            progress = False
            for name in sorted(set(pending) - set(references)):
                owner = next((other for other in sorted(references)
                              if PurePosixPath(other).parent == PurePosixPath(name).parent
                              and exact_text_reference(run(
                                  ["git", "-C", str(root), "show", f"{commit}:{other}"],
                                  root, False).stdout, PurePosixPath(name).name)), None)
                if owner:
                    references[name] = owner
                    progress = True
        for name, reference in references.items():
            attributed[name] = {"attribution_commit": commit, "attribution_reference": reference}
    return attributed


def agent_surface_path(name: str) -> bool:
    return any(name == root or name.startswith(root + "/") for root in AGENT_SURFACE_ROOTS)


def committed_gitignore(root: Path, head: str) -> tuple[list[str], list[str]]:
    result = run(["git", "-C", str(root), "show", f"{head}:.gitignore"], root, False)
    if result.returncode:
        return [], list(REQUIRED_ROOT_IGNORES)
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    return lines, [entry for entry in REQUIRED_ROOT_IGNORES if entry not in lines]


def product_boundary(root: Path, product_ref: str, expected_head: str, policy: dict[str, Any]) -> dict[str, Any]:
    root = exact_worktree(root)
    if product_ref != policy["product_ref"]:
        raise BoundaryError("product ref does not match the reviewed policy")
    head = resolve_commit(root, product_ref, expected_head, "product target")
    names = [name for _, _, name in listed_tree(root, head)]
    forbidden = [name for name in names if any(
        name == prefix or name.startswith(prefix + "/")
        for prefix in (*policy["product_forbidden"], *LEGACY_OPERATIONAL_METADATA)
    )]
    return {"product_ref": product_ref, "product_head": head, "forbidden_controller_paths": forbidden,
            "passed": not forbidden}


def inspect(root: Path, policy: dict[str, Any], *, expected_branch: str | None = None, require_active: bool = True) -> dict[str, Any]:
    root = exact_worktree(root)
    branch = git(root, "symbolic-ref", "-q", "HEAD", check=False) or None
    head = git(root, "rev-parse", "HEAD")
    ancestry_roots = git(root, "rev-list", "--max-parents=0", head).splitlines()
    current_entries = listed_tree(root, head)
    names = [name for _, _, name in current_entries]
    unsafe_modes = [name for mode, _, name in current_entries if mode not in {"100644", "100755"}]
    historical = historical_nested_attributions(root, head, current_entries, policy)
    path_decisions = {name: policy_path_decision(name, policy,
                                                 historical_attribution=historical.get(name))
                      for name in names}
    forbidden_details = [{"path": name, "reason": decision["reason"], "rule": decision["rule"]}
                         for name, decision in path_decisions.items() if not decision["allowed"]]
    forbidden = [item["path"] for item in forbidden_details]
    root_entries = listed_tree(root, ancestry_roots[0]) if len(ancestry_roots) == 1 else []
    root_names = [name for _, _, name in root_entries]
    root_historical = historical_nested_attributions(root, ancestry_roots[0], root_entries, policy) if len(ancestry_roots) == 1 else {}
    forbidden_root_details = []
    for name in root_names:
        decision = policy_path_decision(name, policy, historical_attribution=root_historical.get(name))
        if not decision["allowed"]:
            forbidden_root_details.append({"path": name, "reason": decision["reason"], "rule": decision["rule"]})
    forbidden_root = [item["path"] for item in forbidden_root_details]
    root_entry_map = {name: {"mode": mode, "oid": oid} for mode, oid, name in listed_tree(root, ancestry_roots[0])} if len(ancestry_roots) == 1 else {}
    boundary_path = ".juno_task/receipts/controller-boundary.json"
    root_boundary_text = run(["git", "-C", str(root), "show", f"{ancestry_roots[0]}:{boundary_path}"], root, False).stdout if len(ancestry_roots) == 1 else ""
    current_boundary_text = run(["git", "-C", str(root), "show", f"{head}:{boundary_path}"], root, False).stdout
    preservation_receipt_ok = False
    preservation_missing: list[str] = []
    reviewed_policy_hashes: dict[str, str] = {}
    try:
        boundary = json.loads(root_boundary_text)
        if boundary.get("schema_version") != RECEIPT_SCHEMA or boundary.get("operation") != "controller-boundary":
            raise ValueError("invalid boundary receipt identity")
        preservation = boundary["preserved_metadata"]
        expected_entries = preservation["entries"]
        if not isinstance(expected_entries, list) or preservation["sha256"] != digest(expected_entries):
            raise ValueError("invalid preserved metadata digest")
        reviewed_policy_hashes = boundary["reviewed_policy_sha256"]
        if (set(reviewed_policy_hashes) != {"task_workspace", "integration_workspace", "risk"}
                or any(not re.fullmatch(r"[0-9a-f]{64}", value)
                       for value in reviewed_policy_hashes.values())):
            raise ValueError("invalid reviewed policy identity")
        for entry in expected_entries:
            if set(entry) != {"mode", "oid", "path"} or root_entry_map.get(entry["path"]) != {"mode": entry["mode"], "oid": entry["oid"]}:
                preservation_missing.append(entry.get("path", "<invalid>"))
        preservation_receipt_ok = not preservation_missing
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        preservation_receipt_ok = False
    canonical_prefixes = (".juno_task/tasks", ".juno_task/ledger", ".juno_task/specs")
    missing_canonical = [prefix for prefix in canonical_prefixes if not any(name.startswith(prefix + "/") for name in names)]
    missing_generated = [name for name in policy["generated_metadata"] if name not in names]
    generated_contract_ok = False
    agent_profile_diagnostic = {"status": "missing", "findings": ["agentProfile is absent; package defaults remain active"],
                                "next_command": "yy migrate inventory --help"}
    if not missing_generated:
        try:
            config_value = json.loads(run(["git", "-C", str(root), "show", f"{head}:.juno_task/config.json"], root).stdout)
            policy_text = run(["git", "-C", str(root), "show", f"{head}:.juno_task/config/metadata-controller.json"], root).stdout
            task_policy_text = run(["git", "-C", str(root), "show", f"{head}:.juno_task/config/task-workspace.json"], root).stdout
            integration_policy_text = run(["git", "-C", str(root), "show", f"{head}:.juno_task/config/integration-workspace.json"], root).stdout
            risk_policy_text = run(["git", "-C", str(root), "show", f"{head}:.juno_task/config/risk-policy.json"], root).stdout
            task_policy_value = validate_task_policy(json.loads(task_policy_text))
            integration_policy_value = validate_integration_policy(
                json.loads(integration_policy_text), task_policy_value)
            risk_policy_value = validate_risk_policy(json.loads(risk_policy_text))
            tasks_value = json.loads(run(["git", "-C", str(root), "show", f"{head}:.juno_task/state/tasks.json"], root).stdout)
            profile = config_value.get("agentProfile") if isinstance(config_value, dict) else None
            profile_valid = profile is None or (
                isinstance(profile, dict)
                and set(profile).issubset({"version", "promptAssetRoot", "roleHooks", "environmentBinding"})
                and profile.get("version") == 1
                and profile.get("promptAssetRoot") == ".juno_task/prompts")
            unresolved_assets: list[str] = []
            profile_findings: list[str] = []
            if isinstance(config_value, dict) and profile is not None:
                macros = config_value.get("promptMacros", {})
                for dictionary_name in ("global", "local"):
                    dictionary = macros.get(dictionary_name, {}) if isinstance(macros, dict) else {}
                    if isinstance(dictionary, dict):
                        for macro_name, raw in dictionary.items():
                            if isinstance(raw, dict) and isinstance(raw.get("path"), str):
                                asset = PurePosixPath(".juno_task/prompts") / raw["path"]
                                if ".." in asset.parts or asset.as_posix() not in names:
                                    unresolved_assets.append(f"{dictionary_name}.{macro_name}")
                if unresolved_assets:
                    profile_findings.append("unresolved prompt assets: " + ", ".join(unresolved_assets))
                role_hooks = profile.get("roleHooks") if isinstance(profile, dict) else None
                if role_hooks is not None and (not isinstance(role_hooks, dict)
                        or not set(role_hooks).issubset({"controller", "product"})):
                    profile_valid = False
                    profile_findings.append("unsafe or unowned roleHooks configuration")
                environment = profile.get("environmentBinding") if isinstance(profile, dict) else None
                if environment is not None:
                    source = Path(environment.get("source", "")) if isinstance(environment, dict) else Path("")
                    try:
                        mode = source.lstat().st_mode
                        environment_valid = (isinstance(environment, dict)
                                             and environment.get("authorized") is True
                                             and source.is_absolute() and source.is_file()
                                             and not source.is_symlink() and mode & 0o077 == 0)
                    except OSError:
                        environment_valid = False
                    if not environment_valid:
                        profile_valid = False
                        profile_findings.append("authorized environment source is missing, unsafe, or not mode 0600")
                agent_profile_diagnostic = {
                    "status": "valid" if profile_valid and not unresolved_assets else "invalid",
                    "findings": profile_findings,
                    "source_identity": boundary.get("agent_migration") if isinstance(boundary, dict) else None,
                    "next_command": None if profile_valid and not unresolved_assets else "yy migrate inventory --help"}
            generated_contract_ok = (
                isinstance(config_value, dict)
                and profile_valid and not unresolved_assets
                and "lifecycle" not in config_value
                and config_value.get("controllerWorkspace") == CANONICAL_CONTROLLER_WORKSPACE
                and policy_text == canonical(policy).decode()
                and task_policy_text == canonical(task_policy_value).decode()
                and integration_policy_text == canonical(integration_policy_value).decode()
                and risk_policy_text == canonical(risk_policy_value).decode()
                and digest(task_policy_value) == reviewed_policy_hashes.get("task_workspace")
                and digest(integration_policy_value) == reviewed_policy_hashes.get("integration_workspace")
                and digest(risk_policy_value) == reviewed_policy_hashes.get("risk")
                and tasks_value.get("schema_version") in {
                    "juno_task_workspace_state.v1", "juno_task_workspace_state.v2"}
                and isinstance(tasks_value.get("tasks"), dict)
                and isinstance(tasks_value.get("queues"), dict)
                and (tasks_value.get("schema_version") != "juno_task_workspace_state.v2"
                     or all(not isinstance(record, dict)
                            or record.get("state") not in {"MERGED", "WITHDRAWN"}
                            or (record.get("schema_version") == "juno_task_terminal_tombstone.v1"
                                and record.get("task_id") == task_id)
                            for task_id, record in tasks_value["tasks"].items()))
                and current_boundary_text == root_boundary_text
            )
        except (BoundaryError, KeyError, TypeError, json.JSONDecodeError):
            generated_contract_ok = False
    product_markers = [name for name in names if name == "README.md" or name.startswith(("yylo/", "juno_kanban/", "frontend/", "scripts/", ".github/"))]
    tracked_agent_surface = sorted(name for name in names if agent_surface_path(name))
    gitignore_lines, missing_root_ignores = committed_gitignore(root, head)
    staged = git(root, "diff", "--cached", "--name-only", check=False).splitlines()
    role = git(root, "config", "--worktree", "--get", "juno.workspace.role", check=False)
    runtime_version = git(root, "config", "--worktree", "--get", "juno.controller.runtimeVersion", check=False)
    runtime_executable = git(root, "config", "--worktree", "--get", "juno.controller.runtimeExecutable", check=False)
    runtime_file = root / policy["runtime"]["identity_file"]
    runtime_ok = False
    if runtime_file.is_file():
        try:
            identity = json.loads(runtime_file.read_text())
            checked = runtime_identity(Path(runtime_executable), runtime_version, root)
            runtime_ok = identity == {**checked, "source": "installed-release", "tracked": False}
        except (BoundaryError, OSError, json.JSONDecodeError):
            runtime_ok = False
    controller_wiki_core = all(
        f".juno_task/wiki/controller/{name}" in names for name in CORE_CONTROLLER_WIKI)
    checks = {"branch_exact": expected_branch is None or branch == expected_branch, "single_root_ancestry": len(ancestry_roots) == 1,
              "root_boundary": not [name for name in forbidden_root if not agent_surface_path(name)],
              "root_preservation": preservation_receipt_ok,
              "canonical_metadata_present": not missing_canonical,
              "required_generated_present": not missing_generated,
              "generated_contract": generated_contract_ok,
              "gitignore_materialized": (root / ".gitignore").is_file(),
              "root_agent_ignores": not missing_root_ignores,
              "agent_surface_untracked": not tracked_agent_surface,
              "tracked_boundary": not forbidden, "product_absent": not product_markers,
              "regular_files_only": not unsafe_modes,
              "staged_boundary": all(tracked_allowed(name, policy) for name in staged),
              "runtime_bound": runtime_ok, "runtime_untracked": policy["runtime"]["identity_file"] not in names,
              "controller_wiki_core": controller_wiki_core,
              "role": role == ("controller" if require_active else "controller-pending"),
              "clean": git(root, "status", "--porcelain=v2", "--untracked-files=all", check=False) == ""}
    return {"root": str(root), "branch_ref": branch, "head": head, "root_commit": ancestry_roots[0] if len(ancestry_roots) == 1 else None,
            "tracked_paths": names, "forbidden_tracked": forbidden,
            "forbidden_tracked_details": forbidden_details,
            "historical_tracked_attributions": [
                {"path": name, "rule": policy_path_decision(
                    name, policy, historical_attribution=attribution)["rule"], **attribution}
                for name, attribution in sorted(historical.items())
            ],
            "forbidden_root_tracked": forbidden_root,
            "forbidden_root_tracked_details": forbidden_root_details, "product_markers": product_markers,
            "unsafe_tracked_modes": unsafe_modes,
            "missing_preserved_root_paths": preservation_missing, "missing_canonical_prefixes": missing_canonical,
            "missing_required_generated": missing_generated,
            "gitignore_entries": gitignore_lines, "missing_root_agent_ignores": missing_root_ignores,
            "tracked_agent_surface": tracked_agent_surface,
            "agent_profile_diagnostic": agent_profile_diagnostic,
            "checks": checks, "passed": all(checks.values())}


def external_config_repair_receipt(path: Path, root: Path, common: Path) -> Path:
    output = path.expanduser().resolve()
    protected = [common.resolve()]
    listing = run(["git", "-C", str(root), "worktree", "list", "--porcelain", "-z"], root)
    protected.extend(Path(record.removeprefix("worktree ")).resolve()
                     for record in listing.stdout.split("\0") if record.startswith("worktree "))
    for item in protected:
        try: output.relative_to(item)
        except ValueError: continue
        raise BoundaryError("config repair receipts must be outside all worktrees and Git administration")
    return output


@contextmanager
def config_repair_lock(common: Path) -> Any:
    paths = [common / "juno-repository-writer.lock", common / "juno-controller-config-repair.lock"]
    streams = []
    try:
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            stream = path.open("a+"); fcntl.flock(stream.fileno(), fcntl.LOCK_EX); streams.append(stream)
        yield
    finally:
        for stream in reversed(streams):
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN); stream.close()


def agent_config_correction(value: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """One explicit ownership migration; discarded fields remain in the frozen Git parent."""
    workspace = value.get("controllerWorkspace")
    if workspace not in (CANONICAL_CONTROLLER_WORKSPACE, RETIRED_CONTROLLER_WORKSPACE, {"mode": "metadata-only"}):
        raise BoundaryError("agent config repair requires a recognized metadata-controller workspace")
    known = CONTROLLER_PROFILE_CONFIG_FIELDS | CONTROLLER_PRODUCT_CONFIG_FIELDS | {"controllerWorkspace", "lifecycle"}
    unknown = sorted(set(value) - known)
    if unknown:
        raise BoundaryError("agent config repair requires separate inventory of unknown/secret fields: "
                            + ", ".join(unknown) + "; inspect `yy migrate inventory --help`; no bytes changed")
    dispositions = {name: ("transform" if name == "controllerWorkspace" else
                          "product-only" if name in CONTROLLER_PRODUCT_CONFIG_FIELDS else
                          "retire" if name == "lifecycle" else "migrate") for name in sorted(value)}
    after = {name: item for name, item in value.items() if dispositions[name] == "migrate"}
    after["controllerWorkspace"] = CANONICAL_CONTROLLER_WORKSPACE
    return after, dispositions


def agent_config_runtime(root: Path, branch: str) -> dict[str, Any]:
    try:
        registration = require_controller_registration(root, branch)
        package = package_policy_source(root, registration["runtime_executable"])
        if registration["runtime_version"] != package["version"]:
            raise BoundaryError("registered runtime version differs from migration package")
        return {"registration": registration, "package": package}
    except BoundaryError as exc:
        raise BoundaryError("agent config runtime binding is incompatible (separate from config ownership): "
                            f"{exc}; inspect `yy scripts doctor` and `yy migrate runtime-rebind --help`; "
                            "no implicit upgrade or rebind") from exc


def agent_config_plan(args: argparse.Namespace, policy: dict[str, Any]) -> dict[str, Any]:
    root = exact_physical_controller(args.root)
    if (root / CONFIG_PATH).is_symlink():
        raise BoundaryError("agent config repair refuses a symbolic-link config")
    return config_repair_plan(argparse.Namespace(
        root=root, branch=policy["controller_branch"], product_ref=policy["product_ref"],
        expected_head=git(root, "rev-parse", "HEAD"),
        expected_product_head=git(root, "rev-parse", policy["product_ref"]),
        output=args.output, agent_config=True), policy)


def config_repair_plan(args: argparse.Namespace, policy: dict[str, Any]) -> dict[str, Any]:
    root = exact_worktree(args.root)
    branch = safe_ref(args.branch, "branch"); product_ref = safe_ref(args.product_ref, "product_ref")
    if branch != policy["controller_branch"] or product_ref != policy["product_ref"]:
        raise BoundaryError("controller/product refs do not match the reviewed metadata policy")
    if git(root, "symbolic-ref", "-q", "HEAD", check=False) != branch:
        raise BoundaryError("config repair requires the exact attached metadata controller branch")
    if git(root, "status", "--porcelain=v2", "--untracked-files=all", check=False):
        raise BoundaryError("config repair requires a clean controller")
    head = resolve_commit(root, branch, args.expected_head, "controller ref")
    product_head = resolve_commit(root, product_ref, args.expected_product_head, "product target")
    current, current_value = controller_config(root, head)
    agent_config = getattr(args, "agent_config", False)
    migration: dict[str, Any] = {}
    if agent_config:
        after_value, dispositions = agent_config_correction(current_value)
        if after_value == current_value:
            raise BoundaryError("controller config already has current ownership; no repair needed")
        migration = {"agent_config": {"field_dispositions": dispositions,
                                    "runtime": agent_config_runtime(root, branch)}}
    else:
        if (current_value.get("controllerWorkspace") != RETIRED_CONTROLLER_WORKSPACE
                or "lifecycle" in current_value):
            raise BoundaryError("config repair is limited to the policy-updated controller with the exact retired workspace pointer")
        if CONTROLLER_PRODUCT_CONFIG_FIELDS.intersection(current_value):
            raise BoundaryError("workspace-only repair would retain product-only fields; use `yy migrate controller-config plan --help`")
        after_value = json.loads(json.dumps(current_value))
        after_value["controllerWorkspace"] = CANONICAL_CONTROLLER_WORKSPACE
    after = canonical(after_value)
    output = external_config_repair_receipt(args.output, root, Path(common_dir(root)))
    core = {"schema_version": CONFIG_REPAIR_SCHEMA, "operation": "config-repair",
            "outcome": "planned_no_mutation", "controller": str(root), "branch": branch,
            "head": head, "tree": git(root, "rev-parse", f"{head}^{{tree}}"),
            "git_common_dir": common_dir(root), "product_ref": product_ref,
            "product_head": product_head, "policy_sha256": digest(policy),
            "correction": {"path": CONFIG_PATH, "before_sha256": bytes_digest(current),
                           "before": current_value, "after_sha256": bytes_digest(after),
                           "after": after_value},
            "preservation": {"controller_paths_except_config": "byte-identical",
                             "config_keys_except_controllerWorkspace": "semantically-identical",
                             "branch_identity": "unchanged", "product_ref_mutation": False,
                             "user_work": "clean-worktree-required"},
            "apply_authorized": False, **migration}
    if agent_config:
        core["preservation"]["config_keys_except_controllerWorkspace"] = "reviewed field dispositions; original config preserved in parent commit"
    payload = {**core, "plan_sha256": digest(core)}
    atomic_receipt(output, payload)
    return payload


def validate_config_repair_plan(plan_path: Path) -> tuple[dict[str, Any], str]:
    plan = read_json(plan_path, "config repair plan"); plan_hash = plan.pop("plan_sha256", None)
    if (plan.get("schema_version") != CONFIG_REPAIR_SCHEMA or plan.get("operation") != "config-repair"
            or plan.get("outcome") != "planned_no_mutation" or plan.get("apply_authorized") is not False
            or plan_hash != digest(plan)):
        raise BoundaryError("config repair requires an exact hash-bound no-mutation plan")
    correction = plan.get("correction")
    before = correction.get("before") if isinstance(correction, dict) else None
    after = correction.get("after") if isinstance(correction, dict) else None
    expected_after = json.loads(json.dumps(before)) if isinstance(before, dict) else None
    agent_config = plan.get("agent_config")
    if agent_config is not None:
        if not isinstance(before, dict) or not isinstance(agent_config, dict):
            raise BoundaryError("invalid agent config repair plan")
        expected_after, dispositions = agent_config_correction(before)
        if agent_config.get("field_dispositions") != dispositions or not isinstance(agent_config.get("runtime"), dict):
            raise BoundaryError("agent config repair requires exact derived field dispositions and runtime")
    elif isinstance(expected_after, dict):
        if (before.get("controllerWorkspace") != RETIRED_CONTROLLER_WORKSPACE or "lifecycle" in before
                or CONTROLLER_PRODUCT_CONFIG_FIELDS.intersection(before)):
            raise BoundaryError("workspace-only repair cannot retain product-only or retired fields")
        expected_after["controllerWorkspace"] = CANONICAL_CONTROLLER_WORKSPACE
    if (not isinstance(correction, dict) or correction.get("path") != CONFIG_PATH
            or not isinstance(before, dict) or after != expected_after
            or correction.get("after_sha256") != bytes_digest(canonical(after))):
        raise BoundaryError("config repair correction is not the exact derived workspace-only replacement")
    return plan, plan_hash


def config_repair_state(root: Path, plan: dict[str, Any], plan_hash: str, policy: dict[str, Any]) -> tuple[str, str]:
    if common_dir(root) != plan["git_common_dir"] or digest(policy) != plan["policy_sha256"]:
        raise BoundaryError("config repair repository or reviewed policy changed after planning")
    if "agent_config" in plan:
        exact_physical_controller(root)
        if (root / CONFIG_PATH).is_symlink():
            raise BoundaryError("agent config repair refuses a symbolic-link config")
        if agent_config_runtime(root, plan["branch"]) != plan["agent_config"]["runtime"]:
            raise BoundaryError("agent config runtime binding changed after planning; inspect `yy scripts doctor`")
    if git(root, "symbolic-ref", "-q", "HEAD", check=False) != plan["branch"]:
        raise BoundaryError("config repair controller branch changed after planning")
    if git(root, "status", "--porcelain=v2", "--untracked-files=all", check=False):
        raise BoundaryError("config repair requires a clean controller")
    resolve_commit(root, plan["product_ref"], plan["product_head"], "product target")
    head = git(root, "rev-parse", "HEAD")
    correction = plan["correction"]
    if head == plan["head"]:
        if git(root, "rev-parse", "HEAD^{tree}") != plan["tree"]:
            raise BoundaryError("controller tree changed after config repair planning")
        current, value = controller_config(root, head)
        if bytes_digest(current) != correction["before_sha256"] or value != correction["before"]:
            raise BoundaryError("controller config changed after config repair planning")
        return "before", head
    message = git(root, "show", "-s", "--format=%B", head)
    parent = git(root, "rev-parse", f"{head}^")
    current, value = controller_config(root, head)
    expected_message = f"Repair metadata controller config shape\n\nJuno-Config-Repair-Plan: {plan_hash}"
    if (parent == plan["head"] and message == expected_message
            and bytes_digest(current) == correction["after_sha256"] and value == correction["after"]
            and git(root, "diff", "--name-only", plan["head"], head).splitlines() == [CONFIG_PATH]):
        require_canonical_controller_config(root, head)
        return "after", head
    raise BoundaryError("controller is neither the frozen pre-repair state nor its exact completed repair")


def config_repair_apply(args: argparse.Namespace, policy: dict[str, Any]) -> dict[str, Any]:
    if not args.authorize:
        raise BoundaryError("config repair apply requires --authorize-config-repair")
    plan_path = args.plan.expanduser().resolve(); plan, plan_hash = validate_config_repair_plan(plan_path)
    root = exact_worktree(Path(plan["controller"])); common = Path(plan["git_common_dir"])
    output = external_config_repair_receipt(args.output, root, common)
    intent = external_config_repair_receipt(output.with_name(output.name + ".intent.json"), root, common)
    plan_file_hash = file_digest(plan_path)
    intent_payload = {"schema_version": CONFIG_REPAIR_SCHEMA, "operation": "config-repair-intent",
                      "outcome": "intent_persisted_before_mutation", "plan_file_sha256": plan_file_hash,
                      "plan_sha256": plan_hash, "expected_head": plan["head"],
                      "expected_tree": plan["tree"], "after_sha256": plan["correction"]["after_sha256"],
                      "product_ref_mutation": False}
    preflight_receipt(intent, intent_payload); atomic_receipt(intent, intent_payload)
    with config_repair_lock(common):
        state, head = config_repair_state(root, plan, plan_hash, policy)
        if state == "before":
            if output.exists():
                raise BoundaryError("config repair receipt path must be fresh before mutation")
            config_path = root / CONFIG_PATH
            config_path.write_bytes(canonical(plan["correction"]["after"])); git(root, "add", "--", CONFIG_PATH)
            if git(root, "diff", "--cached", "--name-only").splitlines() != [CONFIG_PATH]:
                git(root, "restore", "--staged", "--worktree", "--source", plan["head"], "--", CONFIG_PATH, check=False)
                raise BoundaryError("config repair staged paths outside the exact correction")
            commit_env = {"GIT_AUTHOR_NAME": "Juno Controller Migration", "GIT_AUTHOR_EMAIL": "juno-controller@local.invalid",
                          "GIT_COMMITTER_NAME": "Juno Controller Migration", "GIT_COMMITTER_EMAIL": "juno-controller@local.invalid"}
            try:
                run(["git", "-C", str(root), "commit", "-m", "Repair metadata controller config shape",
                     "-m", f"Juno-Config-Repair-Plan: {plan_hash}"], root, env=commit_env)
            except BaseException:
                if git(root, "rev-parse", "HEAD") == plan["head"]:
                    git(root, "restore", "--staged", "--worktree", "--source", plan["head"], "--", CONFIG_PATH, check=False)
                raise
            state, head = config_repair_state(root, plan, plan_hash, policy)
        if state != "after": raise BoundaryError("config repair did not reach its exact intended state")
    payload = {"schema_version": CONFIG_REPAIR_SCHEMA, "operation": "config-repair-apply", "outcome": "repaired",
               "plan_file_sha256": plan_file_hash, "plan_sha256": plan_hash,
               "intent_sha256": file_digest(intent), "controller": str(root), "branch": plan["branch"],
               "old_head": plan["head"], "new_head": head, "changed_paths": [CONFIG_PATH],
               "product_ref": plan["product_ref"], "product_head": plan["product_head"],
               "product_ref_mutation": False, "preservation_verified": True}
    atomic_receipt(output, payload)
    return payload


def reject_git_environment() -> None:
    # The harness may disable Git's optional opportunistic locks. This cannot
    # redirect repository/object/config identity; every migration lock is
    # explicit. All other Git process controls are refused rather than curated.
    inherited = sorted(key for key in os.environ
                       if key.startswith("GIT_") and key != "GIT_OPTIONAL_LOCKS")
    if inherited:
        raise BoundaryError(
            "Git environment overrides are not allowed for metadata-policy migration: "
            + ", ".join(inherited))


def exact_physical_controller(value: Path) -> Path:
    supplied = value.expanduser().absolute()
    if supplied.is_symlink():
        raise BoundaryError("metadata-policy migration refuses a symbolic-link controller root")
    root = exact_worktree(supplied)
    for relative in (".juno_task", ".juno_task/config", POLICY_PATH, TASK_POLICY_PATH, RISK_POLICY_PATH):
        cursor = root / relative
        if cursor.is_symlink():
            raise BoundaryError(f"metadata-policy migration refuses symbolic-link endpoint: {relative}")
    nested = root / ".juno_task/config/.git"
    if nested.exists() or nested.is_symlink():
        raise BoundaryError("metadata-policy migration refuses a nested repository at .juno_task/config")
    return root


def package_policy_source(root: Path, runtime_executable: str | None = None) -> dict[str, Any]:
    engine = Path(__file__).resolve()
    if runtime_executable:
        entrypoint = Path(runtime_executable).expanduser().resolve()
        if entrypoint.parent.name != "bin" or entrypoint.parent.parent.name not in {"src", "dist"}:
            raise BoundaryError("registered controller runtime executable is not a yylo package entrypoint")
        package_root = entrypoint.parent.parent.parent
    elif engine.parent.parent.parent.name in {"src", "dist"}:
        package_root = engine.parents[3]
    elif (root / "juno-code/package.json").is_file():
        package_root = root / "juno-code"
    else:
        raise BoundaryError("cannot locate migration package from the managed runtime without registered runtime identity")
    manifest_path = package_root / "package.json"
    manifest = read_json(manifest_path, "yylo package manifest")
    if manifest.get("name") != "@yylo/cli" or not isinstance(manifest.get("version"), str):
        raise BoundaryError("metadata-policy migration requires an identifiable yylo package")
    generation_name = "src" if (runtime_executable and Path(runtime_executable).resolve().parent.parent.name == "src") \
        or (not runtime_executable and engine.parent.parent.parent.name == "src") else "dist"
    template_root = package_root / generation_name / "templates"
    package_engine = template_root / "scripts/metadata_controller.py"
    integration_path = template_root / "config/integration-workspace.json"
    if package_engine.is_symlink() or not package_engine.is_file() or file_digest(package_engine) != file_digest(engine):
        raise BoundaryError("running metadata-policy engine differs from the exact registered package source")
    if integration_path.is_symlink() or not integration_path.is_file():
        raise BoundaryError("packaged integration-workspace policy source is missing or unsafe")
    integration_bytes = integration_path.read_bytes()
    try:
        integration_value = json.loads(integration_bytes)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BoundaryError(f"packaged integration-workspace policy is invalid: {exc}") from exc
    if not isinstance(integration_value, dict):
        raise BoundaryError("packaged integration-workspace policy must be an object")
    engine_sha256 = file_digest(engine)
    validators: dict[str, dict[str, str]] = {}
    for name in ("task_workspace.py", "risk_policy.py", "integration_workspace.py"):
        executing = engine.with_name(name); packaged = template_root / "scripts" / name
        if (executing.is_symlink() or packaged.is_symlink() or not executing.is_file()
                or not packaged.is_file() or file_digest(executing) != file_digest(packaged)):
            raise BoundaryError(f"running metadata-policy validator differs from registered package source: {name}")
        validators[name] = {"path": str(packaged.resolve()), "sha256": file_digest(packaged)}
    source_execution = generation_name == "src"
    runtime_entrypoint = package_root / ("src/bin/cli.ts" if source_execution else "dist/bin/cli.mjs")
    if runtime_entrypoint.is_symlink() or not runtime_entrypoint.is_file():
        raise BoundaryError("yylo migration package runtime entrypoint is missing or unsafe")
    generation_path = root / ".juno_task/runtime/managed-controller/generation.json"
    generation: dict[str, Any] = {"present": False}
    if generation_path.exists() or generation_path.is_symlink():
        if generation_path.is_symlink() or not generation_path.is_file():
            raise BoundaryError("managed controller generation evidence is unsafe")
        generation_bytes = generation_path.read_bytes()
        try:
            generation_value = json.loads(generation_bytes)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise BoundaryError(f"managed controller generation evidence is invalid: {exc}") from exc
        scripts = generation_value.get("scripts") if isinstance(generation_value, dict) else None
        engine_binding = scripts.get(POLICY_PATH.replace("config/metadata-controller.json", "scripts/metadata_controller.py")) \
            if isinstance(scripts, dict) else None
        runtime_engine = root / ".juno_task/scripts/metadata_controller.py"
        if (not isinstance(generation_value, dict)
                or generation_value.get("schema_version") != "juno_managed_controller_runtime.v1"
                or generation_value.get("package_version") != manifest["version"]
                or not isinstance(generation_value.get("target_sha"), str)
                or not SHA_RE.fullmatch(generation_value["target_sha"])
                or not isinstance(engine_binding, dict)
                or engine_binding.get("classification") != "exact"
                or engine_binding.get("source_sha256") != engine_sha256
                or engine_binding.get("actual_sha256") != engine_sha256
                or runtime_engine.is_symlink() or not runtime_engine.is_file()
                or file_digest(runtime_engine) != engine_sha256):
            raise BoundaryError("managed controller generation does not match the exact migration package/runtime engine")
        generation = {"present": True, "sha256": bytes_digest(generation_bytes),
                      "package_version": generation_value["package_version"],
                      "target_sha": generation_value["target_sha"],
                      "engine_binding_sha256": digest(engine_binding)}
    return {"package": "@yylo/cli", "version": manifest["version"],
            "package_manifest_sha256": file_digest(manifest_path),
            "engine_path": str(engine), "engine_sha256": engine_sha256,
            "runtime_entrypoint": str(runtime_entrypoint.resolve()),
            "runtime_entrypoint_sha256": file_digest(runtime_entrypoint),
            "validators": validators,
            "integration_source_path": str(integration_path),
            "integration_source_sha256": bytes_digest(integration_bytes),
            "integration_source_utf8": integration_bytes.decode("utf-8"),
            "runtime_generation": generation}


def require_controller_registration(root: Path, branch: str) -> dict[str, str]:
    paths = git(root, "config", "--local", "--get-all", "juno.controller.path", check=False).splitlines()
    branches = git(root, "config", "--local", "--get-all", "juno.controller.branch", check=False).splitlines()
    if len(paths) != 1 or len(branches) != 1:
        raise BoundaryError("metadata-policy migration requires exactly one persisted controller path/branch registration")
    registered = Path(paths[0]).expanduser().resolve()
    if registered != root or branches[0] != branch:
        raise BoundaryError("metadata-policy migration root/branch does not match persisted controller registration")
    role = git(root, "config", "--worktree", "--get", "juno.workspace.role", check=False)
    mode = git(root, "config", "--worktree", "--get", "juno.controller.mode", check=False)
    runtime_version = git(root, "config", "--worktree", "--get", "juno.controller.runtimeVersion", check=False)
    runtime_executable = git(root, "config", "--worktree", "--get", "juno.controller.runtimeExecutable", check=False)
    if role != "controller" or mode != "metadata-only":
        raise BoundaryError("metadata-policy migration requires the registered active metadata-only controller role")
    if not runtime_version or not runtime_executable:
        raise BoundaryError("metadata-policy migration requires exact registered controller runtime identity")
    return {"path": paths[0], "branch": branches[0], "role": role, "mode": mode,
            "runtime_version": runtime_version,
            "runtime_executable": str(Path(runtime_executable).expanduser().resolve())}


def insert_array_string(source: bytes, field: str, addition: str) -> bytes:
    try:
        text = source.decode("utf-8")
        value = json.loads(text)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BoundaryError(f"metadata-controller policy bytes are invalid: {exc}") from exc
    entries = value.get(field) if isinstance(value, dict) else None
    if not isinstance(entries, list) or any(not isinstance(item, str) for item in entries):
        raise BoundaryError(f"metadata-controller policy {field} must be a string array")
    if addition in entries:
        return source
    match = re.search(rf'"{re.escape(field)}"\s*:\s*\[', text)
    if not match:
        raise BoundaryError(f"metadata-controller policy lacks a byte-preservable {field} array")
    index = match.end(); depth = 1; quoted = False; escaped = False
    while index < len(text) and depth:
        char = text[index]
        if quoted:
            if escaped: escaped = False
            elif char == "\\": escaped = True
            elif char == '"': quoted = False
        elif char == '"': quoted = True
        elif char == '[': depth += 1
        elif char == ']': depth -= 1
        index += 1
    if depth:
        raise BoundaryError(f"metadata-controller policy has an unterminated {field} array")
    close = index - 1
    prefix = text[:close]; suffix = text[close:]
    array_body = text[match.end():close]
    encoded = json.dumps(addition, ensure_ascii=False)
    if not array_body.strip():
        insertion = encoded
    elif "\n" not in array_body:
        insertion = "," + encoded
    else:
        trailing = re.search(r"(\n[ \t]*)$", array_body)
        closing_indent = trailing.group(1) if trailing else "\n"
        item_indent_match = re.search(r"\n([ \t]*)\S", array_body)
        item_indent = item_indent_match.group(1) if item_indent_match else "  "
        if trailing:
            prefix = prefix[:-len(closing_indent)]
        insertion = f",\n{item_indent}{encoded}{closing_indent}"
    return (prefix + insertion + suffix).encode("utf-8")


def derive_policy_migration(before: bytes) -> tuple[str, bytes, dict[str, Any]]:
    try:
        value = json.loads(before)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BoundaryError(f"metadata-controller identity policy is invalid: {exc}") from exc
    if not isinstance(value, dict):
        raise BoundaryError("metadata-controller identity policy must be an object")
    generated = value.get("generated_metadata"); tracked = value.get("tracked_exact")
    if not isinstance(generated, list) or not isinstance(tracked, list):
        raise BoundaryError("metadata-controller policy lacks supported classification arrays")
    generated_has = INTEGRATION_POLICY_PATH in generated
    tracked_has = INTEGRATION_POLICY_PATH in tracked
    if generated_has != tracked_has:
        raise BoundaryError("metadata-controller policy has a partial integration-workspace classification")
    immutable_fields = {key: value.get(key) for key in (
        "schema_version", "controller_branch", "product_ref", "spec_copy_mode",
        "copied_metadata", "product_forbidden", "tracked_recursive",
        "tracked_top_level_files", "runtime")}
    if generated_has:
        return "already_migrated", before, immutable_fields
    required_existing = (POLICY_PATH, TASK_POLICY_PATH, RISK_POLICY_PATH)
    if any(item not in generated or item not in tracked for item in required_existing):
        raise BoundaryError("policy is not the known legacy pre-integration-workspace classification shape")
    after = insert_array_string(insert_array_string(before, "generated_metadata", INTEGRATION_POLICY_PATH),
                                "tracked_exact", INTEGRATION_POLICY_PATH)
    after_value = json.loads(after)
    for key, expected in immutable_fields.items():
        if after_value.get(key) != expected:
            raise BoundaryError(f"metadata-policy migration changed immutable identity field: {key}")
    if set(after_value["generated_metadata"]) != set(generated) | {INTEGRATION_POLICY_PATH} \
            or set(after_value["tracked_exact"]) != set(tracked) | {INTEGRATION_POLICY_PATH}:
        raise BoundaryError("metadata-policy migration derived changes outside the exact semantic additions")
    return "legacy_migration", after, immutable_fields


def policy_migration_snapshot(root: Path, expected_branch: str | None = None,
                              *, owned_index_lock: bool = False) -> dict[str, Any]:
    reject_git_environment()
    branch = git(root, "symbolic-ref", "-q", "HEAD", check=False)
    if not branch or (expected_branch is not None and branch != expected_branch):
        raise BoundaryError("metadata-policy migration requires the exact attached controller branch")
    registration = require_controller_registration(root, branch)
    config_bytes, config_value = controller_config(root, git(root, "rev-parse", "HEAD"))
    if ("lifecycle" in config_value
            or config_value.get("controllerWorkspace") != CANONICAL_CONTROLLER_WORKSPACE):
        raise BoundaryError("metadata-policy migration requires the exact metadata-only controller workspace pointer")
    if git(root, "status", "--porcelain=v2", "--untracked-files=all", check=False):
        raise BoundaryError("metadata-policy migration requires a clean controller with no staged or unrelated dirt")
    index_lock = Path(git(root, "rev-parse", "--path-format=absolute", "--git-path", "index.lock"))
    if (index_lock.exists() or index_lock.is_symlink()) and not owned_index_lock:
        raise BoundaryError(f"metadata-policy migration refuses an existing Git index lock: {index_lock}")
    head = git(root, "rev-parse", "HEAD"); tree = git(root, "rev-parse", "HEAD^{tree}")
    policy_bytes = committed_bytes(root, head, POLICY_PATH)
    if (root / POLICY_PATH).read_bytes() != policy_bytes:
        raise BoundaryError("metadata-controller policy worktree bytes differ from HEAD")
    state, result_bytes, immutable_fields = derive_policy_migration(policy_bytes)
    parsed = json.loads(policy_bytes)
    branch = safe_ref(parsed.get("controller_branch"), "controller_branch")
    product_ref = safe_ref(parsed.get("product_ref"), "product_ref")
    if registration["branch"] != branch:
        raise BoundaryError("registered controller branch differs from reviewed metadata policy")
    product_head = git(root, "rev-parse", f"{product_ref}^{{commit}}", check=False)
    if not SHA_RE.fullmatch(product_head):
        raise BoundaryError("reviewed product ref does not resolve to a commit")
    task_bytes = committed_bytes(root, head, TASK_POLICY_PATH)
    risk_bytes = committed_bytes(root, head, RISK_POLICY_PATH)
    if (root / TASK_POLICY_PATH).read_bytes() != task_bytes or (root / RISK_POLICY_PATH).read_bytes() != risk_bytes:
        raise BoundaryError("reviewed task/risk policy worktree bytes differ from HEAD")
    try:
        task_value = validate_task_policy(json.loads(task_bytes))
        validate_risk_policy(json.loads(risk_bytes))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BoundaryError(f"reviewed task/risk policy bytes are invalid: {exc}") from exc
    source = package_policy_source(root, registration["runtime_executable"])
    if (registration["runtime_version"] != source["version"]
            or registration["runtime_executable"] != source["runtime_entrypoint"]):
        raise BoundaryError("registered controller runtime differs from the exact executing migration package")
    integration_bytes = source["integration_source_utf8"].encode("utf-8")
    validate_integration_policy(json.loads(integration_bytes), task_value)
    with tempfile.TemporaryDirectory(prefix="juno-policy-result-") as temporary:
        result_path = Path(temporary) / "metadata-controller.json"
        result_path.write_bytes(result_bytes)
        load_policy(result_path)
    if state == "already_migrated":
        committed_integration = committed_bytes(root, head, INTEGRATION_POLICY_PATH)
        if committed_integration != integration_bytes or (root / INTEGRATION_POLICY_PATH).read_bytes() != committed_integration:
            raise BoundaryError("already-migrated integration-workspace bytes differ from exact package source")
    else:
        endpoint = root / INTEGRATION_POLICY_PATH
        if endpoint.exists() or endpoint.is_symlink():
            raise BoundaryError("legacy migration requires an absent integration-workspace endpoint")
    common = Path(common_dir(root))
    index_path = Path(git(root, "rev-parse", "--path-format=absolute", "--git-path", "index"))
    root_stat = root.stat(); index_stat = index_path.stat()
    return {"root": str(root), "root_identity": [root_stat.st_dev, root_stat.st_ino, root_stat.st_mode],
            "git_common_dir": str(common), "index_path": str(index_path),
            "git_common_identity": list(common.stat()[:3]),
            "index_identity": [index_stat.st_dev, index_stat.st_ino, index_stat.st_mode],
            "branch": branch, "registration": registration,
            "head": head, "tree": tree,
            "commit_timestamp": git(root, "show", "-s", "--format=%aI", head),
            "config_sha256": bytes_digest(config_bytes),
            "product_ref": product_ref, "product_head": product_head,
            "policy_before_sha256": bytes_digest(policy_bytes),
            "policy_before_utf8": policy_bytes.decode("utf-8"),
            "policy_result_sha256": bytes_digest(result_bytes),
            "policy_result_utf8": result_bytes.decode("utf-8"),
            "task_policy_sha256": bytes_digest(task_bytes), "risk_policy_sha256": bytes_digest(risk_bytes),
            "immutable_identity": immutable_fields, "source": source, "state": state,
            "integration_result_sha256": source["integration_source_sha256"]}


def expected_policy_commit_intent(value: dict[str, Any], changed: list[str]) -> dict[str, Any]:
    return {"parent": value["head"], "base_tree": value["tree"],
            "message": "Migrate metadata controller integration policy\n\nJuno-Metadata-Policy-Migration-Plan: {plan_sha256}",
            "authority": "juno-metadata-policy-migration.v1",
            "identity": {"author_name": "Juno Metadata Policy Migration",
                         "author_email": "juno-controller@local.invalid",
                         "author_date": value["commit_timestamp"],
                         "committer_name": "Juno Metadata Policy Migration",
                         "committer_email": "juno-controller@local.invalid",
                         "committer_date": value["commit_timestamp"]},
            "changed_paths": changed,
            "result_bindings": {POLICY_PATH: value["policy_result_sha256"],
                                INTEGRATION_POLICY_PATH: value["integration_result_sha256"]}}


def policy_migration_plan(args: argparse.Namespace) -> dict[str, Any]:
    root = exact_physical_controller(args.root)
    snapshot = policy_migration_snapshot(root)
    output = external_config_repair_receipt(args.output, root, Path(common_dir(root)))
    changed = [] if snapshot["state"] == "already_migrated" else list(POLICY_MIGRATION_PATHS)
    intent = expected_policy_commit_intent(snapshot, changed)
    core = {"schema_version": POLICY_MIGRATION_SCHEMA, "operation": "metadata-policy-migration",
            "outcome": "already_migrated_no_mutation" if not changed else "planned_no_mutation",
            **snapshot, "changed_paths": changed,
            "semantic_additions": [] if not changed else [
                {"field": "generated_metadata", "value": INTEGRATION_POLICY_PATH},
                {"field": "tracked_exact", "value": INTEGRATION_POLICY_PATH}],
            "result_tree_commit_intent": intent, "apply_authorized": False}
    payload = {**core, "plan_sha256": digest(core)}
    atomic_receipt(output, payload)
    return payload


def validate_policy_migration_plan(path: Path) -> tuple[dict[str, Any], str]:
    plan = read_json(path, "metadata-policy migration plan"); plan_hash = plan.pop("plan_sha256", None)
    changed = plan.get("changed_paths")
    expected_changed = [] if plan.get("state") == "already_migrated" else list(POLICY_MIGRATION_PATHS)
    expected_additions = [] if not expected_changed else [
        {"field": "generated_metadata", "value": INTEGRATION_POLICY_PATH},
        {"field": "tracked_exact", "value": INTEGRATION_POLICY_PATH}]
    if (plan.get("schema_version") != POLICY_MIGRATION_SCHEMA
            or plan.get("operation") != "metadata-policy-migration"
            or plan.get("outcome") not in {"planned_no_mutation", "already_migrated_no_mutation"}
            or plan.get("apply_authorized") is not False or changed != expected_changed
            or plan.get("semantic_additions") != expected_additions
            or plan.get("result_tree_commit_intent") != expected_policy_commit_intent(plan, expected_changed)
            or plan_hash != digest(plan)):
        raise BoundaryError("metadata-policy migration requires an exact canonical hash-bound plan")
    return plan, plan_hash


def acquire_policy_migration_locks(common: Path, branch: str) -> Any:
    identity = f"{common.resolve()}\0{branch}".encode()
    paths = [common / "juno-repository-writer.lock",
             common / "juno-integration-channels" / (hashlib.sha256(identity).hexdigest() + ".lock"),
             common / "juno-metadata-policy-migration.lock"]
    @contextmanager
    def locks() -> Any:
        streams: list[tuple[Path, Any, tuple[int, int]]] = []
        def verify() -> None:
            for path, stream, identity in streams:
                descriptor_stat = os.fstat(stream.fileno())
                try: path_stat = os.lstat(path)
                except OSError as exc:
                    raise BoundaryError(f"metadata-policy migration serialization lease was replaced: {path}") from exc
                if (stat.S_ISLNK(path_stat.st_mode)
                        or (descriptor_stat.st_dev, descriptor_stat.st_ino) != identity
                        or (path_stat.st_dev, path_stat.st_ino) != identity):
                    raise BoundaryError(f"metadata-policy migration serialization lease identity drifted: {path}")
        try:
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
                stream = os.fdopen(descriptor, "a+")
                try: fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    stream.close()
                    raise BoundaryError(f"metadata-policy migration serialization lease busy: {path}") from exc
                value = os.fstat(stream.fileno())
                streams.append((path, stream, (value.st_dev, value.st_ino)))
            verify(); yield verify
        finally:
            for _, stream, _ in reversed(streams):
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN); stream.close()
    return locks()


def assert_policy_plan_snapshot(plan: dict[str, Any], *, owned_index_lock: bool = False) -> tuple[Path, dict[str, Any]]:
    root = exact_physical_controller(Path(plan["root"]))
    current = policy_migration_snapshot(root, plan["branch"], owned_index_lock=owned_index_lock)
    keys = ("root", "root_identity", "git_common_dir", "index_path", "git_common_identity",
            "index_identity", "branch", "registration", "head", "tree", "commit_timestamp",
            "config_sha256", "product_ref",
            "product_head", "policy_before_sha256", "policy_before_utf8", "policy_result_sha256",
            "policy_result_utf8", "task_policy_sha256", "risk_policy_sha256", "immutable_identity",
            "source", "state", "integration_result_sha256")
    if any(current.get(key) != plan.get(key) for key in keys):
        raise BoundaryError("metadata-policy migration plan is stale: controller, policy, package, product ref, or runtime generation drifted")
    return root, current


def migration_temporary_endpoints(root: Path, plan_hash: str) -> list[Path]:
    suffix = plan_hash[:16]
    return [root / relative for relative in (
        f".juno_task/config/.metadata-controller.json.migration-{suffix}",
        f".juno_task/config/.integration-workspace.json.migration-{suffix}")]


def open_config_directory(root: Path) -> tuple[int, tuple[int, int, int]]:
    directory = root / ".juno_task/config"
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try: descriptor = os.open(directory, flags)
    except OSError as exc:
        raise BoundaryError("metadata-policy migration config directory is missing or unsafe") from exc
    value = os.fstat(descriptor)
    if not stat.S_ISDIR(value.st_mode):
        os.close(descriptor)
        raise BoundaryError("metadata-policy migration config endpoint is not a directory")
    return descriptor, (value.st_dev, value.st_ino, value.st_mode)


def endpoint_snapshot_at(directory_fd: int, name: str) -> tuple[bytes, tuple[int, int, int, int, int]] | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try: descriptor = os.open(name, flags, dir_fd=directory_fd)
    except FileNotFoundError: return None
    except OSError as exc:
        raise BoundaryError(f"metadata-policy migration refuses an unsafe endpoint: {name}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise BoundaryError(f"metadata-policy migration endpoint is not a regular file: {name}")
        with os.fdopen(descriptor, "rb", closefd=False) as stream: data = stream.read()
        after = os.fstat(descriptor)
    finally: os.close(descriptor)
    identity = lambda value: (value.st_dev, value.st_ino, value.st_mode,
                              value.st_size, value.st_mtime_ns)
    if identity(before) != identity(after) or len(data) != after.st_size:
        raise BoundaryError(f"metadata-policy endpoint raced during no-follow snapshot: {name}")
    return data, identity(after)


def rename_noreplace_between(source_fd: int, source: str,
                             destination_fd: int, destination: str) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if hasattr(libc, "renameatx_np"):
        operation = libc.renameatx_np
        operation.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        flag = 0x00000004
    elif hasattr(libc, "renameat2"):
        operation = libc.renameat2
        operation.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        flag = 0x1
    else:
        raise BoundaryError("metadata-policy migration requires atomic no-clobber rename support")
    if operation(source_fd, os.fsencode(source), destination_fd, os.fsencode(destination), flag) != 0:
        error = ctypes.get_errno()
        raise BoundaryError(f"metadata-policy atomic quarantine refused: {source}: {os.strerror(error)}")


def exact_unlink_endpoint_at(directory_fd: int, name: str,
                             expected: tuple[bytes, tuple[int, int, int, int, int]],
                             quarantine_fd: int) -> None:
    quarantine = f"endpoint-{os.getpid()}-{os.urandom(16).hex()}"
    rename_noreplace_between(directory_fd, name, quarantine_fd, quarantine)
    if endpoint_snapshot_at(quarantine_fd, quarantine) != expected:
        try: rename_noreplace_between(quarantine_fd, quarantine, directory_fd, name)
        except BoundaryError as exc:
            raise BoundaryError(f"metadata-policy endpoint quarantine raced and restoration failed: {name}") from exc
        raise BoundaryError(f"metadata-policy endpoint identity raced before quarantine: {name}")
    # Exact retired evidence remains preserved outside the worktree. Deleting a
    # verified pathname would reopen the same rename/replacement TOCTOU.
    os.fsync(quarantine_fd)


def exact_unlink_path(path: Path, expected: dict[str, Any]) -> None:
    directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    quarantine = f"retired-{os.getpid()}-{os.urandom(16).hex()}"
    try:
        rename_noreplace_between(directory_fd, path.name, directory_fd, quarantine)
        quarantined = path.parent / quarantine
        if not identity_matches(index_lock_identity(quarantined), expected, relocated=True):
            try: rename_noreplace_between(directory_fd, quarantine, directory_fd, path.name)
            except BoundaryError as exc:
                raise BoundaryError(f"metadata-policy path quarantine raced and restoration failed: {path}") from exc
            raise BoundaryError(f"metadata-policy path identity raced before quarantine: {path}")
        os.fsync(directory_fd)
    finally: os.close(directory_fd)


def atomic_endpoint_publish(directory_fd: int, temporary_name: str, name: str,
                            expected: tuple[bytes, tuple[int, int, int, int, int]] | None,
                            quarantine_fd: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    encoded_temporary = os.fsencode(temporary_name); encoded_name = os.fsencode(name)
    if hasattr(libc, "renameatx_np"):
        operation = libc.renameatx_np
        operation.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        flag = 0x00000004 if expected is None else 0x00000002  # RENAME_EXCL / RENAME_SWAP
    elif hasattr(libc, "renameat2"):
        operation = libc.renameat2
        operation.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        flag = 0x1 if expected is None else 0x2  # RENAME_NOREPLACE / RENAME_EXCHANGE
    else:
        raise BoundaryError("metadata-policy migration requires atomic no-clobber/exchange rename support")
    if operation(directory_fd, encoded_temporary, directory_fd, encoded_name, flag) != 0:
        error = ctypes.get_errno()
        raise BoundaryError(f"metadata-policy endpoint atomic publication refused: {name}: {os.strerror(error)}")
    if expected is not None:
        displaced = endpoint_snapshot_at(directory_fd, temporary_name)
        if displaced != expected:
            # The concurrent entry remains preserved at temporary_name. Restore
            # the reviewed entry atomically; never discard either byte stream.
            if operation(directory_fd, encoded_temporary, directory_fd, encoded_name, flag) != 0:
                error = ctypes.get_errno()
                raise BoundaryError(
                    f"metadata-policy endpoint raced and atomic restoration failed: {name}: {os.strerror(error)}")
            raise BoundaryError(f"metadata-policy endpoint raced at atomic publication: {name}")
        exact_unlink_endpoint_at(directory_fd, temporary_name, displaced, quarantine_fd)


def atomic_index_publish(index_lock: Path, index_path: Path,
                         expected_identity: dict[str, Any]) -> None:
    if index_lock.parent != index_path.parent:
        raise BoundaryError("metadata-policy prepared and real index are not co-located")
    directory_fd = os.open(index_path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    temporary_name = index_lock.name; name = index_path.name
    libc = ctypes.CDLL(None, use_errno=True)
    if hasattr(libc, "renameatx_np"):
        operation = libc.renameatx_np
        operation.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        flag = 0x00000002
    elif hasattr(libc, "renameat2"):
        operation = libc.renameat2
        operation.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        flag = 0x2
    else:
        os.close(directory_fd)
        raise BoundaryError("metadata-policy migration requires atomic index exchange support")
    temporary = os.fsencode(temporary_name); target = os.fsencode(name)
    try:
        if operation(directory_fd, temporary, directory_fd, target, flag) != 0:
            error = ctypes.get_errno()
            raise BoundaryError(f"metadata-policy index atomic exchange failed: {os.strerror(error)}")
        displaced_path = index_lock.parent / temporary_name
        displaced_identity = index_lock_identity(displaced_path)
        if not identity_matches(displaced_identity, expected_identity, relocated=True):
            if operation(directory_fd, temporary, directory_fd, target, flag) != 0:
                error = ctypes.get_errno()
                raise BoundaryError(f"metadata-policy index raced and restoration failed: {os.strerror(error)}")
            raise BoundaryError("metadata-policy real index bytes or identity raced before atomic publication")
        exact_unlink_path(displaced_path, displaced_identity)
        os.fsync(directory_fd)
    finally: os.close(directory_fd)


def index_lock_ownership_path(common: Path, plan_hash: str) -> Path:
    return common / "juno-metadata-policy-index-ownership" / f"{plan_hash}.json"


def index_lock_identity(path: Path) -> dict[str, Any] | None:
    if path.is_symlink() or not path.is_file(): return None
    before = path.stat()
    digest = file_digest(path)
    after = path.stat()
    fields_before = (before.st_dev, before.st_ino, before.st_mode, before.st_size, before.st_mtime_ns)
    fields_after = (after.st_dev, after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns)
    if fields_before != fields_after or path.is_symlink() or not path.is_file(): return None
    return {"path": str(path), "device": after.st_dev, "inode": after.st_ino,
            "mode": after.st_mode, "size": after.st_size, "mtime_ns": after.st_mtime_ns,
            "sha256": digest}


def identity_matches(actual: dict[str, Any] | None, expected: Any, *, relocated: bool = False) -> bool:
    if not isinstance(actual, dict) or not isinstance(expected, dict): return False
    if relocated:
        actual = {key: value for key, value in actual.items() if key != "path"}
        expected = {key: value for key, value in expected.items() if key != "path"}
    return actual == expected


def persist_index_lock_ownership(common: Path, plan_hash: str, index_path: Path,
                                 expected_tree: str, displaced_index: Path | None = None) -> Path:
    marker = index_lock_ownership_path(common, plan_hash)
    if marker.exists() or marker.is_symlink():
        raise BoundaryError(f"metadata-policy migration index ownership collision: {marker}")
    identity = index_lock_identity(index_path)
    displaced_identity = index_lock_identity(displaced_index) if displaced_index is not None else identity
    if identity is None or displaced_identity is None:
        raise BoundaryError("metadata-policy migration cannot bind unsafe prepared or displaced index bytes")
    atomic_receipt(marker, {"schema_version": "juno_metadata_policy_index_ownership.v2",
                            "plan_sha256": plan_hash, "expected_tree": expected_tree,
                            "index_lock": identity, "displaced_index": displaced_identity})
    return marker


def write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise BoundaryError("metadata-policy endpoint write made no progress")
        view = view[written:]


def publish_test_ready(path: Path, payload: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        write_all(descriptor, payload.encode("utf-8")); os.fsync(descriptor)
        os.close(descriptor); descriptor = -1
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(directory_fd)
        finally: os.close(directory_fd)
    finally:
        if descriptor >= 0: os.close(descriptor)
        temporary.unlink(missing_ok=True)


def durable_unlink(path: Path) -> None:
    path.unlink(missing_ok=True)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try: os.fsync(directory_fd)
    finally: os.close(directory_fd)


def index_has_exact_tree(root: Path, index_path: Path, identity: dict[str, Any],
                         expected_tree: str) -> bool:
    # Git's write-tree attempts to create `<GIT_INDEX_FILE>.lock`; when checking
    # the published real index during crash recovery that pathname is precisely
    # the displaced index we must preserve. Verify a byte-exact private copy.
    with tempfile.TemporaryDirectory(prefix="juno-policy-index-verify-") as temporary:
        copy = Path(temporary) / "index"
        copy.write_bytes(index_path.read_bytes())
        if not identity_matches(index_lock_identity(index_path), identity): return False
        return git(root, "write-tree", check=False,
                   env={"GIT_INDEX_FILE": str(copy)}) == expected_tree


def policy_result_tree(root: Path, plan: dict[str, Any]) -> str:
    with tempfile.TemporaryDirectory(prefix="juno-policy-result-tree-") as temporary:
        index = Path(temporary) / "index"
        env = {"GIT_INDEX_FILE": str(index)}
        git(root, "read-tree", plan["head"], env=env)
        add_blob(root, env, POLICY_PATH, plan["policy_result_utf8"].encode("utf-8"))
        add_blob(root, env, INTEGRATION_POLICY_PATH,
                 plan["source"]["integration_source_utf8"].encode("utf-8"))
        return git(root, "write-tree", env=env)


def exact_prepared_index(root: Path, index_path: Path, ownership_path: Path,
                         plan_hash: str, expected_tree: str) -> bool:
    identity = index_lock_identity(index_path)
    if identity is None or ownership_path.is_symlink() or not ownership_path.is_file(): return False
    try: ownership = read_json(ownership_path, "metadata-policy index ownership")
    except BoundaryError: return False
    return (ownership.get("schema_version") == "juno_metadata_policy_index_ownership.v2"
            and ownership.get("plan_sha256") == plan_hash
            and ownership.get("expected_tree") == expected_tree
            and identity_matches(identity, ownership.get("index_lock"),
                                 relocated=str(index_path) != ownership.get("index_lock", {}).get("path"))
            and index_has_exact_tree(root, index_path, identity, expected_tree))


def exact_displaced_index(index_lock: Path, ownership_path: Path) -> bool:
    if ownership_path.is_symlink() or not ownership_path.is_file(): return False
    try: ownership = read_json(ownership_path, "metadata-policy index ownership")
    except BoundaryError: return False
    return (ownership.get("schema_version") == "juno_metadata_policy_index_ownership.v2"
            and identity_matches(index_lock_identity(index_lock), ownership.get("displaced_index"),
                                 relocated=True))


def completed_policy_migration(root: Path, plan: dict[str, Any], plan_hash: str,
                               *, allow_pending_endpoints: bool) -> tuple[str, str] | None:
    head = git(root, "rev-parse", "HEAD")
    if head == plan["head"]:
        return None
    registration = require_controller_registration(root, plan["branch"])
    source = package_policy_source(root, registration["runtime_executable"])
    common = Path(common_dir(root))
    if (str(common) != plan.get("git_common_dir")
            or list(common.stat()[:3]) != plan.get("git_common_identity")
            or str(Path(git(root, "rev-parse", "--path-format=absolute", "--git-path", "index"))) != plan.get("index_path")
            or registration != plan["registration"] or source != plan["source"]
            or git(root, "symbolic-ref", "-q", "HEAD", check=False) != plan["branch"]
            or git(root, "rev-parse", f"{plan['product_ref']}^{{commit}}", check=False) != plan["product_head"]):
        raise BoundaryError("completed metadata-policy commit has stale authority bindings")
    tree = git(root, "rev-parse", "HEAD^{tree}")
    intent = plan["result_tree_commit_intent"]
    message = intent["message"].format(plan_sha256=plan_hash)
    if (git(root, "rev-parse", "HEAD^") != plan["head"]
            or git(root, "show", "-s", "--format=%B", head) != message
            or git(root, "show", "-s", "--format=%an%n%ae%n%aI%n%cn%n%ce%n%cI", head).splitlines()
                != [intent["identity"]["author_name"], intent["identity"]["author_email"],
                    intent["identity"]["author_date"], intent["identity"]["committer_name"],
                    intent["identity"]["committer_email"], intent["identity"]["committer_date"]]
            or git(root, "diff", "--name-only", plan["head"], head).splitlines() != plan["changed_paths"]
            or bytes_digest(committed_bytes(root, head, POLICY_PATH)) != plan["policy_result_sha256"]
            or bytes_digest(committed_bytes(root, head, INTEGRATION_POLICY_PATH)) != plan["integration_result_sha256"]
            or bytes_digest(committed_bytes(root, head, CONFIG_PATH)) != plan["config_sha256"]
            or bytes_digest(committed_bytes(root, head, TASK_POLICY_PATH)) != plan["task_policy_sha256"]
            or bytes_digest(committed_bytes(root, head, RISK_POLICY_PATH)) != plan["risk_policy_sha256"]):
        raise BoundaryError("controller HEAD is not the exact completed metadata-policy migration")
    temporary_endpoints = migration_temporary_endpoints(root, plan_hash)
    pending = set(git(root, "diff", "--name-only", check=False).splitlines()) \
        | set(git(root, "diff", "--cached", "--name-only", check=False).splitlines()) \
        | set(git(root, "ls-files", "--others", "--exclude-standard", check=False).splitlines())
    temporary_relatives = {path.relative_to(root).as_posix() for path in temporary_endpoints}
    pending_without_temporary = pending - temporary_relatives
    if pending_without_temporary and (not allow_pending_endpoints or not pending_without_temporary.issubset(set(plan["changed_paths"]))):
        raise BoundaryError("completed metadata-policy commit has unrelated pending work")
    return head, tree


def policy_migration_apply(args: argparse.Namespace) -> dict[str, Any]:
    if not args.authorize:
        raise BoundaryError("metadata-policy migration apply requires --authorize-metadata-policy-migration")
    reject_git_environment()
    plan_path = args.plan.expanduser().resolve(); plan, plan_hash = validate_policy_migration_plan(plan_path)
    root = exact_physical_controller(Path(plan["root"])); common = Path(common_dir(root))
    index_identity_path = Path(git(root, "rev-parse", "--path-format=absolute", "--git-path", "index"))
    root_stat = root.stat()
    if (str(common) != plan.get("git_common_dir")
            or str(index_identity_path) != plan.get("index_path")
            or [root_stat.st_dev, root_stat.st_ino, root_stat.st_mode] != plan.get("root_identity")
            or list(common.stat()[:3]) != plan.get("git_common_identity")):
        raise BoundaryError("metadata-policy migration repository identity differs from the reviewed plan")
    output = external_config_repair_receipt(args.output, root, common)
    plan_file_hash = file_digest(plan_path)
    if not plan["changed_paths"]:
        with acquire_policy_migration_locks(common, plan["branch"]) as verify_locks:
            verify_locks(); assert_policy_plan_snapshot(plan)
            payload = {"schema_version": POLICY_MIGRATION_SCHEMA, "operation": "metadata-policy-migration-apply",
                       "outcome": "already_migrated_noop", "plan_sha256": plan_hash,
                       "plan_file_sha256": plan_file_hash, "controller": str(root), "head": plan["head"],
                       "changed_paths": [], "commit_created": False, "clean": True}
            atomic_receipt(output, payload)
        return payload
    intent_path = external_config_repair_receipt(output.with_name(output.name + ".intent.json"), root, common)
    intent_payload = {"schema_version": POLICY_MIGRATION_SCHEMA, "operation": "metadata-policy-migration-intent",
                      "outcome": "intent_persisted_before_mutation", "plan_sha256": plan_hash,
                      "plan_file_sha256": plan_file_hash, "expected_head": plan["head"],
                      "changed_paths": plan["changed_paths"], "commit_intent": plan["result_tree_commit_intent"]}
    preflight_receipt(intent_path, intent_payload); atomic_receipt(intent_path, intent_payload)
    committed: str | None = None; result_tree: str | None = None
    with acquire_policy_migration_locks(common, plan["branch"]) as verify_locks:
        verify_locks()
        expected_index = Path(git(root, "rev-parse", "--path-format=absolute", "--git-path", "index"))
        recovery_lock = Path(str(expected_index) + ".lock")
        ownership_path = index_lock_ownership_path(common, plan_hash)
        if recovery_lock.exists() or recovery_lock.is_symlink():
            # Recovery requires the exact durable marker and filesystem identity
            # written with this plan's prepared index. Tree equality alone cannot
            # distinguish an active ordinary Git process.
            completed_probe = completed_policy_migration(root, plan, plan_hash, allow_pending_endpoints=True)
            expected_result_tree = completed_probe[1] if completed_probe is not None \
                else policy_result_tree(root, plan)
            prepared_still_locked = (exact_prepared_index(
                    root, recovery_lock, ownership_path, plan_hash, expected_result_tree)
                    and exact_displaced_index(expected_index, ownership_path))
            published_with_displaced_lock = (completed_probe is not None
                    and exact_prepared_index(root, expected_index, ownership_path,
                                             plan_hash, expected_result_tree)
                    and exact_displaced_index(recovery_lock, ownership_path))
            if not (prepared_still_locked or published_with_displaced_lock):
                raise BoundaryError(f"metadata-policy migration index serialization is busy or unowned: {recovery_lock}")
            lock_expected = index_lock_identity(recovery_lock)
            marker_expected = index_lock_identity(ownership_path)
            if lock_expected is None or marker_expected is None:
                raise BoundaryError("metadata-policy recovery evidence changed before cleanup")
            exact_unlink_path(recovery_lock, lock_expected)
            exact_unlink_path(ownership_path, marker_expected)
        completed = completed_policy_migration(root, plan, plan_hash, allow_pending_endpoints=True)
        if ownership_path.exists() or ownership_path.is_symlink():
            # A crash after publishing the prepared index may leave only its
            # marker. Reclaim it solely after exact completed-state/index proof.
            if (completed is None or ownership_path.is_symlink()
                    or not exact_prepared_index(root, expected_index, ownership_path,
                                                plan_hash, completed[1])):
                raise BoundaryError(f"metadata-policy migration index ownership is stranded or unowned: {ownership_path}")
            marker_expected = index_lock_identity(ownership_path)
            if marker_expected is None:
                raise BoundaryError("metadata-policy index ownership changed before cleanup")
            exact_unlink_path(ownership_path, marker_expected)
        if completed is None:
            if output.exists() or output.is_symlink():
                raise BoundaryError("metadata-policy apply receipt path must be fresh before mutation")
            root, _ = assert_policy_plan_snapshot(plan)
        else:
            committed, result_tree = completed
        if completed is None:
            pause = os.environ.get("JUNO_METADATA_POLICY_MIGRATION_TEST_PAUSE_FILE")
            if pause:
                ready = Path(pause + ".ready"); release = Path(pause + ".release")
                publish_test_ready(ready, "ready\n")
                for _ in range(500):
                    if release.exists(): break
                    import time; time.sleep(0.01)
                else: raise BoundaryError("metadata-policy migration test pause timed out")
                assert_policy_plan_snapshot(plan)
        with tempfile.TemporaryDirectory(prefix="juno-policy-migration-index-") as temporary:
            temporary_index = Path(temporary) / "index"
            index_env = {"GIT_INDEX_FILE": str(temporary_index)}
            git(root, "read-tree", plan["head"], env=index_env)
            add_blob(root, index_env, POLICY_PATH, plan["policy_result_utf8"].encode("utf-8"))
            add_blob(root, index_env, INTEGRATION_POLICY_PATH,
                     plan["source"]["integration_source_utf8"].encode("utf-8"))
            result_tree = git(root, "write-tree", env=index_env)
            changed = git(root, "diff-tree", "--no-commit-id", "--name-only", "-r",
                          plan["tree"], result_tree).splitlines()
            if changed != plan["changed_paths"]:
                raise BoundaryError("metadata-policy migration result tree escaped declared paths")
            index_path = Path(git(root, "rev-parse", "--path-format=absolute", "--git-path", "index"))
            index_lock = Path(str(index_path) + ".lock")
            try:
                with index_lock.open("xb") as stream:
                    stream.write(temporary_index.read_bytes()); stream.flush(); os.fsync(stream.fileno())
            except FileExistsError as exc:
                raise BoundaryError(f"metadata-policy migration index serialization is busy: {index_lock}") from exc
            try:
                expected_real_index_identity = index_lock_identity(index_path)
                if expected_real_index_identity is None:
                    raise BoundaryError("metadata-policy migration cannot bind the real index bytes")
                ownership_path = persist_index_lock_ownership(
                    common, plan_hash, index_lock, result_tree, index_path)
            except BaseException:
                durable_unlink(index_lock)
                raise
            index_published = False
            config_fd, config_identity = open_config_directory(root)
            quarantine_dir = common / "juno-metadata-policy-quarantine"
            quarantine_dir.mkdir(mode=0o700, exist_ok=True)
            quarantine_fd = os.open(quarantine_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                                    | getattr(os, "O_NOFOLLOW", 0))
            if os.fstat(quarantine_fd).st_dev != os.fstat(config_fd).st_dev:
                os.close(config_fd); os.close(quarantine_fd)
                raise BoundaryError(
                    "metadata-policy migration requires config and durable quarantine on one filesystem")
            endpoint_payloads = tuple(
                (relative, Path(relative).name, data,
                 migration_temporary_endpoints(root, plan_hash)[index].name)
                for index, (relative, data) in enumerate((
                    (POLICY_PATH, plan["policy_result_utf8"].encode("utf-8")),
                    (INTEGRATION_POLICY_PATH, plan["source"]["integration_source_utf8"].encode("utf-8")),
                )))
            try:
                # Refuse or reclaim only exact plan-owned endpoint temporaries
                # before publishing the commit. The open directory descriptor
                # fixes the reviewed ancestor across all later operations.
                for relative, name, data, temporary_name in endpoint_payloads:
                    current = endpoint_snapshot_at(config_fd, name)
                    current_bytes = current[0] if current is not None else None
                    before = plan["policy_before_utf8"].encode("utf-8") if relative == POLICY_PATH else None
                    if current_bytes not in {None, before, data}:
                        raise BoundaryError(f"metadata-policy endpoint changed after final snapshot: {relative}")
                    temporary = endpoint_snapshot_at(config_fd, temporary_name)
                    if temporary is not None:
                        recoverable_bytes = {data}
                        if completed is not None and before is not None:
                            # A crash immediately after endpoint exchange leaves
                            # the exact retired preimage at the temporary name.
                            recoverable_bytes.add(before)
                        if temporary[0] not in recoverable_bytes:
                            raise BoundaryError(f"metadata-policy migration temporary collision: {temporary_name}")
                        exact_unlink_endpoint_at(config_fd, temporary_name, temporary, quarantine_fd)
                os.fsync(config_fd)
                if completed is None:
                    assert_policy_plan_snapshot(plan, owned_index_lock=True)
                    index_pause = os.environ.get("JUNO_METADATA_POLICY_MIGRATION_TEST_INDEX_PAUSE_FILE")
                    if index_pause:
                        ready = Path(index_pause + ".ready"); release = Path(index_pause + ".release")
                        publish_test_ready(ready, "ready\n")
                        for _ in range(500):
                            if release.exists(): break
                            import time; time.sleep(0.01)
                        else: raise BoundaryError("metadata-policy migration index test pause timed out")
                        assert_policy_plan_snapshot(plan, owned_index_lock=True)
                    message = plan["result_tree_commit_intent"]["message"].format(plan_sha256=plan_hash)
                    identity = plan["result_tree_commit_intent"]["identity"]
                    commit_env = {"GIT_AUTHOR_NAME": identity["author_name"],
                                  "GIT_AUTHOR_EMAIL": identity["author_email"],
                                  "GIT_AUTHOR_DATE": identity["author_date"],
                                  "GIT_COMMITTER_NAME": identity["committer_name"],
                                  "GIT_COMMITTER_EMAIL": identity["committer_email"],
                                  "GIT_COMMITTER_DATE": identity["committer_date"]}
                    committed = run(["git", "-C", str(root), "commit-tree", result_tree, "-p", plan["head"], "-m", message],
                                    root, env=commit_env).stdout.strip()
                    verify_locks()
                    update = run(["git", "-C", str(root), "update-ref", "-m", "juno metadata-policy migration",
                                  plan["branch"], committed, plan["head"]], root, False)
                    if update.returncode:
                        committed = None
                        raise BoundaryError("metadata-policy migration HEAD CAS failed before commit publication")
                try:
                    for relative, name, data, temporary_name in endpoint_payloads:
                        descriptor = os.open(temporary_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                             0o600, dir_fd=config_fd)
                        try:
                            write_all(descriptor, data); os.fsync(descriptor)
                        finally: os.close(descriptor)
                        before_snapshot = endpoint_snapshot_at(config_fd, name)
                        current = before_snapshot[0] if before_snapshot is not None else None
                        before = plan["policy_before_utf8"].encode("utf-8") if relative == POLICY_PATH else None
                        if current not in {None, before, data}:
                            owned_temporary = endpoint_snapshot_at(config_fd, temporary_name)
                            if owned_temporary is None:
                                raise BoundaryError(f"metadata-policy endpoint temporary disappeared: {temporary_name}")
                            exact_unlink_endpoint_at(config_fd, temporary_name, owned_temporary, quarantine_fd)
                            raise BoundaryError(f"metadata-policy endpoint raced before publication: {relative}")
                        race_pause = os.environ.get("JUNO_METADATA_POLICY_MIGRATION_TEST_ENDPOINT_PAUSE_FILE")
                        if race_pause:
                            ready = Path(race_pause + ".ready"); release = Path(race_pause + ".release")
                            publish_test_ready(ready, relative + "\n")
                            for _ in range(500):
                                if release.exists(): break
                                import time; time.sleep(0.01)
                            else: raise BoundaryError("metadata-policy endpoint race test pause timed out")
                        final_snapshot = endpoint_snapshot_at(config_fd, name)
                        if final_snapshot != before_snapshot:
                            owned_temporary = endpoint_snapshot_at(config_fd, temporary_name)
                            if owned_temporary is None:
                                raise BoundaryError(f"metadata-policy endpoint temporary disappeared: {temporary_name}")
                            exact_unlink_endpoint_at(config_fd, temporary_name, owned_temporary, quarantine_fd)
                            raise BoundaryError(f"metadata-policy endpoint raced before atomic publication: {relative}")
                        atomic_endpoint_publish(config_fd, temporary_name, name, before_snapshot, quarantine_fd)
                    os.fsync(config_fd)
                    current_config_fd, current_config_identity = open_config_directory(root)
                    os.close(current_config_fd)
                    if current_config_identity != config_identity:
                        raise BoundaryError("metadata-policy config directory changed during publication")
                    verify_locks()
                    atomic_index_publish(index_lock, index_path, expected_real_index_identity)
                    index_published = True
                    marker_expected = index_lock_identity(ownership_path)
                    if marker_expected is None:
                        raise BoundaryError("metadata-policy index ownership changed before final cleanup")
                    exact_unlink_path(ownership_path, marker_expected)
                    if git(root, "rev-parse", "HEAD") != committed or git(root, "rev-parse", "HEAD^{tree}") != result_tree:
                        raise BoundaryError("HEAD/tree readback failed")
                    if git(root, "diff", "--name-only", plan["head"], committed).splitlines() != plan["changed_paths"]:
                        raise BoundaryError("commit has an unexpected path set")
                    if git(root, "status", "--porcelain=v2", "--untracked-files=all", check=False):
                        raise BoundaryError("controller is not clean")
                    migrated = load_policy(root / POLICY_PATH)
                    validate_integration_policy(json.loads((root / INTEGRATION_POLICY_PATH).read_bytes()),
                                                validate_task_policy(json.loads((root / TASK_POLICY_PATH).read_bytes())))
                    if migrated["controller_branch"] != plan["branch"] or migrated["product_ref"] != plan["product_ref"]:
                        raise BoundaryError("topology parity failed")
                    verified = completed_policy_migration(root, plan, plan_hash, allow_pending_endpoints=False)
                    if verified != (committed, result_tree):
                        raise BoundaryError("authority and commit readback changed after publication")
                except BaseException as exc:
                    failure = {"schema_version": POLICY_MIGRATION_SCHEMA, "operation": "metadata-policy-migration-apply",
                               "outcome": "committed_postcondition_failed", "plan_sha256": plan_hash,
                               "controller": str(root), "old_head": plan["head"], "new_head": committed,
                               "result_tree": result_tree, "changed_paths": plan["changed_paths"],
                               "commit_created": True, "index_lock_preserved": index_lock.exists(),
                               "error": str(exc)}
                    receipt_error: BaseException | None = None
                    try: atomic_receipt(output, failure)
                    except BaseException as failure_receipt_error: receipt_error = failure_receipt_error
                    detail = f"; failure receipt emission also failed: {receipt_error}" if receipt_error else f"; receipt: {output}"
                    raise BoundaryError(
                        f"metadata-policy migration commit {committed} is durable but post-commit readback failed: {exc}{detail}"
                    ) from exc
            finally:
                os.close(config_fd)
                os.close(quarantine_fd)
                if committed is None and not index_published:
                    durable_unlink(index_lock)
                    if ownership_path.exists() and not ownership_path.is_symlink():
                        durable_unlink(ownership_path)
    payload = {"schema_version": POLICY_MIGRATION_SCHEMA, "operation": "metadata-policy-migration-apply",
               "outcome": "migrated", "plan_sha256": plan_hash, "plan_file_sha256": plan_file_hash,
               "intent_sha256": file_digest(intent_path), "controller": str(root), "branch": plan["branch"],
               "old_head": plan["head"], "new_head": committed, "result_tree": result_tree,
               "changed_paths": plan["changed_paths"], "semantic_additions": plan["semantic_additions"],
               "commit_created": True, "commit_parent_verified": True, "tree_binding_verified": True,
               "task_policy_sha256": plan["task_policy_sha256"], "risk_policy_sha256": plan["risk_policy_sha256"],
               "product_ref": plan["product_ref"], "product_head": plan["product_head"],
               "product_ref_mutation": False, "clean": True}
    try:
        atomic_receipt(output, payload)
    except BaseException as exc:
        raise BoundaryError(
            f"metadata-policy migration commit {committed} is durable and verified, but receipt emission failed: {output}"
        ) from exc
    return payload


def agent_surface_repair_plan(args: argparse.Namespace, policy: dict[str, Any]) -> dict[str, Any]:
    root = exact_worktree(args.root)
    branch = safe_ref(args.branch, "branch"); product_ref = safe_ref(args.product_ref, "product_ref")
    if branch != policy["controller_branch"] or product_ref != policy["product_ref"]:
        raise BoundaryError("controller/product refs do not match the reviewed metadata policy")
    if args.disposition not in {"retire", "externalize"}:
        raise BoundaryError("agent-surface repair requires reviewed disposition retire or externalize")
    if git(root, "symbolic-ref", "-q", "HEAD", check=False) != branch:
        raise BoundaryError("agent-surface repair requires the exact attached metadata controller branch")
    if git(root, "status", "--porcelain=v2", "--untracked-files=all", check=False):
        raise BoundaryError("agent-surface repair requires a clean controller")
    head = resolve_commit(root, branch, args.expected_head, "controller ref")
    product_head = resolve_commit(root, product_ref, args.expected_product_head, "product target")
    entries = [{"mode": mode, "oid": oid, "path": name} for mode, oid, name in listed_tree(root, head)
               if agent_surface_path(name)]
    if not entries:
        raise BoundaryError("agent-surface repair found no tracked instruction or skill evidence")
    if any(entry["mode"] not in {"100644", "100755"} for entry in entries):
        raise BoundaryError("agent-surface repair refuses symlink or gitlink evidence")
    _, missing_ignores = committed_gitignore(root, head)
    if missing_ignores:
        raise BoundaryError("agent-surface repair requires committed root ignore coverage: " + ", ".join(missing_ignores))
    require_canonical_controller_config(root, head)
    inspection = inspect(root, policy, expected_branch=branch, require_active=False)
    invalid = [name for name, passed in inspection["checks"].items()
               if not passed and name not in agent_surface_repair_tolerances(root)]
    if invalid:
        raise BoundaryError("agent-surface repair refuses unrelated controller defects: " + ", ".join(invalid))
    non_agent_forbidden = [name for name in inspection["forbidden_tracked"] if not agent_surface_path(name)]
    if non_agent_forbidden:
        raise BoundaryError("agent-surface repair refuses unrelated tracked paths: " + ", ".join(non_agent_forbidden))
    output = external_config_repair_receipt(args.output, root, Path(common_dir(root)))
    core = {"schema_version": AGENT_SURFACE_REPAIR_SCHEMA, "operation": "agent-surface-repair",
            "outcome": "planned_no_mutation", "controller": str(root), "branch": branch,
            "head": head, "tree": git(root, "rev-parse", f"{head}^{{tree}}"),
            "git_common_dir": common_dir(root), "product_ref": product_ref, "product_head": product_head,
            "policy_sha256": digest(policy), "reviewed_disposition": args.disposition,
            "evidence": {"entries": entries, "sha256": digest(entries),
                         "preserved_in_parent_commit": True},
            "changes": {"remove": [entry["path"] for entry in entries]},
            "apply_authorized": False, "product_ref_mutation": False}
    payload = {**core, "plan_sha256": digest(core)}
    atomic_receipt(output, payload)
    return payload


def validate_agent_surface_repair_plan(path: Path) -> tuple[dict[str, Any], str]:
    plan = read_json(path, "agent-surface repair plan"); plan_hash = plan.pop("plan_sha256", None)
    entries = plan.get("evidence", {}).get("entries") if isinstance(plan.get("evidence"), dict) else None
    removals = plan.get("changes", {}).get("remove") if isinstance(plan.get("changes"), dict) else None
    if (plan.get("schema_version") != AGENT_SURFACE_REPAIR_SCHEMA
            or plan.get("operation") != "agent-surface-repair" or plan.get("outcome") != "planned_no_mutation"
            or plan.get("apply_authorized") is not False or plan.get("product_ref_mutation") is not False
            or plan.get("reviewed_disposition") not in {"retire", "externalize"}
            or not isinstance(entries, list) or not entries
            or plan.get("evidence", {}).get("sha256") != digest(entries)
            or removals != [entry.get("path") for entry in entries]
            or any(set(entry) != {"mode", "oid", "path"} or not agent_surface_path(entry["path"])
                   or entry["mode"] not in {"100644", "100755"} for entry in entries)
            or plan_hash != digest(plan)):
        raise BoundaryError("agent-surface repair requires an exact hash-bound reviewed plan")
    return plan, plan_hash


def agent_surface_repair_state(root: Path, plan: dict[str, Any], plan_hash: str,
                               policy: dict[str, Any]) -> tuple[str, str]:
    if common_dir(root) != plan["git_common_dir"] or digest(policy) != plan["policy_sha256"]:
        raise BoundaryError("agent-surface repair repository or reviewed policy changed after planning")
    if git(root, "symbolic-ref", "-q", "HEAD", check=False) != plan["branch"]:
        raise BoundaryError("agent-surface repair controller branch changed after planning")
    if git(root, "status", "--porcelain=v2", "--untracked-files=all", check=False):
        raise BoundaryError("agent-surface repair requires a clean controller")
    resolve_commit(root, plan["product_ref"], plan["product_head"], "product target")
    head = git(root, "rev-parse", "HEAD")
    if head == plan["head"]:
        actual = [{"mode": mode, "oid": oid, "path": name} for mode, oid, name in listed_tree(root, head)
                  if agent_surface_path(name)]
        if git(root, "rev-parse", "HEAD^{tree}") != plan["tree"] or actual != plan["evidence"]["entries"]:
            raise BoundaryError("tracked agent-surface evidence changed after planning")
        return "before", head
    expected_message = f"Evacuate tracked controller agent surface\n\nJuno-Agent-Surface-Repair-Plan: {plan_hash}"
    changed = git(root, "diff", "--name-only", plan["head"], head).splitlines()
    if (git(root, "rev-parse", f"{head}^") == plan["head"]
            and git(root, "show", "-s", "--format=%B", head) == expected_message
            and changed == plan["changes"]["remove"]
            and not any(agent_surface_path(name) for _, _, name in listed_tree(root, head))):
        return "after", head
    raise BoundaryError("controller is neither the frozen agent-surface state nor its exact completed repair")


def agent_surface_repair_apply(args: argparse.Namespace, policy: dict[str, Any]) -> dict[str, Any]:
    if not args.authorize:
        raise BoundaryError("agent-surface repair apply requires --authorize-agent-surface-repair")
    plan_path = args.plan.expanduser().resolve(); plan, plan_hash = validate_agent_surface_repair_plan(plan_path)
    root = exact_worktree(Path(plan["controller"])); common = Path(plan["git_common_dir"])
    output = external_config_repair_receipt(args.output, root, common)
    with config_repair_lock(common):
        state, head = agent_surface_repair_state(root, plan, plan_hash, policy)
        if state == "before":
            if output.exists(): raise BoundaryError("agent-surface repair receipt path must be fresh before mutation")
            git(root, "rm", "-r", "--", *sorted({entry["path"].split("/", 1)[0] for entry in plan["evidence"]["entries"]}))
            if git(root, "diff", "--cached", "--name-only").splitlines() != plan["changes"]["remove"]:
                git(root, "restore", "--staged", "--worktree", "--source", plan["head"], "--", *AGENT_SURFACE_ROOTS, check=False)
                raise BoundaryError("agent-surface repair staged paths outside the reviewed evidence")
            commit_env = {"GIT_AUTHOR_NAME": "Juno Controller Migration", "GIT_AUTHOR_EMAIL": "juno-controller@local.invalid",
                          "GIT_COMMITTER_NAME": "Juno Controller Migration", "GIT_COMMITTER_EMAIL": "juno-controller@local.invalid"}
            try:
                run(["git", "-C", str(root), "commit", "-m", "Evacuate tracked controller agent surface",
                     "-m", f"Juno-Agent-Surface-Repair-Plan: {plan_hash}"], root, env=commit_env)
            except BaseException:
                if git(root, "rev-parse", "HEAD") == plan["head"]:
                    git(root, "restore", "--staged", "--worktree", "--source", plan["head"], "--", *AGENT_SURFACE_ROOTS, check=False)
                raise
            state, head = agent_surface_repair_state(root, plan, plan_hash, policy)
        if state != "after": raise BoundaryError("agent-surface repair did not reach its exact intended state")
    evidence = inspect(root, policy, expected_branch=plan["branch"], require_active=False)
    if not evidence["checks"]["agent_surface_untracked"] or not evidence["checks"]["root_agent_ignores"]:
        raise BoundaryError("agent-surface repair postcondition verification failed")
    payload = {"schema_version": AGENT_SURFACE_REPAIR_SCHEMA, "operation": "agent-surface-repair-apply",
               "outcome": "repaired", "plan_sha256": plan_hash, "plan_file_sha256": file_digest(plan_path),
               "controller": str(root), "branch": plan["branch"], "old_head": plan["head"], "new_head": head,
               "reviewed_disposition": plan["reviewed_disposition"], "removed_paths": plan["changes"]["remove"],
               "evidence_preserved_in_parent_commit": True, "product_ref": plan["product_ref"],
               "product_head": plan["product_head"], "product_ref_mutation": False}
    atomic_receipt(output, payload); return payload


def agent_surface_repair_verify(args: argparse.Namespace, policy: dict[str, Any]) -> dict[str, Any]:
    plan_path = args.plan.expanduser().resolve(); plan, plan_hash = validate_agent_surface_repair_plan(plan_path)
    root = exact_worktree(Path(plan["controller"])); output = external_config_repair_receipt(args.output, root, Path(plan["git_common_dir"]))
    state, head = agent_surface_repair_state(root, plan, plan_hash, policy)
    evidence = inspect(root, policy, expected_branch=plan["branch"], require_active=False)
    intolerable = [name for name, passed in evidence["checks"].items()
                   if not passed and name not in agent_surface_repair_tolerances(root)]
    if state != "after" or intolerable:
        raise BoundaryError("agent-surface repair verification refused: " + ", ".join(intolerable))
    payload = {"schema_version": AGENT_SURFACE_REPAIR_SCHEMA, "operation": "agent-surface-repair-verify",
               "outcome": "verified", "plan_sha256": plan_hash, "controller": str(root), "head": head,
               "evidence_preserved_in_parent_commit": True, "checks": evidence["checks"], "passed": True}
    atomic_receipt(output, payload); return payload


def resolved_registry_artifact(npm: str, package_spec: str, version: str,
                               destination: Path, root: Path) -> dict[str, Any]:
    """Fetch one immutable tarball and verify npm's exact package digest evidence."""
    result = run([npm, "pack", package_spec, "--json", "--pack-destination", str(destination)],
                 root, False)
    if result.returncode:
        raise BoundaryError(
            f"exact runtime artifact is unavailable for {package_spec}: "
            f"{result.stderr.strip() or result.stdout.strip() or 'npm pack failed'}")
    try:
        rows = json.loads(result.stdout)
        row = rows[0] if isinstance(rows, list) and len(rows) == 1 else None
    except json.JSONDecodeError as exc:
        raise BoundaryError("registry returned invalid exact runtime artifact evidence") from exc
    if (not isinstance(row, dict) or row.get("version") != version
            or not isinstance(row.get("filename"), str)
            or not isinstance(row.get("integrity"), str)
            or not isinstance(row.get("shasum"), str)):
        raise BoundaryError("registry artifact identity does not match the requested exact release")
    tarball = (destination / row["filename"]).resolve()
    try:
        tarball.relative_to(destination.resolve())
    except ValueError as exc:
        raise BoundaryError("registry artifact filename escapes the download directory") from exc
    if tarball.is_symlink() or not tarball.is_file():
        raise BoundaryError("registry artifact tarball is missing or unsafe")
    data = tarball.read_bytes()
    algorithm, separator, encoded = row["integrity"].partition("-")
    if algorithm != "sha512" or not separator:
        raise BoundaryError("registry artifact lacks supported sha512 integrity evidence")
    try:
        expected = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise BoundaryError("registry artifact integrity evidence is malformed") from exc
    if hashlib.sha512(data).digest() != expected or hashlib.sha1(data).hexdigest() != row["shasum"]:
        raise BoundaryError("downloaded exact runtime artifact failed integrity verification")
    return {"source": "registry", "package": "@yylo/cli", "package_spec": package_spec,
            "version": version, "integrity": row["integrity"], "shasum": row["shasum"],
            "sha256": hashlib.sha256(data).hexdigest(),
            "tarball_sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data),
            "tarball": str(tarball)}


def _outside_git_artifact_path(supplied: Path, repository: Path) -> Path:
    lexical = supplied.expanduser()
    if not lexical.is_absolute():
        lexical = Path.cwd() / lexical
    lexical = Path(os.path.abspath(lexical))
    try:
        mode = os.lstat(lexical).st_mode
    except OSError as exc:
        raise BoundaryError(f"local runtime artifact is unavailable: {exc}") from exc
    if not stat.S_ISREG(mode):
        raise BoundaryError("local runtime artifact must be one regular non-symlink npm pack tarball")
    artifact = lexical.resolve(strict=True)
    repo = repository.resolve()
    prohibited = {repo, Path(common_dir(repo)).resolve()}
    listing = run(["git", "-C", str(repo), "worktree", "list", "--porcelain", "-z"], repo)
    prohibited.update(Path(record.removeprefix("worktree ")).resolve()
                      for record in listing.stdout.split("\0") if record.startswith("worktree "))
    for protected in prohibited:
        try:
            artifact.relative_to(protected)
        except ValueError:
            continue
        raise BoundaryError("local runtime artifact must be outside every Git worktree and administration directory")
    if git(artifact.parent, "rev-parse", "--show-toplevel", check=False):
        raise BoundaryError("local runtime artifact must be outside every mutable Git worktree or Git ancestor")
    return artifact


def _authenticate_npm_pack(data: bytes, version: str) -> None:
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            manifest_member: tarfile.TarInfo | None = None
            count = 0
            expanded = 0
            for member in archive:
                count += 1
                expanded += max(member.size, 0)
                if count > MAX_RUNTIME_ARCHIVE_MEMBERS or expanded > MAX_RUNTIME_ARCHIVE_EXPANDED_BYTES:
                    raise BoundaryError("local runtime artifact archive exceeds bounded package limits")
                path = PurePosixPath(member.name)
                if (path.is_absolute() or ".." in path.parts or not path.parts
                        or path.parts[0] != "package" or "\\" in member.name
                        or member.issym() or member.islnk()):
                    raise BoundaryError("local runtime artifact contains an unsafe npm package entry")
                if path == PurePosixPath("package/package.json"):
                    if manifest_member is not None or not member.isfile() or member.size > MAX_RUNTIME_MANIFEST_BYTES:
                        raise BoundaryError("local runtime artifact has an invalid package manifest entry")
                    manifest_member = member
            if manifest_member is None:
                raise BoundaryError("local runtime artifact is missing package/package.json")
            stream = archive.extractfile(manifest_member)
            manifest_data = stream.read(MAX_RUNTIME_MANIFEST_BYTES + 1) if stream else b""
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise BoundaryError(f"local runtime artifact is not a valid npm pack tarball: {exc}") from exc
    try:
        manifest = json.loads(manifest_data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BoundaryError("local runtime artifact package manifest is malformed") from exc
    if (not isinstance(manifest, dict) or manifest.get("name") != "@yylo/cli"
            or manifest.get("version") != version or not valid_semver(manifest.get("version"))):
        raise BoundaryError("local runtime artifact package name/version does not match requested yylo release")


def authenticate_local_runtime_artifact(supplied: Path, version: str,
                                        repository: Path) -> tuple[dict[str, Any], bytes]:
    artifact = _outside_git_artifact_path(supplied, repository)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(artifact, flags)
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_size <= 0
                    or before.st_size > MAX_LOCAL_RUNTIME_ARTIFACT_BYTES):
                raise BoundaryError("local runtime artifact size is outside bounded package limits")
            data = stream.read(MAX_LOCAL_RUNTIME_ARTIFACT_BYTES + 1)
            after = os.fstat(stream.fileno())
    except OSError as exc:
        raise BoundaryError(f"local runtime artifact could not be read safely: {exc}") from exc
    if (len(data) != before.st_size or len(data) > MAX_LOCAL_RUNTIME_ARTIFACT_BYTES
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
        raise BoundaryError("local runtime artifact changed while it was being authenticated")
    _authenticate_npm_pack(data, version)
    evidence = {"source": "local", "path": str(artifact), "package": "@yylo/cli",
                "version": version, "sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data)}
    return evidence, data


def verify_local_runtime_artifact(evidence: dict[str, Any], repository: Path) -> bytes:
    current, data = authenticate_local_runtime_artifact(Path(evidence["path"]), evidence["version"], repository)
    if current != evidence:
        raise BoundaryError("local runtime artifact bytes or identity changed after authentication")
    return data


def bounded_runtime_error(exc: BaseException) -> str:
    detail = str(exc)
    detail = re.sub(r"(?i)(authorization|token|password|secret)(\s*[:=]\s*)\S+", r"\1\2[REDACTED]", detail)
    return detail[-2000:]


def runtime_install_rebind(args: argparse.Namespace, policy: dict[str, Any]) -> dict[str, Any]:
    """Install one authenticated release tarball into a fresh prefix, then bind it."""
    if not valid_semver(args.runtime_version):
        raise BoundaryError("runtime version must be an exact released semantic version")
    prefix = args.install_prefix.expanduser().resolve()
    root = exact_worktree(args.root)
    expected_branch = safe_ref(args.branch, "branch")
    local_artifact = getattr(args, "artifact", None)
    output = external_config_repair_receipt(args.output, root, Path(common_dir(root)))
    if output.is_file() and not output.is_symlink():
        prior = read_json(output, "runtime install/rebind receipt")
        if (prior.get("schema_version") == RECEIPT_SCHEMA
                and prior.get("operation") == "runtime-install-rebind"
                and prior.get("outcome") == "exact_runtime_installed_and_rebound"
                and prior.get("root") == str(root)
                and prior.get("branch") == expected_branch
                and prior.get("runtime", {}).get("version") == args.runtime_version
                and prior.get("install_prefix") in (None, str(prefix))):
            prior_artifact = prior.get("artifact", {})
            if local_artifact is not None:
                supplied, _ = authenticate_local_runtime_artifact(local_artifact, args.runtime_version, root)
                if supplied != prior_artifact:
                    raise BoundaryError("completed receipt does not match the supplied local runtime artifact")
            elif prior_artifact.get("source") == "local":
                raise BoundaryError("completed local-artifact receipt requires the same --artifact input for replay")
            executable = Path(prior["runtime"]["executable"])
            expected_root = prefix / "node_modules/@yylo/cli"
            try:
                executable.resolve().relative_to(expected_root.resolve())
            except ValueError as exc:
                raise BoundaryError("completed runtime receipt is outside its exact install prefix") from exc
            identity = runtime_identity(executable, args.runtime_version, root)
            manifest_path = expected_root / "package.json"
            installation = prior.get("installation", {})
            manifest = read_json(manifest_path, "installed yylo package")
            manifest_matches = (manifest.get("name") == "@yylo/cli"
                                and manifest.get("version") == args.runtime_version)
            if installation:
                manifest_matches = (manifest_matches
                                    and file_digest(manifest_path) == installation.get("package_manifest_sha256"))
            if identity != prior["runtime"] or not manifest_matches:
                raise BoundaryError("completed runtime receipt installed identity has drifted")
            if (git(root, "config", "--worktree", "--get", "juno.controller.runtimeVersion") != args.runtime_version
                    or git(root, "config", "--worktree", "--get", "juno.controller.runtimeExecutable") != str(executable.resolve())):
                raise BoundaryError("completed runtime receipt no longer matches controller registration")
            return prior
        raise BoundaryError(f"prior runtime install/rebind attempt is terminal; use a fresh receipt path: {output}")
    if prefix.exists() or prefix.is_symlink():
        raise BoundaryError(f"runtime install prefix must be fresh and absent: {prefix}")
    for protected, label in ((root, "controller worktree"), (Path(common_dir(root)).resolve(), "controller Git administration directory")):
        try:
            prefix.relative_to(protected)
        except ValueError:
            continue
        raise BoundaryError(f"runtime install prefix must be outside the {label}: {prefix}")
    if output.exists() or output.is_symlink():
        raise BoundaryError(f"immutable receipt collision: {output}")
    if git(root, "symbolic-ref", "-q", "HEAD", check=False) != expected_branch:
        raise BoundaryError("runtime install/rebind refused for wrong controller branch")
    if git(root, "config", "--worktree", "--get", "juno.controller.mode", check=False) != "metadata-only":
        raise BoundaryError("runtime install/rebind refused for non-metadata controller")
    if git(root, "status", "--porcelain=v2", "--untracked-files=all", check=False):
        raise BoundaryError("runtime install/rebind requires a clean metadata controller")
    npm = shutil.which("npm")
    if not npm:
        raise BoundaryError("npm is required to install the exact yylo release")
    before = {"head": git(root, "rev-parse", "HEAD"), "tree": git(root, "write-tree"),
              "runtime_version": git(root, "config", "--worktree", "--get", "juno.controller.runtimeVersion", check=False),
              "runtime_executable": git(root, "config", "--worktree", "--get", "juno.controller.runtimeExecutable", check=False)}
    runtime_file = root / policy["runtime"]["identity_file"]
    before_identity = runtime_file.read_bytes() if runtime_file.exists() else None
    package_spec = f"@yylo/cli@{args.runtime_version}"
    artifact: dict[str, Any] | None = None
    installation: dict[str, Any] | None = None
    installed_runtime: dict[str, str] | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="juno-runtime-artifact-") as temporary:
            temporary_root = Path(temporary)
            if local_artifact is not None:
                artifact, authenticated = authenticate_local_runtime_artifact(local_artifact, args.runtime_version, root)
                snapshot = temporary_root / "yylo-authenticated.tgz"
                snapshot.write_bytes(authenticated)
                # Reauthenticate the owner-visible file immediately before any prefix mutation.
                verify_local_runtime_artifact(artifact, root)
                tarball = snapshot
            else:
                resolved = resolved_registry_artifact(npm, package_spec, args.runtime_version, temporary_root, root)
                artifact = {key: value for key, value in resolved.items() if key != "tarball"}
                tarball = Path(resolved["tarball"])
            install_args = [npm, "install", "--prefix", str(prefix), "--ignore-scripts", "--no-audit",
                            "--no-fund", "--package-lock=false", "--save=false", "--exact"]
            if local_artifact is not None:
                install_args.append("--offline")
            install_args.append(str(tarball))
            result = run(install_args, root, False)
            if result.returncode:
                raise BoundaryError(
                    f"exact runtime installation failed for verified {package_spec}: "
                    f"{result.stderr.strip() or result.stdout.strip() or 'npm failed'}")
            package_root = prefix / "node_modules/@yylo/cli"
            manifest_path = package_root / "package.json"
            manifest = read_json(manifest_path, "installed yylo package")
            if manifest.get("name") != "@yylo/cli" or manifest.get("version") != args.runtime_version:
                raise BoundaryError("installed package identity does not match the requested exact yylo release")
            executable = package_root / "dist/bin/cli.mjs"
            installation = {"prefix": str(prefix), "package_root": str(package_root.resolve()),
                            "package": "@yylo/cli", "version": args.runtime_version,
                            "package_manifest_sha256": file_digest(manifest_path)}
            installed_runtime = runtime_identity(executable, args.runtime_version, root)
            return runtime_rebind(argparse.Namespace(
                root=root, branch=expected_branch, runtime=executable,
                runtime_version=args.runtime_version, output=output,
                artifact=artifact, installation=installation, install_prefix=str(prefix),
            ), policy)
    except BaseException as exc:
        shutil.rmtree(prefix, ignore_errors=True)
        after_identity = runtime_file.read_bytes() if runtime_file.exists() else None
        rollback = {"fresh_prefix_removed": not prefix.exists(),
                    "controller_head_restored": git(root, "rev-parse", "HEAD") == before["head"],
                    "controller_tree_restored": git(root, "write-tree") == before["tree"],
                    "controller_clean": not bool(git(root, "status", "--porcelain=v2", "--untracked-files=all", check=False)),
                    "runtime_version_restored": git(root, "config", "--worktree", "--get", "juno.controller.runtimeVersion", check=False) == before["runtime_version"],
                    "runtime_executable_restored": git(root, "config", "--worktree", "--get", "juno.controller.runtimeExecutable", check=False) == before["runtime_executable"],
                    "runtime_identity_restored": after_identity == before_identity}
        rollback["complete"] = all(rollback.values())
        failure = {"schema_version": RECEIPT_SCHEMA, "operation": "runtime-install-rebind",
                   "outcome": "failed_rolled_back", "root": str(root), "branch": expected_branch,
                   "runtime_version": args.runtime_version, "install_prefix": str(prefix),
                   "artifact": artifact, "installation": installation, "runtime": installed_runtime,
                   "error": bounded_runtime_error(exc), "rollback": rollback,
                   "tracked_changes": False, "product_ref_mutation": False}
        atomic_receipt(output, failure)
        raise


def runtime_rebind(args: argparse.Namespace, policy: dict[str, Any]) -> dict[str, Any]:
    root = exact_worktree(args.root)
    with config_repair_lock(Path(common_dir(root))):
        return _runtime_rebind_locked(args, policy)


def _runtime_rebind_locked(args: argparse.Namespace, policy: dict[str, Any]) -> dict[str, Any]:
    root = exact_worktree(args.root)
    expected_branch = safe_ref(args.branch, "branch")
    if git(root, "symbolic-ref", "-q", "HEAD", check=False) != expected_branch:
        raise BoundaryError("runtime rebind refused for wrong controller branch")
    if git(root, "config", "--worktree", "--get", "juno.controller.mode", check=False) != "metadata-only":
        raise BoundaryError("runtime rebind refused for non-metadata controller")
    if git(root, "status", "--porcelain=v2", "--untracked-files=all", check=False):
        raise BoundaryError("runtime rebind requires a clean metadata controller")
    output = external_config_repair_receipt(
        args.output, root, Path(common_dir(root)))
    before_head = git(root, "rev-parse", "HEAD")
    before_tree = git(root, "write-tree")
    identity = runtime_identity(args.runtime, args.runtime_version, root)
    local_identity = {**identity, "source": "installed-release", "tracked": False}
    runtime_file = root / policy["runtime"]["identity_file"]
    artifact = getattr(args, "artifact", None)
    payload = {"schema_version": RECEIPT_SCHEMA,
               "operation": "runtime-install-rebind" if artifact else "runtime-rebind",
               "outcome": "exact_runtime_installed_and_rebound" if artifact else "local_runtime_rebound",
               "root": str(root), "branch": expected_branch, "head": before_head, "tree": before_tree,
               "runtime_version": args.runtime_version, "runtime": identity, "artifact": artifact,
               "install_prefix": getattr(args, "install_prefix", None),
               "installation": getattr(args, "installation", None),
               "rollback": {"attempted": False, "fresh_prefix_removed": False} if artifact else None,
               "tracked_changes": False, "product_ref_mutation": False}
    preflight_receipt(output, payload)
    old_version_result = run(["git", "-C", str(root), "config", "--worktree", "--get", "juno.controller.runtimeVersion"], root, False)
    old_executable_result = run(["git", "-C", str(root), "config", "--worktree", "--get", "juno.controller.runtimeExecutable"], root, False)
    old_identity = runtime_file.read_bytes() if runtime_file.exists() else None

    def restore_config(key: str, previous: subprocess.CompletedProcess[str]) -> None:
        if previous.returncode == 0:
            git(root, "config", "--worktree", key, previous.stdout.rstrip("\n"))
        else:
            git(root, "config", "--worktree", "--unset-all", key, check=False)

    try:
        git(root, "config", "--worktree", "juno.controller.runtimeVersion", identity["version"])
        git(root, "config", "--worktree", "juno.controller.runtimeExecutable", identity["executable"])
        if os.environ.get("JUNO_RUNTIME_REBIND_TEST_FAIL_AFTER_CONFIG") == "1":
            raise BoundaryError("injected runtime rebind failure after config mutation")
        runtime_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = runtime_file.with_name(f".{runtime_file.name}.tmp-{os.getpid()}")
        temporary.write_bytes(canonical(local_identity)); os.replace(temporary, runtime_file)
        if (git(root, "config", "--worktree", "--get", "juno.controller.runtimeVersion") != identity["version"]
                or git(root, "config", "--worktree", "--get", "juno.controller.runtimeExecutable") != identity["executable"]):
            raise BoundaryError("runtime rebind config CAS readback failed")
        if git(root, "rev-parse", "HEAD") != before_head or git(root, "write-tree") != before_tree or git(root, "status", "--porcelain=v2", "--untracked-files=all"):
            raise BoundaryError("runtime rebind created tracked controller synchronization work")
        atomic_receipt(output, payload)
    except Exception as exc:
        rollback_errors: list[str] = []
        try:
            restore_config("juno.controller.runtimeVersion", old_version_result)
            restore_config("juno.controller.runtimeExecutable", old_executable_result)
        except Exception as rollback_exc:
            rollback_errors.append(f"config: {rollback_exc}")
        try:
            if old_identity is None:
                runtime_file.unlink(missing_ok=True)
            else:
                runtime_file.parent.mkdir(parents=True, exist_ok=True)
                runtime_file.write_bytes(old_identity)
        except OSError as rollback_exc:
            rollback_errors.append(f"identity: {rollback_exc}")
        if rollback_errors:
            raise BoundaryError(f"runtime rebind failed ({exc}); rollback failed: {', '.join(rollback_errors)}") from exc
        raise
    return payload


def transition_plan(args: argparse.Namespace, policy: dict[str, Any], rollback: bool) -> dict[str, Any]:
    plan = read_plan(args.plan.resolve())
    old = exact_worktree(Path(plan["old_controller"])); new = exact_worktree(Path(plan["new_controller"]))
    if common_dir(old) != plan["git_common_dir"] or common_dir(new) != plan["git_common_dir"]:
        raise BoundaryError("controller worktrees are not linked to the planned repository")
    resolve_commit(old, plan["old_branch"], plan["old_head"], "rollback controller ref")
    resolve_commit(old, plan["product_ref"], plan["product_head"], "product target")
    new_head = git(new, "rev-parse", f"{plan['new_branch']}^{{commit}}", check=False)
    evidence = inspect(new, policy, expected_branch=plan["new_branch"], require_active=rollback)
    if not evidence["passed"]:
        raise BoundaryError("metadata controller identity verification refused")
    operation = "rollback-plan" if rollback else "cutover-plan"
    source, target = (str(new), str(old)) if rollback else (str(old), str(new))
    payload = {"schema_version": RECEIPT_SCHEMA, "operation": operation, "outcome": "planned_no_mutation",
               "source_controller": source, "target_controller": target, "old_branch": plan["old_branch"],
               "old_head": plan["old_head"], "new_branch": plan["new_branch"], "new_head": new_head,
               "product_ref": plan["product_ref"], "product_head": plan["product_head"],
               "registration_change_authorized": False, "product_ref_mutation": False, "history_rewrite": False,
               "deletes_worktree": False, "steps": ["freeze writers", "verify exact source registration",
                   "switch registration to exact target identity", "set target role controller", "set source role rollback-read-only",
                   "read back registration and unchanged product ref"]}
    atomic_receipt(args.output, payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("migration-plan")
    plan.add_argument("--old-controller", type=Path, required=True); plan.add_argument("--old-branch", required=True)
    plan.add_argument("--expected-old-head", required=True); plan.add_argument("--new-controller", type=Path, required=True)
    plan.add_argument("--new-branch", required=True); plan.add_argument("--product-ref", required=True)
    plan.add_argument("--expected-product-head", required=True); plan.add_argument("--runtime", type=Path, required=True)
    plan.add_argument("--runtime-version", required=True); plan.add_argument("--output", type=Path, required=True)
    plan.add_argument("--policy-bundle", type=Path,
                      help="reviewed migration policy bundle containing metadata, task-workspace, and risk policies")
    plan.add_argument("--task-workspace-policy", type=Path,
                      help="reviewed task-workspace JSON (requires integration/risk paths; alternative to --policy-bundle)")
    plan.add_argument("--integration-workspace-policy", type=Path,
                      help="reviewed integration-workspace JSON (requires task/risk paths; alternative to --policy-bundle)")
    plan.add_argument("--risk-policy", type=Path,
                      help="reviewed risk-policy JSON (requires task/integration paths; alternative to --policy-bundle)")
    prep = sub.add_parser("prepare"); prep.add_argument("--plan", type=Path, required=True); prep.add_argument("--output", type=Path, required=True)
    verify = sub.add_parser("verify"); verify.add_argument("--root", type=Path, required=True); verify.add_argument("--branch", required=True)
    verify.add_argument("--pending", action="store_true"); verify.add_argument("--output", type=Path, required=True)
    product = sub.add_parser("verify-product"); product.add_argument("--root", type=Path, required=True)
    product.add_argument("--product-ref", required=True); product.add_argument("--expected-head", required=True)
    product.add_argument("--output", type=Path, required=True)
    agent_plan = sub.add_parser("agent-config-plan")
    agent_plan.add_argument("--root", type=Path, required=True)
    agent_plan.add_argument("--output", type=Path, required=True)
    agent_apply = sub.add_parser("agent-config-apply")
    agent_apply.add_argument("--plan", type=Path, required=True)
    agent_apply.add_argument("--output", type=Path, required=True)
    agent_apply.add_argument("--authorize-config-repair", dest="authorize", action="store_true")
    repair_plan = sub.add_parser("config-repair-plan")
    repair_plan.add_argument("--root", type=Path, required=True); repair_plan.add_argument("--branch", required=True)
    repair_plan.add_argument("--expected-head", required=True); repair_plan.add_argument("--product-ref", required=True)
    repair_plan.add_argument("--expected-product-head", required=True); repair_plan.add_argument("--output", type=Path, required=True)
    repair_apply = sub.add_parser("config-repair-apply")
    repair_apply.add_argument("--plan", type=Path, required=True); repair_apply.add_argument("--output", type=Path, required=True)
    repair_apply.add_argument("--authorize-config-repair", dest="authorize", action="store_true")
    policy_plan = sub.add_parser("metadata-policy-plan")
    policy_plan.add_argument("--root", type=Path, required=True); policy_plan.add_argument("--output", type=Path, required=True)
    policy_apply = sub.add_parser("metadata-policy-apply")
    policy_apply.add_argument("--plan", type=Path, required=True); policy_apply.add_argument("--output", type=Path, required=True)
    policy_apply.add_argument("--authorize-metadata-policy-migration", dest="authorize", action="store_true")
    surface_plan = sub.add_parser("agent-surface-repair-plan")
    surface_plan.add_argument("--root", type=Path, required=True); surface_plan.add_argument("--branch", required=True)
    surface_plan.add_argument("--expected-head", required=True); surface_plan.add_argument("--product-ref", required=True)
    surface_plan.add_argument("--expected-product-head", required=True); surface_plan.add_argument("--disposition", required=True)
    surface_plan.add_argument("--output", type=Path, required=True)
    surface_apply = sub.add_parser("agent-surface-repair-apply")
    surface_apply.add_argument("--plan", type=Path, required=True); surface_apply.add_argument("--output", type=Path, required=True)
    surface_apply.add_argument("--authorize-agent-surface-repair", dest="authorize", action="store_true")
    surface_verify = sub.add_parser("agent-surface-repair-verify")
    surface_verify.add_argument("--plan", type=Path, required=True); surface_verify.add_argument("--output", type=Path, required=True)
    rebind = sub.add_parser("runtime-rebind"); rebind.add_argument("--root", type=Path, required=True); rebind.add_argument("--branch", required=True)
    rebind.add_argument("--runtime", type=Path, required=True); rebind.add_argument("--runtime-version", required=True); rebind.add_argument("--output", type=Path, required=True)
    install_rebind = sub.add_parser("runtime-install-rebind")
    install_rebind.add_argument("--root", type=Path, required=True); install_rebind.add_argument("--branch", required=True)
    install_rebind.add_argument("--runtime-version", required=True); install_rebind.add_argument("--install-prefix", type=Path, required=True)
    install_rebind.add_argument("--artifact", type=Path,
                                help="exact local npm pack .tgz outside every Git worktree (otherwise use registry)")
    install_rebind.add_argument("--output", type=Path, required=True)
    for name in ("cutover-plan", "rollback-plan"):
        item = sub.add_parser(name); item.add_argument("--plan", type=Path, required=True); item.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command in {"metadata-policy-plan", "metadata-policy-apply"}:
        policy = {}
    elif args.command in {"agent-config-plan", "agent-config-apply"}:
        if args.policy is not None:
            raise BoundaryError("agent config migration uses the registered controller's own policy")
        root = (args.root if args.command == "agent-config-plan" else
                Path(validate_config_repair_plan(args.plan)[0]["controller"]))
        policy = load_policy(exact_physical_controller(root) / POLICY_PATH)
    elif args.command == "migration-plan" and args.policy_bundle is not None and args.policy is None:
        policy = metadata_policy_from_bundle(args.policy_bundle)
    elif args.command in {"prepare", "cutover-plan", "rollback-plan"} and args.policy is None:
        policy = policy_from_plan_bundle(args.plan)
        if policy is None:
            policy = load_policy((Path(__file__).resolve().parents[1] / "config/metadata-controller.json").resolve())
    else:
        policy_path = (args.policy or Path(__file__).resolve().parents[1] / "config/metadata-controller.json").resolve()
        policy = load_policy(policy_path)
    if args.command == "migration-plan":
        for ref, label in ((args.old_branch, "old_branch"), (args.new_branch, "new_branch"), (args.product_ref, "product_ref")):
            safe_ref(ref, label)
        payload = migration_plan(args, policy)
    elif args.command == "prepare": payload = prepare(args, policy)
    elif args.command == "verify":
        safe_ref(args.branch, "branch")
        payload = {"schema_version": RECEIPT_SCHEMA, "operation": "verify", **inspect(args.root, policy, expected_branch=args.branch, require_active=not args.pending)}
        atomic_receipt(args.output, payload)
        if not payload["passed"]: raise BoundaryError("metadata controller verification refused")
    elif args.command == "verify-product":
        payload = {"schema_version": RECEIPT_SCHEMA, "operation": "verify-product",
                   **product_boundary(args.root, args.product_ref, args.expected_head, policy)}
        atomic_receipt(args.output, payload)
        if not payload["passed"]: raise BoundaryError("product tree still contains controller-private paths")
    elif args.command == "agent-config-plan": payload = agent_config_plan(args, policy)
    elif args.command == "agent-config-apply": payload = config_repair_apply(args, policy)
    elif args.command == "config-repair-plan": payload = config_repair_plan(args, policy)
    elif args.command == "config-repair-apply": payload = config_repair_apply(args, policy)
    elif args.command == "metadata-policy-plan": payload = policy_migration_plan(args)
    elif args.command == "metadata-policy-apply": payload = policy_migration_apply(args)
    elif args.command == "agent-surface-repair-plan": payload = agent_surface_repair_plan(args, policy)
    elif args.command == "agent-surface-repair-apply": payload = agent_surface_repair_apply(args, policy)
    elif args.command == "agent-surface-repair-verify": payload = agent_surface_repair_verify(args, policy)
    elif args.command == "runtime-rebind": payload = runtime_rebind(args, policy)
    elif args.command == "runtime-install-rebind": payload = runtime_install_rebind(args, policy)
    else: payload = transition_plan(args, policy, args.command == "rollback-plan")
    print(json.dumps({"outcome": payload.get("outcome", "verified"), "receipt": str(args.output.resolve())}, sort_keys=True))


if __name__ == "__main__":
    try: main()
    except (BoundaryError, OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"metadata-controller: {exc}", file=os.sys.stderr)
        raise SystemExit(2)
