#!/usr/bin/env python3
"""Create bounded local commits for durable controller state; never push or orchestrate refs."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from git_index_lock import IndexLockError, diagnose_index_lock, require_index_unlocked
import controller_resolver
try:
    import metadata_controller as metadata_boundary
    METADATA_BOUNDARY_IMPORT_ERROR: ImportError | None = None
except ImportError as exc:  # legacy non-metadata installations retain fallback includes
    metadata_boundary = None
    METADATA_BOUNDARY_IMPORT_ERROR = exc
try:
    import controller_workspace
except ImportError:  # installed generations before sparse-controller support
    controller_workspace = None

SCHEMA_VERSION = "juno_controller_checkpoint.v1"
AGENT_SCHEMA_VERSION = "juno_controller_checkpoint_agent.v1"
BOUNDARY_SCHEMA_VERSION = "juno_workspace_commit_boundary.v1"
HOOK_MARKER = "# juno-controller-boundary-hook-v1"
MAX_COMMITTED_DIAGNOSTICS = 20
MAX_COMMITTED_PATHS = 100
MAX_COMMIT_SUBJECT_CHARS = 120
RELEASE_PATHS = (
    "juno-code/package.json",
    "juno-code/package-lock.json",
    "juno-benchmark/package.json",
    "juno-benchmark/package-lock.json",
    "frontend/generated/package-facts.json",
    "frontend/public/workflows/manifest.json",
    "juno-code/README.md",
    "scripts/release-juno-code.sh",
)
QUEUE_STATE_PATH = ".juno_task/state/tasks.json"
QUEUE_ATTRIBUTION_SCHEMA = "juno_checkpoint_queue_attribution.v1"
QUEUE_ATTRIBUTION_PATH = ".juno_task/runtime/controller-checkpoint/queue-attribution.json"
QUEUE_ATTRIBUTION_RECEIPT_KEYS = {
    "schema_version", "producer", "task_ids", "shared_fields", "queue_document_sha256",
}
# Queue-owned shared state lives only under the canonical "queues" section.
# Any other shared drift (schema version, unknown roots) stays inadmissible.
# The section boundary is the real invariant, not any segment charset: real
# queue documents key sub-sections by exact identities and paths (the
# managed-runtime doctor reports scripts by absolute file path), so later
# segments may contain slashes, colons, or an empty leading component.
QUEUE_SHARED_FIELD_ROOT = "queues"
TASK_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
# Fields a queue-owned transition may rewrite on a task record other than the
# checkpoint's primary task. The primary task record itself is unrestricted:
# enqueue/finalize legitimately rewrite it wholesale.
QUEUE_RECORD_TRANSITION_FIELDS = (
    "enqueue_sequence",
    "kanban_sync",
    "last_queue_outcome",
    "queue_attempt",
    "stale_refresh",
    "state",
    "umbrella_child_progress",
)
_MISSING = object()
DEFAULT_INCLUDE = (
    ".juno_task/tasks",
    ".juno_task/ledger",
    ".juno_task/wiki",
    ".juno_task/specs",
    ".juno_task/workflows",
    ".juno_task/plan.md",
    ".juno_task/tasks.md",
    ".juno_task/managed-assets.json",
)
REQUIRED_METADATA_INCLUDE = (
    ".juno_task/tasks",
    ".juno_task/ledger",
    ".juno_task/state/tasks.json",
)


class CheckpointError(Exception):
    pass


@dataclass(frozen=True)
class Dirty:
    kind: str
    xy: str
    path: str
    original: str | None = None
    submodule: str = "N..."

    @property
    def staged(self) -> bool:
        return self.xy[0] not in {".", "?", "!"}

    @property
    def conflicted(self) -> bool:
        return self.kind == "u" or self.xy in {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}

    @property
    def dirty_submodule(self) -> bool:
        return self.submodule != "N..."


def git(root: Path, *args: str, check: bool = True, text: bool = True) -> Any:
    env = dict(os.environ)
    # Read-only status/diff calls must not refresh the shared index. Git still
    # takes mandatory locks for add/commit when optional locks are disabled.
    env["GIT_OPTIONAL_LOCKS"] = "0"
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=text,
        stdin=subprocess.DEVNULL,
        env=env,
    )
    if check and result.returncode != 0:
        stderr = result.stderr.strip() if text else result.stderr.decode(errors="replace").strip()
        raise CheckpointError(f"git {' '.join(args)} failed: {stderr}")
    return result.stdout if text else result.stdout


def repo_root(value: str) -> Path:
    candidate = Path(value).expanduser().resolve()
    root = Path(git(candidate, "rev-parse", "--show-toplevel").strip()).resolve()
    if candidate != root:
        raise CheckpointError(f"--root must be the repository top level: expected {root}, got {candidate}")
    return root


def common_dir(root: Path) -> Path:
    value = git(root, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
    return Path(value).resolve()


def git_path(root: Path, name: str) -> Path:
    value = git(root, "rev-parse", "--path-format=absolute", "--git-path", name).strip()
    return Path(value).resolve()


def acquire_lease(root: Path):
    lease_path = common_dir(root) / "juno-repository-writer.lock"
    lease_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lease_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise CheckpointError(f"repository lease busy: {lease_path}") from exc
    return handle


def acquire_target_channel(root: Path, timeout_seconds: float = 30.0,
                           explicit_target_ref: str | None = None):
    """Serialize branch commits with integration CAS/restoration on the same channel."""
    target_ref = explicit_target_ref or git(root, "symbolic-ref", "--quiet", "HEAD", check=False).strip()
    if not target_ref:
        raise CheckpointError("checkpoint requires a named branch or explicit target ref")
    if not target_ref.startswith("refs/heads/"):
        raise CheckpointError(f"checkpoint branch is not a full local head ref: {target_ref}")
    identity = f"{common_dir(root)}\0{target_ref}".encode()
    channel_path = common_dir(root) / "juno-integration-channels" / (hashlib.sha256(identity).hexdigest() + ".lock")
    channel_path.parent.mkdir(parents=True, exist_ok=True)
    handle = channel_path.open("a+", encoding="utf-8")
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError as exc:
            if time.monotonic() >= deadline:
                handle.close()
                raise CheckpointError(f"target channel lock timeout: {channel_path}") from exc
            time.sleep(0.05)
    if (explicit_target_ref is None
            and git(root, "symbolic-ref", "--quiet", "HEAD", check=False).strip() != target_ref):
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN); handle.close()
        raise CheckpointError("checkpoint branch changed while acquiring target channel")
    return handle, target_ref, channel_path


def normalize_entry(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CheckpointError("unsafe allowlist entry: entries must be non-empty strings")
    value = value.replace("\\", "/").strip().rstrip("/")
    path = PurePosixPath(value)
    if path.is_absolute() or value.startswith("~") or any(part in {"", ".", "..", ".git"} for part in path.parts):
        raise CheckpointError(f"unsafe allowlist entry: {value!r}")
    if any(char in value for char in "*?[]{}"):
        raise CheckpointError(f"unsafe allowlist entry (globs are not supported): {value!r}")
    return path.as_posix()


def policy_checkpoint_includes(policy: dict[str, Any]) -> tuple[str, ...]:
    """Default selection for metadata controllers without persisted checkpoint choices."""
    entries = (
        list(policy["tracked_exact"])
        + list(policy["tracked_recursive"])
        + list(policy["tracked_top_level_files"])
    )
    return tuple(sorted(dict.fromkeys(normalize_entry(item) for item in entries)))


def validate_metadata_includes(root: Path, include: tuple[str, ...], policy: dict[str, Any]) -> None:
    """Admit configured selectors through the shared ownership classifier."""
    refused: list[dict[str, str]] = []
    for entry in include:
        inspect_boundary(root, entry)
        decision = metadata_boundary.policy_path_decision(entry, policy, container=True)
        if not decision["allowed"]:
            refused.append({"path": entry, "reason": decision["reason"], "rule": decision["rule"]})
    if refused:
        raise CheckpointError(
            "checkpoint include roots refused by metadata-controller policy: " + format_refusals(refused))


def require_metadata_includes(include: tuple[str, ...]) -> None:
    missing = [required for required in REQUIRED_METADATA_INCLUDE if not selected(required, include)]
    if missing:
        additions = ", ".join(json.dumps(item) for item in missing)
        raise CheckpointError(
            "checkpoint include is missing required canonical roots: "
            f"{missing}; safe_next_action=add {additions} to "
            ".juno_task/config.json gitCheckpoint.include, then rerun `controller_checkpoint.py plan`")


def load_config(root: Path) -> tuple[tuple[str, ...], dict[str, Any]]:
    path = root / ".juno_task/config.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, json.JSONDecodeError) as exc:
        raise CheckpointError(f"invalid checkpoint configuration: {exc}") from exc
    checkpoint = payload.get("gitCheckpoint", {})
    if not isinstance(checkpoint, dict):
        raise CheckpointError("invalid checkpoint configuration: gitCheckpoint must be an object")
    policy = metadata_controller_policy(root)
    defaults = policy_checkpoint_includes(policy) if policy is not None else DEFAULT_INCLUDE
    raw_include = checkpoint.get("include", list(defaults))
    if not isinstance(raw_include, list):
        raise CheckpointError("invalid checkpoint configuration: include must be an array")
    include = tuple(dict.fromkeys(normalize_entry(item) for item in raw_include))
    if policy is not None:
        validate_metadata_includes(root, include, policy)
        require_metadata_includes(include)
    agent = checkpoint.get("agent", {})
    if not isinstance(agent, dict):
        raise CheckpointError("invalid checkpoint configuration: agent must be an object")
    return include, agent


def metadata_controller_policy(root: Path) -> dict[str, Any] | None:
    """Load the same reviewed policy consumed by runtime-bootstrap."""
    config_path = root / ".juno_task/config.json"
    if not config_path.is_file():
        return None
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CheckpointError(f"invalid controller configuration: {exc}") from exc
    workspace = config.get("controllerWorkspace") if isinstance(config, dict) else None
    if not isinstance(workspace, dict) or workspace.get("mode") != "metadata-only":
        return None
    if metadata_boundary is None:
        raise CheckpointError(
            f"metadata controller boundary authority is unavailable: {METADATA_BOUNDARY_IMPORT_ERROR}")
    if workspace.get("policy") != ".juno_task/config/metadata-controller.json":
        raise CheckpointError("metadata controller policy must use the canonical tracked path")
    try:
        return metadata_boundary.load_policy(root / workspace["policy"])
    except metadata_boundary.BoundaryError as exc:
        raise CheckpointError(f"metadata controller policy refused: {exc}") from exc


def controller_path_refusals(root: Path, paths: list[str]) -> list[dict[str, str]]:
    policy = metadata_controller_policy(root)
    if policy is None:
        return []
    refused = []
    for path in sorted(set(paths)):
        decision = metadata_boundary.policy_path_decision(path, policy)
        if not decision["allowed"]:
            refused.append({"path": path, "reason": decision["reason"], "rule": decision["rule"]})
    return refused


def format_refusals(refused: list[dict[str, str]]) -> str:
    return ", ".join(
        f"{item['path']} (reason={item['reason']}, rule={item['rule']})" for item in refused
    )


def parse_status(root: Path) -> list[Dirty]:
    """Parse porcelain v2 so index, rename, conflict, and submodule state are explicit."""
    raw = git(root, "status", "--porcelain=v2", "-z", "--untracked-files=all", text=False)
    fields = raw.split(b"\0")
    dirty: list[Dirty] = []
    index = 0
    while index < len(fields) and fields[index]:
        field = fields[index]
        kind = field[:1].decode("ascii", errors="replace")
        try:
            if kind == "1":
                parts = field.split(b" ", 8)
                if len(parts) != 9:
                    raise ValueError
                dirty.append(Dirty(kind, parts[1].decode("ascii"), parts[8].decode("utf-8", errors="surrogateescape"), submodule=parts[2].decode("ascii")))
            elif kind == "2":
                parts = field.split(b" ", 9)
                if len(parts) != 10:
                    raise ValueError
                index += 1
                if index >= len(fields) or not fields[index]:
                    raise ValueError
                dirty.append(Dirty(kind, parts[1].decode("ascii"), parts[9].decode("utf-8", errors="surrogateescape"), fields[index].decode("utf-8", errors="surrogateescape"), parts[2].decode("ascii")))
            elif kind == "u":
                parts = field.split(b" ", 10)
                if len(parts) != 11:
                    raise ValueError
                dirty.append(Dirty(kind, parts[1].decode("ascii"), parts[10].decode("utf-8", errors="surrogateescape"), submodule=parts[2].decode("ascii")))
            elif kind in {"?", "!"}:
                dirty.append(Dirty(kind, kind * 2, field[2:].decode("utf-8", errors="surrogateescape")))
            else:
                raise ValueError
        except (UnicodeDecodeError, ValueError) as exc:
            raise CheckpointError("could not parse Git porcelain-v2 status") from exc
        index += 1
    return dirty


def selected(path: str, includes: tuple[str, ...]) -> bool:
    return any(path == entry or path.startswith(entry + "/") for entry in includes)


def shared_queue_delta(before: Any, after: Any) -> list[str]:
    """Deterministic dotted paths for changed non-task queue state.

    The writer (task_workspace.write_state) and this verifier must enumerate
    identical paths, so both use structurally identical walkers: dictionaries
    recurse per key, every other value (including lists) is a leaf, and a
    missing key differs from any present value.
    """
    paths: set[str] = set()

    def walk(old: Any, new: Any, prefix: str) -> None:
        if isinstance(old, dict) and isinstance(new, dict):
            for key in sorted(set(old) | set(new)):
                walk(old.get(key, _MISSING), new.get(key, _MISSING),
                     f"{prefix}.{key}" if prefix else str(key))
            return
        if old is _MISSING or new is _MISSING or old != new:
            paths.add(prefix)

    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after)):
            if key == "tasks":
                continue
            walk(before.get(key, _MISSING), after.get(key, _MISSING), str(key))
    else:
        paths.add("<state>")
    return sorted(paths)


def record_delta_fields(before: Any, after: Any) -> list[str]:
    """Top-level record fields that differ between two task records."""
    fields: set[str] = set()
    if isinstance(before, dict) and isinstance(after, dict):
        for key in set(before) | set(after):
            if before.get(key, _MISSING) != after.get(key, _MISSING):
                fields.add(str(key))
    else:
        fields.add("<record>")
    return sorted(fields)


def queue_owned_shared_field(item: object) -> bool:
    """A shared field is queue-owned iff its first dotted segment is ``queues``.

    The dotted-path walk already confines shared state to non-task roots, so
    the first segment is the authoritative boundary. Whitespace and control
    characters stay inadmissible in any position; every other byte is legal
    because later segments are verbatim JSON keys, including path-like keys.
    """
    if not isinstance(item, str) or not item:
        return False
    if any(character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F
           for character in item):
        return False
    return item.split(".", 1)[0] == QUEUE_SHARED_FIELD_ROOT


def load_queue_attribution(root: Path) -> dict[str, Any] | None:
    """Load one strictly validated queue-attribution receipt, or None."""
    try:
        payload = json.loads((root / QUEUE_ATTRIBUTION_PATH).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or set(payload) != QUEUE_ATTRIBUTION_RECEIPT_KEYS:
        return None
    if payload["schema_version"] != QUEUE_ATTRIBUTION_SCHEMA:
        return None
    if not isinstance(payload["producer"], str) or not payload["producer"].strip():
        return None
    task_ids = payload["task_ids"]
    if (not isinstance(task_ids, list) or not task_ids
            or any(not isinstance(item, str) or not TASK_ID_RE.fullmatch(item) for item in task_ids)
            or sorted(set(task_ids)) != sorted(task_ids)):
        return None
    shared_fields = payload["shared_fields"]
    if (not isinstance(shared_fields, list)
            or any(not queue_owned_shared_field(item) for item in shared_fields)
            or sorted(set(shared_fields)) != sorted(shared_fields)):
        return None
    digest = payload["queue_document_sha256"]
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        return None
    return payload


def clear_queue_attribution(root: Path) -> None:
    """Reset the attribution chain once the queue document is committed."""
    try:
        (root / QUEUE_ATTRIBUTION_PATH).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def umbrella_child_scope_files(root: Path, task_id: str) -> list[str]:
    """Scope files an umbrella's own canonical scope declares for its children."""
    scope_path = root / ".juno_task/task-scopes" / task_id[:2].lower() / f"{task_id}.json"
    try:
        payload = json.loads(scope_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict) or payload.get("schema_version") != "juno_task_canonical_scope.v1":
        return []
    relations = payload.get("umbrella_relations")
    children = relations.get("children") if isinstance(relations, dict) else None
    if not isinstance(children, list):
        return []
    declared = {child for child in children
                if isinstance(child, str) and TASK_ID_RE.fullmatch(child) and child != task_id}
    return sorted(f".juno_task/task-scopes/{child[:2].lower()}/{child}.json" for child in declared)


