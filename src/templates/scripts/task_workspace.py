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
import gzip
import hashlib
import importlib.util
import io
import json
import os
import posixpath
import re
import secrets
import selectors
import shlex
import signal
import stat
import tarfile
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
BOUNDED_STATE_SCHEMA = "juno_task_workspace_state.v2"
TERMINAL_TOMBSTONE_SCHEMA = "juno_task_terminal_tombstone.v1"
STATE_ARCHIVE_PLAN_SCHEMA = "juno_task_state_archive_plan.v1"
STATE_ARCHIVE_MANIFEST_SCHEMA = "juno_task_state_archive_manifest.v1"
STATE_ARCHIVE_RECEIPT_SCHEMA = "juno_task_state_archive_receipt.v1"
STATE_ARCHIVE_COLD_REF = "refs/juno/cold/task-state"
TERMINAL_LIFECYCLE_STATES = {"MERGED", "WITHDRAWN"}
HOT_STATE_TARGET_BYTES = 5 * 1024 * 1024
HOT_STATE_WARNING_BYTES = 8 * 1024 * 1024
HOT_STATE_HARD_BYTES = 25 * 1024 * 1024
COLD_PACK_RAW_TARGET_BYTES = 16 * 1024 * 1024
COLD_PACK_MAX_BYTES = 25 * 1024 * 1024
COLD_PACK_EXPANDED_MAX_BYTES = 20 * 1024 * 1024
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
MANAGED_GENERATION_PATH = ".juno_task/runtime/managed-controller/generation.json"
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
LEGACY_DELIVERY_VERIFICATION_SCHEMA = "juno_task_legacy_delivery_verification.v1"
UMBRELLA_EXECUTION_MODE = "umbrella_owned_sequential"
UMBRELLA_RESERVATIONS_SCHEMA = "juno_task_umbrella_child_reservations.v1"
UMBRELLA_CHILD_CHECKPOINT_SCHEMA = "juno_task_umbrella_child_checkpoint.v1"
DELIVERY_CHECKPOINT_CONTRACT_SCHEMA = "juno_task_delivery_checkpoints.v1"
DELIVERY_CHECKPOINT_EVIDENCE_SCHEMA = "juno_task_delivery_checkpoint_evidence.v1"
DELIVERY_TRACKING_OWNERS_SCHEMA = "juno_task_delivery_tracking_owners.v1"
DELIVERY_CHECKPOINT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
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
    "REVIEWING": "in_progress",
    "RISK_EVIDENCE_READY": "in_progress",
    "CONFLICT": "in_progress",
    "CONFLICT_RESOLVED": "in_progress",
    "REOPENING": "in_progress",
    "REQUEUING_STALE": "in_progress",
    "REVIEW_FINDINGS": "in_progress",
    "REVIEW_FINDINGS_EXHAUSTED": "in_progress",
    "MERGING": "in_progress",
    # Native delivery persists this state after Git succeeds and before the
    # terminal Ledger projection. It must remain recoverable without repeating
    # integration when that projection is interrupted or refused.
    "GIT_INTEGRATED": "in_progress",
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
CANONICAL_VALIDATION_ROOT = ".juno_task/runtime/validation-receipts"
STANDING_PLAN_SCHEMA = "juno_standing_validation_plan.v1"
STANDING_ROOT = ".juno_task/runtime/standing-evidence"
SUBMISSION_ROOT = ".juno_task/runtime/task-submissions"


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

    # A packed release may run the supported profiler directly, without an
    # installed controller. Bind that case to the package containing this
    # exact dist test module; never search neighboring directories.
    packaged_root = test_path.parents[4] if len(test_path.parents) > 4 else None
    packaged_test_root = (packaged_root / "dist/templates/scripts/tests"
                          if packaged_root is not None else None)
    if packaged_test_root is not None and test_path.parent == packaged_test_root:
        try:
            packaged = json.loads((packaged_root / "package.json").read_text())
        except (OSError, json.JSONDecodeError):
            packaged = None
        if (not isinstance(packaged, dict) or packaged.get("name") != "@yylo/cli"
                or not is_valid_semver(packaged.get("version"))):
            raise TaskWorkspaceError("package-bound test fixture has invalid package identity")
        package_fixture = (packaged_root / "scripts/test-support" / fixture_name
                           if fixture_name == "task_workspace_fixture.py"
                           else packaged_test_root / fixture_name)
        return load(package_fixture)

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
            package_fixture = (package_root / "scripts/test-support" / fixture_name
                               if fixture_name == "task_workspace_fixture.py"
                               else package_root / "dist/templates/scripts/tests" / fixture_name)
            return load(package_fixture)

    # Development execution is the only fallback. Its identity is an actual
    # Git worktree plus exact tracked yylo paths, never a guessed sibling.
    discovered = run(["git", "-C", str(test_path.parent), "rev-parse", "--show-toplevel"],
                     test_path.parent, check=False)
    if discovered.returncode == 0:
        source_root = Path(discovered.stdout.strip()).resolve()
        canonical = (source_root / "juno-code/scripts/test-support" / fixture_name
                     if fixture_name == "task_workspace_fixture.py"
                     else source_root / "juno-code/src/templates/scripts/tests" / fixture_name)
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
    optional = {"selectable_paths", "hydration_workflow", "validation_profiles", "documentation_validation",
                "legacy_umbrella_creation"}
    if not isinstance(value, dict):
        raise TaskWorkspaceError("task workspace policy must be a JSON object")
    missing = sorted(required - set(value))
    extra = sorted(set(value) - required - optional)
    if missing or extra:
        details = []
        if missing:
            details.append("missing fields: " + ", ".join(missing))
        if extra:
            details.append("extra fields: " + ", ".join(extra))
        raise TaskWorkspaceError(
            f"task workspace policy field mismatch ({'; '.join(details)})")
    if value.get("schema_version") != CONFIG_SCHEMA:
        raise TaskWorkspaceError(
            f"task workspace policy schema_version must be {CONFIG_SCHEMA}")
    value.setdefault("selectable_paths", [])
    value.setdefault("hydration_workflow", ".juno_task/config/worktree-hydration.yaml")
    value.setdefault("legacy_umbrella_creation", False)
    if not isinstance(value["legacy_umbrella_creation"], bool):
        raise TaskWorkspaceError("legacy_umbrella_creation policy must be boolean")
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
    configured_workspace = value["workspace_root"]
    if configured_workspace == "@state/yylo/task-worktrees":
        state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")).expanduser()
        workspace = state_home / "yylo/task-worktrees"
        value["workspace_root"] = str(workspace)
    else:
        workspace = Path(configured_workspace).expanduser()
    if not workspace.is_absolute() or workspace == Path("/"):
        raise TaskWorkspaceError("workspace_root must be an explicit absolute directory or @state/yylo/task-worktrees")
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


def canonical_requirement_identity(controller: Path, task_id: str) -> dict[str, Any]:
    """Bind authored task requirements and every explicitly linked controller PDR."""
    _path, body = task_manifest(controller, task_id)
    immutable = immutable_task_body(body)
    text = immutable.decode("utf-8", errors="replace")
    pdr_paths = sorted(set(re.findall(
        r"\.juno_task/specs/[A-Za-z0-9][A-Za-z0-9._/-]*\.md", text)))
    pdrs: dict[str, str] = {}
    for relative in pdr_paths:
        path = (controller / relative).resolve()
        try:
            path.relative_to(controller.resolve())
            pdrs[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        except (ValueError, OSError) as exc:
            raise TaskWorkspaceError(
                f"canonical task requirement PDR is missing or unsafe: {relative}") from exc
    material = {"task_requirements_sha256": hashlib.sha256(immutable).hexdigest(),
                "pdr_revisions": pdrs}
    return {**material, "requirements_sha256": stable_sha256(material)}


def delivery_checkpoint_contract(controller: Path, task_id: str) -> Optional[dict[str, Any]]:
    """Parse one bounded ordered checkpoint contract from the authored task body."""
    _path, body = task_manifest(controller, task_id)
    immutable = immutable_task_body(body).decode("utf-8", errors="strict")
    matches = re.findall(
        r"\[delivery_checkpoints\]\s*(.*?)\s*\[/delivery_checkpoints\]",
        immutable, flags=re.DOTALL)
    if not matches:
        return None
    if len(matches) != 1:
        raise TaskWorkspaceError("task has multiple delivery checkpoint contracts")
    try:
        value = json.loads(matches[0])
    except json.JSONDecodeError as exc:
        raise TaskWorkspaceError(f"delivery checkpoint contract is invalid JSON: {exc}") from exc
    if (not isinstance(value, dict)
            or set(value) != {"schema_version", "tracking_task_ids", "checkpoints"}
            or value.get("schema_version") != DELIVERY_CHECKPOINT_CONTRACT_SCHEMA
            or not isinstance(value.get("tracking_task_ids"), list)
            or not isinstance(value.get("checkpoints"), list)
            or not value["checkpoints"] or len(value["checkpoints"]) > 32):
        raise TaskWorkspaceError(
            f"delivery checkpoint contract must use {DELIVERY_CHECKPOINT_CONTRACT_SCHEMA} with 1..32 checkpoints")
    tracking = value["tracking_task_ids"]
    if (len(set(tracking)) != len(tracking)
            or any(not isinstance(item, str) or not TASK_RE.fullmatch(item)
                   or item == task_id for item in tracking)):
        raise TaskWorkspaceError("delivery checkpoint tracking task IDs are invalid or duplicated")
    normalized: list[dict[str, Any]] = []
    ids: set[str] = set()
    for index, row in enumerate(value["checkpoints"]):
        if (not isinstance(row, dict) or set(row) != {"id", "requirement", "final"}
                or not isinstance(row.get("id"), str)
                or not DELIVERY_CHECKPOINT_ID_RE.fullmatch(row["id"])
                or row["id"] in ids
                or not isinstance(row.get("requirement"), str)
                or not row["requirement"].strip()
                or len(row["requirement"].encode()) > 4096
                or not isinstance(row.get("final"), bool)):
            raise TaskWorkspaceError(f"delivery checkpoint {index + 1} is malformed")
        ids.add(row["id"])
        requirement = row["requirement"].strip()
        normalized.append({"id": row["id"], "requirement": requirement,
                           "final": row["final"],
                           "requirement_sha256": stable_sha256({
                               "id": row["id"], "requirement": requirement,
                               "final": row["final"]})})
    finals = [index for index, row in enumerate(normalized) if row["final"]]
    if finals != [len(normalized) - 1]:
        raise TaskWorkspaceError("delivery checkpoint contract requires exactly one final checkpoint, ordered last")
    body_value = {"schema_version": DELIVERY_CHECKPOINT_CONTRACT_SCHEMA,
                  "task_id": task_id, "tracking_task_ids": tracking,
                  "checkpoints": normalized, "source": "ordinary_task_requirements"}
    return {**body_value, "contract_sha256": stable_sha256(body_value)}


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


def delivery_tracking_owners(state: dict[str, Any]) -> dict[str, str]:
    value = state["queues"].setdefault("delivery_tracking_owners", {
        "schema_version": DELIVERY_TRACKING_OWNERS_SCHEMA, "owners": {},
    })
    if (not isinstance(value, dict) or set(value) != {"schema_version", "owners"}
            or value.get("schema_version") != DELIVERY_TRACKING_OWNERS_SCHEMA
            or not isinstance(value.get("owners"), dict)
            or not all(TASK_RE.fullmatch(str(child)) and TASK_RE.fullmatch(str(owner))
                       for child, owner in value["owners"].items())):
        raise TaskWorkspaceError("delivery tracking owner state is invalid")
    return value["owners"]


def tracking_owner(state: dict[str, Any], task_id: str) -> Optional[str]:
    legacy = child_reservations(state).get(task_id)
    delivery = delivery_tracking_owners(state).get(task_id)
    if legacy is not None and delivery is not None and legacy != delivery:
        raise TaskWorkspaceError(f"tracking task {task_id} has conflicting lifecycle owners")
    return delivery or legacy


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
            or value.get("schema_version") not in {STATE_SCHEMA, BOUNDED_STATE_SCHEMA}
            or not isinstance(value.get("tasks"), dict) or not isinstance(value.get("queues"), dict)):
        raise TaskWorkspaceError("invalid task workspace state schema; upgrade YYLO before using bounded lifecycle state")
    if value.get("schema_version") == BOUNDED_STATE_SCHEMA:
        for task_id, record in value["tasks"].items():
            if (isinstance(record, dict) and record.get("state") in TERMINAL_LIFECYCLE_STATES
                    and (record.get("schema_version") != TERMINAL_TOMBSTONE_SCHEMA
                         or record.get("task_id") != task_id)):
                raise TaskWorkspaceError("bounded task state contains a non-tombstone terminal record")
    return value


