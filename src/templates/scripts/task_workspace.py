#!/usr/bin/env python3
"""Small exact-base task-worktree state machine for the Bolt workflow.

The controller owns one compact JSON record per task. Product worktrees contain
only the target tree: this command never copies Kanban, specs, receipts, or
other controller data into them. Integration, review, release, and cleanup are
deliberately outside this interface.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import errno
import fcntl
import hashlib
import importlib.util
import json
import os
import posixpath
import re
import secrets
import selectors
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator, Optional

import task_workflow_helper as lifecycle_runtime
import task_workspace_decisions as decisions
import operation_snapshot as operation_runtime

# --- Pure functional core (Wave 3 pilot of 7djT8N) ---
# Decision planners live in task_workspace_decisions; this shell keeps only
# physical identity resolution, Git/filesystem mutation, locks, validator
# dispatch, receipt persistence, and rendering. The aliases below preserve
# the historical module surface for callers and tests.
path_within = decisions.path_within
validation_profile_selection = decisions.validation_profile_selection
selected_full_suite_commands = decisions.selected_full_suite_commands
selected_focused_rows = decisions.selected_focused_rows
selected_standing_rows = decisions.selected_standing_rows
_QUEUE_MISSING = decisions.QUEUE_MISSING
_shared_queue_delta = decisions.shared_queue_delta

CONFIG_SCHEMA = "juno_task_workspace_config.v1"
STATE_SCHEMA = "juno_task_workspace_state.v1"
RECORD_SCHEMA = "juno_task_workspace_record.v1"
SHA_RE = re.compile(r"[0-9a-f]{40,64}\Z")
TASK_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
SEMVER_RE = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\Z"
)
RUNTIME_PATH = ".juno_task/scripts/task_workspace.py"
TASK_HYDRATE_RECOVERY_SCHEMA = "juno_task_hydrate_recovery.v1"
# Stable package-router capability. Parser command ordering may evolve without
# invalidating hydrate recovery selection.
TASK_RUNTIME_CAPABILITY_HYDRATE_V1 = True
TASK_PREDISPATCH_RECOVERY_SCHEMA = "juno_task_predispatch_recovery.v1"
TASK_RUNTIME_CAPABILITY_PREDISPATCH_RECOVERY_V1 = True
TASK_WALL_BUDGET_RECOVERY_SCHEMA = "juno_task_wall_budget_recovery.v1"
TASK_RUNTIME_CAPABILITY_WALL_BUDGET_RECOVERY_V1 = True
RUNTIME_BOOTSTRAP_SCHEMA = "juno_target_task_runtime_bootstrap.v1"
RUNTIME_BOOTSTRAP_ROOT = ".juno_task/runtime/task-runtime-bootstrap"
MANAGED_INVENTORY_PATH = ".juno_task/managed-assets.json"
GENERATED_OUTPUT_DECLARATION = "juno-code/scripts/implementation-contract.json"
MANAGED_OUTPUT_DECLARATION = "juno-code/src/templates/managed-assets.json"
GENERATED_OUTPUT_SCHEMA = "juno_generated_output_contract.v1"
UMBRELLA_INPUT_SCHEMA = "juno_task_umbrella_admission_input.v1"
UMBRELLA_ADMISSION_SCHEMA = "juno_task_umbrella_admission.v1"
UMBRELLA_RECOVERY_PLAN_SCHEMA = "juno_task_umbrella_recovery_plan.v1"
UMBRELLA_SUPERSESSION_SCHEMA = "juno_task_umbrella_admission_supersession.v1"
UMBRELLA_AUTHORIZATION_SCHEMA = "juno_task_umbrella_recovery_authorization.v1"
UMBRELLA_EXECUTION_MODE = "umbrella_owned_sequential"
UMBRELLA_RESERVATIONS_SCHEMA = "juno_task_umbrella_child_reservations.v1"
UMBRELLA_CHILD_CHECKPOINT_SCHEMA = "juno_task_umbrella_child_checkpoint.v1"
TASK_SCOPE_SCHEMA = "juno_task_canonical_scope.v1"
AUTHORIZATION_LEDGER_SCHEMA = "juno_task_umbrella_authorization_ledger.v1"
TERMINAL_TASK_STATUSES = {"done", "archived", "cancelled", "canceled", "closed"}
PRESTART_TRACKING_STATUSES = {"backlog", "todo"}
# --- Canonical Kanban lifecycle projection -------------------------------
# The hot Kanban task plus its append-only ledger are the authoritative
# user-visible task truth. Lifecycle runtime records stay receipt-backed
# execution evidence; every durable lifecycle transition projects its coarse
# board status and structured detail through the canonical wrapper so the
# board can never silently disagree with an active task record.
KANBAN_SYNC_SCHEMA = "juno_task_kanban_sync.v1"
KANBAN_LIFECYCLE_PROJECTION = "juno_lifecycle_kanban_projection.v1"
KANBAN_SYNC_STATE = "KANBAN_SYNC_REQUIRED"
KANBAN_SYNC_RECOVERY = "yy task sync {task}"
KANBAN_SYNC_ROOT = ".juno_task/runtime/kanban-sync"
# Coarse board status per durable lifecycle state. "done" is documentation
# only here: verified merge finalization exclusively owns the done mutation.
LIFECYCLE_BOARD_STATUS = {
    "HYDRATING": "in_progress",
    "HYDRATION_FAILED": "in_progress",
    "WORKING": "in_progress",
    KANBAN_SYNC_STATE: "in_progress",
    "QUEUED": "in_progress",
    "AWAITING_RISK": "in_progress",
    "AWAITING_RELEASE": "in_progress",
    "REVIEWING": "in_progress",
    "RISK_EVIDENCE_READY": "in_progress",
    "CONFLICT": "in_progress",
    "CONFLICT_RESOLVED": "in_progress",
    "REOPENING": "in_progress",
    "REQUEUING_STALE": "in_progress",
    "REVIEW_FINDINGS": "in_progress",
    "REVIEW_FINDINGS_EXHAUSTED": "in_progress",
    "MERGING": "in_progress",
    # Withdrawn candidates are not done and not in flight: the disposition
    # fields carry the exact truth while the board returns to an owned,
    # non-terminal tracking status.
    "WITHDRAWN": "todo",
    "MERGED": "done",
}
# Structured non-success dispositions recorded without claiming integration.
LIFECYCLE_DISPOSITIONS = {
    "WITHDRAWN": "withdrawn",
    "REVIEW_FINDINGS_EXHAUSTED": "review_findings_exhausted",
}
VALIDATION_TIMING_SCHEMA = "juno_validation_timing.v1"
VALIDATION_PHASES = ("WAITING_FOR_RESOURCE", "SETUP", "RUNNING", "TEARDOWN")
VALIDATION_TERMINALS = {"PASSED", "FAILED", "TIMED_OUT", "INTERRUPTED", "SETUP_FAILED"}
STANDING_EVIDENCE_SCHEMA = "juno_standing_validation_evidence.v1"
CANONICAL_VALIDATION_RECEIPT_SCHEMA = "juno_canonical_validation_receipt.v1"
STANDING_PLAN_SCHEMA = "juno_standing_validation_plan.v1"
STANDING_ROOT = ".juno_task/runtime/standing-evidence"


class TaskWorkspaceError(RuntimeError):
    pass


class KanbanSyncError(TaskWorkspaceError):
    """Canonical Kanban projection could not be proven for one task record."""

    def __init__(self, message: str, evidence: dict[str, Any]):
        super().__init__(message)
        self.evidence = {"schema_version": KANBAN_SYNC_SCHEMA,
                         "status": "required", "error": message[:1024], **evidence}


class ManagedAgentPreDispatchError(TaskWorkspaceError):
    """Controller-bound failure proven to precede provider/model launch."""

    def __init__(self, message: str, receipt: dict[str, str]):
        super().__init__(message)
        self.receipt = receipt


class HydrationFailure(TaskWorkspaceError):
    def __init__(self, message: str, evidence: dict[str, Any]):
        super().__init__(message)
        self.evidence = evidence


class ValidationResourceTimeout(TaskWorkspaceError):
    def __init__(self, message: str, evidence: dict[str, Any]):
        super().__init__(message)
        self.evidence = evidence


def is_valid_semver(value: Any) -> bool:
    """Return whether value is an exact ASCII SemVer 2.0.0 version string."""
    return isinstance(value, str) and SEMVER_RE.fullmatch(value) is not None


def semver_precedes(older: str, newer: str) -> bool:
    """Compare validated SemVer values without trusting an optional dependency."""
    def parts(value: str) -> tuple[tuple[int, int, int], list[str] | None]:
        public = value.split("+", 1)[0]
        core, separator, prerelease = public.partition("-")
        return tuple(int(item) for item in core.split(".")), prerelease.split(".") if separator else None

    older_core, older_pre = parts(older)
    newer_core, newer_pre = parts(newer)
    if older_core != newer_core:
        return older_core < newer_core
    if older_pre is None or newer_pre is None:
        return older_pre is not None and newer_pre is None
    for left, right in zip(older_pre, newer_pre):
        if left == right:
            continue
        left_numeric, right_numeric = left.isdigit(), right.isdigit()
        if left_numeric and right_numeric:
            return int(left) < int(right)
        if left_numeric != right_numeric:
            return left_numeric
        return left < right
    return len(older_pre) < len(newer_pre)


def run(argv: list[str], cwd: Path, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, cwd=cwd, text=True, capture_output=True, stdin=subprocess.DEVNULL)
    if check and result.returncode:
        raise TaskWorkspaceError(result.stderr.strip() or result.stdout.strip() or f"command failed: {argv!r}")
    return result


def git(root: Path, *args: str, check: bool = True) -> str:
    return run(["git", "-C", str(root), *args], root, check=check).stdout.strip()


def git_pathnames(root: Path, *args: str) -> list[str]:
    """Read Git pathnames without display quoting or line-based ambiguity."""
    result = subprocess.run(
        ["git", "-C", str(root), *args], cwd=root, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()
        raise TaskWorkspaceError(detail or f"Git pathname command failed: {args!r}")
    raw = result.stdout
    if raw and not raw.endswith(b"\0"):
        raise TaskWorkspaceError("Git produced malformed NUL-delimited changed paths")
    paths: list[str] = []
    for item in raw.split(b"\0")[:-1] if raw else []:
        if not item:
            raise TaskWorkspaceError("Git produced an empty changed path")
        try:
            value = item.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise TaskWorkspaceError(
                "Git changed path is not valid UTF-8 and cannot be represented in canonical JSON"
            ) from exc
        path = PurePosixPath(value)
        if (path.is_absolute() or path.as_posix() != value or value == "."
                or ".." in path.parts or ".git" in path.parts):
            raise TaskWorkspaceError("Git produced an unsafe changed path")
        paths.append(value)
    return sorted(set(paths))


def git_status_pathnames(root: Path) -> list[str]:
    """Read porcelain-v1 status without trimming its significant XY columns."""
    result = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=all", "-z"],
        cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()
        raise TaskWorkspaceError(detail or "Git status command failed")
    raw = result.stdout
    if raw and not raw.endswith(b"\0"):
        raise TaskWorkspaceError("Git produced malformed porcelain status output")
    entries = raw.split(b"\0")[:-1] if raw else []
    paths: list[str] = []

    def validate_path(item: bytes) -> str:
        try:
            value = item.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise TaskWorkspaceError(
                "Git uncommitted path is not valid UTF-8 and cannot be represented in canonical JSON"
            ) from exc
        path = PurePosixPath(value)
        if (path.is_absolute() or path.as_posix() != value or value == "."
                or ".." in path.parts or ".git" in path.parts):
            raise TaskWorkspaceError("Git produced an unsafe uncommitted path")
        return value

    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4 or entry[2:3] != b" ":
            raise TaskWorkspaceError("Git produced malformed porcelain status output")
        paths.append(validate_path(entry[3:]))
        if entry[0:1] in (b"R", b"C") or entry[1:2] in (b"R", b"C"):
            if index >= len(entries):
                raise TaskWorkspaceError("Git produced malformed porcelain status output")
            validate_path(entries[index])
            index += 1
    return sorted(set(paths))


def load_package_bound_test_fixture(test_file: str, fixture_name: str) -> Any:
    """Load a fixture only from a verified installed package or canonical source tree."""
    if not re.fullmatch(r"[A-Za-z0-9_]+\.py", fixture_name):
        raise TaskWorkspaceError("unsafe package test fixture name")
    test_path = Path(test_file).resolve()

    def load(candidate: Path) -> Any:
        candidate = candidate.resolve()
        if not candidate.is_file():
            raise TaskWorkspaceError("verified package is missing its canonical test fixture")
        spec = importlib.util.spec_from_file_location(
            f"juno_package_fixture_{candidate.stem}_{hashlib.sha256(str(candidate).encode()).hexdigest()[:12]}",
            candidate)
        if spec is None or spec.loader is None:
            raise TaskWorkspaceError("canonical package test fixture is not loadable")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    # Installed execution has exactly one authority: the controller's bound,
    # hash-identified package. Never inspect an adjacent tests directory.
    explicit = os.environ.get("JUNO_TASK_ROOT", "").strip()
    explicit_root = Path(explicit).expanduser().resolve() if explicit else None
    installed_test_root = (test_path.parents[3] if len(test_path.parents) > 3 and
                           test_path.parents[2].name == ".juno_task" else None)
    package_test_root = (explicit_root / "dist/templates/scripts/tests"
                         if explicit_root is not None else None)
    explicit_applies = (explicit_root is not None and
                        (installed_test_root == explicit_root or test_path.parent == package_test_root))
    runtime_root = explicit_root if explicit_applies else installed_test_root
    if runtime_root is not None:
        identity_path = runtime_root / ".juno_task/runtime/identity.json"
        inventory_path = runtime_root / ".juno_task/managed-assets.json"
        if identity_path.exists() or explicit_applies:
            try:
                identity = json.loads(identity_path.read_bytes())
                inventory = json.loads(inventory_path.read_bytes())
                executable = Path(identity["executable"]).expanduser().resolve()
                version = identity["version"]
                executable_hash = hashlib.sha256(executable.read_bytes()).hexdigest()
                package_root = executable.parent.parent.parent
                package = json.loads((package_root / "package.json").read_text())
            except (OSError, KeyError, TypeError, json.JSONDecodeError):
                identity = inventory = package = None
                executable_hash = version = ""
                package_root = Path("/")
            valid = (
                isinstance(identity, dict) and set(identity) == {
                    "package", "version", "executable", "executable_sha256", "source", "tracked"}
                and identity.get("package") == "@yylo/cli"
                and identity.get("source") == "installed-release" and identity.get("tracked") is False
                and is_valid_semver(version)
                and executable_hash == identity.get("executable_sha256")
                and _managed_inventory_identity_valid(inventory)
                and inventory.get("packageVersion") == version
                and isinstance(package, dict) and package.get("name") == "@yylo/cli"
                and package.get("version") == version)
            if not valid:
                raise TaskWorkspaceError(
                    f"package-bound test fixture unavailable: {fixture_name}; run `yy scripts update --force` "
                    "from the controller's bound yylo installation, then retry")
            return load(package_root / "dist/templates/scripts/tests" / fixture_name)

    # Development execution is the only fallback. Its identity is an actual
    # Git worktree plus exact tracked yylo paths, never a guessed sibling.
    discovered = run(["git", "-C", str(test_path.parent), "rev-parse", "--show-toplevel"],
                     test_path.parent, check=False)
    if discovered.returncode == 0:
        source_root = Path(discovered.stdout.strip()).resolve()
        canonical = source_root / "juno-code/src/templates/scripts/tests" / fixture_name
        allowed_tests = {
            source_root / ".juno_task/scripts/tests" / test_path.name,
            source_root / "juno-code/src/templates/scripts/tests" / test_path.name}
        package_path = source_root / "juno-code/package.json"
        tracked = run(["git", "-C", str(source_root), "ls-files", "--error-unmatch",
                       str(canonical.relative_to(source_root)),
                       str(test_path.relative_to(source_root))], source_root, check=False)
        try:
            source_package = json.loads(package_path.read_text())
        except (OSError, json.JSONDecodeError):
            source_package = None
        if (test_path in allowed_tests and tracked.returncode == 0 and
                isinstance(source_package, dict) and source_package.get("name") == "@yylo/cli"):
            return load(canonical)

    raise TaskWorkspaceError(
        f"package-bound test fixture unavailable: {fixture_name}; run `yy scripts update --force` "
        "from the controller's bound yylo installation, then retry")


def normalized_relative(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise TaskWorkspaceError(f"{label} must be a normalized relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or value == "." or ".." in path.parts or ".git" in path.parts:
        raise TaskWorkspaceError(f"unsafe {label}: {value!r}")
    return value.rstrip("/")


def load_config(controller: Path) -> dict[str, Any]:
    path = controller / ".juno_task/config/task-workspace.json"
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise TaskWorkspaceError(f"invalid task workspace policy: {exc}") from exc
    required = {"schema_version", "repository", "target_ref", "workspace_root", "branch_prefix",
                "allowed_paths", "controller_private_paths", "focused_validation",
                "full_suite_validation"}
    optional = {"selectable_paths", "hydration_workflow", "validation_profiles", "documentation_validation"}
    if (not isinstance(value, dict) or not required.issubset(value) or set(value) - required - optional
            or value.get("schema_version") != CONFIG_SCHEMA):
        raise TaskWorkspaceError(f"task workspace policy must contain exactly the {CONFIG_SCHEMA} fields")
    value.setdefault("selectable_paths", [])
    value.setdefault("hydration_workflow", ".juno_task/config/worktree-hydration.yaml")
    documentation = value.setdefault(
        "documentation_validation", lifecycle_runtime.default_documentation_policy())
    expected_documentation_keys = set(lifecycle_runtime.default_documentation_policy())
    if (not isinstance(documentation, dict) or set(documentation) != expected_documentation_keys
            or documentation.get("schema_version") != "juno_documentation_validation_policy.v1"
            or any(not isinstance(documentation.get(field), list)
                   or any(not isinstance(item, str) or not item for item in documentation[field])
                   for field in ("inert_exact_files", "inert_roots", "active_exact_files",
                                 "active_roots", "active_name_patterns", "public_identities",
                                 "cli_top_level"))):
        raise TaskWorkspaceError("documentation_validation policy is malformed")
    try:
        [re.compile(pattern) for pattern in documentation["active_name_patterns"]]
    except re.error as exc:
        raise TaskWorkspaceError(f"documentation_validation pattern is invalid: {exc}") from exc
    value["hydration_workflow"] = normalized_relative(
        value["hydration_workflow"], "hydration_workflow")
    repository = Path(value["repository"])
    if repository.is_absolute() or ".." in repository.parts:
        raise TaskWorkspaceError("repository must stay inside the controller Git worktree")
    target = value["target_ref"]
    prefix = value["branch_prefix"]
    if not isinstance(target, str) or not target.startswith("refs/heads/"):
        raise TaskWorkspaceError("target_ref must be a full local branch ref")
    if not isinstance(prefix, str) or not prefix.startswith("refs/heads/") or not prefix.endswith("-"):
        raise TaskWorkspaceError("branch_prefix must be a full local branch prefix ending in '-'")
    workspace = Path(value["workspace_root"]).expanduser()
    if not workspace.is_absolute() or workspace == Path("/"):
        raise TaskWorkspaceError("workspace_root must be an explicit absolute directory")
    for field in ("allowed_paths", "selectable_paths", "controller_private_paths"):
        items = value[field]
        if not isinstance(items, list) or (field != "selectable_paths" and not items):
            raise TaskWorkspaceError(f"{field} must be a list" + ("" if field == "selectable_paths" else " with at least one path"))
        value[field] = [normalized_relative(item, field) for item in items]
        if len(set(value[field])) != len(value[field]):
            raise TaskWorkspaceError(f"{field} contains duplicates")
    for selected in value["selectable_paths"]:
        if path_within(selected, value["allowed_paths"]) or path_within(selected, value["controller_private_paths"]):
            raise TaskWorkspaceError(f"selectable path overlaps a fixed or controller-private path: {selected}")
    validations = value["focused_validation"]
    if not isinstance(validations, list) or not validations:
        raise TaskWorkspaceError("focused_validation must contain at least one command")
    def validate_row(row: Any, label: str) -> None:
        required_row = {"id", "cwd", "argv", "timeout_seconds", "max_output_bytes"}
        if not isinstance(row, dict) or not required_row.issubset(row) or set(row) - required_row - {"resource", "input_paths"}:
            raise TaskWorkspaceError(
                f"{label} requires id, cwd, argv, timeout_seconds, max_output_bytes, and optional resource/input_paths")
        normalized_relative(row["cwd"], f"{label} cwd")
        if (not isinstance(row["timeout_seconds"], int)
                or isinstance(row["timeout_seconds"], bool)
                or not 1 <= row["timeout_seconds"] <= 3600):
            raise TaskWorkspaceError(
                f"{label} timeout_seconds must be an integer from 1 through 3600")
        if (not isinstance(row["id"], str) or not row["id"] or len(row["id"].encode()) > 128
                or len(row["cwd"].encode()) > 1024
                or not isinstance(row["argv"], list) or not row["argv"] or len(row["argv"]) > 128
                or any(not isinstance(part, str) or not part or len(part.encode()) > 4096
                       for part in row["argv"])
                or not isinstance(row["max_output_bytes"], int)
                or isinstance(row["max_output_bytes"], bool)
                or not 1024 <= row["max_output_bytes"] <= 1048576):
            raise TaskWorkspaceError(f"{label} bounds or argv are invalid")
        input_paths = row.get("input_paths")
        if input_paths is not None:
            if (not isinstance(input_paths, list) or not input_paths or len(input_paths) > 64
                    or any(not isinstance(path, str) for path in input_paths)):
                raise TaskWorkspaceError(f"{label} input_paths must be a bounded nonempty list")
            row["input_paths"] = [normalized_relative(path, f"{label} input path")
                                  for path in input_paths]
            if len(set(row["input_paths"])) != len(row["input_paths"]):
                raise TaskWorkspaceError(f"{label} input_paths contains duplicates")
        resource = row.get("resource")
        if resource is not None:
            if (not isinstance(resource, dict)
                    or set(resource) != {"id", "lock_path", "wait_timeout_seconds"}
                    or not isinstance(resource.get("id"), str) or not resource["id"]
                    or len(resource["id"].encode()) > 128
                    or not isinstance(resource.get("lock_path"), str)
                    or not Path(resource["lock_path"]).is_absolute()
                    or Path(resource["lock_path"]) == Path("/")
                    or not isinstance(resource.get("wait_timeout_seconds"), int)
                    or isinstance(resource.get("wait_timeout_seconds"), bool)
                    or not 1 <= resource["wait_timeout_seconds"] <= 3600):
                raise TaskWorkspaceError(f"{label} resource declaration is invalid")
    for row in validations:
        validate_row(row, "focused validation")
    resource_declarations: dict[str, tuple[str, int]] = {}
    for row in validations:
        resource = row.get("resource")
        if resource is None:
            continue
        declaration = (str(lexical_absolute(Path(resource["lock_path"]))),
                       resource["wait_timeout_seconds"])
        prior = resource_declarations.setdefault(resource["id"], declaration)
        if prior != declaration:
            raise TaskWorkspaceError(
                f"focused validation resource {resource['id']!r} has conflicting declarations")
    full_suite = value["full_suite_validation"]
    validate_row(full_suite, "full-suite validation")
    profiles = _validated_validation_profiles(value, full_suite["id"], validate_row)
    # Keep normalization round-trip safe: a config that authored no profiles
    # must not gain an explicit empty list that its own re-validation rejects.
    if profiles:
        value["validation_profiles"] = profiles
    return value


def _validated_validation_profiles(value: dict[str, Any], full_suite_id: str,
                                   validate_row: Any) -> list[dict[str, Any]]:
    """Admit only deterministic, package-local, product-admissible profiles."""
    profiles = value.get("validation_profiles")
    if profiles is None:
        return []
    if not isinstance(profiles, list) or not profiles or len(profiles) > 16:
        raise TaskWorkspaceError(
            "validation_profiles must be a bounded nonempty list when present")
    seen_ids: set[str] = {full_suite_id}
    seen_ids.update(row["id"] for row in value["focused_validation"])
    seen_roots: list[tuple[str, str]] = []
    for profile in profiles:
        if (not isinstance(profile, dict)
                or set(profile) != {"id", "path_roots", "commands"}):
            raise TaskWorkspaceError(
                "validation profile requires exactly id, path_roots, and commands")
        profile_id = profile["id"]
        if (not isinstance(profile_id, str)
                or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", profile_id)
                or profile_id in seen_ids):
            raise TaskWorkspaceError(
                f"validation profile id is malformed or duplicated: {profile_id!r}")
        seen_ids.add(profile_id)
        roots = profile["path_roots"]
        if (not isinstance(roots, list) or not roots or len(roots) > 16
                or any(not isinstance(root, str) or not root for root in roots)):
            raise TaskWorkspaceError(
                f"validation profile {profile_id!r} path_roots must be a bounded nonempty list")
        profile["path_roots"] = [
            normalized_relative(root, f"validation profile {profile_id!r} path root")
            for root in roots]
        if len(set(profile["path_roots"])) != len(profile["path_roots"]):
            raise TaskWorkspaceError(
                f"validation profile {profile_id!r} path_roots contains duplicates")
        for root in profile["path_roots"]:
            if (not path_within(root, value["allowed_paths"])
                    or path_within(root, value["controller_private_paths"])):
                raise TaskWorkspaceError(
                    f"validation profile {profile_id!r} path root is not product-admissible: {root}")
            for prior_id, prior_root in seen_roots:
                if path_within(root, [prior_root]) or path_within(prior_root, [root]):
                    raise TaskWorkspaceError(
                        f"validation profile path roots overlap: {root} and {prior_id}:{prior_root}")
            seen_roots.append((profile_id, root))
        commands = profile["commands"]
        if (not isinstance(commands, list) or not commands or len(commands) > 16):
            raise TaskWorkspaceError(
                f"validation profile {profile_id!r} requires a bounded nonempty command list")
        for row in commands:
            validate_row(row, f"validation profile {profile_id!r} command")
            if not path_within(row["cwd"], profile["path_roots"]):
                raise TaskWorkspaceError(
                    f"validation profile {profile_id!r} command cwd escapes its package roots: {row['cwd']}")
            if row.get("input_paths") is not None and any(
                    not path_within(path, profile["path_roots"]) for path in row["input_paths"]):
                raise TaskWorkspaceError(
                    f"validation profile {profile_id!r} input path escapes its package roots")
        command_ids = [row["id"] for row in commands]
        if (any(command_id in seen_ids for command_id in command_ids)
                or len(set(command_ids)) != len(command_ids)):
            raise TaskWorkspaceError(
                f"validation profile {profile_id!r} command ids collide with another suite command")
        seen_ids.update(command_ids)
    return profiles


def lexical_absolute(path: Path) -> Path:
    """Normalize spelling without following a filesystem object."""
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


# Platform-canonical alias prefixes: darwin exposes /var, /tmp, and /etc as
# compatibility symlinks into /private. Git resolves those links when it
# reports a worktree root, so the alias spelling of an already proven physical
# worktree is identity-equivalent. Any other symlink component stays refused.
DARWIN_ALIAS_PREFIXES = (("/var", "/private/var"), ("/tmp", "/private/tmp"),
                         ("/etc", "/private/etc"))


def platform_alias_normalize(path: Path) -> Path:
    """Rewrite genuine platform alias prefixes; never touch other spellings."""
    if sys.platform != "darwin":
        return path
    for alias, canonical in DARWIN_ALIAS_PREFIXES:
        if path == Path(alias) or str(path).startswith(alias + "/"):
            # Only rewrite when the alias is the operating system's own
            # compatibility link. An attacker-created same-named directory
            # must never inherit canonical identity.
            alias_path = Path(alias)
            if alias_path.is_symlink() and alias_path.resolve() == Path(canonical):
                return Path(canonical + str(path)[len(alias):])
            return path
    return path


def reject_symlink_components(path: Path, label: str) -> None:
    """Refuse an exact identity path if any existing component is a symlink."""
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                raise TaskWorkspaceError(f"{label} contains a symlink component: {current}")
        except FileNotFoundError:
            # The exact-root check supplies the stable missing/reused diagnosis.
            return


def exact_root(path: Path, label: str, *, physical_identity: bool = True) -> Path:
    lexical = lexical_absolute(path)
    if physical_identity:
        candidate = platform_alias_normalize(lexical)
        reject_symlink_components(candidate, label)
    else:
        candidate = lexical.resolve()
    actual = git(candidate, "rev-parse", "--show-toplevel", check=False)
    actual_path = (platform_alias_normalize(lexical_absolute(Path(actual)))
                   if physical_identity and actual
                   else (Path(actual).resolve() if actual else None))
    if not actual or actual_path != candidate:
        raise TaskWorkspaceError(f"{label} is not an exact Git worktree: {candidate}")
    return candidate


def task_file(controller: Path, task_id: str) -> Path:
    if not TASK_RE.fullmatch(task_id):
        raise TaskWorkspaceError("unsafe task id")
    return controller / ".juno_task/tasks" / task_id[:2].lower() / f"{task_id}.md"


def stable_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def task_manifest(controller: Path, task_id: str) -> tuple[Path, bytes]:
    path = task_file(controller, task_id)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise TaskWorkspaceError(f"canonical hot Kanban task does not exist: {task_id}") from exc
    prefix = data[:4096].decode("utf-8", errors="replace")
    if not re.search(rf"(?m)^id:\s*{re.escape(task_id)}\s*$", prefix):
        raise TaskWorkspaceError(f"canonical Kanban task identity mismatch: {task_id}")
    return path, data


def require_task(controller: Path, task_id: str) -> None:
    task_manifest(controller, task_id)


def read_json_object(path: Path, label: str) -> tuple[dict[str, Any], str]:
    try:
        data = path.read_bytes()
        value = json.loads(data)
    except (OSError, json.JSONDecodeError) as exc:
        raise TaskWorkspaceError(f"invalid {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise TaskWorkspaceError(f"invalid {label}: expected an object")
    return value, hashlib.sha256(data).hexdigest()


def load_umbrella_input(path: Path) -> tuple[dict[str, Any], str]:
    value, source_sha = read_json_object(path, "umbrella admission input")
    if (set(value) != {"schema_version", "execution_mode", "children"}
            or value.get("schema_version") != UMBRELLA_INPUT_SCHEMA
            or value.get("execution_mode") != UMBRELLA_EXECUTION_MODE
            or not isinstance(value.get("children"), list) or not value["children"]
            or not all(isinstance(item, str) and TASK_RE.fullmatch(item)
                       for item in value["children"])):
        raise TaskWorkspaceError(
            f"umbrella admission input must use {UMBRELLA_INPUT_SCHEMA} and declare only ordered child IDs"
        )
    if len(set(value["children"])) != len(value["children"]):
        raise TaskWorkspaceError("umbrella child set is duplicated or cyclic")
    return value, source_sha


def task_status(body: bytes, task_id: str) -> str:
    match = re.search(r"(?m)^status:\s*([A-Za-z_]+)\s*$", body[:4096].decode("utf-8", errors="replace"))
    if not match:
        raise TaskWorkspaceError(f"canonical child {task_id} has no unambiguous lifecycle status")
    return match.group(1).lower()


def task_scope_path(controller: Path, task_id: str) -> Path:
    return controller / ".juno_task/task-scopes" / task_id[:2].lower() / f"{task_id}.json"


def immutable_task_body(body: bytes) -> bytes:
    """Return task requirements while excluding lifecycle metadata and response evidence."""
    marked = re.search(rb"<!-- juno:body:start -->.*?<!-- juno:body:end -->", body, re.DOTALL)
    if marked:
        return marked.group(0)
    text = re.sub(rb"\A---\n.*?\n---\n", b"", body, count=1, flags=re.DOTALL)
    return re.sub(rb"<!-- juno:response:start -->.*?<!-- juno:response:end -->", b"", text,
                  flags=re.DOTALL).strip()


def compatible_task_revision(controller: Path, task_id: str, body: bytes,
                             expected_sha256: Any) -> bool:
    """Permit status/response progress while retaining the frozen authored requirements."""
    if not isinstance(expected_sha256, str):
        return False
    if hashlib.sha256(body).hexdigest() == expected_sha256:
        return True
    relative = task_scope_path(controller, task_id).parent.parent.parent / "tasks" / task_id[:2].lower() / f"{task_id}.md"
    relative_path = relative.relative_to(controller).as_posix()
    revisions = git(controller, "log", "--format=%H", "--", relative_path, check=False).splitlines()
    for revision in revisions:
        result = run(["git", "-C", str(controller), "show", f"{revision}:{relative_path}"],
                     controller, check=False)
        if result.returncode:
            continue
        historical = result.stdout.encode("utf-8")
        if hashlib.sha256(historical).hexdigest() == expected_sha256:
            return immutable_task_body(historical) == immutable_task_body(body)
    return False


def load_task_scope(controller: Path, task_id: str, body: bytes) -> tuple[dict[str, Any], str]:
    value, file_sha = read_json_object(task_scope_path(controller, task_id), f"canonical child scope {task_id}")
    keys = {"schema_version", "task_id", "task_revision_sha256", "lifecycle_status",
            "umbrella_relations", "scope"}
    relation_keys = {"owner", "children"}; scope_keys = {
        "baseline", "selectable_paths", "required_paths", "generated_paths"}
    if (set(value) != keys or value.get("schema_version") != TASK_SCOPE_SCHEMA
            or value.get("task_id") != task_id
            or not compatible_task_revision(controller, task_id, body,
                                            value.get("task_revision_sha256"))
            or not isinstance(value.get("lifecycle_status"), str)
            or not task_status(body, task_id)
            or not isinstance(value.get("umbrella_relations"), dict)
            or set(value["umbrella_relations"]) != relation_keys
            or value["umbrella_relations"].get("owner") is not None
               and not TASK_RE.fullmatch(str(value["umbrella_relations"].get("owner")))
            or not isinstance(value["umbrella_relations"].get("children"), list)
            or not all(isinstance(item, str) and TASK_RE.fullmatch(item)
                       for item in value["umbrella_relations"]["children"])
            or not isinstance(value.get("scope"), dict) or set(value["scope"]) != scope_keys
            or not isinstance(value["scope"].get("baseline"), bool)):
        raise TaskWorkspaceError(f"canonical child scope {task_id} is absent, ambiguous, stale, or malformed")
    for field in ("selectable_paths", "required_paths", "generated_paths"):
        rows = value["scope"].get(field)
        if not isinstance(rows, list):
            raise TaskWorkspaceError(f"canonical child scope {task_id}.{field} must be a list")
        normalized = [normalized_relative(item, f"canonical child scope {task_id}.{field}") for item in rows]
        if normalized != sorted(set(normalized)):
            raise TaskWorkspaceError(f"canonical child scope {task_id}.{field} must be sorted and unique")
    if len(set(value["umbrella_relations"]["children"])) != len(value["umbrella_relations"]["children"]):
        raise TaskWorkspaceError(f"canonical child scope {task_id} has duplicate relations")
    return value, file_sha


def validate_umbrella_graph(controller: Path, umbrella_id: str, child_ids: list[str],
                            umbrella_body: bytes) -> tuple[dict[str, Any], str]:
    umbrella_scope, umbrella_scope_sha = load_task_scope(controller, umbrella_id, umbrella_body)
    if umbrella_scope["umbrella_relations"]["children"] != child_ids:
        raise TaskWorkspaceError("umbrella ordered children contradict canonical scope relations")
    if umbrella_scope["umbrella_relations"]["owner"] is not None:
        raise TaskWorkspaceError("nested/owned umbrella execution is contradictory")
    visited: set[str] = set(); active: set[str] = set()
    def walk(task_id: str) -> None:
        if task_id in active: raise TaskWorkspaceError(f"indirect umbrella cycle detected at {task_id}")
        if task_id in visited: return
        active.add(task_id)
        _path, body = task_manifest(controller, task_id)
        scope, _sha = load_task_scope(controller, task_id, body)
        for nested in scope["umbrella_relations"]["children"]: walk(nested)
        active.remove(task_id); visited.add(task_id)
    walk(umbrella_id)
    return umbrella_scope, umbrella_scope_sha


def child_reservations(state: dict[str, Any]) -> dict[str, str]:
    value = state["queues"].setdefault("umbrella_child_reservations", {
        "schema_version": UMBRELLA_RESERVATIONS_SCHEMA, "owners": {},
    })
    if (not isinstance(value, dict) or set(value) != {"schema_version", "owners"}
            or value.get("schema_version") != UMBRELLA_RESERVATIONS_SCHEMA
            or not isinstance(value.get("owners"), dict)
            or not all(TASK_RE.fullmatch(str(child)) and TASK_RE.fullmatch(str(owner))
                       for child, owner in value["owners"].items())):
        raise TaskWorkspaceError("umbrella child reservation state is invalid")
    return value["owners"]


def state_path(controller: Path) -> Path:
    return controller / ".juno_task/state/tasks.json"


def read_state(controller: Path) -> dict[str, Any]:
    path = state_path(controller)
    if not path.exists():
        return {"schema_version": STATE_SCHEMA, "tasks": {}, "queues": {}}
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise TaskWorkspaceError(f"invalid task state: {exc}") from exc
    # Pre-queue Bolt controllers have the same task-record schema without the
    # canonical queues section. Reading adds the empty section; the next atomic
    # state write performs the one-way, data-preserving schema completion.
    if isinstance(value, dict) and set(value) == {"schema_version", "tasks"} and value.get("schema_version") == STATE_SCHEMA:
        value = {**value, "queues": {}}
    if (not isinstance(value, dict) or set(value) != {"schema_version", "tasks", "queues"}
            or value.get("schema_version") != STATE_SCHEMA
            or not isinstance(value.get("tasks"), dict) or not isinstance(value.get("queues"), dict)):
        raise TaskWorkspaceError("invalid task workspace state schema")
    return value


def write_state(controller: Path, state: dict[str, Any]) -> None:
    path = state_path(controller)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    _record_queue_attribution(controller, data)


QUEUE_ATTRIBUTION_SCHEMA = "juno_checkpoint_queue_attribution.v1"
QUEUE_ATTRIBUTION_PATH = ".juno_task/runtime/controller-checkpoint/queue-attribution.json"
_QUEUE_MISSING = object()


def _committed_state_bytes(controller: Path) -> Optional[bytes]:
    result = subprocess.run(
        ["git", "-C", str(controller), "show", "HEAD:.juno_task/state/tasks.json"],
        capture_output=True, stdin=subprocess.DEVNULL)
    return result.stdout if result.returncode == 0 else None


def _record_queue_attribution(controller: Path, data: bytes) -> None:
    """Bind the dirty queue document to an exact checkpoint attribution receipt.

    The receipt always describes the delta from the committed HEAD baseline to
    the exact bytes now on disk, using the same dotted-path walk the controller
    checkpoint verifier applies, so the declared task set and shared fields can
    never drift from what a task-scoped checkpoint will observe. The consumer
    admits queue-owned multi-task and shared-field mutations that strict
    single-task scoping must keep refusing.
    """
    baseline = _committed_state_bytes(controller)
    try:
        before = (json.loads(baseline) if baseline is not None
                  else {"schema_version": STATE_SCHEMA, "tasks": {}, "queues": {}})
        current = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return
    if not isinstance(before, dict) or not isinstance(current, dict):
        return
    before_tasks = before.get("tasks") if isinstance(before.get("tasks"), dict) else {}
    current_tasks = current.get("tasks") if isinstance(current.get("tasks"), dict) else {}
    changed_tasks = sorted(
        key for key in set(before_tasks) | set(current_tasks)
        if before_tasks.get(key, _QUEUE_MISSING) != current_tasks.get(key, _QUEUE_MISSING)
    )
    if not changed_tasks:
        # Shared-only drift is not attributable to any task lifecycle.
        return
    receipt = {
        "schema_version": QUEUE_ATTRIBUTION_SCHEMA,
        "producer": "task_workspace.write_state",
        "task_ids": changed_tasks,
        "shared_fields": _shared_queue_delta(before, current),
        "queue_document_sha256": hashlib.sha256(data).hexdigest(),
    }
    receipt_path = controller / QUEUE_ATTRIBUTION_PATH
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{receipt_path.name}.", dir=receipt_path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write((json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, receipt_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def assign_enqueue_sequence(state: dict[str, Any]) -> int:
    meta = state["queues"].setdefault(
        "task_workspace_fifo", {"schema_version": "juno_task_workspace_fifo.v1", "next": 1}
    )
    try:
        value = decisions.next_enqueue_sequence(meta)
    except ValueError as exc:
        raise TaskWorkspaceError(str(exc)) from exc
    meta["next"] += 1
    return value


@contextmanager
def state_lock(controller: Path) -> Iterator[Callable[[bool], None]]:
    # Runtime locks are ignored controller-local state; only tasks.json is durable truth.
    lock = controller / ".juno_task/runtime/task-workspace.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        locked = True

        def set_locked(required: bool) -> None:
            nonlocal locked
            if required == locked:
                return
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX if required else fcntl.LOCK_UN)
            locked = required

        try:
            yield set_locked
        finally:
            if not locked:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


@contextmanager
def finish_lock(controller: Path, task_id: str) -> Iterator[None]:
    lock = controller / ".juno_task/runtime/task-workspace" / f"{task_id}.finish.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


def _append_tail(buffer: bytearray, data: bytes, limit: int) -> None:
    buffer.extend(data)
    if len(buffer) > limit:
        del buffer[:len(buffer) - limit]


def _log_component(value: str, fallback: str) -> str:
    cleaned = __import__("re").sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-.")
    return cleaned[:64] or fallback


def allocate_long_run_log(workflow: str, task: str) -> tuple[Path, Any]:
    """Exclusively allocate and announce one predictable, globally observable log."""
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    base = f"yy-{_log_component(workflow, 'run')}-{_log_component(task, 'task')}-{stamp}"
    for suffix in ("", *[f"-{number}" for number in range(1, 100)]):
        path = Path("/tmp") / f"{base}{suffix}.log"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            handle = os.fdopen(fd, "wb", buffering=0)
            print(f"yy long run log: {path}", file=sys.stderr, flush=True)
            return path, handle
        except FileExistsError:
            continue
        except OSError as exc:
            raise TaskWorkspaceError(f"cannot allocate long-run log {path}: {exc}") from exc
    raise TaskWorkspaceError(f"cannot allocate unique long-run log for {base}")


def _announce_long_run_completion(started: float, exit_code: int,
                                  timed_out: bool, log_path: Path) -> tuple[str, int]:
    finished = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    duration_ms = int((time.monotonic() - started) * 1000)
    print("yy long run complete: "
          f"finish_time={finished} duration_ms={duration_ms} exit_code={exit_code} "
          f"timed_out={'true' if timed_out else 'false'} log_path={log_path}",
          file=sys.stderr, flush=True)
    return finished, duration_ms


class ValidationTiming:
    """Monotonic, non-overlapping phase evidence with an injectable clock."""
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.started = clock()
        self.phase_started = self.started
        self.current = VALIDATION_PHASES[0]
        self.states: list[dict[str, Any]] = []

    def transition(self, state: str) -> None:
        if state not in VALIDATION_PHASES or state in {item["state"] for item in self.states}:
            raise TaskWorkspaceError(f"invalid validation timing transition: {state}")
        now = self.clock()
        self.states.append({"state": self.current,
                            "duration_ms": max(0, int((now - self.phase_started) * 1000))})
        self.current, self.phase_started = state, now

    def finish(self, outcome: str) -> dict[str, Any]:
        if outcome not in VALIDATION_TERMINALS:
            raise TaskWorkspaceError(f"invalid validation terminal outcome: {outcome}")
        now = self.clock()
        self.states.append({"state": self.current,
                            "duration_ms": max(0, int((now - self.phase_started) * 1000))})
        self.states.append({"state": outcome, "duration_ms": 0})
        wall_ms = max(0, int((now - self.started) * 1000))
        return {"schema_version": VALIDATION_TIMING_SCHEMA, "states": self.states,
                "wall_duration_ms": wall_ms, "critical_path_contribution_ms": wall_ms}


def _bounded_lock_owner(handle: Any) -> Optional[dict[str, Any]]:
    try:
        handle.seek(0)
        raw = handle.read(4097)
        if len(raw) > 4096:
            return {"diagnostic": "owner metadata exceeded bound"}
        value = json.loads(raw.decode("utf-8")) if raw else None
        if not isinstance(value, dict):
            return None
        allowed = {"pid", "suite_id", "started_at", "command_sha256"}
        return {key: value[key] for key in allowed if key in value}
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {"diagnostic": "owner metadata unreadable"}


def _acquire_validation_resource(row: dict[str, Any], clock: Callable[[], float]) -> tuple[Any, dict[str, Any]]:
    resource = row.get("resource")
    if resource is None:
        return None, {"id": None, "lock_identity_sha256": None,
                      "wait_timeout_seconds": None, "owner_diagnostics": None}
    path = lexical_absolute(Path(resource["lock_path"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    started = clock()
    identity = hashlib.sha256(f"{resource['id']}\0{path}".encode()).hexdigest()
    owner = None
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            owner = _bounded_lock_owner(handle)
            if clock() - started >= resource["wait_timeout_seconds"]:
                handle.close()
                evidence = {"id": resource["id"], "lock_identity_sha256": identity,
                            "wait_timeout_seconds": resource["wait_timeout_seconds"],
                            "owner_diagnostics": owner}
                raise ValidationResourceTimeout(
                    f"validation resource wait timed out ({resource['id']}): owner={owner}", evidence)
            time.sleep(0.05)
    command_sha = stable_sha256(row["argv"])
    payload = {"pid": os.getpid(), "suite_id": row["id"],
               "started_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
               "command_sha256": command_sha}
    handle.seek(0); handle.truncate()
    handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    handle.flush()
    return handle, {"id": resource["id"], "lock_identity_sha256": identity,
                    "wait_timeout_seconds": resource["wait_timeout_seconds"],
                    "owner_diagnostics": owner}


def _validation_subject(row: dict[str, Any], cwd: Path) -> dict[str, Any]:
    head = git(cwd, "rev-parse", "HEAD", check=False)
    tree = git(cwd, "rev-parse", "HEAD^{tree}", check=False)
    return {"command_sha256": stable_sha256(row["argv"]),
            "cwd_sha256": hashlib.sha256(str(cwd.resolve()).encode()).hexdigest(),
            "policy_sha256": stable_sha256(row),
            "candidate_sha": head if SHA_RE.fullmatch(head) else None,
            "candidate_tree": tree if SHA_RE.fullmatch(tree) else None}


def run_validation(row: dict[str, Any], cwd: Path, *,
                   clock: Callable[[], float] = time.monotonic,
                   cancel_event: Any = None) -> dict[str, Any]:
    """Run argv-only validation with separate resource and operation budgets."""
    limit = row["max_output_bytes"]
    timing = ValidationTiming(clock)
    started_at = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    task_label = os.environ.get("JUNO_TASK_ID") or cwd.name
    resource_handle = None
    resource_evidence: dict[str, Any]
    try:
        resource_handle, resource_evidence = _acquire_validation_resource(row, clock)
    except ValidationResourceTimeout as exc:
        timing.transition("SETUP"); timing.transition("RUNNING"); timing.transition("TEARDOWN")
        evidence = timing.finish("TIMED_OUT")
        message = str(exc).encode()
        tail = message[-limit:]
        return {"id": row["id"], "argv": row["argv"], "exit_code": 124, "timed_out": True,
                "timeout_seconds": row["timeout_seconds"], "duration_ms": evidence["wall_duration_ms"],
                "started_at": started_at,
                "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
                "timing": evidence, "resource": exc.evidence,
                "identity": _validation_subject(row, cwd),
                "log_path": None, "log_sha256": hashlib.sha256(message).hexdigest(),
                "log_write_failed": False, "log_write_error": None, "stdout_tail": "",
                "stderr_tail": tail.decode(errors="replace"), "stdout_truncated_bytes": 0,
                "stderr_truncated_bytes": len(message)-len(tail),
                "stdout_sha256": hashlib.sha256(b"").hexdigest(),
                "stderr_sha256": hashlib.sha256(message).hexdigest()}
    timing.transition("SETUP")
    try:
        log_path, log_handle = allocate_long_run_log(f"validation-{row['id']}", task_label)
    except TaskWorkspaceError as exc:
        timing.transition("RUNNING"); timing.transition("TEARDOWN")
        timed = timing.finish("SETUP_FAILED")
        if resource_handle is not None:
            resource_handle.close()
        message = str(exc).encode("utf-8", errors="replace")
        tail = message[-limit:]
        return {"id": row["id"], "argv": row["argv"], "exit_code": 74,
                "timed_out": False, "timeout_seconds": row["timeout_seconds"],
                "duration_ms": timed["wall_duration_ms"], "started_at": started_at,
                "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
                "timing": timed, "resource": resource_evidence,
                "identity": _validation_subject(row, cwd), "log_path": None,
                "log_sha256": hashlib.sha256(message).hexdigest(), "log_write_failed": True,
                "log_write_error": str(exc), "stdout_tail": "",
                "stderr_tail": tail.decode("utf-8", errors="replace"),
                "stdout_truncated_bytes": 0, "stderr_truncated_bytes": len(message) - len(tail),
                "stdout_sha256": hashlib.sha256(b"").hexdigest(),
                "stderr_sha256": hashlib.sha256(message).hexdigest()}
    validation_env = lifecycle_runtime.command_execution_environment()
    try:
        process = subprocess.Popen(row["argv"], cwd=cwd, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True, env=validation_env)
    except OSError as exc:
        message = str(exc).encode("utf-8", errors="replace")
        log_handle.write(message); log_handle.close()
        timing.transition("RUNNING"); timing.transition("TEARDOWN")
        timed = timing.finish("SETUP_FAILED")
        if resource_handle is not None: resource_handle.close()
        tail = message[-limit:]
        return {"id": row["id"], "argv": row["argv"], "exit_code": 127, "timed_out": False,
                "timeout_seconds": row["timeout_seconds"], "duration_ms": timed["wall_duration_ms"],
                "started_at": started_at,
                "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
                "timing": timed, "resource": resource_evidence, "identity": _validation_subject(row, cwd),
                "log_path": str(log_path), "log_sha256": hashlib.sha256(message).hexdigest(),
                "log_write_failed": False, "log_write_error": None, "stdout_tail": "",
                "stderr_tail": tail.decode("utf-8", errors="replace"), "stdout_truncated_bytes": 0,
                "stderr_truncated_bytes": len(message)-len(tail),
                "stdout_sha256": hashlib.sha256(b"").hexdigest(),
                "stderr_sha256": hashlib.sha256(message).hexdigest()}
    timing.transition("RUNNING")
    selector = selectors.DefaultSelector()
    stdout_tail, stderr_tail = bytearray(), bytearray()
    stream_info = {process.stdout: ("stdout", stdout_tail), process.stderr: ("stderr", stderr_tail)}
    totals = {"stdout": 0, "stderr": 0}
    hashes = {"stdout": hashlib.sha256(), "stderr": hashlib.sha256()}
    for stream in stream_info:
        if stream is not None: selector.register(stream, selectors.EVENT_READ)
    # The operation budget begins only after exclusive-resource acquisition and setup.
    deadline = clock() + row["timeout_seconds"]
    timed_out = interrupted = in_teardown = False
    log_write_error: str | None = None
    try:
        while selector.get_map():
            if process.poll() is not None and not in_teardown:
                timing.transition("TEARDOWN")
                in_teardown = True
            if cancel_event is not None and cancel_event.is_set() and not interrupted and not in_teardown:
                interrupted = True
                try: os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError: pass
            if clock() >= deadline and not timed_out and not in_teardown:
                timed_out = True
                try: os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
            for key, _ in selector.select(0.05 if not timed_out else 0.01):
                stream = key.fileobj
                data = os.read(stream.fileno(), 65536)
                if not data:
                    selector.unregister(stream); continue
                name, tail = stream_info[stream]
                totals[name] += len(data); hashes[name].update(data); _append_tail(tail, data, limit)
                if log_write_error is None:
                    try: log_handle.write(data)
                    except OSError as exc:
                        log_write_error = str(exc)
                        try: os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError: pass
                sys.stderr.write(data.decode("utf-8", errors="replace")); sys.stderr.flush()
    except KeyboardInterrupt:
        interrupted = True
        try: os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError: pass
    if not in_teardown:
        timing.transition("TEARDOWN")
    process_exit_code = process.wait()
    exit_code = process_exit_code
    if interrupted: exit_code = 130
    if log_write_error is not None: exit_code = 74
    log_handle.close(); selector.close()
    for stream in stream_info:
        if stream is not None: stream.close()
    if resource_handle is not None: resource_handle.close()
    integrity = lifecycle_runtime.parsed_test_result_integrity(
        row["argv"], log_path, process_exit_code)
    if exit_code == 0 and not integrity["eligible_pass"]:
        exit_code = 65
    outcome = ("INTERRUPTED" if interrupted else "TIMED_OUT" if timed_out else
               "PASSED" if exit_code == 0 else "FAILED")
    timed = timing.finish(outcome)
    completed_at, _ = _announce_long_run_completion(timing.started, exit_code, timed_out, log_path)
    return {"id": row["id"], "argv": row["argv"], "exit_code": exit_code,
            "process_exit_code": process_exit_code, "timed_out": timed_out,
            "cancelled": interrupted and cancel_event is not None and cancel_event.is_set(),
            "result_integrity": integrity,
            "timeout_seconds": row["timeout_seconds"], "duration_ms": timed["wall_duration_ms"],
            "started_at": started_at, "completed_at": completed_at,
            "timing": timed, "resource": resource_evidence, "identity": _validation_subject(row, cwd),
            "log_path": str(log_path), "log_sha256": hashlib.sha256(log_path.read_bytes()).hexdigest(),
            "log_write_failed": log_write_error is not None, "log_write_error": log_write_error,
            "stdout_tail": bytes(stdout_tail).decode("utf-8", errors="replace"),
            "stderr_tail": bytes(stderr_tail).decode("utf-8", errors="replace"),
            "stdout_truncated_bytes": totals["stdout"] - len(stdout_tail),
            "stderr_truncated_bytes": totals["stderr"] - len(stderr_tail),
            "stdout_sha256": hashes["stdout"].hexdigest(),
            "stderr_sha256": hashes["stderr"].hexdigest()}

def run_focused_validations(rows: list[dict[str, Any]], worktree: Path) -> list[dict[str, Any]]:
    """Run independent lanes concurrently and each exclusive-resource lane in policy order."""
    if not rows:
        return []
    lanes: dict[str, list[tuple[int, dict[str, Any], Path]]] = {}
    lane_order: list[str] = []
    for index, row in enumerate(rows):
        cwd = (worktree / row["cwd"]).resolve()
        try:
            cwd.relative_to(worktree)
        except ValueError as exc:
            raise TaskWorkspaceError("focused validation cwd escaped task worktree") from exc
        resource = row.get("resource")
        lane = (f"resource:{resource['id']}:{lexical_absolute(Path(resource['lock_path']))}"
                if resource is not None else f"independent:{index}")
        if lane not in lanes:
            lanes[lane] = []
            lane_order.append(lane)
        lanes[lane].append((index, row, cwd))

    results: list[Optional[dict[str, Any]]] = [None] * len(rows)
    lane_totals: dict[str, int] = {}

    def run_lane(lane: str) -> tuple[str, list[tuple[int, dict[str, Any]]], int]:
        completed: list[tuple[int, dict[str, Any]]] = []
        total = 0
        for position, (index, row, cwd) in enumerate(lanes[lane]):
            evidence = run_validation(row, cwd)
            evidence["schedule"] = {
                "lane": "exclusive_resource" if row.get("resource") is not None else "independent",
                "policy_index": index, "lane_position": position,
                "resource_id": row.get("resource", {}).get("id"),
            }
            completed.append((index, evidence))
            total += evidence["timing"]["wall_duration_ms"]
        return lane, completed, total

    # One worker per lane: only rows declaring the same exclusive resource are
    # serialized. All resource-independent rows retain concurrent execution.
    with ThreadPoolExecutor(max_workers=len(lane_order),
                            thread_name_prefix="juno-focused-validation") as pool:
        futures = [pool.submit(run_lane, lane) for lane in lane_order]
        for future in futures:
            lane, completed, total = future.result()
            lane_totals[lane] = total
            for index, evidence in completed:
                results[index] = evidence

    critical_lane = min(lane_order, key=lambda lane: (-lane_totals[lane], lane_order.index(lane)))
    for lane in lane_order:
        for index, _row, _cwd in lanes[lane]:
            evidence = results[index]
            if evidence is None:  # Defensive: every configured row must produce terminal evidence.
                raise TaskWorkspaceError("focused validation scheduler lost terminal evidence")
            on_critical_path = lane == critical_lane
            evidence["schedule"]["critical_path"] = on_critical_path
    return [evidence for evidence in results if evidence is not None]


def target_blob(repository: Path, target_sha: str, path: str) -> bytes | None:
    """Read one exact tracked blob without trusting the controller checkout."""
    normalized_relative(path, "generated output path")
    result = subprocess.run(
        ["git", "-C", str(repository), "show", f"{target_sha}:{path}"],
        cwd=repository, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return result.stdout if result.returncode == 0 else None


def target_json(repository: Path, target_sha: str, path: str) -> tuple[dict[str, Any], str]:
    data = target_blob(repository, target_sha, path)
    if data is None:
        raise TaskWorkspaceError(f"generated-output declaration is missing: {path}")
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskWorkspaceError(f"invalid generated-output declaration {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise TaskWorkspaceError(f"invalid generated-output declaration {path}: expected object")
    return value, hashlib.sha256(data).hexdigest()


def derived_output_admission(repository: Path, target_sha: str,
                             admitted_paths: list[str]) -> tuple[list[str], dict[str, Any]]:
    """Expand admitted canonical sources to exact, declared parity destinations."""
    generated_bytes = target_blob(repository, target_sha, GENERATED_OUTPUT_DECLARATION)
    managed_bytes = target_blob(repository, target_sha, MANAGED_OUTPUT_DECLARATION)
    if generated_bytes is None and managed_bytes is None:
        return list(admitted_paths), {
            "schema_version": "juno_task_generated_output_admission.v2",
            "declarations": {}, "bindings": [],
            "scope": "product_has_no_juno_generated_output_surface",
        }
    if generated_bytes is None or managed_bytes is None:
        missing = (GENERATED_OUTPUT_DECLARATION if generated_bytes is None
                   else MANAGED_OUTPUT_DECLARATION)
        raise TaskWorkspaceError(
            f"generated-output declaration surface is partial; missing: {missing}")
    generated, generated_sha = target_json(repository, target_sha, GENERATED_OUTPUT_DECLARATION)
    if (set(generated) != {"schema_version", "source", "destinations"}
            or generated.get("schema_version") != GENERATED_OUTPUT_SCHEMA
            or not isinstance(generated.get("destinations"), list)):
        raise TaskWorkspaceError(f"invalid generated-output declaration {GENERATED_OUTPUT_DECLARATION}")
    source = normalized_relative(generated.get("source"), "generated source")
    destinations = [normalized_relative(item, "generated destination")
                    for item in generated["destinations"]]
    if not destinations or len(set(destinations)) != len(destinations) or source in destinations:
        raise TaskWorkspaceError(f"invalid generated-output declaration {GENERATED_OUTPUT_DECLARATION}")
    pairs: list[tuple[str, str, str, str]] = [
        (source, destination, "generator", GENERATED_OUTPUT_DECLARATION)
        for destination in destinations
    ]

    managed, managed_sha = target_json(repository, target_sha, MANAGED_OUTPUT_DECLARATION)
    rows = managed.get("admissionOutputs")
    schema = managed.get("schemaVersion")
    instruction_declaration = managed.get("instructionBundle")
    declaration_valid = (schema == 1 or (schema == 2 and instruction_declaration == {
        "schemaVersion": "juno_instruction_bundle_declaration.v1",
        "semanticVersion": "1.0.0"}))
    if not declaration_valid or not isinstance(managed.get("assets"), list) or not isinstance(rows, list):
        raise TaskWorkspaceError(f"invalid generated-output declaration {MANAGED_OUTPUT_DECLARATION}")
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {"source", "destination"}
                or not isinstance(row.get("source"), str)
                or not isinstance(row.get("destination"), str)):
            raise TaskWorkspaceError(f"invalid generated-output declaration {MANAGED_OUTPUT_DECLARATION}")
        managed_source = normalized_relative(
            f"juno-code/src/templates/{row.get('source')}", "managed source")
        destination = normalized_relative(row.get("destination"), "managed destination")
        if managed_source == destination:
            raise TaskWorkspaceError(f"invalid generated-output declaration {MANAGED_OUTPUT_DECLARATION}")
        pairs.append((managed_source, destination, "managed", MANAGED_OUTPUT_DECLARATION))

    seen_pairs: set[tuple[str, str]] = set()
    destination_sources: dict[str, str] = {}
    for pair_source, destination, _kind, _declaration in pairs:
        pair = (pair_source, destination)
        if pair in seen_pairs:
            raise TaskWorkspaceError(
                f"duplicate generated-output pair: {pair_source} -> {destination}")
        prior_source = destination_sources.get(destination)
        if prior_source is not None and prior_source != pair_source:
            raise TaskWorkspaceError(
                f"conflicting generated-output destination {destination}: {prior_source}, {pair_source}")
        seen_pairs.add(pair)
        destination_sources[destination] = pair_source

    declared: dict[tuple[str, str], tuple[str, str]] = {}
    for pair_source, destination, kind, declaration in pairs:
        if path_within(pair_source, admitted_paths):
            declared[(pair_source, destination)] = (kind, declaration)
    missing: list[str] = []
    bindings: list[dict[str, str]] = []
    expanded = list(admitted_paths)
    for (pair_source, destination), (kind, declaration) in sorted(declared.items()):
        source_bytes = target_blob(repository, target_sha, pair_source)
        destination_bytes = target_blob(repository, target_sha, destination)
        if source_bytes is None:
            missing.append(pair_source)
        if destination_bytes is None:
            missing.append(destination)
        if source_bytes is None or destination_bytes is None:
            continue
        if not path_within(destination, expanded):
            expanded.append(destination)
        bindings.append({
            "source": pair_source, "destination": destination, "kind": kind,
            "declaration": declaration,
            "base_source_sha256": hashlib.sha256(source_bytes).hexdigest(),
            "base_destination_sha256": hashlib.sha256(destination_bytes).hexdigest(),
        })
    if missing:
        raise TaskWorkspaceError(
            "declared generated outputs are missing at task start: " + ", ".join(sorted(set(missing)))
        )
    receipt = {
        "schema_version": "juno_task_generated_output_admission.v1",
        "declarations": {
            GENERATED_OUTPUT_DECLARATION: generated_sha,
            MANAGED_OUTPUT_DECLARATION: managed_sha,
        },
        "bindings": bindings,
    }
    return expanded, receipt


MANAGED_ASSETS_TEMPLATE_ROOT = "juno-code/src/templates/"


def _tip_tree_blobs(repository: Path, tip_sha: str) -> dict[str, str]:
    """Map every tracked blob path to its object ID at one exact commit."""
    output = git(repository, "ls-tree", "-r", tip_sha, check=False)
    blobs: dict[str, str] = {}
    for line in output.splitlines():
        metadata, separator, path = line.partition("\t")
        if not separator:
            continue
        mode, kind, object_id = metadata.split()
        if kind == "blob":
            blobs[path] = object_id
    return blobs


def managed_script_pair_drift(repository: Path, tip_sha: str) -> list[dict[str, str]]:
    """Report lifecycle script pairs whose template and runtime copies diverge.

    The declaration is read from the candidate tip itself, so a task cannot
    dodge the guardrail by narrowing the declaration. A pair whose template or
    runtime side is absent at the tip is drift: adding or removing one side of
    a declared lifecycle script must move both sides in the same candidate.
    """
    declaration_bytes = target_blob(repository, tip_sha, MANAGED_OUTPUT_DECLARATION)
    if declaration_bytes is None:
        return []
    try:
        declaration = json.loads(declaration_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskWorkspaceError(
            f"invalid managed-assets declaration {MANAGED_OUTPUT_DECLARATION}") from exc
    assets = declaration.get("assets") if isinstance(declaration, dict) else None
    if not isinstance(assets, list):
        raise TaskWorkspaceError(
            f"invalid managed-assets declaration {MANAGED_OUTPUT_DECLARATION}")
    blobs = _tip_tree_blobs(repository, tip_sha)
    drift: list[dict[str, str]] = []
    for asset in assets:
        if not isinstance(asset, dict) or asset.get("installClass") != "script":
            continue
        source = asset.get("source"); destination = asset.get("destination")
        if not isinstance(source, str) or not isinstance(destination, str):
            raise TaskWorkspaceError(
                f"invalid managed-assets declaration {MANAGED_OUTPUT_DECLARATION}")
        template_path = MANAGED_ASSETS_TEMPLATE_ROOT + source
        if blobs.get(template_path) is None or blobs.get(destination) is None \
                or blobs[template_path] != blobs[destination]:
            drift.append({"template": template_path, "runtime": destination})
    return drift


def verify_derived_output_parity(repository: Path, tip_sha: str,
                                 admission: Any, changed: list[str]) -> None:
    expected_declarations = {GENERATED_OUTPUT_DECLARATION, MANAGED_OUTPUT_DECLARATION}
    if (isinstance(admission, dict)
            and admission == {
                "schema_version": "juno_task_generated_output_admission.v2",
                "declarations": {}, "bindings": [],
                "scope": "product_has_no_juno_generated_output_surface",
            }):
        return
    if (not isinstance(admission, dict)
            or set(admission) != {"schema_version", "declarations", "bindings"}
            or admission.get("schema_version") != "juno_task_generated_output_admission.v1"
            or not isinstance(admission.get("declarations"), dict)
            or set(admission["declarations"]) != expected_declarations
            or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
                   for value in admission["declarations"].values())
            or not isinstance(admission.get("bindings"), list)):
        raise TaskWorkspaceError("task creation receipt has no valid frozen generated-output admission")
    changed_set = set(changed)
    drift: list[str] = []
    seen_pairs: set[tuple[str, str]] = set()
    destination_sources: dict[str, str] = {}
    for binding in admission["bindings"]:
        if (not isinstance(binding, dict) or set(binding) != {
                "source", "destination", "kind", "declaration",
                "base_source_sha256", "base_destination_sha256"}
                or binding.get("kind") not in {"generator", "managed"}
                or binding.get("declaration") not in expected_declarations
                or any(not isinstance(binding.get(key), str)
                       or not re.fullmatch(r"[0-9a-f]{64}", binding[key])
                       for key in ("base_source_sha256", "base_destination_sha256"))):
            raise TaskWorkspaceError("task generated-output admission is invalid")
        source = normalized_relative(binding["source"], "frozen generated source")
        destination = normalized_relative(binding["destination"], "frozen generated destination")
        pair = (source, destination)
        if (pair in seen_pairs or (destination in destination_sources
                                   and destination_sources[destination] != source)):
            raise TaskWorkspaceError("task generated-output admission has duplicate or conflicting pairs")
        seen_pairs.add(pair)
        destination_sources[destination] = source
        if source not in changed_set and destination not in changed_set:
            continue
        source_bytes = target_blob(repository, tip_sha, source)
        destination_bytes = target_blob(repository, tip_sha, destination)
        if source_bytes is None or destination_bytes is None or source_bytes != destination_bytes:
            drift.append(destination)
    if drift:
        raise TaskWorkspaceError(
            "generated-output byte parity failed: " + ", ".join(sorted(set(drift)))
        )


def product_repository(controller: Path, config: dict[str, Any]) -> Path:
    return exact_root(controller / config["repository"], "configured product repository")


def ref_sha(repository: Path, ref: str) -> str:
    sha = git(repository, "rev-parse", f"{ref}^{{commit}}", check=False)
    if not SHA_RE.fullmatch(sha):
        raise TaskWorkspaceError(f"target ref does not resolve to a commit: {ref}")
    return sha


def optional_ref_sha(repository: Path, ref: str) -> Optional[str]:
    result = run(["git", "-C", str(repository), "rev-parse", f"{ref}^{{commit}}"], repository, check=False)
    value = result.stdout.strip()
    return value if result.returncode == 0 and SHA_RE.fullmatch(value) else None


def runtime_generation(repository: Path, target_sha: str) -> dict[str, Any]:
    """Bind the executing lifecycle bytes to the canonical target generation."""
    running_path = Path(__file__).resolve()
    try:
        running = running_path.read_bytes()
    except OSError as exc:
        raise TaskWorkspaceError(f"cannot read executing task runtime: {exc}") from exc
    target = run(["git", "-C", str(repository), "show",
                  f"{target_sha}:{RUNTIME_PATH}"], repository, check=False)
    target_bytes = target.stdout.encode("utf-8")
    running_sha = hashlib.sha256(running).hexdigest()
    target_sha256 = hashlib.sha256(target_bytes).hexdigest() if target.returncode == 0 else None
    return {"runtime_path": str(running_path), "target_path": RUNTIME_PATH,
            "running_sha256": running_sha, "target_sha256": target_sha256,
            "current": bool(target.returncode == 0 and running_sha == target_sha256)}


def _consumer_runtime_provenance(repository: Path, target_sha: str,
                                 runtime_sha256: str) -> tuple[bool, bool]:
    inventory_bytes = target_blob(repository, target_sha, MANAGED_INVENTORY_PATH)
    if inventory_bytes is None:
        return False, True
    try:
        inventory = json.loads(inventory_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False, False
    assets = inventory.get("assets") if isinstance(inventory, dict) else None
    entry = assets.get(RUNTIME_PATH) if isinstance(assets, dict) else None
    legacy = isinstance(assets, dict) and entry is None
    valid = (
        _managed_inventory_identity_valid(inventory)
        and isinstance(entry, dict)
        and set(entry) == {"type", "templateVersion", "sourceSha256", "installedSha256"}
        and entry.get("type") == "script"
        and is_valid_semver(entry.get("templateVersion"))
        and entry.get("sourceSha256") == runtime_sha256
        and entry.get("installedSha256") == runtime_sha256
    )
    return valid, legacy


def _provenance_repair_error(controller: Path, target_sha: str) -> TaskWorkspaceError:
    receipt = f"/tmp/juno-target-runtime-provenance-{target_sha}.json"
    controller_arg = shlex.quote(str(controller.resolve()))
    receipt_arg = shlex.quote(receipt)
    return TaskWorkspaceError(
        "consumer target runtime lacks exact managed-inventory provenance. Exact repair: "
        f"`yy migrate target-runtime-provenance plan --controller {controller_arg} "
        f"--output {receipt_arg}`; review it, then run "
        f"`yy migrate target-runtime-provenance apply --plan {receipt_arg} "
        f"--output {shlex.quote(receipt + '.applied')} "
        "--authorize-target-runtime-provenance`; then use `yy task runtime-bootstrap "
        "--dry-run` if the admitted package generation is still stale"
    )


def require_current_runtime(repository: Path, target_sha: str,
                            controller: Path | None = None) -> dict[str, Any]:
    generation = runtime_generation(repository, target_sha)
    source_repository = (
        target_blob(repository, target_sha, "juno-code/package.json") is not None
        or target_blob(repository, target_sha,
                       "juno-code/src/templates/scripts/task_workspace.py") is not None
    )
    if generation["current"] and not source_repository:
        provenance, legacy = _consumer_runtime_provenance(
            repository, target_sha, generation["target_sha256"])
        if provenance:
            generation["managed_inventory_provenance"] = True
            return generation
        if legacy and controller is not None:
            raise _provenance_repair_error(controller, target_sha)
        raise TaskWorkspaceError(
            "consumer target runtime managed-inventory provenance is malformed or mismatched"
        )
    if not generation["current"]:
        if source_repository:
            raise TaskWorkspaceError(
                "managed task runtime differs from a Juno source target; use a controller "
                "package/runtime matching that target, or atomically update the source package "
                "template, tracked runtime, and managed inventory if an upgrade is intended"
            )
        target_runtime = target_blob(repository, target_sha, RUNTIME_PATH)
        _, legacy_provenance = _consumer_runtime_provenance(
            repository, target_sha, generation.get("target_sha256") or "")
        if target_runtime is not None and legacy_provenance and controller is not None:
            raise _provenance_repair_error(controller, target_sha)
        raise TaskWorkspaceError(
            "managed task runtime is stale or absent from the consumer target; recover with "
            "`yy task runtime-bootstrap --dry-run`, review its receipt, then run "
            "`yy task runtime-bootstrap --apply <receipt>` and retry"
        )
    return generation


def assert_no_controller_data(repository: Path, sha: str, forbidden: list[str]) -> None:
    # Exact non-recursive prefix lookups avoid enumerating a potentially huge tree.
    offenders = [root for root in forbidden if git(repository, "ls-tree", "--name-only", sha, "--", root)]
    if offenders:
        sample = ", ".join(offenders[:5])
        raise TaskWorkspaceError(f"product target contains controller-private data ({sample}); hard-cut it before task start")


def require_full_task_materialization(worktree: Path, target_sha: str,
                                      allowed_paths: list[str],
                                      selected_entries: Optional[dict[str, dict[str, str]]] = None) -> dict[str, Any]:
    """Prove that a task role received a full checkout, never controller sparsity."""
    sparse = git(worktree, "config", "--worktree", "--bool", "--get",
                 "core.sparseCheckout", check=False).lower()
    if sparse == "true":
        raise TaskWorkspaceError("task worktree still has sparse checkout enabled")
    skipped = [line[2:] for line in git(worktree, "ls-files", "-t").splitlines()
               if line.startswith("S ")]
    if skipped:
        raise TaskWorkspaceError(
            f"task worktree still has skip-worktree paths ({', '.join(skipped[:5])})"
        )
    materialized = []
    for path in allowed_paths:
        if git(worktree, "ls-tree", "-r", "--name-only", target_sha, "--", path):
            if not (worktree / path).exists():
                raise TaskWorkspaceError(f"task worktree did not materialize tracked path: {path}")
            materialized.append(path)
    for path, entry in (selected_entries or {}).items():
        if entry["mode"] != "160000":
            continue
        nested = worktree / path
        actual = git(nested, "rev-parse", "HEAD", check=False) if nested.is_dir() else ""
        if actual != entry["object"]:
            raise TaskWorkspaceError(
                f"selected gitlink was not initialized at the target object: {path} ({entry['object']})"
            )
    return {"mode": "full", "sparse_checkout": False,
            "materialized_allowed_paths": sorted(materialized)}


def selected_task_paths(config: dict[str, Any], repository: Path, target_sha: str,
                        requested: list[str]) -> tuple[list[str], dict[str, dict[str, str]]]:
    normalized = [normalized_relative(item, "required task path") for item in requested]
    if len(set(normalized)) != len(normalized):
        raise TaskWorkspaceError("required task paths contain duplicates")
    unknown = [item for item in normalized if item not in config["selectable_paths"]]
    if unknown:
        raise TaskWorkspaceError(
            f"required task path is not admitted by policy: {', '.join(unknown)}"
        )
    entries: dict[str, dict[str, str]] = {}
    for item in normalized:
        output = git(repository, "ls-tree", target_sha, "--", item, check=False)
        lines = [line for line in output.splitlines() if line]
        if len(lines) != 1:
            raise TaskWorkspaceError(f"required task path is absent or ambiguous at target: {item}")
        metadata, actual_path = lines[0].split("\t", 1)
        mode, kind, object_id = metadata.split()
        if actual_path != item or mode not in {"040000", "160000"} or kind not in {"tree", "commit"}:
            raise TaskWorkspaceError(f"required task path has an unsafe target identity: {item}")
        entries[item] = {"mode": mode, "type": kind, "object": object_id}
    return [*config["allowed_paths"], *normalized], entries


def canonical_child_scope(controller: Path, repository: Path, base_sha: str, child_id: str,
                          body: bytes, config: dict[str, Any], expected_owner: str) -> tuple[list[str], list[dict[str, str]], dict[str, Any]]:
    """Read one pre-implementation, revision-bound authoritative scope declaration."""
    declaration, declaration_sha = load_task_scope(controller, child_id, body)
    lifecycle = declaration["lifecycle_status"]
    if lifecycle not in PRESTART_TRACKING_STATUSES:
        classification = "terminal" if lifecycle in TERMINAL_TASK_STATUSES else "active or unknown"
        raise TaskWorkspaceError(
            f"umbrella child {child_id} lifecycle is not an unowned pre-start tracking state "
            f"({classification}): {lifecycle}; allowed: {', '.join(sorted(PRESTART_TRACKING_STATUSES))}"
        )
    relation = declaration["umbrella_relations"]
    if relation["children"]:
        raise TaskWorkspaceError(
            f"flat umbrella child {child_id} must not declare nested children: {', '.join(relation['children'])}"
        )
    if relation["owner"] != expected_owner:
        raise TaskWorkspaceError(
            f"umbrella child {child_id} relation contradicts owner {expected_owner}: {relation['owner']}"
        )
    scope = declaration["scope"]
    selectable = scope["selectable_paths"]
    unknown = [path for path in selectable if path not in config["selectable_paths"]]
    if unknown:
        raise TaskWorkspaceError(f"umbrella child {child_id} has unadmitted selectable scope: {', '.join(unknown)}")
    selected_task_paths(config, repository, base_sha, selectable)
    exact = [*scope["required_paths"], *scope["generated_paths"]]
    evidence: list[dict[str, str]] = []
    for candidate in exact:
        output = git(repository, "ls-tree", base_sha, "--", candidate, check=False)
        lines = [line for line in output.splitlines() if line]
        if len(lines) != 1:
            raise TaskWorkspaceError(f"umbrella child {child_id} exact scope is absent or ambiguous: {candidate}")
        metadata, actual = lines[0].split("\t", 1); mode, kind, object_id = metadata.split()
        if actual != candidate or kind != "blob" or not mode.startswith("100"):
            raise TaskWorkspaceError(f"umbrella child {child_id} scope is not one exact tracked file: {candidate}")
        evidence.append({"path": candidate, "mode": mode, "object": object_id})
    if not scope["baseline"] and not selectable and not exact:
        raise TaskWorkspaceError(f"umbrella child {child_id} authoritative scope is empty")
    paths = [*selectable, *exact]
    frozen = {"declaration_path": str(task_scope_path(controller, child_id).resolve()),
              "declaration_sha256": declaration_sha, "declaration": declaration,
              "baseline": scope["baseline"]}
    return paths, evidence, frozen


def derive_umbrella_admission(controller: Path, umbrella_id: str, repository: Path,
                              target_ref: str, base_sha: str, input_path: Path,
                              baseline_paths: list[str], state: dict[str, Any],
                              config: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    declaration, source_sha = load_umbrella_input(input_path)
    child_ids = declaration["children"]
    _umbrella_path, umbrella_body = task_manifest(controller, umbrella_id)
    umbrella_scope, umbrella_scope_sha = validate_umbrella_graph(
        controller, umbrella_id, child_ids, umbrella_body)
    if umbrella_id in child_ids:
        raise TaskWorkspaceError(f"umbrella child is self-referential or cyclic: {umbrella_id}")
    reservations = child_reservations(state)
    bindings: list[dict[str, Any]] = []
    union = list(baseline_paths)
    for child_id in child_ids:
        owner = state["tasks"].get(child_id)
        reserved = reservations.get(child_id)
        if owner is not None or (reserved is not None and reserved != umbrella_id):
            identity = reserved or (owner.get("task_id", child_id) if isinstance(owner, dict) else child_id)
            raise TaskWorkspaceError(f"umbrella child {child_id} is already owned by {identity}")
        _path, body = task_manifest(controller, child_id)
        exact_paths, evidence, frozen_scope = canonical_child_scope(
            controller, repository, base_sha, child_id, body, config, umbrella_id)
        for required in exact_paths:
            if not path_within(required, union):
                union.append(required)
        bindings.append({
            "task_id": child_id,
            "task_revision_sha256": hashlib.sha256(body).hexdigest(),
            "scope_evidence": evidence,
            "scope_evidence_sha256": stable_sha256(evidence),
            "required_paths": exact_paths, "canonical_scope": frozen_scope,
            "target_ref": target_ref, "base_sha": base_sha,
        })
    admission = {
        "schema_version": UMBRELLA_ADMISSION_SCHEMA,
        "execution_mode": UMBRELLA_EXECUTION_MODE,
        "input_path": str(input_path.resolve()), "input_sha256": source_sha,
        "umbrella_scope_sha256": umbrella_scope_sha, "umbrella_scope": umbrella_scope,
        "ordered_child_ids": child_ids,
        "child_bindings": bindings,
        "union_paths": sorted(union),
        "union_paths_sha256": stable_sha256(sorted(union)),
    }
    return sorted(union), admission


def finalize_umbrella_admission(repository: Path, base_sha: str, union: list[str],
                                admission: dict[str, Any]) -> tuple[list[str], dict[str, Any], dict[str, Any]]:
    _all_paths, all_generated = derived_output_admission(repository, base_sha, ["juno-code"])
    generated_by_child: dict[str, list[dict[str, str]]] = {}
    expanded = list(union)
    for binding in admission["child_bindings"]:
        pairs = [row for row in all_generated["bindings"]
                 if (path_within(row["source"], binding["required_paths"])
                     or path_within(row["destination"], binding["required_paths"]))]
        for row in pairs:
            for exact in (row["source"], row["destination"]):
                if not path_within(exact, expanded):
                    expanded.append(exact)
        generated_by_child[binding["task_id"]] = sorted([
            {"source": row["source"], "destination": row["destination"], "kind": row["kind"]}
            for row in pairs
        ], key=lambda row: (row["source"], row["destination"], row["kind"]))
    union, generated = derived_output_admission(repository, base_sha, expanded)
    return sorted(union), {**admission, "union_paths": sorted(union),
                           "union_paths_sha256": stable_sha256(sorted(union)),
                           "generated_output_bindings": generated_by_child}, generated


def umbrella_drift(controller: Path, repository: Path, admission: Any,
                   generated: Any, state: dict[str, Any], umbrella_id: str) -> list[dict[str, str]]:
    expected_keys = {"schema_version", "execution_mode", "input_path", "input_sha256",
                     "umbrella_scope_sha256", "umbrella_scope", "ordered_child_ids",
                     "child_bindings", "union_paths", "union_paths_sha256", "generated_output_bindings"}
    if (not isinstance(admission, dict) or set(admission) != expected_keys
            or admission.get("schema_version") != UMBRELLA_ADMISSION_SCHEMA
            or admission.get("execution_mode") != UMBRELLA_EXECUTION_MODE
            or not isinstance(admission.get("ordered_child_ids"), list)
            or not isinstance(admission.get("child_bindings"), list)):
        return [{"reason": "malformed_frozen_admission"}]
    drift: list[dict[str, str]] = []
    try:
        _input, current_input_sha = load_umbrella_input(Path(admission["input_path"]))
        if current_input_sha != admission["input_sha256"]:
            drift.append({"reason": "umbrella_input_bytes_drift"})
    except (TaskWorkspaceError, TypeError):
        drift.append({"reason": "umbrella_input_unavailable"})
    if (admission["ordered_child_ids"] != [row.get("task_id") for row in admission["child_bindings"]]
            or stable_sha256(admission.get("union_paths")) != admission.get("union_paths_sha256")):
        drift.append({"reason": "order_or_union_hash_drift"})
    reservations = child_reservations(state)
    try:
        _umbrella_path, umbrella_body = task_manifest(controller, umbrella_id)
        current_umbrella_scope, current_umbrella_sha = load_task_scope(controller, umbrella_id, umbrella_body)
        if (current_umbrella_scope != admission["umbrella_scope"]
                or current_umbrella_sha != admission["umbrella_scope_sha256"]):
            drift.append({"reason": "umbrella_scope_drift"})
    except TaskWorkspaceError:
        drift.append({"reason": "umbrella_scope_unavailable"})
    generated_pairs = {(row.get("source"), row.get("destination"), row.get("kind"))
                       for row in generated.get("bindings", [])} if isinstance(generated, dict) else set()
    bound_targets = {(row.get("target_ref"), row.get("base_sha"))
                     for row in admission["child_bindings"] if isinstance(row, dict)}
    if len(bound_targets) != 1:
        drift.append({"reason": "child_target_or_base_binding_drift"})
    for binding in admission["child_bindings"]:
        child_id = binding.get("task_id", "unknown") if isinstance(binding, dict) else "unknown"
        if (not isinstance(binding, dict) or set(binding) != {"task_id", "task_revision_sha256",
                "scope_evidence", "scope_evidence_sha256", "required_paths", "canonical_scope",
                "target_ref", "base_sha"}):
            drift.append({"task_id": child_id, "reason": "malformed_child_binding"})
            continue
        try:
            _path, body = task_manifest(controller, child_id)
            config = load_config(controller)
            paths, evidence, frozen_scope = canonical_child_scope(
                controller, repository, binding.get("base_sha", ""), child_id, body, config, umbrella_id)
        except TaskWorkspaceError:
            drift.append({"task_id": child_id, "reason": "canonical_child_unavailable"})
            continue
        if (not compatible_task_revision(controller, child_id, body,
                                         binding.get("task_revision_sha256"))
                or paths != binding.get("required_paths")
                or evidence != binding.get("scope_evidence")
                or stable_sha256(evidence) != binding.get("scope_evidence_sha256")
                or frozen_scope != binding.get("canonical_scope")):
            drift.append({"task_id": child_id, "reason": "revision_or_scope_drift"})
        if reservations.get(child_id) != umbrella_id:
            drift.append({"task_id": child_id, "reason": "child_reservation_drift"})
        expected_generated = sorted(
            ({"source": source, "destination": destination, "kind": kind}
             for source, destination, kind in generated_pairs
             if path_within(str(source), paths) or path_within(str(destination), paths)),
            key=lambda row: (row["source"], row["destination"], row["kind"]),
        )
        if expected_generated != admission["generated_output_bindings"].get(child_id):
            drift.append({"task_id": child_id, "reason": "generated_binding_drift"})
    return drift


def effective_admission(record: dict[str, Any]) -> tuple[list[str], Any, str]:
    supersessions = record.get("admission_supersessions", [])
    if supersessions:
        latest = supersessions[-1]
        if (len(supersessions) != 1
                or stable_sha256(latest) != record.get("admission_supersession_sha256")):
            raise TaskWorkspaceError("authorized umbrella superseding admission identity drifted")
        return (latest["umbrella_admission"]["union_paths"],
                latest["generated_output_admission"], "superseding")
    receipt = record.get("creation_receipt", {})
    return (receipt.get("allowed_paths", []), receipt.get("generated_output_admission"), "historical_creation")


def frozen_umbrella_admission(record: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Return the effective frozen umbrella admission, never a mutable copy."""
    supersessions = record.get("admission_supersessions", [])
    if supersessions:
        return supersessions[-1].get("umbrella_admission")
    return record.get("creation_receipt", {}).get("umbrella_admission")


def umbrella_progress_projection(record: dict[str, Any],
                                 ordered_child_ids: list[str]) -> dict[str, Any]:
    """Project recorded child checkpoints onto the immutable admission order.

    Progress entries must follow the admission order strictly: each new child
    is exactly the next unrecorded child, and only the most recently recorded
    child may gain additional (rework) entries before the next child starts.
    """
    entries = record.get("umbrella_child_progress")
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        raise TaskWorkspaceError("umbrella child progress is malformed")
    previous_tip: Optional[str] = record.get("base_sha")
    sequence: list[str] = []
    for entry in entries:
        if (not isinstance(entry, dict)
                or entry.get("schema_version") != UMBRELLA_CHILD_CHECKPOINT_SCHEMA
                or not TASK_RE.fullmatch(str(entry.get("child_id", "")))
                or not SHA_RE.fullmatch(str(entry.get("tip_sha", "")))
                or not SHA_RE.fullmatch(str(entry.get("base_sha", "")))
                or not isinstance(entry.get("changed_paths"), list)):
            raise TaskWorkspaceError("umbrella child progress is malformed")
        child_id = entry["child_id"]
        if child_id not in ordered_child_ids:
            raise TaskWorkspaceError(
                f"umbrella child progress names unadmitted child {child_id}")
        if child_id not in sequence:
            if ordered_child_ids.index(child_id) != len(sequence):
                raise TaskWorkspaceError(
                    f"umbrella child progress is out of admission order at {child_id}")
            sequence.append(child_id)
        elif sequence[-1] != child_id:
            raise TaskWorkspaceError(
                f"umbrella child progress reopens closed child {child_id}")
        if entry["base_sha"] != previous_tip:
            raise TaskWorkspaceError(
                f"umbrella child progress for {child_id} does not chain from the previous tip")
        previous_tip = entry["tip_sha"]
    current = (ordered_child_ids[len(sequence)]
               if len(sequence) < len(ordered_child_ids) else None)
    return {"entries": entries, "completed_child_ids": sequence,
            "current_child_id": current,
            "remaining_child_ids": ordered_child_ids[len(sequence):],
            "latest_tip_sha": previous_tip}


def umbrella_child_allowed_paths(admission: dict[str, Any], child_id: str) -> list[str]:
    """Resolve one child's per-checkpoint boundary from the frozen admission."""
    binding = next((row for row in admission.get("child_bindings", [])
                    if isinstance(row, dict) and row.get("task_id") == child_id), None)
    if binding is None:
        raise TaskWorkspaceError(f"umbrella admission has no binding for child {child_id}")
    allowed = [path for path in binding.get("required_paths", [])
               if isinstance(path, str)]
    generated = admission.get("generated_output_bindings") or {}
    for row in generated.get(child_id, []):
        allowed.extend((row["source"], row["destination"]))
    canonical_scope = binding.get("canonical_scope") or {}
    declaration = canonical_scope.get("declaration") or {}
    scope = declaration.get("scope") or {}
    if scope.get("baseline"):
        # Baseline children union the unreserved baseline surface with their
        # own declared scope; a sibling reservation never strips a path the
        # child itself explicitly declared (sequential same-file children).
        reserved_elsewhere: list[str] = []
        for other in admission.get("child_bindings", []):
            if not isinstance(other, dict) or other.get("task_id") == child_id:
                continue
            reserved_elsewhere.extend(
                path for path in other.get("required_paths", []) if isinstance(path, str))
            for row in generated.get(other.get("task_id"), []):
                reserved_elsewhere.extend((row["source"], row["destination"]))
        allowed.extend(path for path in admission.get("union_paths", [])
                       if not path_within(path, reserved_elsewhere))
    return sorted(set(allowed))


def umbrella_child_checkpoint(controller: Path, task_id: str, child_id: str,
                              lease_token: Optional[str] = None) -> dict[str, Any]:
    """Record one sequential child's committed increment on the umbrella worktree."""
    if not TASK_RE.fullmatch(task_id) or not TASK_RE.fullmatch(child_id):
        raise TaskWorkspaceError("unsafe task id")
    if task_id == child_id:
        raise TaskWorkspaceError("umbrella child checkpoint requires a distinct child task id")
    config = load_config(controller)
    require_task(controller, task_id)
    require_task(controller, child_id)
    repository = product_repository(controller, config)
    require_current_runtime(repository, ref_sha(repository, config["target_ref"]), controller)
    with state_lock(controller):
        state = read_state(controller)
        record = state["tasks"].get(task_id)
        _require_lease_fence(controller, "child-checkpoint", task_id, lease_token, record=record)
        umbrella_gate = decisions.plan_command_transition(
            decisions.CommandRequest("child-checkpoint", task_id),
            decisions.TaskSnapshot(
                task_id, None if not isinstance(record, dict) else record.get("state")))
        if not umbrella_gate.admitted:
            raise TaskWorkspaceError(umbrella_gate.finding.message)
        admission = frozen_umbrella_admission(record)
        if not isinstance(admission, dict):
            raise TaskWorkspaceError("task has no frozen umbrella admission")
        _paths, frozen_generated, _source = effective_admission(record)
        drift = umbrella_drift(controller, repository, admission,
                               frozen_generated, state, task_id)
        if drift:
            raise TaskWorkspaceError(
                "frozen umbrella admission drifted: " + json.dumps(drift, sort_keys=True))
        frozen = json.loads(json.dumps(record))
    ordered = [child for child in admission["ordered_child_ids"]]
    if child_id not in ordered:
        raise TaskWorkspaceError(f"umbrella never admitted child {child_id}")
    projection = umbrella_progress_projection(frozen, ordered)
    current = projection["current_child_id"]
    completed = projection["completed_child_ids"]
    # The next unrecorded child is checkpointable; once every child is
    # recorded, only the most recently recorded child may still gain bounded
    # rework entries before the umbrella leaves WORKING.
    reworkable = current if current is not None else (completed[-1] if completed else None)
    if child_id != reworkable:
        detail = (f"child {child_id} already has recorded progress"
                  if child_id in completed
                  else f"current child is {current}")
        raise TaskWorkspaceError(
            f"umbrella child checkpoint is out of order: {detail}")
    _repository, _worktree, head, _changed = observe_working_task(
        frozen, repository, config, task_id)
    previous_tip = projection["latest_tip_sha"] or frozen["base_sha"]
    if head == previous_tip:
        raise TaskWorkspaceError(
            f"child {child_id} has no committed diff since checkpoint base {previous_tip}")
    child_changed = git_pathnames(
        _worktree, "diff", "--name-only", "--no-renames", "--diff-filter=ACDMRTUXB",
        "-z", f"{previous_tip}..{head}")
    if not child_changed:
        raise TaskWorkspaceError(f"child {child_id} checkpoint has no product diff")
    allowed = umbrella_child_allowed_paths(admission, child_id)
    escaped = sorted(path for path in child_changed if not path_within(path, allowed))
    if escaped:
        raise TaskWorkspaceError(
            f"child {child_id} commit escapes its admitted scope: {', '.join(escaped)}")
    binding = next(row for row in admission["child_bindings"] if row["task_id"] == child_id)
    entry = {"schema_version": UMBRELLA_CHILD_CHECKPOINT_SCHEMA,
             "umbrella_task_id": task_id, "child_id": child_id,
             "base_sha": previous_tip, "tip_sha": head,
             "changed_paths": sorted(child_changed),
             "child_binding_sha256": stable_sha256(binding),
             "recorded_at_unix_ns": time.time_ns()}
    with state_lock(controller):
        state = read_state(controller)
        record = state["tasks"].get(task_id)
        if not isinstance(record, dict) or record.get("state") != "WORKING":
            raise TaskWorkspaceError("umbrella state changed during child checkpoint")
        live_admission = frozen_umbrella_admission(record)
        if (not isinstance(live_admission, dict)
                or stable_sha256(live_admission) != stable_sha256(admission)):
            raise TaskWorkspaceError("umbrella admission changed during child checkpoint")
        live_projection = umbrella_progress_projection(record, ordered)
        live_completed = live_projection["completed_child_ids"]
        live_reworkable = (live_projection["current_child_id"]
                           if live_projection["current_child_id"] is not None
                           else (live_completed[-1] if live_completed else None))
        if (live_reworkable != child_id
                or live_projection["entries"] != projection["entries"]):
            raise TaskWorkspaceError("umbrella child progress changed during checkpoint")
        record.setdefault("umbrella_child_progress", []).append(entry)
        state["tasks"][task_id] = record
        write_state(controller, state)
    final = umbrella_progress_projection(
        read_state(controller)["tasks"][task_id], ordered)
    return {"schema_version": RECORD_SCHEMA, "task_id": task_id, "state": "WORKING",
            "outcome": "umbrella_child_checkpointed", "child_id": child_id,
            "checkpoint": entry,
            "completed_child_ids": final["completed_child_ids"],
            "current_child_id": final["current_child_id"],
            "remaining_child_ids": final["remaining_child_ids"]}


def _declared_submodule_urls(repository: Path, commit: str) -> dict[str, str]:
    raw = run(["git", "-C", str(repository), "show", f"{commit}:.gitmodules"],
              repository, check=False)
    if raw.returncode:
        return {}
    with tempfile.TemporaryDirectory(prefix="juno-gitmodules-") as temporary:
        config = Path(temporary) / ".gitmodules"
        config.write_text(raw.stdout)
        paths = run(["git", "config", "-f", str(config), "--get-regexp",
                     r"^submodule\..*\.path$"], repository, check=False).stdout.splitlines()
        result: dict[str, str] = {}
        for row in paths:
            key, _, path = row.partition(" ")
            name = key.removeprefix("submodule.").removesuffix(".path")
            url = run(["git", "config", "-f", str(config), "--get",
                       f"submodule.{name}.url"], repository, check=False).stdout.strip()
            if path and url:
                result[path] = url
        return result


def _resolved_submodule_url(parent_url: str | None, child_url: str) -> str:
    if (child_url.startswith("/") or child_url.startswith("file://")
            or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", child_url)
            or re.match(r"^[^/]+@[^:]+:", child_url)
            or not child_url.startswith(("./", "../"))):
        return child_url
    if not parent_url:
        raise TaskWorkspaceError(f"relative submodule URL has no authoritative parent remote: {child_url}")
    if parent_url.startswith("file://"):
        return "file://" + str((Path(parent_url.removeprefix("file://")).parent / child_url).resolve())
    if parent_url.startswith("/"):
        return str((Path(parent_url).parent / child_url).resolve())
    if "://" in parent_url:
        return urllib.parse.urljoin(parent_url.rstrip("/") + "/", child_url)
    scp = re.fullmatch(r"([^/:\s]+@[^:\s]+):(.+)", parent_url)
    if scp:
        resolved = posixpath.normpath(posixpath.join(scp.group(2), child_url))
        if resolved == ".." or resolved.startswith("../"):
            raise TaskWorkspaceError(f"relative submodule URL escapes SSH remote namespace: {child_url}")
        return f"{scp.group(1)}:{resolved}"
    raise TaskWorkspaceError(f"cannot resolve relative submodule URL safely: {child_url}")


def nested_gitlink_remote_closure(repository: Path, commit: str,
                                  parent_remote_url: str | None = None,
                                  prefix: str = "") -> dict[str, Any]:
    """Prove gitlinks recursively from isolated fetches of declared remotes.

    The probe repositories have no alternates and never borrow objects from a
    product worktree, so accidental local availability cannot become
    publication truth. Callers may safely run this before allocating or moving
    a worktree.
    """
    commit = ref_sha(repository, commit)
    tree = git(repository, "ls-tree", "-r", commit, check=False)
    gitlinks: list[tuple[str, str]] = []
    for line in tree.splitlines():
        metadata, separator, path = line.partition("\t")
        fields = metadata.split()
        if separator and len(fields) == 3 and fields[0] == "160000" and fields[1] == "commit":
            gitlinks.append((path, fields[2]))
    urls = _declared_submodule_urls(repository, commit)
    evidence: list[dict[str, Any]] = []
    available = True
    for path, child_sha in gitlinks:
        full_path = f"{prefix}/{path}" if prefix else path
        declared = urls.get(path)
        if not declared:
            evidence.append({"path": full_path, "sha": child_sha, "remote": None,
                             "available": False, "failed_check": "declared_remote_missing"})
            available = False
            continue
        try:
            remote = _resolved_submodule_url(parent_remote_url, declared)
        except TaskWorkspaceError as exc:
            evidence.append({"path": full_path, "sha": child_sha, "remote": declared,
                             "available": False, "failed_check": "remote_resolution",
                             "detail": str(exc)})
            available = False
            continue
        with tempfile.TemporaryDirectory(prefix="juno-gitlink-closure-") as temporary:
            probe = Path(temporary) / "probe.git"
            run(["git", "init", "--bare", str(probe)], repository)
            fetched = run(["git", "-C", str(probe), "-c", "protocol.file.allow=always",
                           "fetch", "--no-tags", "--depth=1", remote, child_sha], probe,
                          check=False)
            row: dict[str, Any] = {"path": full_path, "sha": child_sha,
                                   "remote": remote, "available": fetched.returncode == 0,
                                   "failed_check": None if fetched.returncode == 0 else "fetch_exact"}
            if fetched.returncode:
                row["detail"] = (fetched.stderr or fetched.stdout).strip()[-2000:]
                available = False
            else:
                nested = nested_gitlink_remote_closure(
                    probe, child_sha, remote, full_path)
                row["nested"] = nested["gitlinks"]
                if not nested["available"]:
                    row["available"] = False
                    row["failed_check"] = "nested_gitlink_unavailable"
                    available = False
            evidence.append(row)
    return {"root_sha": commit, "available": available, "gitlinks": evidence,
            "source": "isolated_declared_remote_fetch"}


def initialize_selected_gitlinks(worktree: Path, entries: dict[str, dict[str, str]]) -> None:
    for path, entry in entries.items():
        if entry["mode"] != "160000":
            continue
        run(["git", "-C", str(worktree), "submodule", "update", "--init", "--", path], worktree)


def branch_ref(config: dict[str, Any], task_id: str) -> str:
    ref = f"{config['branch_prefix']}{task_id}"
    if run(["git", "check-ref-format", ref], Path.cwd(), check=False).returncode:
        raise TaskWorkspaceError(f"derived task branch is invalid: {ref}")
    return ref


def worktree_path(config: dict[str, Any], task_id: str) -> Path:
    return lexical_absolute(Path(config["workspace_root"]) / task_id)


def routing_identity(controller: Path) -> dict[str, str]:
    invocation = os.environ.get("JUNO_CONTROL_INVOCATION_ROOT", "").strip()
    role = os.environ.get("JUNO_CONTROL_INVOCATION_ROLE", "").strip()
    effective = os.environ.get("JUNO_CONTROL_EFFECTIVE_ROOT", "").strip()
    policy_operation = os.environ.get("JUNO_CONTROL_OPERATION", "").strip()
    values = (invocation, role, effective, policy_operation)
    if not any(values):
        return {"invocation_root": str(controller.resolve()), "invocation_role": "controller",
                "effective_root": str(controller.resolve())}
    if not all(values) or role not in {"controller", "task", "integration-owner"}:
        raise TaskWorkspaceError("forwarded control audit identity is incomplete or invalid")
    if Path(effective).expanduser().resolve() != controller.resolve():
        raise TaskWorkspaceError("forwarded control audit effective root mismatched the controller")
    invocation_root = exact_root(Path(invocation), "control invocation root")
    controller_common = git(controller, "rev-parse", "--path-format=absolute", "--git-common-dir")
    invocation_common = git(invocation_root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    persisted_role = git(invocation_root, "config", "--worktree", "--get", "juno.workspace.role", check=False)
    role_matches = (invocation_root == controller.resolve() if role == "controller"
                    else persisted_role == role)
    if Path(controller_common).resolve() != Path(invocation_common).resolve() or not role_matches:
        raise TaskWorkspaceError("forwarded control audit invocation identity is not registered")
    return {"invocation_root": str(invocation_root), "invocation_role": role,
            "effective_root": str(controller.resolve()), "policy_operation": policy_operation}


def record_control_audit(controller: Path, surface: str, operation: str,
                         task_id: Optional[str] = None) -> dict[str, str]:
    routing = routing_identity(controller)
    forwarded_policy = routing.get("policy_operation")
    expected_policy = ("kanban" if operation in {"status", "preflight", "recovery-plan", "contract", "handoff", "evidence-status", "doctor", "lease-status"}
                       else "orchestration")
    if surface == "task" and operation not in {
            "start", "run", "recover-predispatch", "recover-wall-budget", "status", "hydrate", "preflight", "finish", "contract", "handoff",
            "checkpoint", "child-checkpoint", "evidence-run", "evidence-status", "evidence-await",
            "recovery-plan", "recovery-authorize", "recovery-apply", "sync", "doctor",
            "lease-status", "lease-heartbeat", "lease-handoff", "lease-successor",
            "lease-revoke", "lease-release"}:
        raise TaskWorkspaceError(f"unsupported task audit operation: {operation}")
    if surface == "merge" and operation not in {"status", "drive", "next", "resolve", "review", "reopen", "reconcile", "refresh", "withdraw"}:
        raise TaskWorkspaceError(f"unsupported merge audit operation: {operation}")
    if forwarded_policy is not None and forwarded_policy != expected_policy:
        raise TaskWorkspaceError(
            f"forwarded control audit policy mismatch: expected {expected_policy}, found {forwarded_policy}"
        )
    routing = {key: value for key, value in routing.items() if key != "policy_operation"}
    receipt = {
        "schema_version": "juno_control_operation_audit.v1",
        "surface": surface, "operation": operation, "policy_operation": expected_policy,
        "task_id": task_id,
        "routing": routing, "recorded_at_unix_ns": time.time_ns(),
    }
    data = (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode()
    root = controller / ".juno_task/runtime/control-audit" / surface
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{receipt['recorded_at_unix_ns']}-{secrets.token_hex(12)}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data); handle.flush(); os.fsync(handle.fileno())
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(data).hexdigest()}


def clean_identity(record: dict[str, Any], repository: Path, target_sha: str,
                   config: dict[str, Any], states: set[str] | None = None) -> bool:
    worktree = Path(record["worktree"])
    branch = record["branch_ref"]
    identity = record.get("workspace_identity", {})
    creation_receipt = record.get("creation_receipt", {})
    try:
        allowed_paths, selected_entries = selected_task_paths(
            config, repository, target_sha, creation_receipt.get("requested_paths", [])
        )
        umbrella = creation_receipt.get("umbrella_admission")
        if isinstance(umbrella, dict):
            allowed_paths = umbrella.get("union_paths", [])
        allowed_paths, generated_output_admission = derived_output_admission(
            repository, target_sha, allowed_paths)
        if (allowed_paths != creation_receipt.get("allowed_paths")
                or generated_output_admission != creation_receipt.get("generated_output_admission")):
            return False
        materialization = require_full_task_materialization(
            worktree, target_sha, allowed_paths, selected_entries
        )
    except (OSError, TaskWorkspaceError):
        return False
    return (
        record.get("state") in (states or {"WORKING"})
        and stable_sha256(creation_receipt) == identity.get("create_receipt_sha256")
        and record.get("base_sha") == target_sha
        and worktree.is_dir()
        and git(worktree, "config", "--worktree", "--get", "juno.workspace.role", check=False) == "task"
        and git(worktree, "config", "--worktree", "--get", "juno.workspace.roleBase", check=False) == target_sha
        and git(worktree, "config", "--worktree", "--get", "juno.workspace.taskId", check=False) == record.get("task_id")
        and git(worktree, "config", "--worktree", "--get", "juno.workspace.manifestIdentity", check=False) == identity.get("manifest_identity")
        and git(worktree, "config", "--worktree", "--get", "juno.workspace.createReceiptSha256", check=False) == identity.get("create_receipt_sha256")
        and git(worktree, "config", "--worktree", "--get", "juno.workspace.expectedPathsSha256", check=False) == identity.get("expected_paths_sha256")
        and stable_sha256(materialization) == identity.get("materialization_sha256")
        and git(worktree, "config", "--worktree", "--get", "juno.workspace.materializationSha256", check=False) == identity.get("materialization_sha256")
        and git(worktree, "status", "--porcelain=v1", "--untracked-files=all", check=False) == ""
        and git(worktree, "rev-parse", "HEAD", check=False) == target_sha
        and git(repository, "rev-parse", branch, check=False) == target_sha
        and git(worktree, "symbolic-ref", "-q", "HEAD", check=False) == branch
    )


def hydration_identity(repository: Path, target_sha: str, config: dict[str, Any]) -> dict[str, Any]:
    relative = config["hydration_workflow"]
    data = target_blob(repository, target_sha, relative)
    if data is None:
        return {"configured": False, "path": relative, "sha256": None,
                "reason": "legacy_target_has_no_hydration_workflow"}
    return {"configured": True, "path": relative,
            "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}


def validation_dependency_evidence(worktree: Path, config: dict[str, Any]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    seen: set[str] = set()
    rows = [*config["focused_validation"], config["full_suite_validation"]]
    for profile in config.get("validation_profiles") or []:
        rows.extend(profile["commands"])
    for row in rows:
        relative = normalized_relative(row["cwd"], "validation cwd")
        if relative in seen:
            continue
        seen.add(relative)
        cwd = worktree / relative
        node_lock = cwd / "package-lock.json"
        if node_lock.is_file():
            sentinel = cwd / "node_modules/.package-lock.json"
            if not sentinel.is_file():
                raise TaskWorkspaceError(
                    f"validation_dependencies_missing: {relative}/node_modules is absent after hydration")
            evidence.append({"cwd": relative, "ecosystem": "node",
                             "lock_path": f"{relative}/package-lock.json",
                             "lock_sha256": hashlib.sha256(node_lock.read_bytes()).hexdigest(),
                             "sentinel": f"{relative}/node_modules/.package-lock.json"})
    return evidence


HYDRATION_DIAGNOSTIC_LIMIT = 32 * 1024


def _write_hydration_lint_diagnostics(out_dir: Path, runner: Path, argv: list[str],
                                      cwd: Path, stdout: bytes, stderr: bytes, *,
                                      exit_code: int, started_at_unix_ns: int,
                                      timed_out: bool = False,
                                      error: Optional[str] = None) -> dict[str, Any]:
    """Persist bounded causal lint evidence before hydration state can fail."""
    os.chmod(out_dir, 0o700)
    streams: dict[str, Any] = {}
    for name, content in (("stdout", stdout), ("stderr", stderr)):
        tail = content[-HYDRATION_DIAGNOSTIC_LIMIT:]
        path = out_dir / f"lint.{name}"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(tail); handle.flush(); os.fsync(handle.fileno())
        streams[name] = {
            "path": str(path), "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content), "persisted_bytes": len(tail),
            "truncated_bytes": len(content) - len(tail),
        }
    diagnostic = {
        "schema_version": "juno_task_hydration_lint_diagnostic.v1",
        "stage": "lint", "argv": argv, "cwd": str(cwd.resolve()),
        "runner": {"path": str(runner),
                   "sha256": hashlib.sha256(runner.read_bytes()).hexdigest()},
        "python_executable": sys.executable, "exit_code": exit_code,
        "timed_out": timed_out, "error": error, "streams": streams,
        "started_at_unix_ns": started_at_unix_ns,
        "completed_at_unix_ns": time.time_ns(),
    }
    data = (json.dumps(diagnostic, sort_keys=True, separators=(",", ":")) + "\n").encode()
    path = out_dir / "lint-diagnostic.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data); handle.flush(); os.fsync(handle.fileno())
    directory_fd = os.open(out_dir, os.O_RDONLY)
    try: os.fsync(directory_fd)
    finally: os.close(directory_fd)
    return {"path": str(path), "sha256": hashlib.sha256(data).hexdigest()}


def _hydration_manifest_evidence(run_dir: Path) -> dict[str, Optional[str]]:
    manifest = run_dir / "manifest.json"
    return {
        "manifest_path": str(manifest) if manifest.is_file() else None,
        "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest() if manifest.is_file() else None,
    }


def _dependency_content_manifest(worktree: Path, config: dict[str, Any]) -> dict[str, str]:
    """Content-address every installed dependency file per configured lock cwd.

    npm metadata validation cannot detect tampered or corrupted installed
    bytes, so hydration records a byte manifest of every regular file and
    symlink under each lock cwd's node_modules. The manifest is stored as a
    controller-side artifact and verified before any worker budget is spent.
    """
    manifest: dict[str, str] = {}
    rows = [*config["focused_validation"], config["full_suite_validation"]]
    for profile in config.get("validation_profiles") or []:
        rows.extend(profile["commands"])
    seen: set[str] = set()
    for row in rows:
        relative = normalized_relative(row["cwd"], "validation cwd")
        if relative in seen:
            continue
        seen.add(relative)
        node_modules = worktree / relative / "node_modules"
        if not (worktree / relative / "package-lock.json").is_file():
            continue
        if not node_modules.is_dir():
            continue
        for path in sorted(node_modules.rglob("*")):
            entry = path.relative_to(worktree).as_posix()
            if path.is_symlink():
                manifest[entry] = f"link:{os.readlink(path)}"
            elif path.is_file():
                manifest[entry] = hashlib.sha256(path.read_bytes()).hexdigest()
    return manifest


def run_task_hydration(controller: Path, worktree: Path, task_id: str,
                       frozen: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    if not frozen.get("configured"):
        return {"status": "legacy_skipped", "workflow": frozen,
                "dependency_locks": [], "recovery_command": None}
    workflow = worktree / str(frozen["path"])
    if (not workflow.is_file()
            or hashlib.sha256(workflow.read_bytes()).hexdigest() != frozen["sha256"]):
        raise HydrationFailure("frozen hydration workflow is missing or drifted", {
            "status": "failed", "workflow": frozen, "failed_stage": "identity",
            "recovery_command": f"yy task hydrate {task_id}",
        })
    runner = Path(__file__).resolve().with_name("workflow_runner.sh")
    attempt = f"{time.time_ns()}-{secrets.token_hex(6)}"
    out_dir = controller / ".juno_task/runtime/task-hydration" / task_id / attempt
    out_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
    os.chmod(out_dir, 0o700)
    run_dir = out_dir / "run"
    env = dict(os.environ)
    # Hydration evaluates the already registered task worktree. Controller-side
    # wrapper assertions describe the parent, not this managed child boundary.
    env.pop("JUNO_WORKSPACE_ROLE", None)
    env.pop("JUNO_PROJECT_PATH", None)
    env["JUNO_CONTROLLER_CHECKPOINT_ACTIVE"] = "1"
    commands = [
        [sys.executable, str(runner), "lint", "--workflow", str(workflow),
         "--project-root", str(worktree)],
        [sys.executable, str(runner), "--workflow", str(workflow),
         "--project-root", str(worktree), "--out-dir", str(run_dir),
         "--no-print-step-stdout", "--print-output", "none"],
    ]
    started = time.monotonic()
    lint_diagnostic: Optional[dict[str, Any]] = None
    for stage, argv in zip(("lint", "run"), commands):
        stage_started_at_unix_ns = time.time_ns()
        try:
            completed = subprocess.run(
                argv, cwd=worktree, env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=3700, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            if stage == "lint":
                stdout = exc.stdout if isinstance(getattr(exc, "stdout", None), bytes) else b""
                stderr = exc.stderr if isinstance(getattr(exc, "stderr", None), bytes) else b""
                timed_out = isinstance(exc, subprocess.TimeoutExpired)
                lint_diagnostic = _write_hydration_lint_diagnostics(
                    out_dir, runner, argv, worktree, stdout, stderr,
                    exit_code=124 if timed_out else 127,
                    started_at_unix_ns=stage_started_at_unix_ns,
                    timed_out=timed_out, error=str(exc))
            raise HydrationFailure(f"task hydration {stage} could not complete", {
                "status": "failed", "workflow": frozen, "failed_stage": stage,
                "exit_code": 124 if isinstance(exc, subprocess.TimeoutExpired) else 127,
                "duration_ms": round((time.monotonic() - started) * 1000),
                "artifact_dir": str(out_dir), "lint_diagnostic": lint_diagnostic,
                **(_hydration_manifest_evidence(run_dir) if stage == "run" else {}),
                "recovery_command": f"yy task hydrate {task_id}",
            }) from exc
        if stage == "lint":
            lint_diagnostic = _write_hydration_lint_diagnostics(
                out_dir, runner, argv, worktree, completed.stdout, completed.stderr,
                exit_code=completed.returncode,
                started_at_unix_ns=stage_started_at_unix_ns)
        if completed.returncode:
            raise HydrationFailure(f"task hydration {stage} failed; inspect {out_dir}", {
                "status": "failed", "workflow": frozen, "failed_stage": stage,
                "exit_code": completed.returncode,
                "duration_ms": round((time.monotonic() - started) * 1000),
                "artifact_dir": str(out_dir), "lint_diagnostic": lint_diagnostic,
                **(_hydration_manifest_evidence(run_dir) if stage == "run" else {}),
                "recovery_command": f"yy task hydrate {task_id}",
            })
    drift = git(worktree, "status", "--porcelain=v1", "--untracked-files=all", check=False)
    if drift:
        raise HydrationFailure("task hydration left tracked or unignored drift", {
            "status": "failed", "workflow": frozen, "failed_stage": "clean_tree",
            "duration_ms": round((time.monotonic() - started) * 1000),
            "artifact_dir": str(out_dir), "recovery_command": f"yy task hydrate {task_id}",
        })
    try:
        dependencies = validation_dependency_evidence(worktree, config)
    except TaskWorkspaceError as exc:
        raise HydrationFailure(str(exc), {
            "status": "failed", "workflow": frozen, "failed_stage": "dependency_evidence",
            "duration_ms": round((time.monotonic() - started) * 1000),
            "artifact_dir": str(out_dir), "recovery_command": f"yy task hydrate {task_id}",
        }) from exc
    # Record a content manifest of every installed dependency byte so later
    # gates can detect tampering that npm metadata validation cannot see.
    content_manifest = _dependency_content_manifest(worktree, config)
    manifest_bytes = json.dumps(content_manifest, sort_keys=True,
                                separators=(",", ":")).encode("utf-8")
    manifest_path = out_dir / "content-manifest.json"
    temporary = manifest_path.with_name(f".{manifest_path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(manifest_bytes)
    os.replace(temporary, manifest_path)
    return {"status": "passed", "workflow": frozen,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "artifact_dir": str(out_dir),
            **_hydration_manifest_evidence(run_dir),
            "dependency_locks": dependencies,
            "content_manifest": {
                "path": str(manifest_path.resolve()),
                "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                "file_count": len(content_manifest),
            },
            "recovery_command": f"yy task hydrate {task_id}"}


def verify_hydration_evidence(record: dict[str, Any], worktree: Path) -> None:
    frozen = record.get("creation_receipt", {}).get("hydration_workflow")
    evidence = record.get("hydration")
    if not isinstance(frozen, dict) or not isinstance(evidence, dict):
        raise TaskWorkspaceError("validation_dependencies_missing: task has no hydration identity/evidence")
    allowed = {"passed"} if frozen.get("configured") else {"legacy_skipped"}
    if evidence.get("status") not in allowed or evidence.get("workflow") != frozen:
        raise TaskWorkspaceError("validation_dependencies_missing: hydration evidence is missing or stale")
    manifest_path = evidence.get("manifest_path")
    manifest_sha256 = evidence.get("manifest_sha256")
    if frozen.get("configured") and (not isinstance(manifest_path, str)
            or not isinstance(manifest_sha256, str)):
        raise TaskWorkspaceError("validation_dependencies_missing: hydration manifest evidence is absent")
    if frozen.get("configured"):
        manifest = Path(manifest_path)
        if (not manifest.is_file()
                or hashlib.sha256(manifest.read_bytes()).hexdigest() != manifest_sha256):
            raise TaskWorkspaceError("validation_dependencies_missing: hydration manifest is missing or stale")
    for lock in evidence.get("dependency_locks", []):
        lock_path = worktree / lock["lock_path"]
        sentinel = worktree / lock["sentinel"]
        if (not lock_path.is_file() or not sentinel.is_file()
                or hashlib.sha256(lock_path.read_bytes()).hexdigest() != lock["lock_sha256"]):
            raise TaskWorkspaceError(
                f"validation_dependencies_missing: {lock['cwd']} dependencies are absent or lock-mismatched; "
                f"safe recovery: run the frozen workflow at {frozen['path']} through Workflow Runner")


def _kanban_wrapper(controller: Path) -> Path:
    wrapper = controller / ".juno_task/scripts/kanban.sh"
    if not wrapper.is_file():
        raise KanbanSyncError(
            "canonical Kanban wrapper is missing",
            {"recovery_command": KANBAN_SYNC_RECOVERY.format(task="TASK_ID")})
    return wrapper


def _run_kanban(controller: Path, argv: list[str]) -> str:
    wrapper = _kanban_wrapper(controller)
    result = subprocess.run([str(wrapper), *argv], cwd=controller,
                            stdin=subprocess.DEVNULL, text=True, capture_output=True)
    if result.returncode:
        raise KanbanSyncError(
            result.stderr.strip()[:512] or "canonical Kanban wrapper failed",
            {"argv": argv[:6], "returncode": result.returncode})
    return result.stdout


def read_kanban_task(controller: Path, task_id: str) -> dict[str, Any]:
    payload = _run_kanban(controller, ["-f", "json", "get", task_id])
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise KanbanSyncError("Kanban task readback is not valid JSON",
                              {"task_id": task_id}) from exc
    if isinstance(decoded, list) and len(decoded) == 1:
        decoded = decoded[0]
    if not isinstance(decoded, dict) or decoded.get("id") != task_id:
        raise KanbanSyncError("Kanban task readback identity mismatched",
                              {"task_id": task_id})
    return decoded


def kanban_board_identity(controller: Path, task_id: str) -> dict[str, str]:
    """Canonical self identity from the task's append-only Ledger event chain.

    Unlike ``get``, history contains no relation-expanded task projections.  Its
    latest after hash is the normalized hot task record revision, while the
    immutable event hash binds that revision to this task's chain.
    """
    payload = _run_kanban(controller, ["-f", "json", "--raw", "history", task_id])
    try:
        events = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise KanbanSyncError("Kanban ledger history is not valid JSON",
                              {"task_id": task_id}) from exc
    if not isinstance(events, list) or not events:
        raise KanbanSyncError("Kanban ledger history is empty", {"task_id": task_id})
    event = events[-1]
    revision = event.get("after_sha256") if isinstance(event, dict) else None
    event_sha256 = event.get("event_sha256") if isinstance(event, dict) else None
    event_id = event.get("event_id") if isinstance(event, dict) else None
    event_task_id = event.get("task_id") if isinstance(event, dict) else None
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{16,128}", revision):
        raise KanbanSyncError("Kanban ledger revision is malformed", {"task_id": task_id})
    if not isinstance(event_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", event_sha256):
        raise KanbanSyncError("Kanban ledger event hash is malformed", {"task_id": task_id})
    if not isinstance(event_id, str) or not event_id or event_task_id != task_id:
        raise KanbanSyncError("Kanban ledger event identity mismatched", {"task_id": task_id})
    return {"task_sha256": revision, "revision": revision,
            "event_sha256": event_sha256, "event_id": event_id}


def kanban_board_revision(controller: Path, task_id: str) -> str:
    """Current normalized hot task revision from the append-only Ledger chain."""
    return kanban_board_identity(controller, task_id)["revision"]


def _kanban_sync_receipt_path(controller: Path, task_id: str, identity: dict[str, Any]) -> Path:
    name = stable_sha256(identity)
    return (controller / KANBAN_SYNC_ROOT / task_id[:2].lower() / task_id
            / f"{name}.json")


def _kanban_lifecycle_fields(lifecycle_state: str, disposition: Optional[str],
                             continuation: Optional[str]) -> dict[str, Any]:
    """Board identity fields: state, disposition, and continuation only.

    Transient labels such as the triggering phase stay in mutation receipts so
    re-projecting the same lifecycle state is exactly idempotent.
    """
    fields = {"lifecycle_projection": KANBAN_LIFECYCLE_PROJECTION,
              "lifecycle_state": lifecycle_state}
    if disposition:
        fields["lifecycle_disposition"] = disposition
    if continuation:
        fields["continuation_task_id"] = continuation
    return fields


def project_kanban_lifecycle(controller: Path, task_id: str, lifecycle_state: str, *,
                             phase: Optional[str] = None,
                             record: Optional[dict[str, Any]] = None,
                             allow_done: bool = False) -> dict[str, Any]:
    """Project one lifecycle state onto the canonical board, fail-closed.

    Idempotent: an already-projected board returns ``verified`` without a
    mutation. Every mutation is revision-CAS bound through the append-only
    ledger, writes one immutable receipt, and is readback-verified.
    """
    if lifecycle_state not in LIFECYCLE_BOARD_STATUS:
        raise KanbanSyncError(f"lifecycle state has no board projection: {lifecycle_state}",
                              {"task_id": task_id, "lifecycle_state": lifecycle_state})
    board_status = LIFECYCLE_BOARD_STATUS[lifecycle_state]
    if board_status == "done" and not allow_done:
        # Verified merge finalization exclusively owns the done mutation; the
        # projection only verifies it after the fact.
        current = read_kanban_task(controller, task_id)
        if current.get("status") == "done":
            return {"schema_version": KANBAN_SYNC_SCHEMA, "task_id": task_id,
                    "lifecycle_state": lifecycle_state,
                    "outcome": "verified",
                    "board_status": "done",
                    "recovery_command": None}
        raise KanbanSyncError(
            "merge finalization owns the done mutation; run the merge queue recovery",
            {"task_id": task_id, "lifecycle_state": lifecycle_state,
             "board_status": current.get("status"),
             "recovery_command": "yy merge next"})
    disposition = LIFECYCLE_DISPOSITIONS.get(lifecycle_state)
    continuation = None
    if isinstance(record, dict):
        for key in ("continuation_task_id", "superseded_by_task_id"):
            value = record.get(key)
            if isinstance(value, str) and TASK_RE.fullmatch(value):
                continuation = value
                break
    desired_fields = _kanban_lifecycle_fields(lifecycle_state, disposition,
                                               continuation)
    current = read_kanban_task(controller, task_id)
    current_status = current.get("status")
    current_fields = current.get("fields") if isinstance(current.get("fields"), dict) else {}
    if (current_status == board_status
            and all(current_fields.get(key) == value
                    for key, value in desired_fields.items())):
        return {"schema_version": KANBAN_SYNC_SCHEMA, "task_id": task_id,
                "lifecycle_state": lifecycle_state, "outcome": "verified",
                "board_status": board_status,
                "board_revision": kanban_board_revision(controller, task_id)}
    if current_status in TERMINAL_TASK_STATUSES and board_status not in TERMINAL_TASK_STATUSES:
        # A manual owner change is preserved, never overwritten.
        raise KanbanSyncError(
            f"canonical Kanban status {current_status} is terminal and conflicts with "
            f"lifecycle projection {lifecycle_state} -> {board_status}; "
            "resolve the owner decision, then rerun the recovery command",
            {"task_id": task_id, "lifecycle_state": lifecycle_state,
             "board_status": current_status,
             "recovery_command": KANBAN_SYNC_RECOVERY.format(task=task_id)})
    revision = kanban_board_revision(controller, task_id)
    identity = {"schema_version": KANBAN_SYNC_SCHEMA, "task_id": task_id,
                "lifecycle_state": lifecycle_state, "phase": phase,
                "board_status": board_status, "expected_revision": revision,
                "fields": desired_fields}
    receipt_path = _kanban_sync_receipt_path(controller, task_id, identity)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    argv = ["-f", "json", "update", task_id,
            "--field", f"lifecycle_projection={json.dumps(KANBAN_LIFECYCLE_PROJECTION)}",
            "--field", f"lifecycle_state={json.dumps(lifecycle_state)}"]
    if disposition:
        argv.append("--field")
        argv.append(f"lifecycle_disposition={json.dumps(disposition)}")
    if continuation:
        argv.append("--field")
        argv.append(f"continuation_task_id={json.dumps(continuation)}")
    if current_status != board_status:
        argv += ["--status", board_status]
    argv += ["--expected-revision", revision,
             "--receipt-file", str(receipt_path)]
    result = subprocess.run([str(_kanban_wrapper(controller)), *argv], cwd=controller,
                            stdin=subprocess.DEVNULL, text=True, capture_output=True)
    stderr = result.stderr.strip()
    if result.returncode or "stale task revision" in stderr:
        raise KanbanSyncError(
            "canonical Kanban projection was refused by revision CAS or failed; "
            "the board was not overwritten",
            {"task_id": task_id, "lifecycle_state": lifecycle_state,
             "board_status": current_status, "detail": stderr[:512],
             "recovery_command": KANBAN_SYNC_RECOVERY.format(task=task_id)})
    readback = read_kanban_task(controller, task_id)
    readback_fields = readback.get("fields") if isinstance(readback.get("fields"), dict) else {}
    if (readback.get("status") != board_status
            or any(readback_fields.get(key) != value
                   for key, value in desired_fields.items())):
        raise KanbanSyncError("canonical Kanban projection readback mismatched",
                              {"task_id": task_id, "lifecycle_state": lifecycle_state,
                               "board_status": readback.get("status"),
                               "recovery_command": KANBAN_SYNC_RECOVERY.format(task=task_id)})
    return {"schema_version": KANBAN_SYNC_SCHEMA, "task_id": task_id,
            "lifecycle_state": lifecycle_state, "phase": phase,
            "outcome": "projected" if current_status != board_status else "updated",
            "board_status": board_status,
            "board_revision": kanban_board_revision(controller, task_id),
            "receipt": {"path": str(receipt_path),
                        "sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest()
                        } if receipt_path.is_file() else None}


def ensure_kanban_sync(controller: Path, task_id: str, record: dict[str, Any], *,
                       phase: Optional[str] = None) -> dict[str, Any]:
    """Idempotent board sync for one task record's current lifecycle state."""
    lifecycle_state = record.get("state")
    if not isinstance(lifecycle_state, str):
        raise KanbanSyncError("task record has no lifecycle state",
                              {"task_id": task_id})
    return project_kanban_lifecycle(controller, task_id, lifecycle_state,
                                    phase=phase, record=record)


def _stamp_kanban_sync(controller: Path, task_id: str, frozen: dict[str, Any],
                       kanban_sync: dict[str, Any], *, restore_state: Optional[str] = None) -> dict[str, Any]:
    """Durably stamp sync evidence (or the explicit required state) on a record."""
    with state_lock(controller):
        state = read_state(controller)
        current = state["tasks"].get(task_id)
        if current != frozen:
            raise TaskWorkspaceError("task state changed during Kanban synchronization; "
                                     "inspect status and rerun the recovery command")
        updated = {**current, "kanban_sync": kanban_sync}
        if restore_state is not None:
            updated["state"] = restore_state
        state["tasks"][task_id] = updated
        write_state(controller, state)
        return updated


def _demote_to_kanban_sync_required(record: dict[str, Any], exc: KanbanSyncError) -> dict[str, Any]:
    """Fail-closed demotion preserving the exact restorable lifecycle state."""
    restore = record.get("state")
    if not isinstance(restore, str) or restore == KANBAN_SYNC_STATE:
        restore = "WORKING"
    return {**record, "state": KANBAN_SYNC_STATE,
            "kanban_sync": {**exc.evidence, "pending_phase": "none",
                            "restore_state": restore}}


def recover_kanban_sync(controller: Path, task_id: str,
                        lease_token: Optional[str] = None) -> dict[str, Any]:
    """One exact recovery command for a pending lifecycle board projection.

    Resumes a ``KANBAN_SYNC_REQUIRED`` record (restoring its saved lifecycle
    state, rerunning pre-hydration hydration when needed) or idempotently
    verifies/repairs the projection of any active record.
    """
    if not TASK_RE.fullmatch(task_id):
        raise TaskWorkspaceError("unsafe task id")
    config = load_config(controller)
    require_task(controller, task_id)
    with state_lock(controller):
        state = read_state(controller)
        record = state["tasks"].get(task_id)
    if not isinstance(record, dict):
        raise TaskWorkspaceError("task has not been started")
    _require_lease_fence(controller, "sync", task_id, lease_token, record=record)
    pending = record.get("state") == KANBAN_SYNC_STATE
    pending_evidence = record.get("kanban_sync") if isinstance(record.get("kanban_sync"), dict) else {}
    restore_state = pending_evidence.get("restore_state") if pending else record.get("state")
    if not isinstance(restore_state, str) or restore_state not in LIFECYCLE_BOARD_STATUS:
        raise TaskWorkspaceError(f"task sync cannot restore lifecycle state {restore_state!r}")
    pending_phase = pending_evidence.get("pending_phase") if pending else "none"
    # Project first: the board mutation is the unproven step.
    try:
        evidence = project_kanban_lifecycle(
            controller, task_id, restore_state,
            phase="sync-recovery" if pending else "sync-verify", record=record)
    except KanbanSyncError as exc:
        raise TaskWorkspaceError(
            f"task Kanban projection still failing: {exc}; "
            f"recover with: {KANBAN_SYNC_RECOVERY.format(task=task_id)}") from exc
    outcome = evidence.get("outcome")
    with state_lock(controller):
        state = read_state(controller)
        current = state["tasks"].get(task_id)
        if current != record:
            raise TaskWorkspaceError("task state changed during Kanban sync recovery; "
                                     "inspect status and rerun the recovery command")
        record = {**current, "kanban_sync": evidence}
        if pending:
            record["state"] = restore_state
        state["tasks"][task_id] = record
        write_state(controller, state)
    result = {**record, "outcome": "recovered" if pending else outcome}
    if pending and pending_phase == "hydration" and not isinstance(record.get("hydration"), dict):
        # The boundary failed before hydration ever ran: finish the exact
        # start path so one command returns the task to agent-ready truth.
        frozen_hydration = record.get("creation_receipt", {}).get("hydration_workflow")
        if not isinstance(frozen_hydration, dict):
            raise TaskWorkspaceError("task sync cannot resume hydration without its frozen identity")
        with state_lock(controller) as control_state_lock:
            state = read_state(controller)
            current = state["tasks"].get(task_id)
            if current != record or current.get("state") != "HYDRATING":
                raise TaskWorkspaceError("task state changed before hydration resume")
            frozen_record = json.loads(json.dumps(current))
            control_state_lock(False)
            try:
                hydration = run_task_hydration(
                    controller, Path(frozen_record["worktree"]), task_id,
                    frozen_hydration, config)
            except HydrationFailure as exc:
                control_state_lock(True)
                state = read_state(controller)
                if state["tasks"].get(task_id) != frozen_record:
                    raise TaskWorkspaceError("task state changed during hydration resume") from exc
                record = {**frozen_record, "state": "HYDRATION_FAILED", "hydration": exc.evidence}
                state["tasks"][task_id] = record
                write_state(controller, state)
                control_state_lock(False)
                try:
                    failure_sync = project_kanban_lifecycle(
                        controller, task_id, "HYDRATION_FAILED",
                        phase="hydration-failed", record=record)
                except KanbanSyncError as sync_exc:
                    control_state_lock(True)
                    state = read_state(controller)
                    if state["tasks"].get(task_id) != record:
                        raise TaskWorkspaceError(
                            "task state changed during hydration-failure Kanban projection") from sync_exc
                    record = _demote_to_kanban_sync_required(record, sync_exc)
                    state["tasks"][task_id] = record
                    write_state(controller, state)
                else:
                    control_state_lock(True)
                    state = read_state(controller)
                    record = {**record, "kanban_sync": failure_sync}
                    state["tasks"][task_id] = record
                    write_state(controller, state)
                raise TaskWorkspaceError(
                    f"task hydration failed during sync recovery: {exc}; "
                    f"safe recovery: yy task hydrate {task_id}") from exc
            finally:
                control_state_lock(True)
            state = read_state(controller)
            if state["tasks"].get(task_id) != frozen_record:
                raise TaskWorkspaceError("task state changed during hydration resume")
            record = {**frozen_record, "state": "WORKING", "hydration": hydration}
            state["tasks"][task_id] = record
            write_state(controller, state)
            control_state_lock(False)
            try:
                working_sync = project_kanban_lifecycle(
                    controller, task_id, "WORKING", phase="working", record=record)
            except KanbanSyncError as sync_exc:
                control_state_lock(True)
                state = read_state(controller)
                record = _demote_to_kanban_sync_required(record, sync_exc)
                state["tasks"][task_id] = record
                write_state(controller, state)
                raise TaskWorkspaceError(
                    f"task hydration resumed but its Kanban projection failed: {sync_exc}; "
                    f"recover with: {KANBAN_SYNC_RECOVERY.format(task=task_id)}") from sync_exc
            else:
                control_state_lock(True)
                state = read_state(controller)
                record = {**record, "kanban_sync": working_sync}
                state["tasks"][task_id] = record
                write_state(controller, state)
            result = {**record, "outcome": "recovered"}
    return result


def kanban_sync_doctor(controller: Path, task_id: Optional[str] = None) -> dict[str, Any]:
    """Bounded read-only reconciliation of board truth versus task records."""
    state = read_state(controller)
    records = state.get("tasks", {})
    selected = sorted(records.items())
    if task_id is not None:
        if not TASK_RE.fullmatch(task_id):
            raise TaskWorkspaceError("unsafe task id")
        if task_id not in records:
            return {"schema_version": KANBAN_SYNC_SCHEMA, "task_id": task_id,
                    "rows": [], "summary": {"examined": 0, "drift": 0},
                    "outcome": "no_task_record"}
        selected = [(task_id, records[task_id])]
    rows: list[dict[str, Any]] = []
    drift = 0
    for current_id, record in selected[:200]:
        if not isinstance(record, dict):
            continue
        lifecycle_state = record.get("state")
        expected = LIFECYCLE_BOARD_STATUS.get(lifecycle_state) if isinstance(lifecycle_state, str) else None
        reasons: list[str] = []
        board_status = None
        board_fields: dict[str, Any] = {}
        board_error = None
        try:
            board = read_kanban_task(controller, current_id)
            board_status = board.get("status")
            board_fields = board.get("fields") if isinstance(board.get("fields"), dict) else {}
        except (KanbanSyncError, OSError) as exc:
            board_error = str(exc)[:256]
            reasons.append("kanban_read_failed")
        if board_error is None:
            if (expected is not None and expected != "done"
                    and board_status in PRESTART_TRACKING_STATUSES):
                reasons.append("active_lifecycle_record_in_backlog_or_todo")
            if (isinstance(lifecycle_state, str) and lifecycle_state != "MERGED"
                    and board_status == "done"):
                reasons.append("board_done_without_merge_truth")
            if (isinstance(lifecycle_state, str) and lifecycle_state not in {"MERGED", "WITHDRAWN"}
                    and board_status == "archive"):
                reasons.append("board_archived_while_lifecycle_active")
            if (isinstance(lifecycle_state, str)
                    and board_fields.get("lifecycle_projection") == KANBAN_LIFECYCLE_PROJECTION
                    and board_fields.get("lifecycle_state") != lifecycle_state):
                reasons.append("lifecycle_field_stale")
        if isinstance(record.get("kanban_sync"), dict) and record["kanban_sync"].get("status") == "required":
            reasons.append("kanban_sync_required")
        if reasons:
            drift += 1
        rows.append({"task_id": current_id, "lifecycle_state": lifecycle_state,
                     "board_status": board_status,
                     "expected_board_status": expected,
                     "agreement": "drift" if reasons else "agree",
                     "reasons": reasons,
                     "recovery_command": (KANBAN_SYNC_RECOVERY.format(task=current_id)
                                           if reasons else None)})
    return {"schema_version": KANBAN_SYNC_SCHEMA, "task_id": task_id,
            "rows": rows,
            "summary": {"examined": len(rows), "drift": drift,
                        "agree": len(rows) - drift},
            "outcome": "drift" if drift else "agree"}


def start(controller: Path, task_id: str, requested_paths: Optional[list[str]] = None,
          umbrella_input: Optional[Path] = None,
          lease_token: Optional[str] = None) -> dict[str, Any]:
    config = load_config(controller)
    require_task(controller, task_id)
    repository = product_repository(controller, config)
    target_sha = ref_sha(repository, config["target_ref"])
    requested_paths = requested_paths or []
    allowed_paths, selected_entries = selected_task_paths(config, repository, target_sha, requested_paths)
    umbrella_admission = None
    provisional_state = read_state(controller)
    if umbrella_input is not None:
        allowed_paths, umbrella_admission = derive_umbrella_admission(
            controller, task_id, repository, config["target_ref"], target_sha,
            umbrella_input.resolve(), allowed_paths, provisional_state, config)
        allowed_paths, umbrella_admission, generated_output_admission = finalize_umbrella_admission(
            repository, target_sha, allowed_paths, umbrella_admission)
    else:
        allowed_paths, generated_output_admission = derived_output_admission(
            repository, target_sha, allowed_paths)
    frozen_hydration = hydration_identity(repository, target_sha, config)
    generation = require_current_runtime(repository, target_sha, controller)
    assert_no_controller_data(repository, target_sha, config["controller_private_paths"])
    branch = branch_ref(config, task_id)
    worktree = worktree_path(config, task_id)
    with state_lock(controller) as control_state_lock:
        state = read_state(controller)
        reservations = child_reservations(state)
        reserved_owner = reservations.get(task_id)
        start_admission = decisions.plan_command_transition(
            decisions.CommandRequest("start", task_id),
            decisions.TaskSnapshot(task_id, None, reserved_owner))
        if not start_admission.admitted:
            raise TaskWorkspaceError(start_admission.finding.message)
        if umbrella_input is not None:
            locked_baseline, locked_entries = selected_task_paths(
                config, repository, target_sha, requested_paths)
            locked_union, locked_umbrella = derive_umbrella_admission(
                controller, task_id, repository, config["target_ref"], target_sha,
                umbrella_input.resolve(), locked_baseline, state, config)
            locked_union, locked_umbrella, locked_generated = finalize_umbrella_admission(
                repository, target_sha, locked_union, locked_umbrella)
            if ((locked_union, locked_entries, locked_umbrella, locked_generated)
                    != (allowed_paths, selected_entries, umbrella_admission,
                        generated_output_admission)):
                raise TaskWorkspaceError("umbrella admission changed before mutation")
        existing = state["tasks"].get(task_id)
        if existing:
            _require_lease_fence(controller, "start", task_id, lease_token, record=existing)
            receipt = existing.get("creation_receipt", {})
            if receipt.get("requested_paths", []) != requested_paths:
                raise TaskWorkspaceError("task start required paths differ from the frozen creation receipt")
            frozen_umbrella = receipt.get("umbrella_admission")
            if ((umbrella_admission is None) != (frozen_umbrella is None)
                    or (umbrella_admission is not None and umbrella_admission != frozen_umbrella)):
                raise TaskWorkspaceError(
                    "task start umbrella admission differs from the frozen creation receipt")
            if receipt.get("hydration_workflow") != frozen_hydration:
                raise TaskWorkspaceError("task start hydration identity differs from the frozen creation receipt")
            if (existing.get("state") in {"HYDRATION_FAILED", "HYDRATING"}
                    and clean_identity(existing, repository, target_sha, config,
                                       {"HYDRATION_FAILED", "HYDRATING"})):
                # Validate and restore any persisted hydration return state so
                # an interrupted queue-owned repair hydration resumes to its
                # origin state on both the success and failure paths here as
                # well, never collapsing to WORKING or HYDRATION_FAILED.
                if existing.get("state") == "HYDRATING":
                    return_state = existing.get("hydration_return_state")
                    if return_state not in (None, "REVIEW_FINDINGS"):
                        raise TaskWorkspaceError("task hydration return state is invalid")
                else:
                    return_state = None
                frozen_existing = json.loads(json.dumps(existing))
                control_state_lock(False)
                try:
                    hydration = run_task_hydration(
                        controller, Path(existing["worktree"]), task_id, frozen_hydration, config)
                except HydrationFailure as exc:
                    control_state_lock(True)
                    state = read_state(controller)
                    if state["tasks"].get(task_id) != frozen_existing:
                        raise TaskWorkspaceError("task state changed during hydration retry") from exc
                    existing = {**existing,
                                "state": return_state or "HYDRATION_FAILED",
                                "hydration": exc.evidence}
                    state["tasks"][task_id] = existing
                    write_state(controller, state)
                    raise
                finally:
                    control_state_lock(True)
                state = read_state(controller)
                if state["tasks"].get(task_id) != frozen_existing:
                    raise TaskWorkspaceError("task state changed during hydration retry")
                existing = {**existing, "state": return_state or "WORKING",
                            "hydration": hydration}
                state["tasks"][task_id] = existing
                write_state(controller, state)
                control_state_lock(False)
                try:
                    recovered_sync = ensure_kanban_sync(
                        controller, task_id, existing, phase="hydration-recovered")
                except KanbanSyncError as sync_exc:
                    control_state_lock(True)
                    state = read_state(controller)
                    if state["tasks"].get(task_id) != existing:
                        raise TaskWorkspaceError(
                            "task state changed during hydration-recovery Kanban projection") from sync_exc
                    existing = _demote_to_kanban_sync_required(existing, sync_exc)
                    state["tasks"][task_id] = existing
                    write_state(controller, state)
                    raise TaskWorkspaceError(
                        f"hydration recovered but its Kanban projection failed: {sync_exc}; "
                        f"recover with: {KANBAN_SYNC_RECOVERY.format(task=task_id)}") from sync_exc
                else:
                    control_state_lock(True)
                    state = read_state(controller)
                    if state["tasks"].get(task_id) != existing:
                        raise TaskWorkspaceError(
                            "task state changed during hydration-recovery Kanban projection")
                    existing = {**existing, "kanban_sync": recovered_sync}
                    state["tasks"][task_id] = existing
                    write_state(controller, state)
                return {**existing, "outcome": "hydration_recovered"}
            if clean_identity(existing, repository, target_sha, config):
                control_state_lock(False)
                try:
                    # Heal board drift for an already-started task: an active
                    # record must never leave the canonical board in backlog
                    # or todo (the recorded live discrepancy class).
                    started_sync = ensure_kanban_sync(
                        controller, task_id, existing, phase="already-started")
                except KanbanSyncError as sync_exc:
                    control_state_lock(True)
                    state = read_state(controller)
                    if state["tasks"].get(task_id) != existing:
                        raise TaskWorkspaceError(
                            "task state changed during already-started Kanban projection") from sync_exc
                    existing = _demote_to_kanban_sync_required(existing, sync_exc)
                    state["tasks"][task_id] = existing
                    write_state(controller, state)
                    raise TaskWorkspaceError(
                        f"task is started but its Kanban projection failed: {sync_exc}; "
                        f"recover with: {KANBAN_SYNC_RECOVERY.format(task=task_id)}") from sync_exc
                else:
                    control_state_lock(True)
                    state = read_state(controller)
                    if state["tasks"].get(task_id) != existing:
                        raise TaskWorkspaceError(
                            "task state changed during already-started Kanban projection")
                    existing = {**existing, "kanban_sync": started_sync}
                    state["tasks"][task_id] = existing
                    write_state(controller, state)
                return {**existing, "outcome": "already_started"}
            raise TaskWorkspaceError("task start identity drifted; preserve the worktree and inspect task status")
        # show-ref is intentionally quiet; its exit status is the branch-collision contract.
        if run(["git", "-C", str(repository), "show-ref", "--verify", "--quiet", branch], repository, check=False).returncode == 0:
            raise TaskWorkspaceError(f"task branch already exists without a task record: {branch}")
        if worktree.exists():
            raise TaskWorkspaceError(f"task worktree path already exists without a task record: {worktree}")
        worktree.parent.mkdir(parents=True, exist_ok=True)
        created = False
        persisted = False
        try:
            run(["git", "-C", str(repository), "worktree", "add", "-b", branch.removeprefix("refs/heads/"), str(worktree), target_sha], repository)
            created = True
            run(["git", "-C", str(repository), "config", "extensions.worktreeConfig", "true"], repository)
            run(["git", "-C", str(worktree), "sparse-checkout", "disable"], worktree)
            initialize_selected_gitlinks(worktree, selected_entries)
            materialization = require_full_task_materialization(
                worktree, target_sha, allowed_paths, selected_entries
            )
            manifest_identity = hashlib.sha256(task_file(controller, task_id).read_bytes()).hexdigest()
            expected_paths_sha256 = stable_sha256(allowed_paths)
            materialization_sha256 = stable_sha256(materialization)
            routing = routing_identity(controller)
            creation_receipt = {"schema_version": "juno_task_workspace_creation.v1", "task_id": task_id,
                                "repository": str(repository), "target_ref": config["target_ref"],
                                "base_sha": target_sha, "branch_ref": branch, "worktree": str(worktree),
                                "manifest_identity": manifest_identity, "allowed_paths": allowed_paths,
                                "requested_paths": requested_paths, "selected_entries": selected_entries,
                                "expected_paths_sha256": expected_paths_sha256,
                                "materialization": materialization, "routing": routing,
                                "runtime_generation": generation,
                                "hydration_workflow": frozen_hydration,
                                "generated_output_admission": generated_output_admission}
            if umbrella_admission is not None:
                creation_receipt["umbrella_admission"] = umbrella_admission
            create_receipt_sha256 = stable_sha256(creation_receipt)
            identity = {"manifest_identity": manifest_identity,
                        "create_receipt_sha256": create_receipt_sha256,
                        "expected_paths_sha256": expected_paths_sha256,
                        "materialization_sha256": materialization_sha256}
            record = {"schema_version": RECORD_SCHEMA, "task_id": task_id, "state": "HYDRATING",
                      "repository": str(repository), "target_ref": config["target_ref"], "base_sha": target_sha,
                      "branch_ref": branch, "worktree": str(worktree), "tip_sha": target_sha,
                      "workspace_identity": identity, "creation_receipt": creation_receipt, "routing": routing,
                      "changed_paths": [], "validation": []}
            # Fresh admission issues the initial fencing attempt before the
            # first durable record write; its receipt precedes the state
            # mutation, and the bearer token is returned exactly once.
            issue_payload = {
                "schema_version": FENCING_RECEIPT_SCHEMA, "kind": "issue",
                "task_id": task_id, "attempt": 1, "authority_kind": "initial",
                "reason": "task start", "recorded_utc": _utc_now(),
            }
            issue_receipt = _write_fencing_receipt(controller, task_id, issue_payload)
            initial_lease, initial_token = _new_lease(
                task_id, 1, "process", "initial", issue_receipt, reason="task start",
                producer_pid=os.getpid())
            record = _apply_lease(record, initial_lease)
            state["tasks"][task_id] = record
            if umbrella_admission is not None:
                for child_id in umbrella_admission["ordered_child_ids"]:
                    reservations[child_id] = task_id
            for key, value in (("role", "task"), ("roleBase", target_sha), ("taskId", task_id),
                               ("manifestIdentity", manifest_identity),
                               ("createReceiptSha256", create_receipt_sha256),
                               ("expectedPathsSha256", expected_paths_sha256),
                               ("materializationSha256", materialization_sha256)):
                run(["git", "-C", str(worktree), "config", "--worktree", f"juno.workspace.{key}", value], worktree)
            run(["git", "-C", str(worktree), "config", "--worktree", "--unset-all",
                 "juno.workspace.roleAuthority"], worktree, check=False)
            write_state(controller, state)
            persisted = True
            frozen_record = json.loads(json.dumps(record))
            control_state_lock(False)
            try:
                # Durable start boundary: the worktree and its task record now
                # exist, so the canonical board must project in_progress with
                # structured lifecycle detail before hydration or any
                # agent-visible work begins. A failed projection preserves the
                # worktree and exposes one exact recovery command.
                try:
                    boundary_sync = project_kanban_lifecycle(
                        controller, task_id, "HYDRATING",
                        phase="start-boundary", record=record)
                except KanbanSyncError as exc:
                    control_state_lock(True)
                    state = read_state(controller)
                    if state["tasks"].get(task_id) != frozen_record:
                        raise TaskWorkspaceError(
                            "task state changed during Kanban projection; preserve evidence and inspect task status") from exc
                    record = {**record, "state": KANBAN_SYNC_STATE,
                              "kanban_sync": {**exc.evidence, "pending_phase": "hydration",
                                              "restore_state": "HYDRATING"}}
                    state["tasks"][task_id] = record
                    write_state(controller, state)
                    raise TaskWorkspaceError(
                        f"task start canonical Kanban projection failed: {exc}; "
                        f"recover with: {KANBAN_SYNC_RECOVERY.format(task=task_id)}") from exc
                hydration = run_task_hydration(
                    controller, worktree, task_id, frozen_hydration, config)
            except HydrationFailure as exc:
                control_state_lock(True)
                state = read_state(controller)
                if state["tasks"].get(task_id) != frozen_record:
                    raise TaskWorkspaceError("task state changed during initial hydration") from exc
                record = {**record, "state": "HYDRATION_FAILED", "hydration": exc.evidence}
                state["tasks"][task_id] = record
                write_state(controller, state)
                control_state_lock(False)
                try:
                    # Hydration failure is truthful active state, not success:
                    # the board keeps in_progress with the exact detail.
                    failure_sync = project_kanban_lifecycle(
                        controller, task_id, "HYDRATION_FAILED",
                        phase="hydration-failed", record=record)
                except KanbanSyncError as sync_exc:
                    control_state_lock(True)
                    state = read_state(controller)
                    if state["tasks"].get(task_id) != record:
                        raise TaskWorkspaceError(
                            "task state changed during hydration-failure Kanban projection") from sync_exc
                    record = {**record, "state": KANBAN_SYNC_STATE,
                              "kanban_sync": {**sync_exc.evidence, "pending_phase": "none",
                                              "restore_state": "HYDRATION_FAILED"}}
                    state["tasks"][task_id] = record
                    write_state(controller, state)
                else:
                    control_state_lock(True)
                    state = read_state(controller)
                    if state["tasks"].get(task_id) != record:
                        raise TaskWorkspaceError(
                            "task state changed during hydration-failure Kanban projection")
                    record = {**record, "kanban_sync": failure_sync}
                    state["tasks"][task_id] = record
                    write_state(controller, state)
                raise
            finally:
                control_state_lock(True)
            state = read_state(controller)
            if state["tasks"].get(task_id) != frozen_record:
                raise TaskWorkspaceError("task state changed during initial hydration")
            record = {**record, "state": "WORKING", "hydration": hydration}
            state["tasks"][task_id] = record
            write_state(controller, state)
            control_state_lock(False)
            try:
                working_sync = project_kanban_lifecycle(
                    controller, task_id, "WORKING", phase="working", record=record)
            except KanbanSyncError as sync_exc:
                control_state_lock(True)
                state = read_state(controller)
                if state["tasks"].get(task_id) != record:
                    raise TaskWorkspaceError(
                        "task state changed during working Kanban projection") from sync_exc
                record = {**record, "state": KANBAN_SYNC_STATE,
                          "kanban_sync": {**sync_exc.evidence, "pending_phase": "none",
                                          "restore_state": "WORKING"}}
                state["tasks"][task_id] = record
                write_state(controller, state)
                raise TaskWorkspaceError(
                    f"task start completed but its Kanban projection failed: {sync_exc}; "
                    f"recover with: {KANBAN_SYNC_RECOVERY.format(task=task_id)}") from sync_exc
            else:
                control_state_lock(True)
                state = read_state(controller)
                if state["tasks"].get(task_id) != record:
                    raise TaskWorkspaceError(
                        "task state changed during working Kanban projection")
                record = {**record, "kanban_sync": working_sync}
                state["tasks"][task_id] = record
                write_state(controller, state)
        except Exception as creation_error:
            # Creation is not admitted without durable controller truth. Keep no
            # unrecorded branch/worktree if the atomic state write itself fails.
            if created and not persisted:
                run(["git", "-C", str(worktree), "submodule", "deinit", "-f", "--all"], worktree, check=False)
                run(["git", "-C", str(repository), "worktree", "remove", "--force", str(worktree)], repository, check=False)
                run(["git", "-C", str(repository), "branch", "-D", branch.removeprefix("refs/heads/")], repository, check=False)
                branch_exists = run(["git", "-C", str(repository), "show-ref", "--verify", "--quiet", branch],
                                    repository, check=False).returncode == 0
                if worktree.exists() or branch_exists:
                    raise TaskWorkspaceError(
                        "task creation failed and registered-worktree rollback was incomplete; preserve evidence and inspect Git worktrees"
                    ) from creation_error
            raise
    return {**record, "outcome": "started", "lease_token": initial_token,
            "lease_note": "store this fencing token; gated task mutations require --lease-token"}


def hydrate(controller: Path, task_id: str, lease_token: Optional[str] = None) -> dict[str, Any]:
    """Explicitly rerun frozen hydration without broadening task authority."""
    if not TASK_RE.fullmatch(task_id):
        raise TaskWorkspaceError("unsafe task id")
    config = load_config(controller)
    require_task(controller, task_id)
    repository = product_repository(controller, config)
    with finish_lock(controller, task_id):
        with state_lock(controller):
            state = read_state(controller)
            record = state["tasks"].get(task_id)
            _require_lease_fence(controller, "hydrate", task_id, lease_token, record=record)
            hydrate_admission = decisions.plan_command_transition(
                decisions.CommandRequest("hydrate", task_id),
                decisions.TaskSnapshot(
                    task_id,
                    None if not isinstance(record, dict) else record.get("state")))
            if not hydrate_admission.admitted:
                raise TaskWorkspaceError(hydrate_admission.finding.message)
            receipt = record.get("creation_receipt", {})
            if stable_sha256(receipt) != record.get("workspace_identity", {}).get("create_receipt_sha256"):
                raise TaskWorkspaceError("task hydration creation identity drifted")
            worktree = exact_root(Path(record["worktree"]), "task worktree")
            head = git(worktree, "rev-parse", "HEAD")
            if (git(worktree, "symbolic-ref", "-q", "HEAD", check=False) != record["branch_ref"]
                    or optional_ref_sha(repository, record["branch_ref"]) != head
                    or git(worktree, "status", "--porcelain=v1", "--untracked-files=all", check=False)):
                raise TaskWorkspaceError("task hydration requires the exact clean task branch/worktree")
            frozen = receipt.get("hydration_workflow")
            if not isinstance(frozen, dict):
                raise TaskWorkspaceError("task hydration identity is absent")
            # A queue-owned repair state (REVIEW_FINDINGS) is preserved
            # through both healing and failure: hydration refreshes evidence,
            # it never reclassifies the task's lifecycle position. The return
            # state is persisted inside the HYDRATING record so an interrupted
            # hydration resumes to the same origin state after a crash.
            if record.get("state") == "REVIEW_FINDINGS":
                repair_state: Optional[str] = "REVIEW_FINDINGS"
            elif record.get("state") == "HYDRATING":
                repair_state = record.get("hydration_return_state")
                if repair_state not in (None, "REVIEW_FINDINGS"):
                    raise TaskWorkspaceError("task hydration return state is invalid")
            else:
                repair_state = None
            pending = {**record, "state": "HYDRATING",
                       "hydration_return_state": repair_state}
            state["tasks"][task_id] = pending
            write_state(controller, state)
        try:
            evidence = run_task_hydration(controller, worktree, task_id, frozen, config)
        except HydrationFailure as exc:
            with state_lock(controller):
                state = read_state(controller)
                if state["tasks"].get(task_id) != pending:
                    raise TaskWorkspaceError("task state changed during hydration") from exc
                failed = {**pending,
                          "state": repair_state or "HYDRATION_FAILED",
                          "hydration": exc.evidence}
                state["tasks"][task_id] = failed
                write_state(controller, state)
            _sync_after_hydrate(controller, task_id, failed, strict=False)
            raise
        with state_lock(controller):
            state = read_state(controller)
            if state["tasks"].get(task_id) != pending:
                raise TaskWorkspaceError("task state changed during hydration")
            completed = {**pending, "state": repair_state or "WORKING", "hydration": evidence}
            state["tasks"][task_id] = completed
            write_state(controller, state)
        completed = _sync_after_hydrate(controller, task_id, completed)
        return {**completed, "outcome": "hydrated"}


def _sync_after_hydrate(controller: Path, task_id: str, record: dict[str, Any], *,
                        strict: bool = True) -> dict[str, Any]:
    """Project the post-hydration lifecycle state, fail-closed when strict."""
    try:
        evidence = ensure_kanban_sync(controller, task_id, record, phase="hydrated")
    except KanbanSyncError as exc:
        demoted = _demote_to_kanban_sync_required(record, exc)
        try:
            updated = _stamp_kanban_sync(controller, task_id, record,
                                         demoted["kanban_sync"],
                                         restore_state=KANBAN_SYNC_STATE)
        except TaskWorkspaceError:
            return record
        if strict:
            raise TaskWorkspaceError(
                f"hydration finished but its Kanban projection failed: {exc}; "
                f"recover with: {KANBAN_SYNC_RECOVERY.format(task=task_id)}") from exc
        return updated
    if strict:
        return _stamp_kanban_sync(controller, task_id, record, evidence)
    try:
        return _stamp_kanban_sync(controller, task_id, record, evidence)
    except TaskWorkspaceError:
        return record


def _recovery_plan_locked(controller: Path, task_id: str, input_path: Path,
                          config: dict[str, Any], repository: Path,
                          state: dict[str, Any]) -> dict[str, Any]:
    record = state["tasks"].get(task_id)
    if not isinstance(record, dict) or record.get("state") != "WORKING":
        raise TaskWorkspaceError("umbrella recovery requires an already-WORKING task")
    receipt = record.get("creation_receipt", {}); predecessor_sha = stable_sha256(receipt)
    if predecessor_sha != record.get("workspace_identity", {}).get("create_receipt_sha256"):
        raise TaskWorkspaceError("historical creation receipt identity drifted; preserve this umbrella and create a replacement")
    if receipt.get("umbrella_admission") is not None:
        raise TaskWorkspaceError("umbrella already has start-time child-union admission")
    if (Path(record.get("repository", "")).resolve() != repository
            or record.get("target_ref") != config["target_ref"]
            or record.get("base_sha") != receipt.get("base_sha")
            or record.get("branch_ref") != receipt.get("branch_ref")
            or record.get("worktree") != receipt.get("worktree")
            or ref_sha(repository, config["target_ref"]) != record["base_sha"]):
        raise TaskWorkspaceError("umbrella target/base/branch/worktree identity drifted; preserve it and create a replacement")
    worktree = exact_root(Path(record["worktree"]), "recorded umbrella worktree")
    head = git(worktree, "rev-parse", "HEAD")
    if (git(worktree, "symbolic-ref", "-q", "HEAD", check=False) != record["branch_ref"]
            or optional_ref_sha(repository, record["branch_ref"]) != head
            or git(worktree, "status", "--porcelain=v1", "--untracked-files=all")):
        raise TaskWorkspaceError("umbrella recovery requires the exact clean branch/worktree identity")
    if run(["git", "-C", str(repository), "merge-base", "--is-ancestor",
            record["base_sha"], head], repository, check=False).returncode:
        raise TaskWorkspaceError("umbrella tip is rewritten or does not descend from its frozen base")
    _path, umbrella_body = task_manifest(controller, task_id)
    if hashlib.sha256(umbrella_body).hexdigest() != receipt.get("manifest_identity"):
        raise TaskWorkspaceError("umbrella task body changed since start; preserve it and create a replacement")
    baseline, _selected = selected_task_paths(config, repository, record["base_sha"], receipt.get("requested_paths", []))
    union, admission = derive_umbrella_admission(
        controller, task_id, repository, record["target_ref"], record["base_sha"],
        input_path.resolve(), baseline, state, config)
    union, admission, generated = finalize_umbrella_admission(repository, record["base_sha"], union, admission)
    original_allowed = receipt.get("allowed_paths", [])
    commits = git(worktree, "rev-list", "--reverse", "--parents", f"{record['base_sha']}..{head}").splitlines()
    history: list[dict[str, Any]] = []; escaped: list[str] = []
    for row in commits:
        commit, *parents = row.split()
        edges: list[dict[str, Any]] = []
        # Every parent edge is authority. In particular, merge commits are not
        # reduced to first-parent combined diff semantics.
        for parent in parents:
            paths = sorted(set(git(worktree, "diff", "--name-only", parent, commit).splitlines()))
            edges.append({"parent": parent, "paths": paths, "paths_sha256": stable_sha256(paths)})
            escaped.extend(path for path in paths if not path_within(path, original_allowed))
        history.append({"commit": commit, "parent_edges": edges,
                        "parent_edges_sha256": stable_sha256(edges)})
    if escaped:
        raise TaskWorkspaceError("prior umbrella commit history escaped the historical admission: " + ", ".join(sorted(set(escaped))))
    changed = sorted(set(git(worktree, "diff", "--name-only", f"{record['base_sha']}..{head}").splitlines()))
    return {"schema_version": UMBRELLA_RECOVERY_PLAN_SCHEMA, "task_id": task_id,
            "repository": str(repository), "target_ref": record["target_ref"],
            "base_sha": record["base_sha"], "branch_ref": record["branch_ref"],
            "worktree": record["worktree"], "current_tip": head,
            "predecessor_receipt_sha256": predecessor_sha,
            "umbrella_manifest_identity": receipt["manifest_identity"],
            "umbrella_input_sha256": admission["input_sha256"],
            "umbrella_admission": admission, "generated_output_admission": generated,
            "newly_admitted_paths": sorted(path for path in union if not path_within(path, original_allowed)),
            "prior_changed_paths": changed, "prior_commit_history": history,
            "prior_changes_within_predecessor": True}


def build_umbrella_recovery_plan(controller: Path, task_id: str, input_path: Path) -> dict[str, Any]:
    config = load_config(controller); require_task(controller, task_id)
    repository = product_repository(controller, config)
    with state_lock(controller):
        return _recovery_plan_locked(controller, task_id, input_path, config, repository, read_state(controller))


def authorization_ledger(state: dict[str, Any]) -> dict[str, Any]:
    value = state["queues"].setdefault("umbrella_authorization_ledger", {
        "schema_version": AUTHORIZATION_LEDGER_SCHEMA, "issued": {},
    })
    if (not isinstance(value, dict) or set(value) != {"schema_version", "issued"}
            or value.get("schema_version") != AUTHORIZATION_LEDGER_SCHEMA
            or not isinstance(value.get("issued"), dict)):
        raise TaskWorkspaceError("umbrella authorization ledger is invalid")
    return value["issued"]


def issue_umbrella_recovery_authorization(controller: Path, task_id: str,
                                           plan_path: Path, input_path: Path) -> dict[str, Any]:
    plan, plan_file_sha = read_json_object(plan_path, "umbrella recovery plan")
    plan_sha = stable_sha256(plan)
    config = load_config(controller); repository = product_repository(controller, config)
    with state_lock(controller):
        state = read_state(controller)
        expected = _recovery_plan_locked(controller, task_id, input_path, config, repository, state)
        if plan != expected:
            raise TaskWorkspaceError("only the exact current reviewed recovery plan can be authorized")
        issued = authorization_ledger(state)
        for authorization_id, row in issued.items():
            if row.get("plan_sha256") == plan_sha and row.get("plan_file_sha256") == plan_file_sha:
                return {**row, "authorization_id": authorization_id, "outcome": "already_issued"}
        authorization_id = secrets.token_hex(24)
        root = controller / ".juno_task/receipts/task-admission-authorizations"
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{task_id}-{authorization_id}.json"
        receipt = {"schema_version": UMBRELLA_AUTHORIZATION_SCHEMA,
                   "authorization_id": authorization_id, "task_id": task_id,
                   "action": "supersede_umbrella_admission", "plan_sha256": plan_sha,
                   "plan_file_sha256": plan_file_sha,
                   "predecessor_receipt_sha256": plan["predecessor_receipt_sha256"]}
        data = (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data); handle.flush(); os.fsync(handle.fileno())
        row = {"path": str(path.resolve()), "sha256": hashlib.sha256(data).hexdigest(),
               "plan_sha256": plan_sha, "plan_file_sha256": plan_file_sha,
               "predecessor_receipt_sha256": plan["predecessor_receipt_sha256"]}
        issued[authorization_id] = row
        try: write_state(controller, state)
        except Exception:
            path.unlink(missing_ok=True); raise
    return {**row, "authorization_id": authorization_id, "outcome": "issued"}


def apply_umbrella_recovery(controller: Path, task_id: str, plan_path: Path,
                            input_path: Path, authorization_path: Path) -> dict[str, Any]:
    authorization_path = authorization_path.expanduser().resolve()
    canonical_authorizations = (controller / ".juno_task/receipts/task-admission-authorizations").resolve()
    try:
        authorization_path.relative_to(canonical_authorizations)
    except ValueError as exc:
        raise TaskWorkspaceError("authorization receipt is not in the canonical immutable controller receipt root") from exc
    plan, plan_file_sha = read_json_object(plan_path, "umbrella recovery plan")
    authorization, authorization_file_sha = read_json_object(authorization_path, "umbrella recovery authorization")
    plan_sha = stable_sha256(plan)
    if (plan.get("schema_version") != UMBRELLA_RECOVERY_PLAN_SCHEMA or plan.get("task_id") != task_id
            or set(authorization) != {"schema_version", "authorization_id", "task_id", "action",
                                          "plan_sha256", "plan_file_sha256", "predecessor_receipt_sha256"}
            or authorization.get("schema_version") != UMBRELLA_AUTHORIZATION_SCHEMA
            or authorization.get("task_id") != task_id or authorization.get("action") != "supersede_umbrella_admission"
            or authorization.get("plan_sha256") != plan_sha
            or authorization.get("plan_file_sha256") != plan_file_sha
            or authorization.get("predecessor_receipt_sha256") != plan.get("predecessor_receipt_sha256")
            or not isinstance(authorization.get("authorization_id"), str) or not authorization["authorization_id"]):
        raise TaskWorkspaceError("canonical immutable recovery authorization does not bind this exact reviewed plan")
    config = load_config(controller); repository = product_repository(controller, config)
    with state_lock(controller):
        state = read_state(controller); record = state["tasks"].get(task_id)
        if not isinstance(record, dict): raise TaskWorkspaceError("umbrella disappeared before recovery apply")
        ledger_row = authorization_ledger(state).get(authorization.get("authorization_id"))
        if (not isinstance(ledger_row, dict)
                or ledger_row.get("path") != str(authorization_path)
                or ledger_row.get("sha256") != authorization_file_sha
                or ledger_row.get("plan_sha256") != plan_sha
                or ledger_row.get("plan_file_sha256") != plan_file_sha):
            raise TaskWorkspaceError("authorization receipt was not issued by trusted controller ledger")
        existing = record.get("admission_supersessions", [])
        if (existing and existing[-1].get("reviewed_plan_sha256") == plan_sha
                and existing[-1].get("authorization_receipt", {}).get("sha256") == authorization_file_sha):
            return {**record, "outcome": "already_applied", "admission_status": "authorized_superseding"}
        if existing: raise TaskWorkspaceError("umbrella already has a different superseding admission")
        expected = _recovery_plan_locked(controller, task_id, input_path, config, repository, state)
        if expected != plan:
            raise TaskWorkspaceError("recovery plan is stale or a locked identity/scope/binding changed")
        supersession = {"schema_version": UMBRELLA_SUPERSESSION_SCHEMA,
            "authorization_receipt": {"path": str(authorization_path.resolve()),
                                      "sha256": authorization_file_sha,
                                      "authorization_id": authorization["authorization_id"]},
            "reviewed_plan": {"path": str(plan_path.resolve()), "sha256": plan_sha,
                              "file_sha256": plan_file_sha},
            "reviewed_plan_sha256": plan_sha,
            "predecessor_receipt_sha256": plan["predecessor_receipt_sha256"],
            "current_tip": plan["current_tip"], "newly_admitted_paths": plan["newly_admitted_paths"],
            "unaffected_prior_evidence": {"changed_paths": plan["prior_changed_paths"],
                                          "commit_history": plan["prior_commit_history"],
                                          "within_predecessor": True},
            "umbrella_admission": plan["umbrella_admission"],
            "generated_output_admission": plan["generated_output_admission"],
            "rollback_semantics": "preserve predecessor and supersession; never narrow or rewrite either receipt",
            "refusal_semantics": "preserve umbrella and create a newly admitted replacement; never start a child worktree"}
        reservations = child_reservations(state)
        for child_id in plan["umbrella_admission"]["ordered_child_ids"]:
            if reservations.get(child_id) not in {None, task_id}:
                raise TaskWorkspaceError(f"child ownership changed before recovery apply: {child_id}")
            reservations[child_id] = task_id
        updated = {**record, "admission_supersessions": [supersession],
                   "admission_supersession_sha256": stable_sha256(supersession)}
        state["tasks"][task_id] = updated; write_state(controller, state)
    return {**updated, "outcome": "applied", "admission_status": "authorized_superseding"}


def _persist_failed_validation(controller: Path, task_id: str, frozen: dict[str, Any], validations: list[dict[str, Any]]) -> None:
    with state_lock(controller):
        state = read_state(controller)
        current = state["tasks"].get(task_id)
        if current != frozen:
            raise TaskWorkspaceError("task state changed during focused validation; inspect status and retry")
        state["tasks"][task_id] = {**current, "validation": validations,
                                   "last_validation_outcome": "TIMEOUT" if validations[-1]["timed_out"] else "FAILED"}
        write_state(controller, state)


def observe_task_identity(record: dict[str, Any], configured_repository: Path,
                           config: dict[str, Any], task_id: str) -> tuple[Path, Path, str]:
    """Verify recorded identity and return (repository, worktree, tip head)."""
    creation_receipt = record.get("creation_receipt", {})
    identity = record.get("workspace_identity", {})
    expected_worktree = worktree_path(config, task_id)
    receipt_matches = (
        isinstance(creation_receipt, dict)
        and stable_sha256(creation_receipt) == identity.get("create_receipt_sha256")
        and creation_receipt.get("task_id") == task_id
        and creation_receipt.get("repository") == record.get("repository")
        and creation_receipt.get("target_ref") == record.get("target_ref")
        and creation_receipt.get("base_sha") == record.get("base_sha")
        and creation_receipt.get("branch_ref") == record.get("branch_ref")
        and creation_receipt.get("worktree") == record.get("worktree")
        and creation_receipt.get("manifest_identity") == identity.get("manifest_identity")
        and creation_receipt.get("expected_paths_sha256") == identity.get("expected_paths_sha256")
        and stable_sha256(creation_receipt.get("allowed_paths")) == identity.get("expected_paths_sha256")
        and stable_sha256(creation_receipt.get("materialization")) == identity.get("materialization_sha256")
    )
    if record.get("task_id") != task_id or record.get("state") != "WORKING" or not receipt_matches:
        raise TaskWorkspaceError("task creation receipt or recorded identity drifted")
    try:
        recorded_repository = exact_root(
            Path(record["repository"]), "recorded task repository", physical_identity=True)
        worktree = exact_root(
            Path(record["worktree"]), "recorded task worktree", physical_identity=True)
    except (KeyError, TypeError, OSError, TaskWorkspaceError) as exc:
        raise TaskWorkspaceError(
            f"recorded task repository/worktree is missing or reused: {exc}"
        ) from exc
    if recorded_repository != configured_repository or worktree != expected_worktree:
        raise TaskWorkspaceError("task repository/worktree identity drifted")
    if (Path(git(recorded_repository, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
            != Path(git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()):
        raise TaskWorkspaceError("recorded task worktree belongs to a different repository")
    metadata = {
        "role": "task", "roleBase": record["base_sha"], "taskId": task_id,
        "manifestIdentity": identity.get("manifest_identity"),
        "createReceiptSha256": identity.get("create_receipt_sha256"),
        "expectedPathsSha256": identity.get("expected_paths_sha256"),
        "materializationSha256": identity.get("materialization_sha256"),
    }
    drifted = [key for key, expected in metadata.items()
               if not isinstance(expected, str) or not expected
               or git(worktree, "config", "--worktree", "--get",
                      f"juno.workspace.{key}", check=False) != expected]
    if drifted:
        raise TaskWorkspaceError(
            "task worktree role/identity drifted: " + ", ".join(drifted)
        )
    head = git(worktree, "rev-parse", "HEAD", check=False)
    branch = record["branch_ref"]
    if (not SHA_RE.fullmatch(head)
            or git(worktree, "symbolic-ref", "-q", "HEAD", check=False) != branch
            or git(recorded_repository, "rev-parse", branch, check=False) != head):
        raise TaskWorkspaceError("task branch/worktree identity drifted")
    return recorded_repository, worktree, head


def observe_working_task(record: dict[str, Any], configured_repository: Path,
                         config: dict[str, Any], task_id: str) -> tuple[Path, Path, str, list[str]]:
    """Read one admitted WORKING task from live Git identity, never its start snapshot."""
    recorded_repository, worktree, head = observe_task_identity(
        record, configured_repository, config, task_id)
    if git(worktree, "status", "--porcelain=v1", "--untracked-files=all"):
        raise TaskWorkspaceError("task worktree is dirty; commit or remove all changes")
    if run(["git", "-C", str(recorded_repository), "merge-base", "--is-ancestor",
            record["base_sha"], head], recorded_repository, check=False).returncode:
        raise TaskWorkspaceError("task tip no longer descends from the exact recorded base")
    changed = git_pathnames(
        worktree, "diff", "--name-only", "--no-renames", "--diff-filter=ACDMRTUXB",
        "-z", f"{record['base_sha']}..{head}"
    )
    return recorded_repository, worktree, head, changed


def observe_task_diff(record: dict[str, Any], configured_repository: Path,
                      config: dict[str, Any], task_id: str) -> tuple[Path, Path, str, list[str], list[str]]:
    """Report committed base..tip and uncommitted paths without requiring a clean tree.

    The result stays bound to the creation receipt base_sha and branch_ref and
    fails closed on missing or moved identity, exactly like the strict WORKING
    observation. Only the clean-tree requirement is lifted so mid-work status
    reads keep reporting the committed diff.
    """
    recorded_repository, worktree, head = observe_task_identity(
        record, configured_repository, config, task_id)
    if run(["git", "-C", str(recorded_repository), "merge-base", "--is-ancestor",
            record["base_sha"], head], recorded_repository, check=False).returncode:
        raise TaskWorkspaceError("task tip no longer descends from the exact recorded base")
    committed = git_pathnames(
        worktree, "diff", "--name-only", "--no-renames", "--diff-filter=ACDMRTUXB",
        "-z", f"{record['base_sha']}..{head}"
    )
    uncommitted = git_status_pathnames(worktree)
    return recorded_repository, worktree, head, committed, uncommitted


def review_ready_closure(controller: Path, config: dict[str, Any], record: dict[str, Any],
                         configured_repository: Path, task_id: str,
                         runtime: dict[str, Any]) -> tuple[
                             Path, Path, str, list[str], dict[str, Any]]:
    """Validate the cheap finish boundary and bind it as one immutable closure."""
    repository, worktree, head, changed = observe_working_task(
        record, configured_repository, config, task_id
    )
    if head == record["base_sha"]:
        raise TaskWorkspaceError("task has no committed changes")
    if not changed:
        raise TaskWorkspaceError("task has no product diff from its exact recorded base")
    forbidden = [path for path in changed if path_within(path, config["controller_private_paths"])]
    creation_receipt = record.get("creation_receipt", {})
    if stable_sha256(creation_receipt) != record.get("workspace_identity", {}).get("create_receipt_sha256"):
        raise TaskWorkspaceError("task creation receipt identity drifted")
    frozen_allowed, frozen_generated_admission, _admission_source = effective_admission(record)
    if not isinstance(frozen_allowed, list) or not frozen_allowed:
        raise TaskWorkspaceError("task admission has no frozen allowed paths")
    frozen_umbrella = (record.get("admission_supersessions", [{}])[-1].get("umbrella_admission")
                       if record.get("admission_supersessions")
                       else creation_receipt.get("umbrella_admission"))
    if frozen_umbrella is not None:
        drift = umbrella_drift(controller, repository, frozen_umbrella,
                               frozen_generated_admission, read_state(controller), task_id)
        if drift:
            raise TaskWorkspaceError(
                f"frozen umbrella child admission drifted: {json.dumps(drift, sort_keys=True)}")
    outside = [path for path in changed if not path_within(path, frozen_allowed)]
    if forbidden or outside:
        raise TaskWorkspaceError(
            f"task changed disallowed paths: {', '.join(sorted(set(forbidden + outside)))}"
        )
    verify_derived_output_parity(repository, head, frozen_generated_admission, changed)
    script_pair_drift = managed_script_pair_drift(repository, head)
    if script_pair_drift:
        divergent = ", ".join(
            f"{row['runtime']} != {row['template']}" for row in script_pair_drift)
        raise TaskWorkspaceError(
            "managed lifecycle script pairs diverged between template and runtime copies: "
            f"{divergent}; sync every divergent pair in the same candidate before queue mutation")
    policy_path = controller / ".juno_task/config/risk-policy.json"
    try:
        policy_sha256 = hashlib.sha256(policy_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise TaskWorkspaceError("risk policy is missing during task preflight") from exc
    closure_body = {
        "schema_version": "juno_task_review_ready_closure.v1",
        "task_id": task_id,
        "base_sha": record["base_sha"],
        "tip_sha": head,
        "tree_sha": git(repository, "rev-parse", f"{head}^{{tree}}"),
        "changed_paths": changed,
        "changed_paths_sha256": stable_sha256(changed),
        "allowed_paths_sha256": stable_sha256(frozen_allowed),
        "creation_receipt_sha256": record["workspace_identity"]["create_receipt_sha256"],
        "generated_output_admission_sha256": stable_sha256(
            frozen_generated_admission
        ),
        "risk_policy_sha256": policy_sha256,
        "runtime_sha256": runtime["running_sha256"],
        "unresolved_findings_candidate_sha": record.get("prior_findings_candidate_sha"),
    }
    closure = {**closure_body, "closure_sha256": stable_sha256(closure_body)}
    return repository, worktree, head, changed, closure


PREIMPLEMENTATION_CONTRACT_SCHEMA = "juno_preimplementation_acceptance.v1"
CONTRACTS_ROOT = ".juno_task/runtime/contracts"


def _contract_sections(body: str) -> dict[str, list[str]]:
    """Split a canonical task body into bounded section bullet/line lists."""
    sections: dict[str, list[str]] = {}
    current: Optional[str] = None
    for line in body.splitlines():
        header = re.match(r"(?m)^##\s+(.{1,120})\s*$", line)
        if header:
            current = header.group(1).strip().lower()[:64]
            sections.setdefault(current, [])
            continue
        if current is None:
            continue
        stripped = line.strip()
        if stripped and len(stripped) <= 512 and not stripped.startswith("```"):
            sections[current].append(stripped)
    return {name: lines[:64] for name, lines in sections.items() if lines}


def _parity_pairs(changed_paths: list[str]) -> list[dict[str, str]]:
    """Runtime/template parity surfaces implied by changed paths."""
    pairs: list[dict[str, str]] = []
    runtime_prefix = ".juno_task/scripts/"
    template_prefix = "juno-code/src/templates/scripts/"
    for path in changed_paths:
        if path.startswith(runtime_prefix):
            twin = template_prefix + path[len(runtime_prefix):]
            pairs.append({"runtime": path, "template": twin})
        elif path.startswith(template_prefix):
            twin = runtime_prefix + path[len(template_prefix):]
            pairs.append({"runtime": twin, "template": path})
    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, str]] = []
    for pair in pairs:
        key = (pair["runtime"], pair["template"])
        if key not in seen:
            seen.add(key)
            unique.append(pair)
    return unique[:16]


def _likely_test_files(changed_paths: list[str], worktree: Optional[Path]) -> list[str]:
    """Existing sibling test files for changed sources (read-only discovery)."""
    likely: list[str] = []
    for path in changed_paths:
        if not path.endswith((".ts", ".tsx", ".py")) or ".test." in path:
            continue
        stem, suffix = path.rsplit(".", 1)
        candidate = f"{stem}.test.{suffix}"
        if worktree is not None and (worktree / candidate).is_file():
            likely.append(candidate)
    return likely[:16]


def preimplementation_contract(controller: Path, task_id: str) -> dict[str, Any]:
    """Build one versioned read-only acceptance contract for a task.

    The contract is deterministic and read-only: it freezes the requirements
    digest, base/target identities, validation surface, invariant parity pairs,
    likely files, focused tests, and the final reviewer checklist derived from
    the task's own acceptance sections. Implementation handoff is refused
    (status blocked_handoff) while material owner decisions remain open. A new
    contract supersedes its predecessor by reference and never rewrites it.
    """
    if not TASK_RE.fullmatch(task_id):
        raise TaskWorkspaceError("unsafe task id")
    manifest_path, manifest_bytes = task_manifest(controller, task_id)
    config = load_config(controller)
    repository = product_repository(controller, config)
    runtime_candidates = [controller / ".juno_task/scripts/task_workspace.py",
                           repository / ".juno_task/scripts/task_workspace.py"]
    runtime_sha: Optional[str] = None
    for candidate in runtime_candidates:
        try:
            runtime_sha = hashlib.sha256(candidate.read_bytes()).hexdigest()
            break
        except OSError:
            continue
    try:
        manifest = json.loads(manifest_bytes[: manifest_bytes.find(b"---", 4)] or b"{}")
    except (UnicodeError, json.JSONDecodeError):
        manifest = {}
    body_match = re.search(rb"<!-- juno:body:start -->\n(.*?)<!-- juno:body:end -->",
                           manifest_bytes, re.S)
    body = body_match.group(1).decode("utf-8", errors="replace") if body_match else ""
    sections = _contract_sections(body)
    requirements_sha = hashlib.sha256(manifest_bytes).hexdigest()

    record: dict[str, Any] = {}
    try:
        record = read_state(controller)["tasks"].get(task_id) or {}
    except TaskWorkspaceError:
        record = {}
    worktree_value = record.get("worktree")
    worktree = Path(worktree_value) if isinstance(worktree_value, str) and worktree_value else None
    changed = sorted(set(record.get("changed_paths") or []))[:64]
    target_ref = config.get("target_ref") or record.get("target_ref")
    base_sha = record.get("base_sha")
    source_identity: dict[str, Optional[str]] = {"head": None, "tree": None}
    if worktree is not None and worktree.is_dir():
        head = git(worktree, "rev-parse", "HEAD", check=False)
        tree = git(worktree, "rev-parse", "HEAD^{tree}", check=False)
        source_identity = {"head": head or None, "tree": tree or None}

    profiles: list[dict[str, Any]] = []
    changed_roots = {path.split("/")[0] for path in changed if path}
    for profile in config.get("validation_profiles") or []:
        if not isinstance(profile, dict):
            continue
        roots = set(profile.get("path_roots") or [])
        if not changed or (roots & changed_roots):
            profiles.append({
                "id": profile.get("id"),
                "path_roots": sorted(roots),
                "commands": [row.get("id") for row in profile.get("commands") or []
                             if isinstance(row, dict)],
            })

    owner_decisions: list[str] = [
        line for line in sections.get("unresolved decisions", [])
        if re.search(r"\b(must|should|needs)?\s*(owner|decision|authorize)\b", line)
    ][:8]
    acceptance = (sections.get("acceptance") or sections.get("acceptance criteria")
                  or sections.get("required behavior") or [])
    reviewer_checklist = [f"Acceptance: {line}" for line in acceptance[:24]]
    for pair in _parity_pairs(changed):
        reviewer_checklist.append(
            f"Parity: {pair['runtime']} is byte-identical to {pair['template']}")
    reviewer_checklist = reviewer_checklist[:32]

    contracts_dir = controller / CONTRACTS_ROOT / task_id
    contracts_dir.mkdir(parents=True, exist_ok=True)
    predecessors = sorted(contracts_dir.glob("v*.json"))
    predecessor: Optional[dict[str, str]] = None
    if predecessors:
        latest = predecessors[-1]
        predecessor = {"path": str(latest),
                       "sha256": hashlib.sha256(latest.read_bytes()).hexdigest()}

    contract = {
        "schema_version": PREIMPLEMENTATION_CONTRACT_SCHEMA,
        "task_id": task_id,
        "status": "blocked_handoff" if owner_decisions else "ready",
        "version": len(predecessors) + 1,
        "predecessor": predecessor,
        "binding": {
            "task_manifest_path": str(manifest_path),
            "task_manifest_sha256": requirements_sha,
            "task_last_modified": manifest.get("last_modified"),
            "base_sha": base_sha, "target_ref": target_ref,
            "source": source_identity,
        },
        "planner": {"mode": "deterministic-static", "runtime_sha256": runtime_sha,
                    "package_version": None, "model": None, "session_id": None},
        "requirements_sections": {name: lines for name, lines in sections.items()},
        "changed_paths": changed,
        "parity_pairs": _parity_pairs(changed),
        "likely_test_files": _likely_test_files(changed, worktree),
        "validation_profiles": profiles[:8],
        "owner_decisions": owner_decisions,
        "implementation_choices": [
            line for line in sections.get("implementation choices", [])][:16],
        "reviewer_checklist": reviewer_checklist,
        "negative_cases": [f"Refuse: {line}" for line in
                           (sections.get("exclusions") or sections.get("risks and constraints")
                            or [])][:16],
    }
    destination = contracts_dir / f"v{contract['version']}.json"
    if destination.exists():
        raise TaskWorkspaceError("contract version already exists")
    payload = (json.dumps(contract, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False) + "\n").encode()
    with tempfile.NamedTemporaryFile(dir=contracts_dir, prefix=".contract-",
                                     delete=False) as handle:
        handle.write(payload); handle.flush(); os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, destination)
    return {**contract, "contract_path": str(destination),
            "contract_sha256": hashlib.sha256(payload).hexdigest()}


def active_preimplementation_contract(controller: Path,
                                      task_id: str) -> Optional[dict[str, Any]]:
    """Latest non-superseded contract for a task, or None."""
    contracts_dir = controller / CONTRACTS_ROOT / task_id
    try:
        versions = sorted(contracts_dir.glob("v*.json"))
    except OSError:
        return None
    if not versions:
        return None
    try:
        contract = json.loads(versions[-1].read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(contract, dict) or contract.get("task_id") != task_id:
        return None
    return contract


def _standing_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload); handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary, path)


def _standing_root(controller: Path, task_id: str) -> Path:
    if not TASK_RE.fullmatch(task_id):
        raise TaskWorkspaceError("unsafe task id")
    return controller / STANDING_ROOT / task_id


def _git_blob(repository: Path, head: str, relative: str) -> Optional[str]:
    value = git(repository, "rev-parse", f"{head}:{relative}", check=False)
    return value if SHA_RE.fullmatch(value) else None


def _command_input_closure(repository: Path, head: str, row: dict[str, Any],
                           config: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
    policy_path = repository / ".juno_task/config/risk-policy.json"
    owned_roots: Optional[list[str]] = None
    for profile in config.get("validation_profiles") or []:
        if any(command.get("id") == row.get("id") for command in profile.get("commands") or []):
            owned_roots = profile["path_roots"]
            break
    return lifecycle_runtime.command_closure(
        repository, head, row, config_sha256=stable_sha256(config),
        policy_sha256=(hashlib.sha256(policy_path.read_bytes()).hexdigest()
                       if policy_path.is_file() else None),
        runtime_sha256=runtime["running_sha256"], owned_roots=owned_roots,
    )


def compile_standing_operation_snapshot(**inputs: Any) -> dict[str, Any]:
    """Task-lifecycle seam for the immutable operation/read-set compiler."""
    return operation_runtime.compile_identity_operation_snapshot(**inputs)


def _standing_snapshot_inputs(repository: Path, candidate: str, target: str,
                              planned: list[dict[str, Any]], config: dict[str, Any],
                              runtime: dict[str, Any], documentation: dict[str, Any]) -> dict[str, Any]:
    commands = [entry["command"] for entry in planned]
    routing = {row["id"]: "standing" for row in commands}
    validation_units = [
        {"phase": "validation", "id": entry["command"]["id"],
         "inputs": {"input_closure_sha256": entry["input_closure"]["input_closure_sha256"]}}
        for entry in planned
    ] or [{"phase": "validation", "id": "zero-command",
           "inputs": {"documentation_route": stable_sha256(documentation)}}]
    policy_path = repository / ".juno_task/config/risk-policy.json"
    policy_identity = (hashlib.sha256(policy_path.read_bytes()).hexdigest()
                       if policy_path.is_file() else stable_sha256(config.get("risk_policy", {})))
    phase_units = [*validation_units,
        {"phase": "risk", "id": "policy", "inputs": {"policy": policy_identity}},
        {"phase": "review", "id": "finding-policy",
         "inputs": {"policy": stable_sha256(config.get("review", config.get("risk", {})))}},
        {"phase": "documentation", "id": "active-contract",
         "inputs": {"route": stable_sha256(documentation),
                    "policy": stable_sha256(config.get("documentation_validation", {}))}},
        {"phase": "integration", "id": "runtime",
         "inputs": {"runtime": str(runtime["running_sha256"]),
                    "target_ref": stable_sha256(config.get("target_ref"))}},
    ]
    environment = (planned[0]["input_closure"].get("environment", {}) if planned else {})
    return {"candidate": candidate, "target": target, "commands": commands,
            "routing": routing, "environment": environment,
            "phase_units": phase_units,
            "managed_outputs": {"task_workspace_runtime": str(runtime["running_sha256"])},
            "discovery": {"complete": True, "kind": "exact-import-closure"}}


def standing_checkpoint(controller: Path, task_id: str,
                        lease_token: Optional[str] = None) -> dict[str, Any]:
    config = load_config(controller)
    require_task(controller, task_id)
    repository = product_repository(controller, config)
    runtime = require_current_runtime(repository, ref_sha(repository, config["target_ref"]), controller)
    with state_lock(controller):
        state = read_state(controller)
        record = state["tasks"].get(task_id)
        _require_lease_fence(controller, "checkpoint", task_id, lease_token, record=record)
        checkpoint_admission = decisions.plan_command_transition(
            decisions.CommandRequest("checkpoint", task_id),
            decisions.TaskSnapshot(
                task_id,
                None if not isinstance(record, dict) else record.get("state"),
                child_reservations(state).get(task_id)))
        if not checkpoint_admission.admitted:
            raise TaskWorkspaceError(checkpoint_admission.finding.message)
        frozen = json.loads(json.dumps(record))
    verify_hydration_evidence(frozen, Path(frozen["worktree"]))
    _repo, worktree, head, changed = observe_working_task(
        frozen, repository, config, task_id)
    if head == frozen["base_sha"] or not changed:
        raise TaskWorkspaceError("standing checkpoint requires a committed product diff")
    path_status = lifecycle_runtime.changed_path_status(
        repository, frozen["base_sha"], head)
    documentation = lifecycle_runtime.documentation_route(
        path_status, config["documentation_validation"])
    routing = validation_profile_selection(config, changed)
    if documentation["mode"] == "inert_zero_command":
        rows, selection_reason = [], "exact inert-documentation zero-command proof"
    elif documentation["mode"] == "active_audit":
        rows, selection_reason = [lifecycle_runtime.active_documentation_row()], "exact active-documentation audit"
    else:
        rows = selected_standing_rows(config, changed)
        selection_reason = ("single registered package profile" if routing["mode"] == "profile"
                            else "conservative focused fallback")
    planned = [{"command": row,
                "input_closure": _command_input_closure(repository, head, row, config, runtime),
                "reason": selection_reason}
               for row in rows]
    coherence = lifecycle_runtime.grouped_coherence(
        controller, repository, head, changed,
        active_doc_paths=documentation["active_paths"],
        documentation_policy=config["documentation_validation"])
    if coherence["outcome"] != "PASSED":
        raise TaskWorkspaceError(
            "grouped coherence failed: " + json.dumps(
                coherence["findings"], sort_keys=True))
    operation_snapshot = compile_standing_operation_snapshot(**_standing_snapshot_inputs(
        repository, head, ref_sha(repository, config["target_ref"]), planned,
        config, runtime, documentation))
    body = {"schema_version": STANDING_PLAN_SCHEMA, "task_id": task_id,
            "base_sha": frozen["base_sha"], "tip_sha": head,
            "tree_sha": git(repository, "rev-parse", f"{head}^{{tree}}"),
            "branch_ref": frozen["branch_ref"], "changed_paths": changed,
            "changed_path_status": path_status, "documentation_route": documentation,
            "grouped_coherence": coherence, "operation_snapshot": operation_snapshot,
            "selection": routing, "commands": planned,
            "created_at_unix_ns": time.time_ns()}
    identity_body = {key: value for key, value in body.items() if key != "created_at_unix_ns"}
    plan_sha = stable_sha256(identity_body)
    plan = {**body, "plan_sha256": plan_sha}
    root = _standing_root(controller, task_id)
    plan_path = root / plan_sha / "plan.json"
    if not plan_path.exists():
        _standing_atomic(plan_path, plan)
    latest_path = root / "latest.json"
    previous: Optional[dict[str, Any]] = None
    if latest_path.exists():
        try: previous = json.loads(latest_path.read_text())
        except (OSError, json.JSONDecodeError): previous = None
    if isinstance(previous, dict) and previous.get("plan_sha256") != plan_sha:
        previous_sha = previous.get("plan_sha256")
        if isinstance(previous_sha, str) and re.fullmatch(r"[0-9a-f]{64}", previous_sha):
            supersession = {"schema_version": STANDING_EVIDENCE_SCHEMA,
                            "outcome": "SUPERSEDED", "task_id": task_id,
                            "plan_sha256": previous_sha, "superseded_by": plan_sha,
                            "recorded_at_unix_ns": time.time_ns()}
            old = root / previous_sha / f"superseded-by-{plan_sha}.json"
            if not old.exists(): _standing_atomic(old, supersession)
    _standing_atomic(latest_path, {"schema_version": STANDING_PLAN_SCHEMA,
                                  "task_id": task_id, "plan_sha256": plan_sha,
                                  "tip_sha": head})
    return {**plan, "outcome": "CHECKPOINT_PLANNED", "plan_path": str(plan_path)}


def _standing_plan(controller: Path, task_id: str) -> tuple[dict[str, Any], Path]:
    root = _standing_root(controller, task_id)
    try:
        latest = json.loads((root / "latest.json").read_text())
        plan_sha = latest["plan_sha256"]
        plan_path = root / plan_sha / "plan.json"
        plan = json.loads(plan_path.read_text())
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        raise TaskWorkspaceError("standing evidence has no valid latest checkpoint") from exc
    identity = {key: value for key, value in plan.items()
                if key not in {"plan_sha256", "created_at_unix_ns"}}
    if (plan.get("schema_version") != STANDING_PLAN_SCHEMA
            or plan.get("task_id") != task_id
            or plan.get("plan_sha256") != stable_sha256(identity)):
        raise TaskWorkspaceError("standing checkpoint identity is malformed")
    return plan, plan_path


def _active_documentation_validation(repository: Path, head: str,
                                     plan: dict[str, Any], row: dict[str, Any],
                                     documentation_policy: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    started = time.monotonic()
    audit = lifecycle_runtime.active_documentation_audit(
        repository, head, plan["documentation_route"]["active_paths"],
        documentation_policy)
    output = lifecycle_runtime.canonical_bytes(audit)
    integrity = lifecycle_runtime.parsed_test_result_integrity(
        row["argv"], output, 0 if audit["outcome"] == "PASSED" else 1)
    exit_code = 0 if audit["outcome"] == "PASSED" and integrity["eligible_pass"] else 65
    elapsed = int((time.monotonic() - started) * 1000)
    return {"id": row["id"], "argv": row["argv"], "exit_code": exit_code,
            "process_exit_code": 0 if audit["outcome"] == "PASSED" else 1,
            "timed_out": False, "cancelled": False,
            "timeout_seconds": row["timeout_seconds"], "duration_ms": elapsed,
            "started_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
            "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
            "result_integrity": integrity, "active_documentation_audit": audit,
            "stdout_tail": output[-row["max_output_bytes"]:].decode(errors="replace"),
            "stderr_tail": "", "stdout_truncated_bytes": max(0, len(output)-row["max_output_bytes"]),
            "stderr_truncated_bytes": 0, "stdout_sha256": hashlib.sha256(output).hexdigest(),
            "stderr_sha256": hashlib.sha256(b"").hexdigest(), "log_path": None,
            "log_sha256": hashlib.sha256(output).hexdigest(), "log_write_failed": False,
            "log_write_error": None,
            "timing": {"schema_version": VALIDATION_TIMING_SCHEMA,
                       "states": [{"state": "WAITING_FOR_RESOURCE", "duration_ms": 0},
                                  {"state": "SETUP", "duration_ms": 0},
                                  {"state": "RUNNING", "duration_ms": elapsed},
                                  {"state": "TEARDOWN", "duration_ms": 0},
                                  {"state": "PASSED" if exit_code == 0 else "FAILED", "duration_ms": 0}],
                       "wall_duration_ms": elapsed, "critical_path_contribution_ms": elapsed},
            "resource": {"id": None, "lock_identity_sha256": None,
                         "wait_timeout_seconds": None, "owner_diagnostics": None},
            "identity": {"command_sha256": stable_sha256(row["argv"]),
                         "candidate_sha": head}}


def _standing_readiness_identity(record: dict[str, Any], worktree: Path,
                                  config: dict[str, Any]) -> str:
    return stable_sha256({"hydration": record.get("hydration"),
                          "dependencies": validation_dependency_evidence(worktree, config)})


def standing_evidence_run(controller: Path, task_id: str,
                          *, raise_on_failure: bool = True,
                          lease_token: Optional[str] = None) -> dict[str, Any]:
    plan, plan_path = _standing_plan(controller, task_id)
    config = load_config(controller)
    repository = product_repository(controller, config)
    with state_lock(controller):
        record = read_state(controller)["tasks"].get(task_id)
    _require_lease_fence(controller, "evidence-run", task_id, lease_token, record=record)
    evidence_gate = decisions.plan_command_transition(
        decisions.CommandRequest("evidence-run", task_id),
        decisions.TaskSnapshot(
            task_id, None if not isinstance(record, dict) else record.get("state")))
    if not evidence_gate.admitted:
        raise TaskWorkspaceError(evidence_gate.finding.message)
    _repo, worktree, head, changed = observe_working_task(record, repository, config, task_id)
    if head != plan["tip_sha"] or changed != plan["changed_paths"]:
        raise TaskWorkspaceError("standing checkpoint is stale; create a new task checkpoint")
    runtime = require_current_runtime(repository, ref_sha(repository, config["target_ref"]), controller)
    current_planned = [{**entry, "input_closure": _command_input_closure(
        repository, head, entry["command"], config, runtime)} for entry in plan["commands"]]
    current_snapshot = compile_standing_operation_snapshot(**_standing_snapshot_inputs(
        repository, head, ref_sha(repository, config["target_ref"]), current_planned,
        config, runtime, plan["documentation_route"]))
    invalidation = operation_runtime.phase_invalidation(
        plan.get("operation_snapshot"), current_snapshot)
    affected = [row for row in invalidation
                if row.get("phase") in {"validation", "documentation"}]
    if affected:
        raise TaskWorkspaceError("standing operation snapshot changed; checkpoint a successor: "
                                 + json.dumps(affected, sort_keys=True))
    lane = _standing_root(controller, task_id) / ".local-lane.lock"
    lane.parent.mkdir(parents=True, exist_ok=True)
    decision_log: list[dict[str, Any]] = []
    executed = reused = invalidated = 0
    active_wall_ms = 0
    failure: Optional[tuple[dict[str, Any], dict[str, Any]]] = None
    readiness_sha256 = _standing_readiness_identity(record, worktree, config)
    with lane.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        # Adapter half: gather immutable receipt facts, then plan purely.
        facts: list[Optional[decisions.ReceiptFact]] = []
        loaded: list[Optional[dict[str, Any]]] = []
        base_paths: list[Path] = []
        for index, planned in enumerate(plan["commands"]):
            row, closure = planned["command"], planned["input_closure"]
            key = closure["input_closure_sha256"]
            base_receipt_path = (_standing_root(controller, task_id) / plan["plan_sha256"]
                                 / f"command-{index}-{key}.json")
            receipt: Optional[dict[str, Any]] = None
            fact: Optional[decisions.ReceiptFact] = None
            if base_receipt_path.exists():
                try: receipt = json.loads(base_receipt_path.read_text())
                except (OSError, json.JSONDecodeError): receipt = None
                verification = lifecycle_runtime.verify_complete_input_closure(
                    receipt.get("input_closure") if isinstance(receipt, dict) else None,
                    closure,
                    receipt.get("complete_input_identity") if isinstance(receipt, dict) else None)
                if (not isinstance(receipt, dict) or receipt.get("schema_version") not in {
                            STANDING_EVIDENCE_SCHEMA, CANONICAL_VALIDATION_RECEIPT_SCHEMA}
                        or not verification["valid"] or receipt.get("command") != row
                        or not isinstance(receipt.get("result"), dict)):
                    raise TaskWorkspaceError("standing command receipt is malformed: "
                                             + json.dumps(verification["reasons"], sort_keys=True))
                failed_prior = bool(receipt["result"].get("timed_out")
                                    or receipt["result"].get("exit_code"))
                prior_readiness = receipt.get("readiness_sha256")
                supersession_path = base_receipt_path.with_name(
                    base_receipt_path.stem + f".readiness-{readiness_sha256}.json")
                fact = decisions.ReceiptFact(
                    present=True, valid=True, failed_prior=failed_prior,
                    readiness_sha256=prior_readiness,
                    supersession_exists=(failed_prior
                                         and prior_readiness != readiness_sha256
                                         and supersession_path.exists()))
            facts.append(fact)
            loaded.append(receipt)
            base_paths.append(base_receipt_path)
        reuse_plan = decisions.plan_evidence_reuse(
            plan["commands"], facts, readiness_sha256,
            plan.get("documentation_route"))
        receipts: list[dict[str, Any]] = []
        for index, entry in enumerate(reuse_plan.entries):
            planned = plan["commands"][index]
            row, closure = planned["command"], planned["input_closure"]
            base_receipt_path = base_paths[index]
            receipt_path = base_receipt_path
            receipt = loaded[index]
            if entry.finding is not None:
                raise TaskWorkspaceError(entry.finding.message)
            execute = False
            if entry.action == decisions.ACTION_FAILURE_STANDS:
                failure = (row, receipt["result"])
            elif entry.action == decisions.ACTION_INVALIDATE:
                receipt_path = base_receipt_path.with_name(
                    base_receipt_path.stem + (entry.supersession_suffix or ""))
                receipt = None; invalidated += 1; execute = True
                decision_log.append(lifecycle_runtime.evidence_decision(
                    row["id"], "invalidated", closure=closure,
                    invalidation=entry.invalidation,
                    reason="failed evidence remains immutable; readiness changed"))
            elif entry.action == decisions.ACTION_REUSE:
                reused += 1
                decision_log.append(lifecycle_runtime.evidence_decision(
                    row["id"], "reused", closure=closure,
                    source={"path": str(receipt_path),
                            "sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest()}))
            else:
                execute = True
            if execute:
                cwd = (worktree / row["cwd"]).resolve()
                try: cwd.relative_to(worktree)
                except ValueError as exc:
                    raise TaskWorkspaceError("standing validation cwd escaped task worktree") from exc
                evidence = (_active_documentation_validation(
                                repository, head, plan, row,
                                config["documentation_validation"])
                            if row["argv"] == lifecycle_runtime.ACTIVE_DOC_ARGV
                            else run_validation(row, cwd))
                policy_identity = {
                    "routing_config_sha256": closure.get("routing_config_sha256"),
                    "risk_policy_sha256": closure.get("risk_policy_sha256"),
                    "runtime_sha256": closure.get("runtime_sha256")}
                snapshot_sha = plan["operation_snapshot"]["snapshot_sha256"]
                outcome_identity = {
                    "schema_version": closure.get("outcome_schema"),
                    "result_sha256": stable_sha256(evidence),
                    "verdict": ("PASSED" if not evidence["timed_out"]
                                and evidence["exit_code"] == 0 else "FAILED")}
                index_identity = {
                    "command_closure_sha256": closure["input_closure_sha256"],
                    "policy_identity": policy_identity,
                    "outcome_identity": outcome_identity,
                    "repository_identity": lifecycle_runtime.repository_identity(repository),
                    "snapshot_lineage": {"producer_snapshot_sha256": snapshot_sha,
                                         "consuming_snapshot_sha256": snapshot_sha}}
                receipt = {
                    "schema_version": CANONICAL_VALIDATION_RECEIPT_SCHEMA,
                    "receipt_kind": "executed", "phase": "task_closure",
                    "task_id": task_id, "plan_sha256": plan["plan_sha256"],
                    "tip_sha": head, "command_index": index, "command_id": row["id"],
                    "command": row, "input_closure": closure,
                    "complete_input_identity": lifecycle_runtime.complete_input_identity(closure),
                    "policy_identity": policy_identity, "outcome_identity": outcome_identity,
                    "index_identity": index_identity,
                    "index_sha256": stable_sha256(index_identity),
                    "consuming_candidate": {"candidate_sha": head,
                                             "candidate_tree": plan["tree_sha"]},
                    "snapshot_lineage": index_identity["snapshot_lineage"],
                    "source": None,
                    "decision_reason": "supported registered validation command execution",
                    "readiness_sha256": readiness_sha256,
                    "result": evidence, "recorded_at_unix_ns": time.time_ns()}
                _standing_atomic(receipt_path, receipt); executed += 1
                active_wall_ms += max(0, int(
                    evidence.get("timing", {}).get("wall_duration_ms",
                                                     evidence.get("duration_ms", 0))))
                decision_log.append(lifecycle_runtime.evidence_decision(
                    row["id"], "executed", closure=closure,
                    source={"path": str(receipt_path)}))
                if receipt["result"]["timed_out"] or receipt["result"]["exit_code"]:
                    failure = (row, receipt["result"])
            receipts.append({"path": str(receipt_path),
                             "sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
                             "command_id": row["id"]})
            if failure is not None:
                break
        if reuse_plan.terminal is not None:
            terminal = reuse_plan.terminal
            decision_log.append(lifecycle_runtime.evidence_decision(
                terminal["command_id"], terminal["decision"],
                closure=terminal["closure"], reason=terminal["reason"]))
        summary = {"schema_version": STANDING_EVIDENCE_SCHEMA, "task_id": task_id,
                   "plan_sha256": plan["plan_sha256"], "tip_sha": head,
                   "outcome": "FAILED" if failure else "PASSED",
                   "executed": executed, "reused": reused, "invalidated": invalidated,
                   "active_wall_ms": active_wall_ms,
                   "decisions": decision_log,
                   "counters": lifecycle_runtime.evidence_counters(decision_log),
                   "replay_trace": lifecycle_runtime.evidence_replay_trace(
                       decision_log, phase="task_evidence"),
                   "documentation_route": plan["documentation_route"],
                   "grouped_coherence": plan["grouped_coherence"],
                   "readiness_sha256": readiness_sha256, "receipts": receipts,
                   "completed_at_unix_ns": time.time_ns()}
        _standing_atomic(plan_path.parent / "summary.json", summary)
    if failure and raise_on_failure:
        row, result = failure
        raise TaskWorkspaceError(decisions.validation_failure_message(row, result))
    return summary


def standing_evidence_status(controller: Path, task_id: str) -> dict[str, Any]:
    plan, plan_path = _standing_plan(controller, task_id)
    summary_path = plan_path.parent / "summary.json"
    summary = None
    if summary_path.exists():
        try: summary = json.loads(summary_path.read_text())
        except (OSError, json.JSONDecodeError): summary = None
    return {"schema_version": STANDING_EVIDENCE_SCHEMA, "task_id": task_id,
            "plan_sha256": plan["plan_sha256"], "tip_sha": plan["tip_sha"],
            "state": "COMPLETE" if isinstance(summary, dict) and summary.get("outcome") == "PASSED" else "PENDING",
            "summary": summary}


def preflight(controller: Path, task_id: str) -> dict[str, Any]:
    """Run finish identity/admission checks without validation or queue mutation."""
    if not TASK_RE.fullmatch(task_id):
        raise TaskWorkspaceError("unsafe task id")
    config = load_config(controller)
    require_task(controller, task_id)
    configured_repository = product_repository(controller, config)
    runtime = require_current_runtime(configured_repository,
                                      ref_sha(configured_repository, config["target_ref"]),
                                      controller)
    with state_lock(controller):
        state = read_state(controller)
        record = state["tasks"].get(task_id)
        preflight_admission = decisions.plan_command_transition(
            decisions.CommandRequest("preflight", task_id),
            decisions.TaskSnapshot(
                task_id,
                None if not isinstance(record, dict) else record.get("state"),
                child_reservations(state).get(task_id)))
        if not preflight_admission.admitted:
            raise TaskWorkspaceError(preflight_admission.finding.message)
        frozen_record = json.loads(json.dumps(record))
    verify_hydration_evidence(frozen_record, Path(frozen_record["worktree"]))
    _, worktree, head, changed, closure = review_ready_closure(
        controller, config, frozen_record, configured_repository, task_id, runtime
    )
    if load_config(controller) != config:
        raise TaskWorkspaceError("task workspace policy changed during preflight")
    return {"schema_version": RECORD_SCHEMA, "task_id": task_id, "state": "WORKING",
            "outcome": "preflight_passed", "worktree": str(worktree), "tip_sha": head,
            "changed_paths": changed, "review_ready_closure": closure}


def _finish_once(controller: Path, task_id: str,
                 lease_token: Optional[str] = None) -> dict[str, Any]:
    config = load_config(controller)
    require_task(controller, task_id)
    configured_repository = product_repository(controller, config)
    runtime = require_current_runtime(configured_repository,
                                      ref_sha(configured_repository, config["target_ref"]),
                                      controller)
    queued_record: Optional[dict[str, Any]] = None
    with state_lock(controller):
        state = read_state(controller)
        record = state["tasks"].get(task_id)
        _require_lease_fence(controller, "finish", task_id, lease_token, record=record)
        finish_admission = decisions.plan_command_transition(
            decisions.CommandRequest("finish", task_id),
            decisions.TaskSnapshot(
                task_id,
                None if not isinstance(record, dict) else record.get("state"),
                child_reservations(state).get(task_id)))
        if not finish_admission.admitted:
            raise TaskWorkspaceError(finish_admission.finding.message)
        if finish_admission.idempotent:
            queued_record = record
        else:
            frozen_record = json.loads(json.dumps(record))
    if queued_record is not None:
        # Idempotent retry: verify or repair the queue projection so a crash
        # between queue mutation and board projection cannot leave drift.
        # The queued branch/worktree must still sit at the recorded tip; a
        # moved tip means the queue closure no longer describes the current
        # candidate and must not be reported as successful validation.
        released_record = _ensure_lease_released(
            controller, task_id, queued_record, reason="queued")
        if released_record is not queued_record:
            # Commit only the terminal lease release; any other concurrent
            # drift stays a hard error on the comparisons below.
            with state_lock(controller):
                state = read_state(controller)
                if state["tasks"].get(task_id) == queued_record:
                    state["tasks"][task_id] = released_record
                    write_state(controller, state)
                    queued_record = released_record
        queued_worktree = exact_root(Path(queued_record["worktree"]), "recorded task worktree")
        queued_head = git(queued_worktree, "rev-parse", "HEAD")
        queued_branch = git(queued_worktree, "symbolic-ref", "-q", "HEAD", check=False)
        queued_ref_sha = optional_ref_sha(configured_repository, queued_record["branch_ref"])
        if (queued_head != queued_record.get("tip_sha")
                or queued_branch != queued_record.get("branch_ref")
                or queued_ref_sha != queued_record.get("tip_sha")):
            raise TaskWorkspaceError(
                f"task is queued at {queued_record.get('tip_sha')} but its branch/worktree tip is "
                f"{queued_head}; use `yy merge reopen {task_id}` for a descendant correction or "
                "restore the exact queued tip before retrying finish")
        try:
            queue_sync = ensure_kanban_sync(controller, task_id, queued_record, phase="queued")
        except KanbanSyncError as exc:
            _stamp_kanban_sync(controller, task_id, queued_record,
                               _demote_to_kanban_sync_required(queued_record, exc)["kanban_sync"],
                               restore_state=KANBAN_SYNC_STATE)
            raise TaskWorkspaceError(
                f"task is queued but its Kanban projection failed: {exc}; "
                f"recover with: {KANBAN_SYNC_RECOVERY.format(task=task_id)}") from exc
        return {**_stamp_kanban_sync(controller, task_id, queued_record, queue_sync),
                "outcome": "already_queued"}
    verify_hydration_evidence(frozen_record, Path(frozen_record["worktree"]))

    # Validations run outside the controller state lock. Independent feature
    # finishes therefore stay concurrent; the compare below prevents stale state.
    repository, worktree, head, changed, closure = review_ready_closure(
        controller, config, frozen_record, configured_repository, task_id, runtime
    )
    _frozen_allowed, frozen_generated_admission, _admission_source = effective_admission(
        frozen_record)
    frozen_umbrella = (
        frozen_record.get("admission_supersessions", [{}])[-1].get("umbrella_admission")
        if frozen_record.get("admission_supersessions")
        else frozen_record.get("creation_receipt", {}).get("umbrella_admission")
    )
    routing = validation_profile_selection(config, changed)
    checkpoint_plan = standing_checkpoint(controller, task_id, lease_token)
    selected_focused = [planned["command"] for planned in checkpoint_plan["commands"]]
    standing = standing_evidence_run(controller, task_id, raise_on_failure=False,
                                     lease_token=lease_token)
    if checkpoint_plan["plan_sha256"] != standing["plan_sha256"]:
        raise TaskWorkspaceError("standing evidence plan changed during finish")
    validations = [json.loads(Path(reference["path"]).read_text())["result"]
                   for reference in standing["receipts"]]
    closure_body = {key: value for key, value in closure.items() if key != "closure_sha256"}
    closure_body["standing_validation"] = {
        "schema_version": STANDING_EVIDENCE_SCHEMA,
        "plan_sha256": standing["plan_sha256"], "tip_sha": standing["tip_sha"],
        "outcome": standing["outcome"], "receipts": standing["receipts"],
        "decisions": standing["decisions"], "counters": standing["counters"],
        "active_wall_ms": standing["active_wall_ms"],
        "documentation_route": standing["documentation_route"],
        "grouped_coherence": standing["grouped_coherence"],
        "operation_snapshot": checkpoint_plan["operation_snapshot"],
        "summary_sha256": stable_sha256(standing),
    }
    closure = {**closure_body, "closure_sha256": stable_sha256(closure_body)}
    for row, evidence in zip(selected_focused, validations):
        if evidence["timed_out"] or evidence["exit_code"]:
            # Persist every terminal result from this one deterministic schedule.
            # A failed row never causes automatic multiplication of an unchanged run.
            _persist_failed_validation(controller, task_id, frozen_record, validations)
            if evidence["timed_out"]:
                resource = evidence.get("resource", {})
                wait_ms = evidence["timing"]["states"][0]["duration_ms"]
                wait_budget_ms = (resource.get("wait_timeout_seconds") or 0) * 1000
                if resource.get("id") and wait_ms >= max(0, wait_budget_ms - 100):
                    raise TaskWorkspaceError(
                        f"focused validation resource wait timed out ({row['id']}, {resource['id']}): "
                        f"owner={resource.get('owner_diagnostics')}; unchanged retries are not automatic")
                raise TaskWorkspaceError(f"focused validation timed out ({row['id']}) after {row['timeout_seconds']}s")
            detail = evidence["stderr_tail"] or evidence["stdout_tail"]
            raise TaskWorkspaceError(f"focused validation failed ({row['id']}, exit {evidence['exit_code']}): {detail}")
    if load_config(controller) != config:
        raise TaskWorkspaceError("task workspace policy changed during focused validation")
    try:
        post_repository, post_worktree, post_head, post_changed = observe_working_task(
            record, configured_repository, config, task_id
        )
    except TaskWorkspaceError as exc:
        raise TaskWorkspaceError("task tip or worktree changed during focused validation") from exc
    if ((post_repository, post_worktree, post_head, post_changed)
            != (repository, worktree, head, changed)):
        raise TaskWorkspaceError("task tip or worktree changed during focused validation")
    queued = {**record, "state": "QUEUED", "tip_sha": head, "changed_paths": changed,
              "review_ready_closure": closure,
              "validation_routing": routing,
              "review_round": 1,
              "validation": validations, "last_validation_outcome": "PASSED"}
    # Queueing terminates worker authority: one terminal release receipt
    # precedes the queued record mutation so the boundary is crash-idempotent.
    queued = _ensure_lease_released(controller, task_id, queued, reason="queued")
    with state_lock(controller):
        state = read_state(controller)
        current = state["tasks"].get(task_id)
        if current != frozen_record:
            if isinstance(current, dict) and current.get("state") == "QUEUED" and current.get("tip_sha") == head:
                return {**current, "outcome": "already_queued"}
            raise TaskWorkspaceError("task state changed during focused validation; inspect status and retry")
        # Final locked checkpoint: no queue mutation follows stale child,
        # declaration, generated-binding, branch, tip, or cleanliness evidence.
        if (git(worktree, "rev-parse", "HEAD") != head
                or optional_ref_sha(repository, current["branch_ref"]) != head
                or git(worktree, "symbolic-ref", "-q", "HEAD", check=False) != current["branch_ref"]
                or git(worktree, "status", "--porcelain=v1", "--untracked-files=all")):
            raise TaskWorkspaceError("task branch/tip/worktree changed before queue mutation")
        if frozen_umbrella is not None:
            final_drift = umbrella_drift(controller, repository, frozen_umbrella,
                                         frozen_generated_admission, state, task_id)
            if final_drift:
                raise TaskWorkspaceError(
                    f"frozen umbrella admission drifted before queue mutation: {json.dumps(final_drift, sort_keys=True)}"
                )
        queued["enqueue_sequence"] = assign_enqueue_sequence(state)
        state["tasks"][task_id] = queued
        write_state(controller, state)
    try:
        queue_sync = ensure_kanban_sync(controller, task_id, queued, phase="queued")
    except KanbanSyncError as exc:
        _stamp_kanban_sync(controller, task_id, queued,
                           _demote_to_kanban_sync_required(queued, exc)["kanban_sync"],
                           restore_state=KANBAN_SYNC_STATE)
        raise TaskWorkspaceError(
            f"task queued but its Kanban projection failed: {exc}; "
            f"recover with: {KANBAN_SYNC_RECOVERY.format(task=task_id)}") from exc
    queued = _stamp_kanban_sync(controller, task_id, queued, queue_sync)
    return {**queued, "outcome": "queued"}


def finish(controller: Path, task_id: str, lease_token: Optional[str] = None) -> dict[str, Any]:
    # Same-task finish calls serialize across validation; different task IDs use
    # different leases and continue in parallel.
    if not TASK_RE.fullmatch(task_id):
        raise TaskWorkspaceError("unsafe task id")
    with finish_lock(controller, task_id):
        return _finish_once(controller, task_id, lease_token)


HANDOFF_SCHEMA = "juno_run_handoff.v1"
HANDOFF_ROOT = ".juno_task/runtime/handoff"
HANDOFF_MAX_BYTES = 8192
HANDOFF_NEXT_COMMANDS = {
    "NOT_STARTED": "yy task start {task}",
    "WORKING": "yy task preflight {task}",
    KANBAN_SYNC_STATE: KANBAN_SYNC_RECOVERY,
    "QUEUED": "yy merge next",
    "AWAITING_RISK": "yy merge review {task}",
    "AWAITING_RELEASE": "yy release train status <declaration>",
    "REVIEWING": "yy merge review {task}",
    "REVIEW_FINDINGS": "repair findings in the task worktree, then yy merge reopen {task}",
    "REVIEW_FINDINGS_EXHAUSTED": "yy merge reconcile plan",
    "CONFLICT": "yy merge resolve {task}",
    "CONFLICT_RESOLVED": "yy merge next",
    "REOPENING": "yy merge status",
    "REQUEUING_STALE": "yy merge refresh plan {task}",
    "RISK_EVIDENCE_READY": "yy merge next {task}",
    "MERGING": "yy merge next",
    "MERGED": "none: task integrated; archive the Kanban task",
    "WITHDRAWN": "none: candidate withdrawn; create or bind a continuation task",
}


_handoff_phase = decisions.handoff_phase


def run_handoff(controller: Path, task_id: str) -> dict[str, Any]:
    """Deterministic bounded evidence-backed handoff for the next agent.

    Everything is derived from durable Juno evidence (task record, queue
    attempt, receipts, runtime generation) - never model memory. Missing
    values are explicit; conflicting evidence fails closed with the
    reconciliation command instead of a misleading next step.
    """
    if not TASK_RE.fullmatch(task_id):
        raise TaskWorkspaceError("unsafe task id")
    config = load_config(controller)
    require_task(controller, task_id)
    repository = product_repository(controller, config)
    current_target = optional_ref_sha(repository, config["target_ref"])
    generation = runtime_generation(repository, current_target) if current_target else None
    state = read_state(controller)
    record = state.get("tasks", {}).get(task_id) or {}
    task_state = record.get("state") or "NOT_STARTED"
    attempt = record.get("queue_attempt") if isinstance(record.get("queue_attempt"), dict) else {}

    validation_rows = [
        {"id": row.get("id"), "exit_code": row.get("exit_code"),
         "timed_out": bool(row.get("timed_out"))}
        for row in (record.get("validation") or attempt.get("validation") or [])
        if isinstance(row, dict)][:12]
    risk = attempt.get("risk") if isinstance(attempt.get("risk"), dict) else {}
    progress = risk.get("review_progress") if isinstance(risk, dict) else {}
    admission = (progress or {}).get("full_suite_admission") \
        if isinstance(progress, dict) else None
    receipts = [row.get("receipt_path") for row in
                ((admission or {}).get("receipts") or [])
                if isinstance(row, dict) and isinstance(row.get("receipt_path"), str)][:16]

    conflicts: list[str] = []
    candidate_sha = attempt.get("candidate_sha") or record.get("candidate_sha")
    if task_state in {"AWAITING_RISK", "REVIEWING", "RISK_EVIDENCE_READY"}:
        if not candidate_sha:
            conflicts.append("queue state requires a frozen candidate identity")
        elif isinstance(admission, dict) and not receipts:
            conflicts.append("full-suite admission has no bound receipts")
    if task_state == "MERGED" and not (attempt.get("outcome") == "MERGED"
                                       or record.get("outcome") == "MERGED"):
        conflicts.append("merged state lacks its merged attempt outcome")

    next_command = ("yy integration runtime-doctor" if conflicts else
                    HANDOFF_NEXT_COMMANDS.get(task_state, "yy merge status").format(task=task_id))
    evidence_reuse = [row.get("command_id") for row in (attempt.get("evidence_reuse") or [])
                      if isinstance(row, dict)][:12]
    handoff = {
        "schema_version": HANDOFF_SCHEMA, "task_id": task_id,
        "phase": _handoff_phase(task_state), "state": task_state,
        "conflicts": conflicts, "next_command": next_command,
        "identity": {
            "controller_branch": git(controller, "rev-parse", "--abbrev-ref", "HEAD",
                                      check=False) or None,
            "controller_head": git(controller, "rev-parse", "HEAD", check=False) or None,
            "product_repository": str(repository),
            "target_ref": config["target_ref"],
            "target_sha": current_target,
            "runtime_generation_current": bool(generation and generation.get("current")),
            "runtime_running_sha256": (generation or {}).get("running_sha256"),
            "node_version": os.environ.get("JUNO_NODE_VERSION") or None,
        },
        "task": {
            "base_sha": record.get("base_sha") or attempt.get("base_sha"),
            "candidate_sha": candidate_sha,
            "worktree": record.get("worktree"),
            "tip_sha": record.get("tip_sha"),
            "changed_path_count": len(record.get("changed_paths") or []),
            "validation": validation_rows,
            "review_status": (risk or {}).get("status"),
            "review_round": record.get("review_round"),
            "evidence_reuse_commands": evidence_reuse,
            "blockers": sorted(record.get("blocked_by") or [])[:8],
        },
        "references": {
            "full_suite_receipts": receipts,
            "queue_evidence": (risk or {}).get("evidence"),
            "task_manifest": str(task_file(controller, task_id)),
        },
    }
    text = _handoff_text(handoff)
    if len(text.encode()) > HANDOFF_MAX_BYTES:
        raise TaskWorkspaceError("handoff exceeded its documented byte budget")
    directory = controller / HANDOFF_ROOT
    directory.mkdir(parents=True, exist_ok=True)
    for name, payload in ((f"{task_id}.json", (json.dumps(
                                handoff, sort_keys=True, indent=1) + "\n").encode()),
                          (f"{task_id}.md", text.encode())):
        with tempfile.NamedTemporaryFile(dir=directory, prefix="." + name, delete=False) as handle:
            handle.write(payload); handle.flush(); os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, directory / name)
    return {**handoff, "handoff_text": text,
            "handoff_paths": [str(directory / f"{task_id}.json"),
                              str(directory / f"{task_id}.md")]}


def _handoff_text(handoff: dict[str, Any]) -> str:
    phases = ["planned", "working", "queued", "validating", "reviewing",
              "approved", "merged"]
    phase = handoff["phase"]
    on_rail = phase in phases
    completed = phases.index(phase) if on_rail else -1
    markers = " ".join(
        ("[x]" if (completed > index or phase == "merged") else
         "[>]" if phase == name else "[ ]")
        for index, name in enumerate(phases))
    flow = " -> ".join(phases) + "\n" + markers
    if not on_rail:
        flow += f"\n! off-rail phase: {phase}"
    identity, task, references = handoff["identity"], handoff["task"], handoff["references"]
    validation = ", ".join(f"{row['id']}={row['exit_code']}"
                           for row in task["validation"][:6]) or "none recorded"
    lines = [
        f"# Run handoff: {handoff['task_id']}",
        "",
        "```",
        flow,
        "```",
        f"- state: {handoff['state']} (phase {phase})",
        f"- conflicts: {', '.join(handoff['conflicts']) or 'none'}",
        f"- next command: {handoff['next_command']}",
        f"- target: {identity['target_ref']} @ {identity['target_sha']}",
        f"- runtime: current={identity['runtime_generation_current']} "
        f"sha={str(identity['runtime_running_sha256'])[:12]}",
        f"- base {str(task['base_sha'])[:12]} candidate {str(task['candidate_sha'])[:12]} "
        f"tip {str(task['tip_sha'])[:12]}",
        f"- validation: {validation}",
        f"- review: {task['review_status']} round {task['review_round']}",
        f"- evidence reuse: {', '.join(task['evidence_reuse_commands']) or 'none'}",
        f"- blockers: {', '.join(task['blockers']) or 'none'}",
        f"- receipts: {'; '.join(str(p) for p in references['full_suite_receipts'][:4]) or 'none'}",
        "- cost/duration: not available through canonical provenance (explicitly absent)",
        "",
        f"Full JSON: {handoff['handoff_paths'][0] if 'handoff_paths' in handoff else 'controller runtime'}",
    ]
    return "\n".join(lines)


def status(controller: Path, task_id: str) -> dict[str, Any]:
    config = load_config(controller)
    require_task(controller, task_id)
    configured_repository = product_repository(controller, config)
    current_target = optional_ref_sha(configured_repository, config["target_ref"])
    generation = runtime_generation(configured_repository, current_target) if current_target else None
    state = read_state(controller)
    record = state["tasks"].get(task_id)
    if not record:
        projection = decisions.status_projection(
            decisions.TaskSnapshot(task_id, None, child_reservations(state).get(task_id)))
        projected: dict[str, Any] = {
            "schema_version": RECORD_SCHEMA, "task_id": task_id,
            "state": projection.state, "outcome": "status",
            "runtime_generation": generation}
        if projection.umbrella_owner_task_id is not None:
            projected["umbrella_owner_task_id"] = projection.umbrella_owner_task_id
            projected["next_action"] = projection.next_action
        return projected
    result = {**record, "outcome": "status", "runtime_generation": generation}
    if isinstance(record.get("kanban_sync"), dict):
        kanban_sync = record["kanban_sync"]
        result["kanban_sync"] = kanban_sync
        if kanban_sync.get("status") == "required":
            result["kanban_sync_required"] = True
            result["recovery_command"] = KANBAN_SYNC_RECOVERY.format(task=task_id)
    if record.get("state") == "WORKING":
        _, _, live_tip, committed_paths, uncommitted_paths = observe_task_diff(
            record, configured_repository, config, task_id
        )
        result.update({"tip_sha": live_tip, "changed_paths": committed_paths,
                       "uncommitted_paths": uncommitted_paths,
                       "changed_paths_scope": "base_sha..tip committed diff"})
    frozen_umbrella = (record.get("admission_supersessions", [{}])[-1].get("umbrella_admission")
                       if record.get("admission_supersessions")
                       else record.get("creation_receipt", {}).get("umbrella_admission"))
    if frozen_umbrella is not None:
        _paths, frozen_generated, source = effective_admission(record)
        ordered_children = [child for child in frozen_umbrella.get("ordered_child_ids", [])
                            if isinstance(child, str) and TASK_RE.fullmatch(child)]
        projection = (umbrella_progress_projection(record, ordered_children)
                      if ordered_children else None)
        admission_status = {
            "authority": ("authorized_superseding" if source == "superseding"
                          else "historical_creation"),
            "ordered_child_ids": frozen_umbrella.get("ordered_child_ids"),
            "child_bindings": frozen_umbrella.get("child_bindings"),
            "union_paths_sha256": frozen_umbrella.get("union_paths_sha256"),
            "child_revision_drift": umbrella_drift(
                controller, configured_repository, frozen_umbrella,
                frozen_generated, state, task_id),
        }
        if projection is not None:
            admission_status.update({
                "completed_child_ids": projection["completed_child_ids"],
                "current_child_id": projection["current_child_id"],
                "remaining_child_ids": projection["remaining_child_ids"],
                "child_progress": [{"child_id": entry["child_id"],
                                    "base_sha": entry["base_sha"],
                                    "tip_sha": entry["tip_sha"],
                                    "changed_paths": entry["changed_paths"],
                                    "recorded_at_unix_ns": entry["recorded_at_unix_ns"]}
                                   for entry in projection["entries"]],
            })
        result["umbrella_admission_status"] = admission_status
    repository = Path(record.get("repository", ""))
    if repository.is_dir():
        current = optional_ref_sha(repository, record.get("target_ref", ""))
        result["current_target_sha"] = current or None
        result["target_available"] = bool(current)
        result["target_moved"] = (current != record.get("base_sha")) if current else None
        if not current:
            result["target_error"] = "target_ref_unavailable"
    else:
        result.update({"current_target_sha": None, "target_available": False,
                       "target_moved": None, "target_error": "repository_unavailable"})
    return result


def _load_boundary_runtime(filename: str, module_name: str) -> Any:
    sibling = Path(__file__).resolve().with_name(filename)
    if not sibling.is_file():
        raise TaskWorkspaceError(f"packaged boundary validator is missing: {filename}")
    spec = importlib.util.spec_from_file_location(module_name, sibling)
    if spec is None or spec.loader is None:
        raise TaskWorkspaceError(f"cannot load boundary validator: {filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def require_metadata_only_controller(controller: Path,
                                     task_config: dict[str, Any]) -> dict[str, Any]:
    metadata_path = controller / ".juno_task/config/metadata-controller.json"
    try:
        boundary = _load_boundary_runtime(
            "metadata_controller.py", "juno_task_runtime_metadata_boundary")
        policy = boundary.load_policy(metadata_path)
    except Exception as exc:
        raise TaskWorkspaceError(f"runtime bootstrap requires a valid metadata-controller policy: {exc}") from exc
    resolver_path = Path(__file__).resolve().with_name("controller_resolver.py")
    if not resolver_path.is_file():
        raise TaskWorkspaceError("packaged controller registration validator is missing")
    resolver_env = {key: value for key, value in os.environ.items()
                    if key not in {"JUNO_TASK_ROOT", "JUNO_CONTROLLER_BRANCH",
                                  "JUNO_WORKSPACE_ROLE"}}
    resolved = subprocess.run(
        [sys.executable, str(resolver_path), "--cwd", str(controller),
         "--operation", "orchestration", "--format", "json"],
        cwd=controller, env=resolver_env, text=True, capture_output=True,
        stdin=subprocess.DEVNULL)
    if resolved.returncode:
        raise TaskWorkspaceError(resolved.stderr.strip() or "controller registration refused")
    try:
        route = json.loads(resolved.stdout)
    except json.JSONDecodeError as exc:
        raise TaskWorkspaceError("controller registration validator returned invalid evidence") from exc
    branch = git(controller, "symbolic-ref", "-q", "HEAD", check=False)
    role = git(controller, "config", "--worktree", "--get", "juno.workspace.role", check=False)
    registered_path = git(controller, "config", "--local", "--get", "juno.controller.path", check=False)
    registered_branch = git(controller, "config", "--local", "--get", "juno.controller.branch", check=False)
    try:
        config_json = json.loads((controller / ".juno_task/config.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise TaskWorkspaceError(f"runtime bootstrap controller config is invalid: {exc}") from exc
    expected_shape = {"mode": "metadata-only",
                      "policy": ".juno_task/config/metadata-controller.json"}
    if (branch != policy["controller_branch"] or role != "controller"
            or not registered_path
            or Path(registered_path).expanduser().resolve() != controller.resolve()
            or registered_branch not in {policy["controller_branch"],
                                         policy["controller_branch"].removeprefix("refs/heads/")}
            or route.get("valid") is not True or Path(str(route.get("path", ""))).resolve() != controller.resolve()
            or route.get("role") != "controller"
            or route.get("role_source") != "controller-registration"
            or not isinstance(config_json, dict) or "lifecycle" in config_json
            or config_json.get("controllerWorkspace") != expected_shape
            or task_config.get("target_ref") != policy["product_ref"]):
        raise TaskWorkspaceError(
            "runtime bootstrap is restricted to the exact registered metadata-only controller")
    inspection = boundary.inspect(controller, policy,
                                  expected_branch=policy["controller_branch"], require_active=True)
    required_checks = {"branch_exact", "tracked_boundary", "product_absent", "role"}
    failed = sorted(name for name in required_checks if inspection.get("checks", {}).get(name) is not True)
    if failed:
        forbidden = inspection.get("forbidden_tracked_details", [])
        details = "; ".join(
            f"{item['path']} (reason={item['reason']}, rule={item['rule']})"
            for item in forbidden
        )
        suffix = f"; forbidden paths: {details}" if details else ""
        raise TaskWorkspaceError(
            "runtime bootstrap metadata-controller boundary failed: " + ", ".join(failed) + suffix)
    return {"policy_sha256": _file_sha256(metadata_path),
            "controller_branch": policy["controller_branch"],
            "product_ref": policy["product_ref"], "checks": sorted(required_checks),
            "historical_tracked_attributions": inspection.get(
                "historical_tracked_attributions", [])}


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _controller_bootstrap_identity(controller: Path) -> dict[str, Any]:
    metadata = controller / ".juno_task/config/metadata-controller.json"
    return {
        "root": str(controller.resolve()),
        "git_common_dir": str(Path(git(controller, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()),
        "head_sha": git(controller, "rev-parse", "HEAD^{commit}"),
        "head_tree": git(controller, "rev-parse", "HEAD^{tree}"),
        "metadata_controller_sha256": _file_sha256(metadata) if metadata.is_file() else None,
    }


def _bootstrap_receipt_path(controller: Path, digest: str) -> Path:
    root = (controller / RUNTIME_BOOTSTRAP_ROOT).resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = (root / f"{digest}-plan.json").resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise TaskWorkspaceError("unsafe task-runtime bootstrap receipt path") from exc
    return path


def _bootstrap_target_status(repository: Path) -> str:
    return git(repository, "status", "--porcelain=v1", "--untracked-files=all", "--", ".",
               f":(exclude){RUNTIME_BOOTSTRAP_ROOT}")


def _managed_inventory_entries_valid(assets: Any) -> bool:
    try:
        return isinstance(assets, dict) and all(
            isinstance(path, str) and normalized_relative(path, "managed inventory path") == path
            and isinstance(record, dict)
            and set(record) == {"type", "templateVersion", "sourceSha256", "installedSha256"}
            and isinstance(record.get("type"), str) and bool(record["type"])
            and is_valid_semver(record.get("templateVersion"))
            and re.fullmatch(r"[0-9a-f]{64}", str(record.get("sourceSha256", ""))) is not None
            and re.fullmatch(r"[0-9a-f]{64}", str(record.get("installedSha256", ""))) is not None
            for path, record in assets.items())
    except TaskWorkspaceError:
        return False


def _managed_inventory_identity_valid(inventory: Any) -> bool:
    if not isinstance(inventory, dict) or inventory.get("schemaVersion") not in {1, 2}:
        return False
    expected = {"schemaVersion", "packageName", "packageVersion", "assets"}
    if inventory["schemaVersion"] == 2:
        expected.add("instructionBundle")
    if (set(inventory) != expected or inventory.get("packageName") != "@yylo/cli"
            or not is_valid_semver(inventory.get("packageVersion"))
            or not _managed_inventory_entries_valid(inventory.get("assets"))):
        return False
    if inventory["schemaVersion"] == 1:
        return True
    identity = inventory.get("instructionBundle")
    assets = inventory["assets"]
    projected = [{"destination": destination, "type": record.get("type"),
                  "sourceSha256": record.get("sourceSha256"),
                  "installedSha256": record.get("installedSha256")}
                 for destination, record in sorted(assets.items())]
    assets_sha = hashlib.sha256(
        json.dumps(projected, separators=(",", ":")).encode()).hexdigest()
    core = {"schemaVersion": identity.get("schemaVersion") if isinstance(identity, dict) else None,
            "semanticVersion": identity.get("semanticVersion") if isinstance(identity, dict) else None,
            "packageVersion": identity.get("packageVersion") if isinstance(identity, dict) else None,
            "assetCount": identity.get("assetCount") if isinstance(identity, dict) else None,
            "assetsSha256": identity.get("assetsSha256") if isinstance(identity, dict) else None}
    bundle_sha = hashlib.sha256(json.dumps(core, separators=(",", ":")).encode()).hexdigest()
    return bool(isinstance(identity, dict)
                and identity.get("schemaVersion") == "juno_instruction_bundle.v1"
                and identity.get("semanticVersion") == "1.0.0"
                and identity.get("packageVersion") == inventory["packageVersion"]
                and identity.get("assetCount") == len(assets)
                and identity.get("assetsSha256") == assets_sha
                and identity.get("bundleSha256") == bundle_sha)


def _bind_instruction_bundle_identity(inventory: dict[str, Any]) -> None:
    if inventory.get("schemaVersion") != 2:
        return
    assets = inventory["assets"]
    projected = [{"destination": destination, "type": record.get("type"),
                  "sourceSha256": record.get("sourceSha256"),
                  "installedSha256": record.get("installedSha256")}
                 for destination, record in sorted(assets.items())]
    core = {"schemaVersion": "juno_instruction_bundle.v1", "semanticVersion": "1.0.0",
            "packageVersion": inventory["packageVersion"], "assetCount": len(assets),
            "assetsSha256": hashlib.sha256(
                json.dumps(projected, separators=(",", ":")).encode()).hexdigest()}
    inventory["instructionBundle"] = {**core, "bundleSha256": hashlib.sha256(
        json.dumps(core, separators=(",", ":")).encode()).hexdigest()}


def cli_version_output_valid(result: subprocess.CompletedProcess[str],
                             version: str, cwd: Path) -> bool:
    """Accept only the current or compatible canonical --version contracts."""
    if result.stdout in {f"{version}\n", f"yylo {version}\n"} and result.stderr == "":
        return True
    if result.stdout != f"{version}\n":
        return False
    node_version = r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    node_platform = r"(?:aix|android|darwin|freebsd|linux|openbsd|sunos|win32)"
    historical_banner = (
        rf"\n🎯 YYLO v{re.escape(version)} - TypeScript CLI\n"
        rf"   Node\.js {node_version} on {node_platform}\n"
        rf"   Working directory: {re.escape(str(cwd))}\n\n"
    )
    return re.fullmatch(historical_banner, result.stderr) is not None


def _legacy_installed_runtime_prior(controller: Path, prior: bytes, prior_mode: str,
                                    recovery_package_version: str) -> dict[str, Any]:
    """Prove an inventory-less consumer blob came from the registered old release."""
    identity_path = controller / ".juno_task/runtime/identity.json"
    if identity_path.is_symlink() or not identity_path.is_file():
        raise TaskWorkspaceError(
            "consumer target task runtime lacks managed inventory and installed runtime identity")
    try:
        identity_bytes = identity_path.read_bytes()
        identity = json.loads(identity_bytes)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TaskWorkspaceError(
            "consumer target installed runtime identity is missing or invalid") from exc
    required = {"package", "version", "executable", "executable_sha256", "source", "tracked"}
    if (not isinstance(identity, dict) or set(identity) != required
            or identity.get("package") != "@yylo/cli"
            or identity.get("source") != "installed-release"
            or identity.get("tracked") is not False
            or not is_valid_semver(identity.get("version"))
            or not semver_precedes(identity["version"], recovery_package_version)
            or re.fullmatch(r"[0-9a-f]{64}", str(identity.get("executable_sha256", ""))) is None):
        raise TaskWorkspaceError(
            "consumer target installed runtime identity is invalid or not older than recovery")
    configured_version = git(
        controller, "config", "--worktree", "--get", "juno.controller.runtimeVersion",
        check=False)
    configured_executable = git(
        controller, "config", "--worktree", "--get", "juno.controller.runtimeExecutable",
        check=False)
    try:
        executable = Path(identity["executable"]).expanduser().resolve(strict=True)
    except (OSError, TypeError, ValueError) as exc:
        raise TaskWorkspaceError("consumer target installed runtime executable is missing") from exc
    if (str(executable) != identity["executable"]
            or configured_version != identity["version"]
            or configured_executable != identity["executable"]
            or not executable.is_file() or not os.access(executable, os.X_OK)):
        raise TaskWorkspaceError("consumer target installed runtime identity is stale or tampered")
    executable_sha256 = hashlib.sha256(executable.read_bytes()).hexdigest()
    if executable_sha256 != identity["executable_sha256"]:
        raise TaskWorkspaceError("consumer target installed runtime identity is stale or tampered")
    if git(executable.parent, "rev-parse", "--show-toplevel", check=False):
        raise TaskWorkspaceError("consumer target installed runtime must be outside Git")
    try:
        package_root = executable.parents[2]
    except IndexError as exc:
        raise TaskWorkspaceError(
            "consumer target installed runtime package layout is invalid") from exc
    if (executable.parent.parent != package_root / "dist"
            or executable.name not in {"cli.mjs", "cli.js"}):
        raise TaskWorkspaceError("consumer target installed runtime package layout is invalid")
    try:
        manifest_path = package_root / "package.json"
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
        template = package_root / "dist/templates/scripts/task_workspace.py"
        template_bytes = template.read_bytes()
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TaskWorkspaceError(
            "consumer target installed runtime package/template identity is missing") from exc
    if (not isinstance(manifest, dict) or manifest.get("name") != "@yylo/cli"
            or manifest.get("version") != identity["version"] or template.is_symlink()
            or template_bytes != prior):
        raise TaskWorkspaceError(
            "consumer target task runtime does not match the registered installed template")
    version_result = run([str(executable), "--version"], executable.parent, check=False)
    if (version_result.returncode != 0
            or not cli_version_output_valid(
                version_result, identity["version"], executable.parent)
            or hashlib.sha256(executable.read_bytes()).hexdigest() != executable_sha256):
        raise TaskWorkspaceError("consumer target installed runtime version output mismatched")
    prior_sha = hashlib.sha256(prior).hexdigest()
    provenance = {
        "identity_sha256": hashlib.sha256(identity_bytes).hexdigest(),
        "version": identity["version"], "executable": str(executable),
        "executable_sha256": executable_sha256, "package_root": str(package_root),
        "package_json_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "template": str(template),
        "template_sha256": hashlib.sha256(template_bytes).hexdigest(),
    }
    return {"state": "present", "mode": prior_mode, "sha256": prior_sha,
            "bytes_base64": base64.b64encode(prior).decode(),
            "classification": "exact_registered_legacy_installed_consumer_generation",
            "package_version": identity["version"], "inventory_package_version": None,
            "inventory_mode": None, "inventory_sha256": None,
            "inventory_bytes_base64": None, "legacy_runtime": provenance}


def _runtime_prior_state(controller: Path, repository: Path, target_sha: str,
                         proposed: bytes, recovery_package_version: str) -> dict[str, Any]:
    prior = target_blob(repository, target_sha, RUNTIME_PATH)
    package_bytes = target_blob(repository, target_sha, "juno-code/package.json")
    source = target_blob(repository, target_sha,
                         "juno-code/src/templates/scripts/task_workspace.py")
    try:
        package = json.loads(package_bytes) if package_bytes is not None else None
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskWorkspaceError("target package identity is invalid; refusing bootstrap") from exc
    source_repository = package_bytes is not None or source is not None
    if source_repository and (not isinstance(package, dict)
                              or package.get("name") != "@yylo/cli"
                              or not is_valid_semver(package.get("version"))):
        raise TaskWorkspaceError("Juno source target package identity is invalid")
    if prior is None:
        if source_repository:
            target_package_version = package["version"]
            if source != proposed:
                if not semver_precedes(target_package_version, recovery_package_version):
                    raise TaskWorkspaceError(
                        "Juno source target runtime is absent at a non-older package/template "
                        "generation; upgrade or rebind the controller package/runtime to match "
                        "the target, then repair source identities atomically if still required")
                raise TaskWorkspaceError(
                    "Juno source target runtime is absent at an older package/template "
                    "generation; update package template/runtime/inventory atomically")
            raise TaskWorkspaceError(
                "Juno source target runtime is absent; update package template/runtime/inventory "
                "atomically instead of runtime bootstrap")
        inventory_bytes = target_blob(repository, target_sha, MANAGED_INVENTORY_PATH)
        if inventory_bytes is None:
            return {"state": "absent", "mode": None, "sha256": None,
                    "bytes_base64": None, "classification": "missing",
                    "inventory_mode": None, "inventory_sha256": None,
                    "inventory_bytes_base64": None}
        try:
            inventory = json.loads(inventory_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TaskWorkspaceError(
                "consumer target managed inventory is invalid; refusing bootstrap") from exc
        prior_version = inventory.get("packageVersion") if isinstance(inventory, dict) else None
        assets = inventory.get("assets") if isinstance(inventory, dict) else None
        entry = assets.get(RUNTIME_PATH) if isinstance(assets, dict) else None
        all_entries_valid = _managed_inventory_entries_valid(assets)
        runtime_version = entry.get("templateVersion") if isinstance(entry, dict) else None
        entry_valid = entry is None or (
            isinstance(entry, dict)
            and entry.get("type") == "script"
            and entry.get("installedSha256") == entry.get("sourceSha256")
            and is_valid_semver(runtime_version)
            and (runtime_version == recovery_package_version
                 or semver_precedes(runtime_version, recovery_package_version)))
        if (not _managed_inventory_identity_valid(inventory)
                or not is_valid_semver(prior_version) or not all_entries_valid
                or not entry_valid
                or (prior_version != recovery_package_version
                    and not semver_precedes(prior_version, recovery_package_version))):
            raise TaskWorkspaceError(
                "consumer target missing runtime lacks an exact non-newer managed-inventory "
                "generation; refusing bootstrap")
        inventory_row = git(repository, "ls-tree", target_sha, "--", MANAGED_INVENTORY_PATH)
        inventory_mode = inventory_row.split(None, 1)[0] if inventory_row else ""
        if inventory_mode not in {"100644", "100755"}:
            raise TaskWorkspaceError("target managed inventory has an unsafe Git mode")
        return {"state": "absent", "mode": None, "sha256": None,
                "bytes_base64": None, "classification": "missing",
                "inventory_mode": inventory_mode,
                "inventory_sha256": hashlib.sha256(inventory_bytes).hexdigest(),
                "inventory_bytes_base64": base64.b64encode(inventory_bytes).decode()}
    tree_row = git(repository, "ls-tree", target_sha, "--", RUNTIME_PATH)
    try:
        prior_mode = tree_row.split(None, 1)[0]
    except (AttributeError, IndexError) as exc:
        raise TaskWorkspaceError("target task runtime tree identity is invalid") from exc
    if prior_mode not in {"100644", "100755"}:
        raise TaskWorkspaceError("target task runtime has an unsafe Git mode")
    prior_sha = hashlib.sha256(prior).hexdigest()
    source_path = "juno-code/src/templates/scripts/task_workspace.py"
    source = target_blob(repository, target_sha, source_path)
    inventory_bytes = target_blob(repository, target_sha, MANAGED_INVENTORY_PATH)
    try:
        inventory = json.loads(inventory_bytes) if inventory_bytes is not None else None
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskWorkspaceError("target managed inventory is invalid; refusing bootstrap") from exc
    inventory_package_version = inventory.get("packageVersion") if isinstance(inventory, dict) else None
    assets = inventory.get("assets") if isinstance(inventory, dict) else None
    entry = assets.get(RUNTIME_PATH) if isinstance(assets, dict) else None
    runtime_package_version = entry.get("templateVersion") if isinstance(entry, dict) else None
    all_entries_valid = _managed_inventory_entries_valid(assets)
    inventory_valid = (
        _managed_inventory_identity_valid(inventory)
        and is_valid_semver(inventory_package_version)
        and all_entries_valid
        and isinstance(entry, dict)
        and entry.get("type") == "script"
        and is_valid_semver(runtime_package_version)
        and entry.get("sourceSha256") == prior_sha
        and entry.get("installedSha256") == prior_sha
    )
    if source_repository:
        if source != prior:
            raise TaskWorkspaceError("Juno source target template/runtime identity is inconsistent")
        if not inventory_valid or package.get("version") != runtime_package_version:
            raise TaskWorkspaceError(
                "Juno source target runtime is customized or lacks exact "
                "package/source/inventory provenance; refusing bootstrap")
        if not semver_precedes(runtime_package_version, recovery_package_version):
            raise TaskWorkspaceError(
                "Juno source target generation is not older than the recovery package; upgrade "
                "or rebind the controller package/runtime to match the target")
        raise TaskWorkspaceError(
            "Juno source target runtime is stale; update package template/runtime/inventory "
            "atomically instead of runtime bootstrap")
    if not inventory_valid:
        if inventory_bytes is None:
            return _legacy_installed_runtime_prior(
                controller, prior, prior_mode, recovery_package_version)
        raise TaskWorkspaceError(
            "consumer target task runtime is customized or lacks exact managed-inventory "
            "provenance; refusing bootstrap")
    if not semver_precedes(runtime_package_version, recovery_package_version):
        raise TaskWorkspaceError(
            "consumer target managed runtime package generation is not older than the recovery "
            "package; refusing bootstrap")
    inventory_row = git(repository, "ls-tree", target_sha, "--", MANAGED_INVENTORY_PATH)
    inventory_mode = inventory_row.split(None, 1)[0] if inventory_row else ""
    if inventory_mode not in {"100644", "100755"}:
        raise TaskWorkspaceError("target managed inventory has an unsafe Git mode")
    return {"state": "present", "mode": prior_mode, "sha256": prior_sha,
            "bytes_base64": base64.b64encode(prior).decode(),
            "classification": "exact_managed_inventory_consumer_generation",
            "package_version": runtime_package_version,
            "inventory_package_version": inventory_package_version,
            "inventory_mode": inventory_mode,
            "inventory_sha256": hashlib.sha256(inventory_bytes).hexdigest(),
            "inventory_bytes_base64": base64.b64encode(inventory_bytes).decode()}


def _proposed_inventory(prior: dict[str, Any], package_version: str,
                        runtime_sha256: str) -> dict[str, Any]:
    if not isinstance(prior, dict):
        raise TaskWorkspaceError("task-runtime bootstrap prior inventory binding is invalid")
    encoded = prior.get("inventory_bytes_base64")
    if encoded is None:
        inventory = {"schemaVersion": 1, "packageName": "@yylo/cli",
                     "packageVersion": package_version, "assets": {}}
        inventory_mode = "100644"
    else:
        try:
            inventory = json.loads(base64.b64decode(encoded, validate=True))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TaskWorkspaceError("task-runtime bootstrap prior inventory is invalid") from exc
        inventory_mode = prior.get("inventory_mode")
        if (not _managed_inventory_identity_valid(inventory)
                or not isinstance(inventory.get("assets"), dict)
                or inventory_mode not in {"100644", "100755"}):
            raise TaskWorkspaceError("task-runtime bootstrap prior inventory binding is invalid")
    inventory["packageVersion"] = package_version
    inventory["assets"][RUNTIME_PATH] = {
        "type": "script", "templateVersion": package_version,
        "sourceSha256": runtime_sha256, "installedSha256": runtime_sha256,
    }
    _bind_instruction_bundle_identity(inventory)
    inventory_bytes = (json.dumps(inventory, indent=2) + "\n").encode()
    return {"path": MANAGED_INVENTORY_PATH, "mode": inventory_mode,
            "sha256": hashlib.sha256(inventory_bytes).hexdigest(),
            "bytes_base64": base64.b64encode(inventory_bytes).decode()}


def _runtime_bootstrap_plan(controller: Path, package_version: str,
                            package_runtime_sha256: str) -> dict[str, Any]:
    config = load_config(controller)
    controller_class = require_metadata_only_controller(controller, config)
    if not is_valid_semver(package_version):
        raise TaskWorkspaceError("invalid package version identity")
    running = Path(__file__).resolve().read_bytes()
    running_sha = hashlib.sha256(running).hexdigest()
    if not re.fullmatch(r"[0-9a-f]{64}", package_runtime_sha256) or running_sha != package_runtime_sha256:
        raise TaskWorkspaceError("package task-runtime hash does not match the executing recovery engine")
    repository = product_repository(controller, config)
    if _bootstrap_target_status(repository):
        raise TaskWorkspaceError("configured target worktree is dirty; refusing runtime bootstrap")
    target_ref = config["target_ref"]
    target_sha = ref_sha(repository, target_ref)
    target_tree = git(repository, "rev-parse", f"{target_sha}^{{tree}}")
    prior = _runtime_prior_state(
        controller, repository, target_sha, running, package_version)
    if (prior["sha256"] == running_sha
            and (prior.get("classification") != "exact_managed_inventory_consumer_generation"
                 or prior.get("mode") != "100755")):
        raise TaskWorkspaceError("target task runtime already matches the package")
    proposed_inventory = _proposed_inventory(prior, package_version, running_sha)
    plan = {
        "schema_version": RUNTIME_BOOTSTRAP_SCHEMA,
        "operation": "plan",
        "controller_identity": {**_controller_bootstrap_identity(controller),
                                "controller_class": controller_class},
        "package": {"name": "@yylo/cli", "version": package_version,
                    "task_runtime_sha256": running_sha},
        "target": {"repository": str(repository), "ref": target_ref,
                   "sha": target_sha, "tree": target_tree},
        "path": RUNTIME_PATH,
        "prior": prior,
        "proposed": {"mode": "100755", "sha256": running_sha,
                     "bytes_base64": base64.b64encode(running).decode(),
                     "inventory": proposed_inventory},
    }
    raw = (json.dumps(plan, sort_keys=True, separators=(",", ":")) + "\n").encode()
    digest = hashlib.sha256(raw).hexdigest()
    path = _bootstrap_receipt_path(controller, digest)
    if path.exists() and path.read_bytes() != raw:
        raise TaskWorkspaceError("immutable task-runtime bootstrap receipt collision")
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw); handle.flush(); os.fsync(handle.fileno())
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    return {**plan, "receipt": {"path": str(path), "sha256": digest}}


def _load_runtime_bootstrap_plan(controller: Path, receipt_path: Path,
                                 package_version: str,
                                 package_runtime_sha256: str) -> tuple[dict[str, Any], str]:
    path = receipt_path.expanduser().resolve()
    root = (controller / RUNTIME_BOOTSTRAP_ROOT).resolve()
    try:
        path.relative_to(root)
        raw = path.read_bytes()
        plan = json.loads(raw)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        raise TaskWorkspaceError(f"invalid task-runtime bootstrap receipt: {exc}") from exc
    digest = hashlib.sha256(raw).hexdigest()
    if path.name != f"{digest}-plan.json":
        raise TaskWorkspaceError("task-runtime bootstrap receipt immutable identity mismatch")
    required = {"schema_version", "operation", "controller_identity", "package",
                "target", "path", "prior", "proposed"}
    if (not isinstance(plan, dict) or set(plan) != required
            or plan.get("schema_version") != RUNTIME_BOOTSTRAP_SCHEMA
            or plan.get("operation") != "plan" or plan.get("path") != RUNTIME_PATH
            or plan.get("package") != {"name": "@yylo/cli", "version": package_version,
                                       "task_runtime_sha256": package_runtime_sha256}
            or not isinstance(plan.get("controller_identity"), dict)
            or not isinstance(plan.get("target"), dict)
            or set(plan["target"]) != {"repository", "ref", "sha", "tree"}
            or not isinstance(plan["target"].get("repository"), str)
            or not isinstance(plan["target"].get("ref"), str)
            or not SHA_RE.fullmatch(str(plan["target"].get("sha", "")))
            or not SHA_RE.fullmatch(str(plan["target"].get("tree", "")))
            or not isinstance(plan.get("prior"), dict)
            or not isinstance(plan.get("proposed"), dict)):
        raise TaskWorkspaceError("task-runtime bootstrap receipt/controller/package identity mismatch")
    try:
        proposed = base64.b64decode(plan["proposed"]["bytes_base64"], validate=True)
    except (KeyError, TypeError, ValueError) as exc:
        raise TaskWorkspaceError("task-runtime bootstrap proposed bytes are invalid") from exc
    if (set(plan["proposed"]) != {"mode", "sha256", "bytes_base64", "inventory"}
            or hashlib.sha256(proposed).hexdigest() != package_runtime_sha256
            or plan["proposed"].get("sha256") != package_runtime_sha256
            or plan["proposed"].get("mode") != "100755"
            or hashlib.sha256(Path(__file__).resolve().read_bytes()).hexdigest() != package_runtime_sha256):
        raise TaskWorkspaceError("task-runtime bootstrap package bytes/hash mismatch")
    proposed_inventory = plan["proposed"].get("inventory")
    expected_inventory = _proposed_inventory(
        plan.get("prior", {}), package_version, package_runtime_sha256)
    if proposed_inventory != expected_inventory:
        raise TaskWorkspaceError(
            "task-runtime bootstrap inventory is not derived from bound prior/package bytes")
    consumed = root / f"{digest}-applied.json"
    durable = root / f"{digest}-completion-durable.json"
    if consumed.exists() and durable.exists():
        raise TaskWorkspaceError("task-runtime bootstrap receipt has already been applied")
    return plan, digest


def _write_runtime_bootstrap_record(path: Path, payload: dict[str, Any]) -> bytes:
    raw = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if path.exists():
        if path.read_bytes() != raw:
            raise TaskWorkspaceError(f"immutable task-runtime bootstrap record collision: {path.name}")
        return raw
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(6)}")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    return raw


def _target_ref_holders(repository: Path, target_ref: str) -> list[dict[str, Any]]:
    output = run(["git", "-C", str(repository), "worktree", "list", "--porcelain"], repository)
    records: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for line in [*output.stdout.splitlines(), ""]:
        if not line:
            if current.get("branch") == target_ref:
                records.append(current)
            current = {}
            continue
        key, _, value = line.partition(" ")
        if key in {"worktree", "HEAD", "branch", "locked"}:
            current[key.lower()] = value if value else True
    return records


@contextmanager
def _target_mutation_lock(repository: Path, target_ref: str) -> Iterator[None]:
    # Contend on the merge queue's repository/ref lock inode. Runtime recovery
    # and queue delivery must never mutate the same target concurrently.
    common = Path(git(repository, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    key = hashlib.sha256(f"{common}\0{target_ref}".encode()).hexdigest()
    path = common / "juno-locks/merge-queue" / f"{key}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise TaskWorkspaceError(
                    "another worker owns this repository/target-ref queue; refusing runtime bootstrap"
                ) from exc
            raise
        yield


def _admit_target_holder(repository: Path, target_ref: str,
                         expected_sha: str) -> dict[str, Any] | None:
    holders = _target_ref_holders(repository, target_ref)
    if len(holders) > 1:
        raise TaskWorkspaceError(
            "target ref has multiple checked-out holders; remove the extra holder with "
            "`git worktree remove <path>` after review, then rerun the same --apply receipt")
    if not holders:
        return None
    row = holders[0]
    if row.get("locked"):
        raise TaskWorkspaceError(
            "target-ref holder is locked; unlock it with `git worktree unlock <path>` after review, "
            "then rerun the same --apply receipt")
    holder = exact_root(Path(str(row.get("worktree", ""))), "target-ref holder")
    if (git(holder, "symbolic-ref", "-q", "HEAD", check=False) != target_ref
            or git(holder, "rev-parse", "HEAD^{commit}", check=False) != expected_sha):
        raise TaskWorkspaceError("target-ref holder HEAD/ref moved; refusing before target mutation")
    if git(holder, "status", "--porcelain=v1", "--untracked-files=all", check=False):
        raise TaskWorkspaceError(
            "target-ref holder is dirty; clean it without stash/reset automation, then rerun "
            "the same --apply receipt")
    return {"path": str(holder), "branch": target_ref, "previous_sha": expected_sha,
            "git_common_dir": str(Path(git(holder, "rev-parse", "--path-format=absolute",
                                           "--git-common-dir")).resolve())}


def _validate_intent_holder(repository: Path, intent_holder: Any,
                            target_ref: str) -> Path | None:
    holders = _target_ref_holders(repository, target_ref)
    if intent_holder is None:
        if holders:
            raise TaskWorkspaceError(
                "a target-ref holder appeared after planning apply; refusing durable intent recovery")
        return None
    if (not isinstance(intent_holder, dict) or set(intent_holder) != {
            "path", "branch", "previous_sha", "git_common_dir"}
            or intent_holder.get("branch") != target_ref):
        raise TaskWorkspaceError("task-runtime bootstrap target-holder intent is invalid")
    if len(holders) != 1 or Path(str(holders[0].get("worktree", ""))).resolve() != Path(
            intent_holder["path"]).resolve() or holders[0].get("locked"):
        raise TaskWorkspaceError("target-ref holder topology changed after durable apply intent")
    holder = exact_root(Path(intent_holder["path"]), "durable target-ref holder")
    if (Path(git(holder, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
            != Path(intent_holder["git_common_dir"]).resolve()):
        raise TaskWorkspaceError("target-ref holder Git identity changed")
    return holder


def _bootstrap_path_bytes(prior: dict[str, Any], proposed: bytes,
                          proposed_inventory: bytes | None) -> dict[str, tuple[bytes | None, bytes]]:
    prior_runtime = (base64.b64decode(prior["bytes_base64"], validate=True)
                     if prior.get("bytes_base64") is not None else None)
    paths = {}
    if prior_runtime != proposed or prior.get("mode") != "100755":
        paths[RUNTIME_PATH] = (prior_runtime, proposed)
    if proposed_inventory is not None:
        prior_inventory = (base64.b64decode(prior["inventory_bytes_base64"], validate=True)
                           if prior.get("inventory_bytes_base64") is not None else None)
        if prior_inventory != proposed_inventory:
            paths[MANAGED_INVENTORY_PATH] = (prior_inventory, proposed_inventory)
    if not paths:
        raise TaskWorkspaceError("task-runtime bootstrap has no exact path transition")
    return paths


def _holder_dirt_matches_interrupted_runtime_sync(
        holder: Path, prior: dict[str, Any], proposed: bytes,
        proposed_inventory: bytes | None = None) -> bool:
    status = run(["git", "-C", str(holder), "status", "--porcelain=v1",
                  "--untracked-files=all"], holder, check=False).stdout.rstrip("\n")
    rows = [line for line in status.splitlines() if line]
    try:
        paths = _bootstrap_path_bytes(prior, proposed, proposed_inventory)
    except (KeyError, TypeError, ValueError):
        return False
    if not rows or any(line[3:] not in paths for line in rows):
        return False
    saw_proposed = False
    for path, (prior_bytes, proposed_bytes) in paths.items():
        destination = holder / path
        working = destination.read_bytes() if destination.is_file() else None
        index_result = subprocess.run(
            ["git", "-C", str(holder), "show", f":{path}"], cwd=holder,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        indexed = index_result.stdout if index_result.returncode == 0 else None
        # Every path must remain at an exact prior/proposed boundary. At least
        # one proposed side proves this is a package-created partial transition.
        admitted = {prior_bytes, proposed_bytes}
        if working not in admitted or indexed not in admitted:
            return False
        saw_proposed = saw_proposed or proposed_bytes in {working, indexed}
    return saw_proposed


def _holder_is_prepared_for_cas(holder: Path, previous_sha: str,
                                proposed: bytes,
                                proposed_inventory: bytes | None = None) -> bool:
    if git(holder, "rev-parse", "HEAD^{commit}", check=False) != previous_sha:
        return False
    prior = {
        "mode": (git(holder, "ls-tree", previous_sha, "--", RUNTIME_PATH,
                     check=False).split(None, 1) or [""])[0],
        "bytes_base64": base64.b64encode(
            target_blob(holder, previous_sha, RUNTIME_PATH) or b"").decode(),
        "inventory_bytes_base64": (base64.b64encode(
            target_blob(holder, previous_sha, MANAGED_INVENTORY_PATH) or b"").decode()
            if target_blob(holder, previous_sha, MANAGED_INVENTORY_PATH) is not None else None),
    }
    paths = {path: after for path, (_, after) in _bootstrap_path_bytes(
        prior, proposed, proposed_inventory).items()}
    expected_status = []
    for path in sorted(paths):
        prior = run(["git", "-C", str(holder), "cat-file", "-e",
                     f"{previous_sha}:{path}"], holder, check=False)
        expected_status.append(f'{"M" if prior.returncode == 0 else "A"}  {path}')
    status = git(holder, "status", "--porcelain=v1", "--untracked-files=all", check=False)
    if status.splitlines() != expected_status:
        return False
    for path, expected in paths.items():
        destination = holder / path
        if not destination.is_file() or destination.read_bytes() != expected:
            return False
        indexed = subprocess.run(
            ["git", "-C", str(holder), "show", f":{path}"], cwd=holder,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if indexed.returncode != 0 or indexed.stdout != expected:
            return False
    return True


def _prepare_target_holder_for_cas(holder: Path, target_ref: str,
                                   previous_sha: str, commit_sha: str,
                                   prior: dict[str, Any], proposed: bytes,
                                   proposed_inventory: bytes | None = None) -> None:
    current = git(holder, "rev-parse", "HEAD^{commit}", check=False)
    status = git(holder, "status", "--porcelain=v1", "--untracked-files=all", check=False)
    branch = git(holder, "symbolic-ref", "-q", "HEAD", check=False)
    if current != previous_sha or branch != target_ref:
        raise TaskWorkspaceError("target-ref holder moved outside the durable apply intent")
    if _holder_is_prepared_for_cas(
            holder, previous_sha, proposed, proposed_inventory):
        return
    recovering_interruption = bool(status) and _holder_dirt_matches_interrupted_runtime_sync(
        holder, prior, proposed, proposed_inventory)
    if recovering_interruption:
        paths = " ".join(sorted(_bootstrap_path_bytes(
            prior, proposed, proposed_inventory)))
        raise TaskWorkspaceError(
            "target-holder synchronization stopped in an exact package-created partial state; "
            f"after review run `git restore --source={previous_sha} --staged --worktree -- "
            f"{paths}` in {holder}, then rerun the same --apply receipt")
    if status:
        raise TaskWorkspaceError("target-ref holder became dirty before synchronization")
    # Prepare the exact planned-path index/worktree transition while the ref still names
    # previous_sha. Only after exact prepared-state verification may CAS advance
    # the branch. Thus no post-CAS operation can overwrite concurrent holder dirt.
    # A one-tree merge is deliberately non-destructive: unlike --reset, Git
    # refuses when tracked or untracked working bytes raced the admitted index.
    result = run(["git", "-C", str(holder), "read-tree", "-m", "-u", commit_sha],
                 holder, check=False)
    if result.returncode:
        raise TaskWorkspaceError(
            "target-holder synchronization was interrupted before CAS; rerun the same --apply receipt")
    if (git(holder, "symbolic-ref", "-q", "HEAD", check=False) != target_ref
            or not _holder_is_prepared_for_cas(
                holder, previous_sha, proposed, proposed_inventory)):
        raise TaskWorkspaceError(
            "target-holder synchronization is incomplete before CAS; rerun the same --apply receipt")


def _validate_runtime_bootstrap_commit(repository: Path, plan: dict[str, Any],
                                       commit_sha: str, proposed: bytes,
                                       proposed_inventory: bytes | None = None) -> str:
    previous_sha = plan["target"]["sha"]
    if git(repository, "rev-parse", f"{commit_sha}^", check=False) != previous_sha:
        raise TaskWorkspaceError("runtime bootstrap commit parent mismatch")
    committed_row = git(repository, "ls-tree", commit_sha, "--", RUNTIME_PATH, check=False)
    changed = git(repository, "diff-tree", "--no-commit-id", "--name-only", "-r",
                  commit_sha, check=False).splitlines()
    expected_paths = list(_bootstrap_path_bytes(
        plan["prior"], proposed, proposed_inventory))
    inventory_valid = True
    if proposed_inventory is not None:
        inventory_row = git(repository, "ls-tree", commit_sha, "--", MANAGED_INVENTORY_PATH,
                            check=False)
        inventory_valid = (
            target_blob(repository, commit_sha, MANAGED_INVENTORY_PATH) == proposed_inventory
            and inventory_row.startswith(plan["proposed"]["inventory"]["mode"] + " blob "))
    if (target_blob(repository, commit_sha, RUNTIME_PATH) != proposed
            or not committed_row.startswith(plan["proposed"]["mode"] + " blob ")
            or sorted(changed) != sorted(expected_paths) or not inventory_valid):
        raise TaskWorkspaceError("runtime bootstrap reviewed commit identity mismatch")
    return git(repository, "rev-parse", f"{commit_sha}^{{tree}}")


def _apply_runtime_bootstrap(controller: Path, package_version: str,
                             package_runtime_sha256: str, receipt_path: Path) -> dict[str, Any]:
    config = load_config(controller)
    controller_class = require_metadata_only_controller(controller, config)
    plan, digest = _load_runtime_bootstrap_plan(
        controller, receipt_path, package_version, package_runtime_sha256)
    expected_controller_identity = {**_controller_bootstrap_identity(controller),
                                    "controller_class": controller_class}
    if plan.get("controller_identity") != expected_controller_identity:
        raise TaskWorkspaceError("task-runtime bootstrap controller identity mismatch")
    repository = product_repository(controller, config)
    target = plan["target"]
    if str(repository) != target.get("repository") or config["target_ref"] != target.get("ref"):
        raise TaskWorkspaceError("task-runtime bootstrap target identity changed")
    proposed = base64.b64decode(plan["proposed"]["bytes_base64"], validate=True)
    if (_runtime_prior_state(controller, repository, target["sha"], proposed, package_version)
            != plan.get("prior")):
        raise TaskWorkspaceError(
            "task-runtime bootstrap bound target prior state does not match the receipt")
    inventory_plan = plan["proposed"].get("inventory")
    proposed_inventory = (base64.b64decode(inventory_plan["bytes_base64"], validate=True)
                          if inventory_plan is not None else None)
    record_root = (controller / RUNTIME_BOOTSTRAP_ROOT).resolve()
    intent_path = record_root / f"{digest}-apply-intent.json"
    applied_path = record_root / f"{digest}-applied.json"
    durable_path = record_root / f"{digest}-completion-durable.json"
    intent: dict[str, Any] | None = None
    if not intent_path.exists() and _bootstrap_target_status(repository):
        raise TaskWorkspaceError("configured target worktree is dirty; refusing runtime bootstrap")
    if intent_path.exists():
        try:
            intent = json.loads(intent_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise TaskWorkspaceError("task-runtime bootstrap apply intent is invalid") from exc
        if (not isinstance(intent, dict) or set(intent) != {
                "schema_version", "operation", "plan_sha256", "target_ref",
                "previous_sha", "commit_sha", "tree", "path", "package", "target_holder"}
                or intent.get("schema_version") != RUNTIME_BOOTSTRAP_SCHEMA
                or intent.get("operation") != "apply-intent" or intent.get("plan_sha256") != digest
                or intent.get("target_ref") != config["target_ref"]
                or intent.get("previous_sha") != target.get("sha")
                or intent.get("path") != RUNTIME_PATH
                or not SHA_RE.fullmatch(str(intent.get("commit_sha", "")))
                or not SHA_RE.fullmatch(str(intent.get("tree", "")))
                or intent.get("package") != plan["package"]):
            raise TaskWorkspaceError("task-runtime bootstrap apply intent identity mismatch")
        commit_sha = intent.get("commit_sha", "")
        tree = _validate_runtime_bootstrap_commit(
            repository, plan, commit_sha, proposed, proposed_inventory)
        if tree != intent.get("tree"):
            raise TaskWorkspaceError("task-runtime bootstrap apply intent tree mismatch")
    else:
        current_sha = ref_sha(repository, config["target_ref"])
        if (current_sha != target.get("sha")
                or git(repository, "rev-parse", f"{current_sha}^{{tree}}") != target.get("tree")):
            raise TaskWorkspaceError("task-runtime bootstrap target ref moved after planning")
        if _runtime_prior_state(controller, repository, current_sha, proposed,
                                package_version) != plan.get("prior"):
            raise TaskWorkspaceError("task-runtime bootstrap prior path state changed")
        workspace_root = Path(config["workspace_root"])
        workspace_root.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".yy-task-runtime-bootstrap-", dir=workspace_root))
        added = False
        try:
            temporary.rmdir()
            run(["git", "-C", str(repository), "worktree", "add", "--detach",
                 str(temporary), current_sha], repository)
            added = True
            if git(temporary, "status", "--porcelain=v1", "--untracked-files=all"):
                raise TaskWorkspaceError("isolated target worktree is not clean")
            changed_paths = list(_bootstrap_path_bytes(
                plan["prior"], proposed, proposed_inventory))
            if RUNTIME_PATH in changed_paths:
                destination = temporary / RUNTIME_PATH
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(proposed); destination.chmod(0o755)
            if MANAGED_INVENTORY_PATH in changed_paths:
                inventory_destination = temporary / MANAGED_INVENTORY_PATH
                inventory_destination.write_bytes(proposed_inventory)
                inventory_destination.chmod(int(inventory_plan["mode"], 8) & 0o777)
            run(["git", "-C", str(temporary), "add", "--", *changed_paths], temporary)
            if (git(temporary, "diff", "--cached", "--name-only").splitlines()
                    != sorted(changed_paths)):
                raise TaskWorkspaceError("runtime bootstrap staged an unexpected path")
            run(["git", "-C", str(temporary), "-c", "core.hooksPath=/dev/null", "commit", "-m",
                 f"chore(juno): bootstrap package task runtime\n\nReviewed-Plan: {digest}\nJuno-Package: {package_version}"], temporary)
            commit_sha = git(temporary, "rev-parse", "HEAD^{commit}")
            tree = _validate_runtime_bootstrap_commit(
                repository, plan, commit_sha, proposed, proposed_inventory)
        finally:
            if added:
                run(["git", "-C", str(repository), "worktree", "remove", "--force",
                     str(temporary)], repository, check=False)
            elif temporary.exists():
                temporary.rmdir()
        with _target_mutation_lock(repository, config["target_ref"]):
            if ref_sha(repository, config["target_ref"]) != current_sha:
                raise TaskWorkspaceError("task-runtime bootstrap target ref raced before durable intent")
            target_holder = _admit_target_holder(repository, config["target_ref"], current_sha)
            intent = {"schema_version": RUNTIME_BOOTSTRAP_SCHEMA, "operation": "apply-intent",
                      "plan_sha256": digest, "target_ref": config["target_ref"],
                      "previous_sha": current_sha, "commit_sha": commit_sha, "tree": tree,
                      "path": RUNTIME_PATH, "package": plan["package"],
                      "target_holder": target_holder}
            _write_runtime_bootstrap_record(intent_path, intent)

    guard_holder: Path | None = None
    guard_ownership_path = record_root / f"{digest}-guard-ownership.json"
    try:
        with _target_mutation_lock(repository, config["target_ref"]):
            if intent["target_holder"] is None:
                workspace_root = Path(config["workspace_root"])
                expected_guard = (workspace_root /
                                  f".yy-task-runtime-bootstrap-guard-{digest}").resolve()
                ownership = {"schema_version": RUNTIME_BOOTSTRAP_SCHEMA,
                             "operation": "guard-ownership", "plan_sha256": digest,
                             "repository": str(repository), "target_ref": config["target_ref"],
                             "path": str(expected_guard)}
                ownership_exists = guard_ownership_path.exists()
                if ownership_exists:
                    try:
                        if json.loads(guard_ownership_path.read_text()) != ownership:
                            raise TaskWorkspaceError("package-owned target guard record mismatch")
                    except (OSError, json.JSONDecodeError) as exc:
                        raise TaskWorkspaceError("package-owned target guard record is invalid") from exc
                holders = _target_ref_holders(repository, config["target_ref"])
                if holders:
                    if not ownership_exists:
                        raise TaskWorkspaceError(
                            "target-ref holder lacks durable package guard ownership")
                    if (len(holders) != 1 or holders[0].get("locked")
                            or Path(str(holders[0].get("worktree", ""))).resolve()
                            != expected_guard):
                        raise TaskWorkspaceError(
                            "a non-guard target-ref holder appeared after durable apply intent")
                    holder = exact_root(expected_guard, "durable package-owned target guard")
                    guard_digest = git(holder, "config", "--worktree", "--get",
                                       "juno.bootstrap.guardDigest", check=False)
                    if git(holder, "symbolic-ref", "-q", "HEAD", check=False) != config["target_ref"]:
                        raise TaskWorkspaceError("durable package-owned target guard identity changed")
                    if not guard_digest:
                        if (git(holder, "rev-parse", "HEAD^{commit}", check=False)
                                != intent["previous_sha"]
                                or git(holder, "status", "--porcelain=v1",
                                       "--untracked-files=all", check=False)):
                            raise TaskWorkspaceError(
                                "incomplete package-owned target guard is not clean at expected SHA")
                        run(["git", "-C", str(holder), "config", "--worktree",
                             "juno.bootstrap.guardDigest", digest], holder)
                    elif guard_digest != digest:
                        raise TaskWorkspaceError("durable package-owned target guard identity changed")
                    guard_holder = holder
                else:
                    holder = None
            else:
                holder = _validate_intent_holder(
                    repository, intent["target_holder"], config["target_ref"])
            current_sha = ref_sha(repository, config["target_ref"])
            if current_sha not in {intent["previous_sha"], intent["commit_sha"]}:
                raise TaskWorkspaceError(
                    "task-runtime bootstrap target ref moved outside the durable apply intent")
            if holder is None:
                # Hold the branch in a package-owned clean worktree through CAS
                # until immediately before durable completion. Ordinary Git worktree creation then
                # fails instead of racing the no-holder observation.
                _validate_intent_holder(repository, None, config["target_ref"])
                workspace_root = Path(config["workspace_root"])
                workspace_root.mkdir(parents=True, exist_ok=True)
                guard_holder = (workspace_root /
                                f".yy-task-runtime-bootstrap-guard-{digest}").resolve()
                if guard_holder.exists():
                    raise TaskWorkspaceError(
                        "durable package-owned target guard path exists outside Git registration")
                _write_runtime_bootstrap_record(guard_ownership_path, ownership)
                branch = config["target_ref"].removeprefix("refs/heads/")
                added = run(["git", "-C", str(repository), "worktree", "add",
                             str(guard_holder), branch], repository, check=False)
                if added.returncode:
                    raise TaskWorkspaceError(
                        "target-ref holder appeared before guarded CAS; refusing target mutation")
                run(["git", "-C", str(guard_holder), "config", "--worktree",
                     "juno.bootstrap.guardDigest", digest], guard_holder)
                holder = guard_holder
            if current_sha == intent["previous_sha"]:
                index_lock = Path(git(holder, "rev-parse", "--path-format=absolute",
                                      "--git-path", "index.lock"))
                if index_lock.exists():
                    raise TaskWorkspaceError(
                        "target-holder index is locked; refusing before target CAS advancement")
                _prepare_target_holder_for_cas(holder, config["target_ref"],
                                               intent["previous_sha"], intent["commit_sha"],
                                               plan["prior"], proposed, proposed_inventory)
                holders = _target_ref_holders(repository, config["target_ref"])
                if (len(holders) != 1
                        or Path(str(holders[0].get("worktree", ""))).resolve() != holder
                        or ref_sha(repository, config["target_ref"]) != intent["previous_sha"]
                        or not _holder_is_prepared_for_cas(
                            holder, intent["previous_sha"], proposed,
                            proposed_inventory)):
                    raise TaskWorkspaceError("target-ref holder raced before target CAS advancement")
                cas = run(["git", "-C", str(repository), "update-ref", config["target_ref"],
                           intent["commit_sha"], intent["previous_sha"]], repository, check=False)
                if cas.returncode:
                    raise TaskWorkspaceError("task-runtime bootstrap target ref CAS advancement failed")
            if (git(holder, "symbolic-ref", "-q", "HEAD", check=False) != config["target_ref"]
                    or git(holder, "rev-parse", "HEAD^{commit}", check=False) != intent["commit_sha"]
                    or git(holder, "status", "--porcelain=v1", "--untracked-files=all", check=False)):
                raise TaskWorkspaceError(
                    "target-holder changed during CAS; concurrent dirt was preserved; "
                    "rerun the same --apply receipt after review")
            result = {"schema_version": RUNTIME_BOOTSTRAP_SCHEMA, "operation": "apply",
                      "outcome": "completed", "plan_sha256": digest,
                      "target_ref": config["target_ref"], "previous_sha": intent["previous_sha"],
                      "commit_sha": intent["commit_sha"], "tree": intent["tree"],
                      "path": RUNTIME_PATH, "package": plan["package"],
                      "target_holder": intent["target_holder"]}
            if guard_holder is not None:
                if (git(guard_holder, "config", "--worktree", "--get",
                        "juno.bootstrap.guardDigest", check=False) != digest
                        or git(guard_holder, "status", "--porcelain=v1",
                               "--untracked-files=all", check=False)):
                    raise TaskWorkspaceError(
                        "package-owned target guard changed; refusing cleanup and completion")
                removed = run(["git", "-C", str(repository), "worktree", "remove",
                               str(guard_holder)], repository, check=False)
                if removed.returncode:
                    raise TaskWorkspaceError(
                        "package-owned target guard cleanup failed; rerun the same --apply receipt")
                guard_holder = None
                guard_ownership_path.unlink(missing_ok=True)
            try:
                raw = _write_runtime_bootstrap_record(applied_path, result)
                completion = {"schema_version": RUNTIME_BOOTSTRAP_SCHEMA,
                              "operation": "completion-durable", "plan_sha256": digest,
                              "applied_sha256": hashlib.sha256(raw).hexdigest(),
                              "commit_sha": intent["commit_sha"]}
                _write_runtime_bootstrap_record(durable_path, completion)
            except (OSError, TaskWorkspaceError) as exc:
                raise TaskWorkspaceError(
                    "target CAS completed but durable completion recording failed; "
                    "rerun the same --apply receipt") from exc
    finally:
        # Never force-remove a guard: process interruption leaves its exact Git
        # registration and digest for safe same-receipt recovery.
        pass
    return {**result, "receipt": {"path": str(applied_path),
                                   "sha256": hashlib.sha256(raw).hexdigest()},
            "completion_durable": {"path": str(durable_path)}}


def runtime_bootstrap(controller: Path, package_version: str,
                      package_runtime_sha256: str,
                      receipt_path: Optional[Path]) -> dict[str, Any]:
    return (_runtime_bootstrap_plan(controller, package_version, package_runtime_sha256)
            if receipt_path is None else
            _apply_runtime_bootstrap(controller, package_version,
                                     package_runtime_sha256, receipt_path))


TASK_RUN_ROOT = ".juno_task/runtime/lifecycle-runs/task"


def _verify_dependency_tree(worktree: Path, config: dict[str, Any],
                             hydration: Optional[dict[str, Any]] = None) -> None:
    """Verify installed dependency-tree integrity per configured lock cwd.

    Mirrors the frozen hydration workflow's non-mutating verify-node-lock
    probe: the lock stamp must equal the checked-in lock digest (catching a
    stale install) and npm must validate the installed tree against the exact
    lock. Beyond metadata, every installed dependency byte is verified against
    the hydration-time content manifest bound into the task record, so
    tampered or corrupted installed files that preserve all manifests are
    still detected before any worker budget is spent.
    """
    rows = [*config["focused_validation"], config["full_suite_validation"]]
    for profile in config.get("validation_profiles") or []:
        rows.extend(profile["commands"])
    seen: set[str] = set()
    had_locks = False
    for row in rows:
        relative = normalized_relative(row["cwd"], "validation cwd")
        if relative in seen:
            continue
        seen.add(relative)
        package = worktree / relative
        lock = package / "package-lock.json"
        if not lock.is_file():
            continue
        had_locks = True
        stamp = package / "node_modules/.yylo-package-lock.sha256"
        if (not stamp.is_file() or stamp.is_symlink()
                or stamp.read_text().strip() != hashlib.sha256(lock.read_bytes()).hexdigest()):
            raise TaskWorkspaceError(
                f"validation_dependencies_missing: {relative} installed Node dependencies "
                "are missing or stale for package-lock.json")
        result = subprocess.run(
            ["npm", "ls", "--depth=0", "--ignore-scripts"], cwd=package,
            stdin=subprocess.DEVNULL, text=True, capture_output=True, check=False)
        if result.returncode:
            detail = (result.stdout or "").strip() or (result.stderr or "").strip()
            raise TaskWorkspaceError(
                f"validation_dependencies_missing: {relative} installed Node dependency "
                f"tree does not satisfy the exact lock: {detail[-300:]}")
    if not had_locks:
        return
    manifest_reference = (hydration or {}).get("content_manifest")
    if not isinstance(manifest_reference, dict) or not manifest_reference.get("path"):
        raise TaskWorkspaceError(
            "validation_dependencies_missing: hydration recorded no installed-content "
            "manifest; rerun the authorized exact-lock hydration")
    try:
        manifest_bytes = Path(str(manifest_reference["path"])).read_bytes()
    except OSError as exc:
        raise TaskWorkspaceError(
            "validation_dependencies_missing: hydration content manifest is unavailable") from exc
    if hashlib.sha256(manifest_bytes).hexdigest() != manifest_reference.get("sha256"):
        raise TaskWorkspaceError(
            "validation_dependencies_missing: hydration content manifest digest drifted")
    try:
        expected = json.loads(manifest_bytes)
    except json.JSONDecodeError as exc:
        raise TaskWorkspaceError(
            "validation_dependencies_missing: hydration content manifest is malformed") from exc
    actual = _dependency_content_manifest(worktree, config)
    drift = [path for path in sorted(set(expected) | set(actual))
             if expected.get(path) != actual.get(path)][:8]
    if drift:
        raise TaskWorkspaceError(
            "validation_dependencies_missing: installed dependency contents drifted from "
            f"the hydration manifest: {', '.join(drift)}")


def _managed_hydration_gate(controller: Path, record: dict[str, Any],
                            lease_token: Optional[str] = None) -> dict[str, Any]:
    """Verify frozen hydration/dependency evidence before any worker budget.

    A resumed WORKING task must not spend its sole model attempt or receive
    passed worker receipts against missing, stale, or lock-drifted
    dependencies. One durable authorized exact-lock rerun is attempted when
    the frozen evidence no longer verifies; an unrecoverable gate fails closed
    before any worker launch.
    """
    worktree = exact_root(Path(record["worktree"]), "task worktree")
    config = load_config(controller)
    try:
        verify_hydration_evidence(record, worktree)
        _verify_dependency_tree(worktree, config, record.get("hydration"))
    except TaskWorkspaceError:
        hydrate(controller, record["task_id"], lease_token)
        with state_lock(controller):
            refreshed = read_state(controller)["tasks"].get(record["task_id"])
        if not isinstance(refreshed, dict):
            raise TaskWorkspaceError("managed hydration gate lost the task record")
        verify_hydration_evidence(refreshed, worktree)
        _verify_dependency_tree(worktree, config, refreshed.get("hydration"))
        record = refreshed
    dependency_evidence = validation_dependency_evidence(worktree, config)
    hydration = record.get("hydration") if isinstance(record.get("hydration"), dict) else {}
    return {"record": record,
            "dependency_evidence": dependency_evidence,
            "hydration_manifest_sha256": hydration.get("manifest_sha256")}


def _managed_worker_receipts(run_dir: Path, record: dict[str, Any],
                             hydration_gate: dict[str, Any]) -> tuple[Path, Path, Path]:
    worktree = Path(record["worktree"])
    common = str(Path(git(worktree, "rev-parse", "--path-format=absolute",
                          "--git-common-dir")).resolve())
    # Worker path authority is the effective admission: an authorized umbrella
    # supersession union is authoritative over the historical creation receipt.
    admitted_paths, _generated_admission, admission_kind = effective_admission(record)
    if admission_kind == "historical_creation":
        # Creation order is immutable authority. Older creation receipts append
        # managed outputs after their original scope, so sorting here invents a
        # different digest even though the path set is unchanged.
        expected_paths = list(admitted_paths)
        expected_paths_sha256 = stable_sha256(expected_paths)
        creation_receipt = record.get("creation_receipt", {})
        workspace_identity = record.get("workspace_identity", {})
        configured_sha256 = git(
            worktree, "config", "--worktree", "--get",
            "juno.workspace.expectedPathsSha256", check=False)
        configured_creation_sha256 = git(
            worktree, "config", "--worktree", "--get",
            "juno.workspace.createReceiptSha256", check=False)
        if (not expected_paths
                or any(not isinstance(value, str) or not value for value in expected_paths)
                or len(expected_paths) != len(set(expected_paths))
                or creation_receipt.get("expected_paths_sha256") != expected_paths_sha256
                or stable_sha256(creation_receipt)
                   != workspace_identity.get("create_receipt_sha256")
                or configured_creation_sha256
                   != workspace_identity.get("create_receipt_sha256")
                or workspace_identity.get("expected_paths_sha256") != expected_paths_sha256
                or configured_sha256 != expected_paths_sha256):
            raise TaskWorkspaceError("historical creation path authority drifted")
    else:
        # Modern superseding admission remains canonical and sorted.
        expected_paths = sorted(admitted_paths)
        expected_paths_sha256 = stable_sha256(expected_paths)
    create = {"schema_version": "juno_managed_task_run_create.v1",
              "task_id": record["task_id"], "worktree": str(worktree.resolve()),
              "branch_ref": record["branch_ref"], "git_common_dir": common,
              "expected_paths": expected_paths,
              "expected_paths_sha256": expected_paths_sha256,
              "admission_kind": admission_kind, "workspace_role": "task",
              "clean_tip_sha": git(worktree, "rev-parse", "HEAD"),
              "creation_receipt_sha256": record["workspace_identity"]["create_receipt_sha256"],
              "workspace_manifest_identity": record["creation_receipt"]["manifest_identity"]}
    supersession_sha256 = record.get("admission_supersession_sha256")
    if supersession_sha256:
        create["admission_supersession_sha256"] = supersession_sha256
    # The verify receipt reports the hydration gate that actually ran before
    # this launch; passed=True is truthful because the gate raised otherwise.
    paths = (run_dir / "create-receipt.json", run_dir / "verify-receipt.json",
             run_dir / "edit-preflight-receipt.json")
    create_ref = lifecycle_runtime.atomic_json(paths[0], create, exclusive=True)
    verify = {"schema_version": "juno_managed_task_run_verify.v1",
              "task_id": record["task_id"], "passed": True, "workspace_role": "task",
              "tip_sha": git(worktree, "rev-parse", "HEAD"),
              "create_receipt_sha256": create_ref["sha256"],
              "hydration_manifest_sha256": hydration_gate.get("hydration_manifest_sha256"),
              "dependency_evidence": hydration_gate.get("dependency_evidence", [])}
    verify_ref = lifecycle_runtime.atomic_json(paths[1], verify, exclusive=True)
    edit = {"schema_version": "juno_managed_task_run_edit_preflight.v1",
            "task_id": record["task_id"], "passed": True, "workspace_role": "task",
            "tip_sha": git(worktree, "rev-parse", "HEAD"),
            "create_receipt_sha256": create_ref["sha256"],
            "verify_receipt_sha256": verify_ref["sha256"],
            "allowed_paths_sha256": expected_paths_sha256}
    lifecycle_runtime.atomic_json(paths[2], edit, exclusive=True)
    return paths


def _predispatch_receipt(attempt_dir: Path, task_id: str, before_sha: str,
                         completed: subprocess.CompletedProcess[str],
                         receipt_paths: tuple[Path, Path, Path]) -> dict[str, str]:
    """Seal proof that the canonical runner refused before provider dispatch."""
    managed = attempt_dir / "managed-agent"
    forbidden = [managed / name for name in
                 ("launch.json", "receipt.json", "terminal.json", "active.json")]
    if any(path.exists() for path in forbidden):
        raise TaskWorkspaceError(
            "managed task worker lacks a receipt but has ambiguous launch evidence; model budget is consumed")
    inputs = []
    for path in receipt_paths:
        data = path.read_bytes()
        inputs.append({"path": str(path.resolve()),
                       "sha256": hashlib.sha256(data).hexdigest()})
    payload = {
        "schema_version": TASK_PREDISPATCH_RECOVERY_SCHEMA,
        "classification": "controller_pre_dispatch",
        "task_id": task_id, "before_sha": before_sha,
        "provider_launch_observed": False, "model_budget_consumed": False,
        "runner_exit_code": completed.returncode,
        "stdout_sha256": hashlib.sha256(completed.stdout.encode()).hexdigest(),
        "stderr_sha256": hashlib.sha256(completed.stderr.encode()).hexdigest(),
        "stderr_tail": completed.stderr[-512:], "admission_receipts": inputs,
        "safe_next_action": f"yy task run {task_id}",
    }
    return lifecycle_runtime.atomic_json(
        attempt_dir / "controller-predispatch-receipt.json", payload, exclusive=True)


def _launch_task_worker(controller: Path, task_id: str, record: dict[str, Any],
                        run_dir: Path, prompt_seed: Path, *, repair: bool,
                        timeout_seconds: int,
                        context_bytes: bytes = b"",
                        hydration_gate: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    worktree = Path(record["worktree"])
    before = git(worktree, "rev-parse", "HEAD")
    if hydration_gate is None:
        hydration_gate = _managed_hydration_gate(controller, record)
        record = hydration_gate["record"]
    create, verify, edit = _managed_worker_receipts(run_dir, record, hydration_gate)
    prompt = run_dir / "worker-prompt.md"
    task_data = task_file(controller, task_id).read_bytes()
    seed = prompt_seed.read_bytes()
    if not seed or len(seed) + len(task_data) + len(context_bytes) > 4 * 1024 * 1024:
        raise TaskWorkspaceError("managed task-run prompt is empty or unbounded")
    prompt.write_bytes(seed + b"\n\n# Canonical task\n\n" + task_data
                       + (b"\n\n# Exact failure context\n\n" + context_bytes
                          if context_bytes else b""))
    out_dir = run_dir / "managed-agent"
    runner = controller / ".juno_task/scripts/managed_agent_runner.py"
    branch = git(controller, "symbolic-ref", "-q", "HEAD")
    command = [sys.executable, str(runner), "run", "--mode", "worker",
               "--controller-root", str(controller), "--controller-branch", branch,
               "--agent-root", str(worktree), "--prompt-file", str(prompt),
               "--out-dir", str(out_dir), "--tool-id",
               "yy_task_test_repair" if repair else "yy_task_implementation",
               "--task-id", task_id, "--create-receipt", str(create),
               "--verify-receipt", str(verify), "--edit-preflight-receipt", str(edit),
               "--require-terminal-result", "--timeout-seconds", str(timeout_seconds),
               "--external-side-effects", "forbidden", "--lifecycle-hooks", "disabled"]
    completed = subprocess.run(command, cwd=controller, stdin=subprocess.DEVNULL,
                               text=True, capture_output=True)
    receipt_path = out_dir / "receipt.json"
    try:
        receipt = json.loads(receipt_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        recovery = _predispatch_receipt(
            run_dir, task_id, before, completed, (create, verify, edit))
        raise ManagedAgentPreDispatchError(
            "managed task worker was refused before provider dispatch", recovery) from exc
    reference = {"path": str(receipt_path.resolve()),
                 "sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest()}
    if completed.returncode:
        raise TaskWorkspaceError(
            f"managed task worker failed: {(completed.stderr or completed.stdout)[-512:]} receipt={reference}")
    terminal = receipt.get("terminal_result")
    if (not isinstance(terminal, dict)
            or terminal.get("state") not in {"completed", "blocked", "incomplete"}):
        raise TaskWorkspaceError("managed task worker omitted its typed terminal result")
    after = git(worktree, "rev-parse", "HEAD")
    if terminal["state"] == "completed":
        commits = git(worktree, "rev-list", "--count", f"{before}..{after}")
        if commits != "1" or git(worktree, "status", "--porcelain=v1", "--untracked-files=all"):
            raise TaskWorkspaceError(
                "managed task worker must produce exactly one logical commit and a clean worktree")
    elif after != before or git(worktree, "status", "--porcelain=v1", "--untracked-files=all"):
        raise TaskWorkspaceError(
            "blocked or ambiguous managed worker must edit zero product bytes")
    return {"terminal_state": terminal["state"], "before_sha": before,
            "after_sha": after, "receipt": reference,
            "session_id": receipt.get("session_id")}


def _task_plan_execution_identity(plan: dict[str, Any]) -> str:
    return lifecycle_runtime.digest({key: value for key, value in plan.items()
                                     if key not in {"controller_commit", "compiled_plan_sha256"}})


def _recover_task_worker(record: dict[str, Any], attempt: dict[str, Any]) -> Optional[dict[str, Any]]:
    attempt_dir = Path(str(attempt.get("attempt_dir", "")))
    receipt_path = attempt_dir / "managed-agent/receipt.json"
    if not receipt_path.is_file():
        return None
    try:
        receipt_bytes = receipt_path.read_bytes()
        receipt = json.loads(receipt_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise TaskWorkspaceError("interrupted managed worker receipt is malformed") from exc
    terminal = receipt.get("terminal_result")
    if not isinstance(terminal, dict) or terminal.get("state") not in {
            "completed", "blocked", "incomplete"}:
        raise TaskWorkspaceError("interrupted managed worker has no typed terminal receipt")
    worktree = Path(record["worktree"])
    before = attempt.get("before_sha")
    after = git(worktree, "rev-parse", "HEAD")
    clean = not git(worktree, "status", "--porcelain=v1", "--untracked-files=all")
    if terminal["state"] == "completed":
        if (not isinstance(before, str)
                or git(worktree, "rev-list", "--count", f"{before}..{after}") != "1"
                or not clean):
            raise TaskWorkspaceError("interrupted completed worker commit is not exact and clean")
    elif after != before or not clean:
        raise TaskWorkspaceError("interrupted non-completed worker changed product bytes")
    return {"terminal_state": terminal["state"], "before_sha": before,
            "after_sha": after,
            "receipt": {"path": str(receipt_path.resolve()),
                        "sha256": hashlib.sha256(receipt_bytes).hexdigest()},
            "session_id": receipt.get("session_id"), "recovered": True}


def _task_projection(controller: Path, task_id: str, run_dir: Path,
                     latest: Path, journal_path: Path, journal: dict[str, Any],
                     plan: dict[str, Any], *, state_name: str,
                     blocker: Optional[dict[str, Any]], counters: Optional[dict[str, int]] = None,
                     terminal: bool = False) -> dict[str, Any]:
    counters = counters or {name: 0 for name in
                            ("executed", "reused", "invalidated", "skipped", "not_applicable")}
    attempts = journal["attempts"]
    artifacts = [journal["compiled_plan"], *[item["receipt"] for item in
                 journal.get("workers", []) if isinstance(item.get("receipt"), dict)]]
    with state_lock(controller):
        state_record = read_state(controller)["tasks"].get(task_id, {})
    revision = hashlib.sha256(task_file(controller, task_id).read_bytes()).hexdigest()
    projection = lifecycle_runtime.compact_projection(
        kind="task-run", run_id=journal["run_id"], task_id=task_id, state=state_name,
        plan=plan, started=lifecycle_runtime.lifecycle_elapsed_started(journal),
        counters=counters, attempts=attempts, blocker=blocker,
        next_action=(f"yy merge drive --through {task_id}" if state_name == "QUEUED" else
                    (f"resolve the blocker and resume yy task run {task_id}"
                     if state_name == "NEEDS_DECISION" else
                     # BLOCKED is terminal: replaying yy task run returns this
                     # same verified projection; continuation requires an
                     # explicit reviewed task-run replacement.
                     f"resolve the blocker, then request an explicit reviewed task-run replacement for {task_id}")),
        artifacts=artifacts,
        identities={"controller_commit": plan["controller_commit"],
                    "task_revision_sha256": revision,
                    "base_sha": state_record.get("base_sha"),
                    "tip_sha": state_record.get("tip_sha"),
                    "deadline_unix_ns": journal["deadline_unix_ns"]})
    projection_index = len(journal.get("projections", [])) + 1
    # Crash recovery: a crash after the projection write but before the journal
    # append leaves the exact next numbered artifact on disk. Adopt it (its
    # identity fields are immutable) instead of colliding on recomputed bytes.
    adopted = lifecycle_runtime.adopt_interrupted_projection(
        run_dir, journal, projection_index, state_name, kind="task-run",
        expected=projection)
    if adopted is not None:
        projection = adopted
        projection_path = run_dir / "projections" / f"{projection_index:04d}-{state_name.lower()}.json"
        projection_ref = {"path": str(projection_path.resolve()),
                          "sha256": hashlib.sha256(projection_path.read_bytes()).hexdigest()}
    else:
        projection_ref = lifecycle_runtime.atomic_json(
            run_dir / "projections" / f"{projection_index:04d}-{state_name.lower()}.json",
            projection, exclusive=True)
    journal.setdefault("projections", []).append(projection_ref)
    journal["state"] = state_name
    journal["terminal"] = terminal
    lifecycle_runtime.lifecycle_journal_write(journal_path, journal)
    summary_ref = None
    if terminal:
        # The summary derives deterministically from the published projection,
        # so an interrupted publication is repaired by an exact rewrite.
        summary = lifecycle_runtime.deterministic_summary(projection)
        summary_ref = lifecycle_runtime.atomic_json(run_dir / "summary.json", summary)
    lifecycle_runtime.atomic_json(latest, {
        "schema_version": "juno_managed_task_run_latest.v2", "run_id": journal["run_id"],
        "compiled_plan_sha256": plan["compiled_plan_sha256"],
        "execution_identity_sha256": journal["execution_identity_sha256"],
        "task_revision_sha256": revision, "terminal": terminal,
        "projection_path": projection_ref["path"], "summary": summary_ref})
    return projection


def _ensure_run_fence(controller: Path, task_id: str) -> Optional[str]:
    """Natural receipt-bound successor for the controller-owned worker.

    A provably dead predecessor attempt yields exactly one successor lease
    here without operator cleanup; every other authority failure stops with
    its actionable machine code. Returns the holder token when a successor
    was issued.
    """
    with state_lock(controller):
        record = read_state(controller)["tasks"].get(task_id)
    lease = _lease_view(record)
    if lease is None or lease.get("state") != decisions.LEASE_ACTIVE:
        return None
    observation = _observe_producer(lease.get("producer"))
    authority = decisions.plan_lease_authority(
        "run", task_id, lease, None, observation, os.getpid())
    if authority.admitted:
        return None
    if authority.code != decisions.LEASE_CODE_PRODUCER_DEAD:
        raise TaskWorkspaceError(f"task run refused ({authority.code}): {authority.message}")
    successor = lease_successor(controller, task_id)
    return successor["lease_token"]


def _verify_receipt_reference(reference: Any, label: str) -> dict[str, Any]:
    if (not isinstance(reference, dict) or set(reference) != {"path", "sha256"}
            or not re.fullmatch(r"[0-9a-f]{64}", str(reference.get("sha256", "")))):
        raise TaskWorkspaceError(f"{label} reference is malformed")
    path = Path(str(reference["path"])).resolve()
    try:
        data = path.read_bytes()
        value = json.loads(data)
    except (OSError, json.JSONDecodeError) as exc:
        raise TaskWorkspaceError(f"{label} is unavailable or malformed") from exc
    if hashlib.sha256(data).hexdigest() != reference["sha256"]:
        raise TaskWorkspaceError(f"{label} digest drifted")
    return value


def _release_predispatch_budget(journal: dict[str, Any], attempt: dict[str, Any],
                                receipt: dict[str, str]) -> None:
    if attempt.get("terminal_state") == "controller_pre_dispatch":
        if attempt.get("predispatch_receipt") != receipt:
            raise TaskWorkspaceError("pre-dispatch recovery receipt identity drifted")
        return
    if attempt.get("terminal_state") is not None:
        raise TaskWorkspaceError("only a pending worker can release pre-dispatch budget")
    if attempt.get("kind") not in {"implementation", "attributable_test_repair"}:
        raise TaskWorkspaceError("pre-dispatch recovery worker kind is unsupported")
    counter = ("implementation" if attempt["kind"] == "implementation"
               else "attributable_test_repair")
    if journal["attempts"].get(counter, 0) < 1 or journal["attempts"].get("worker_launches", 0) < 1:
        raise TaskWorkspaceError("pre-dispatch recovery counters are inconsistent")
    # worker_launches is the monotonic output-directory serial, not model
    # budget. Keep it consumed so immutable attempt directories are never reused.
    journal["attempts"][counter] -= 1
    attempt.update({"terminal_state": "controller_pre_dispatch",
                    "predispatch_receipt": receipt, "model_budget_consumed": False})


def _pending_predispatch_receipt(record: dict[str, Any], attempt: dict[str, Any]) \
        -> Optional[dict[str, str]]:
    path = Path(str(attempt.get("attempt_dir", ""))) / "controller-predispatch-receipt.json"
    if not path.is_file():
        return None
    data = path.read_bytes()
    try:
        receipt = json.loads(data)
    except json.JSONDecodeError as exc:
        raise TaskWorkspaceError("controller pre-dispatch receipt is malformed") from exc
    if (data != lifecycle_runtime.canonical_bytes(receipt)
            or receipt.get("schema_version") != TASK_PREDISPATCH_RECOVERY_SCHEMA
            or receipt.get("classification") != "controller_pre_dispatch"
            or receipt.get("task_id") != record.get("task_id")
            or receipt.get("before_sha") != attempt.get("before_sha")
            or receipt.get("provider_launch_observed") is not False
            or receipt.get("model_budget_consumed") is not False):
        raise TaskWorkspaceError("controller pre-dispatch receipt identity mismatch")
    worktree = Path(record["worktree"])
    if (git(worktree, "rev-parse", "HEAD") != attempt.get("before_sha")
            or git(worktree, "status", "--porcelain=v1", "--untracked-files=all")):
        raise TaskWorkspaceError("pre-dispatch recovery requires the exact clean unchanged worktree")
    managed = path.parent / "managed-agent"
    if any((managed / name).exists() for name in
           ("launch.json", "receipt.json", "terminal.json", "active.json")):
        raise TaskWorkspaceError("pre-dispatch recovery found ambiguous provider launch evidence")
    references = receipt.get("admission_receipts")
    if not isinstance(references, list) or len(references) != 3:
        raise TaskWorkspaceError("pre-dispatch admission receipt binding is malformed")
    for index, reference in enumerate(references):
        _verify_receipt_reference(reference, f"pre-dispatch admission receipt {index + 1}")
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(data).hexdigest()}


def recover_task_predispatch(controller: Path, task_id: str, run_id: str) -> dict[str, Any]:
    """Release only the exact no-provider budget consumed by the historical defect."""
    if not TASK_RE.fullmatch(task_id) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", run_id):
        raise TaskWorkspaceError("unsafe task/run identity")
    root = controller / TASK_RUN_ROOT / task_id
    with lifecycle_runtime.lifecycle_claim(root / ".claim.lock"):
        run_dir = root / run_id; journal_path = run_dir / "journal.json"
        try:
            journal = json.loads(journal_path.read_text())
            latest = json.loads((root / "latest.json").read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise TaskWorkspaceError("pre-dispatch recovery run is unavailable") from exc
        if (latest.get("run_id") != run_id or journal.get("run_id") != run_id
                or journal.get("task_id") != task_id or journal.get("terminal") is not False):
            raise TaskWorkspaceError("pre-dispatch recovery requires the exact active run")
        with state_lock(controller):
            record = read_state(controller)["tasks"].get(task_id)
        if not isinstance(record, dict) or record.get("state") != "WORKING":
            raise TaskWorkspaceError("pre-dispatch recovery requires an exact WORKING task")
        workers = journal.get("workers")
        if not isinstance(workers, list) or len(workers) != 1:
            raise TaskWorkspaceError("pre-dispatch recovery requires one exact worker attempt")
        attempt = workers[0]
        existing = attempt.get("predispatch_receipt")
        if attempt.get("terminal_state") == "controller_pre_dispatch":
            value = _verify_receipt_reference(existing, "pre-dispatch recovery receipt")
            expected_path = Path(attempt["attempt_dir"]) / "controller-predispatch-receipt.json"
            if (Path(str(existing["path"])).resolve() != expected_path.resolve()
                    or value.get("schema_version") != TASK_PREDISPATCH_RECOVERY_SCHEMA
                    or value.get("task_id") != task_id
                    or value.get("provider_launch_observed") is not False
                    or value.get("model_budget_consumed") is not False):
                raise TaskWorkspaceError("pre-dispatch recovery receipt identity drifted")
            return {"schema_version": TASK_PREDISPATCH_RECOVERY_SCHEMA,
                    "task_id": task_id, "run_id": run_id, "outcome": "already_recovered",
                    "receipt": existing, "next_command": f"yy task run {task_id}"}
        receipt = _pending_predispatch_receipt(record, attempt)
        if receipt is None:
            # Compatibility seam for only the two preserved defect runs. Their
            # historical bytes remain untouched; this command appends evidence.
            exact_runs = {
                "Vhc90c": "1787888756340564000-bcc587c7f20f6f21",
                "0IttWR": "1787889795351197000-f1998918bd8a759c",
            }
            if exact_runs.get(task_id) != run_id:
                raise TaskWorkspaceError("run has no receipt-bound controller pre-dispatch classification")
            errors = [(event.get("boundary"), event.get("phase"), event.get("detail", {}).get("error"))
                      for event in journal.get("events", []) if event.get("boundary") == "ERROR"]
            if (not errors or errors[0] != ("ERROR", "task-run", "managed task worker has no immutable receipt")
                    or any(error not in {"managed task worker has no immutable receipt",
                        "implementation child was interrupted without terminal receipt; model budget is consumed"}
                           for _boundary, _phase, error in errors)
                    or attempt.get("kind") != "implementation" or attempt.get("index") != 1
                    or journal.get("attempts") != {"implementation": 1, "decision_resumes": 0,
                        "attributable_test_repair": 0, "worker_launches": 1}):
                raise TaskWorkspaceError("historical pre-dispatch journal shape drifted")
            worktree = Path(record["worktree"]); before = attempt.get("before_sha")
            create_path = Path(attempt["attempt_dir"]) / "create-receipt.json"
            verify_path = create_path.with_name("verify-receipt.json")
            edit_path = create_path.with_name("edit-preflight-receipt.json")
            values = []
            refs = []
            for path in (create_path, verify_path, edit_path):
                data = path.read_bytes(); value = json.loads(data)
                if data != lifecycle_runtime.canonical_bytes(value):
                    raise TaskWorkspaceError("historical admission receipt bytes drifted")
                values.append(value); refs.append({"path": str(path.resolve()),
                    "sha256": hashlib.sha256(data).hexdigest()})
            create, verify, edit = values
            common = str(Path(git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve())
            admitted, _generated, kind = effective_admission(record)
            if (kind != "historical_creation" or create.get("admission_kind") != kind
                    or create.get("task_id") != task_id or Path(create.get("worktree", "")).resolve() != worktree.resolve()
                    or create.get("branch_ref") != record.get("branch_ref")
                    or create.get("git_common_dir") != common
                    or create.get("expected_paths") != admitted
                    or create.get("workspace_manifest_identity") != record["creation_receipt"].get("manifest_identity")
                    or verify.get("schema_version") != "juno_managed_task_run_verify.v1"
                    or verify.get("task_id") != task_id or verify.get("passed") is not True
                    or verify.get("tip_sha") != before
                    or verify.get("hydration_manifest_sha256") != record.get("hydration", {}).get("manifest_sha256")
                    or edit != {"schema_version": "juno_managed_task_run_edit_preflight.v1",
                                "task_id": task_id, "passed": True,
                                "allowed_paths_sha256": stable_sha256(admitted)}
                    or git(worktree, "config", "--worktree", "--get", "juno.workspace.role", check=False) != "task"
                    or git(worktree, "config", "--worktree", "--get", "juno.workspace.taskId", check=False) != task_id
                    or git(worktree, "symbolic-ref", "-q", "HEAD") != record.get("branch_ref")
                    or git(worktree, "rev-parse", "HEAD") != before
                    or git(worktree, "status", "--porcelain=v1", "--untracked-files=all")):
                raise TaskWorkspaceError("historical pre-dispatch task/receipt/worktree identity drifted")
            managed = Path(attempt["attempt_dir"]) / "managed-agent"
            if any((managed / name).exists() for name in
                   ("launch.json", "receipt.json", "terminal.json", "active.json")):
                raise TaskWorkspaceError("historical pre-dispatch run has ambiguous launch evidence")
            payload = {"schema_version": TASK_PREDISPATCH_RECOVERY_SCHEMA,
                       "classification": "controller_pre_dispatch", "legacy_shape": "historical_creation_refusal_v1",
                       "task_id": task_id, "run_id": run_id, "before_sha": before,
                       "source_journal_sha256": hashlib.sha256(journal_path.read_bytes()).hexdigest(),
                       "provider_launch_observed": False, "model_budget_consumed": False,
                       "admission_receipts": refs, "safe_next_action": f"yy task run {task_id}"}
            receipt = lifecycle_runtime.atomic_json(
                Path(attempt["attempt_dir"]) / "controller-predispatch-receipt.json",
                payload, exclusive=True)
        _release_predispatch_budget(journal, attempt, receipt)
        lifecycle_runtime.lifecycle_checkpoint(
            journal_path, journal, phase=f"implementation-{attempt['index']}", boundary="RECOVERED",
            detail={"classification": "controller_pre_dispatch", "receipt": receipt,
                    "model_budget_consumed": False})
        return {"schema_version": TASK_PREDISPATCH_RECOVERY_SCHEMA,
                "task_id": task_id, "run_id": run_id, "outcome": "recovered",
                "receipt": receipt, "next_command": f"yy task run {task_id}"}


def _wall_recovery_runtime_identity(controller: Path, plan: dict[str, Any],
                                    journal: dict[str, Any]) -> dict[str, Any]:
    """Prove the frozen policy is still executed by the integrated target runtime."""
    try:
        current = lifecycle_runtime.compile_lifecycle_template(
            controller, "task-run", plan["task_id"], model_identity=plan.get("model_identity"))
    except lifecycle_runtime.LifecycleContractError as exc:
        raise TaskWorkspaceError("wall-budget recovery runtime/policy identity drifted") from exc
    if (_task_plan_execution_identity(current) != journal.get("execution_identity_sha256")
            or current.get("controller_commit") != plan.get("controller_commit")
            or current.get("template") != plan.get("template")
            or current.get("budgets") != plan.get("budgets")
            or current.get("runtime_sha256") != plan.get("runtime_sha256")):
        raise TaskWorkspaceError("wall-budget recovery runtime/policy identity drifted")
    config = load_config(controller)
    repository = product_repository(controller, config)
    target_sha = ref_sha(repository, config["target_ref"])
    generation = require_current_runtime(repository, target_sha, controller)
    running_sha = hashlib.sha256(Path(__file__).resolve().read_bytes()).hexdigest()
    if generation.get("running_sha256") != running_sha:
        raise TaskWorkspaceError("wall-budget recovery integrated runtime identity drifted")
    policy_path = controller / ".juno_task/config/task-workspace.json"
    policy_relative = ".juno_task/config/task-workspace.json"
    policy_check = subprocess.run(
        ["git", "-C", str(controller), "diff", "--quiet", "HEAD", "--", policy_relative],
        cwd=controller, stdin=subprocess.DEVNULL, capture_output=True, check=False)
    if (policy_check.returncode != 0
            or git(controller, "ls-files", "--error-unmatch", policy_relative,
                   check=False) != policy_relative):
        raise TaskWorkspaceError("wall-budget recovery runtime/policy identity drifted")
    return {
        "controller_commit": plan["controller_commit"],
        "execution_identity_sha256": journal["execution_identity_sha256"],
        "lifecycle_runtime_sha256": plan["runtime_sha256"],
        "task_runtime_sha256": running_sha,
        "task_workspace_policy_sha256": hashlib.sha256(policy_path.read_bytes()).hexdigest(),
        "target_sha": target_sha,
        "template": plan["template"],
    }


def _verified_wall_predispatch(record: dict[str, Any], journal: dict[str, Any],
                               attempt: dict[str, Any], task_id: str, run_id: str,
                               attempt_index: int,
                               receipt_sha256: str) -> tuple[dict[str, str], dict[str, Any]]:
    if (len(journal.get("workers", [])) != 1 or attempt.get("kind") != "implementation"
            or attempt.get("index") != attempt_index or attempt_index != 1
            or attempt.get("terminal_state") != "controller_pre_dispatch"
            or attempt.get("model_budget_consumed") is not False
            or journal.get("attempts") != {"implementation": 0, "decision_resumes": 0,
                "attributable_test_repair": 0, "worker_launches": 1}):
        raise TaskWorkspaceError("wall-budget recovery requires one restored unused model attempt")
    reference = attempt.get("predispatch_receipt")
    value = _verify_receipt_reference(reference, "wall-budget pre-dispatch receipt")
    attempt_dir = Path(str(attempt.get("attempt_dir", ""))).resolve()
    expected_path = attempt_dir / "controller-predispatch-receipt.json"
    if (reference.get("sha256") != receipt_sha256
            or Path(str(reference.get("path", ""))).resolve() != expected_path
            or value.get("schema_version") != TASK_PREDISPATCH_RECOVERY_SCHEMA
            or value.get("classification") != "controller_pre_dispatch"
            or value.get("task_id") != task_id or value.get("run_id") != run_id
            or value.get("before_sha") != attempt.get("before_sha")
            or value.get("provider_launch_observed") is not False
            or value.get("model_budget_consumed") is not False):
        raise TaskWorkspaceError("wall-budget pre-dispatch receipt identity mismatch")
    raw = expected_path.read_bytes()
    if raw != lifecycle_runtime.canonical_bytes(value):
        raise TaskWorkspaceError("wall-budget pre-dispatch receipt is non-canonical")
    managed = attempt_dir / "managed-agent"
    if any((managed / name).exists() for name in
           ("launch.json", "receipt.json", "terminal.json", "active.json")):
        raise TaskWorkspaceError("wall-budget recovery found provider or managed-agent evidence")
    worktree = Path(record["worktree"])
    if (git(worktree, "rev-parse", "HEAD") != attempt.get("before_sha")
            or record.get("tip_sha") != attempt.get("before_sha")
            or git(worktree, "status", "--porcelain=v1", "--untracked-files=all")):
        raise TaskWorkspaceError("wall-budget recovery requires zero commits and dirty bytes")
    references = value.get("admission_receipts")
    names = ("create-receipt.json", "verify-receipt.json", "edit-preflight-receipt.json")
    if not isinstance(references, list) or len(references) != len(names):
        raise TaskWorkspaceError("wall-budget pre-dispatch admission evidence is malformed")
    admission = []
    for index, (reference_value, name) in enumerate(zip(references, names), 1):
        if Path(str(reference_value.get("path", ""))).resolve() != attempt_dir / name:
            raise TaskWorkspaceError("wall-budget pre-dispatch admission receipt path drifted")
        admission.append(_verify_receipt_reference(
            reference_value, f"wall-budget admission receipt {index}"))
    create, verify, edit = admission
    admitted, _generated, admission_kind = effective_admission(record)
    common = str(Path(git(worktree, "rev-parse", "--path-format=absolute",
                          "--git-common-dir")).resolve())
    if (create.get("task_id") != task_id
            or Path(str(create.get("worktree", ""))).resolve() != worktree.resolve()
            or create.get("branch_ref") != record.get("branch_ref")
            or create.get("git_common_dir") != common
            or create.get("expected_paths") != admitted
            or create.get("admission_kind") != admission_kind
            or verify.get("task_id") != task_id or verify.get("passed") is not True
            or verify.get("tip_sha") != attempt.get("before_sha")
            or edit.get("task_id") != task_id or edit.get("passed") is not True
            or edit.get("allowed_paths_sha256") != stable_sha256(admitted)):
        raise TaskWorkspaceError("wall-budget task/admission identity drifted")
    return reference, value


def _wall_timeout_failure(journal: dict[str, Any], attempt: dict[str, Any],
                          receipt: dict[str, str]) -> tuple[dict[str, Any], int]:
    events = journal.get("events")
    if not isinstance(events, list) or len(events) < 3:
        raise TaskWorkspaceError("wall-budget recovery evidence is malformed or interrupted")
    phase = f"implementation-{attempt['index']}"
    pre = [event for event in events
           if event.get("phase") == phase and event.get("boundary") == "PRE"]
    recovered = [event for event in events
                 if event.get("phase") == phase and event.get("boundary") == "RECOVERED"
                 and event.get("detail", {}).get("classification") == "controller_pre_dispatch"]
    if len(recovered) != 1:
        raise TaskWorkspaceError("wall-budget recovery failure identity is not exact")
    recovered_index = events.index(recovered[0])
    trailing = events[recovered_index + 1:]
    timeout = trailing[0] if trailing else {}
    if (len(pre) != 1 or len(trailing) not in {1, 2}
            or (len(trailing) == 2 and not
                (trailing[1].get("phase") == "wall-budget-recovery"
                 and trailing[1].get("boundary") == "RECOVERED"))
            or recovered[0].get("detail", {}).get("receipt") != receipt
            or recovered[0].get("detail", {}).get("model_budget_consumed") is not False
            or timeout.get("phase") != "task-run" or timeout.get("boundary") != "ERROR"
            or timeout.get("detail") != {"error_type": "LifecycleContractError",
                                         "error": "lifecycle cumulative wall budget exhausted"}):
        raise TaskWorkspaceError("wall-budget recovery failure identity is not exact")
    pre_ns = pre[0].get("recorded_at_unix_ns")
    if not isinstance(pre_ns, int):
        raise TaskWorkspaceError("wall-budget recovery pre-dispatch interval is malformed")
    return timeout, pre_ns


def _verify_existing_wall_recovery(run_dir: Path, reference: Any,
                                   expected: dict[str, Any]) -> dict[str, Any]:
    value = _verify_receipt_reference(reference, "wall-budget recovery receipt")
    path = Path(str(reference["path"])).resolve()
    if (path.parent != (run_dir / "wall-budget-recoveries").resolve()
            or path.name != f"{reference['sha256']}.json"
            or value.get("schema_version") != TASK_WALL_BUDGET_RECOVERY_SCHEMA):
        raise TaskWorkspaceError("wall-budget recovery receipt location/identity drifted")
    for key, expected_value in expected.items():
        if value.get(key) != expected_value:
            raise TaskWorkspaceError(f"wall-budget recovery {key} identity drifted")
    recovered_at = value.get("recovered_at_unix_ns")
    recovered_deadline = value.get("recovered_deadline_unix_ns")
    remaining = value.get("remaining_budget_ns")
    forgiven = value.get("forgiven_interval_ns")
    original = value.get("original_deadline_unix_ns")
    frozen_seconds = value.get("frozen_total_wall_seconds")
    consumed = value.get("consumed_before_predispatch_ns")
    integers = (recovered_at, recovered_deadline, remaining, forgiven,
                original, frozen_seconds, consumed)
    if not all(isinstance(item, int) and not isinstance(item, bool) for item in integers):
        raise TaskWorkspaceError("wall-budget recovery deadline evidence is malformed")
    predispatch_started = recovered_at - forgiven
    expected_predispatch_started = original - frozen_seconds * 1_000_000_000 + consumed
    if (value.get("classification") != "controller_predispatch_wall_interval"
            or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("source_journal_sha256", "")))
            or remaining <= 0 or forgiven < 0
            or predispatch_started != expected_predispatch_started
            or recovered_deadline != recovered_at + remaining):
        raise TaskWorkspaceError("wall-budget recovery deadline evidence is malformed")
    return value


def recover_task_wall_budget(controller: Path, task_id: str, run_id: str,
                             attempt_index: int, predispatch_receipt_sha256: str,
                             original_deadline_unix_ns: int) -> dict[str, Any]:
    """Forgive once only the receipt-proven controller pre-dispatch interval."""
    if (not TASK_RE.fullmatch(task_id)
            or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", run_id)
            or attempt_index != 1
            or not re.fullmatch(r"[0-9a-f]{64}", predispatch_receipt_sha256)
            or original_deadline_unix_ns <= 0):
        raise TaskWorkspaceError("unsafe wall-budget recovery identity")
    root = controller / TASK_RUN_ROOT / task_id
    run_dir = root / run_id
    journal_path = run_dir / "journal.json"
    if not journal_path.is_file() or not (root / "latest.json").is_file():
        raise TaskWorkspaceError("wall-budget recovery run is unavailable or malformed")
    with lifecycle_runtime.lifecycle_claim(root / ".claim.lock"):
        try:
            journal_bytes = journal_path.read_bytes()
            journal = json.loads(journal_bytes)
            latest = json.loads((root / "latest.json").read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise TaskWorkspaceError("wall-budget recovery run is unavailable or malformed") from exc
        if (journal_bytes != lifecycle_runtime.canonical_bytes(journal)
                or journal.get("schema_version") != "juno_managed_task_run_journal.v2"
                or journal.get("task_id") != task_id or journal.get("run_id") != run_id
                or latest.get("run_id") != run_id or latest.get("terminal") is not False
                or journal.get("terminal") is not False):
            raise TaskWorkspaceError("wall-budget recovery requires the exact active run")
        with state_lock(controller):
            record = read_state(controller)["tasks"].get(task_id)
        if not isinstance(record, dict) or record.get("state") != "WORKING":
            raise TaskWorkspaceError("wall-budget recovery requires the exact WORKING task")
        plan = _verify_receipt_reference(journal.get("compiled_plan"), "wall-budget compiled plan")
        if (Path(str(journal["compiled_plan"]["path"])).resolve()
                != (run_dir / "compiled-plan.json").resolve()
                or plan.get("schema_version") != "juno_compiled_lifecycle_plan.v1"
                or plan.get("kind") != "task-run" or plan.get("task_id") != task_id):
            raise TaskWorkspaceError("wall-budget frozen plan identity drifted")
        budget_seconds = plan.get("budgets", {}).get("total_wall_seconds")
        started_ns = journal.get("started_at_unix_ns")
        if (not isinstance(budget_seconds, int) or budget_seconds <= 0
                or not isinstance(started_ns, int)
                or journal.get("deadline_unix_ns") != original_deadline_unix_ns
                or original_deadline_unix_ns != started_ns + budget_seconds * 1_000_000_000):
            raise TaskWorkspaceError("wall-budget original deadline identity drifted")
        if time.time_ns() <= original_deadline_unix_ns:
            raise TaskWorkspaceError("wall-budget recovery requires the exact expired original deadline")
        runtime_identity = _wall_recovery_runtime_identity(controller, plan, journal)
        workers = journal.get("workers")
        if not isinstance(workers, list) or not workers:
            raise TaskWorkspaceError("wall-budget recovery worker evidence is malformed")
        attempt = workers[0]
        predispatch_ref, _predispatch = _verified_wall_predispatch(
            record, journal, attempt, task_id, run_id, attempt_index,
            predispatch_receipt_sha256)
        timeout_event, predispatch_started_ns = _wall_timeout_failure(
            journal, attempt, predispatch_ref)
        consumed_before_ns = predispatch_started_ns - started_ns
        total_budget_ns = budget_seconds * 1_000_000_000
        remaining_ns = total_budget_ns - consumed_before_ns
        if consumed_before_ns < 0 or remaining_ns <= 0 or remaining_ns > total_budget_ns:
            raise TaskWorkspaceError("wall-budget attributable interval is invalid")
        expected = {
            "task_id": task_id, "run_id": run_id, "attempt_index": attempt_index,
            "predispatch_receipt": predispatch_ref,
            "original_deadline_unix_ns": original_deadline_unix_ns,
            "frozen_total_wall_seconds": budget_seconds,
            "consumed_before_predispatch_ns": consumed_before_ns,
            "remaining_budget_ns": remaining_ns,
            "failure_event_sha256": lifecycle_runtime.digest(timeout_event),
            "compiled_plan": journal["compiled_plan"],
            "integrated_identity": runtime_identity,
        }
        existing_pointer = journal.get("wall_budget_recovery")
        recovery_dir = run_dir / "wall-budget-recoveries"
        candidates = sorted(recovery_dir.glob("*.json")) if recovery_dir.is_dir() else []
        if existing_pointer is not None:
            value = _verify_existing_wall_recovery(run_dir, existing_pointer, expected)
            if (len(candidates) != 1
                    or candidates[0].resolve() != Path(str(existing_pointer["path"])).resolve()):
                raise TaskWorkspaceError("wall-budget recovery has duplicate or chained receipts")
            events = [event for event in journal.get("events", [])
                      if event.get("phase") == "wall-budget-recovery"]
            if (len(events) != 1 or events[0].get("boundary") != "RECOVERED"
                    or events[0].get("detail", {}).get("receipt") != existing_pointer
                    or events[0].get("detail", {}).get("recovered_deadline_unix_ns")
                    != value["recovered_deadline_unix_ns"]):
                raise TaskWorkspaceError("wall-budget recovery journal disposition drifted")
            return {"schema_version": TASK_WALL_BUDGET_RECOVERY_SCHEMA,
                    "task_id": task_id, "run_id": run_id, "attempt_index": attempt_index,
                    "outcome": "already_recovered", "receipt": existing_pointer,
                    "recovered_deadline_unix_ns": value["recovered_deadline_unix_ns"],
                    "next_command": f"yy task run {task_id}"}
        if any(event.get("phase") == "wall-budget-recovery"
               for event in journal.get("events", [])):
            raise TaskWorkspaceError("wall-budget recovery has a chained or malformed disposition")
        if len(candidates) > 1:
            raise TaskWorkspaceError("wall-budget recovery has duplicate interrupted receipts")
        if candidates:
            candidate = candidates[0]
            reference = {"path": str(candidate.resolve()),
                         "sha256": hashlib.sha256(candidate.read_bytes()).hexdigest()}
            value = _verify_existing_wall_recovery(run_dir, reference, expected)
            if (candidate.name != f"{reference['sha256']}.json"
                    or value.get("source_journal_sha256") != hashlib.sha256(journal_bytes).hexdigest()):
                raise TaskWorkspaceError("interrupted wall-budget recovery evidence drifted")
        else:
            recovered_at_ns = time.time_ns()
            payload = {"schema_version": TASK_WALL_BUDGET_RECOVERY_SCHEMA, **expected,
                       "classification": "controller_predispatch_wall_interval",
                       "recovered_at_unix_ns": recovered_at_ns,
                       "recovered_deadline_unix_ns": recovered_at_ns + remaining_ns,
                       "forgiven_interval_ns": recovered_at_ns - predispatch_started_ns,
                       "source_journal_sha256": hashlib.sha256(journal_bytes).hexdigest()}
            payload_bytes = lifecycle_runtime.canonical_bytes(payload)
            digest = hashlib.sha256(payload_bytes).hexdigest()
            reference = lifecycle_runtime.atomic_json(
                recovery_dir / f"{digest}.json", payload, exclusive=True)
            value = payload
        journal["wall_budget_recovery"] = reference
        lifecycle_runtime.lifecycle_checkpoint(
            journal_path, journal, phase="wall-budget-recovery", boundary="RECOVERED",
            detail={"classification": "controller_predispatch_wall_interval",
                    "receipt": reference,
                    "original_deadline_unix_ns": original_deadline_unix_ns,
                    "recovered_deadline_unix_ns": value["recovered_deadline_unix_ns"]})
        return {"schema_version": TASK_WALL_BUDGET_RECOVERY_SCHEMA,
                "task_id": task_id, "run_id": run_id, "attempt_index": attempt_index,
                "outcome": "recovered", "receipt": reference,
                "recovered_deadline_unix_ns": value["recovered_deadline_unix_ns"],
                "next_command": f"yy task run {task_id}"}


def _task_run_remaining_seconds(journal: dict[str, Any]) -> int:
    reference = journal.get("wall_budget_recovery")
    if reference is None:
        return lifecycle_runtime.lifecycle_remaining_seconds(journal)
    value = _verify_receipt_reference(reference, "task-run wall-budget recovery receipt")
    if (value.get("schema_version") != TASK_WALL_BUDGET_RECOVERY_SCHEMA
            or value.get("task_id") != journal.get("task_id")
            or value.get("run_id") != journal.get("run_id")
            or value.get("original_deadline_unix_ns") != journal.get("deadline_unix_ns")
            or len([event for event in journal.get("events", [])
                    if event.get("phase") == "wall-budget-recovery"
                    and event.get("boundary") == "RECOVERED"
                    and event.get("detail", {}).get("receipt") == reference]) != 1):
        raise TaskWorkspaceError("task-run wall-budget recovery authority is malformed")
    deadline = value.get("recovered_deadline_unix_ns")
    if not isinstance(deadline, int):
        raise TaskWorkspaceError("task-run recovered deadline is malformed")
    remaining_ns = deadline - time.time_ns()
    if remaining_ns <= 0:
        raise lifecycle_runtime.LifecycleContractError("lifecycle cumulative wall budget exhausted")
    return max(1, remaining_ns // 1_000_000_000)


def managed_task_run(controller: Path, task_id: str) -> dict[str, Any]:
    """Resume one durably claimed controller-owned typed task workflow."""
    if not TASK_RE.fullmatch(task_id):
        raise TaskWorkspaceError("unsafe task id")
    root = controller / TASK_RUN_ROOT / task_id
    latest = root / "latest.json"
    lock_path = root / ".claim.lock"
    with lifecycle_runtime.lifecycle_claim(lock_path):
        task_path, task_bytes = task_manifest(controller, task_id)
        run_token = _ensure_run_fence(controller, task_id)
        current_plan = lifecycle_runtime.compile_lifecycle_template(
            controller, "task-run", task_id, model_identity=os.environ.get("JUNO_MODEL"))
        execution_identity = _task_plan_execution_identity(current_plan)
        root.mkdir(parents=True, exist_ok=True)
        if latest.is_file():
            try:
                latest_value = json.loads(latest.read_text())
                run_id = latest_value["run_id"]
                run_dir = root / run_id
                journal_path = run_dir / "journal.json"
                journal = json.loads(journal_path.read_text())
                plan = json.loads((run_dir / "compiled-plan.json").read_text())
            except (OSError, KeyError, json.JSONDecodeError) as exc:
                raise TaskWorkspaceError("durable task-run claim is malformed") from exc
            if (journal.get("schema_version") != "juno_managed_task_run_journal.v2"
                    or journal.get("run_id") != run_id
                    or journal.get("execution_identity_sha256") != execution_identity):
                raise TaskWorkspaceError(
                    "active task-run identity is incompatible; explicit reviewed replacement required")
            projection_path = Path(str(latest_value.get("projection_path", "")))
            if journal.get("terminal"):
                # The journal is the authority: a crash between the terminal
                # journal write and latest-pointer publication leaves the
                # pointer stale, so derive the authoritative projection from
                # the journal and repair the pointer before returning.
                # The journal's final reference is the sole authority: a
                # missing or malformed final artifact fails closed instead of
                # promoting whatever the pointer happens to reference.
                journal_refs = [ref for ref in journal.get("projections", [])
                                if isinstance(ref, dict) and ref.get("path")]
                if not journal_refs:
                    raise TaskWorkspaceError(
                        "terminal task-run journal has no final projection reference")
                final_ref = journal_refs[-1]
                candidate = Path(str(final_ref["path"]))
                if not candidate.is_file():
                    raise TaskWorkspaceError(
                        "terminal task-run journal projection artifact is missing")
                projection_value = lifecycle_runtime.verified_projection_bytes(
                    candidate, expected_sha256=final_ref.get("sha256"),
                    kind="task-run", run_id=journal.get("run_id"))
                if projection_value.get("state") not in {"QUEUED", "BLOCKED"}:
                    raise TaskWorkspaceError(
                        "terminal task-run journal projection is not terminal")
                authoritative = (candidate, projection_value)
                if authoritative is not None:
                    path, projection_value = authoritative
                    # The expected canonical summary derives from the verified
                    # projection; existing bytes are verified, not trusted.
                    expected_summary = lifecycle_runtime.deterministic_summary(
                        projection_value)
                    expected_summary_bytes = lifecycle_runtime.canonical_bytes(
                        expected_summary)
                    summary_path = run_dir / "summary.json"
                    if (not summary_path.is_file()
                            or summary_path.read_bytes() != expected_summary_bytes):
                        summary_ref = lifecycle_runtime.atomic_json(
                            summary_path, expected_summary)
                    else:
                        summary_ref = {"path": str(summary_path.resolve()),
                                       "sha256": hashlib.sha256(
                                           expected_summary_bytes).hexdigest()}
                    pointer_repaired = {
                        "schema_version": "juno_managed_task_run_latest.v2",
                        "run_id": journal["run_id"],
                        "compiled_plan_sha256": plan["compiled_plan_sha256"],
                        "execution_identity_sha256": journal["execution_identity_sha256"],
                        "task_revision_sha256":
                            (projection_value.get("identities") or {}).get(
                                "task_revision_sha256"),
                        "terminal": True, "projection_path": str(path.resolve()),
                        "summary": summary_ref}
                    if latest_value != pointer_repaired:
                        lifecycle_runtime.atomic_json(latest, pointer_repaired)
                    return projection_value
        else:
            run_id = f"{time.time_ns()}-{secrets.token_hex(8)}"
            run_dir = root / run_id
            run_dir.mkdir(parents=True, exist_ok=False)
            plan = current_plan
            plan_ref = lifecycle_runtime.atomic_json(
                run_dir / "compiled-plan.json", plan, exclusive=True)
            frozen_prompts = []
            for index, prompt in enumerate(plan["prompts"], 1):
                source = controller / prompt["path"]
                target = run_dir / "frozen-prompts" / f"{index}.md"
                target.parent.mkdir(parents=True, exist_ok=True)
                payload = source.read_bytes()
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(payload); stream.flush(); os.fsync(stream.fileno())
                frozen_prompts.append({"path": str(target.resolve()),
                                       "sha256": hashlib.sha256(payload).hexdigest()})
            started_ns = time.time_ns()
            journal = {
                "schema_version": "juno_managed_task_run_journal.v2", "run_id": run_id,
                "task_id": task_id, "execution_identity_sha256": execution_identity,
                "compiled_plan": plan_ref, "frozen_prompts": frozen_prompts,
                "started_at_unix_ns": started_ns,
                "deadline_unix_ns": started_ns + int(plan["budgets"]["total_wall_seconds"]) * 1_000_000_000,
                "attempts": {"implementation": 0, "decision_resumes": 0,
                             "attributable_test_repair": 0, "worker_launches": 0},
                "workers": [], "events": [], "projections": [], "state": "CLAIMED",
                "terminal": False, "task_revisions": []}
            journal_path = run_dir / "journal.json"
            lifecycle_runtime.lifecycle_journal_write(journal_path, journal)
            # Publish latest while the claim lock is held and before any product/Kanban side effect.
            lifecycle_runtime.atomic_json(latest, {
                "schema_version": "juno_managed_task_run_latest.v2", "run_id": run_id,
                "compiled_plan_sha256": plan["compiled_plan_sha256"],
                "execution_identity_sha256": execution_identity,
                "task_revision_sha256": hashlib.sha256(task_bytes).hexdigest(),
                "terminal": False, "projection_path": None, "summary": None})
            lifecycle_runtime.lifecycle_checkpoint(
                journal_path, journal, phase="claim", boundary="POST",
                detail={"task_revision_sha256": hashlib.sha256(task_bytes).hexdigest()})
        try:
            _task_run_remaining_seconds(journal)
            # Recover orchestration boundaries whose side effect became durable
            # before their POST checkpoint (for example process death after queueing).
            for recoverable_phase in ("admit", "finish-initial", "finish-after-repair"):
                matching = [event for event in journal["events"]
                            if event.get("phase") == recoverable_phase]
                if matching and matching[-1].get("boundary") == "PRE":
                    with state_lock(controller):
                        observed = read_state(controller)["tasks"].get(task_id, {})
                    observed_state = observed.get("state") if isinstance(observed, dict) else None
                    if ((recoverable_phase == "admit" and observed_state == "WORKING")
                            or (recoverable_phase.startswith("finish")
                                and observed_state == "QUEUED")):
                        lifecycle_runtime.lifecycle_checkpoint(
                            journal_path, journal, phase=recoverable_phase,
                            boundary="RECOVERED", detail={"state": observed_state})
            task_revision = hashlib.sha256(task_bytes).hexdigest()
            questions = lifecycle_runtime.readiness_questions(task_bytes)
            prior_revision = journal["task_revisions"][-1] if journal["task_revisions"] else None
            if prior_revision != task_revision:
                lifecycle_runtime.lifecycle_checkpoint(
                    journal_path, journal, phase=f"readiness-{len(journal['task_revisions']) + 1}",
                    boundary="PRE", detail={"task_revision_sha256": task_revision})
                journal["task_revisions"].append(task_revision)
                lifecycle_runtime.lifecycle_checkpoint(
                    journal_path, journal, phase=f"readiness-{len(journal['task_revisions'])}",
                    boundary="POST", detail={"task_revision_sha256": task_revision,
                                              "questions": questions})
            if questions:
                decision = {"schema_version": "juno_task_run_decision.v1", "task_id": task_id,
                            "task_revision_sha256": task_revision, "questions": questions,
                            "resume_command": f"yy task run {task_id}"}
                decision_path = run_dir / "decisions" / f"{len(journal['task_revisions']):04d}.json"
                # The receipt is immutable canonical bytes: reuse requires exact
                # byte equality, not semantic JSON equality.
                expected_decision_bytes = lifecycle_runtime.canonical_bytes(decision)
                if decision_path.is_file():
                    if decision_path.read_bytes() != expected_decision_bytes:
                        raise TaskWorkspaceError(
                            "ambiguous task-run decision receipt collision: "
                            f"{decision_path}")
                    decision_ref = {"path": str(decision_path.resolve()),
                                    "sha256": hashlib.sha256(
                                        expected_decision_bytes).hexdigest()}
                else:
                    decision_ref = lifecycle_runtime.atomic_json(
                        decision_path, decision, exclusive=True)
                # The journal reference is verified against the receipt bytes and
                # repaired when a crash left it missing or stale.
                references = [ref for ref in journal.get("decision_receipts", [])
                              if isinstance(ref, dict)]
                known = next((ref for ref in references
                              if ref.get("path") == decision_ref["path"]), None)
                if known is None:
                    journal.setdefault("decision_receipts", []).append(decision_ref)
                elif known.get("sha256") != decision_ref["sha256"]:
                    known["sha256"] = decision_ref["sha256"]
                return _task_projection(
                    controller, task_id, run_dir, latest, journal_path, journal, plan,
                    state_name="NEEDS_DECISION",
                    blocker={"category": "ambiguity", "questions": questions})

            with state_lock(controller):
                state_record = read_state(controller)["tasks"].get(task_id)
            if not isinstance(state_record, dict):
                lifecycle_runtime.lifecycle_checkpoint(
                    journal_path, journal, phase="admit", boundary="PRE")
                start(controller, task_id, lease_token=run_token)
                with state_lock(controller):
                    state_record = read_state(controller)["tasks"][task_id]
                lifecycle_runtime.lifecycle_checkpoint(
                    journal_path, journal, phase="admit", boundary="POST",
                    detail={"base_sha": state_record.get("base_sha")})
            if state_record.get("state") == "QUEUED":
                standing = state_record.get("review_ready_closure", {}).get("standing_validation", {})
                return _task_projection(
                    controller, task_id, run_dir, latest, journal_path, journal, plan,
                    state_name="QUEUED", blocker=None, counters=standing.get("counters"), terminal=True)
            if state_record.get("state") != "WORKING":
                raise TaskWorkspaceError(f"task run cannot implement from {state_record.get('state')}")
            # Gate every first worker launch and every recovery on verified
            # frozen hydration/dependency evidence, with one durable authorized
            # exact-lock rerun when the evidence is stale. No worker receipt,
            # model budget, or product edit is spent before this gate passes.
            hydration_gate = _managed_hydration_gate(controller, state_record,
                                                      lease_token=run_token)
            state_record = hydration_gate["record"]

            completed_worker = next((item for item in journal["workers"]
                                     if item.get("kind") == "implementation"
                                     and item.get("terminal_state") == "completed"), None)
            if completed_worker is None:
                pending = next((item for item in reversed(journal["workers"])
                                if item.get("kind") == "implementation"
                                and item.get("terminal_state") is None), None)
                predispatch = _pending_predispatch_receipt(state_record, pending) if pending else None
                if pending and predispatch is not None:
                    _release_predispatch_budget(journal, pending, predispatch)
                    lifecycle_runtime.lifecycle_checkpoint(
                        journal_path, journal, phase=f"implementation-{pending['index']}",
                        boundary="RECOVERED", detail={"classification": "controller_pre_dispatch",
                            "receipt": predispatch, "model_budget_consumed": False})
                    pending = None
                worker = _recover_task_worker(state_record, pending) if pending else None
                if pending and worker is None:
                    raise TaskWorkspaceError(
                        "implementation child was interrupted without terminal receipt; model budget is consumed")
                blocked = next((item for item in reversed(journal["workers"])
                                if item.get("kind") == "implementation"
                                and item.get("terminal_state") == "blocked"), None)
                if worker is None and blocked and blocked.get("task_revision_sha256") == task_revision:
                    projection_path = Path(json.loads(latest.read_text()).get("projection_path"))
                    return json.loads(projection_path.read_text())
                if worker is None:
                    is_resume = blocked is not None
                    if is_resume and journal["attempts"]["decision_resumes"] >= int(
                            plan["budgets"].get("decision_resumes", 1)):
                        raise TaskWorkspaceError("managed decision-resume budget exhausted")
                    if not is_resume and journal["attempts"]["implementation"] >= int(
                            plan["budgets"]["implementation_attempts"]):
                        raise TaskWorkspaceError("managed implementation budget exhausted")
                    index = journal["attempts"]["worker_launches"] + 1
                    attempt_dir = run_dir / "workers" / f"implementation-{index:04d}"
                    attempt = {"kind": "implementation", "index": index,
                               "attempt_dir": str(attempt_dir.resolve()),
                               "task_revision_sha256": task_revision,
                               "before_sha": git(Path(state_record["worktree"]), "rev-parse", "HEAD"),
                               "terminal_state": None}
                    journal["workers"].append(attempt)
                    journal["attempts"]["worker_launches"] += 1
                    journal["attempts"]["decision_resumes" if is_resume else "implementation"] += 1
                    lifecycle_runtime.lifecycle_checkpoint(
                        journal_path, journal, phase=f"implementation-{index}", boundary="PRE",
                        detail={key: attempt[key] for key in
                                ("attempt_dir", "task_revision_sha256", "before_sha")})
                    try:
                        worker = _launch_task_worker(
                            controller, task_id, state_record, attempt_dir,
                            Path(journal["frozen_prompts"][0]["path"]), repair=False,
                            timeout_seconds=_task_run_remaining_seconds(journal),
                            hydration_gate=hydration_gate)
                    except ManagedAgentPreDispatchError as exc:
                        _release_predispatch_budget(journal, attempt, exc.receipt)
                        lifecycle_runtime.lifecycle_checkpoint(
                            journal_path, journal, phase=f"implementation-{index}", boundary="RECOVERED",
                            detail={"classification": "controller_pre_dispatch",
                                    "receipt": exc.receipt, "model_budget_consumed": False})
                        raise
                target = pending if pending else journal["workers"][-1]
                target.update(worker)
                lifecycle_runtime.lifecycle_checkpoint(
                    journal_path, journal, phase=f"implementation-{target['index']}",
                    boundary="RECOVERED" if worker.get("recovered") else "POST",
                    detail={"terminal_state": worker["terminal_state"],
                            "after_sha": worker["after_sha"], "receipt": worker["receipt"]})
                if worker["terminal_state"] != "completed":
                    state_name = "NEEDS_DECISION" if worker["terminal_state"] == "blocked" else "BLOCKED"
                    return _task_projection(
                        controller, task_id, run_dir, latest, journal_path, journal, plan,
                        state_name=state_name,
                        blocker={"category": "implementation",
                                 "terminal_state": worker["terminal_state"]},
                        terminal=state_name == "BLOCKED")

            initial_error = next((event["detail"].get("error") for event in journal["events"]
                                  if event["phase"] == "finish-initial"
                                  and event["boundary"] == "POST"
                                  and event["detail"].get("outcome") == "attributable_failure"), None)
            if initial_error is None:
                lifecycle_runtime.lifecycle_checkpoint(
                    journal_path, journal, phase="finish-initial", boundary="PRE")
                try:
                    queued = finish(controller, task_id, lease_token=run_token)
                except TaskWorkspaceError as exc:
                    attributable = ("focused validation failed" in str(exc)
                                    or "parsed" in str(exc) or "test" in str(exc).lower())
                    if not attributable:
                        raise
                    initial_error = str(exc)[:32768]
                    lifecycle_runtime.lifecycle_checkpoint(
                        journal_path, journal, phase="finish-initial", boundary="POST",
                        detail={"outcome": "attributable_failure", "error": initial_error})
                else:
                    lifecycle_runtime.lifecycle_checkpoint(
                        journal_path, journal, phase="finish-initial", boundary="POST",
                        detail={"outcome": "queued", "tip_sha": queued.get("tip_sha")})
            if initial_error is not None:
                repair = next((item for item in reversed(journal["workers"])
                               if item.get("kind") == "attributable_test_repair"), None)
                if repair and repair.get("terminal_state") == "controller_pre_dispatch":
                    repair = None
                if repair and not repair.get("terminal_state"):
                    repair_predispatch = _pending_predispatch_receipt(state_record, repair)
                    if repair_predispatch is not None:
                        _release_predispatch_budget(journal, repair, repair_predispatch)
                        lifecycle_runtime.lifecycle_checkpoint(
                            journal_path, journal, phase=f"attributable-repair-{repair['index']}",
                            boundary="RECOVERED", detail={"classification": "controller_pre_dispatch",
                                "receipt": repair_predispatch, "model_budget_consumed": False})
                        repair = None
                repaired = _recover_task_worker(state_record, repair) if repair and not repair.get("terminal_state") else repair
                if repaired is None:
                    if journal["attempts"]["attributable_test_repair"] >= int(
                            plan["budgets"]["attributable_test_repairs"]):
                        raise TaskWorkspaceError("attributable test repair budget exhausted")
                    # The implementation commit may have changed the exact-lock
                    # dependency tree; complete and verify a fresh hydration
                    # gate BEFORE appending the repair attempt, incrementing
                    # worker counters, or recording the launch PRE checkpoint,
                    # so an unrecoverable gate consumes no repair budget and
                    # leaves no pending repair attempt behind.
                    repair_gate = _managed_hydration_gate(controller, state_record,
                                                          lease_token=run_token)
                    state_record = repair_gate["record"]
                    index = journal["attempts"]["worker_launches"] + 1
                    attempt_dir = run_dir / "workers" / f"attributable-repair-{index:04d}"
                    repair = {"kind": "attributable_test_repair", "index": index,
                              "attempt_dir": str(attempt_dir.resolve()),
                              "task_revision_sha256": task_revision,
                              "before_sha": git(Path(state_record["worktree"]), "rev-parse", "HEAD"),
                              "terminal_state": None}
                    journal["workers"].append(repair)
                    journal["attempts"]["worker_launches"] += 1
                    journal["attempts"]["attributable_test_repair"] += 1
                    lifecycle_runtime.lifecycle_checkpoint(
                        journal_path, journal, phase=f"attributable-repair-{index}", boundary="PRE",
                        detail={"attempt_dir": repair["attempt_dir"], "before_sha": repair["before_sha"]})
                    try:
                        repaired = _launch_task_worker(
                            controller, task_id, state_record, attempt_dir,
                            Path(journal["frozen_prompts"][1]["path"]), repair=True,
                            timeout_seconds=_task_run_remaining_seconds(journal),
                            context_bytes=initial_error.encode("utf-8", errors="replace"),
                            hydration_gate=repair_gate)
                    except ManagedAgentPreDispatchError as exc:
                        _release_predispatch_budget(journal, repair, exc.receipt)
                        lifecycle_runtime.lifecycle_checkpoint(
                            journal_path, journal, phase=f"attributable-repair-{index}", boundary="RECOVERED",
                            detail={"classification": "controller_pre_dispatch",
                                    "receipt": exc.receipt, "model_budget_consumed": False})
                        raise
                assert repair is not None
                if isinstance(repaired, dict) and "terminal_state" in repaired:
                    repair.update(repaired)
                lifecycle_runtime.lifecycle_checkpoint(
                    journal_path, journal, phase=f"attributable-repair-{repair['index']}",
                    boundary="RECOVERED" if repaired.get("recovered") else "POST",
                    detail={"terminal_state": repaired.get("terminal_state"),
                            "after_sha": repaired.get("after_sha"), "receipt": repaired.get("receipt")})
                if repaired.get("terminal_state") != "completed":
                    raise TaskWorkspaceError("attributable test repair did not complete")
                lifecycle_runtime.lifecycle_checkpoint(
                    journal_path, journal, phase="finish-after-repair", boundary="PRE")
                queued = finish(controller, task_id, lease_token=run_token)
                lifecycle_runtime.lifecycle_checkpoint(
                    journal_path, journal, phase="finish-after-repair", boundary="POST",
                    detail={"outcome": queued.get("outcome"), "tip_sha": queued.get("tip_sha")})
            with state_lock(controller):
                final_record = read_state(controller)["tasks"][task_id]
            standing = final_record.get("review_ready_closure", {}).get("standing_validation", {})
            return _task_projection(
                controller, task_id, run_dir, latest, journal_path, journal, plan,
                state_name="QUEUED", blocker=None, counters=standing.get("counters"), terminal=True)
        except (TaskWorkspaceError, lifecycle_runtime.LifecycleContractError, OSError) as exc:
            lifecycle_runtime.lifecycle_checkpoint(
                journal_path, journal, phase="task-run", boundary="ERROR",
                detail={"error_type": type(exc).__name__, "error": str(exc)[:1024]})
            raise TaskWorkspaceError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Fenced task/worktree attempts (task rrx4b8, Wave 2 of the sealed
# merge-train PDR). The lease is controller-issued mutation authority over
# one task record. Producers prove identity with the exact bearer token;
# in-process workers additionally pass through live same-pid continuity.
# Expiry alone never grants takeover: successor issuance requires a proven
# dead producer, an explicit handoff receipt, or an operator revoke receipt.
# Every lease transition writes one immutable receipt under the task's
# private runtime directory before the state mutation commits it.
# ---------------------------------------------------------------------------

FENCING_SCHEMA = "juno_task_fencing_lease.v1"
FENCING_RECEIPT_SCHEMA = "juno_task_fencing_receipt.v1"
FENCING_RECEIPT_ROOT = ".juno_task/runtime/leases"
FENCING_HISTORY_LIMIT = 16
FENCING_DIRTY_PATH_LIMIT = 64


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _fencing_root(controller: Path, task_id: str) -> Path:
    return controller / FENCING_RECEIPT_ROOT / task_id[:2].lower() / task_id


def _producer_lstart(pid: int) -> Optional[str]:
    """Bounded process start-time readback; None when unobservable."""
    try:
        result = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                                capture_output=True, text=True,
                                stdin=subprocess.DEVNULL, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _observe_producer(producer: Any) -> decisions.LeaseObservation:
    """Controller-owned liveness readback for one lease producer.

    "dead" requires a positive observation: the pid is gone, or a live pid
    whose start time differs from the recorded anchor (recycled pid). A
    missing anchor or unreadable ps leaves the producer "unknown", which
    never grants takeover.
    """
    if not isinstance(producer, dict) or not isinstance(producer.get("pid"), int) \
            or isinstance(producer.get("pid"), bool):
        return decisions.LeaseObservation("unknown", "producer pid is not recorded")
    pid = producer["pid"]
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return decisions.LeaseObservation("dead", f"pid {pid} no longer exists")
    except PermissionError:
        pass
    except OSError as exc:
        return decisions.LeaseObservation("unknown", f"pid {pid} probe failed: {exc}")
    anchored = producer.get("lstart")
    if not isinstance(anchored, str) or not anchored:
        return decisions.LeaseObservation("unknown", f"pid {pid} is alive without a start-time anchor")
    current = _producer_lstart(pid)
    if current is None:
        return decisions.LeaseObservation("unknown", f"pid {pid} start time is unreadable")
    if current != anchored:
        return decisions.LeaseObservation(
            "dead", f"pid {pid} was recycled (start time changed)")
    return decisions.LeaseObservation("alive", f"pid {pid} anchored at {anchored}")


def _write_fencing_receipt(controller: Path, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Write one immutable lease receipt; the state mutation commits it."""
    directory = _fencing_root(controller, task_id)
    directory.mkdir(parents=True, exist_ok=True)
    name = f"{int(payload['attempt']):04d}-{payload['kind']}-{secrets.token_hex(6)}.json"
    data = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    return {"path": str((directory / name).resolve()),
            "sha256": hashlib.sha256(data).hexdigest()}


def _lease_view(record: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    lease = record.get("fencing") if isinstance(record, dict) else None
    return lease if isinstance(lease, dict) else None


def _token_digest(token: Optional[str]) -> Optional[str]:
    if not isinstance(token, str) or not token:
        return None
    return hashlib.sha256(token.encode()).hexdigest()


def _new_lease(task_id: str, attempt: int, producer_kind: str, authority_kind: str,
               receipt: dict[str, Any], *, reason: Optional[str],
               producer_pid: Optional[int], recovery: Optional[dict[str, Any]] = None) \
        -> tuple[dict[str, Any], str]:
    token = f"{task_id}:{attempt}:{secrets.token_hex(20)}"
    pid = producer_pid if isinstance(producer_pid, int) else os.getpid()
    lease = {
        "schema_version": FENCING_SCHEMA,
        "attempt": attempt,
        "state": decisions.LEASE_ACTIVE,
        "producer_kind": producer_kind,
        "producer": {"pid": pid, "lstart": _producer_lstart(pid),
                     "host": os.uname().nodename, "issued_utc": _utc_now()},
        "token_sha256": _token_digest(token),
        "issued_utc": _utc_now(),
        "heartbeat_utc": None,
        "heartbeat_seq": 0,
        "authority": {"kind": authority_kind, "reason": reason,
                      "receipt_path": receipt["path"],
                      "receipt_sha256": receipt["sha256"]},
    }
    if recovery is not None:
        lease["recovery"] = recovery
    return lease, token


def _history_entry(lease: dict[str, Any], *, state: str, reason: Optional[str]) -> dict[str, Any]:
    return {"attempt": lease.get("attempt"), "state": state,
            "producer_kind": lease.get("producer_kind"),
            "issued_utc": lease.get("issued_utc"), "ended_utc": _utc_now(),
            "reason": reason,
            "authority": lease.get("authority", {}).get("kind")}


def _apply_lease(record: dict[str, Any], lease: dict[str, Any]) -> dict[str, Any]:
    previous = _lease_view(record)
    history = list(record.get("fencing_history") or []) \
        if isinstance(record.get("fencing_history"), list) else []
    if previous is not None:
        history.append(_history_entry(previous, state=lease["state"],
                                      reason=lease["authority"].get("reason")))
    del history[:-FENCING_HISTORY_LIMIT]
    return {**record, "fencing": lease, "fencing_history": history}


_LEASE_RECORD_UNSET = object()


def _require_lease_fence(controller: Path, command: str, task_id: str,
                         lease_token: Optional[str], *,
                         record: Any = _LEASE_RECORD_UNSET) -> Optional[dict[str, Any]]:
    """Fail closed unless this command holds task mutation authority.

    Read-only: gated operations call this inside their existing state lock
    so the authority observation cannot race a concurrent lease transition.
    Heartbeat refresh is an explicit holder command and never a side effect
    of another mutation, so frozen-record comparisons stay stable.
    """
    if record is _LEASE_RECORD_UNSET:
        with state_lock(controller):
            record = read_state(controller)["tasks"].get(task_id)
    lease = _lease_view(record)
    observation = (_observe_producer(lease.get("producer"))
                   if isinstance(lease, dict) else
                   decisions.LeaseObservation("unknown", "no lease"))
    decision = decisions.plan_lease_authority(
        command, task_id, lease, _token_digest(lease_token),
        observation, os.getpid())
    if not decision.admitted:
        raise TaskWorkspaceError(
            f"task {command} refused ({decision.code}): {decision.message}")
    return lease


def _ensure_lease_released(controller: Path, task_id: str, record: dict[str, Any],
                           *, reason: str) -> dict[str, Any]:
    """Terminate an ACTIVE lease terminally (queue/withdrawal boundary)."""
    lease = _lease_view(record)
    if lease is None or lease.get("state") != decisions.LEASE_ACTIVE:
        return record
    receipt_payload = {
        "schema_version": FENCING_RECEIPT_SCHEMA, "kind": "release",
        "task_id": task_id, "attempt": lease.get("attempt"),
        "token_sha256": lease.get("token_sha256"), "reason": reason,
        "recorded_utc": _utc_now(),
    }
    receipt = _write_fencing_receipt(controller, task_id, receipt_payload)
    terminal = {**lease, "state": decisions.LEASE_RELEASED,
                "released_utc": _utc_now(), "release_reason": reason,
                "release_receipt": receipt}
    return _apply_lease(record, terminal)


def lease_status(controller: Path, task_id: str) -> dict[str, Any]:
    """Read-only fencing observation with actionable reason codes."""
    if not TASK_RE.fullmatch(task_id):
        raise TaskWorkspaceError("unsafe task id")
    require_task(controller, task_id)
    with state_lock(controller):
        record = read_state(controller)["tasks"].get(task_id)
    lease = _lease_view(record)
    observation = (_observe_producer(lease.get("producer"))
                   if isinstance(lease, dict) else None)
    authority = decisions.plan_lease_authority(
        "status", task_id, lease, None,
        observation or decisions.LeaseObservation("unknown", "no lease"),
        os.getpid())
    successor = decisions.plan_lease_successor(
        task_id, lease, observation or decisions.LeaseObservation("unknown", "no lease"),
        lease.get("handoff") if isinstance(lease, dict) else None)
    return {
        "schema_version": FENCING_SCHEMA, "task_id": task_id,
        "lease": lease, "history": (record or {}).get("fencing_history") or [],
        "producer_observation": {"status": observation.status, "detail": observation.detail}
        if observation else None,
        "mutation_authority": {"admitted": authority.admitted, "code": authority.code,
                               "message": authority.message},
        "successor_readiness": {"admitted": successor.admitted, "code": successor.code,
                                "authority_kind": successor.authority_kind,
                                "message": successor.message},
    }


def _lease_holder_authority(controller: Path, command: str, task_id: str,
                            lease_token: Optional[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Holder-exclusive authority: only the exact token admits."""
    with state_lock(controller):
        record = read_state(controller)["tasks"].get(task_id)
    if not isinstance(record, dict):
        raise TaskWorkspaceError("task has not been started")
    lease = _lease_view(record)
    if lease is None or lease.get("state") != decisions.LEASE_ACTIVE:
        raise TaskWorkspaceError(
            f"task {command} refused (lease_not_active): "
            f"task {task_id} holds no ACTIVE fencing lease")
    digest = _token_digest(lease_token)
    if digest is None or digest != lease.get("token_sha256"):
        raise TaskWorkspaceError(
            f"task {command} refused (lease_fence_stale): "
            f"present the current --lease-token for attempt {lease.get('attempt')}")
    return record, lease


def lease_heartbeat(controller: Path, task_id: str, lease_token: Optional[str]) -> dict[str, Any]:
    if not TASK_RE.fullmatch(task_id):
        raise TaskWorkspaceError("unsafe task id")
    require_task(controller, task_id)
    record, lease = _lease_holder_authority(controller, "lease-heartbeat", task_id, lease_token)
    with state_lock(controller):
        state = read_state(controller)
        current = state["tasks"].get(task_id)
        if current != record:
            raise TaskWorkspaceError("task state changed during lease heartbeat; retry")
        refreshed = {**lease, "heartbeat_utc": _utc_now(),
                     "heartbeat_seq": int(lease.get("heartbeat_seq") or 0) + 1}
        state["tasks"][task_id] = _apply_lease(current, refreshed)
        write_state(controller, state)
        return {"schema_version": FENCING_SCHEMA, "task_id": task_id,
                "outcome": "heartbeat", "attempt": refreshed["attempt"],
                "heartbeat_seq": refreshed["heartbeat_seq"],
                "heartbeat_utc": refreshed["heartbeat_utc"]}


def lease_handoff(controller: Path, task_id: str, lease_token: Optional[str],
                  reason: Optional[str]) -> dict[str, Any]:
    """Holder releases authority to one explicit successor receipt."""
    if not TASK_RE.fullmatch(task_id):
        raise TaskWorkspaceError("unsafe task id")
    require_task(controller, task_id)
    record, lease = _lease_holder_authority(controller, "lease-handoff", task_id, lease_token)
    receipt_payload = {
        "schema_version": FENCING_RECEIPT_SCHEMA, "kind": "handoff",
        "task_id": task_id, "attempt": lease.get("attempt"),
        "token_sha256": lease.get("token_sha256"), "reason": reason,
        "producer": lease.get("producer"), "recorded_utc": _utc_now(),
    }
    receipt = _write_fencing_receipt(controller, task_id, receipt_payload)
    with state_lock(controller):
        state = read_state(controller)
        current = state["tasks"].get(task_id)
        if current != record:
            raise TaskWorkspaceError("task state changed during lease handoff; retry")
        handed = {**lease, "state": decisions.LEASE_HANDED_OFF,
                 "handed_off_utc": _utc_now(), "handoff": receipt}
        state["tasks"][task_id] = _apply_lease(current, handed)
        write_state(controller, state)
        return {"schema_version": FENCING_SCHEMA, "task_id": task_id,
                "outcome": "handed_off", "attempt": handed["attempt"],
                "handoff_receipt": receipt,
                "next_command": f"yy task lease-successor {task_id} --handoff-receipt {receipt['path']}"}


def lease_revoke(controller: Path, task_id: str, reason: Optional[str]) -> dict[str, Any]:
    """Operator-only explicit termination of task mutation authority."""
    if not TASK_RE.fullmatch(task_id):
        raise TaskWorkspaceError("unsafe task id")
    if not reason or not reason.strip():
        raise TaskWorkspaceError("lease revoke requires --reason (operator decision record)")
    require_task(controller, task_id)
    with state_lock(controller):
        state = read_state(controller)
        record = state["tasks"].get(task_id)
    if not isinstance(record, dict):
        raise TaskWorkspaceError("task has not been started")
    lease = _lease_view(record)
    if lease is None:
        raise TaskWorkspaceError("task holds no fencing lease to revoke")
    if lease.get("state") == decisions.LEASE_RELEASED:
        return {"schema_version": FENCING_SCHEMA, "task_id": task_id,
                "outcome": "already_released", "attempt": lease.get("attempt")}
    receipt_payload = {
        "schema_version": FENCING_RECEIPT_SCHEMA, "kind": "revoke",
        "task_id": task_id, "attempt": lease.get("attempt"),
        "reason": reason, "operator": os.environ.get("USER") or None,
        "recorded_utc": _utc_now(),
    }
    receipt = _write_fencing_receipt(controller, task_id, receipt_payload)
    with state_lock(controller):
        state = read_state(controller)
        current = state["tasks"].get(task_id)
        if current != record:
            raise TaskWorkspaceError("task state changed during lease revoke; retry")
        revoked = {**lease, "state": decisions.LEASE_REVOKED,
                   "revoked_utc": _utc_now(), "revoke_reason": reason,
                   "revoke_receipt": receipt}
        state["tasks"][task_id] = _apply_lease(current, revoked)
        write_state(controller, state)
        return {"schema_version": FENCING_SCHEMA, "task_id": task_id,
                "outcome": "revoked", "attempt": revoked["attempt"],
                "revoke_receipt": receipt,
                "next_command": f"yy task lease-successor {task_id}"}


def _worktree_recovery_classification(record: dict[str, Any]) -> dict[str, Any]:
    """Bounded dirty-bytes inventory for one successor attempt."""
    worktree = record.get("worktree")
    if not isinstance(worktree, str) or not Path(worktree).exists():
        return {"classification": "worktree_absent",
                "next_command": "yy task status " + str(record.get("task_id"))}
    porcelain = git(Path(worktree), "status", "--porcelain=v1",
                    "--untracked-files=all", check=False) or ""
    dirty = [line[3:] for line in porcelain.splitlines() if line.strip()]
    if not dirty:
        return {"classification": "clean_resume",
                "next_command": "yy task status " + str(record.get("task_id"))}
    return {
        "classification": "dirty_recovery_required",
        "dirty_paths": dirty[:FENCING_DIRTY_PATH_LIMIT],
        "dirty_path_count": len(dirty),
        "preservation": "dirty bytes are preserved for one bounded recovery agent",
        "escalate_only": ["semantic ambiguity", "scope or authority expansion",
                          "sensitive action", "unrecoverable state"],
        "next_command": "yy task handoff " + str(record.get("task_id")),
    }


def lease_successor(controller: Path, task_id: str,
                    handoff_receipt_path: Optional[Path] = None) -> dict[str, Any]:
    """Issue the next fencing attempt after proven predecessor termination."""
    if not TASK_RE.fullmatch(task_id):
        raise TaskWorkspaceError("unsafe task id")
    require_task(controller, task_id)
    handoff_receipt: Optional[dict[str, Any]] = None
    if handoff_receipt_path is not None:
        try:
            handoff_receipt = json.loads(Path(handoff_receipt_path).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise TaskWorkspaceError(f"handoff receipt is unreadable: {exc}") from exc
        if (not isinstance(handoff_receipt, dict)
                or handoff_receipt.get("schema_version") != FENCING_RECEIPT_SCHEMA
                or handoff_receipt.get("kind") != "handoff"
                or handoff_receipt.get("task_id") != task_id):
            raise TaskWorkspaceError("handoff receipt is not an exact task handoff receipt")
    with state_lock(controller):
        state = read_state(controller)
        record = state["tasks"].get(task_id)
        if not isinstance(record, dict):
            raise TaskWorkspaceError("task has not been started")
        lease = _lease_view(record)
        observation = (_observe_producer(lease.get("producer"))
                       if isinstance(lease, dict) else
                       decisions.LeaseObservation("unknown", "no lease"))
        recorded_handoff = lease.get("handoff") if isinstance(lease, dict) else None
        if handoff_receipt is not None and recorded_handoff is not None:
            receipt_file_sha = hashlib.sha256(
                Path(handoff_receipt_path).read_bytes()).hexdigest()
            if (handoff_receipt.get("attempt") != lease.get("attempt")
                    or receipt_file_sha != recorded_handoff.get("sha256")):
                raise TaskWorkspaceError(
                    "handoff receipt does not match the recorded handoff for this attempt")
        plan = decisions.plan_lease_successor(task_id, lease, observation, handoff_receipt)
        if not plan.admitted:
            raise TaskWorkspaceError(
                f"task lease-successor refused ({plan.code}): {plan.message}")
        recovery = _worktree_recovery_classification(record)
        attempt = (int(lease["attempt"]) + 1) if isinstance(lease, dict) else 1
        receipt_payload = {
            "schema_version": FENCING_RECEIPT_SCHEMA, "kind": "successor",
            "task_id": task_id, "attempt": attempt,
            "predecessor_attempt": lease.get("attempt") if isinstance(lease, dict) else None,
            "authority_kind": plan.authority_kind, "reason": plan.message,
            "recovery": {"classification": recovery["classification"],
                         "dirty_path_count": recovery.get("dirty_path_count", 0)},
            "producer_observation": {"status": observation.status, "detail": observation.detail}
            if isinstance(lease, dict) else None,
            "recorded_utc": _utc_now(),
        }
        receipt = _write_fencing_receipt(controller, task_id, receipt_payload)
        lease_next, token = _new_lease(
            task_id, attempt, "process", plan.authority_kind, receipt,
            reason=plan.message, producer_pid=os.getpid(), recovery=recovery)
        state["tasks"][task_id] = _apply_lease(record, lease_next)
        write_state(controller, state)
        return {"schema_version": FENCING_SCHEMA, "task_id": task_id,
                "outcome": "successor_issued", "attempt": attempt,
                "authority_kind": plan.authority_kind, "lease_token": token,
                "recovery": recovery,
                "receipt": receipt,
                "note": "the lease token is shown once; store it and present it via --lease-token"}


def lease_release(controller: Path, task_id: str, lease_token: Optional[str]) -> dict[str, Any]:
    """Holder terminal release without queueing (work abandoned or reassigned)."""
    if not TASK_RE.fullmatch(task_id):
        raise TaskWorkspaceError("unsafe task id")
    require_task(controller, task_id)
    record, lease = _lease_holder_authority(controller, "lease-release", task_id, lease_token)
    released = _ensure_lease_released(controller, task_id, record, reason="holder release")
    with state_lock(controller):
        state = read_state(controller)
        current = state["tasks"].get(task_id)
        if current != record:
            raise TaskWorkspaceError("task state changed during lease release; retry")
        state["tasks"][task_id] = released
        write_state(controller, state)
        return {"schema_version": FENCING_SCHEMA, "task_id": task_id,
                "outcome": "released", "attempt": lease.get("attempt"),
                "release_receipt": released["fencing"].get("release_receipt")}


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("operation", choices=(
        "start", "run", "recover-predispatch", "recover-wall-budget", "status", "hydrate", "preflight", "finish", "contract", "handoff",
        "checkpoint", "child-checkpoint", "evidence-run", "evidence-status", "evidence-await",
        "recovery-plan", "recovery-authorize", "recovery-apply", "runtime-bootstrap",
        "sync", "doctor", "lease-status", "lease-heartbeat", "lease-handoff",
        "lease-successor", "lease-revoke", "lease-release"))
    value.add_argument("--task")
    value.add_argument("--run-id", help="exact active task-run identity for receipt-bound recovery")
    value.add_argument("--attempt", type=int,
                       help="exact task-run worker attempt for wall-budget recovery")
    value.add_argument("--predispatch-receipt-sha256",
                       help="exact integrated controller pre-dispatch receipt digest")
    value.add_argument("--original-deadline-unix-ns", type=int,
                       help="immutable original cumulative task-run deadline")
    value.add_argument("--child",
                       help="admitted ordered umbrella child task id for child-checkpoint")
    value.add_argument("--path", action="append", default=[], help="required policy-admitted product root")
    value.add_argument("--umbrella-admission", type=Path,
                       help="versioned ordered-child exact-scope input")
    value.add_argument("--plan", type=Path, help="exact reviewed recovery plan")
    value.add_argument("--output", type=Path, help="exclusive recovery plan output")
    value.add_argument("--authorization-receipt", type=Path,
                       help="canonical immutable authorization binding the exact reviewed plan")
    value.add_argument("--controller", type=Path, default=Path.cwd(), help=argparse.SUPPRESS)
    value.add_argument("--dry-run", action="store_true")
    value.add_argument("--apply", type=Path)
    value.add_argument("--package-version")
    value.add_argument("--package-runtime-sha256")
    value.add_argument("--lease-token", help="current fencing lease token for gated mutations")
    value.add_argument("--reason", help="operator decision record for lease revoke or handoff")
    value.add_argument("--handoff-receipt", type=Path,
                       help="exact handoff receipt consumed by lease-successor")
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        controller = exact_root(args.controller, "controller", physical_identity=False)
        if args.operation == "runtime-bootstrap":
            if (args.task or args.path or args.umbrella_admission or args.plan or args.output
                    or args.authorization_receipt or args.child or not args.package_version
                    or not args.package_runtime_sha256):
                raise TaskWorkspaceError("runtime-bootstrap package identity is incomplete")
            if args.dry_run == bool(args.apply):
                raise TaskWorkspaceError("runtime-bootstrap requires exactly one of --dry-run or --apply <receipt>")
            result = runtime_bootstrap(controller, args.package_version,
                                       args.package_runtime_sha256, args.apply)
        else:
            if not args.task and args.operation not in {"doctor"}:
                raise TaskWorkspaceError(f"task {args.operation} requires --task")
            if args.operation != "start" and args.path:
                raise TaskWorkspaceError("--path is supported only for task start")
            recovery_operations = {"recover-predispatch", "recover-wall-budget"}
            if bool(args.run_id) != (args.operation in recovery_operations):
                raise TaskWorkspaceError("--run-id is required only for receipt-bound task recovery")
            wall_options = (args.attempt, args.predispatch_receipt_sha256,
                            args.original_deadline_unix_ns)
            if args.operation == "recover-wall-budget":
                if any(option is None for option in wall_options):
                    raise TaskWorkspaceError(
                        "recover-wall-budget requires --attempt, --predispatch-receipt-sha256, "
                        "and --original-deadline-unix-ns")
            elif any(option is not None for option in wall_options):
                raise TaskWorkspaceError(
                    "wall-budget identity options are supported only for task recover-wall-budget")
            if args.operation != "child-checkpoint" and args.child:
                raise TaskWorkspaceError("--child is supported only for task child-checkpoint")
            if args.dry_run or args.apply or args.package_version or args.package_runtime_sha256:
                raise TaskWorkspaceError("runtime-bootstrap options are not supported for task lifecycle operations")
            if args.lease_token and args.operation not in (
                    set(decisions.LEASE_GATED_COMMANDS) | {"lease-heartbeat", "lease-handoff",
                                                          "lease-release", "evidence-await"}):
                raise TaskWorkspaceError("--lease-token is supported only for fenced task mutations and holder lease commands")
            if args.reason and args.operation not in {"lease-revoke", "lease-handoff"}:
                raise TaskWorkspaceError("--reason is supported only for lease revoke or handoff")
            if args.handoff_receipt and args.operation != "lease-successor":
                raise TaskWorkspaceError("--handoff-receipt is supported only for lease-successor")
            audit = record_control_audit(controller, "task", args.operation, args.task)
            if args.operation in {"lease-status", "lease-heartbeat", "lease-handoff",
                                  "lease-successor", "lease-revoke", "lease-release"}:
                if args.umbrella_admission or args.plan or args.output or args.authorization_receipt:
                    raise TaskWorkspaceError(
                        "admission/recovery options are unsupported for lease operations")
                if args.operation == "lease-status":
                    result = lease_status(controller, args.task)
                elif args.operation == "lease-heartbeat":
                    result = lease_heartbeat(controller, args.task, args.lease_token)
                elif args.operation == "lease-handoff":
                    result = lease_handoff(controller, args.task, args.lease_token, args.reason)
                elif args.operation == "lease-successor":
                    result = lease_successor(controller, args.task, args.handoff_receipt)
                elif args.operation == "lease-revoke":
                    result = lease_revoke(controller, args.task, args.reason)
                else:
                    result = lease_release(controller, args.task, args.lease_token)
            elif args.operation == "start":
                if args.plan or args.output or args.authorization_receipt:
                    raise TaskWorkspaceError("recovery options are not supported for task start")
                result = start(controller, args.task, args.path, args.umbrella_admission,
                               args.lease_token)
            elif args.operation == "recovery-plan":
                if not args.umbrella_admission or not args.output or args.authorization_receipt or args.plan:
                    raise TaskWorkspaceError(
                        "recovery-plan requires --umbrella-admission and --output")
                plan = build_umbrella_recovery_plan(
                    controller, args.task, args.umbrella_admission)
                args.output.parent.mkdir(parents=True, exist_ok=True)
                data = (json.dumps(plan, sort_keys=True, separators=(",", ":")) + "\n").encode()
                fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data); handle.flush(); os.fsync(handle.fileno())
                result = {"schema_version": UMBRELLA_RECOVERY_PLAN_SCHEMA,
                          "task_id": args.task, "outcome": "planned",
                          "plan_path": str(args.output.resolve()),
                          "plan_sha256": stable_sha256(plan),
                          "plan_file_sha256": hashlib.sha256(data).hexdigest()}
            elif args.operation == "recovery-authorize":
                if not args.umbrella_admission or not args.plan or args.authorization_receipt or args.output:
                    raise TaskWorkspaceError(
                        "recovery-authorize requires --umbrella-admission and --plan")
                result = issue_umbrella_recovery_authorization(
                    controller, args.task, args.plan, args.umbrella_admission)
            elif args.operation == "recovery-apply":
                if (not args.umbrella_admission or not args.plan
                        or not args.authorization_receipt or args.output):
                    raise TaskWorkspaceError(
                        "recovery-apply requires --umbrella-admission, --plan, and --authorization-receipt")
                result = apply_umbrella_recovery(
                    controller, args.task, args.plan, args.umbrella_admission,
                    args.authorization_receipt)
            else:
                if args.umbrella_admission or args.plan or args.output or args.authorization_receipt:
                    raise TaskWorkspaceError(
                        "admission/recovery options are unsupported for this operation")
                if args.operation == "run":
                    result = managed_task_run(controller, args.task)
                elif args.operation == "recover-predispatch":
                    result = recover_task_predispatch(controller, args.task, args.run_id)
                elif args.operation == "recover-wall-budget":
                    result = recover_task_wall_budget(
                        controller, args.task, args.run_id, args.attempt,
                        args.predispatch_receipt_sha256, args.original_deadline_unix_ns)
                elif args.operation == "contract":
                    result = preimplementation_contract(controller, args.task)
                elif args.operation == "handoff":
                    result = run_handoff(controller, args.task)
                elif args.operation == "checkpoint":
                    result = standing_checkpoint(controller, args.task, args.lease_token)
                elif args.operation == "child-checkpoint":
                    if not args.child:
                        raise TaskWorkspaceError("child-checkpoint requires --child")
                    result = umbrella_child_checkpoint(controller, args.task, args.child,
                                                       args.lease_token)
                elif args.operation == "evidence-run":
                    result = standing_evidence_run(controller, args.task,
                                                   lease_token=args.lease_token)
                elif args.operation == "evidence-status":
                    result = standing_evidence_status(controller, args.task)
                elif args.operation == "evidence-await":
                    current = standing_evidence_status(controller, args.task)
                    result = current if current["state"] == "COMPLETE" else standing_evidence_run(
                        controller, args.task, lease_token=args.lease_token)
                elif args.operation == "sync":
                    result = recover_kanban_sync(controller, args.task, args.lease_token)
                elif args.operation == "doctor":
                    result = kanban_sync_doctor(controller, args.task or None)
                elif args.operation == "status":
                    result = status(controller, args.task)
                elif args.operation == "preflight":
                    result = preflight(controller, args.task)
                elif args.operation == "hydrate":
                    result = hydrate(controller, args.task, args.lease_token)
                else:
                    result = finish(controller, args.task, args.lease_token)
            result = {**result, "control_audit": audit}
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except (TaskWorkspaceError, OSError, json.JSONDecodeError) as exc:
        print(f"task workspace: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