def scoped_includes(root: Path, includes: tuple[str, ...], task_id: str | None) -> tuple[str, ...]:
    """Select only state attributable to one lifecycle task operation."""
    if task_id is None:
        return includes
    if not TASK_ID_RE.fullmatch(task_id):
        raise CheckpointError("--task-id is invalid")
    prefix = task_id[:2].lower()
    replacements = {
        ".juno_task/tasks": f".juno_task/tasks/{prefix}/{task_id}.md",
        ".juno_task/ledger": f".juno_task/ledger/{prefix}/{task_id}",
    }
    scoped = [replacement for include_root, replacement in replacements.items()
              if selected(include_root, includes)]
    # Task-scope files are revision-bound controller metadata written by the
    # managed admission path, so the task-scoped selection always admits the
    # task's own scope file plus the children its umbrella scope declares.
    scoped.append(f".juno_task/task-scopes/{prefix}/{task_id}.json")
    scoped.extend(umbrella_child_scope_files(root, task_id))
    if selected(QUEUE_STATE_PATH, includes):
        scoped.append(QUEUE_STATE_PATH)
        scoped.extend(receipt_task_namespaces(root, task_id, includes))
    return tuple(scoped)


def receipt_task_namespaces(root: Path, task_id: str, includes: tuple[str, ...]) -> list[str]:
    """Task/ledger namespaces a receipt-bound queue transition may dirty.

    Widening requires the receipt to name this task, to digest-bind the exact
    current queue document bytes, and those bytes to still differ from HEAD.
    A stale receipt therefore can never widen a strict single-task selection.
    """
    receipt = load_queue_attribution(root)
    if receipt is None or task_id not in receipt["task_ids"]:
        return []
    try:
        current = (root / QUEUE_STATE_PATH).read_bytes()
    except OSError:
        return []
    head = subprocess.run(
        ["git", "-C", str(root), "show", f"HEAD:{QUEUE_STATE_PATH}"],
        capture_output=True, stdin=subprocess.DEVNULL,
    )
    if head.returncode or head.stdout == current:
        return []
    if hashlib.sha256(current).hexdigest() != receipt["queue_document_sha256"]:
        return []
    extra: set[str] = set()
    for other in receipt["task_ids"]:
        if other == task_id:
            continue
        other_prefix = other[:2].lower()
        if selected(".juno_task/tasks", includes):
            extra.add(f".juno_task/tasks/{other_prefix}/{other}.md")
        if selected(".juno_task/ledger", includes):
            extra.add(f".juno_task/ledger/{other_prefix}/{other}")
    return sorted(extra)