def write_state(controller: Path, state: dict[str, Any], *, allow_compaction: bool = False) -> None:
    path = state_path(controller)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(data) > HOT_STATE_HARD_BYTES and not allow_compaction:
        raise TaskWorkspaceError(
            f"task state would be {len(data)} bytes, above the {HOT_STATE_HARD_BYTES}-byte hard limit; "
            "run `yy task state-archive-plan --output <external-plan>` and apply the reviewed compaction")
    if len(data) > HOT_STATE_WARNING_BYTES:
        print(f"warning: task state is {len(data)} bytes (warning threshold {HOT_STATE_WARNING_BYTES})", file=sys.stderr)
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
        durations = {row["state"]: row["duration_ms"] for row in self.states}
        return {"schema_version": VALIDATION_TIMING_SCHEMA, "states": self.states,
                "resource_wait_ms": durations.get("WAITING_FOR_RESOURCE", 0),
                "setup_ms": durations.get("SETUP", 0),
                "execution_ms": durations.get("RUNNING", 0),
                "settlement_ms": durations.get("TEARDOWN", 0),
                "first_failure_ms": wall_ms if outcome != "PASSED" else None,
                "overall_elapsed_ms": wall_ms,
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
            # Keep timeout enforcement comfortably inside the public bound;
            # a 50ms selector quantum made the one-second contract flaky on a
            # loaded host even though the process group was killed correctly.
            for key, _ in selector.select(0.01):
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
                        except (ProcessLookupError, PermissionError): pass
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


# BEGIN GENERATED INSTRUCTION COMPATIBILITY POLICY
INSTRUCTION_COMPATIBILITY = {"policySchema":"juno_instruction_bundle_compatibility.v1","manifestSchemas":[1,2],"declarationSchema":"juno_instruction_bundle_declaration.v1","identitySchema":"juno_instruction_bundle.v1","supportedMajor":"1","stableVersionPattern":"^(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)$","recovery":"Preserve the current generation; use an authenticated CLI package supporting this instruction schema/major before migration."}
# END GENERATED INSTRUCTION COMPATIBILITY POLICY


def instruction_version_compatible(value: Any) -> bool:
    """Stable minor/patch revisions preserve the supported instruction contract."""
    return (isinstance(value, str)
            and re.fullmatch(INSTRUCTION_COMPATIBILITY["stableVersionPattern"], value) is not None
            and value.split(".")[0] == INSTRUCTION_COMPATIBILITY["supportedMajor"])


def instruction_declaration_compatible(schema: Any, declaration: Any) -> bool:
    # bool is not a JSON integer schema, even though Python bool subclasses int.
    if type(schema) is not int or schema not in INSTRUCTION_COMPATIBILITY["manifestSchemas"]:
        return False
    if schema == 1:
        return declaration is None
    return (isinstance(declaration, dict)
            and set(declaration) == {"schemaVersion", "semanticVersion"}
            and declaration.get("schemaVersion") == INSTRUCTION_COMPATIBILITY["declarationSchema"]
            and instruction_version_compatible(declaration.get("semanticVersion")))


def instruction_compatibility_error() -> str:
    return "instruction_bundle_incompatible: " + INSTRUCTION_COMPATIBILITY["recovery"]


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
    declaration_valid = instruction_declaration_compatible(schema, instruction_declaration)
    if not declaration_valid or not isinstance(managed.get("assets"), list) or not isinstance(rows, list):
        raise TaskWorkspaceError(f"invalid generated-output declaration {MANAGED_OUTPUT_DECLARATION}; "
                                 + instruction_compatibility_error())
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


def path_origin_projection(repository: Path, base_sha: str, source_sha: str,
                           target_sha: str, candidate_sha: Optional[str],
                           admitted_paths: list[str], generated_admission: Any,
                           conflict_paths: Optional[list[str]] = None) -> dict[str, Any]:
    """Git adapter for the canonical pure blob-origin projection."""
    candidate_sha = candidate_sha or source_sha
    for label, sha in (("base", base_sha), ("source", source_sha),
                       ("target", target_sha), ("candidate", candidate_sha)):
        if (not isinstance(sha, str) or not SHA_RE.fullmatch(sha)
                or run(["git", "-C", str(repository), "cat-file", "-e", f"{sha}^{{commit}}"],
                       repository, check=False).returncode):
            raise TaskWorkspaceError(f"path origin {label} object is missing or forged")
    bindings = (generated_admission.get("bindings", [])
                if isinstance(generated_admission, dict) else [])
    return decisions.project_path_origins(
        base_tree=_tip_tree_blobs(repository, base_sha),
        source_tree=_tip_tree_blobs(repository, source_sha),
        target_tree=_tip_tree_blobs(repository, target_sha),
        candidate_tree=_tip_tree_blobs(repository, candidate_sha),
        admitted_paths=admitted_paths, generated_bindings=bindings,
        conflict_paths=conflict_paths or [])


def _tip_tree_blobs(repository: Path, tip_sha: str) -> dict[str, str]:
    """Map every tracked blob path using NUL framing (never quoted path text)."""
    result = run(["git", "-C", str(repository), "ls-tree", "-rz", tip_sha],
                 repository, check=False)
    if result.returncode:
        raise TaskWorkspaceError("path origin tree object is unreadable")
    output = result.stdout
    blobs: dict[str, str] = {}
    for entry in output.split("\0"):
        if not entry:
            continue
        metadata, separator, path = entry.partition("\t")
        if not separator:
            raise TaskWorkspaceError("path origin tree entry is malformed")
        mode, kind, object_id = metadata.split()
        if kind in {"blob", "commit"}:
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


def controller_generation_admission(controller: Path, repository: Path) -> Optional[dict[str, Any]]:
    """Consumer projects may use an authenticated controller-local generation.

    Bootstrap only the engine bytes authenticated by the retained tarball, then
    let that one package-owned assessor validate the full generation. Never run
    a module merely because a mutable receipt names its filesystem location.
    """
    marker_path = controller / ".juno_task/runtime/generation-migration/current.json"
    if not marker_path.exists() and not marker_path.is_symlink():
        return None
    try:
        if marker_path.resolve() != marker_path or not marker_path.is_file():
            raise ValueError("unsafe generation marker")
        marker = json.loads(marker_path.read_bytes())
        if marker.get("schema_version") != "yylo_controller_generation.v1":
            raise ValueError("unknown generation schema")
        evidence = marker["candidate"]
        root, artifact = Path(evidence["root"]), Path(evidence["artifact"])
        relative = "dist/templates/maintenance/controller_generation_migration.py"
        engine = root / relative
        for file in (artifact, engine):
            info = file.lstat()
            if (not file.is_absolute() or file.resolve() != file or not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1 or info.st_size > 64 * 1024 * 1024):
                raise ValueError("unsafe generation evidence")
        archive_bytes = artifact.read_bytes()
        if hashlib.sha256(archive_bytes).hexdigest() != evidence["sha256"]:
            raise ValueError("generation artifact digest mismatch")
        captured: dict[str, bytes] = {}
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
            seen, total = set(), 0
            for index, member in enumerate(archive):
                if index >= 10000:
                    raise ValueError("too many generation archive members")
                if member.isdir():
                    continue
                name = member.name
                total += member.size
                if (name in seen or not member.isfile() or member.size < 0 or not name.startswith("package/")
                        or "\\" in name or any(part in {"", ".", ".."} for part in name.split("/"))
                        or total > 64 * 1024 * 1024 or len(seen) > 10000):
                    raise ValueError("unsafe generation archive member")
                seen.add(name)
                if name.startswith("package/dist/templates/"):
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise ValueError("missing generation archive member")
                    captured[name.removeprefix("package/")] = stream.read()
        if captured.get(relative) != engine.read_bytes():
            raise ValueError("generation engine digest mismatch")
        # Execute captured imports too: an installed-directory race must never
        # substitute metadata_controller/task_workspace dependencies after auth.
        with tempfile.TemporaryDirectory(prefix="yylo-generation-readback-") as temporary:
            closure = Path(temporary)
            for name, data in captured.items():
                destination = closure / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
            result = subprocess.run([sys.executable, "-I", "-B", str(closure / relative), "runtime-ready",
                                     "--controller", str(repository), "--projection", str(controller),
                                     "--running-runtime", str(Path(__file__).resolve())],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=90, check=False)
        if result.returncode:
            raise ValueError((result.stdout + result.stderr).decode(errors="replace")[-4000:])
        admitted = json.loads(result.stdout)
        if (admitted.get("schema_version") != "yylo_controller_generation_admission.v1"
                or admitted.get("controller") != str(repository)
                or admitted.get("projection") != str(controller)
                or admitted.get("runtime_sha256") != hashlib.sha256(Path(__file__).read_bytes()).hexdigest()):
            raise ValueError("generation admission identity mismatch")
        return admitted
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError, subprocess.TimeoutExpired) as exc:
        raise TaskWorkspaceError(f"controller generation admission refused: {exc}; preserve bytes and inspect `yy scripts generation doctor`") from exc


def require_current_runtime(repository: Path, target_sha: str,
                            controller: Path | None = None) -> dict[str, Any]:
    generation = runtime_generation(repository, target_sha)
    source_repository = (
        target_blob(repository, target_sha, "juno-code/package.json") is not None
        or target_blob(repository, target_sha,
                       "juno-code/src/templates/scripts/task_workspace.py") is not None
    )
    if not source_repository and controller is not None:
        admitted = controller_generation_admission(controller, repository)
        if admitted is not None:
            return {**generation, "current": True, "target_copy_current": generation["current"],
                    "controller_generation_admission": admitted}
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
            previous = None
            if controller is not None:
                try:
                    managed = json.loads((controller / MANAGED_GENERATION_PATH).read_text())
                    candidate = managed.get("target_sha") if isinstance(managed, dict) else None
                    previous = candidate if isinstance(candidate, str) and SHA_RE.fullmatch(candidate) else None
                except (OSError, json.JSONDecodeError):
                    pass
            if previous is None:
                history = run(["git", "-C", str(repository), "rev-list", "--max-count=256",
                               target_sha, "--", RUNTIME_PATH], repository, check=False)
                for candidate in history.stdout.splitlines() if history.returncode == 0 else []:
                    blob = target_blob(repository, candidate, RUNTIME_PATH)
                    if blob is not None and hashlib.sha256(blob).hexdigest() == generation["running_sha256"]:
                        previous = candidate
                        break
            if previous and controller is not None:
                prefix = Path.home() / ".local/share/juno/runtimes" / f"source-{target_sha[:12]}"
                receipt = Path("/tmp") / f"yylo-source-runtime-adoption-{target_sha[:12]}.json"
                raise TaskWorkspaceError(
                    "managed task runtime differs from a Juno source target. Complete safe recovery: "
                    f"`yy integration runtime-adopt-source --previous-sha {previous} "
                    f"--target-sha {target_sha} --install-prefix {shlex.quote(str(prefix))} "
                    f"--output {shlex.quote(str(receipt))}`; this one transaction builds and authenticates "
                    "the exact unpublished artifact, rebinds the clean controller, refreshes managed runtime, "
                    "runs runtime-doctor, and verifies task-start admission; do not use "
                    "runtime-install-rebind or runtime-refresh alone"
                )
            raise TaskWorkspaceError(
                "managed task runtime differs from a Juno source target; recover only with the complete "
                "`yy integration runtime-adopt-source --help` transaction (the current managed generation "
                "identity is unavailable), not runtime-install-rebind or runtime-refresh alone"
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
    """Resolve explicit roots or exact tracked files without implicit broadening."""
    normalized = [normalized_relative(item, "required task path") for item in requested]
    if len(set(normalized)) != len(normalized):
        raise TaskWorkspaceError("required task paths contain duplicates")
    entries: dict[str, dict[str, str]] = {}
    for item in normalized:
        selectable = item in config["selectable_paths"]
        if not selectable and not path_within(item, config["allowed_paths"]):
            raise TaskWorkspaceError(f"required task path is not admitted by policy: {item}")
        output = git(repository, "ls-tree", target_sha, "--", item, check=False)
        lines = [line for line in output.splitlines() if line]
        if not lines and not selectable and item in config["allowed_paths"]:
            # A complete exact policy entry may reserve one new file without
            # granting its parent or any sibling. The frozen target SHA binds
            # the proven absence; the zero object is an explicit receipt
            # identity, not a wildcard or inferred directory permission.
            entries[item] = {"mode": "000000", "type": "absent", "object": "0" * 40}
            continue
        if len(lines) != 1:
            raise TaskWorkspaceError(f"required task path is absent or ambiguous at target: {item}")
        metadata, actual_path = lines[0].split("\t", 1)
        mode, kind, object_id = metadata.split()
        safe = ((selectable and mode in {"040000", "160000"} and kind in {"tree", "commit"})
                or (not selectable and mode in {"100644", "100755"} and kind == "blob"))
        if actual_path != item or not safe:
            raise TaskWorkspaceError(f"required task path has an unsafe target identity: {item}")
        entries[item] = {"mode": mode, "type": kind, "object": object_id}
    exact_mode = any(item not in config["selectable_paths"] for item in normalized)
    return (normalized if exact_mode else [*config["allowed_paths"], *normalized]), entries


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
    current_record = read_state(controller)["tasks"].get(task_id)
    if isinstance(current_record, dict) and _frozen_delivery_contract(current_record) is not None:
        # Finite command alias: converted records use the ordinary checkpoint
        # implementation and never regain per-child lifecycle authority.
        return accept_delivery_checkpoint(controller, task_id, child_id, lease_token)
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
    expected_policy = ("kanban" if operation in {"status", "admission", "preflight", "recovery-plan", "recovery-verify", "evidence-status", "doctor", "lease-status"}
                       else "orchestration")
    if surface == "task" and operation not in {
            "start", "run", "resume", "recover-predispatch", "recover-wall-budget", "status", "admission", "hydrate", "preflight", "finish",
            "checkpoint", "child-checkpoint", "evidence-run", "evidence-status", "evidence-await",
            "recovery-plan", "recovery-authorize", "recovery-apply", "recovery-verify", "sync", "doctor",
            "lease-status", "lease-heartbeat", "lease-handoff", "lease-successor",
            "lease-revoke", "lease-release"}:
        raise TaskWorkspaceError(f"unsupported task audit operation: {operation}")
    if surface == "merge" and operation not in {"status", "land", "project"}:
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


MUTABLE_DEPENDENCY_CACHE_PATHS = frozenset({".vite/vitest/results.json"})


def _dependency_content_manifest(worktree: Path, config: dict[str, Any]) -> dict[str, str]:
    """Content-address every behavior-relevant dependency file per lock cwd.

    npm metadata validation cannot detect tampered or corrupted installed
    bytes, so hydration records regular files and symlinks under each lock
    cwd's node_modules. Explicit test-run caches are excluded because they are
    outputs, not command inputs. The controller-side manifest is verified
    before any worker budget is spent.
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
            dependency_relative = path.relative_to(node_modules).as_posix()
            if dependency_relative in MUTABLE_DEPENDENCY_CACHE_PATHS:
                continue
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
                             allow_done: bool = False,
                             commit_hash: Optional[str] = None,
                             response: Optional[str] = None) -> dict[str, Any]:
    """Project one lifecycle state onto the canonical board, fail-closed.

    Idempotent: an already-projected board returns ``verified`` without a
    mutation. Every mutation is revision-CAS bound through the append-only
    ledger, writes one immutable receipt, and is readback-verified.
    """
    if lifecycle_state not in LIFECYCLE_BOARD_STATUS:
        raise KanbanSyncError(f"lifecycle state has no board projection: {lifecycle_state}",
                              {"task_id": task_id, "lifecycle_state": lifecycle_state})
    board_status = LIFECYCLE_BOARD_STATUS[lifecycle_state]
    if commit_hash is not None and (board_status != "done" or not re.fullmatch(r"[0-9a-f]{40}", commit_hash)):
        raise KanbanSyncError("only a verified merged projection may carry an integration commit",
                              {"task_id": task_id, "lifecycle_state": lifecycle_state})
    if board_status == "done" and not allow_done:
        # Native delivery projection exclusively owns the done mutation; this
        # helper only verifies it after the fact.
        current = read_kanban_task(controller, task_id)
        if current.get("status") == "done":
            return {"schema_version": KANBAN_SYNC_SCHEMA, "task_id": task_id,
                    "lifecycle_state": lifecycle_state,
                    "outcome": "verified",
                    "board_status": "done",
                    "recovery_command": None}
        raise KanbanSyncError(
            "native delivery projection owns the done mutation; retry Ledger projection",
            {"task_id": task_id, "lifecycle_state": lifecycle_state,
             "board_status": current.get("status"),
             "recovery_command": f"yy merge project {task_id}"})
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
                    for key, value in desired_fields.items())
            and (commit_hash is None or current.get("commit_hash") == commit_hash)):
        return {"schema_version": KANBAN_SYNC_SCHEMA, "task_id": task_id,
                "lifecycle_state": lifecycle_state, "outcome": "verified",
                "board_status": board_status,
                "board_revision": kanban_board_revision(controller, task_id)}
    if (current_status == "done" and commit_hash is not None
            and current.get("commit_hash") not in {None, commit_hash}):
        raise KanbanSyncError(
            "canonical Kanban task is done with a different integration commit",
            {"task_id": task_id, "lifecycle_state": lifecycle_state,
             "board_status": current_status, "commit_hash": current.get("commit_hash")})
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
                "fields": desired_fields, "commit_hash": commit_hash,
                "response": response}
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
    if commit_hash is not None:
        argv += ["--commit", commit_hash]
    if response is not None:
        argv += ["--response", response]
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
                   for key, value in desired_fields.items())
            or (commit_hash is not None and readback.get("commit_hash") != commit_hash)):
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


DOCTOR_PAGE_SIZE = 100
DOCTOR_ROW_LIMIT = 1000
DOCTOR_MAX_PAGES = 100
DOCTOR_READ_BYTES = 1024 * 1024


def _doctor_read(controller: Path, argv: list[str]) -> str:
    """Bound one read-only Ledger child, including malformed/oversized replies."""
    process = subprocess.Popen([str(_kanban_wrapper(controller)), *argv], cwd=controller,
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True)
    chunks: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + 30
    completed = False
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            while selector.get_map():
                if time.monotonic() >= deadline:
                    raise KanbanSyncError("doctor Ledger read timed out", {})
                for key, _ in selector.select(0.1):
                    data = os.read(key.fileobj.fileno(), 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    chunks[key.data].extend(data)
                    if sum(map(len, chunks.values())) > DOCTOR_READ_BYTES:
                        raise KanbanSyncError("doctor Ledger output exceeded byte limit", {})
        process.wait(timeout=max(0.01, deadline - time.monotonic()))
        if process.returncode:
            # Never echo task bodies or arbitrary stderr into the doctor report.
            raise KanbanSyncError("doctor Ledger read failed; snapshot may have changed",
                                  {"returncode": process.returncode})
        payload = chunks["stdout"].decode("utf-8")
        completed = True
        return payload
    except (UnicodeDecodeError, subprocess.TimeoutExpired) as exc:
        raise KanbanSyncError("doctor Ledger response unavailable", {}) from exc
    finally:
        if not completed or process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait()
        process.stdout.close()
        process.stderr.close()


def _doctor_json_documents(payload: str) -> list[Any]:
    decoder = json.JSONDecoder()
    documents: list[Any] = []
    remaining = payload.strip()
    try:
        while remaining:
            value, end = decoder.raw_decode(remaining)
            documents.append(value)
            remaining = remaining[end:].strip()
            if len(documents) > 2:
                raise ValueError("unexpected framing")
    except (ValueError, json.JSONDecodeError) as exc:
        raise KanbanSyncError("doctor Ledger response is malformed", {}) from exc
    return documents


def _doctor_board_rows(controller: Path, ids: list[str], *, exact: bool = False
                       ) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Cursor-bound hot pages; cold lookup only for exact requested missing IDs.

    This is not an atomic snapshot with lifecycle state or cold reads. Never
    retry a stale cursor by offset or silently fall back to per-row hot reads.
    """
    board: dict[str, dict[str, Any]] = {}
    coverage: dict[str, Any] = {"read_calls": 0, "hot_pages": 0, "cold_batches": 0,
                                "complete": True, "consistency": "non_atomic_observation"}
    if any(not isinstance(key, str) or not TASK_RE.fullmatch(key) for key in ids):
        raise TaskWorkspaceError("unsafe task id in doctor selection")
    wanted = set(ids)
    seen: set[str] = set()
    deadline = time.monotonic() + 90

    def acquire(argv: list[str]) -> list[Any]:
        if time.monotonic() >= deadline:
            raise KanbanSyncError("doctor scan time budget reached", {})
        coverage["read_calls"] += 1
        return _doctor_json_documents(_doctor_read(controller, ["-f", "json", *argv]))

    def accept(rows: Any, allowed: Optional[set[str]] = None) -> None:
        if not isinstance(rows, list) or len(rows) > DOCTOR_PAGE_SIZE:
            raise KanbanSyncError("doctor Ledger row count/shape is invalid", {})
        for row in rows:
            if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
                    or not TASK_RE.fullmatch(row["id"]) or row["id"] in seen
                    or (allowed is not None and row["id"] not in allowed)
                    or row.get("status") not in ("backlog", "todo", "in_progress", "done", "archive")
                    or not isinstance(row.get("fields", {}), dict)):
                raise KanbanSyncError("doctor Ledger row identity/shape is invalid", {})
            seen.add(row["id"])
            if row["id"] in wanted:
                fields = row.get("fields", {})
                board[row["id"]] = {"status": row["status"], "fields": {
                    key: fields.get(key) for key in ("lifecycle_projection", "lifecycle_state")}}

    try:
        if not exact and ids:
            cursor = None
            cursors: set[str] = set()
            for _ in range(DOCTOR_MAX_PAGES):
                argv = ["list", "--limit", str(DOCTOR_PAGE_SIZE), "--projection", "metadata",
                        "--fields", "id,status,fields", "--show-cursor"]
                if cursor is not None:
                    argv += ["--cursor", cursor]
                docs = acquire(argv)
                coverage["hot_pages"] += 1
                if (len(docs) != 2 or not isinstance(docs[1], dict)
                        or not isinstance(docs[1].get("summary"), dict)
                        or "next_cursor" not in docs[1]["summary"]):
                    raise KanbanSyncError("doctor Ledger page framing is invalid", {})
                accept(docs[0])
                cursor = docs[1]["summary"]["next_cursor"]
                if cursor is None:
                    break
                if not isinstance(cursor, str) or not cursor or cursor in cursors or len(cursor) > 4096:
                    raise KanbanSyncError("doctor Ledger cursor is invalid", {})
                cursors.add(cursor)
            else:
                raise KanbanSyncError("doctor Ledger hot page limit reached", {})
        missing = sorted(wanted - board.keys())
        for offset in range(0, len(missing), DOCTOR_PAGE_SIZE):
            batch = missing[offset:offset + DOCTOR_PAGE_SIZE]
            docs = acquire(["get", *batch, "--compact"])
            coverage["cold_batches"] += 0 if exact else 1
            if len(docs) != 1:
                raise KanbanSyncError("doctor Ledger exact read framing is invalid", {})
            values = [docs[0]] if isinstance(docs[0], dict) else docs[0]
            accept(values, set(batch))
        if wanted - board.keys():
            coverage.update(complete=False, reason="kanban_task_absent")
    except (KanbanSyncError, OSError) as exc:
        coverage.update(complete=False, reason="kanban_read_failed",
                        diagnostic=str(exc)[:256])
    coverage["readable"] = len(board)
    return board, coverage


def kanban_sync_doctor(controller: Path, task_id: Optional[str] = None, *,
                       limit: int = DOCTOR_ROW_LIMIT, offset: int = 0) -> dict[str, Any]:
    """Bounded read-only reconciliation of board truth versus task records."""
    state = read_state(controller)
    records = state.get("tasks", {})
    if not 1 <= limit <= DOCTOR_ROW_LIMIT or offset < 0:
        raise TaskWorkspaceError("doctor requires 1 <= limit <= 1000 and offset >= 0")
    selected = sorted(records.items())
    total = len(selected)
    if task_id is not None:
        if not TASK_RE.fullmatch(task_id):
            raise TaskWorkspaceError("unsafe task id")
        if task_id not in records:
            return {"schema_version": KANBAN_SYNC_SCHEMA, "task_id": task_id,
                    "rows": [], "summary": {"examined": 0, "drift": 0},
                    "outcome": "no_task_record"}
        selected = [(task_id, records[task_id])]
        total = 1
    else:
        selected = selected[offset:offset + limit]
    board_rows, coverage = _doctor_board_rows(controller, [key for key, _ in selected],
                                              exact=task_id is not None)
    coverage.update(total_records=total, selected_records=len(selected), offset=offset,
                    next_offset=(offset + len(selected)
                                 if task_id is None and offset + len(selected) < total else None))
    coverage["complete"] = coverage["complete"] and (task_id is not None or
                             (offset == 0 and len(selected) == total))
    rows: list[dict[str, Any]] = []
    drift = 0
    for current_id, record in selected:
        if not isinstance(record, dict):
            coverage.update(complete=False, reason="malformed_lifecycle_record")
            record = {}
        lifecycle_state = record.get("state")
        expected = LIFECYCLE_BOARD_STATUS.get(lifecycle_state) if isinstance(lifecycle_state, str) else None
        reasons: list[str] = []
        board_status = None
        board_fields: dict[str, Any] = {}
        board_error = None
        board = board_rows.get(current_id)
        if board is None:
            board_error = coverage.get("reason", "kanban_read_failed")
            reasons.append(board_error)
        else:
            board_status = board.get("status")
            board_fields = board["fields"]
        if expected is None:
            reasons.append("malformed_lifecycle_record")
            coverage.update(complete=False, reason="malformed_lifecycle_record")
        if board_error is None and isinstance(lifecycle_state, str) and expected is not None:
            if board_status != expected:
                if expected == "in_progress" and board_status in PRESTART_TRACKING_STATUSES:
                    reasons.append("active_lifecycle_record_in_backlog_or_todo")
                elif expected == "done":
                    reasons.append("merged_lifecycle_not_done_on_board")
                elif lifecycle_state == "WITHDRAWN":
                    reasons.append("withdrawn_board_status_mismatch")
                elif board_status == "done":
                    reasons.append("board_done_without_merge_truth")
                elif board_status == "archive":
                    reasons.append("board_archived_while_lifecycle_active")
                else:
                    reasons.append("board_status_mismatch")
            projection = board_fields.get("lifecycle_projection")
            if projection != KANBAN_LIFECYCLE_PROJECTION:
                reasons.append("lifecycle_projection_missing")
            elif board_fields.get("lifecycle_state") != lifecycle_state:
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
                     "recovery_command": ((f"yy merge project {current_id}"
                                            if lifecycle_state == "MERGED"
                                            else KANBAN_SYNC_RECOVERY.format(task=current_id))
                                           if reasons else None)})
    if read_state(controller) != state:
        coverage.update(complete=False, reason="lifecycle_snapshot_changed")
    return {"schema_version": KANBAN_SYNC_SCHEMA, "task_id": task_id,
            "rows": rows, "coverage": coverage,
            "summary": {"examined": len(rows), "drift": drift,
                        "agree": len(rows) - drift},
            "outcome": ("incomplete" if not coverage["complete"] else
                        "drift" if drift else "agree")}


def start(controller: Path, task_id: str, requested_paths: Optional[list[str]] = None,
          umbrella_input: Optional[Path] = None,
          lease_token: Optional[str] = None) -> dict[str, Any]:
    config = load_config(controller)
    require_task(controller, task_id)
    repository = product_repository(controller, config)
    target_sha = ref_sha(repository, config["target_ref"])
    requested_paths = requested_paths or []
    allowed_paths, selected_entries = selected_task_paths(config, repository, target_sha, requested_paths)
    checkpoint_contract = delivery_checkpoint_contract(controller, task_id)
    if checkpoint_contract is not None and not requested_paths:
        raise TaskWorkspaceError(
            "ordinary delivery checkpoints require explicit exact --path scope at task start")
    umbrella_admission = None
    provisional_state = read_state(controller)
    if umbrella_input is not None and not config["legacy_umbrella_creation"]:
        raise TaskWorkspaceError(
            "new umbrella execution is retired; declare ordered [delivery_checkpoints] on one ordinary task")
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
        delivery_owners = delivery_tracking_owners(state)
        reserved_owner = tracking_owner(state, task_id)
        start_admission = decisions.plan_command_transition(
            decisions.CommandRequest("start", task_id),
            decisions.TaskSnapshot(task_id, None, reserved_owner))
        if not start_admission.admitted:
            raise TaskWorkspaceError(start_admission.finding.message)
        if delivery_checkpoint_contract(controller, task_id) != checkpoint_contract:
            raise TaskWorkspaceError("delivery checkpoint requirements changed before task start mutation")
        if checkpoint_contract is not None:
            conflicts = {child: tracking_owner(state, child)
                         for child in checkpoint_contract["tracking_task_ids"]
                         if tracking_owner(state, child) not in (None, task_id)}
            if conflicts:
                raise TaskWorkspaceError(
                    "delivery tracking task already has a lifecycle owner: "
                    + ", ".join(f"{child}={owner}" for child, owner in sorted(conflicts.items())))
            for child in checkpoint_contract["tracking_task_ids"]:
                _child_path, child_body = task_manifest(controller, child)
                if task_status(child_body, child) not in PRESTART_TRACKING_STATUSES:
                    raise TaskWorkspaceError(
                        f"delivery tracking task {child} is not in a pre-start reporting state")
                child_record = state["tasks"].get(child)
                if isinstance(child_record, dict):
                    raise TaskWorkspaceError(
                        f"delivery tracking task {child} already has independent lifecycle state")
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
            if existing.get("schema_version") == TERMINAL_TOMBSTONE_SCHEMA:
                raise TaskWorkspaceError(
                    f"task {task_id} is terminal ({existing.get('state')}); use explicit cold archive lookup for full evidence")
            _require_lease_fence(controller, "start", task_id, lease_token, record=existing)
            receipt = existing.get("creation_receipt", {})
            if receipt.get("requested_paths", []) != requested_paths:
                raise TaskWorkspaceError("task start required paths differ from the frozen creation receipt")
            if receipt.get("delivery_checkpoint_contract") != checkpoint_contract:
                raise TaskWorkspaceError("task delivery checkpoint requirements differ from the frozen creation receipt")
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
            if checkpoint_contract is not None:
                creation_receipt["delivery_checkpoint_contract"] = checkpoint_contract
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
            if checkpoint_contract is not None:
                for child_id in checkpoint_contract["tracking_task_ids"]:
                    delivery_owners[child_id] = task_id
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
            "lease_note": "store this fencing token privately; it remains valid after this "
            "command exits until the attempt is superseded or terminated. At the unchanged "
            f"clean base: yy task start {task_id} --lease-token <returned-token>; "
            "pass the same token to subsequent gated commands such as finish. "
            "Do not issue a successor merely because the helper exited; never log the token"}


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
    historical_admission = receipt.get("umbrella_admission")
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
    if historical_admission is not None:
        if (historical_admission != admission
                or receipt.get("generated_output_admission") != generated):
            raise TaskWorkspaceError(
                "historical umbrella admission differs from verified conversion input")
        drift = umbrella_drift(controller, repository, historical_admission,
                               generated, state, task_id)
        if drift:
            raise TaskWorkspaceError(
                "historical umbrella admission drifted before conversion: "
                + json.dumps(drift, sort_keys=True))
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