def require_attributable_queue_state(root: Path, chosen: list[str], task_id: str | None) -> dict[str, Any] | None:
    """Prove the shared queue document changed only through attributable queue ownership.

    Strict single-task attribution needs no receipt: only the named task's
    record changed and no shared state moved. Any wider delta must be bound by
    the queue writer's attribution receipt: the exact changed-task set, the
    exact queue-owned shared fields, and the exact current document bytes.
    """
    if task_id is None or QUEUE_STATE_PATH not in chosen:
        return None
    before_result = subprocess.run(
        ["git", "-C", str(root), "show", f"HEAD:{QUEUE_STATE_PATH}"],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    if before_result.returncode:
        raise CheckpointError("task-scoped queue state must already be canonical tracked state")
    try:
        before = json.loads(before_result.stdout)
        after = json.loads((root / QUEUE_STATE_PATH).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CheckpointError(f"task-scoped queue state is invalid: {exc}") from exc
    if not isinstance(before, dict) or not isinstance(after, dict):
        raise CheckpointError("task-scoped queue state must be a JSON object")
    before_tasks = before.get("tasks")
    after_tasks = after.get("tasks")
    if not isinstance(before_tasks, dict) or not isinstance(after_tasks, dict):
        raise CheckpointError("task-scoped queue state must contain a tasks object")
    changed_tasks = sorted(
        key for key in set(before_tasks) | set(after_tasks)
        if before_tasks.get(key, _MISSING) != after_tasks.get(key, _MISSING)
    )
    shared_delta = shared_queue_delta(before, after)
    if not shared_delta and changed_tasks == [task_id]:
        return None
    reason = queue_attribution_refusal(
        root, task_id, before_tasks, after_tasks, changed_tasks, shared_delta)
    if reason is not None:
        raise CheckpointError("task-scoped queue attribution refused: " + reason)
    return load_queue_attribution(root)


def queue_attribution_refusal(
    root: Path, task_id: str, before_tasks: dict[str, Any], after_tasks: dict[str, Any],
    changed_tasks: list[str], shared_delta: list[str],
) -> str | None:
    """Return None when a valid receipt admits the observed queue mutation."""
    receipt = load_queue_attribution(root)
    if receipt is None:
        return (f"task_id={task_id} changed_tasks={changed_tasks} "
                f"shared_fields_changed={bool(shared_delta)}; "
                "no queue attribution receipt binds this queue document "
                f"({QUEUE_ATTRIBUTION_PATH} is missing or invalid)")
    try:
        current_digest = hashlib.sha256((root / QUEUE_STATE_PATH).read_bytes()).hexdigest()
    except OSError as exc:
        return f"queue document unreadable: {exc}"
    if task_id not in receipt["task_ids"]:
        return (f"task_id={task_id} changed_tasks={changed_tasks} "
                f"shared_fields_changed={bool(shared_delta)}; receipt attributes "
                f"task_ids={receipt['task_ids']} without this task")
    if receipt["queue_document_sha256"] != current_digest:
        return (f"task_id={task_id} changed_tasks={changed_tasks} "
                f"shared_fields_changed={bool(shared_delta)}; receipt digest does not "
                "bind the current queue document bytes")
    if receipt["task_ids"] != changed_tasks:
        return (f"task_id={task_id} changed_tasks={changed_tasks} "
                f"shared_fields_changed={bool(shared_delta)}; receipt task set "
                f"{receipt['task_ids']} does not match the observed change")
    if receipt["shared_fields"] != shared_delta:
        return (f"task_id={task_id} changed_tasks={changed_tasks} "
                f"shared_fields_changed={bool(shared_delta)}; receipt shared fields "
                f"{receipt['shared_fields']} do not match the observed change {shared_delta}")
    for other in changed_tasks:
        if other == task_id:
            continue
        fields = record_delta_fields(before_tasks.get(other), after_tasks.get(other))
        escaped = [field for field in fields if field not in QUEUE_RECORD_TRANSITION_FIELDS]
        if escaped or not fields:
            return (f"task_id={task_id} changed_tasks={changed_tasks} "
                    f"shared_fields_changed={bool(shared_delta)}; task {other} record "
                    f"delta escapes queue transition fields: {fields}")
    return None


def workspace_policy(root: Path) -> dict[str, Any] | None:
    # The sparse controller policy is a retired pre-cutover authority.  Once
    # the canonical metadata-controller pointer exists, re-reading that stale
    # policy can strand checkpoint recovery on rules the cutover replaced.
    if metadata_controller_policy(root) is not None:
        return None
    pointer = root / ".juno_task/config/controller-workspace.json"
    if not pointer.is_file():
        return None
    if controller_workspace is None:
        raise CheckpointError("controller workspace authority is not installed")
    try:
        return controller_workspace.load_policy(pointer)
    except controller_workspace.WorkspaceError as exc:
        raise CheckpointError(f"controller workspace policy refused: {exc}") from exc


def require_sparse_controller(root: Path, *, allow_pending_changes: bool = False) -> dict[str, Any] | None:
    policy = workspace_policy(root)
    if policy is None:
        return None
    try:
        evidence = controller_workspace.inspect(root, policy)
    except controller_workspace.WorkspaceError as exc:
        raise CheckpointError(f"sparse controller verification failed: {exc}") from exc
    failed = sorted(key for key, value in evidence["checks"].items()
                    if not value and not (allow_pending_changes and key == "clean"))
    if failed:
        raise CheckpointError("sparse controller policy drift blocks checkpoint: " + ",".join(failed))
    return evidence


def status_names(item: Dirty) -> tuple[str, ...]:
    return (item.path, item.original) if item.original else (item.path,)


def unrelated_task_residue(path: str, includes: tuple[str, ...]) -> bool:
    scoped = any(item.startswith((".juno_task/tasks/", ".juno_task/ledger/",
                                 ".juno_task/task-scopes/")) for item in includes)
    return scoped and path.startswith((".juno_task/tasks/", ".juno_task/ledger/",
                                       ".juno_task/task-scopes/")) and not selected(path, includes)


def inspect_boundary(root: Path, relative: str) -> None:
    candidate = root / relative
    current = root
    parts = PurePosixPath(relative).parts
    for part in parts:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            break
        if stat.S_ISLNK(mode):
            raise CheckpointError(f"unsafe symlink path: {relative}")
        if current != root and (current / ".git").exists():
            raise CheckpointError(f"unsafe nested repository/submodule path: {relative}")
    try:
        candidate.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise CheckpointError(f"unsafe path escape: {relative}") from exc


def branch_and_head(root: Path) -> tuple[str, str]:
    branch = git(root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).strip()
    if not branch:
        raise CheckpointError("checkpoint requires a named branch; detached HEAD is not allowed")
    return branch, git(root, "rev-parse", "HEAD").strip()


def fingerprint(root: Path, path: str) -> str:
    candidate = root / path
    digest = hashlib.sha256(path.encode("utf-8", errors="surrogateescape"))
    try:
        info = candidate.lstat()
    except FileNotFoundError:
        digest.update(b"\0deleted")
        return digest.hexdigest()
    digest.update(f"\0{info.st_mode}\0{info.st_size}".encode())
    if candidate.is_file():
        with candidate.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    else:
        digest.update(b"\0non-file")
    return digest.hexdigest()


def excluded_untracked(item: Dirty, includes: tuple[str, ...]) -> bool:
    """Residue outside selection is not write authority and is never staged."""
    return item.kind == "?" and not any(selected(name, includes) for name in status_names(item))


def inspect(
    root: Path, includes: tuple[str, ...], *, recover_stale_lock: bool = True,
    task_id: str | None = None,
) -> dict[str, Any]:
    if os.environ.get("GIT_INDEX_FILE"):
        raise CheckpointError("alternate GIT_INDEX_FILE is not allowed for controller checkpoints")
    try:
        index_lock = (
            require_index_unlocked(root) if recover_stale_lock else diagnose_index_lock(root)
        )
        if index_lock["lock_present"]:
            raise IndexLockError(
                "git_index_lock_present: "
                f"path={index_lock['lock_path']} safe_next_action=preserve_and_coordinate"
            )
    except IndexLockError as exc:
        raise CheckpointError(str(exc)) from exc
    branch, head = branch_and_head(root)
    dirt = parse_status(root)
    if any(item.conflicted for item in dirt):
        paths = [item.path for item in dirt if item.conflicted]
        raise CheckpointError(f"unmerged conflict paths block checkpoint: {paths}")
    if any(item.dirty_submodule for item in dirt):
        paths = [item.path for item in dirt if item.dirty_submodule]
        raise CheckpointError(f"dirty submodule state blocks checkpoint: {paths}")
    staged = sorted({name for item in dirt if item.staged for name in status_names(item)})
    if staged:
        raise CheckpointError(f"pre-existing staged index blocks checkpoint: {staged}")
    excluded = sorted(item.path for item in dirt if excluded_untracked(item, includes))
    eligible = [item for item in dirt if not excluded_untracked(item, includes)]
    for item in eligible:
        for name in status_names(item):
            inspect_boundary(root, name)
    all_names = sorted({name for item in eligible for name in status_names(item)})
    policy_refused = controller_path_refusals(root, all_names)
    if policy_refused:
        raise CheckpointError("blocked non-controller paths under metadata-controller policy: "
                              + format_refusals(policy_refused))
    chosen = sorted({name for name in all_names if selected(name, includes)})
    require_attributable_queue_state(root, chosen, task_id)
    blocked = sorted({name for name in all_names
                      if not selected(name, includes) and not unrelated_task_residue(name, includes)})
    if blocked:
        raise CheckpointError(f"blocked non-controller paths: {blocked}")
    return {
        "branch": branch,
        "head": head,
        "index_lock": index_lock,
        "selected": chosen,
        "excluded": [{"path": name, "reason": "untracked_outside_checkpoint_selection",
                      "safe_next_action": "leave untouched; if relocation is desired, obtain owner approval and preserve bytes outside the controller"}
                     for name in excluded],
        "fingerprints": {path: fingerprint(root, path) for path in chosen},
    }


def assert_frozen(root: Path, includes: tuple[str, ...], frozen: dict[str, Any], remaining: list[str],
                  task_id: str | None = None) -> None:
    current = inspect(root, includes, task_id=task_id)
    if current["branch"] != frozen["branch"] or current["head"] != frozen["head"]:
        raise CheckpointError("repository HEAD/ref changed during checkpoint")
    if current["selected"] != sorted(remaining):
        raise CheckpointError("dirty path set changed during checkpoint")
    expected = {path: frozen["fingerprints"][path] for path in remaining}
    if current["fingerprints"] != expected:
        raise CheckpointError("selected controller content changed during checkpoint")


INDEX_TREE = ":index"


def path_modes(root: Path, path: str, treeish: str | None) -> set[str]:
    """Return all modes for a path in an index/tree endpoint; None is an empty tree."""
    if treeish is None:
        return set()
    if treeish == INDEX_TREE:
        rows = git(root, "ls-files", "--stage", "--", path, check=False).splitlines()
    else:
        rows = git(root, "ls-tree", treeish, "--", path, check=False).splitlines()
    return {parts[0] for row in rows if (parts := row.split(None, 3))}


def classify_paths(
    root: Path,
    includes: tuple[str, ...],
    paths: list[str],
    role: str,
    *,
    evidence_pairs: tuple[tuple[str | None, str | None], ...],
) -> list[dict[str, str]]:
    """Single role/path/mode classifier used by checkpoints, hooks, and history audits."""
    offending: list[dict[str, str]] = []
    for path in sorted(set(paths)):
        reason: str | None = None
        if role == "integration-owner":
            reason = "integration_owner_commit_forbidden"
        elif role == "controller" and not selected(path, includes):
            reason = "product_path"
        if role == "controller":
            metadata_refused = controller_path_refusals(root, [path])
            if metadata_refused:
                reason = metadata_refused[0]["reason"] + ":" + metadata_refused[0]["rule"]
            policy = workspace_policy(root)
            if policy is not None:
                try:
                    ownership = controller_workspace.classify(policy, path)
                    if ownership == "product_canonical": reason = "product_path"
                    elif ownership == "local_ignored": reason = "local_ignored_path"
                except controller_workspace.WorkspaceError:
                    reason = "unclassified_path"

            # Inspect both endpoints of every audited delta. A deleted or replaced
            # gitlink may be absent from both the filesystem and the new tree.
            modes = {
                mode
                for old_tree, new_tree in evidence_pairs
                for tree in (old_tree, new_tree)
                for mode in path_modes(root, path, tree)
            }
            if "160000" in modes:
                reason = "gitlink"
            try:
                inspect_boundary(root, path)
            except CheckpointError:
                if reason != "gitlink":
                    reason = "nested_repository_or_unsafe_boundary"
        if reason:
            offending.append({"path": path, "reason": reason})
    return offending


def resolve_role(root: Path, *, persisted_only: bool = False) -> dict[str, Any]:
    persisted_role = git(root, "config", "--worktree", "--get", "juno.workspace.role", check=False).strip()
    registered = git(root, "config", "--get", "juno.controller.path", check=False).strip()
    if persisted_role == "controller" and workspace_policy(root) is not None:
        if not registered or Path(registered).expanduser().resolve() != root:
            raise CheckpointError("sparse checkpoint root is not the exact registered controller")
        # require_sparse_controller already proved branch, policy, generation,
        # materialization, and registration identity before pending dirt was
        # inspected. Re-entering the generic resolver here would reject the
        # intended clean=false state before the checkpoint can classify it.
        return {"role": "controller", "role_source": "registered-sparse-checkpoint"}
    if not persisted_only:
        return controller_resolver.resolve(root, "diagnostic")
    keys = ("JUNO_TASK_ROOT", "JUNO_CONTROLLER_BRANCH", "JUNO_WORKSPACE_ROLE")
    saved = {key: os.environ.pop(key) for key in keys if key in os.environ}
    try:
        return controller_resolver.resolve(root, "diagnostic")
    finally:
        os.environ.update(saved)


def staged_paths(root: Path) -> list[str]:
    dirt = parse_status(root)
    return sorted({name for item in dirt if item.staged for name in status_names(item)})


def boundary_payload(root: Path, includes: tuple[str, ...], paths: list[str], *,
                     evidence_pairs: tuple[tuple[str | None, str | None], ...] | None = None,
                     resolution: dict[str, Any] | None = None) -> dict[str, Any]:
    resolution = resolution or resolve_role(root)
    pairs = evidence_pairs or (("HEAD", INDEX_TREE),)
    offending = classify_paths(root, includes, paths, str(resolution["role"]), evidence_pairs=pairs)
    return {"schema_version": BOUNDARY_SCHEMA_VERSION, "passed": not offending,
            "root": str(root), "branch": git(root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False) or None,
            "head": git(root, "rev-parse", "HEAD"), "role": resolution["role"],
            "role_source": resolution.get("role_source"), "paths": paths, "offending": offending,
            "safe_next_action": "run `yy task start TASK_ID` from the registered controller to create an exact-base task worktree"}


def require_boundary(payload: dict[str, Any]) -> None:
    if payload["passed"]:
        return
    details = ", ".join(
        f"{item['path']} ({item['reason']})"
        + (f" commit={item['commit']} subject={item['subject']!r}" if item.get("commit") else "")
        for item in payload["offending"]
    )
    omitted = payload.get("offending_count", len(payload["offending"])) - len(payload["offending"])
    suffix = f", omitted={omitted}" if omitted else ""
    raise CheckpointError(
        f"workspace commit boundary refused role={payload['role']} branch={payload['branch'] or 'DETACHED'} "
        f"offending=[{details}]{suffix}; safe_next_action={payload['safe_next_action']}"
    )


def assert_staged_boundary(
    root: Path,
    includes: tuple[str, ...],
    frozen: dict[str, Any],
    remaining: list[str],
    staged_paths: list[str],
) -> None:
    branch, head = branch_and_head(root)
    if branch != frozen["branch"] or head != frozen["head"]:
        raise CheckpointError("repository HEAD/ref changed after staging")
    dirt = parse_status(root)
    if any(item.conflicted for item in dirt):
        raise CheckpointError("conflict appeared during checkpoint staging")
    if any(item.dirty_submodule for item in dirt):
        raise CheckpointError("dirty submodule state appeared during checkpoint staging")
    blocked = sorted({name for item in dirt if not excluded_untracked(item, includes)
                      for name in status_names(item)
                      if not selected(name, includes) and not unrelated_task_residue(name, includes)})
    if blocked:
        raise CheckpointError(f"blocked non-controller paths appeared during checkpoint: {blocked}")
    actual_staged = sorted({name for item in dirt if item.staged for name in status_names(item)})
    if actual_staged != sorted(staged_paths):
        raise CheckpointError(
            f"staged path set escaped frozen group: expected={sorted(staged_paths)} actual={actual_staged}"
        )
    dirty_paths = sorted({name for item in dirt if not excluded_untracked(item, includes)
                          for name in status_names(item)
                          if not unrelated_task_residue(name, includes)})
    if dirty_paths != sorted(remaining):
        raise CheckpointError("dirty path set changed after staging")
    if any(fingerprint(root, path) != frozen["fingerprints"][path] for path in remaining):
        raise CheckpointError("selected controller content changed after staging")
    require_boundary(boundary_payload(root, includes, actual_staged))


def validate_message(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value or len(value) > 500:
        raise CheckpointError("agent proposal contains an invalid commit message")
    return value.strip()


def agent_groups(root: Path, frozen: dict[str, Any], config: dict[str, Any]) -> list[tuple[list[str], str]]:
    if os.environ.get("JUNO_CONTROLLER_CHECKPOINT_ACTIVE") == "1":
        raise CheckpointError("recursive controller checkpoint agent invocation rejected")
    timeout = config.get("timeoutSeconds", 120)
    if not isinstance(timeout, int) or not 1 <= timeout <= 600:
        raise CheckpointError("agent timeoutSeconds must be an integer from 1 to 600")
    context = {
        "schema_version": AGENT_SCHEMA_VERSION,
        "instruction": "Return JSON only. Group every supplied path exactly once and provide a concise local commit message.",
        "paths": frozen["selected"],
        "diff_stat": git(root, "diff", "--stat", "--", *frozen["selected"]),
    }
    override = os.environ.get("JUNO_CHECKPOINT_AGENT_COMMAND", "").strip()
    if override:
        command = shlex.split(override)
    else:
        service = str(config.get("service", "pi"))
        model = str(config.get("model", ":luna"))
        command = ["yylo", service, "--no-hooks", "--allowed-tools", "Read,Grep,Glob", "--model", model, "-p", json.dumps(context)]
    env = dict(os.environ)
    env["JUNO_CONTROLLER_CHECKPOINT_ACTIVE"] = "1"
    try:
        result = subprocess.run(command, cwd=root, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired as exc:
        raise CheckpointError(f"agent proposal timed out after {timeout}s") from exc
    if result.returncode != 0:
        raise CheckpointError(f"agent proposal failed with exit {result.returncode}")
    try:
        proposal = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CheckpointError("agent proposal is not valid JSON") from exc
    if not isinstance(proposal, dict) or proposal.get("schema_version") != AGENT_SCHEMA_VERSION:
        raise CheckpointError("agent proposal has an invalid schema_version")
    if set(proposal) != {"schema_version", "groups"}:
        raise CheckpointError("agent proposal contains unknown top-level fields")
    groups = proposal.get("groups")
    if not isinstance(groups, list):
        raise CheckpointError("agent proposal groups must be an array")
    output: list[tuple[list[str], str]] = []
    flattened: list[str] = []
    allowed = set(frozen["selected"])
    for group in groups:
        if not isinstance(group, dict) or set(group) != {"paths", "message"}:
            raise CheckpointError("agent proposal group must contain only paths and message")
        if not isinstance(group.get("paths"), list) or not group["paths"]:
            raise CheckpointError("agent proposal contains an invalid group")
        paths = group["paths"]
        if any(not isinstance(path, str) or path not in allowed for path in paths):
            raise CheckpointError("agent proposal contains a path outside the frozen selection")
        flattened.extend(paths)
        output.append((paths, validate_message(group.get("message"))))
    if sorted(flattened) != sorted(frozen["selected"]) or len(flattened) != len(set(flattened)):
        raise CheckpointError("agent proposal must include every selected path exactly once")
    return output


def stage_and_commit(root: Path, includes: tuple[str, ...], frozen: dict[str, Any], groups: list[tuple[list[str], str]],
                     task_id: str | None = None) -> list[str]:
    remaining = list(frozen["selected"])
    commits: list[str] = []
    for paths, message in groups:
        assert_frozen(root, includes, frozen, remaining, task_id)
        staged_by_checkpoint = False
        try:
            git(root, "add", "--", *paths)
            staged_by_checkpoint = True
            staged_status = parse_status(root)
            staged = sorted({name for item in staged_status if item.staged for name in status_names(item)})
            if staged != sorted(paths):
                raise CheckpointError(f"staged path set escaped frozen group: expected={sorted(paths)} actual={staged}")
            # inspect() rejects any index ownership, so use the staging-aware
            # boundary check to catch blocked paths, conflicts, ref/content races,
            # and any path staged outside this explicit group.
            assert_staged_boundary(root, includes, frozen, remaining, paths)
            git(root, "commit", "--no-verify", "-m", message, "--", *paths)
            staged_by_checkpoint = False
        except BaseException:
            # A failed/raced commit must not strand checkpoint-owned index state.
            # Restore only the explicit group; worktree content remains untouched.
            if staged_by_checkpoint:
                git(root, "restore", "--staged", "--", *paths, check=False)
            raise
        commits.append(git(root, "rev-parse", "HEAD").strip())
        frozen["head"] = commits[-1]
        remaining = [path for path in remaining if path not in paths]
        if QUEUE_STATE_PATH in paths:
            # The queue document is durable now; its attribution chain resets so
            # the next queue command starts from committed truth.
            clear_queue_attribution(root)
            frozen["fingerprints"].pop(QUEUE_STATE_PATH, None)
    post = inspect(root, includes, task_id=task_id)
    if post["selected"]:
        raise CheckpointError(f"checkpoint postcondition failed; selected dirt remains: {post['selected']}")
    return commits


def hook_paths(root: Path) -> tuple[Path, Path, Path]:
    raw = Path(git(root, "rev-parse", "--git-path", "hooks/pre-commit").strip())
    hook = (raw if raw.is_absolute() else root / raw).resolve()
    return hook, hook.with_name("pre-commit.juno-user"), hook.with_name("pre-commit.juno-metadata.json")


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def managed_hook(helper: Path, user_hook: Path | None, user_sha256: str | None) -> str:
    guard = f"python3 {shlex.quote(str(helper.resolve()))} --root \"$(git rev-parse --show-toplevel)\" staged-check\n"
    if user_hook is None:
        user = ""
    else:
        assert user_sha256 is not None
        quoted = shlex.quote(str(user_hook))
        digest = shlex.quote(user_sha256)
        user = (f"actual=$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],\"rb\").read()).hexdigest())' {quoted})\n"
                f"if [ \"$actual\" != {digest} ]; then echo 'controller-checkpoint: approved user hook hash mismatch' >&2; exit 2; fi\n"
                f"if [ -x {quoted} ]; then\n"
                f"  set +e\n"
                f"  {quoted} \"$@\"\n"
                f"  status=$?\n"
                f"  set -e\n"
                f"  if [ \"$status\" -ne 0 ]; then exit \"$status\"; fi\n"
                f"  {guard}"
                f"fi\n")
    return "#!/bin/sh\nset -eu\n" + HOOK_MARKER + "\n" + guard + user


def load_hook_metadata(metadata: Path) -> dict[str, Any]:
    try:
        value = json.loads(metadata.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not value.get("helper"):
            raise ValueError("missing helper")
        if bool(value.get("user_hook")) != bool(value.get("user_sha256")):
            raise ValueError("incomplete user hook identity")
        return value
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise CheckpointError(f"managed hook metadata invalid: {exc}") from exc


def verify_managed_hook(hook: Path, metadata: Path) -> tuple[dict[str, Any], str]:
    if not metadata.is_file():
        raise CheckpointError("managed hook metadata missing; refuse operation")
    meta = load_hook_metadata(metadata)
    user = Path(meta["user_hook"]) if meta.get("user_hook") else None
    if user is not None:
        if not user.is_file() or file_sha(user) != meta["user_sha256"]:
            raise CheckpointError("approved user hook hash mismatch; refuse operation")
    expected = managed_hook(Path(meta["helper"]), user, meta.get("user_sha256"))
    if hook.read_text(encoding="utf-8") != expected:
        raise CheckpointError("managed hook drift; refuse operation")
    return meta, expected


def parse_approval(value: str | None) -> tuple[Path, str] | None:
    if not value:
        return None
    raw_path, separator, digest = value.rpartition("=")
    if not separator or not re_full_sha(digest):
        raise CheckpointError("--approve-existing must be exact PATH=SHA256")
    return Path(raw_path).expanduser().resolve(), digest


def re_full_sha(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def hook_command(root: Path, action: str, approval_value: str | None) -> dict[str, Any]:
    hook, user_hook, metadata = hook_paths(root)
    approval = parse_approval(approval_value)
    if action == "status":
        managed = hook.is_file() and HOOK_MARKER in hook.read_text(encoding="utf-8", errors="replace")
        healthy = False
        if managed:
            verify_managed_hook(hook, metadata)
            healthy = True
        return {"action": action, "hook": str(hook), "exists": hook.exists(), "sha256": file_sha(hook) if hook.is_file() else None,
                "managed": managed, "healthy": healthy}
    if action == "install":
        existing_managed = hook.is_file() and HOOK_MARKER in hook.read_text(encoding="utf-8", errors="replace")
        if existing_managed:
            verify_managed_hook(hook, metadata)
            return {"action": action, "outcome": "already_installed", "hook": str(hook)}
        if hook.exists():
            if approval is None or approval[0] != hook or approval[1] != file_sha(hook):
                raise CheckpointError(f"existing pre-commit hook requires explicit approval: --approve-existing {hook}={file_sha(hook)}")
            if user_hook.exists() or metadata.exists(): raise CheckpointError("hook composition sidecar collision")
        elif approval is not None:
            raise CheckpointError("--approve-existing supplied but no existing hook is present")
        helper = Path(__file__).resolve()
        approved_user_sha = approval[1] if hook.exists() and approval else None
        content = managed_hook(helper, user_hook if hook.exists() else None, approved_user_sha)
        hook.parent.mkdir(parents=True, exist_ok=True)
        if hook.exists(): hook.rename(user_hook)
        hook.write_text(content, encoding="utf-8"); hook.chmod(0o755)
        metadata.write_text(json.dumps({"helper": str(helper), "user_hook": str(user_hook) if user_hook.exists() else None,
                                        "user_sha256": approved_user_sha}, sort_keys=True) + "\n")
        return {"action": action, "outcome": "installed", "hook": str(hook), "user_hook": str(user_hook) if user_hook.exists() else None}
    if action == "remove":
        if not hook.is_file() or HOOK_MARKER not in hook.read_text(encoding="utf-8", errors="replace"):
            if metadata.exists() or user_hook.exists():
                raise CheckpointError("managed hook sidecars remain without an intact dispatcher; refuse removal")
            return {"action": action, "outcome": "not_managed_preserved" if hook.exists() else "already_absent", "hook": str(hook)}
        meta, _ = verify_managed_hook(hook, metadata)
        hook.unlink(); metadata.unlink()
        if user_hook.exists(): user_hook.rename(hook)
        return {"action": action, "outcome": "removed", "hook": str(hook)}
    raise CheckpointError(f"unknown hook action: {action}")


def commit_evidence(root: Path, commit: str) -> tuple[list[str], tuple[tuple[str | None, str | None], ...]]:
    """Return paths and old/new tree evidence for every parent of an audited commit."""
    row = git(root, "rev-list", "--parents", "-n", "1", commit).strip().split()
    parents = row[1:]
    if not parents:
        paths = git(root, "diff-tree", "--root", "--no-commit-id", "--name-only", "--no-renames", "-r", "-z", commit)
        return sorted(set(value for value in paths.split("\0") if value)), ((None, commit),)
    changed: set[str] = set()
    pairs: list[tuple[str | None, str | None]] = []
    for parent in parents:
        paths = git(root, "diff", "--name-only", "--no-renames", "-z", parent, commit)
        changed.update(value for value in paths.split("\0") if value)
        pairs.append((parent, commit))
    return sorted(changed), tuple(pairs)


def committed_admission(root: Path, fallback_base: str | None = None, *, prefer_persisted: bool = True,
                        protected_role_override: str | None = None) -> dict[str, Any]:
    """Audit every commit since protected authority with bounded exact evidence."""
    root = repo_root(str(root))
    includes, _ = load_config(root)
    persisted_base = git(root, "config", "--worktree", "--get", "juno.workspace.roleBase", check=False).strip() or None
    requested_base = (persisted_base or fallback_base) if prefer_persisted else (fallback_base or persisted_base)
    if not requested_base:
        raise CheckpointError("committed-check requires --base or persisted workspace roleBase evidence")
    base = git(root, "rev-parse", f"{requested_base}^{{commit}}").strip()
    head = git(root, "rev-parse", "HEAD").strip()
    relation = subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", base, head],
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
    commits: list[str] = []
    if relation == 0:
        commits = git(root, "rev-list", "--reverse", "--topo-order", f"{base}..{head}").splitlines()
        classification = "at_or_advanced_from_role_base"
    elif subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", head, base],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        # A protected integration may advance authority while preserving a detached
        # runtime checkout at the old SHA. It contains no unadmitted new commits.
        classification = "checkout_behind_protected_role_base"
    else:
        raise CheckpointError("workspace HEAD diverges from persisted roleBase authority")
    if not (root / ".juno_task").is_dir():
        if commits:
            raise CheckpointError("unmanaged repository has commits beyond protected admission base")
        resolution = {"role": "unmanaged-exact", "role_source": "fallback-base"}
    elif protected_role_override == "integration-owner":
        # The protected integration owner must audit an as-yet-unregistered
        # owner checkout before CAS. No CLI exposes this internal classification.
        resolution = {"role": "integration-owner", "role_source": "protected-integration-preflight"}
    else:
        resolution = resolve_role(root, persisted_only=True)

    all_paths: set[str] = set()
    offending_total = 0
    diagnostics: list[dict[str, str]] = []
    for commit in commits:
        paths, evidence_pairs = commit_evidence(root, commit)
        all_paths.update(paths)
        offending = classify_paths(root, includes, paths, str(resolution["role"]), evidence_pairs=evidence_pairs)
        if not offending:
            continue
        subject = git(root, "show", "-s", "--format=%s", commit).replace("\n", " ")[:MAX_COMMIT_SUBJECT_CHARS]
        offending_total += len(offending)
        remaining = MAX_COMMITTED_DIAGNOSTICS - len(diagnostics)
        diagnostics.extend({**item, "commit": commit, "subject": subject} for item in offending[:max(remaining, 0)])

    sorted_paths = sorted(all_paths)
    payload = {"schema_version": BOUNDARY_SCHEMA_VERSION, "passed": offending_total == 0,
               "root": str(root), "branch": git(root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False) or None,
               "head": head, "role": resolution["role"], "role_source": resolution.get("role_source"),
               "paths": sorted_paths[:MAX_COMMITTED_PATHS], "path_count": len(sorted_paths),
               "offending": diagnostics, "offending_count": offending_total,
               "diagnostics_truncated": offending_total > len(diagnostics), "commits_checked": len(commits),
               "safe_next_action": "run `yy task start TASK_ID` from the registered controller to create an exact-base task worktree",
               "base": base, "classification": classification}
    require_boundary(payload)
    return payload


def release_admission(root: Path, requested_paths: list[str], *, require_changes: bool) -> dict[str, Any]:
    """Read-only admission shared by release preflight and guarded commit."""
    if os.environ.get("GIT_INDEX_FILE"):
        raise CheckpointError("alternate GIT_INDEX_FILE is not allowed for release admission")
    resolution = resolve_role(root)
    if resolution.get("role") != "integration-owner":
        raise CheckpointError("release-commit requires persisted integration-owner authority")
    paths = list(dict.fromkeys(normalize_entry(value) for value in requested_paths))
    if tuple(paths) != RELEASE_PATHS:
        raise CheckpointError(f"release-commit paths must exactly match protected release identity: {list(RELEASE_PATHS)}")
    hook, _, metadata = hook_paths(root)
    if hook.exists():
        if not hook.is_file() or HOOK_MARKER not in hook.read_text(encoding="utf-8", errors="replace"):
            raise CheckpointError("release-commit refuses an unmanaged pre-commit hook")
        verify_managed_hook(hook, metadata)
    dirt = parse_status(root)
    if any(item.conflicted or item.dirty_submodule for item in dirt):
        raise CheckpointError("release-commit refuses conflicts or dirty submodules")
    if any(item.staged for item in dirt):
        raise CheckpointError("release-commit refuses a pre-existing staged index")
    dirty_paths = sorted({name for item in dirt for name in status_names(item)})
    blocked = [path for path in dirty_paths if path not in RELEASE_PATHS]
    if blocked:
        raise CheckpointError(f"release-commit refuses non-release dirt: {blocked}")
    if require_changes and not dirty_paths:
        raise CheckpointError("release-commit requires release metadata changes")
    # Protected release identity is a fixed authority boundary, not merely a
    # dirty-path filter. Inspect every requested endpoint even when the tree is
    # clean so a committed symlink, missing endpoint, directory, or untracked
    # replacement cannot inherit release authority.
    for path in paths:
        inspect_boundary(root, path)
        candidate = root / path
        try:
            mode = candidate.lstat().st_mode
        except FileNotFoundError as exc:
            raise CheckpointError(f"release-commit protected path is missing: {path}") from exc
        if not stat.S_ISREG(mode):
            raise CheckpointError(f"release-commit protected path is not a regular file: {path}")
        if not git(root, "ls-files", "--error-unmatch", "--", path, check=False).strip():
            raise CheckpointError(f"release-commit protected path is not tracked: {path}")
    return {"schema_version": BOUNDARY_SCHEMA_VERSION, "action": "release_preflight", "passed": True,
            "role": "integration-owner", "head": git(root, "rev-parse", "HEAD").strip(),
            "paths": list(RELEASE_PATHS), "dirty_paths": dirty_paths}


def release_commit(root: Path, message: str, requested_paths: list[str],
                   *, target_ref: str | None = None,
                   expected_target: str | None = None) -> dict[str, Any]:
    """Create the one explicit package-release commit without weakening the hook."""
    admission = release_admission(root, requested_paths, require_changes=True)
    dirty_paths = admission["dirty_paths"]
    before = admission["head"]
    frozen = {path: fingerprint(root, path) for path in dirty_paths}
    git(root, "add", "--", *RELEASE_PATHS)
    try:
        staged = staged_paths(root)
        if staged != dirty_paths or any(path not in RELEASE_PATHS for path in staged):
            raise CheckpointError(f"release-commit staged identity mismatch: dirty={dirty_paths} staged={staged}")
        if git(root, "rev-parse", "HEAD").strip() != before:
            raise CheckpointError("release-commit HEAD changed after admission")
        if any(fingerprint(root, path) != frozen[path] for path in dirty_paths):
            raise CheckpointError("release-commit content changed after staging")
        staged_tree = git(root, "write-tree").strip()
        exact_message = validate_message(message)
        # A detached canonical owner creates the commit object first and then
        # advances the explicit target with an expected-SHA CAS. It never owns
        # the target branch by checkout mode.
        detached_release = target_ref is not None or expected_target is not None
        if detached_release:
            if not target_ref or not expected_target:
                raise CheckpointError("detached release requires target ref and expected target SHA")
            if git(root, "symbolic-ref", "--quiet", "HEAD", check=False).strip():
                raise CheckpointError("explicit-target release requires detached HEAD")
            if before != expected_target or git(root, "rev-parse", target_ref).strip() != expected_target:
                raise CheckpointError("release target/ref lease identity changed")
            created = git(root, "commit-tree", staged_tree, "-p", before,
                          "-m", exact_message).strip()
            update = subprocess.run(["git", "-C", str(root), "update-ref", target_ref,
                                     created, expected_target], capture_output=True, text=True,
                                    stdin=subprocess.DEVNULL,
                                    env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
            if update.returncode != 0:
                raise CheckpointError("release target CAS failed")
            try:
                git(root, "reset", "--hard", created)
            except BaseException:
                git(root, "update-ref", target_ref, expected_target, created, check=False)
                git(root, "reset", "--hard", expected_target, check=False)
                raise
        else:
            # The ordinary integration-owner classifier remains a hard deny.
            git(root, "commit", "--no-verify", "-m", exact_message, "--", *RELEASE_PATHS)

        # HEAD is mutable even while this process owns the Juno authority lock:
        # unrelated Git can advance the ref immediately after `git commit`.
        # Recover the commit created by this admission from the reflog and bind
        # it to the frozen parent, staged tree, exact message, and changed paths.
        reflog = git(root, "reflog", "--all", "--format=%H", check=False).splitlines()
        candidates: list[str] = []
        for candidate in dict.fromkeys(([created] if detached_release else []) +
                                       [row.strip() for row in reflog if row.strip()]):
            parent_row = git(root, "rev-list", "--parents", "-n", "1", candidate, check=False).split()
            if parent_row != [candidate, before]:
                continue
            if git(root, "show", "-s", "--format=%T", candidate).strip() != staged_tree:
                continue
            if git(root, "show", "-s", "--format=%B", candidate).rstrip("\n") != exact_message:
                continue
            changed = sorted(git(root, "diff-tree", "--no-commit-id", "--name-only", "-r", candidate).splitlines())
            if changed != staged or any(path not in RELEASE_PATHS for path in changed):
                continue
            candidates.append(candidate)
        if len(candidates) != 1:
            raise CheckpointError(f"release-commit could not uniquely bind created commit: candidates={candidates}")
        created = candidates[0]
    except BaseException:
        git(root, "restore", "--staged", "--", *RELEASE_PATHS, check=False)
        raise
    git(root, "config", "--worktree", "juno.workspace.roleBase", created)
    return {"schema_version": BOUNDARY_SCHEMA_VERSION, "action": "release_commit", "passed": True,
            "role": "integration-owner", "before": before, "head": created, "tree": staged_tree,
            "message": exact_message, "paths": staged}


def emit(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, sort_keys=True))
    else:
        if "outcome" not in payload or "selected" not in payload:
            print(f"workspace boundary: {payload.get('action', 'passed')} passed={payload.get('passed', True)} role={payload.get('role', '')}")
            return
        selected_paths = payload.get("selected", [])
        print(f"controller checkpoint: {payload['outcome']} ({len(selected_paths)} selected)")
        for path in selected_paths:
            print(f"  {path}")
        for commit in payload.get("commits", []):
            print(f"  commit {commit}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=os.getcwd(), help="Exact repository top level")
    parser.add_argument("--task-id", help="Limit task and ledger selection to one canonical task namespace")
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", help="Inspect without mutation")
    plan.add_argument("--json", action="store_true")
    commit = sub.add_parser("commit", help="Create bounded local commit(s)")
    commit.add_argument("--message", default="chore(controller): checkpoint durable controller state")
    commit.add_argument("--agent", action="store_true")
    commit.add_argument("--json", action="store_true")
    clean = sub.add_parser("require-clean", help="Require clean state, optionally checkpoint first")
    clean.add_argument("--checkpoint", action="store_true")
    clean.add_argument("--message", default="chore(controller): checkpoint eligible metadata")
    clean.add_argument("--json", action="store_true")
    staged = sub.add_parser("staged-check", help="Read-only pre-commit staged-tree boundary")
    staged.add_argument("--json", action="store_true")
    committed = sub.add_parser("committed-check", help="Independent committed-tree check for hook bypass")
    committed.add_argument("--base"); committed.add_argument("--json", action="store_true")
    preflight = sub.add_parser("release-preflight", help="Read-only exact release-commit eligibility check")
    preflight.add_argument("--path", action="append", required=True); preflight.add_argument("--json", action="store_true")
    release = sub.add_parser("release-commit", help="Create an exact guarded package release commit")
    release.add_argument("--message", required=True); release.add_argument("--path", action="append", required=True); release.add_argument("--json", action="store_true")
    release.add_argument("--target-ref"); release.add_argument("--expected-target")
    hook = sub.add_parser("hook", help="Explicit managed pre-commit adoption")
    hook.add_argument("action", choices=["install", "status", "remove"]); hook.add_argument("--approve-existing"); hook.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    root = repo_root(args.root)
    includes, agent_config = load_config(root)
    includes = scoped_includes(root, includes, args.task_id)
    persisted_role = git(root, "config", "--worktree", "--get", "juno.workspace.role", check=False).strip()
    pending_commands = {"plan", "commit", "require-clean", "staged-check"}
    if persisted_role == "controller":
        # A checkpoint exists to consume eligible controller dirt. All sparse
        # identity/materialization checks remain mandatory before mutation; only
        # the expected pre-commit clean check is deferred to terminal readback.
        require_sparse_controller(root, allow_pending_changes=args.command in pending_commands)
    if args.command == "staged-check":
        payload = boundary_payload(root, includes, staged_paths(root)); require_boundary(payload); emit(payload, args.json); return 0
    if args.command == "committed-check":
        payload = committed_admission(root, args.base, prefer_persisted=args.base is None); emit(payload, args.json); return 0
    if args.command == "release-preflight":
        payload = release_admission(root, args.path, require_changes=False); emit(payload, args.json); return 0
    if args.command == "release-commit":
        # This dispatch guard must precede acquire_lease/acquire_target_channel:
        # alternate-index refusal is not allowed to create writer/channel state.
        # release_admission repeats the check as defense in depth for callers.
        if os.environ.get("GIT_INDEX_FILE"):
            raise CheckpointError("alternate GIT_INDEX_FILE is not allowed for release admission")
        lease = acquire_lease(root); channel = None
        try:
            if bool(args.target_ref) != bool(args.expected_target):
                raise CheckpointError("--target-ref and --expected-target must be provided together")
            channel = acquire_target_channel(root, explicit_target_ref=args.target_ref)
            payload = release_commit(root, args.message, args.path,
                                     target_ref=args.target_ref,
                                     expected_target=args.expected_target)
        finally:
            if channel: fcntl.flock(channel[0].fileno(), fcntl.LOCK_UN); channel[0].close()
            fcntl.flock(lease.fileno(), fcntl.LOCK_UN); lease.close()
        emit(payload, args.json); return 0
    if args.command == "hook":
        payload = hook_command(root, args.action, args.approve_existing); emit(payload, args.json); return 0
    should_commit = args.command == "commit" or (args.command == "require-clean" and args.checkpoint)
    lease = acquire_lease(root); channel = None
    try:
        if should_commit: channel = acquire_target_channel(root)
        frozen = inspect(root, includes, recover_stale_lock=should_commit, task_id=args.task_id)
        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "root": str(root),
            "target_channel": None if channel is None else {"target_ref": channel[1], "lock_path": str(channel[2])},
            "branch": frozen["branch"],
            "head": frozen["head"],
            "index_lock": frozen["index_lock"],
            "selected": frozen["selected"],
            "excluded": frozen["excluded"],
            "commits": [],
        }
        if args.command == "require-clean" and frozen["excluded"]:
            raise CheckpointError("controller is not clean; preserved excluded paths: "
                                  + ", ".join(item["path"] for item in frozen["excluded"]))
        if not frozen["selected"]:
            payload["outcome"] = "noop"
        elif should_commit:
            groups = agent_groups(root, frozen, agent_config) if getattr(args, "agent", False) else [(frozen["selected"], validate_message(args.message))]
            # Agent is read-only: reject any repository mutation before staging.
            assert_frozen(root, includes, frozen, frozen["selected"], args.task_id)
            payload["commits"] = stage_and_commit(root, includes, frozen, groups, args.task_id)
            payload["outcome"] = "committed"
            payload["head"] = payload["commits"][-1]
            if persisted_role == "controller":
                payload["sparse_controller_readback"] = require_sparse_controller(root, allow_pending_changes=True)
        elif args.command == "require-clean":
            raise CheckpointError(f"controller is dirty; run checkpoint first: {frozen['selected']}")
        else:
            payload["outcome"] = "planned"
        emit(payload, getattr(args, "json", False))
        return 0
    finally:
        if channel: fcntl.flock(channel[0].fileno(), fcntl.LOCK_UN); channel[0].close()
        fcntl.flock(lease.fileno(), fcntl.LOCK_UN)
        lease.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (CheckpointError, controller_resolver.ResolverError, OSError) as exc:
        print(f"controller_checkpoint: error: {exc}", file=sys.stderr)
        raise SystemExit(2)