def _legacy_delivery_conversion(task_id: str,
                                admission: dict[str, Any]) -> dict[str, Any]:
    bindings = admission.get("child_bindings", [])
    checkpoints: list[dict[str, Any]] = []
    for index, binding in enumerate(bindings):
        child_id = binding["task_id"]
        requirement = f"Preserved canonical requirements for reporting task {child_id}"
        final = index == len(bindings) - 1
        checkpoints.append({
            "id": child_id, "requirement": requirement, "final": final,
            "requirement_sha256": stable_sha256({
                "id": child_id, "requirement": requirement, "final": final}),
        })
    body = {"schema_version": DELIVERY_CHECKPOINT_CONTRACT_SCHEMA,
            "task_id": task_id,
            "tracking_task_ids": admission["ordered_child_ids"],
            "checkpoints": checkpoints, "source": "verified_legacy_conversion",
            "legacy_admission_sha256": stable_sha256(admission)}
    return {**body, "contract_sha256": stable_sha256(body)}


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
        delivery_owners = delivery_tracking_owners(state)
        conversion_contract = _legacy_delivery_conversion(
            task_id, plan["umbrella_admission"])
        for child_id in plan["umbrella_admission"]["ordered_child_ids"]:
            if reservations.get(child_id) not in {None, task_id}:
                raise TaskWorkspaceError(f"child ownership changed before recovery apply: {child_id}")
            if delivery_owners.get(child_id) not in {None, task_id}:
                raise TaskWorkspaceError(f"delivery tracking ownership changed before recovery apply: {child_id}")
            reservations[child_id] = task_id
            delivery_owners[child_id] = task_id
        conversion = {
            "schema_version": "juno_task_umbrella_to_delivery_conversion.v1",
            "contract": conversion_contract,
            "reviewed_plan_sha256": plan_sha,
            "authorization_receipt_sha256": authorization_file_sha,
            "predecessor_receipt_sha256": plan["predecessor_receipt_sha256"],
            "preserved_prior_changed_paths": plan["prior_changed_paths"],
            "preserved_prior_commit_history": plan["prior_commit_history"],
            "activation": "fixture_or_drain_only; live activation requires release coordination",
        }
        updated = {**record, "admission_supersessions": [supersession],
                   "admission_supersession_sha256": stable_sha256(supersession),
                   "delivery_conversion": conversion,
                   "delivery_checkpoint_progress": []}
        state["tasks"][task_id] = updated; write_state(controller, state)
    return {**updated, "outcome": "applied", "admission_status": "authorized_superseding"}


def verify_umbrella_recovery(controller: Path, task_id: str, plan_path: Path,
                             input_path: Path, authorization_path: Path) -> dict[str, Any]:
    """Verify one finite legacy conversion without changing controller bytes."""
    authorization_path = authorization_path.expanduser().resolve()
    canonical = (controller / ".juno_task/receipts/task-admission-authorizations").resolve()
    try:
        authorization_path.relative_to(canonical)
    except ValueError as exc:
        raise TaskWorkspaceError(
            "authorization receipt is not in the canonical immutable controller receipt root") from exc
    plan, plan_file_sha = read_json_object(plan_path, "umbrella recovery plan")
    authorization, authorization_file_sha = read_json_object(
        authorization_path, "umbrella recovery authorization")
    plan_sha = stable_sha256(plan)
    if (plan.get("schema_version") != UMBRELLA_RECOVERY_PLAN_SCHEMA
            or plan.get("task_id") != task_id
            or authorization.get("schema_version") != UMBRELLA_AUTHORIZATION_SCHEMA
            or authorization.get("task_id") != task_id
            or authorization.get("action") != "supersede_umbrella_admission"
            or authorization.get("plan_sha256") != plan_sha
            or authorization.get("plan_file_sha256") != plan_file_sha
            or authorization.get("predecessor_receipt_sha256")
                != plan.get("predecessor_receipt_sha256")):
        raise TaskWorkspaceError(
            "legacy conversion verification inputs do not bind one exact reviewed plan")
    config = load_config(controller)
    repository = product_repository(controller, config)
    with state_lock(controller):
        state = read_state(controller)
        expected = _recovery_plan_locked(
            controller, task_id, input_path, config, repository, state)
        if expected != plan:
            raise TaskWorkspaceError(
                "legacy conversion verification plan is stale or an immutable input changed")
        record = state["tasks"].get(task_id)
        issued = authorization_ledger(state).get(authorization.get("authorization_id"))
        if (not isinstance(record, dict) or not isinstance(issued, dict)
                or issued.get("path") != str(authorization_path)
                or issued.get("sha256") != authorization_file_sha
                or issued.get("plan_sha256") != plan_sha
                or issued.get("plan_file_sha256") != plan_file_sha):
            raise TaskWorkspaceError(
                "legacy conversion authorization is not bound by the trusted controller ledger")
        supersessions = record.get("admission_supersessions", [])
        if (len(supersessions) != 1
                or stable_sha256(supersessions[0])
                    != record.get("admission_supersession_sha256")
                or supersessions[0].get("reviewed_plan_sha256") != plan_sha
                or supersessions[0].get("authorization_receipt", {}).get("sha256")
                    != authorization_file_sha):
            raise TaskWorkspaceError("legacy conversion supersession identity drifted")
        expected_contract = _legacy_delivery_conversion(
            task_id, plan["umbrella_admission"])
        conversion = record.get("delivery_conversion")
        if (not isinstance(conversion, dict)
                or conversion.get("schema_version")
                    != "juno_task_umbrella_to_delivery_conversion.v1"
                or conversion.get("contract") != expected_contract
                or conversion.get("reviewed_plan_sha256") != plan_sha
                or conversion.get("authorization_receipt_sha256")
                    != authorization_file_sha
                or conversion.get("predecessor_receipt_sha256")
                    != plan["predecessor_receipt_sha256"]
                or conversion.get("preserved_prior_changed_paths")
                    != plan["prior_changed_paths"]
                or conversion.get("preserved_prior_commit_history")
                    != plan["prior_commit_history"]):
            raise TaskWorkspaceError("legacy delivery conversion identity drifted")
        reservations = child_reservations(state)
        owners = delivery_tracking_owners(state)
        children = plan["umbrella_admission"]["ordered_child_ids"]
        if any(reservations.get(child) != task_id or owners.get(child) != task_id
               for child in children):
            raise TaskWorkspaceError("legacy reporting ownership drifted after conversion")
    return {
        "schema_version": LEGACY_DELIVERY_VERIFICATION_SCHEMA,
        "task_id": task_id,
        "outcome": "verified",
        "plan_sha256": plan_sha,
        "plan_file_sha256": plan_file_sha,
        "authorization_receipt_sha256": authorization_file_sha,
        "predecessor_receipt_sha256": plan["predecessor_receipt_sha256"],
        "conversion_contract_sha256": expected_contract["contract_sha256"],
        "preserved_prior_changed_paths_sha256": stable_sha256(plan["prior_changed_paths"]),
        "preserved_prior_commit_history_sha256": stable_sha256(plan["prior_commit_history"]),
        "mutation": False,
    }


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


def _admission_from_observation(record: dict[str, Any], repository: Path,
                                config: dict[str, Any], task_id: str, head: str,
                                dirty: list[str]) -> dict[str, Any]:
    """Classify one already-observed task identity without refreezing Git."""
    allowed, generated, source = effective_admission(record)
    projection = path_origin_projection(
        repository, record["base_sha"], head,
        ref_sha(repository, config["target_ref"]), head, [], generated)
    authored = projection["authored_paths"]
    refused = sorted({path for path in authored + dirty
                      if path_within(path, config["controller_private_paths"])
                      or not path_within(path, allowed)} | set(projection["ambiguous_paths"]))
    result = {"schema_version": "juno_task_admission_check.v1", "task_id": task_id,
              "base_sha": record["base_sha"], "tip_sha": head,
              "admission_source": source, "authored_paths": authored,
              "dirty_paths": dirty, "origin_projection": projection,
              "refused_paths": refused,
              "recovery": "explicitly replan exact paths, then start a supported successor"}
    if refused:
        raise TaskWorkspaceError(
            f"early exact admission refused; disallowed paths: {', '.join(refused)}; "
            f"origin=authored-or-ambiguous; admission_source={source}; "
            "explicitly replan exact paths before continuing")
    return {**result, "outcome": "admitted"}


def task_admission_check(controller: Path, task_id: str) -> dict[str, Any]:
    """Deterministic read-only dirty/committed exact admission check."""
    config = load_config(controller)
    repository = product_repository(controller, config)
    with state_lock(controller):
        record = json.loads(json.dumps(read_state(controller)["tasks"].get(task_id)))
    if not isinstance(record, dict):
        raise TaskWorkspaceError("task has not been started")
    verify_hydration_evidence(record, Path(record["worktree"]))
    _repo, _worktree, head, _committed, dirty = observe_task_diff(
        record, repository, config, task_id)
    return _admission_from_observation(
        record, repository, config, task_id, head, dirty)


def _submission_origin_identity(projection: dict[str, Any]) -> str:
    relevant = []
    for row in projection.get("paths", []):
        origins = set(row.get("origins", []))
        if origins & {"authored", "generated", "ambiguous-legacy-admission"}:
            relevant.append({key: row.get(key) for key in
                             ("path", "origins", "base_blob", "source_blob", "candidate_blob")})
    return stable_sha256({
        "schema_version": projection.get("schema_version"),
        "authored_paths": projection.get("authored_paths", []),
        "target_derived_paths": projection.get("target_derived_paths", []),
        "generated_paths": projection.get("generated_paths", []),
        "ambiguous_paths": projection.get("ambiguous_paths", []),
        "paths": relevant,
    })


def review_ready_closure(controller: Path, config: dict[str, Any], record: dict[str, Any],
                         configured_repository: Path, task_id: str,
                         runtime: dict[str, Any]) -> tuple[
                             Path, Path, str, list[str], dict[str, Any]]:
    """Validate the cheap finish boundary and bind it as one immutable closure."""
    verify_hydration_evidence(record, Path(record["worktree"]))
    _verify_dependency_tree(Path(record["worktree"]), config, record.get("hydration"))
    repository, worktree, head, changed = observe_working_task(
        record, configured_repository, config, task_id
    )
    admission_check = _admission_from_observation(
        record, repository, config, task_id, head, [])
    changed = admission_check["authored_paths"]
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
    requirements = canonical_requirement_identity(controller, task_id)
    dependency_evidence = validation_dependency_evidence(worktree, config)
    validation_selection = validation_profile_selection(config, changed)
    validation_rows = selected_standing_rows(config, changed)
    tree_sha = git(repository, "rev-parse", f"{head}^{{tree}}")
    submission_body = {
        "task_id": task_id,
        "requirements_sha256": requirements["requirements_sha256"],
        "admitted_scope_sha256": stable_sha256(frozen_allowed),
        "generated_scope_sha256": stable_sha256(frozen_generated_admission),
        "base_sha": record["base_sha"], "tip_sha": head, "tree_sha": tree_sha,
        "origin_projection_sha256": _submission_origin_identity(
            admission_check["origin_projection"]),
        "hydration_sha256": stable_sha256({
            "workflow": record.get("creation_receipt", {}).get("hydration_workflow"),
            "manifest_sha256": record.get("hydration", {}).get("manifest_sha256"),
            "content_manifest_sha256": record.get("hydration", {}).get(
                "content_manifest", {}).get("sha256")}),
        "dependency_sha256": stable_sha256(dependency_evidence),
        "runtime_sha256": runtime["running_sha256"],
        "validation_sha256": stable_sha256({
            "selection": validation_selection, "commands": validation_rows,
            "documentation_policy": config.get("documentation_validation", {})}),
        "risk_sha256": stable_sha256({
            "policy_sha256": policy_sha256,
            "task_risk_flags": record.get("risk_flags", [])}),
    }
    submission = {**submission_body,
                  "submission_sha256": stable_sha256(submission_body)}
    closure_body = {
        "schema_version": "juno_task_review_ready_closure.v1",
        "task_id": task_id,
        "base_sha": record["base_sha"],
        "tip_sha": head,
        "tree_sha": tree_sha,
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
        "origin_projection": admission_check["origin_projection"],
        "requirements": requirements,
        "dependency_evidence": dependency_evidence,
        "validation_selection": validation_selection,
        "submission": submission,
    }
    closure = {**closure_body, "closure_sha256": stable_sha256(closure_body)}
    return repository, worktree, head, changed, closure


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
                              runtime: dict[str, Any], documentation: dict[str, Any],
                              submission: dict[str, Any]) -> dict[str, Any]:
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
            "submission": {key: value for key, value in submission.items()
                           if key != "submission_sha256"},
            "discovery": {"complete": True, "kind": "exact-import-closure"}}


def standing_checkpoint(controller: Path, task_id: str,
                        lease_token: Optional[str] = None,
                        submission_closure: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    config = load_config(controller)
    require_task(controller, task_id)
    repository = product_repository(controller, config)
    with state_lock(controller):
        state = read_state(controller)
        record = state["tasks"].get(task_id)
        _require_lease_fence(controller, "checkpoint", task_id, lease_token, record=record)
        checkpoint_admission = decisions.plan_command_transition(
            decisions.CommandRequest("checkpoint", task_id),
            decisions.TaskSnapshot(
                task_id,
                None if not isinstance(record, dict) else record.get("state"),
                tracking_owner(state, task_id)))
        if not checkpoint_admission.admitted:
            raise TaskWorkspaceError(checkpoint_admission.finding.message)
        frozen = json.loads(json.dumps(record))
    # State/fence refusal precedes runtime resolution and all validation planning.
    runtime = require_current_runtime(repository, ref_sha(repository, config["target_ref"]), controller)
    if submission_closure is None:
        _repo, worktree, head, changed, submission_closure = review_ready_closure(
            controller, config, frozen, repository, task_id, runtime)
    else:
        worktree = Path(frozen["worktree"])
        head = submission_closure.get("tip_sha")
        changed = submission_closure.get("changed_paths")
        submission = submission_closure.get("submission")
        body = {key: value for key, value in submission_closure.items()
                if key != "closure_sha256"}
        if (submission_closure.get("schema_version") != "juno_task_review_ready_closure.v1"
                or submission_closure.get("closure_sha256") != stable_sha256(body)
                or not isinstance(submission, dict)
                or submission.get("submission_sha256") != stable_sha256({
                    key: value for key, value in submission.items()
                    if key != "submission_sha256"})):
            raise TaskWorkspaceError("immutable task submission is malformed or tampered")
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
        config, runtime, documentation, submission_closure["submission"]))
    body = {"schema_version": STANDING_PLAN_SCHEMA, "task_id": task_id,
            "base_sha": frozen["base_sha"], "tip_sha": head,
            "tree_sha": git(repository, "rev-parse", f"{head}^{{tree}}"),
            "branch_ref": frozen["branch_ref"], "changed_paths": changed,
            "submission": submission_closure["submission"],
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
    verify_hydration_evidence(record, Path(record["worktree"]))
    _repo, worktree, head, changed = observe_working_task(record, repository, config, task_id)
    changed = _admission_from_observation(
        record, repository, config, task_id, head, [])["authored_paths"]
    if head != plan["tip_sha"] or changed != plan["changed_paths"]:
        raise TaskWorkspaceError("standing checkpoint is stale; create a new task checkpoint")
    runtime = require_current_runtime(repository, ref_sha(repository, config["target_ref"]), controller)
    current_planned = [{**entry, "input_closure": _command_input_closure(
        repository, head, entry["command"], config, runtime)} for entry in plan["commands"]]
    current_snapshot = compile_standing_operation_snapshot(**_standing_snapshot_inputs(
        repository, head, ref_sha(repository, config["target_ref"]), current_planned,
        config, runtime, plan["documentation_route"], plan["submission"]))
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
            execute = entry.action not in {
                decisions.ACTION_REUSE, decisions.ACTION_FAILURE_STANDS}
            if entry.action == decisions.ACTION_INVALIDATE:
                invalidated += 1
                decision_log.append(lifecycle_runtime.evidence_decision(
                    row["id"], "invalidated", closure=closure,
                    invalidation=entry.invalidation,
                    reason="legacy readiness wrapper retired; canonical command identity is unchanged"))
            legacy = None
            if receipt is not None:
                legacy = (receipt, {"path": str(base_receipt_path),
                                    "sha256": hashlib.sha256(base_receipt_path.read_bytes()).hexdigest()})
            cwd = (worktree / row["cwd"]).resolve()
            try: cwd.relative_to(worktree)
            except ValueError as exc:
                raise TaskWorkspaceError("standing validation cwd escaped task worktree") from exc

            def execute_terminal() -> dict[str, Any]:
                return (_active_documentation_validation(
                            repository, head, plan, row,
                            config["documentation_validation"])
                        if row["argv"] == lifecycle_runtime.ACTIVE_DOC_ARGV
                        else run_validation(row, cwd))

            try:
                terminal = lifecycle_runtime.consume_or_execute_command_result(
                    controller / CANONICAL_VALIDATION_ROOT, repository, closure,
                    execute_terminal, phase="task_closure", task_id=task_id,
                    legacy=legacy)
            except lifecycle_runtime.LifecycleContractError as exc:
                raise TaskWorkspaceError(str(exc)) from exc
            receipt = terminal["receipt"]
            reference = terminal["reference"]
            receipt_path = Path(reference["path"])
            actual = terminal["decision"]
            if actual == "executed":
                executed += 1
                active_wall_ms += max(0, int(
                    receipt["result"].get("timing", {}).get(
                        "wall_duration_ms", receipt["result"].get("duration_ms", 0))))
            else:
                reused += 1
            decision_log.append(lifecycle_runtime.evidence_decision(
                row["id"], actual, closure=closure, source=reference,
                reason="canonical terminal command result"))
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


def _frozen_delivery_contract(record: dict[str, Any]) -> Optional[dict[str, Any]]:
    value = record.get("creation_receipt", {}).get("delivery_checkpoint_contract")
    if value is None and isinstance(record.get("delivery_conversion"), dict):
        value = record["delivery_conversion"].get("contract")
    if value is None:
        return None
    body = {key: item for key, item in value.items() if key != "contract_sha256"}
    if (not isinstance(value, dict)
            or value.get("schema_version") != DELIVERY_CHECKPOINT_CONTRACT_SCHEMA
            or value.get("contract_sha256") != stable_sha256(body)):
        raise TaskWorkspaceError("frozen delivery checkpoint contract is malformed")
    return value


def delivery_checkpoint_projection(controller: Path, task_id: str,
                                   record: dict[str, Any]) -> Optional[dict[str, Any]]:
    contract = _frozen_delivery_contract(record)
    if contract is None:
        return None
    if (contract.get("source") == "ordinary_task_requirements"
            and delivery_checkpoint_contract(controller, task_id) != contract):
        raise TaskWorkspaceError("authored delivery checkpoint requirements drifted")
    progress = record.get("delivery_checkpoint_progress", [])
    if not isinstance(progress, list):
        raise TaskWorkspaceError("delivery checkpoint evidence is malformed")
    expected = contract["checkpoints"]
    if len(progress) > len(expected):
        raise TaskWorkspaceError("delivery checkpoint evidence exceeds the frozen requirement order")
    previous_tip = record.get("base_sha")
    for index, evidence in enumerate(progress):
        requirement = expected[index]
        body = {key: value for key, value in evidence.items() if key != "evidence_sha256"}
        if (not isinstance(evidence, dict)
                or evidence.get("schema_version") != DELIVERY_CHECKPOINT_EVIDENCE_SCHEMA
                or evidence.get("task_id") != task_id
                or evidence.get("checkpoint_id") != requirement["id"]
                or evidence.get("requirement_sha256") != requirement["requirement_sha256"]
                or evidence.get("base_sha") != previous_tip
                or evidence.get("evidence_sha256") != stable_sha256(body)):
            raise TaskWorkspaceError("delivery checkpoint evidence is malformed or out of order")
        previous_tip = evidence.get("tip_sha")
    completed = [row["checkpoint_id"] for row in progress]
    remaining = [row["id"] for row in expected[len(progress):]]
    return {"schema_version": DELIVERY_CHECKPOINT_CONTRACT_SCHEMA,
            "contract_sha256": contract["contract_sha256"],
            "completed_checkpoint_ids": completed,
            "current_checkpoint_id": remaining[0] if remaining else None,
            "remaining_checkpoint_ids": remaining,
            "final_accepted": len(progress) == len(expected),
            "implementation_state": "IMPLEMENTED" if len(progress) == len(expected) else "IN_PROGRESS",
            "integration_state": "INTEGRATED" if record.get("state") == "MERGED" else "NOT_INTEGRATED",
            "tracking_task_ids": contract["tracking_task_ids"],
            "evidence": progress}


def require_complete_delivery_acceptance(controller: Path, task_id: str,
                                         record: dict[str, Any], tip_sha: str) -> Optional[dict[str, Any]]:
    projection = delivery_checkpoint_projection(controller, task_id, record)
    if projection is None:
        return None
    if not projection["final_accepted"]:
        missing = ", ".join(projection["remaining_checkpoint_ids"])
        raise TaskWorkspaceError(f"delivery checkpoints are incomplete: {missing}")
    final = projection["evidence"][-1]
    if final.get("tip_sha") != tip_sha or not final.get("final"):
        raise TaskWorkspaceError("final cumulative delivery acceptance does not bind the submitted tip")
    return projection


def bind_delivery_acceptance(closure: dict[str, Any],
                             projection: Optional[dict[str, Any]]) -> dict[str, Any]:
    if projection is None:
        return closure
    body = {key: value for key, value in closure.items() if key != "closure_sha256"}
    body["delivery_acceptance"] = projection
    return {**body, "closure_sha256": stable_sha256(body)}


def accept_delivery_checkpoint(controller: Path, task_id: str, checkpoint_id: str,
                               lease_token: Optional[str] = None) -> dict[str, Any]:
    """Validate and record one ordered checkpoint on the ordinary task."""
    if not DELIVERY_CHECKPOINT_ID_RE.fullmatch(checkpoint_id):
        raise TaskWorkspaceError("unsafe delivery checkpoint id")
    config = load_config(controller)
    repository = product_repository(controller, config)
    with state_lock(controller):
        state = read_state(controller)
        record = state["tasks"].get(task_id)
        _require_lease_fence(controller, "checkpoint", task_id, lease_token, record=record)
        if not isinstance(record, dict) or record.get("state") != "WORKING":
            raise TaskWorkspaceError("delivery checkpoint requires a WORKING task")
        projection = delivery_checkpoint_projection(controller, task_id, record)
        if projection is None:
            raise TaskWorkspaceError("task has no frozen delivery checkpoint requirements")
        progress = projection["evidence"]
        if checkpoint_id in projection["completed_checkpoint_ids"]:
            existing = progress[projection["completed_checkpoint_ids"].index(checkpoint_id)]
            live_tip = git(Path(record["worktree"]), "rev-parse", "HEAD")
            if existing["tip_sha"] == live_tip:
                return {"outcome": "delivery_checkpoint_already_accepted",
                        "checkpoint": existing, "projection": projection}
            raise TaskWorkspaceError("accepted delivery checkpoint cannot be rewritten")
        if projection["current_checkpoint_id"] != checkpoint_id:
            raise TaskWorkspaceError(
                f"delivery checkpoint is out of order; expected {projection['current_checkpoint_id']}")
        frozen = json.loads(json.dumps(record))
    runtime = require_current_runtime(repository, ref_sha(repository, config["target_ref"]), controller)
    _repo, worktree, head, changed, closure = review_ready_closure(
        controller, config, frozen, repository, task_id, runtime)
    plan = standing_checkpoint(controller, task_id, lease_token, submission_closure=closure)
    evidence = standing_evidence_run(controller, task_id, raise_on_failure=False,
                                     lease_token=lease_token)
    if evidence.get("outcome") != "PASSED" or evidence.get("plan_sha256") != plan.get("plan_sha256"):
        raise TaskWorkspaceError("delivery checkpoint evidence is not a complete passing result")
    contract = _frozen_delivery_contract(frozen)
    requirement = contract["checkpoints"][len(frozen.get("delivery_checkpoint_progress", []))]
    previous_tip = (frozen.get("delivery_checkpoint_progress") or [{}])[-1].get(
        "tip_sha", frozen["base_sha"])
    if run(["git", "-C", str(repository), "merge-base", "--is-ancestor",
            previous_tip, head], repository, check=False).returncode != 0:
        raise TaskWorkspaceError("delivery checkpoint tip does not descend from prior checkpoint evidence")
    entry_body = {
        "schema_version": DELIVERY_CHECKPOINT_EVIDENCE_SCHEMA,
        "task_id": task_id, "checkpoint_id": checkpoint_id,
        "requirement_sha256": requirement["requirement_sha256"],
        "base_sha": previous_tip, "tip_sha": head,
        "tree_sha": git(repository, "rev-parse", f"{head}^{{tree}}"),
        "final": requirement["final"],
        "cumulative_changed_paths": changed,
        "submission_sha256": closure["submission"]["submission_sha256"],
        "validation_plan_sha256": evidence["plan_sha256"],
        "validation_summary_sha256": stable_sha256(evidence),
        "validation_receipts": evidence["receipts"],
        "recorded_at_unix_ns": time.time_ns(),
    }
    entry = {**entry_body, "evidence_sha256": stable_sha256(entry_body)}
    with state_lock(controller):
        state = read_state(controller)
        current = state["tasks"].get(task_id)
        if current != frozen:
            raise TaskWorkspaceError("task state changed while checkpoint evidence was produced")
        if (git(worktree, "rev-parse", "HEAD") != head
                or git(worktree, "status", "--porcelain=v1", "--untracked-files=all")):
            raise TaskWorkspaceError("task tip or worktree changed before checkpoint mutation")
        current.setdefault("delivery_checkpoint_progress", []).append(entry)
        state["tasks"][task_id] = current
        write_state(controller, state)
    return {"outcome": "delivery_checkpoint_accepted", "checkpoint": entry,
            "projection": delivery_checkpoint_projection(controller, task_id, current)}


def _submission_receipt(controller: Path, task_id: str,
                        closure: dict[str, Any]) -> dict[str, str]:
    submission = closure.get("submission")
    if (not isinstance(submission, dict)
            or submission.get("submission_sha256") != stable_sha256({
                key: value for key, value in submission.items()
                if key != "submission_sha256"})):
        raise TaskWorkspaceError("immutable task submission is malformed")
    path = (controller / SUBMISSION_ROOT / task_id
            / f"{submission['submission_sha256']}.json")
    data = (json.dumps(closure, sort_keys=True, separators=(",", ":")) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        try:
            existing = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise TaskWorkspaceError("immutable task submission receipt is malformed") from exc
        existing_body = ({key: value for key, value in existing.items()
                          if key != "closure_sha256"}
                         if isinstance(existing, dict) else {})
        existing_submission = existing.get("submission") if isinstance(existing, dict) else None
        if (not isinstance(existing_submission, dict)
                or existing.get("schema_version") != "juno_task_review_ready_closure.v1"
                or existing.get("closure_sha256") != stable_sha256(existing_body)
                or existing_submission != submission):
            raise TaskWorkspaceError("immutable task submission receipt is tampered or collided")
    else:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
    return {"path": str(path.resolve()),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "submission_sha256": submission["submission_sha256"]}


def preflight(controller: Path, task_id: str) -> dict[str, Any]:
    """Run finish identity/admission checks without validation or queue mutation."""
    if not TASK_RE.fullmatch(task_id):
        raise TaskWorkspaceError("unsafe task id")
    config = load_config(controller)
    require_task(controller, task_id)
    configured_repository = product_repository(controller, config)
    with state_lock(controller):
        state = read_state(controller)
        record = state["tasks"].get(task_id)
        preflight_admission = decisions.plan_command_transition(
            decisions.CommandRequest("preflight", task_id),
            decisions.TaskSnapshot(
                task_id,
                None if not isinstance(record, dict) else record.get("state"),
                tracking_owner(state, task_id)))
        if not preflight_admission.admitted:
            raise TaskWorkspaceError(preflight_admission.finding.message)
        frozen_record = json.loads(json.dumps(record))
    # State admission is intentionally cheaper than runtime/Git identity work.
    runtime = require_current_runtime(configured_repository,
                                      ref_sha(configured_repository, config["target_ref"]),
                                      controller)
    _, worktree, head, changed, closure = review_ready_closure(
        controller, config, frozen_record, configured_repository, task_id, runtime
    )
    if load_config(controller) != config:
        raise TaskWorkspaceError("task workspace policy changed during preflight")
    delivery_acceptance = require_complete_delivery_acceptance(
        controller, task_id, frozen_record, head)
    closure = bind_delivery_acceptance(closure, delivery_acceptance)
    receipt = _submission_receipt(controller, task_id, closure)
    return {"schema_version": RECORD_SCHEMA, "task_id": task_id, "state": "WORKING",
            "outcome": "preflight_passed", "worktree": str(worktree), "tip_sha": head,
            "changed_paths": changed, "submission_receipt": receipt,
            "review_ready_closure": closure}


def _finish_once(controller: Path, task_id: str,
                 lease_token: Optional[str] = None) -> dict[str, Any]:
    config = load_config(controller)
    require_task(controller, task_id)
    configured_repository = product_repository(controller, config)
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
                tracking_owner(state, task_id)))
        if not finish_admission.admitted:
            raise TaskWorkspaceError(finish_admission.finding.message)
        if finish_admission.idempotent:
            queued_record = record
        else:
            frozen_record = json.loads(json.dumps(record))
    # Never pay runtime/Git validation cost for a state- or fence-ineligible finish.
    runtime = require_current_runtime(configured_repository,
                                      ref_sha(configured_repository, config["target_ref"]),
                                      controller)
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
                f"{queued_head}; create a new task for a descendant correction or "
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
    # Validations run outside the controller state lock. Independent feature
    # finishes therefore stay concurrent; the compare below prevents stale state.
    repository, worktree, head, changed, closure = review_ready_closure(
        controller, config, frozen_record, configured_repository, task_id, runtime
    )
    delivery_acceptance = require_complete_delivery_acceptance(
        controller, task_id, frozen_record, head)
    closure = bind_delivery_acceptance(closure, delivery_acceptance)
    submission_receipt = _submission_receipt(controller, task_id, closure)
    _frozen_allowed, frozen_generated_admission, _admission_source = effective_admission(
        frozen_record)
    frozen_umbrella = (
        frozen_record.get("admission_supersessions", [{}])[-1].get("umbrella_admission")
        if frozen_record.get("admission_supersessions")
        else frozen_record.get("creation_receipt", {}).get("umbrella_admission")
    )
    routing = validation_profile_selection(config, changed)
    checkpoint_plan = standing_checkpoint(
        controller, task_id, lease_token, submission_closure=closure)
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
        _verify_dependency_tree(worktree, config, record.get("hydration"))
        post_repository, post_worktree, post_head, post_changed = observe_working_task(
            record, configured_repository, config, task_id
        )
        post_changed = _admission_from_observation(
            record, post_repository, config, task_id, post_head, [])["authored_paths"]
    except TaskWorkspaceError as exc:
        raise TaskWorkspaceError("task tip or worktree changed during focused validation") from exc
    if ((post_repository, post_worktree, post_head, post_changed)
            != (repository, worktree, head, changed)):
        raise TaskWorkspaceError("task tip or worktree changed during focused validation")
    queued = {**record, "state": "QUEUED", "tip_sha": head, "changed_paths": changed,
              "review_ready_closure": closure,
              "submission_receipt": submission_receipt,
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


_handoff_phase = decisions.handoff_phase


def _task_resume_projection(controller: Path, task_id: str,
                            record: dict[str, Any]) -> dict[str, Any]:
    """Retirement is unconditional; historical readiness never authorizes replay.

    Keep the projection key for readers, but do not reinterpret or rewrite old
    journals. Current deliverable state and ownership are reported by status.
    """
    return {"classification": "retired", "admitted": False,
            "owner_command": f"yy task lease-status {task_id}",
            "restart_stage": None,
            "reason_code": "managed_task_execution_retired",
            "safe_next_action": "Preserve the existing worktree and historical runs. "
            "Verify current ownership before explicit agent continuation; do not "
            "restart, reset attempts, or infer completion from a worker exit."}


def status(controller: Path, task_id: str) -> dict[str, Any]:
    config = load_config(controller)
    require_task(controller, task_id)
    configured_repository = product_repository(controller, config)
    current_target = optional_ref_sha(configured_repository, config["target_ref"])
    generation = runtime_generation(configured_repository, current_target) if current_target else None
    state = read_state(controller)
    record = state["tasks"].get(task_id)
    if not record:
        owner = tracking_owner(state, task_id)
        projection = decisions.status_projection(
            decisions.TaskSnapshot(task_id, None, owner))
        eligibility = decisions.task_mutation_eligibility(
            task_id, None, tracking_owner=owner)
        projected: dict[str, Any] = {
            "schema_version": RECORD_SCHEMA, "task_id": task_id,
            "state": projection.state, "outcome": "status",
            "runtime_generation": generation,
            "producer_fence": {"state": "NONE", "attempt": None,
                               "producer_status": "inactive",
                               "detail": "no active task producer"},
            "mutation_eligibility": {
                "operation": eligibility.operation, "eligible": eligibility.eligible,
                "reason_code": eligibility.reason_code,
                "invalidating_change": eligibility.invalidating_change,
                "safe_next_action": eligibility.safe_next_action,
                "authority_checked_live_by_executor": True},
            "prior_terminal_evidence": None}
        if projection.umbrella_owner_task_id is not None:
            projected["umbrella_owner_task_id"] = projection.umbrella_owner_task_id
            projected["next_action"] = projection.next_action
            if delivery_tracking_owners(state).get(task_id) == projection.umbrella_owner_task_id:
                owner_record = state["tasks"].get(projection.umbrella_owner_task_id, {})
                projected.update({
                    "reporting_only": True,
                    "delivery_owner_task_id": projection.umbrella_owner_task_id,
                    "owner_delivery_state": owner_record.get("state"),
                    "separate_integration": False,
                })
        return projected
    result = {**record, "outcome": "status", "runtime_generation": generation}
    delivery_projection = delivery_checkpoint_projection(controller, task_id, record)
    if delivery_projection is not None:
        result["delivery_checkpoint_status"] = delivery_projection
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
    result["resume_decision"] = _task_resume_projection(controller, task_id, record)
    lease = _lease_view(record)
    observation = (_observe_producer(lease.get("producer"))
                   if isinstance(lease, dict) and lease.get("state") == decisions.LEASE_ACTIVE
                   else decisions.LeaseObservation("inactive", "no active task producer"))
    eligibility = decisions.task_mutation_eligibility(
        task_id, record.get("state"), tracking_owner=tracking_owner(state, task_id))
    result["producer_fence"] = {
        "state": lease.get("state") if isinstance(lease, dict) else "NONE",
        "attempt": lease.get("attempt") if isinstance(lease, dict) else None,
        "producer_status": observation.status, "detail": observation.detail,
        "note": "producer liveness is not token validity; the current token admits manual "
        "gated commands with --lease-token even after the helper exits. "
        "This read-only status does not test a token"}
    result["mutation_eligibility"] = {
        "operation": eligibility.operation, "eligible": eligibility.eligible,
        "reason_code": eligibility.reason_code,
        "invalidating_change": eligibility.invalidating_change,
        "safe_next_action": eligibility.safe_next_action,
        "authority_checked_live_by_executor": True}
    failed_rows = record.get("validation") if isinstance(record.get("validation"), list) else []
    failed = failed_rows[-1] if failed_rows and isinstance(failed_rows[-1], dict) else None
    result["prior_terminal_evidence"] = (
        {key: failed.get(key) for key in
         ("id", "exit_code", "timed_out", "timing", "identity", "log_sha256")}
        if record.get("last_validation_outcome") in {"FAILED", "TIMEOUT"} and failed is not None
        else record.get("prior_queue_failure") or record.get("last_queue_outcome"))
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


def _managed_inventory_records_identity(assets: dict[str, Any]) -> str:
    """Hash records in locale-independent UTF-8 destination order."""
    projected = [{"destination": destination, "type": record.get("type"),
                  "sourceSha256": record.get("sourceSha256"),
                  "installedSha256": record.get("installedSha256")}
                 for destination, record in sorted(
                     assets.items(), key=lambda item: item[0].encode("utf-8"))]
    return hashlib.sha256(json.dumps(
        projected, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


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
    assets_sha = _managed_inventory_records_identity(assets)
    core = {"schemaVersion": identity.get("schemaVersion") if isinstance(identity, dict) else None,
            "semanticVersion": identity.get("semanticVersion") if isinstance(identity, dict) else None,
            "packageVersion": identity.get("packageVersion") if isinstance(identity, dict) else None,
            "assetCount": identity.get("assetCount") if isinstance(identity, dict) else None,
            "assetsSha256": identity.get("assetsSha256") if isinstance(identity, dict) else None}
    bundle_sha = hashlib.sha256(json.dumps(core, separators=(",", ":")).encode()).hexdigest()
    return bool(isinstance(identity, dict)
                and set(identity) == set(core) | {"bundleSha256"}
                and identity.get("schemaVersion") == INSTRUCTION_COMPATIBILITY["identitySchema"]
                and instruction_version_compatible(identity.get("semanticVersion"))
                and identity.get("packageVersion") == inventory["packageVersion"]
                and identity.get("assetCount") == len(assets)
                and identity.get("assetsSha256") == assets_sha
                and identity.get("bundleSha256") == bundle_sha)


def _bind_instruction_bundle_identity(inventory: dict[str, Any]) -> None:
    if inventory.get("schemaVersion") != 2:
        return
    assets = inventory["assets"]
    # Hash rebinding preserves the admitted revision; absence is not a default.
    identity = inventory.get("instructionBundle")
    semantic_version = identity.get("semanticVersion") if isinstance(identity, dict) else None
    if not instruction_version_compatible(semantic_version):
        raise TaskWorkspaceError("unsupported managed instruction bundle version; "
                                 + instruction_compatibility_error())
    core = {"schemaVersion": INSTRUCTION_COMPATIBILITY["identitySchema"], "semanticVersion": semantic_version,
            "packageVersion": inventory["packageVersion"], "assetCount": len(assets),
            "assetsSha256": _managed_inventory_records_identity(assets)}
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
    # Contend on the native delivery adapter's repository/ref lock inode. Runtime
    # recovery and delivery must never mutate the same target concurrently.
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
                               "message": authority.message,
                               "note": "observed without a token; the current token still "
                               "admits manual gated commands after the helper exits"},
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
        "next_command": "yy task status " + str(record.get("task_id")),
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
                "note": "the lease token is shown once; store it privately. It remains valid "
                "after this command exits until the attempt is superseded or terminated. "
                "Retry the original gated command with --lease-token <returned-token>. "
                f"At the unchanged clean base: yy task start {task_id} --lease-token <returned-token>; "
                "pass the same token to subsequent gated commands such as finish. "
                "Tokenless manual retry is not authorized; do not repeat successor when "
                "you have its token. Recovery classification is not hydration or validation "
                "clearance; never log the token"}


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


def _canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _exclusive_json(path: Path, value: dict[str, Any]) -> dict[str, str]:
    data = _canonical_bytes(value)
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise TaskWorkspaceError(f"output already exists: {path}") from exc
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data); stream.flush(); os.fsync(stream.fileno())
    return {"path": str(path), "sha256": hashlib.sha256(data).hexdigest()}


def _require_external_archive_output(controller: Path, path: Path) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(controller.resolve())
    except ValueError:
        return resolved
    raise TaskWorkspaceError("state archive plans and receipts must be outside the repository")


def _state_archive_intent(output: Path, payload: dict[str, Any]) -> dict[str, str]:
    intent = output.with_name(output.name + ".intent.json")
    data = _canonical_bytes(payload)
    if intent.exists():
        if intent.read_bytes() != data:
            raise TaskWorkspaceError("state archive intent receipt already exists with different bytes")
        return {"path": str(intent), "sha256": hashlib.sha256(data).hexdigest()}
    return _exclusive_json(intent, payload)


def _state_archive_material(state: dict[str, Any], source_sha256: str,
                            cold_ref: str, source_identity: Optional[dict[str, Any]] = None
                            ) -> tuple[dict[str, bytes], dict[str, Any], dict[str, Any]]:
    if state.get("schema_version") != STATE_SCHEMA:
        raise TaskWorkspaceError("state archive plan requires the one-cut v1 source schema")
    terminal: list[tuple[str, dict[str, Any], bytes]] = []
    for task_id, record in sorted(state["tasks"].items()):
        if not isinstance(record, dict) or record.get("task_id") != task_id:
            raise TaskWorkspaceError(f"task state record identity is malformed: {task_id}")
        if record.get("state") in TERMINAL_LIFECYCLE_STATES:
            data = _canonical_bytes(record)
            if len(data) > COLD_PACK_EXPANDED_MAX_BYTES:
                raise TaskWorkspaceError(f"terminal task record exceeds expanded pack limit: {task_id}")
            terminal.append((task_id, record, data))
    if not terminal:
        raise TaskWorkspaceError("task state has no full terminal records to compact")
    archive_id = source_sha256[:24]
    files: dict[str, bytes] = {}
    entries: dict[str, dict[str, Any]] = {}
    groups: list[list[tuple[str, dict[str, Any], bytes]]] = []
    current: list[tuple[str, dict[str, Any], bytes]] = []
    current_bytes = 0
    for row in terminal:
        if current and current_bytes + len(row[2]) > COLD_PACK_RAW_TARGET_BYTES:
            groups.append(current); current = []; current_bytes = 0
        current.append(row); current_bytes += len(row[2])
    if current:
        groups.append(current)
    for pack_index, group in enumerate(groups, 1):
        raw = b"".join(row[2] for row in group)
        packed = gzip.compress(raw, compresslevel=9, mtime=0)
        if len(packed) > COLD_PACK_MAX_BYTES:
            raise TaskWorkspaceError(f"cold pack {pack_index} exceeds {COLD_PACK_MAX_BYTES} bytes")
        name = f"task-state/{archive_id}/packs/{pack_index:04d}.ndjson.gz"
        files[name] = packed
        for line_index, (task_id, record, data) in enumerate(group):
            entries[task_id] = {
                "state": record["state"], "pack": name, "line": line_index,
                "record_bytes": len(data), "record_sha256": hashlib.sha256(data).hexdigest(),
            }
    pack_identities = {name: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                       for name, data in sorted(files.items())}
    manifest_body = {
        "schema_version": STATE_ARCHIVE_MANIFEST_SCHEMA,
        "archive_id": archive_id, "source_state_sha256": source_sha256,
        "source_identity": source_identity or {},
        "terminal_states": sorted(TERMINAL_LIFECYCLE_STATES),
        "record_count": len(entries), "pack_count": len(groups),
        "pack_max_bytes": COLD_PACK_MAX_BYTES,
        "expanded_pack_max_bytes": COLD_PACK_EXPANDED_MAX_BYTES,
        "packs": pack_identities, "entries": entries,
    }
    manifest_bytes = _canonical_bytes(manifest_body)
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    manifest_path = f"task-state/{archive_id}/manifest.json"
    files[manifest_path] = manifest_bytes
    tasks: dict[str, Any] = {}
    for task_id, record in state["tasks"].items():
        entry = entries.get(task_id)
        if entry is None:
            tasks[task_id] = record
            continue
        tombstone = {
            "schema_version": TERMINAL_TOMBSTONE_SCHEMA,
            "task_id": task_id, "state": record["state"],
            "tip_sha": record.get("tip_sha"),
            "archive": {"archive_id": archive_id, "cold_ref": cold_ref,
                        "manifest_path": manifest_path, "manifest_sha256": manifest_sha256,
                        "record_sha256": entry["record_sha256"]},
        }
        for key in ("integrated_sha", "last_queue_outcome"):
            if record.get(key) is not None:
                tombstone[key] = record[key]
        tasks[task_id] = tombstone
    compacted = {"schema_version": BOUNDED_STATE_SCHEMA, "tasks": tasks,
                 "queues": state["queues"]}
    return files, manifest_body, compacted


def _state_archive_controller_dirt(controller: Path) -> str:
    return git(controller, "status", "--porcelain=v1", "--untracked-files=all", "--", ".",
               ":(exclude).juno_task/runtime")


@contextmanager
def _state_archive_repository_lock(controller: Path) -> Iterator[None]:
    common = Path(git(controller, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    path = common / "juno-repository-writer.lock"
    handle = path.open("a+b")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise TaskWorkspaceError(f"repository writer lock is busy: {path}") from exc
        yield
    finally:
        handle.close()


def state_archive_plan(controller: Path, output: Path, cold_ref: str) -> dict[str, Any]:
    output = _require_external_archive_output(controller, output)
    if subprocess.run(["git", "check-ref-format", cold_ref], capture_output=True).returncode:
        raise TaskWorkspaceError("cold archive ref is invalid")
    if _state_archive_controller_dirt(controller):
        raise TaskWorkspaceError("state archive planning requires a clean controller")
    path = state_path(controller)
    source_head = git(controller, "rev-parse", "HEAD")
    controller_branch = git(controller, "symbolic-ref", "--quiet", "HEAD")
    data = path.read_bytes()
    committed = subprocess.run(["git", "-C", str(controller), "diff", "--quiet", "HEAD", "--",
                                str(path.relative_to(controller))]).returncode == 0
    if (not committed or source_head != git(controller, "rev-parse", "HEAD")
            or controller_branch != git(controller, "symbolic-ref", "--quiet", "HEAD")
            or _state_archive_controller_dirt(controller)):
        raise TaskWorkspaceError("state archive planning requires stable committed tasks.json bytes")
    state = json.loads(data)
    source_sha256 = hashlib.sha256(data).hexdigest()
    files, manifest, compacted = _state_archive_material(
        state, source_sha256, cold_ref,
        {"controller_branch": controller_branch, "source_head": source_head})
    compacted_bytes = _canonical_bytes(compacted)
    reduction_percent = 100 - (len(compacted_bytes) * 100 / len(data))
    if len(compacted_bytes) > HOT_STATE_TARGET_BYTES or reduction_percent < 90:
        raise TaskWorkspaceError(
            f"projected hot state is {len(compacted_bytes)} bytes with {reduction_percent:.2f}% reduction; "
            f"requires at most {HOT_STATE_TARGET_BYTES} bytes and at least 90% reduction")
    cold_head = git(controller, "rev-parse", "--verify", cold_ref, check=False) or None
    plan_body = {
        "schema_version": STATE_ARCHIVE_PLAN_SCHEMA,
        "controller": str(controller.resolve()),
        "controller_branch": controller_branch,
        "source_head": source_head,
        "source_state_sha256": source_sha256, "source_state_bytes": len(data),
        "cold_ref": cold_ref, "expected_cold_head": cold_head,
        "archive_id": manifest["archive_id"],
        "manifest_sha256": hashlib.sha256(files[f"task-state/{manifest['archive_id']}/manifest.json"]).hexdigest(),
        "pack_sha256": {name: hashlib.sha256(value).hexdigest()
                        for name, value in sorted(files.items()) if name.endswith(".gz")},
        "terminal_task_ids": sorted(manifest["entries"]),
        "terminal_counts": {name: sum(1 for entry in manifest["entries"].values()
                                      if entry["state"] == name)
                            for name in sorted(TERMINAL_LIFECYCLE_STATES)},
        "projected_state_sha256": hashlib.sha256(compacted_bytes).hexdigest(),
        "projected_state_bytes": len(compacted_bytes),
        "projected_reduction_percent": round(reduction_percent, 4),
        "minimum_reduction_percent": 90,
    }
    plan = {**plan_body, "plan_sha256": stable_sha256(plan_body)}
    reference = _exclusive_json(output, plan)
    return {"schema_version": STATE_ARCHIVE_PLAN_SCHEMA, "outcome": "planned",
            "plan": reference, "summary": {key: plan[key] for key in
            ("source_state_bytes", "projected_state_bytes", "terminal_counts", "archive_id")}}


def _load_state_archive_plan(controller: Path, path: Path) -> dict[str, Any]:
    try:
        plan = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise TaskWorkspaceError(f"state archive plan is unreadable: {exc}") from exc
    body = {key: value for key, value in plan.items() if key != "plan_sha256"}
    if (plan.get("schema_version") != STATE_ARCHIVE_PLAN_SCHEMA
            or plan.get("plan_sha256") != stable_sha256(body)
            or plan.get("controller") != str(controller.resolve())):
        raise TaskWorkspaceError("state archive plan identity is invalid")
    return plan


def _git_object_publish(controller: Path, files: dict[str, bytes], cold_ref: str,
                        expected: Optional[str], message: str) -> str:
    current = git(controller, "rev-parse", "--verify", cold_ref, check=False) or None
    if current != expected:
        raise TaskWorkspaceError("cold archive ref drifted from the reviewed plan")
    with tempfile.TemporaryDirectory(prefix="juno-task-state-index-") as directory:
        index = Path(directory) / "index"
        env = {**os.environ, "GIT_INDEX_FILE": str(index)}
        if current:
            tree = git(controller, "rev-parse", f"{current}^{{tree}}")
            subprocess.run(["git", "-C", str(controller), "read-tree", tree], env=env, check=True)
        else:
            subprocess.run(["git", "-C", str(controller), "read-tree", "--empty"], env=env, check=True)
        for name, data in sorted(files.items()):
            blob = subprocess.run(["git", "-C", str(controller), "hash-object", "-w", "--stdin"],
                                  input=data, capture_output=True, env=env, check=True).stdout.decode().strip()
            subprocess.run(["git", "-C", str(controller), "update-index", "--add", "--cacheinfo",
                            "100644", blob, name], env=env, check=True)
        tree = subprocess.run(["git", "-C", str(controller), "write-tree"], env=env,
                              capture_output=True, text=True, check=True).stdout.strip()
    command = ["git", "-C", str(controller), "commit-tree", tree]
    if current:
        command.extend(["-p", current])
    commit_env = {**os.environ, "GIT_AUTHOR_NAME": "YYLO state archive",
                  "GIT_AUTHOR_EMAIL": "state-archive@invalid",
                  "GIT_COMMITTER_NAME": "YYLO state archive",
                  "GIT_COMMITTER_EMAIL": "state-archive@invalid"}
    commit = subprocess.run(command, input=message + "\n", text=True, capture_output=True,
                            env=commit_env, check=True).stdout.strip()
    update = subprocess.run(["git", "-C", str(controller), "update-ref", cold_ref, commit,
                             current or ("0" * len(commit))], capture_output=True, text=True)
    if update.returncode:
        raise TaskWorkspaceError("cold archive ref CAS failed")
    return commit


def _cold_file(controller: Path, ref: str, name: str) -> bytes:
    result = subprocess.run(["git", "-C", str(controller), "show", f"{ref}:{name}"],
                            capture_output=True)
    if result.returncode:
        raise TaskWorkspaceError(f"cold archive object is unavailable: {name}")
    return result.stdout


def state_archive_get(controller: Path, task_id: str, cold_ref: str,
                      manifest_path: Optional[str] = None,
                      tombstone: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    if not TASK_RE.fullmatch(task_id):
        raise TaskWorkspaceError("unsafe task id")
    record = tombstone if tombstone is not None else read_state(controller)["tasks"].get(task_id)
    if not isinstance(record, dict) or record.get("schema_version") != TERMINAL_TOMBSTONE_SCHEMA:
        raise TaskWorkspaceError(f"task {task_id} has no cold terminal tombstone")
    archive = record.get("archive", {})
    if cold_ref != archive.get("cold_ref"):
        raise TaskWorkspaceError("cold ref differs from the tombstone")
    manifest_name = manifest_path or archive.get("manifest_path")
    manifest_data = _cold_file(controller, cold_ref, str(manifest_name))
    if hashlib.sha256(manifest_data).hexdigest() != archive.get("manifest_sha256"):
        raise TaskWorkspaceError("cold archive manifest digest mismatch")
    manifest = json.loads(manifest_data)
    if (manifest.get("schema_version") != STATE_ARCHIVE_MANIFEST_SCHEMA
            or manifest.get("archive_id") != archive.get("archive_id")
            or manifest.get("record_count") != len(manifest.get("entries", {}))):
        raise TaskWorkspaceError("cold archive manifest schema is invalid")
    entry = manifest.get("entries", {}).get(task_id)
    if not isinstance(entry, dict) or entry.get("record_sha256") != archive.get("record_sha256"):
        raise TaskWorkspaceError("cold archive entry is missing or mismatched")
    packed = _cold_file(controller, cold_ref, entry["pack"])
    pack_identity = manifest.get("packs", {}).get(entry["pack"])
    if (not isinstance(pack_identity, dict) or pack_identity.get("bytes") != len(packed)
            or pack_identity.get("sha256") != hashlib.sha256(packed).hexdigest()
            or len(packed) > COLD_PACK_MAX_BYTES):
        raise TaskWorkspaceError("cold archive pack identity is invalid")
    expanded_limit = manifest.get("expanded_pack_max_bytes", COLD_PACK_EXPANDED_MAX_BYTES)
    if not isinstance(expanded_limit, int) or not 1 <= expanded_limit <= COLD_PACK_EXPANDED_MAX_BYTES:
        raise TaskWorkspaceError("cold archive expanded limit is invalid")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(packed), mode="rb") as stream:
            raw = stream.read(expanded_limit + 1)
            trailing = stream.read(1)
    except (OSError, EOFError) as exc:
        raise TaskWorkspaceError("cold archive pack is malformed") from exc
    if len(raw) > expanded_limit or trailing:
        raise TaskWorkspaceError("cold archive pack exceeds expanded limit")
    lines = raw.splitlines(keepends=True)
    line = entry.get("line")
    if not isinstance(line, int) or line < 0 or line >= len(lines):
        raise TaskWorkspaceError("cold archive line index is invalid")
    data = lines[line]
    if hashlib.sha256(data).hexdigest() != entry["record_sha256"]:
        raise TaskWorkspaceError("cold terminal record digest mismatch")
    value = json.loads(data)
    if value.get("task_id") != task_id or value.get("state") != record.get("state"):
        raise TaskWorkspaceError("cold terminal record identity mismatch")
    return {"schema_version": STATE_ARCHIVE_RECEIPT_SCHEMA, "outcome": "retrieved",
            "task_id": task_id, "record": value, "record_sha256": entry["record_sha256"]}


def state_archive_apply(controller: Path, plan_path: Path, output: Path,
                        authorized: bool) -> dict[str, Any]:
    if not authorized:
        raise TaskWorkspaceError("state archive apply requires --authorize-state-compaction")
    output = _require_external_archive_output(controller, output)
    plan = _load_state_archive_plan(controller, plan_path)
    if output.exists():
        existing = json.loads(output.read_text())
        if (existing.get("schema_version") != STATE_ARCHIVE_RECEIPT_SCHEMA
                or existing.get("plan_sha256") != plan["plan_sha256"]
                or existing.get("outcome") not in {"applied", "already_applied"}):
            raise TaskWorkspaceError("state archive apply receipt already exists with different identity")
        state_archive_verify(controller, plan_path)
        return existing
    intent = _state_archive_intent(output, {
        "schema_version": STATE_ARCHIVE_RECEIPT_SCHEMA, "operation": "apply-intent",
        "plan_sha256": plan["plan_sha256"], "source_head": plan["source_head"],
        "source_state_sha256": plan["source_state_sha256"], "cold_ref": plan["cold_ref"],
        "expected_cold_head": plan["expected_cold_head"],
    })
    with _state_archive_repository_lock(controller), state_lock(controller):
        if git(controller, "symbolic-ref", "--quiet", "HEAD") != plan["controller_branch"]:
            raise TaskWorkspaceError("controller branch drifted from the reviewed plan")
        current_data = state_path(controller).read_bytes()
        current_sha = hashlib.sha256(current_data).hexdigest()
        current_cold = git(controller, "rev-parse", "--verify", plan["cold_ref"], check=False) or None
        if current_sha == plan["projected_state_sha256"]:
            state_archive_verify(controller, plan_path)
            receipt = {"schema_version": STATE_ARCHIVE_RECEIPT_SCHEMA,
                       "outcome": "already_applied", "plan_sha256": plan["plan_sha256"],
                       "cold_head": current_cold, "hot_state_sha256": current_sha,
                       "intent": intent}
            _exclusive_json(output, receipt)
            return receipt
        if (git(controller, "rev-parse", "HEAD") != plan["source_head"]
                or current_sha != plan["source_state_sha256"]):
            raise TaskWorkspaceError("state archive source drifted from the reviewed plan")
        if _state_archive_controller_dirt(controller):
            raise TaskWorkspaceError("state archive apply requires a clean controller")
        state = json.loads(current_data)
        files, manifest, compacted = _state_archive_material(
            state, current_sha, plan["cold_ref"],
            {"controller_branch": plan["controller_branch"], "source_head": plan["source_head"]})
        if (hashlib.sha256(files[f"task-state/{manifest['archive_id']}/manifest.json"]).hexdigest()
                != plan["manifest_sha256"]
                or {name: hashlib.sha256(value).hexdigest() for name, value in files.items()
                    if name.endswith(".gz")} != plan["pack_sha256"]):
            raise TaskWorkspaceError("cold archive material differs from the reviewed plan")
        observed_cold = git(controller, "rev-parse", "--verify", plan["cold_ref"], check=False) or None
        if observed_cold != plan["expected_cold_head"]:
            # Crash recovery: adopt only an already-published exact archive.
            if not observed_cold or any(
                    hashlib.sha256(_cold_file(controller, observed_cold, name)).hexdigest()
                    != hashlib.sha256(data).hexdigest() for name, data in files.items()):
                raise TaskWorkspaceError("cold archive ref drifted from the reviewed plan")
            cold_head = observed_cold
        else:
            cold_head = _git_object_publish(controller, files, plan["cold_ref"],
                                            plan["expected_cold_head"],
                                            f"archive terminal task state {plan['archive_id']}")
        for task_id in plan["terminal_task_ids"]:
            # Full readback through the just-published ref before hot replacement.
            state_archive_get(controller, task_id, plan["cold_ref"],
                              tombstone=compacted["tasks"][task_id])
        compacted_data = _canonical_bytes(compacted)
        if hashlib.sha256(compacted_data).hexdigest() != plan["projected_state_sha256"]:
            raise TaskWorkspaceError("projected hot state differs from the reviewed plan")
        write_state(controller, compacted, allow_compaction=True)
    receipt = {"schema_version": STATE_ARCHIVE_RECEIPT_SCHEMA, "outcome": "applied",
               "plan_sha256": plan["plan_sha256"], "cold_head": cold_head,
               "hot_state_sha256": plan["projected_state_sha256"],
               "hot_state_bytes": plan["projected_state_bytes"],
               "archived_records": len(plan["terminal_task_ids"]), "intent": intent}
    _exclusive_json(output, receipt)
    return receipt


def state_archive_verify(controller: Path, plan_path: Path) -> dict[str, Any]:
    plan = _load_state_archive_plan(controller, plan_path)
    data = state_path(controller).read_bytes()
    if hashlib.sha256(data).hexdigest() != plan["projected_state_sha256"]:
        raise TaskWorkspaceError("hot state does not match the reviewed compacted projection")
    for task_id in plan["terminal_task_ids"]:
        state_archive_get(controller, task_id, plan["cold_ref"])
    return {"schema_version": STATE_ARCHIVE_RECEIPT_SCHEMA, "outcome": "verified",
            "plan_sha256": plan["plan_sha256"], "hot_state_bytes": len(data),
            "archived_records": len(plan["terminal_task_ids"])}


def state_archive_rollback(controller: Path, plan_path: Path, output: Path,
                           authorized: bool) -> dict[str, Any]:
    if not authorized:
        raise TaskWorkspaceError("state archive rollback requires --authorize-state-rollback")
    output = _require_external_archive_output(controller, output)
    plan = _load_state_archive_plan(controller, plan_path)
    if output.exists():
        existing = json.loads(output.read_text())
        if (existing.get("schema_version") != STATE_ARCHIVE_RECEIPT_SCHEMA
                or existing.get("plan_sha256") != plan["plan_sha256"]
                or existing.get("outcome") != "rolled_back"
                or hashlib.sha256(state_path(controller).read_bytes()).hexdigest()
                   != plan["source_state_sha256"]):
            raise TaskWorkspaceError("state archive rollback receipt already exists with different identity")
        return existing
    intent = _state_archive_intent(output, {
        "schema_version": STATE_ARCHIVE_RECEIPT_SCHEMA, "operation": "rollback-intent",
        "plan_sha256": plan["plan_sha256"],
        "expected_hot_state_sha256": plan["projected_state_sha256"],
        "restored_state_sha256": plan["source_state_sha256"],
    })
    with _state_archive_repository_lock(controller), state_lock(controller):
        if hashlib.sha256(state_path(controller).read_bytes()).hexdigest() != plan["projected_state_sha256"]:
            raise TaskWorkspaceError("rollback hot state differs from the exact applied projection")
        result = subprocess.run(["git", "-C", str(controller), "show",
                                 f"{plan['source_head']}:{state_path(controller).relative_to(controller)}"],
                                capture_output=True)
        if result.returncode or hashlib.sha256(result.stdout).hexdigest() != plan["source_state_sha256"]:
            raise TaskWorkspaceError("rollback preimage is unavailable or mismatched")
        restored = json.loads(result.stdout)
        write_state(controller, restored, allow_compaction=True)
    receipt = {"schema_version": STATE_ARCHIVE_RECEIPT_SCHEMA, "outcome": "rolled_back",
               "plan_sha256": plan["plan_sha256"],
               "restored_state_sha256": plan["source_state_sha256"],
               "cold_ref_preserved": plan["cold_ref"], "intent": intent}
    _exclusive_json(output, receipt)
    return receipt


def archive_terminal_transition(controller: Path, state: dict[str, Any], task_id: str,
                                record: dict[str, Any]) -> dict[str, Any]:
    """Publish one future terminal record before replacing it with a hot tombstone."""
    if state.get("schema_version") != BOUNDED_STATE_SCHEMA:
        raise TaskWorkspaceError("compact lifecycle state must be migrated before a new terminal transition")
    if record.get("state") not in TERMINAL_LIFECYCLE_STATES or record.get("task_id") != task_id:
        raise TaskWorkspaceError("terminal archive transition identity is invalid")
    synthetic = {"schema_version": STATE_SCHEMA, "tasks": {task_id: record}, "queues": {}}
    source_sha = hashlib.sha256(_canonical_bytes(record)).hexdigest()
    files, manifest, compacted = _state_archive_material(
        synthetic, source_sha, STATE_ARCHIVE_COLD_REF,
        {"controller_branch": git(controller, "symbolic-ref", "--quiet", "HEAD"),
         "source_head": git(controller, "rev-parse", "HEAD"), "task_id": task_id})
    expected = git(controller, "rev-parse", "--verify", STATE_ARCHIVE_COLD_REF, check=False) or None
    _git_object_publish(controller, files, STATE_ARCHIVE_COLD_REF, expected,
                        f"archive terminal task state {task_id}")
    tombstone = compacted["tasks"][task_id]
    # Verify from the published ref before the caller mutates hot state.
    state_archive_get(controller, task_id, STATE_ARCHIVE_COLD_REF, tombstone=tombstone)
    return tombstone


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("operation", choices=(
        "start", "run", "resume", "recover-predispatch", "recover-wall-budget", "status", "admission", "hydrate", "preflight", "finish",
        "checkpoint", "child-checkpoint", "evidence-run", "evidence-status", "evidence-await",
        "recovery-plan", "recovery-authorize", "recovery-apply", "recovery-verify", "runtime-bootstrap",
        "sync", "doctor", "lease-status", "lease-heartbeat", "lease-handoff",
        "lease-successor", "lease-revoke", "lease-release",
        "state-archive-plan", "state-archive-apply", "state-archive-verify",
        "state-archive-get", "state-archive-rollback"))
    value.add_argument("--task")
    value.add_argument("--limit", type=int, default=DOCTOR_ROW_LIMIT,
                       help="doctor only: maximum lifecycle rows (1-1000)")
    value.add_argument("--offset", type=int, default=0,
                       help="doctor only: lifecycle row offset in sorted task IDs")
    value.add_argument("--run-id", help="exact active task-run identity for receipt-bound recovery")
    value.add_argument("--attempt", type=int,
                       help="exact task-run worker attempt for wall-budget recovery")
    value.add_argument("--predispatch-receipt-sha256",
                       help="exact integrated controller pre-dispatch receipt digest")
    value.add_argument("--original-deadline-unix-ns", type=int,
                       help="immutable original cumulative task-run deadline")
    value.add_argument("--child",
                       help="admitted ordered umbrella child task id for child-checkpoint")
    value.add_argument("--accept-checkpoint",
                       help="accept one ordered ordinary-delivery checkpoint with exact evidence")
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
    value.add_argument("--cold-ref", default=STATE_ARCHIVE_COLD_REF,
                       help="dedicated opt-in Git ref for cold lifecycle records")
    value.add_argument("--authorize-state-compaction", action="store_true")
    value.add_argument("--authorize-state-rollback", action="store_true")
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.operation in {"run", "resume", "recover-predispatch", "recover-wall-budget"}:
            raise TaskWorkspaceError(
                "managed_task_execution_retired: agents implement outside the delivery CLI. "
                "Inspect task status and lease-status; preserve existing worktrees and "
                "historical runs. Verify authority before explicit continuation. "
                "For new work use task start; after implementation/tests/commit use task finish.")
        controller = exact_root(args.controller, "controller", physical_identity=False)
        generation_fence = controller / ".juno_task/runtime/generation-migration/fence.json"
        if (generation_fence.exists() or generation_fence.is_symlink()) and args.operation not in {
                "status", "doctor", "admission", "preflight", "lease-status", "evidence-status",
                "state-archive-get", "state-archive-verify"}:
            raise TaskWorkspaceError("generation_transition_incomplete: preserve controller state; inspect `yy scripts generation doctor`")
        archive_operations = {"state-archive-plan", "state-archive-apply", "state-archive-verify",
                              "state-archive-get", "state-archive-rollback"}
        if args.operation in archive_operations:
            if args.operation == "state-archive-plan":
                if args.task or args.plan or not args.output:
                    raise TaskWorkspaceError("state-archive-plan requires only --output")
                result = state_archive_plan(controller, args.output, args.cold_ref)
            elif args.operation == "state-archive-apply":
                if args.task or not args.plan or not args.output:
                    raise TaskWorkspaceError("state-archive-apply requires --plan and --output")
                result = state_archive_apply(controller, args.plan, args.output,
                                             args.authorize_state_compaction)
            elif args.operation == "state-archive-verify":
                if args.task or not args.plan or args.output:
                    raise TaskWorkspaceError("state-archive-verify requires only --plan")
                result = state_archive_verify(controller, args.plan)
            elif args.operation == "state-archive-get":
                if not args.task or args.plan or args.output:
                    raise TaskWorkspaceError("state-archive-get requires only --task")
                result = state_archive_get(controller, args.task, args.cold_ref)
            else:
                if args.task or not args.plan or not args.output:
                    raise TaskWorkspaceError("state-archive-rollback requires --plan and --output")
                result = state_archive_rollback(controller, args.plan, args.output,
                                                args.authorize_state_rollback)
        elif args.operation == "runtime-bootstrap":
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
            if args.operation != "checkpoint" and args.accept_checkpoint:
                raise TaskWorkspaceError("--accept-checkpoint is supported only for task checkpoint")
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
            elif args.operation == "recovery-verify":
                if (not args.umbrella_admission or not args.plan
                        or not args.authorization_receipt or args.output):
                    raise TaskWorkspaceError(
                        "recovery-verify requires --umbrella-admission, --plan, and --authorization-receipt")
                result = verify_umbrella_recovery(
                    controller, args.task, args.plan, args.umbrella_admission,
                    args.authorization_receipt)
            else:
                if args.umbrella_admission or args.plan or args.output or args.authorization_receipt:
                    raise TaskWorkspaceError(
                        "admission/recovery options are unsupported for this operation")
                if args.operation == "checkpoint":
                    result = (accept_delivery_checkpoint(
                        controller, args.task, args.accept_checkpoint, args.lease_token)
                        if args.accept_checkpoint else
                        standing_checkpoint(controller, args.task, args.lease_token))
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
                    result = kanban_sync_doctor(controller, args.task or None,
                                                limit=args.limit, offset=args.offset)
                elif args.operation == "status":
                    result = status(controller, args.task)
                elif args.operation == "admission":
                    result = task_admission_check(controller, args.task)
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
