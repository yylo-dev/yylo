#!/usr/bin/env python3
"""Conflict-aware single-writer merge queue for Bolt task workspaces.

Only target-ref mutation is serialized. Feature worktrees remain independent,
and controller commits never participate in product history.
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

import integration_workspace as integration_runtime
import task_workspace as task_runtime
import risk_policy as risk_runtime
import task_workflow_helper as lifecycle_runtime
import operation_snapshot as operation_runtime

QUEUE_SCHEMA = task_runtime.STATE_SCHEMA
MERGE_STATUS_SCHEMA = "juno_merge_status.v1"
MERGE_STATUS_SUMMARY_PROJECTION = "merge-status.summary.v1"
MERGE_STATUS_DETAIL_PROJECTION = "merge-status.detail.v1"
MERGE_STATUS_FULL_PROJECTION = "merge-status.full.v1"
MERGE_STATUS_MAX_BYTES = 32768
MERGE_STATUS_SUMMARY_ROWS = 10
MERGE_STATUS_BLOCKER_ROWS = 8
MERGE_STATUS_DETAIL_ITEMS = 12
MERGE_STATUS_STRING_CHARS = 512
MERGE_STATUS_VISIBLE_STATES = {
    "QUEUED", "MERGING", "CONFLICT", "CONFLICT_RESOLVED", "AWAITING_RISK",
    "REVIEW_FINDINGS", "REVIEW_FINDINGS_EXHAUSTED",
    "REOPENING", "REQUEUING_STALE", "MERGED", "WITHDRAWN",
}
MERGE_STATUS_ACTIVE_STATES = MERGE_STATUS_VISIBLE_STATES - {"MERGED", "WITHDRAWN"}
MERGE_STATUS_BLOCKER_STATES = {
    "CONFLICT", "REVIEW_FINDINGS", "REVIEW_FINDINGS_EXHAUSTED",
    "REQUEUING_STALE", "REOPENING",
}
ATTEMPT_SCHEMA = "juno_merge_queue_attempt.v1"
PLAN_SCHEMA = "juno_merge_candidate_feasibility.v1"
PLAN_ID_SCHEMA = "juno_merge_candidate_plan_identity.v1"
REFRESH_SCHEMA = "juno_merge_target_refresh_plan.v1"
REFRESH_ID_SCHEMA = "juno_merge_target_refresh_identity.v1"
RECONCILE_SCHEMA = "juno_merge_terminal_reconciliation_plan.v1"
RECONCILE_ID_SCHEMA = "juno_merge_terminal_reconciliation_identity.v1"
RECONCILE_REFERENCE_SCHEMA = "juno_merge_terminal_reconciliation_reference.v1"
WITHDRAW_SCHEMA = "juno_merge_queue_withdraw_receipt.v1"
AUTHORITY_SCHEMA = "juno_merge_live_authority.v1"
PRE_CAS_EDIT_RECOVERY_SCHEMA = "juno_merge_pre_cas_edit_recovery.v1"
PRE_CAS_EDIT_RECOVERY_ROOT = ".juno_task/runtime/merge-queue/pre-cas-edit-recovery"
LIFECYCLE_SUPERSESSION_SCHEMA = "juno_merge_lifecycle_journal_supersession.v1"
FULL_SUITE_REPAIR_SCHEMA = "juno_merge_deterministic_full_suite_repair.v1"
FULL_SUITE_REPAIR_ROOT = ".juno_task/runtime/merge-queue/full-suite-repair"
REPAIR_PREDISPATCH_RECOVERY_SCHEMA = "juno_merge_repair_predispatch_recovery.v1"
WITHDRAWABLE_STATES = {"QUEUED", "AWAITING_RISK", "REVIEW_FINDINGS",
                       "REVIEW_FINDINGS_EXHAUSTED", "CONFLICT", "CONFLICT_RESOLVED",
                       "REOPENING", "REQUEUING_STALE"}
OWNER_SCHEMA = "juno_merge_queue_candidate_owner.v1"
RISK_STATE_SCHEMA = "juno_merge_queue_risk_state.v1"
REVIEW_PROMPT_FIELDS = {
    "task_id", "review_kind", "reviewer_index", "repository", "base_sha",
    "tip_sha", "checklist_path", "findings_summary_path",
    "validation_evidence_path", "requirements_bundle",
    "findings_summary",
}
REVIEW_PLACEHOLDER_RE = re.compile(r"{{\s*([a-z_][a-z0-9_]*)\s*}}")
INTEGRATION_OWNER_CONFIG = "juno.integration.ownerPath"
INTEGRATION_OWNER_AUTHORITY = "protected-integration.v1"


class MergeQueueError(RuntimeError):
    pass


def compile_merge_operation_snapshot(**inputs: Any) -> dict[str, Any]:
    """Merge-lifecycle seam for the immutable operation/read-set compiler."""
    return operation_runtime.compile_identity_operation_snapshot(**inputs)


def verify_merge_operation_snapshot(snapshot: Any) -> dict[str, Any]:
    """Fail closed when task-to-merge operation evidence is partial or tampered."""
    verification = operation_runtime.verify_operation_snapshot(snapshot)
    if not verification["valid"]:
        raise MergeQueueError("operation snapshot is missing, tampered, or ambiguous: "
                              + json.dumps(verification["reasons"], sort_keys=True))
    return snapshot


def verify_task_submission(controller: Path, repository: Path, task_id: str,
                           record: dict[str, Any]) -> dict[str, Any]:
    """Verify the one task-produced submission; admit finite pre-submission records."""
    closure = record.get("review_ready_closure")
    if closure is None:
        return {"kind": "legacy_creation_identity", "valid": True}
    body = {key: value for key, value in closure.items() if key != "closure_sha256"}
    if (not isinstance(closure, dict)
            or closure.get("schema_version") != "juno_task_review_ready_closure.v1"
            or closure.get("closure_sha256") != task_runtime.stable_sha256(body)):
        return {"kind": "submission", "valid": False, "reason": "closure_tampered"}
    submission = closure.get("submission")
    if submission is None:
        return {"kind": "legacy_review_ready_closure", "valid": True}
    submission_body = {key: value for key, value in submission.items()
                       if key != "submission_sha256"} if isinstance(submission, dict) else {}
    refresh = closure.get("target_refresh")
    source_tip = (refresh.get("source_tip") if isinstance(refresh, dict)
                  else record.get("tip_sha"))
    expected = {
        "task_id": task_id,
        "base_sha": record.get("base_sha"), "tip_sha": source_tip,
        "tree_sha": task_runtime.git(repository, "rev-parse",
                                      f"{source_tip}^{{tree}}", check=False),
        "admitted_scope_sha256": task_runtime.stable_sha256(
            (record.get("creation_receipt") or {}).get("allowed_paths")),
        "generated_scope_sha256": task_runtime.stable_sha256(
            (record.get("creation_receipt") or {}).get("generated_output_admission")),
    }
    try:
        requirements = task_runtime.canonical_requirement_identity(controller, task_id)
        delivery_acceptance = task_runtime.delivery_checkpoint_projection(
            controller, task_id, record)
    except task_runtime.TaskWorkspaceError as exc:
        return {"kind": "submission", "valid": False, "reason": str(exc)}
    if delivery_acceptance is not None:
        if (not delivery_acceptance.get("final_accepted")
                or closure.get("delivery_acceptance") != delivery_acceptance
                or delivery_acceptance.get("evidence", [{}])[-1].get("tip_sha") != source_tip):
            return {"kind": "submission", "valid": False,
                    "reason": "delivery_acceptance_identity_mismatch"}
    elif "delivery_acceptance" in closure:
        return {"kind": "submission", "valid": False,
                "reason": "unexpected_delivery_acceptance"}
    if (not isinstance(submission, dict)
            or set(submission_body) != operation_runtime.SUBMISSION_FIELDS
            or submission.get("submission_sha256") != task_runtime.stable_sha256(submission_body)
            or any(submission.get(key) != value for key, value in expected.items())
            or submission.get("requirements_sha256") != requirements["requirements_sha256"]):
        return {"kind": "submission", "valid": False,
                "reason": "submission_identity_mismatch"}
    return {"kind": "submission", "valid": True,
            "submission_sha256": submission["submission_sha256"]}


class AuthorityDriftError(MergeQueueError):
    """Live task/queue/target authority no longer matches the admitted attempt."""

    def __init__(self, boundary: str, reasons: list[dict[str, Any]]) -> None:
        super().__init__(f"live authority drift at {boundary}: "
                         + ", ".join(row["code"] for row in reasons))
        self.boundary = boundary
        self.reasons = reasons


class PostIntegrationError(MergeQueueError):
    """A durable post-CAS phase failed and must resume without another CAS."""


class AdmissionStateError(MergeQueueError):
    """A persisted admission tag is unsafe to interpret or replace."""


class IntegrationOwnerAdvancementError(MergeQueueError):
    """The target CAS landed, but its registered owner did not become exact."""


class MergeValidationError(MergeQueueError):
    def __init__(self, message: str, evidence: list[dict[str, Any]],
                 receipt_reference: Optional[dict[str, str]] = None) -> None:
        super().__init__(message)
        self.evidence = evidence
        self.receipt_reference = receipt_reference


class DependencyLockMismatchError(MergeQueueError):
    """An exact candidate/feature lock mismatch that a feature refresh may repair."""

    def __init__(self, evidence: dict[str, str]) -> None:
        super().__init__("candidate validation package lock differs from feature worktree")
        self.evidence = evidence


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def write_canonical_exclusive(path: Path, value: dict[str, Any], limit: int) -> None:
    data = risk_runtime.canonical(value)
    if not data or len(data) > limit:
        raise MergeQueueError("exclusive queue artifact exceeds its bound")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise MergeQueueError(f"queue admission artifact already exists: {path}") from exc
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data); handle.flush(); os.fsync(handle.fileno())
    except BaseException:
        try: path.unlink()
        except OSError: pass
        raise


def repository_identity(repository: Path) -> str:
    common = task_runtime.git(repository, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return str(Path(common).resolve())


def target_key(repository: Path, target_ref: str) -> str:
    material = f"{repository_identity(repository)}\0{target_ref}".encode()
    return hashlib.sha256(material).hexdigest()


def registered_worktrees(repository: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    output = task_runtime.git(repository, "worktree", "list", "--porcelain")
    for line in [*output.splitlines(), ""]:
        if not line:
            if current:
                rows.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        if key in {"bare", "detached", "locked", "prunable"}:
            current[key] = True if not value else value
        else:
            current[key] = value
    return rows


def integration_owner_readback(owner: Path) -> dict[str, Any]:
    """Read authority from the checkout itself; never substitute the target ref."""
    full, reasons = integration_runtime.full_checkout(owner)
    return {
        "path": str(owner),
        "head": task_runtime.ref_sha(owner, "HEAD"),
        "role_base": integration_runtime.worktree_config(owner, "juno.workspace.roleBase"),
        "role": integration_runtime.worktree_config(owner, "juno.workspace.role"),
        "authority": integration_runtime.worktree_config(
            owner, "juno.workspace.roleAuthority"),
        "clean": not bool(task_runtime.git(
            owner, "status", "--porcelain=v1", "--untracked-files=all", check=False)),
        "detached": not bool(task_runtime.git(
            owner, "symbolic-ref", "-q", "HEAD", check=False)),
        "full_checkout": full,
        "full_checkout_reasons": reasons,
        "submodules": integration_runtime.submodule_state(owner),
    }


def registered_owner_preflight(repository: Path, expected: str,
                               candidate: str) -> tuple[Path | None, dict[str, Any] | None]:
    raw = task_runtime.git(repository, "config", "--local", "--get",
                           INTEGRATION_OWNER_CONFIG, check=False)
    if not raw:
        return None, None
    owner = Path(raw).expanduser().resolve()
    rows = {str(Path(row["worktree"]).resolve()): row for row in registered_worktrees(repository)
            if isinstance(row.get("worktree"), str) and row.get("prunable") is not True}
    if str(owner) not in rows or not owner.is_dir():
        raise MergeQueueError("registered integration owner is not a live linked worktree")
    observed = integration_owner_readback(owner)
    exact = (observed["role"] == "integration-owner"
             and observed["authority"] == INTEGRATION_OWNER_AUTHORITY
             and observed["head"] in {expected, candidate}
             and observed["role_base"] in {expected, candidate}
             and observed["clean"] and observed["detached"]
             and observed["full_checkout"]
             and all(row["state"] == "exact" for row in observed["submodules"]))
    if not exact:
        raise MergeQueueError(
            "registered integration owner is not clean, detached, full, submodule-exact, and authoritative"
        )
    return owner, observed


def advance_registered_owner(repository: Path, expected: str, candidate: str,
                             owner: Path | None,
                             before: dict[str, Any] | None) -> dict[str, Any]:
    """Move and verify the registered checkout after the admitted target CAS."""
    if owner is None:
        return {"status": "not_registered", "before": None, "after": None,
                "target_sha": candidate}
    try:
        current = integration_owner_readback(owner)
        if current != before:
            raise MergeQueueError("registered integration owner changed during target CAS")
        if current["head"] != candidate:
            task_runtime.git(owner, "switch", "--detach", candidate)
        task_runtime.git(owner, "submodule", "sync", "--recursive")
        task_runtime.git(owner, "submodule", "update", "--init", "--recursive", "--checkout")
        moved = integration_owner_readback(owner)
        if (moved["head"] != candidate or not moved["clean"] or not moved["detached"]
                or not moved["full_checkout"]
                or moved["role"] != "integration-owner"
                or moved["authority"] != INTEGRATION_OWNER_AUTHORITY
                or any(row["state"] != "exact" for row in moved["submodules"])):
            raise MergeQueueError("integration owner checkout advancement readback is not exact")
        if moved["role_base"] != candidate:
            integration_runtime.advance_owner_role_base(owner, moved["role_base"], candidate)
        after = integration_owner_readback(owner)
        if (after["head"] != candidate or after["role_base"] != candidate
                or not after["clean"] or not after["detached"] or not after["full_checkout"]
                or after["role"] != "integration-owner"
                or after["authority"] != INTEGRATION_OWNER_AUTHORITY
                or any(row["state"] != "exact" for row in after["submodules"])):
            raise MergeQueueError("integration owner final authority readback is not exact")
        return {"status": "already_advanced" if before["head"] == candidate
                and before["role_base"] == candidate else "advanced",
                "path": str(owner), "before": before, "after": after,
                "target_sha": candidate}
    except Exception as exc:
        try:
            after = integration_owner_readback(owner)
        except Exception as readback_exc:
            after = {"path": str(owner), "readback_error": str(readback_exc)}
        return {"status": "partial", "path": str(owner), "before": before,
                "after": after, "target_sha": candidate, "error": str(exc),
                "recovery_command": "yy integration sync"}


def require_owner_advancement(authority: dict[str, Any]) -> None:
    if authority.get("status") == "partial":
        raise IntegrationOwnerAdvancementError(
            "target integrated but integration-owner advancement failed; "
            f"recover with: {authority['recovery_command']}"
        )


def assert_target_unchecked_out(repository: Path, target_ref: str) -> None:
    owners = sorted(row.get("worktree", "") for row in registered_worktrees(repository)
                    if row.get("branch") == target_ref)
    if owners:
        raise MergeQueueError(
            f"target ref is checked out; detach its worktree before queue CAS: {', '.join(owners)}"
        )


def target_entry(state: dict[str, Any], repository: Path, target_ref: str) -> dict[str, Any]:
    key = target_key(repository, target_ref)
    entry = state["queues"].setdefault(key, {
        "repository_identity": repository_identity(repository),
        "target_ref": target_ref,
        "last_attempt": None,
        "conflicts": {},
    })
    expected = {"repository_identity", "target_ref", "last_attempt", "conflicts"}
    if not isinstance(entry, dict) or set(entry) != expected or not isinstance(entry.get("conflicts"), dict):
        raise MergeQueueError("invalid target queue entry")
    if entry["repository_identity"] != repository_identity(repository) or entry["target_ref"] != target_ref:
        raise MergeQueueError("target queue identity collision")
    return entry


@contextmanager
def target_lock(_controller: Path, repository: Path, target_ref: str) -> Iterator[None]:
    # The lock belongs to the repository, not a controller checkout. Distinct
    # controllers targeting the same full ref therefore contend on one inode.
    common = Path(repository_identity(repository))
    lock = common / "juno-locks/merge-queue" / f"{target_key(repository, target_ref)}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise MergeQueueError("another worker owns this repository/target-ref queue") from exc
            raise
        yield


@contextmanager
def review_lock(repository: Path, task_id: str) -> Iterator[None]:
    lock = Path(repository_identity(repository)) / "juno-locks/review" / f"{task_id}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise MergeQueueError("another reviewer owns this task review claim") from exc
            raise
        yield


def _project_queue_board_state(controller: Path, task_id: str, state_name: str) -> None:
    """Project one durable queue transition onto the canonical board.

    Queue-owned states stay non-terminal on the board. A failed projection is
    recorded as an explicit pending sync on the record without destroying the
    queue state machine, then fails closed with one exact recovery command.
    """
    with task_runtime.state_lock(controller):
        record = task_runtime.read_state(controller)["tasks"].get(task_id)
    finalization = (((record or {}).get("queue_attempt") or {}).get("post_integration") or {}).get(
        "kanban_finalization") if isinstance(record, dict) else None
    landed = ((record or {}).get("queue_attempt") or {}).get("landed_delivery") \
        if isinstance(record, dict) else None
    if (state_name == "MERGED" or isinstance(landed, dict)
            or (isinstance(finalization, dict) and finalization.get("status") == "complete")):
        # After landed proof is journaled, the outbox exclusively owns board
        # finalization. Queue-state projection must not race it or overwrite done.
        return
    if not isinstance(record, dict):
        raise MergeQueueError(f"queue board projection lost task {task_id}")
    try:
        evidence = task_runtime.ensure_kanban_sync(controller, task_id, record, phase="queue")
    except task_runtime.KanbanSyncError as exc:
        try:
            task_runtime._stamp_kanban_sync(controller, task_id, record, exc.evidence)
        except task_runtime.TaskWorkspaceError:
            pass
        raise MergeQueueError(
            f"merge queue Kanban projection failed for {task_id}: {exc}; "
            f"recover with: {task_runtime.KANBAN_SYNC_RECOVERY.format(task=task_id)}") from exc
    if evidence.get("outcome") != "verified":
        try:
            task_runtime._stamp_kanban_sync(controller, task_id, record, evidence)
        except task_runtime.TaskWorkspaceError:
            pass


def persist_attempt(controller: Path, attempt: dict[str, Any], *, state_name: Optional[str] = None,
                    conflict: Optional[dict[str, Any]] = None, remove_conflict: bool = False,
                    expected_record_sha256: Optional[str] = None) -> None:
    """Apply one short task/queue transaction, optionally revision-CAS bound."""
    if (expected_record_sha256 is not None
            and not re.fullmatch(r"[0-9a-f]{64}", expected_record_sha256)):
        raise MergeQueueError("expected queue record revision is malformed")
    with task_runtime.state_lock(controller):
        state = task_runtime.read_state(controller)
        current = state["tasks"].get(attempt["task_id"])
        if not isinstance(current, dict) or current.get("tip_sha") != attempt["feature_sha"]:
            raise MergeQueueError("task record changed while merge candidate was active")
        if expected_record_sha256 is not None and digest(current) != expected_record_sha256:
            raise MergeQueueError("task/queue revision compare-and-swap refused stale transition")
        if state_name:
            state["tasks"][attempt["task_id"]] = {
                **current, "state": state_name, "queue_attempt": attempt,
                "last_queue_outcome": attempt["outcome"],
            }
        config = task_runtime.load_config(controller)
        repository = task_runtime.product_repository(controller, config)
        entry = target_entry(state, repository, config["target_ref"])
        entry["last_attempt"] = attempt
        if remove_conflict:
            entry["conflicts"].pop(attempt["task_id"], None)
        elif conflict is not None:
            entry["conflicts"][attempt["task_id"]] = conflict
        # Tasks and queue/conflict truth cross one atomic replace boundary.
        task_runtime.write_state(controller, state)
    if state_name:
        _project_queue_board_state(controller, attempt["task_id"], state_name)


def changed_paths(root: Path) -> list[str]:
    names: set[str] = set()
    for args in (("diff", "--name-only"), ("diff", "--cached", "--name-only"),
                 ("ls-files", "--others", "--exclude-standard")):
        names.update(filter(None, task_runtime.git(root, *args, check=False).splitlines()))
    return sorted(names)


def conflict_paths(root: Path) -> list[str]:
    return sorted(filter(None, task_runtime.git(root, "diff", "--name-only", "--diff-filter=U").splitlines()))


def optional_revision(root: Path, revision: str) -> Optional[str]:
    result = task_runtime.run(["git", "-C", str(root), "rev-parse", f"{revision}^{{commit}}"], root, check=False)
    value = result.stdout.strip()
    return value if result.returncode == 0 and task_runtime.SHA_RE.fullmatch(value) else None


def file_digest(path: Path) -> Optional[str]:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def git_blob_digest(repository: Path, blob_sha: str) -> Optional[str]:
    if not re.fullmatch(r"[0-9a-f]{40,64}", blob_sha):
        return None
    result = subprocess.run(
        ["git", "-C", str(repository), "cat-file", "blob", blob_sha],
        cwd=repository, stdin=subprocess.DEVNULL, capture_output=True,
    )
    return hashlib.sha256(result.stdout).hexdigest() if result.returncode == 0 else None


def guard_snapshot(root: Path, paths: list[str]) -> dict[str, Any]:
    return {path: {
        "worktree_sha256": file_digest(root / path),
        "index": task_runtime.git(root, "ls-files", "-s", "--", path, check=False),
    } for path in paths}


def _git_tree(repository: Path, revision: str, env: Optional[dict[str, str]] = None) -> dict[str, str]:
    result = subprocess.run(
        ["git", "-C", str(repository), "ls-tree", "-r", "-z", revision],
        cwd=repository, env=env, stdin=subprocess.DEVNULL, capture_output=True,
    )
    if result.returncode:
        return {}
    rows: dict[str, str] = {}
    for item in result.stdout.split(b"\0"):
        if not item:
            continue
        metadata, _, raw_path = item.partition(b"\t")
        fields = metadata.decode("ascii", "replace").split()
        if len(fields) == 3:
            rows[raw_path.decode("utf-8", "surrogateescape")] = fields[2]
    return rows


def _blob_bytes(repository: Path, revision: str, path: str,
                env: Optional[dict[str, str]] = None) -> Optional[bytes]:
    result = subprocess.run(
        ["git", "-C", str(repository), "show", f"{revision}:{path}"],
        cwd=repository, env=env, stdin=subprocess.DEVNULL, capture_output=True,
    )
    return result.stdout if result.returncode == 0 else None


def _prospective_tree(repository: Path, target_sha: str, feature_sha: str
                      ) -> tuple[dict[str, str], list[str], Optional[str]]:
    """Compose into an isolated object directory; repository bytes stay untouched."""
    with tempfile.TemporaryDirectory(prefix="juno-merge-plan-") as temporary:
        object_dir = Path(temporary) / "objects"
        object_dir.mkdir()
        env = os.environ.copy()
        env["GIT_OBJECT_DIRECTORY"] = str(object_dir)
        env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str(
            Path(repository_identity(repository)) / "objects")
        result = subprocess.run(
            ["git", "-C", str(repository), "merge-tree", "--write-tree",
             "--name-only", "--messages", target_sha, feature_sha],
            cwd=repository, env=env, stdin=subprocess.DEVNULL,
            text=True, capture_output=True,
        )
        lines = result.stdout.splitlines()
        tree_sha = lines[0] if lines and re.fullmatch(r"[0-9a-f]{40,64}", lines[0]) else None
        conflicts: list[str] = []
        if tree_sha:
            for line in lines[1:]:
                if not line:
                    break
                if not line.startswith(("CONFLICT ", "Auto-merging ")):
                    conflicts.append(line)
        tree = _git_tree(repository, tree_sha, env) if tree_sha else {}
        return tree, sorted(set(conflicts)), tree_sha


def _path_allowed(path: str, roots: Any) -> bool:
    return isinstance(roots, list) and task_runtime.path_within(path, roots)


def _finding(code: str, severity: str, phase: str, evidence: dict[str, Any],
             repair: str, invalidates: bool = True,
             tests_safe: bool = False) -> dict[str, Any]:
    return {"code": code, "severity": severity, "phase": phase,
            "evidence": evidence, "repair_command": repair,
            "repair_invalidates_plan": invalidates,
            "tests_safe_before_repair": tests_safe}


def _json_file_identity(repository: Path, revision: str, path: str) -> dict[str, Any]:
    raw = _blob_bytes(repository, revision, path)
    if raw is None:
        return {"path": path, "present": False, "sha256": None, "version": None,
                "valid_semver": None}
    version: Any = None
    malformed = False
    try:
        value = json.loads(raw)
        version = value.get("version") if isinstance(value, dict) else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        malformed = True
    return {"path": path, "present": True, "sha256": hashlib.sha256(raw).hexdigest(),
            "version": version, "valid_semver": (task_runtime.is_valid_semver(version)
                                                   if version is not None else None),
            "malformed": malformed}


def _target_refresh_package_pair(repository: Path, feature_sha: str, lock_path: str,
                                 authored: set[str]) -> dict[str, Any]:
    """Authenticate one task-authored npm manifest/lock pair for target refresh."""
    parent, separator, _ = lock_path.rpartition("/")
    manifest_path = f"{parent}/package.json" if separator else "package.json"
    evidence: dict[str, Any] = {
        "lock_path": lock_path, "manifest_path": manifest_path,
        "lock_authored": lock_path in authored,
        "manifest_authored": manifest_path in authored,
    }
    if not evidence["lock_authored"] or not evidence["manifest_authored"]:
        return {**evidence, "valid": False, "reason": "pair_not_task_authored"}
    manifest_raw = _blob_bytes(repository, feature_sha, manifest_path)
    lock_raw = _blob_bytes(repository, feature_sha, lock_path)
    try:
        manifest = json.loads(manifest_raw) if manifest_raw is not None else None
        lock = json.loads(lock_raw) if lock_raw is not None else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {**evidence, "valid": False, "reason": "malformed_json"}
    if not isinstance(manifest, dict) or not isinstance(lock, dict):
        return {**evidence, "valid": False, "reason": "non_object_json"}
    lockfile_version = lock.get("lockfileVersion")
    packages = lock.get("packages")
    root = packages.get("") if isinstance(packages, dict) else None
    if lockfile_version not in {2, 3} or not isinstance(root, dict):
        return {**evidence, "valid": False, "reason": "unsupported_lock_shape",
                "lockfile_version": lockfile_version}
    for field in ("name", "version"):
        expected = manifest.get(field)
        if expected is not None and (lock.get(field) != expected or root.get(field) != expected):
            return {**evidence, "valid": False, "reason": f"{field}_mismatch",
                    "lockfile_version": lockfile_version}
    for field in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        expected = manifest.get(field, {})
        observed = root.get(field, {})
        if not isinstance(expected, dict) or observed != expected:
            return {**evidence, "valid": False, "reason": f"{field}_mismatch",
                    "lockfile_version": lockfile_version}
    return {**evidence, "valid": True, "reason": "authenticated_task_authored_pair",
            "lockfile_version": lockfile_version}


def _current_target_refresh_receipt(controller: Path, task_id: str, record: dict[str, Any],
                                    feature_sha: str, target_sha: str) -> dict[str, Any]:
    """Verify the immutable refresh receipt binding the frozen tip to this target."""
    evidence: dict[str, Any] = {"feature_sha": feature_sha, "target_sha": target_sha}
    references = record.get("target_refreshes")
    if not isinstance(references, list):
        return {**evidence, "valid": False, "reason": "reference_missing"}
    reference = next((row for row in reversed(references) if isinstance(row, dict)
                      and row.get("schema_version") ==
                          "juno_merge_target_refresh_reference.v1"
                      and row.get("refreshed_tip") == feature_sha
                      and row.get("target_sha") == target_sha), None)
    if reference is None:
        return {**evidence, "valid": False, "reason": "current_reference_missing"}
    plan_id = reference.get("plan_id")
    receipt_sha256 = reference.get("receipt_sha256")
    receipt_path = reference.get("receipt_path")
    if not all(isinstance(value, str) for value in
               (plan_id, receipt_sha256, receipt_path)):
        return {**evidence, "valid": False, "reason": "reference_malformed"}
    root = _refresh_receipt_root(controller)
    path = Path(receipt_path).expanduser().resolve()
    try:
        path.relative_to(root)
        if path != root / task_id / f"{plan_id}.json":
            raise ValueError("receipt path mismatch")
        raw = path.read_bytes()
        plan = json.loads(raw)
    except (ValueError, OSError, json.JSONDecodeError):
        return {**evidence, "valid": False, "reason": "receipt_absent_or_unauthorized"}
    if hashlib.sha256(raw).hexdigest() != receipt_sha256:
        return {**evidence, "valid": False, "reason": "receipt_hash_mismatch"}
    if (not isinstance(plan, dict) or plan.get("schema_version") != REFRESH_SCHEMA
            or plan.get("task_id") != task_id or plan.get("plan_id") != plan_id
            or plan.get("target_sha") != target_sha
            or plan.get("refreshed_tip") != feature_sha
            or plan_id != digest({"schema_version": REFRESH_ID_SCHEMA,
                                  "plan": {key: value for key, value in plan.items()
                                           if key != "plan_id"}})):
        return {**evidence, "valid": False, "reason": "receipt_identity_mismatch"}
    return {**evidence, "valid": True, "reason": "current_receipt_bound_refresh",
            "plan_id": plan_id, "receipt_sha256": receipt_sha256}


def _runtime_identities(controller: Path, repository: Path, feature_sha: str,
                        findings: list[dict[str, Any]]) -> dict[str, Any]:
    runtime_path = controller / ".juno_task/scripts/merge_queue.py"
    runtime_hash = file_digest(runtime_path)
    template_path = "juno-code/src/templates/scripts/merge_queue.py"
    installed_path = controller / ".juno_task/runtime/identity.json"
    installed_hash = file_digest(installed_path)
    template = _blob_bytes(repository, feature_sha, template_path)
    product_runtime = _blob_bytes(repository, feature_sha, ".juno_task/scripts/merge_queue.py")
    if runtime_hash is None:
        findings.append(_finding(
            "runtime.missing", "error", "runtime_template_parity",
            {"path": str(runtime_path)}, "yy scripts update --force"))
    if template is not None and product_runtime is not None and template != product_runtime:
        findings.append(_finding(
            "runtime.template_mismatch", "error", "runtime_template_parity",
            {"runtime_sha256": hashlib.sha256(product_runtime).hexdigest(),
             "template_sha256": hashlib.sha256(template).hexdigest()},
            "sync .juno_task/scripts/merge_queue.py with juno-code/src/templates/scripts/merge_queue.py"))
    return {"running_path": str(runtime_path), "running_sha256": runtime_hash,
            "installed_identity_sha256": installed_hash,
            "feature_runtime_sha256": (hashlib.sha256(product_runtime).hexdigest()
                                        if product_runtime is not None else None),
            "feature_template_sha256": (hashlib.sha256(template).hexdigest()
                                         if template is not None else None)}


def merge_plan(controller: Path, task_id: str, against: Optional[str] = None,
               operation: Optional[str] = None) -> dict[str, Any]:
    """Return one byte-stable, offline feasibility report without durable writes."""
    if not task_runtime.TASK_RE.fullmatch(task_id):
        raise MergeQueueError("unsafe task id")
    findings: list[dict[str, Any]] = []
    config_path = controller / ".juno_task/config/task-workspace.json"
    risk_path = risk_policy_path(controller)
    try:
        config = task_runtime.load_config(controller)
    except (task_runtime.TaskWorkspaceError, OSError, json.JSONDecodeError) as exc:
        evidence = {"path": str(config_path), "sha256": file_digest(config_path),
                    "error": str(exc)}
        findings.append(_finding("policy.malformed", "error", "policy",
                                 evidence, "repair .juno_task/config/task-workspace.json"))
        body = {"schema_version": PLAN_SCHEMA, "task_id": task_id, "ready": False,
                "operation": operation or "next", "identities": {"task_policy": evidence},
                "composition": {"paths": [], "conflict_paths": []},
                "validation_commands": [], "findings": findings,
                "invalidation": {"rule": "any bound identity change invalidates this plan"}}
        return {**body, "plan_id": digest({"schema_version": PLAN_ID_SCHEMA, "report": body})}
    repository = task_runtime.product_repository(controller, config)
    state = task_runtime.read_state(controller)
    record = state.get("tasks", {}).get(task_id)
    if not isinstance(record, dict):
        raise MergeQueueError("task has no merge-queue record")
    submission_verification = verify_task_submission(
        controller, repository, task_id, record)
    if not submission_verification["valid"]:
        findings.append(_finding(
            "submission.invalid", "error", "task_queue_lifecycle",
            submission_verification, f"yy task status {task_id}"))
    if operation is None:
        operation = ("resolve" if record.get("state") in {"CONFLICT", "CONFLICT_RESOLVED"}
                     else "reopen" if record.get("state") in {
                         "REVIEW_FINDINGS", "REVIEW_FINDINGS_EXHAUSTED", "REQUEUING_STALE"}
                     else "next")
    target_ref = against or config["target_ref"]
    target_sha = optional_revision(repository, target_ref)
    if target_sha is None:
        raise MergeQueueError(f"against ref is not an exact commit: {target_ref}")
    base_sha, frozen_feature_sha = record.get("base_sha"), record.get("tip_sha")
    if not isinstance(base_sha, str) or not isinstance(frozen_feature_sha, str):
        raise MergeQueueError("task record lacks frozen base/tip identity")
    feature_sha = frozen_feature_sha
    if operation in {"reopen", "target-refresh"}:
        observed_branch_tip = task_runtime.git(
            repository, "rev-parse", record.get("branch_ref", ""), check=False)
        if task_runtime.SHA_RE.fullmatch(observed_branch_tip):
            feature_sha = observed_branch_tip

    policy_identity = {"task_workspace_sha256": file_digest(config_path),
                       "risk_policy_sha256": file_digest(risk_path)}
    try:
        risk_runtime.load_policy(risk_path)
    except (risk_runtime.RiskPolicyError, OSError, json.JSONDecodeError) as exc:
        findings.append(_finding("policy.risk_malformed", "error", "policy",
                                 {**policy_identity, "error": str(exc)},
                                 "repair .juno_task/config/risk-policy.json"))

    worktree_value = record.get("worktree")
    worktree = Path(worktree_value).resolve() if isinstance(worktree_value, str) else None
    if worktree is None or not worktree.is_dir():
        findings.append(_finding("task.worktree_missing", "error", "task_queue_lifecycle",
                                 {"worktree": worktree_value}, f"yy task status {task_id}"))
    else:
        dirty = task_runtime.git(worktree, "status", "--porcelain=v1",
                                 "--untracked-files=all", check=False)
        if dirty:
            findings.append(_finding("task.worktree_dirty", "error", "task_queue_lifecycle",
                                     {"paths": sorted(line[3:] for line in dirty.splitlines())},
                                     f"git -C {worktree} status --short"))
        observed_head = task_runtime.git(worktree, "rev-parse", "HEAD", check=False)
        branch_tip = task_runtime.git(repository, "rev-parse", record.get("branch_ref", ""), check=False)
        if observed_head != feature_sha or branch_tip != feature_sha:
            findings.append(_finding("task.tip_moved", "error", "ancestry_target_movement",
                                     {"frozen_tip": frozen_feature_sha,
                                      "planned_tip": feature_sha,
                                      "worktree_head": observed_head,
                                      "branch_tip": branch_tip}, f"yy task status {task_id}"))
    if task_runtime.run(["git", "-C", str(repository), "merge-base", "--is-ancestor",
                         base_sha, feature_sha], repository, check=False).returncode:
        findings.append(_finding("ancestry.forged_feature", "error", "ancestry_target_movement",
                                 {"base_sha": base_sha, "tip_sha": feature_sha},
                                 f"yy task status {task_id}"))
    target_descends_base = task_runtime.run(
        ["git", "-C", str(repository), "merge-base", "--is-ancestor", base_sha, target_sha],
        repository, check=False).returncode == 0
    if not target_descends_base:
        findings.append(_finding("target.not_descendant_of_base", "error",
                                 "ancestry_target_movement",
                                 {"base_sha": base_sha, "target_sha": target_sha},
                                 f"yy merge refresh plan {task_id}"))

    eligible = ({"next": {"QUEUED", "AWAITING_RISK", "REQUEUING_STALE"}, "resolve": {"CONFLICT", "CONFLICT_RESOLVED"},
                 "reopen": {"REVIEW_FINDINGS", "REVIEW_FINDINGS_EXHAUSTED",
                            "CONFLICT_RESOLVED", "QUEUED", "AWAITING_RISK",
                            "REOPENING", "REQUEUING_STALE"},
                 "target-refresh": {"QUEUED", "AWAITING_RISK", "REVIEW_FINDINGS",
                                    "CONFLICT_RESOLVED", "REOPENING", "REQUEUING_STALE"}}
                .get(operation, {"QUEUED", "CONFLICT", "CONFLICT_RESOLVED",
                                 "REVIEW_FINDINGS", "REVIEW_FINDINGS_EXHAUSTED",
                                 "REQUEUING_STALE"}))
    if record.get("state") not in eligible:
        findings.append(_finding("queue.state_ineligible", "error", "task_queue_lifecycle",
                                 {"state": record.get("state"), "eligible_states": sorted(eligible)},
                                 f"yy merge status"))
    blockers = _unmet_dependency_blockers(record)
    if blockers:
        findings.append(_finding("queue.dependencies_unmet", "error", "task_queue_lifecycle",
                                 {"task_ids": sorted(blockers)}, "yy merge status"))

    owner_raw = task_runtime.git(repository, "config", "--local", "--get",
                                 INTEGRATION_OWNER_CONFIG, check=False)
    owner_identity: dict[str, Any] = {"registered": bool(owner_raw), "path": owner_raw or None}
    if owner_raw:
        try:
            owner = Path(owner_raw).expanduser().resolve()
            observed = integration_owner_readback(owner)
            owner_identity["observed"] = observed
            if (not observed["clean"] or not observed["detached"] or not observed["full_checkout"]
                    or observed["role"] != "integration-owner"
                    or observed["authority"] != INTEGRATION_OWNER_AUTHORITY
                    or any(row["state"] != "exact" for row in observed["submodules"])):
                findings.append(_finding("topology.integration_owner_not_ready", "error",
                                         "topology_authority", observed, "yy integration repair --dry-run"))
        except (OSError, MergeQueueError, KeyError) as exc:
            findings.append(_finding("topology.integration_owner_invalid", "error",
                                     "topology_authority", {"path": owner_raw, "error": str(exc)},
                                     "yy integration status"))
    target_holders = sorted(row.get("worktree", "") for row in registered_worktrees(repository)
                            if row.get("branch") == target_ref)
    if target_holders:
        findings.append(_finding("topology.target_checked_out", "error", "topology_authority",
                                 {"worktrees": target_holders, "target_ref": target_ref},
                                 "yy integration repair --dry-run"))

    base_tree = _git_tree(repository, base_sha)
    target_tree = _git_tree(repository, target_sha)
    feature_tree = _git_tree(repository, feature_sha)
    prospective_tree, conflicts, prospective_tree_sha = _prospective_tree(
        repository, target_sha, feature_sha)
    if not prospective_tree_sha:
        findings.append(_finding("composition.failed", "error", "prospective_composition",
                                 {"target_sha": target_sha, "feature_sha": feature_sha},
                                 f"git merge-tree {target_sha} {feature_sha}"))
    if conflicts:
        findings.append(_finding("composition.conflicts", "error", "prospective_composition",
                                 {"paths": conflicts}, f"yy merge next {task_id}",
                                 invalidates=False, tests_safe=False))
    all_paths = sorted(set(base_tree) | set(target_tree) | set(feature_tree) | set(prospective_tree))
    frozen_allowed = (record.get("creation_receipt") or {}).get("allowed_paths", config["allowed_paths"])
    generated_paths: set[str] = set()
    generated_bindings: list[dict[str, Any]] = []
    generated = (record.get("creation_receipt") or {}).get("generated_output_admission")
    if isinstance(generated, dict):
        generated_paths.update(path for path in generated.get("destinations", []) if isinstance(path, str))
        generated_bindings = [row for row in generated.get("bindings", [])
                              if isinstance(row, dict)]
        generated_paths.update(row["destination"] for row in generated_bindings
                               if isinstance(row.get("destination"), str))
    classifications: list[dict[str, Any]] = []
    admitted = sorted(record.get("changed_paths", []))
    admitted_set = set(admitted)
    eligible_generated = {
        row["destination"] for row in generated_bindings
        if row.get("kind") == "managed"
        and isinstance(row.get("source"), str)
        and isinstance(row.get("destination"), str)
        and row["source"] in admitted_set
        and feature_tree.get(row["source"]) is not None
        and feature_tree.get(row["source"]) == feature_tree.get(row["destination"])
    }
    # Pre-binding task receipts shipped the canonical merge runtime as a
    # managed asset but omitted it from generated_output_admission. Preserve a
    # single exact compatibility pair; both the admitted source and byte-exact
    # feature blobs remain mandatory.
    legacy_managed_pairs = {
        "juno-code/src/templates/scripts/merge_queue.py":
            ".juno_task/scripts/merge_queue.py",
    }
    eligible_generated.update(
        destination for source, destination in legacy_managed_pairs.items()
        if source in admitted_set
        and feature_tree.get(source) is not None
        and feature_tree.get(source) == feature_tree.get(destination)
    )
    for path in all_paths:
        base_blob, target_blob = base_tree.get(path), target_tree.get(path)
        feature_blob, merged_blob = feature_tree.get(path), prospective_tree.get(path)
        origins: list[str] = []
        # A supported target refresh retains the original frozen authored-path
        # admission. Bytes equal to the protected target on every other path
        # are inherited, not newly authored by the task.
        if feature_blob != base_blob and (path in admitted_set or feature_blob != target_blob):
            origins.append("task-authored")
        if target_blob != base_blob and feature_blob == target_blob and path not in admitted_set:
            origins.append("unchanged target-derived")
        if path in conflicts:
            origins.append("conflicted")
        if path in generated_paths:
            origins.append("generated/managed output")
        if merged_blob != target_blob and path not in conflicts:
            origins.append("guarded candidate byte")
        if (_path_allowed(path, config["controller_private_paths"])
                or not _path_allowed(path, frozen_allowed)):
            origins.append("disallowed/controller-private")
        if origins and (base_blob != target_blob or base_blob != feature_blob
                        or target_blob != merged_blob):
            classifications.append({"path": path, "origins": origins,
                                    "base_blob": base_blob, "target_blob": target_blob,
                                    "feature_blob": feature_blob, "prospective_blob": merged_blob})
    disallowed = sorted(row["path"] for row in classifications
                        if "disallowed/controller-private" in row["origins"]
                        and "task-authored" in row["origins"])
    authored = sorted(row["path"] for row in classifications if "task-authored" in row["origins"])
    missing_admitted = sorted(admitted_set - set(authored))
    unexpected_authored = sorted(set(authored) - admitted_set - eligible_generated)
    if disallowed or (admitted and (missing_admitted or unexpected_authored)):
        findings.append(_finding("admission.path_scope", "error", "path_admission",
                                 {"disallowed": disallowed, "authored": authored,
                                  "admitted": admitted,
                                  "eligible_generated": sorted(eligible_generated),
                                  "missing_admitted": missing_admitted,
                                  "unexpected_authored": unexpected_authored},
                                 f"yy task status {task_id}"))

    origin_projection = task_runtime.decisions.project_path_origins(
        base_tree=base_tree, source_tree=feature_tree, target_tree=target_tree,
        candidate_tree=prospective_tree, admitted_paths=admitted,
        generated_bindings=generated_bindings, conflict_paths=conflicts)
    if origin_projection["ambiguous_paths"]:
        findings.append(_finding(
            "admission.ambiguous_legacy_paths", "error", "path_admission",
            {"paths": origin_projection["ambiguous_paths"],
             "projection_schema": origin_projection["schema_version"]},
            f"yy task status {task_id}"))

    package_paths = sorted(path for path in set(target_tree) | set(feature_tree)
                           if path.endswith(("package.json", "package-lock.json")))
    packages = {"target": [_json_file_identity(repository, target_sha, path)
                           for path in package_paths],
                "feature": [_json_file_identity(repository, feature_sha, path)
                            for path in package_paths]}
    for side, identities in packages.items():
        for identity in identities:
            if identity["path"].endswith("package.json") and identity["present"] and (
                    identity.get("malformed") or identity.get("valid_semver") is False):
                findings.append(_finding("package.invalid_semver", "error",
                                         "package_lock_version", {"side": side, **identity},
                                         f"repair {identity['path']} with strict SemVer"))
    for path in package_paths:
        if path.endswith("package-lock.json"):
            target_item = next(row for row in packages["target"] if row["path"] == path)
            feature_item = next(row for row in packages["feature"] if row["path"] == path)
            if target_item["present"] and feature_item["present"] and target_item["sha256"] != feature_item["sha256"]:
                pair = _target_refresh_package_pair(
                    repository, feature_sha, path, set(authored))
                refresh = ({"valid": True, "reason": "exact_base_refresh_not_required",
                            "feature_sha": feature_sha, "target_sha": target_sha}
                           if target_sha == base_sha else
                           {"valid": True, "reason": "target_refresh_planning"}
                           if operation == "target-refresh" else
                           _current_target_refresh_receipt(
                               controller, task_id, record, feature_sha, target_sha))
                if not pair["valid"] or not refresh["valid"]:
                    findings.append(_finding("package.lock_diverged", "error", "package_lock_version",
                                             {"path": path, "target_sha256": target_item["sha256"],
                                              "feature_sha256": feature_item["sha256"],
                                              "target_refresh_pair": pair,
                                              "target_refresh_receipt": refresh},
                                             f"yy merge refresh plan {task_id}"))
    versions = {row["path"]: row.get("version") for row in packages["feature"]
                if row["path"].endswith("package.json") and isinstance(row.get("version"), str)}
    fixture_hits: list[dict[str, str]] = []
    stable_re = re.compile(r"(?<![0-9A-Za-z-])(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?![0-9A-Za-z.+-])")
    # Literal SemVer matches are heuristic evidence, not proof of a stale
    # expectation. Limit the scan to task-authored validation sources so
    # unchanged repository fixtures cannot make every merge infeasible.
    for path in authored:
        if not ("test" in path.lower() or "fixture" in path.lower()):
            continue
        raw = _blob_bytes(repository, feature_sha, path)
        if raw is None or len(raw) > 1024 * 1024:
            continue
        text = raw.decode("utf-8", "ignore")
        hits = sorted(set(stable_re.findall(text)) - set(versions.values()))
        if hits:
            fixture_hits.extend({"path": path, "literal": value} for value in hits)
    if fixture_hits:
        findings.append(_finding("validation.hardcoded_semver_fixture", "warning",
                                 "validation_plan", {"matches": fixture_hits},
                                 "replace hardcoded fixture versions with package-bound SemVer",
                                 tests_safe=True))

    suite_commands, suite_routing = task_runtime.selected_full_suite_commands(
        config, authored)
    validation_commands = [{**row, "phase": "full_suite",
                            "command": " ".join(row["argv"])}
                           for row in suite_commands]
    focused: list[dict[str, Any]] = []
    for row in task_runtime.selected_focused_rows(config, authored):
        item = {key: row[key] for key in ("id", "cwd", "argv", "timeout_seconds", "max_output_bytes")}
        if "resource" in row:
            item["resource"] = row["resource"]
        item.update({"phase": "focused", "command": " ".join(item["argv"])})
        focused.append(item)
        cwd = Path(record.get("worktree", "")) / item["cwd"]
        if not cwd.is_dir():
            findings.append(_finding("validation.cwd_missing", "error", "dependency_readiness",
                                     {"id": item["id"], "cwd": str(cwd)},
                                     f"restore validation cwd {item['cwd']}"))
        lock = cwd / "package-lock.json"
        if lock.is_file() and not (cwd / "node_modules").is_dir():
            findings.append(_finding("validation.dependencies_missing", "error",
                                     "dependency_readiness", {"id": item["id"],
                                     "lock_sha256": file_digest(lock)},
                                     f"cd {item['cwd']} && npm ci"))
    validation_commands = focused + validation_commands
    runtime_identity = _runtime_identities(controller, repository, feature_sha, findings)
    queue_entry = state.get("queues", {}).get(target_key(repository, config["target_ref"]))
    identities = {
        "repository_common_dir": repository_identity(repository),
        "controller": {"path": str(controller),
                       "head": optional_revision(controller, "HEAD"),
                       "ref": task_runtime.git(controller, "symbolic-ref", "-q", "HEAD", check=False)},
        "task": {"task_id": task_id, "state": record.get("state"), "base_sha": base_sha,
                 "frozen_tip_sha": frozen_feature_sha, "tip_sha": feature_sha,
                 "branch_ref": record.get("branch_ref"),
                 "worktree": worktree_value, "record_sha256": digest(record)},
        "target": {"ref": target_ref, "sha": target_sha, "configured_ref": config["target_ref"]},
        "queue_sha256": digest(queue_entry), "policy": policy_identity,
        "runtime": runtime_identity, "packages": packages,
        "integration_owner": owner_identity,
        "prospective_tree_sha": prospective_tree_sha,
        "validation_routing": suite_routing,
        "validation_sha256": digest(validation_commands),
    }
    findings.sort(key=lambda row: (row["phase"], row["code"], canonical(row["evidence"])))
    blocking = [row for row in findings if row["severity"] == "error"]
    body = {"schema_version": PLAN_SCHEMA, "task_id": task_id, "operation": operation,
            "ready": not blocking, "identities": identities,
            "composition": {"paths": classifications, "conflict_paths": conflicts,
                            "origin_projection": origin_projection,
                            "refresh_eligible": target_descends_base,
                            "target_moved_from_base": target_sha != base_sha},
            "validation_commands": validation_commands, "findings": findings,
            "invalidation": {"rule": "any bound identity change invalidates this plan",
                             "execution_option": "--plan-id <plan_id>"}}
    return {**body, "plan_id": digest({"schema_version": PLAN_ID_SCHEMA, "report": body})}


def assert_static_plan(controller: Path, task_id: str, operation: str,
                       expected_plan_id: Optional[str] = None) -> dict[str, Any]:
    report = merge_plan(controller, task_id, operation=operation)
    if expected_plan_id is not None and report["plan_id"] != expected_plan_id:
        raise MergeQueueError("merge feasibility plan is stale; rerun yy merge plan")
    # The running script itself is execution authority; imported test harnesses
    # may not materialize it beneath their synthetic controller. Planning still
    # reports that packaging blocker. Existing CONFLICT/repair paths likewise
    # own their explicit conflict surface after this shared static pass.
    allowed = {"runtime.missing", "composition.conflicts"}
    # These gates describe supported refresh/readiness repair and remain in the
    # report/identity, while the established operation owns their transition.
    if operation == "resolve":
        allowed.update({"topology.target_checked_out",
                        "topology.integration_owner_not_ready"})
    blockers = [row["code"] for row in report["findings"]
                if row["severity"] == "error" and row["code"] not in allowed]
    if blockers:
        prefix = ("target ref is checked out; "
                  if "topology.target_checked_out" in blockers else
                  "task worktree must be clean; "
                  if "task.worktree_dirty" in blockers else "")
        raise MergeQueueError(prefix + "merge feasibility blocked before validation: "
                              + ", ".join(blockers))
    return report


def validate_record(config: dict[str, Any], repository: Path, record: dict[str, Any]) -> Path:
    required = {"task_id", "state", "repository", "target_ref", "base_sha", "branch_ref",
                "worktree", "tip_sha", "changed_paths", "validation"}
    if not required.issubset(record):
        raise MergeQueueError("queued task record is incomplete")
    worktree = task_runtime.exact_root(Path(record["worktree"]), "recorded feature worktree")
    tip = record["tip_sha"]
    if Path(record["repository"]).resolve() != repository or record["target_ref"] != config["target_ref"]:
        raise MergeQueueError("queued task repository/target identity drifted")
    if task_runtime.git(worktree, "status", "--porcelain=v1", "--untracked-files=all"):
        raise MergeQueueError("queued feature worktree is dirty; preserve it for inspection")
    if task_runtime.git(worktree, "rev-parse", "HEAD") != tip:
        raise MergeQueueError("queued feature worktree tip drifted")
    if task_runtime.git(repository, "rev-parse", record["branch_ref"], check=False) != tip:
        raise MergeQueueError("queued feature branch tip drifted")
    if task_runtime.run(["git", "-C", str(repository), "merge-base", "--is-ancestor", record["base_sha"], tip], repository, check=False).returncode:
        raise MergeQueueError("queued feature no longer descends from its frozen base")
    return worktree


def select_next(controller: Path, config: dict[str, Any]) -> dict[str, Any]:
    with task_runtime.state_lock(controller):
        tasks = task_runtime.read_state(controller)["tasks"]
        candidates = [row for row in tasks.values() if isinstance(row, dict)
                      and row.get("state") == "QUEUED"
                      and row.get("target_ref") == config["target_ref"]]
    if not candidates:
        raise MergeQueueError("no QUEUED task is ready for this target")
    return sorted(candidates, key=lambda row: (
        row.get("enqueue_sequence", 2**63 - 1), row["task_id"]
    ))[0]


def candidate_directory(controller: Path, task_id: str, target_sha: str, feature_sha: str) -> Path:
    root = controller / ".juno_task/runtime/merge-queue/candidates"
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f"{task_id}-{target_sha[:10]}-{feature_sha[:10]}-", dir=root))


def owner_marker(controller: Path, checkout: Path) -> Path:
    root = (controller / ".juno_task/runtime/merge-queue/candidates").resolve()
    return root / f".{checkout.resolve().name}.owner.json"


def create_candidate_checkout(controller: Path, repository: Path, task_id: str,
                              target_ref: str, target_sha: str, feature_sha: str) -> tuple[Path, str]:
    checkout = candidate_directory(controller, task_id, target_sha, feature_sha)
    checkout.rmdir()
    token = secrets.token_hex(24)
    task_runtime.run(["git", "-C", str(repository), "worktree", "add", "--detach",
                      str(checkout), target_sha], repository)
    marker = owner_marker(controller, checkout)
    ownership = {"schema_version": OWNER_SCHEMA, "token": token, "task_id": task_id,
                 "repository_identity": repository_identity(repository), "target_ref": target_ref,
                 "target_sha": target_sha, "feature_sha": feature_sha,
                 "candidate_checkout": str(checkout.resolve())}
    try:
        # A repository migrated to worktree-local sparse configuration may
        # still retain the legacy common core.sparseCheckout=true value. New
        # worktrees inherit that common value until they establish their own
        # setting, so an internal candidate created from a sparse controller
        # can otherwise omit every product path. Candidates are product
        # validation roots and must always be explicitly full checkouts.
        task_runtime.run(["git", "-C", str(checkout), "sparse-checkout", "disable"], checkout)
        sparse = task_runtime.git(
            checkout, "config", "--worktree", "--bool", "core.sparseCheckout", check=False
        )
        skipped = [line for line in task_runtime.git(
            checkout, "ls-files", "-t", check=False
        ).splitlines() if line.startswith("S ")]
        if sparse not in {"", "false"} or skipped:
            raise MergeQueueError("candidate full-checkout materialization failed")
        with marker.open("x") as handle:
            handle.write(canonical(ownership) + "\n")
    except Exception:
        # Registration succeeded but ownership admission did not. The exact
        # fresh path is still known in this stack frame; leave a loud error if
        # Git itself refuses the internal rollback.
        removed = task_runtime.run(["git", "-C", str(repository), "worktree", "remove", "--force",
                                    str(checkout)], repository, check=False)
        marker.unlink(missing_ok=True)
        if removed.returncode:
            raise MergeQueueError(f"candidate ownership creation failed and rollback failed: {checkout}")
        raise
    return checkout, token


def read_candidate_owner(controller: Path, checkout: Path) -> dict[str, Any]:
    marker = owner_marker(controller, checkout)
    try:
        value = json.loads(marker.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise MergeQueueError(f"candidate ownership marker is missing or invalid: {marker}") from exc
    required = {"schema_version", "token", "task_id", "repository_identity", "target_ref",
                "target_sha", "feature_sha", "candidate_checkout"}
    if not isinstance(value, dict) or set(value) != required or value.get("schema_version") != OWNER_SCHEMA:
        raise MergeQueueError(f"candidate ownership marker schema is invalid: {marker}")
    return value


def verify_candidate_owner(controller: Path, repository: Path, checkout: Path, token: str) -> dict[str, Any]:
    checkout = checkout.resolve()
    root = (controller / ".juno_task/runtime/merge-queue/candidates").resolve()
    try:
        checkout.relative_to(root)
    except ValueError as exc:
        raise MergeQueueError("candidate path is outside the configured queue root") from exc
    owner = read_candidate_owner(controller, checkout)
    if (owner["token"] != token or owner["candidate_checkout"] != str(checkout)
            or owner["repository_identity"] != repository_identity(repository)):
        raise MergeQueueError("candidate ownership token or repository identity mismatch")
    rows = [row for row in registered_worktrees(repository)
            if Path(row.get("worktree", "")).resolve() == checkout]
    if len(rows) != 1 or rows[0].get("branch") or not rows[0].get("detached"):
        raise MergeQueueError("candidate is not the exact registered detached queue worktree")
    if repository_identity(checkout) != repository_identity(repository):
        raise MergeQueueError("candidate checkout common-dir identity mismatch")
    return owner


def rollback_unadmitted_candidate(controller: Path, repository: Path, checkout: Path, token: str) -> None:
    """Remove only the exact current-attempt internal worktree, even if conflicted."""
    verify_candidate_owner(controller, repository, checkout, token)
    marker = owner_marker(controller, checkout)
    removed = task_runtime.run(["git", "-C", str(repository), "worktree", "remove", "--force",
                                str(checkout)], repository, check=False)
    still_registered = any(Path(row.get("worktree", "")).resolve() == checkout.resolve()
                           for row in registered_worktrees(repository))
    if removed.returncode or checkout.exists() or still_registered:
        orphan = Path(repository_identity(repository)) / "juno-orphans/merge-queue" / f"{token}.json"
        orphan.parent.mkdir(parents=True, exist_ok=True)
        orphan.write_text(canonical({"schema_version": OWNER_SCHEMA, "outcome": "ORPHANED",
                                     "candidate_owner": read_candidate_owner(controller, checkout),
                                     "controller_marker": str(marker),
                                     "remove_exit_code": removed.returncode,
                                     "still_registered": still_registered,
                                     "path_exists": checkout.exists()}) + "\n")
        raise MergeQueueError(f"unadmitted candidate rollback failed; orphan marker: {orphan}")
    marker.unlink()


@contextmanager
def validation_dependencies(candidate: Path, cwd: Path,
                            source_root: Optional[Path]) -> Iterator[None]:
    """Temporarily expose exact lock-compatible Node dependencies to a candidate."""
    lock = cwd / "package-lock.json"
    if source_root is None or not lock.is_file():
        yield
        return
    relative_cwd = cwd.relative_to(candidate)
    source_cwd = (source_root / relative_cwd).resolve()
    try:
        source_cwd.relative_to(source_root.resolve())
    except ValueError as exc:
        raise MergeQueueError("validation dependency source escaped feature worktree") from exc
    source_lock = source_cwd / "package-lock.json"
    source_modules = source_cwd / "node_modules"
    candidate_modules = cwd / "node_modules"
    if not source_lock.is_file():
        raise MergeQueueError("candidate validation package lock differs from feature worktree")
    lock_digest = hashlib.sha256(lock.read_bytes()).digest()
    source_lock_digest = hashlib.sha256(source_lock.read_bytes()).digest()
    if lock_digest != source_lock_digest:
        lock_path = lock.relative_to(candidate).as_posix()
        source_lock_path = source_lock.relative_to(source_root.resolve()).as_posix()
        candidate_blob = task_runtime.git(candidate, "rev-parse", f"HEAD:{lock_path}", check=False)
        source_blob = task_runtime.git(source_root, "rev-parse", f"HEAD:{source_lock_path}", check=False)
        candidate_head = task_runtime.git(candidate, "rev-parse", "HEAD", check=False)
        source_head = task_runtime.git(source_root, "rev-parse", "HEAD", check=False)
        identities = (candidate_blob, source_blob, candidate_head, source_head)
        if (lock_path != source_lock_path
                or any(not re.fullmatch(r"[0-9a-f]{40,64}", value) for value in identities)):
            raise MergeQueueError("candidate validation package lock differs from feature worktree")
        raise DependencyLockMismatchError({
            "schema_version": "juno_merge_queue_dependency_lock_refusal.v1",
            "lock_path": lock_path,
            "candidate_head": candidate_head,
            "candidate_blob": candidate_blob,
            "candidate_sha256": lock_digest.hex(),
            "source_head": source_head,
            "source_blob": source_blob,
            "source_sha256": source_lock_digest.hex(),
        })
    if not source_modules.is_dir():
        raise MergeQueueError("lock-compatible feature dependencies are unavailable")
    relative_modules = str(candidate_modules.relative_to(candidate))
    ignore_probe = f"{relative_modules}/.juno-validation-dependency-probe"
    ignored = task_runtime.run(
        ["git", "-C", str(candidate), "check-ignore", "--quiet", "--", ignore_probe],
        candidate, check=False,
    )
    if ignored.returncode:
        raise MergeQueueError("candidate validation dependency path is not Git-ignored")

    def assert_clean() -> None:
        if task_runtime.git(candidate, "status", "--porcelain=v1", "--untracked-files=all",
                            check=False):
            raise MergeQueueError("candidate dependency bridge changed Git-visible state")

    def assert_locks_unchanged() -> None:
        if (not lock.is_file() or not source_lock.is_file()
                or hashlib.sha256(lock.read_bytes()).digest() != lock_digest
                or hashlib.sha256(source_lock.read_bytes()).digest() != source_lock_digest):
            raise MergeQueueError("candidate validation package lock identity changed")

    source_canonical = source_modules.resolve(strict=True)
    candidate_canonical = candidate_modules.resolve(strict=False)
    if source_canonical == candidate_canonical:
        # A direct-descendant candidate is the feature worktree itself. Its
        # hydrated dependencies are already the exact lock-compatible source;
        # do not manufacture a bridge over the directory that supplies them.
        provenance = source_modules.stat()
        assert_clean()
        try:
            yield
        finally:
            if (not candidate_modules.is_dir()
                    or candidate_modules.resolve(strict=False) != source_canonical):
                raise MergeQueueError("candidate validation dependency identity changed")
            current = candidate_modules.stat()
            if (current.st_dev, current.st_ino) != (provenance.st_dev, provenance.st_ino):
                raise MergeQueueError("candidate validation dependency identity changed")
            assert_locks_unchanged()
            assert_clean()
        return

    if candidate_modules.exists() or candidate_modules.is_symlink():
        raise MergeQueueError("candidate validation dependency path already exists")
    candidate_modules.mkdir()
    linked: list[tuple[Path, Path]] = []
    try:
        for source_entry in sorted(source_modules.iterdir(), key=lambda path: path.name):
            candidate_entry = candidate_modules / source_entry.name
            candidate_entry.symlink_to(source_entry, target_is_directory=source_entry.is_dir())
            linked.append((candidate_entry, source_entry))
        assert_clean()
        yield
    finally:
        for candidate_entry, source_entry in reversed(linked):
            if (not candidate_entry.is_symlink()
                    or candidate_entry.resolve(strict=False) != source_entry.resolve(strict=False)):
                raise MergeQueueError("candidate dependency bridge identity changed")
            candidate_entry.unlink()
        candidate_modules.rmdir()
        assert_locks_unchanged()
        assert_clean()


def validation_rows(config: dict[str, Any], candidate: Path,
                    dependency_source: Optional[Path] = None,
                    changed_paths: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """Run the routing-selected focused rows against one exact checkout."""
    evidence = []
    rows = (task_runtime.selected_focused_rows(config, changed_paths)
            if changed_paths is not None else config["focused_validation"])
    for row in rows:
        cwd = (candidate / row["cwd"]).resolve()
        try:
            cwd.relative_to(candidate)
        except ValueError as exc:
            raise MergeQueueError("affected validation cwd escaped candidate") from exc
        with validation_dependencies(candidate, cwd, dependency_source):
            result = task_runtime.run_validation(row, cwd)
        evidence.append(result)
        if result["timed_out"] or result["exit_code"]:
            detail = result["stderr_tail"] or result["stdout_tail"]
            raise MergeValidationError(f"affected validation failed ({row['id']}): {detail}", evidence)
    return evidence


def full_suite_command(config: dict[str, Any]) -> dict[str, Any]:
    row = config["full_suite_validation"]
    command = {key: row[key] for key in
               ("id", "cwd", "argv", "timeout_seconds", "max_output_bytes")}
    if "resource" in row:
        command["resource"] = row["resource"]
    return command


def full_suite_selection(config: dict[str, Any],
                         changed_paths: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Deterministic package-local suite routing from the authored Git paths."""
    return task_runtime.selected_full_suite_commands(config, changed_paths)


def fit_full_suite_receipt(receipt: dict[str, Any], limit: int) -> dict[str, Any]:
    """Fit captured UTF-8 tails inside the whole immutable receipt bound."""
    if len(risk_runtime.canonical(receipt)) <= limit:
        return receipt
    original = {
        name: receipt["result"][name]["tail"].encode("utf-8")
        for name in ("stdout", "stderr")
    }

    def with_cap(cap: int) -> dict[str, Any]:
        fitted = json.loads(json.dumps(receipt))
        for name, data in original.items():
            suffix = data[-cap:] if cap else b""
            tail = suffix.decode("utf-8", errors="ignore")
            kept = len(tail.encode("utf-8"))
            fitted["result"][name]["tail"] = tail
            fitted["result"][name]["truncated_bytes"] += len(data) - kept
        return fitted

    low, high = 0, max((len(value) for value in original.values()), default=0)
    best = with_cap(0)
    if len(risk_runtime.canonical(best)) > limit:
        raise MergeQueueError("full-suite receipt metadata exceeds its bound")
    while low <= high:
        middle = (low + high) // 2
        candidate = with_cap(middle)
        if len(risk_runtime.canonical(candidate)) <= limit:
            best, low = candidate, middle + 1
        else:
            high = middle - 1
    return best


FULL_SUITE_RETRY_MAX_FILES = 4
FULL_SUITE_RETRY_MAX_ATTEMPTS = 2
FULL_SUITE_RETRY_TAIL_BYTES = 4096  # verifier bound: _validate_full_suite_retries
FULL_SUITE_RETRY_LOG_READ_BYTES = 1_048_576
_VITEST_FAIL_LINE = re.compile(
    r"^\s*(?:\x1b\[[0-9;]*m|\s)*FAIL(?:\s|\x1b\[[0-9;]*m)+(\S+\.test\.[a-zA-Z0-9]+)(?:\s|$)")
_VITEST_SUMMARY_FILES = re.compile(
    r"(?:\x1b\[[0-9;]*m|\s)*Test Files(?:\s|\x1b\[[0-9;]*m)+(\d+)\s+failed")


def _vitest_suite_row(row: dict[str, Any]) -> bool:
    """Retry eligibility: only vitest suite invocations, never build/typecheck."""
    return row["argv"][:2] == ["npm", "test"]


def _vitest_failed_files(output: str) -> list[str]:
    """Ordered unique failing test files from bounded vitest output."""
    files: list[str] = []
    for line in output.splitlines():
        match = _VITEST_FAIL_LINE.match(line)
        if match and match.group(1) not in files:
            files.append(match.group(1))
    return files


def _suite_failure_output(evidence: dict[str, Any]) -> Optional[str]:
    """Authoritative bounded reporter output for retry parsing.

    Admission rows cap receipt tails at max_output_bytes while real suites
    emit far more, so the tail is routinely truncated. run_validation preserves
    the complete combined stream in its long-run log: parse that instead. The
    bounded log tail is rejected when the terminal summary is absent, and the
    caller still reconciles the summary count against the parsed FAIL lines.
    """
    log_path = evidence.get("log_path")
    if (isinstance(log_path, str) and log_path
            and evidence.get("log_write_failed") is not True):
        try:
            with open(log_path, "rb") as stream:
                stream.seek(0, os.SEEK_END)
                size = stream.tell()
                stream.seek(max(0, size - FULL_SUITE_RETRY_LOG_READ_BYTES))
                data = stream.read()
        except OSError:
            return None
        return data.decode("utf-8", errors="replace")
    if evidence["stdout_truncated_bytes"] or evidence["stderr_truncated_bytes"]:
        return None
    return "\n".join(
        part for part in (evidence["stderr_tail"], evidence["stdout_tail"]) if part)


def bounded_file_retry(row: dict[str, Any], cwd: Path, evidence: dict[str, Any],
                       candidate: Path,
                       dependency_source: Optional[Path] = None) -> Optional[dict[str, Any]]:
    """Bounded isolated re-run of failing vitest files; joins verdict fail-closed.

    One ambient-load flake must not restart a whole admission: when a vitest
    suite command fails with a small parseable set of failing files, each file
    re-runs alone up to FULL_SUITE_RETRY_MAX_ATTEMPTS times. The joined verdict
    passes only if every failing file passes isolated; a file that also fails
    alone is a real failure and the original verdict stands. Timeouts, broad
    failures, and non-vitest commands are never retried.

    Absorption is bound to the reporter's own terminal summary read from the
    preserved full run log (bounded to its last megabyte; untruncated tails are
    the fallback): a "Test Files  N failed" summary must be present and its
    failed-file count must equal the parsed unique FAIL-line count. Truncated
    or aborted reporter output therefore yields no retry instead of silently
    absorbing an unseen failure.
    """
    if evidence["timed_out"] or not _vitest_suite_row(row):
        return None
    combined_output = _suite_failure_output(evidence)
    if combined_output is None:
        return None
    files = _vitest_failed_files(combined_output)
    if not files or len(files) > FULL_SUITE_RETRY_MAX_FILES:
        return None
    summary = _VITEST_SUMMARY_FILES.search(combined_output)
    if summary is None or int(summary.group(1)) != len(files):
        return None
    entries: list[dict[str, Any]] = []
    for suite_file in files:
        attempts: list[dict[str, Any]] = []
        passed = False
        for attempt in range(1, FULL_SUITE_RETRY_MAX_ATTEMPTS + 1):
            retry_row = {**row,
                         "id": f"{row['id']}#retry{attempt}:{Path(suite_file).name}",
                         "argv": [*row["argv"], "--", suite_file]}
            with validation_dependencies(candidate, cwd, dependency_source):
                run = task_runtime.run_validation(retry_row, cwd)
            attempts.append({"exit_code": run["exit_code"], "timed_out": run["timed_out"]})
            final_tail = run["stderr_tail"] or run["stdout_tail"]
            if run["exit_code"] == 0 and not run["timed_out"]:
                passed = True
                break
        # Slice by bytes, not chars: a multibyte-heavy tail sliced by chars can
        # exceed the verifier's 4096-byte bound and be refused fail-closed.
        tail_bytes = final_tail.encode("utf-8", errors="replace")
        entries.append({"file": suite_file, "passed": passed, "attempts": attempts,
                        "final_tail": tail_bytes[-FULL_SUITE_RETRY_TAIL_BYTES:].decode(
                            "utf-8", errors="ignore")})
        if not passed:
            break
    return {
        "policy": {"max_files": FULL_SUITE_RETRY_MAX_FILES,
                   "max_attempts_per_file": FULL_SUITE_RETRY_MAX_ATTEMPTS},
        "files": entries,
        "absorbed": bool(entries) and all(entry["passed"] for entry in entries),
    }


CANONICAL_VALIDATION_RECEIPT_SCHEMA = "juno_canonical_validation_receipt.v1"
CANONICAL_VALIDATION_ROOT = ".juno_task/runtime/validation-receipts"
# Behavior-affecting environment keys admitted into evidence identity. v1 is
# deliberately empty: admission suites must not depend on ambient env at all.
EVIDENCE_ENV_KEYS: tuple[str, ...] = ()


def _bounded_text(path: Path, limit: int) -> Optional[str]:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) > limit:
        return None
    return data.decode("utf-8", errors="replace")


def _toolchain_versions(cwd: Path) -> dict[str, Optional[str]]:
    def probe(argv: list[str]) -> Optional[str]:
        try:
            result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                                     text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return result.stdout.strip()[:64] if result.returncode == 0 else None
    package = _bounded_text(cwd / "package.json", 1 << 16)
    package_version: Optional[str] = None
    if package is not None:
        try:
            package_version = str(json.loads(package).get("version"))[:64]
        except (UnicodeError, json.JSONDecodeError):
            package_version = None
    return {"package_version": package_version,
            "node": probe(["node", "--version"]),
            "python": probe(["python3", "--version"])}


def validation_evidence_identity(row: dict[str, Any], candidate: Path, cwd: Path,
                                  identity: dict[str, str], plan: dict[str, Any],
                                  repository: Path) -> dict[str, Any]:
    """Content-addressed behavioral identity of one validation command.

    Every behavior-affecting input is hashed: the candidate tree (which covers
    test sources and submodule gitlinks), the exact command row, the frozen
    validation-policy identity, the exact dependency lock bytes, the executing
    toolchain (package/node/python), the repository identity (cross-repository
    receipts refuse reuse), and the admitted environment keys. One byte changed
    in any input yields a different key and forces fresh validation.
    """
    lock = cwd / "package-lock.json"
    lock_sha: Optional[str] = None
    if lock.is_file():
        lock_sha = hashlib.sha256(lock.read_bytes()).hexdigest()
    material = {
        "schema_version": lifecycle_runtime.COMMAND_CLOSURE_SCHEMA,
        "candidate_tree": plan["candidate"]["candidate_tree"],
        "policy_identity": plan["policy_identity"],
        "command": {key: row[key] for key in
                    ("id", "cwd", "argv", "timeout_seconds", "max_output_bytes")},
        "validation_identity": identity,
        "dependency_lock_sha256": lock_sha,
        "toolchain": _toolchain_versions(cwd),
        "repository_identity": repository_identity(repository),
        "environment_keys": list(EVIDENCE_ENV_KEYS),
    }
    return {**material, "input_closure_sha256": lifecycle_runtime.digest(material)}


def full_suite_validation(commands: list[dict[str, Any]], candidate: Path,
                          plan: dict[str, Any], identity: dict[str, str],
                          receipt_paths: list[Path], claim: dict[str, Any],
                          dependency_source: Optional[Path] = None,
                          controller: Optional[Path] = None,
                          repository: Optional[Path] = None,
                          cancel_event: Any = None
                          ) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """Run the routed suite in order; write one immutable receipt per command.

    A receipt prefix already on disk is a crash-recovery boundary: each prefix
    receipt is strictly re-verified before the remaining commands resume.

    When controller and repository are provided, each command consumes the
    shared canonical terminal-result index. Attempt receipts remain claim-bound
    protocol projections; they neither redefine the command key nor authorize
    delivery, and the returned rows expose actual execution/reuse decisions.
    """
    if len(receipt_paths) != len(commands):
        raise MergeQueueError("full-suite receipt schedule does not bind its commands")
    references: list[dict[str, str]] = []
    reuse_rows: list[dict[str, Any]] = []
    for index, (row, receipt_path) in enumerate(zip(commands, receipt_paths)):
        if cancel_event is not None and cancel_event.is_set():
            raise MergeValidationError("full-suite validation cancelled by blocking Reviewer A", [],
                                       references[-1] if references else None)
        if receipt_path.exists():
            reference = evidence_reference(receipt_path)
            verified = risk_runtime.verify_full_suite_receipt_v3(
                reference, plan, identity, commands, claim, require_success=False)
            if verified["timed_out"] or verified["exit_code"]:
                raise MergeValidationError(
                    f"recovered full-suite attempt failed ({row['id']})",
                    [verified], reference)
            references.append(reference)
            recovered_closure = (validation_evidence_identity(
                row, candidate, (candidate / row["cwd"]).resolve(), identity, plan, repository)
                if repository is not None else {"input_closure_sha256": None})
            reuse_rows.append(lifecycle_runtime.evidence_decision(
                row["id"], "reused", closure=recovered_closure,
                source=reference, reason="durable stage receipt prefix verified"))
            continue
        cwd = (candidate / row["cwd"]).resolve()
        try:
            cwd.relative_to(candidate)
        except ValueError as exc:
            raise MergeQueueError("full-suite validation cwd escaped candidate") from exc
        if controller is not None and repository is not None:
            candidate_exists = task_runtime.run(
                ["git", "-C", str(repository), "cat-file", "-e",
                 f'{plan["candidate"]["candidate_sha"]}^{{commit}}'],
                repository, check=False).returncode == 0
            if candidate_exists:
                command_config = task_runtime.load_config(controller)
                command_runtime = task_runtime.runtime_generation(
                    repository, plan["candidate"]["candidate_sha"])
                evidence_identity = task_runtime._command_input_closure(
                    repository, plan["candidate"]["candidate_sha"], row,
                    command_config, command_runtime)
            else:
                # Direct unit fixtures predating Git-backed candidates retain a
                # finite compatibility identity. Public queue plans prove the
                # candidate commit before reaching this branch.
                evidence_identity = validation_evidence_identity(
                    row, candidate, cwd, identity, plan, repository)
        else:
            evidence_identity = {"input_closure_sha256": None}
        started_at = risk_runtime.utc_now()

        def execute_terminal() -> dict[str, Any]:
            with validation_dependencies(candidate, cwd, dependency_source):
                executed_result = (task_runtime.run_validation(
                    row, cwd, cancel_event=cancel_event) if cancel_event is not None
                    else task_runtime.run_validation(row, cwd))
            result_integrity = executed_result.get("result_integrity") or {}
            terminal_contradiction = bool(result_integrity.get("contradiction"))
            retry = None
            if not terminal_contradiction and (executed_result["timed_out"]
                                                or executed_result["exit_code"]):
                retry = bounded_file_retry(row, cwd, executed_result, candidate,
                                           dependency_source)
                if retry is not None and retry["absorbed"]:
                    executed_result = {**executed_result, "exit_code": 0,
                                       "timed_out": False}
            return {**executed_result,
                    "canonical_retry_evidence": retry}

        if controller is not None and repository is not None:
            try:
                terminal = lifecycle_runtime.consume_or_execute_command_result(
                    controller / CANONICAL_VALIDATION_ROOT, repository,
                    evidence_identity, execute_terminal, phase="full_suite",
                    task_id=str(plan.get("task_id") or ""))
            except lifecycle_runtime.LifecycleContractError as exc:
                raise MergeQueueError(str(exc)) from exc
            evidence = terminal["receipt"]["result"]
        else:
            evidence = execute_terminal()
            terminal = {"decision": "executed", "reference": None}
        retry_evidence = evidence.get("canonical_retry_evidence")
        result_integrity = evidence.get("result_integrity") or {}
        terminal_contradiction = bool(result_integrity.get("contradiction"))
        completed_at = risk_runtime.utc_now()
        receipt = {
            "schema_version": risk_runtime.FULL_SUITE_RECEIPT_V3_SCHEMA,
            "producer": {"schema_version": risk_runtime.FULL_SUITE_PRODUCER_SCHEMA,
                         "tool_id": risk_runtime.FULL_SUITE_TOOL_ID},
            "candidate": {"candidate_sha": plan["candidate"]["candidate_sha"],
                          "candidate_tree": plan["candidate"]["candidate_tree"]},
            "policy_identity": plan["policy_identity"],
            "claim": claim,
            "validation_identity": identity,
            "commands": commands,
            "command_index": index,
            "command": row,
            "started_at": started_at, "completed_at": completed_at,
            "timing": evidence["timing"], "resource": evidence["resource"],
            "identity": evidence["identity"],
            "result": {"exit_code": evidence["exit_code"], "timed_out": evidence["timed_out"],
                       "process_exit_code": evidence.get("process_exit_code"),
                       "result_integrity": {
                           "contradiction": terminal_contradiction,
                           "eligible_pass": bool(result_integrity.get("eligible_pass")),
                           "integrity_sha256": result_integrity.get("integrity_sha256")},
                       "stdout": {"sha256": evidence["stdout_sha256"],
                                  "tail": evidence["stdout_tail"],
                                  "truncated_bytes": evidence["stdout_truncated_bytes"]},
                       "stderr": {"sha256": evidence["stderr_sha256"],
                                  "tail": evidence["stderr_tail"],
                                  "truncated_bytes": evidence["stderr_truncated_bytes"]},
                       **({"retries": retry_evidence} if retry_evidence is not None else {})},
        }
        receipt = fit_full_suite_receipt(receipt, plan["evidence_limits"]["max_receipt_bytes"])
        write_canonical_exclusive(receipt_path, receipt,
                                  plan["evidence_limits"]["max_receipt_bytes"])
        reference = evidence_reference(receipt_path)
        references.append(reference)
        reuse_rows.append(lifecycle_runtime.evidence_decision(
            row["id"], terminal["decision"], closure=evidence_identity,
            source=(terminal["reference"] or reference),
            reason=("canonical command result index" if terminal["reference"]
                    else "unindexed compatibility execution")))
        if evidence["timed_out"] or evidence["exit_code"]:
            detail = evidence["stderr_tail"] or evidence["stdout_tail"]
            raise MergeValidationError(
                f"full-suite validation failed ({row['id']}): {detail}",
                [evidence], reference)
    return references, reuse_rows


def assert_frozen_candidate(controller: Path, config: dict[str, Any], checkout: Path, candidate_sha: str) -> None:
    if task_runtime.load_config(controller) != config:
        raise MergeQueueError("task workspace policy changed while candidate validation was active")
    if task_runtime.git(checkout, "rev-parse", "HEAD", check=False) != candidate_sha:
        raise MergeQueueError("candidate HEAD changed while validation/review was active")
    if task_runtime.git(checkout, "status", "--porcelain=v1", "--untracked-files=all", check=False):
        raise MergeQueueError("candidate checkout became dirty while validation/review was active")


def risk_policy_path(controller: Path) -> Path:
    return controller / ".juno_task/config/risk-policy.json"


def risk_request(repository: Path, candidate_sha: str, target_ref: str,
                 expected_target_sha: str) -> dict[str, str]:
    return {"repository": str(repository.resolve()), "candidate_sha": candidate_sha,
            "target_ref": target_ref, "expected_target_sha": expected_target_sha}


def risk_flags(record: dict[str, Any]) -> Any:
    # Risk is derived from Git. An absent optional flag list is never treated as
    # an asserted low tier; it merely supplies no additional escalation flags.
    return record.get("risk_flags", [])


def evidence_path(controller: Path, task_id: str, candidate_sha: str,
                  attempt_number: Optional[int] = None) -> Path:
    suffix = f".attempt-{attempt_number}" if attempt_number is not None else ""
    return (controller / ".juno_task/runtime/merge-queue/evidence" / task_id
            / f"{candidate_sha}{suffix}.json")


def evidence_reference(path: Path) -> dict[str, str]:
    return {"receipt_path": str(path.resolve()),
            "receipt_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def semantic_snapshot(controller: Path, record: dict[str, Any], plan: dict[str, Any],
                      authority: dict[str, Any], *, commands: int = 0,
                      wall_ms: int = 0, write_collision: bool = False) -> dict[str, Any]:
    """Compile the bounded phase identities that may authorize semantic reuse."""
    closure = record.get("review_ready_closure")
    known = (isinstance(closure, dict)
             and closure.get("schema_version") == "juno_task_review_ready_closure.v1"
             and isinstance(closure.get("closure_sha256"), str))
    inputs = {"candidate_product": plan["candidate"]["product_digest"]}
    if known:
        for key in ("closure_sha256", "changed_paths_sha256", "allowed_paths_sha256",
                    "creation_receipt_sha256", "generated_output_admission_sha256"):
            value = closure.get(key)
            if isinstance(value, str) and risk_runtime.DIGEST_RE.fullmatch(value):
                inputs[key] = value
    try:
        prompt_identity = hashlib.sha256(managed_review_prompt(controller).read_bytes()).hexdigest()
        prompt_unknown = False
    except MergeQueueError:
        # Missing diagnostics may only disable reuse; ordinary execution still
        # reaches the canonical reviewer resolver when a review is required.
        prompt_identity = digest({"review_prompt": "unavailable"})
        prompt_unknown = True
    authority_projection = {
        "target": authority.get("target"), "fifo": authority.get("fifo"),
        "task": authority.get("task"), "owner_ready": (authority.get("integration_owner") or {}).get("ready"),
    }
    runtime_identity = (closure.get("runtime_sha256") if known else None)
    if not isinstance(runtime_identity, str) or not risk_runtime.DIGEST_RE.fullmatch(runtime_identity):
        runtime_identity = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return {"schema_version": risk_runtime.SEMANTIC_SNAPSHOT_SCHEMA,
            "input_identity": dict(sorted(inputs.items())),
            "policy_identity": plan["policy_identity"],
            "runtime_identity": runtime_identity,
            "authority_identity": digest(authority_projection),
            "review_prompt_identity": prompt_identity,
            "closure_unknown": not known or prompt_unknown, "write_collision": write_collision,
            "executed": {"commands": max(0, int(commands)),
                         "wall_ms": max(0, int(wall_ms))}}


def semantic_sidecar_path(receipt_path: Path) -> Path:
    return receipt_path.with_name(receipt_path.name + ".semantic.json")


def write_semantic_sidecar(receipt_path: Path, snapshot: dict[str, Any]) -> dict[str, Any]:
    reference = evidence_reference(receipt_path)
    lineage = {**reference, "candidate_sha": json.loads(receipt_path.read_text())["candidate"]["candidate_sha"],
               "snapshot_sha256": risk_runtime.digest(snapshot)}
    body = {"schema_version": "juno_merge_semantic_lineage.v1",
            "snapshot": snapshot, "source_lineage": lineage,
            "risk_evidence": reference}
    path = semantic_sidecar_path(receipt_path); data = risk_runtime.canonical(body)
    try:
        with path.open("xb") as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
    except FileExistsError:
        if path.read_bytes() != data:
            raise MergeQueueError("semantic reuse decision: write_collision")
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(data).hexdigest()}


def select_predecessor_semantic_evidence(controller: Path, task_id: str,
                                         plan: dict[str, Any]) -> tuple[Any, Any]:
    root = controller / ".juno_task/runtime/merge-queue/evidence" / task_id
    if not root.is_dir():
        return None, None
    eligible: list[tuple[str, dict[str, Any]]] = []
    malformed_seen = False
    for path in sorted(root.glob("*.semantic.json")):
        try:
            raw = path.read_bytes(); sidecar = json.loads(raw)
            if (set(sidecar) != {"schema_version", "snapshot", "source_lineage", "risk_evidence"}
                    or sidecar.get("schema_version") != "juno_merge_semantic_lineage.v1"):
                malformed_seen = True
                continue
            receipt = json.loads(Path(sidecar["risk_evidence"]["receipt_path"]).read_text())
            prior = receipt.get("candidate", {})
            same_predecessor = (prior.get("candidate_sha") == plan["candidate"]["candidate_sha"]
                                or prior.get("source_feature_tip")
                                == plan["candidate"]["source_feature_tip"])
            if same_predecessor:
                eligible.append((path.name, sidecar))
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            malformed_seen = True
            continue
    if malformed_seen:
        return {"snapshot": {"malformed": True}, "source_lineage": {}}, None
    if not eligible:
        return None, None
    sidecar = eligible[-1][1]
    previous = {"snapshot": sidecar["snapshot"],
                "source_lineage": sidecar["source_lineage"]}
    return previous, sidecar["risk_evidence"]


def verify_risk_evidence(policy: dict[str, Any], request: dict[str, str], flags: Any,
                         reference: Any) -> dict[str, Any]:
    try:
        return risk_runtime.verify_candidate_evidence(policy, request, flags, reference)
    except risk_runtime.RiskPolicyError as exc:
        raise MergeQueueError(f"candidate risk evidence refused: {exc}") from exc


def requeue_stale_candidate(controller: Path, config: dict[str, Any], repository: Path,
                            record: dict[str, Any], observed_target_sha: str) -> dict[str, Any]:
    attempt = record.get("queue_attempt")
    if record.get("state") != "REQUEUING_STALE":
        if not isinstance(attempt, dict):
            raise MergeQueueError("stale awaiting task has no candidate attempt")
        if (record.get("task_id") is None
                or Path(record.get("repository", "")).resolve() != repository
                or record.get("target_ref") != config["target_ref"]):
            raise MergeQueueError("stale task repository/target identity drifted")
        checkout_value = attempt.get("candidate_checkout")
        owner = read_candidate_owner(controller, Path(checkout_value)) if checkout_value else None
        source_failure = None
        resolved_outcome = record.get("last_queue_outcome")
        resolved_candidate = (record.get("state") == "CONFLICT_RESOLVED"
                              and resolved_outcome in {"FAILED_TEST", "STALE_TARGET"}
                              and attempt.get("outcome") == resolved_outcome)
        legacy_stale_refusal = (resolved_candidate
                                and resolved_outcome == "STALE_TARGET"
                                and "dependency_lock_refusal" not in attempt)
        structured_stale_refusal = (resolved_candidate
                                    and resolved_outcome == "STALE_TARGET"
                                    and isinstance(attempt.get("dependency_lock_refusal"), dict))
        if (resolved_candidate and resolved_outcome == "STALE_TARGET"
                and not legacy_stale_refusal and not structured_stale_refusal):
            raise MergeQueueError("stale dependency-lock refusal identity mismatched")
        resolved_source = (resolved_candidate
                           and (resolved_outcome == "FAILED_TEST"
                                or legacy_stale_refusal or structured_stale_refusal))
        if resolved_source:
            if not checkout_value or not attempt.get("candidate_token"):
                raise MergeQueueError("stale resolved candidate ownership is incomplete")
            checkout = task_runtime.exact_root(Path(checkout_value), "stale resolved candidate")
            expected_owner = {
                "task_id": record["task_id"], "token": attempt["candidate_token"],
                "repository_identity": repository_identity(repository),
                "target_ref": config["target_ref"],
                "target_sha": attempt.get("expected_target_sha"),
                "feature_sha": record.get("tip_sha"),
                "candidate_checkout": str(checkout.resolve()),
            }
            if (any(owner.get(key) != value for key, value in expected_owner.items())
                    or task_runtime.git(checkout, "rev-parse", "HEAD", check=False)
                        != attempt.get("candidate_sha")
                    or task_runtime.git(checkout, "status", "--porcelain=v1",
                                        "--untracked-files=all", check=False)):
                raise MergeQueueError("stale resolved candidate ownership mismatched")
            verify_candidate_owner(controller, repository, checkout, attempt["candidate_token"])
            if legacy_stale_refusal:
                current_target_sha = task_runtime.ref_sha(repository, config["target_ref"])
                expected_target_sha = attempt.get("expected_target_sha")
                expected_tree = task_runtime.git(checkout, "rev-parse", "HEAD^{tree}", check=False)
                parents = task_runtime.git(
                    checkout, "show", "-s", "--format=%P", "HEAD", check=False).split()
                if (observed_target_sha != current_target_sha
                        or current_target_sha == expected_target_sha
                        or attempt.get("schema_version") != "juno_merge_queue_attempt.v1"
                        or attempt.get("task_id") != record["task_id"]
                        or attempt.get("target_ref") != config["target_ref"]
                        or attempt.get("feature_sha") != record.get("tip_sha")
                        or not isinstance(attempt.get("candidate_checkout"), str)
                        or not isinstance(attempt.get("candidate_token"), str)
                        or not task_runtime.SHA_RE.fullmatch(str(expected_target_sha))
                        or not task_runtime.SHA_RE.fullmatch(str(attempt.get("candidate_sha")))
                        or attempt.get("candidate_tree") != expected_tree
                        or parents != [expected_target_sha, record.get("tip_sha")]):
                    raise MergeQueueError("legacy stale resolved candidate identity mismatched")
            source_failure = {
                "schema_version": "juno_merge_queue_prior_failure.v1",
                "outcome": resolved_outcome, "candidate_sha": attempt["candidate_sha"],
                "expected_target_sha": attempt.get("expected_target_sha"),
                "validation": attempt.get("validation", []),
            }
            if legacy_stale_refusal:
                # RC.0.5 persisted no structured lock evidence. Preserve that
                # exact historical attempt; never synthesize a lock identity.
                source_failure["legacy_refusal_schema"] = \
                    "juno_merge_queue_legacy_unstructured_lock_refusal.v1"
                source_failure["legacy_queue_attempt"] = attempt
            elif resolved_outcome == "STALE_TARGET":
                refusal = attempt["dependency_lock_refusal"]
                lock_path = refusal.get("lock_path")
                if (not isinstance(lock_path, str) or Path(lock_path).is_absolute()
                        or ".." in Path(lock_path).parts):
                    raise MergeQueueError("stale dependency-lock refusal identity mismatched")
                candidate_blob = (task_runtime.git(
                    checkout, "rev-parse", f"HEAD:{lock_path}", check=False)
                    if isinstance(lock_path, str) else "")
                source_blob = (task_runtime.git(
                    repository, "rev-parse", f"{record.get('tip_sha')}:{lock_path}", check=False)
                    if isinstance(lock_path, str) else "")
                expected_refusal = {
                    "schema_version": "juno_merge_queue_dependency_lock_refusal.v1",
                    "lock_path": lock_path,
                    "candidate_head": attempt.get("candidate_sha"),
                    "candidate_blob": candidate_blob,
                    "candidate_sha256": (file_digest(checkout / lock_path)
                                         if isinstance(lock_path, str) else None),
                    "source_head": record.get("tip_sha"),
                    "source_blob": source_blob,
                    "source_sha256": git_blob_digest(repository, source_blob),
                }
                if (refusal != expected_refusal
                        or len(canonical(refusal).encode()) > 4096
                        or refusal.get("candidate_sha256") == refusal.get("source_sha256")
                        or not isinstance(lock_path, str)
                        or Path(lock_path).is_absolute() or ".." in Path(lock_path).parts):
                    raise MergeQueueError("stale dependency-lock refusal identity mismatched")
                source_failure["dependency_lock_refusal"] = refusal
        stale = {"schema_version": "juno_merge_queue_stale_requeue.v2",
                 "task_id": record["task_id"], "old_candidate_sha": attempt["candidate_sha"],
                 "old_candidate_checkout": checkout_value,
                 "old_candidate_token": attempt.get("candidate_token"),
                 "old_candidate_owner": owner, "observed_target_sha": observed_target_sha,
                 "repository_identity": repository_identity(repository),
                 "target_ref": config["target_ref"], "source_state": record.get("state"),
                 "source_failure_evidence": source_failure, "bound_conflict": None}
        with task_runtime.state_lock(controller):
            state = task_runtime.read_state(controller)
            if state["tasks"].get(record["task_id"]) != record:
                raise MergeQueueError("task changed before stale-candidate admission")
            if resolved_source:
                conflict = target_entry(
                    state, repository, config["target_ref"])["conflicts"].get(record["task_id"])
                if (not isinstance(conflict, dict)
                        or conflict.get("resolution_state") != "RESOLVED"
                        or conflict.get("repository_identity") != repository_identity(repository)
                        or conflict.get("target_ref") != config["target_ref"]
                        or conflict.get("task_id") != record["task_id"]
                        or conflict.get("feature_sha") != record.get("tip_sha")
                        or conflict.get("resolved_candidate_sha") != attempt.get("candidate_sha")
                        or conflict.get("expected_target_sha") != attempt.get("expected_target_sha")
                        or (legacy_stale_refusal
                            and (conflict.get("candidate_checkout") != checkout_value
                                 or conflict.get("candidate_token") != attempt.get("candidate_token")
                                 or conflict.get("candidate_head") != attempt.get("expected_target_sha")
                                 or conflict.get("merge_head") != record.get("tip_sha")
                                 or conflict.get("resolved_candidate_tree")
                                    != attempt.get("candidate_tree")))):
                    raise MergeQueueError("stale resolved conflict ownership mismatched")
                stale["bound_conflict"] = conflict
            reopening = {**record, "state": "REQUEUING_STALE", "stale_requeue": stale}
            state["tasks"][record["task_id"]] = reopening
            task_runtime.write_state(controller, state)
        record = reopening
    stale = record.get("stale_requeue")
    if (not isinstance(stale, dict)
            or stale.get("schema_version") not in {"juno_merge_queue_stale_requeue.v1",
                                                   "juno_merge_queue_stale_requeue.v2"}):
        raise MergeQueueError("REQUEUING_STALE recovery identity is invalid")
    if stale.get("schema_version") == "juno_merge_queue_stale_requeue.v2":
        if (stale.get("task_id") != record.get("task_id")
                or stale.get("repository_identity") != repository_identity(repository)
                or stale.get("target_ref") != config["target_ref"]):
            raise MergeQueueError("REQUEUING_STALE repository/target identity drifted")
        with task_runtime.state_lock(controller):
            state = task_runtime.read_state(controller)
            if state["tasks"].get(record["task_id"]) != record:
                raise MergeQueueError("task changed during stale-candidate recovery")
            if (stale.get("source_state") == "CONFLICT_RESOLVED"
                    and target_entry(state, repository, config["target_ref"])["conflicts"].get(
                        record["task_id"]) != stale.get("bound_conflict")):
                raise MergeQueueError("stale resolved conflict ownership drifted")
    checkout_value, token = stale.get("old_candidate_checkout"), stale.get("old_candidate_token")
    if checkout_value:
        checkout = Path(checkout_value)
        if checkout.exists():
            if read_candidate_owner(controller, checkout) != stale.get("old_candidate_owner"):
                raise MergeQueueError("stale candidate ownership drifted")
            rollback_unadmitted_candidate(controller, repository, checkout, token)
        else:
            marker = owner_marker(controller, checkout)
            if any(Path(row.get("worktree", "")).resolve() == checkout.resolve()
                   for row in registered_worktrees(repository)):
                raise MergeQueueError("absent stale checkout remains registered")
            if marker.exists():
                owner = read_candidate_owner(controller, checkout)
                if (owner != stale.get("old_candidate_owner") or owner.get("token") != token
                        or owner.get("task_id") != stale.get("task_id")
                        or owner.get("repository_identity") != repository_identity(repository)):
                    raise MergeQueueError("stale orphan marker ownership mismatched")
                parents = task_runtime.git(repository, "show", "-s", "--format=%P",
                                           stale["old_candidate_sha"], check=False).split()
                if parents != [owner.get("target_sha"), owner.get("feature_sha")]:
                    raise MergeQueueError("stale orphan marker candidate binding mismatched")
                marker.unlink()
    queued = {key: value for key, value in record.items()
              if key not in {"queue_attempt", "last_queue_outcome", "stale_requeue"}}
    queued.update({"state": "QUEUED", "last_queue_outcome": "RISK_TARGET_MOVED",
                   "observed_target_sha": stale.get("observed_target_sha", observed_target_sha)})
    failure_evidence = stale.get("source_failure_evidence")
    if isinstance(failure_evidence, dict):
        queued["prior_queue_failure"] = failure_evidence
    with task_runtime.state_lock(controller):
        state = task_runtime.read_state(controller)
        if state["tasks"].get(record["task_id"]) != record:
            raise MergeQueueError("task changed during stale-candidate cleanup")
        entry = target_entry(state, repository, config["target_ref"])
        if (stale.get("source_state") == "CONFLICT_RESOLVED"
                and entry["conflicts"].get(record["task_id"]) != stale.get("bound_conflict")):
            raise MergeQueueError("stale resolved conflict ownership drifted")
        queued["enqueue_sequence"] = task_runtime.assign_enqueue_sequence(state)
        state["tasks"][record["task_id"]] = queued
        entry["conflicts"].pop(record["task_id"], None)
        task_runtime.write_state(controller, state)
    return {**queued, "outcome": "RISK_TARGET_MOVED"}


def review_candidate(controller: Path, record: dict[str, Any], candidate_sha: str,
                     repository: Path, target_sha: str, validation_root: Path,
                     attempt: dict[str, Any]) -> dict[str, Any]:
    """Plan and strictly verify risk evidence for every frozen candidate.

    Low and optional-review normal candidates get a canonical zero-review
    receipt immediately. Higher-risk and release candidates are durably paused;
    absence or a hand-written PASS object can never authorize CAS.
    """
    try:
        policy = risk_runtime.load_policy(risk_policy_path(controller))
        request = risk_request(repository, candidate_sha, attempt["target_ref"], target_sha)
        flags = risk_flags(record)
        plan = risk_runtime.classify(policy, request, flags)
    except risk_runtime.RiskPolicyError as exc:
        raise MergeQueueError(f"candidate risk plan refused: {exc}") from exc
    current = attempt.get("risk")
    reference = current.get("evidence") if isinstance(current, dict) else None
    reference_from_state = reference is not None
    canonical_path = evidence_path(controller, record["task_id"], candidate_sha)
    if reference is None and canonical_path.is_file():
        reference = evidence_reference(canonical_path)
    authority = compile_live_authority_snapshot(
        controller, task_runtime.load_config(controller), repository, record["task_id"])
    snapshot = semantic_snapshot(controller, record, plan, authority)
    semantic_previous, semantic_reference = select_predecessor_semantic_evidence(
        controller, record["task_id"], plan)
    semantic_decision = risk_runtime.semantic_reuse_decision(semantic_previous, snapshot)
    risk = {"schema_version": RISK_STATE_SCHEMA, "candidate_sha": candidate_sha,
            "policy_identity": plan["policy_identity"], "plan": plan,
            "evidence": reference, "semantic_reuse_decision": semantic_decision,
            "semantic_previous_evidence": semantic_reference}
    if semantic_decision["stop"]:
        raise MergeQueueError("semantic reuse authority stop: " + semantic_decision["code"])
    if reference is not None:
        try:
            verified = verify_risk_evidence(policy, request, flags, reference)
            if verified["eligible"] and plan["full_suite_required"]:
                evidence = json.loads(Path(reference["receipt_path"]).read_text())
                loaded = task_runtime.load_config(controller)
                identity = full_validation_identity(
                    controller, loaded, record, validation_root, candidate_sha)
                admission = evidence["validation"]["full_suite_admission"]
                if (isinstance(admission, dict)
                        and admission.get("schema_version")
                            == risk_runtime.FULL_SUITE_ADMISSION_SCHEMA):
                    verify_queue_full_suite_admission_legacy(
                        controller, record["task_id"], plan, identity,
                        full_suite_command(loaded), admission)
                else:
                    suite_commands, suite_routing = full_suite_selection(
                        loaded, plan["candidate"]["changed_paths"])
                    verify_queue_full_suite_admission(
                        controller, record["task_id"], plan, identity,
                        suite_commands, suite_routing, admission)
        except MergeQueueError:
            if reference_from_state or not (plan["min_reviews"] or plan["full_suite_required"]):
                raise
            # External/legacy PASS projections are not cache authority. The
            # local full-suite + semantic workflow replaces them from scratch.
            verified = {"eligible": False}
            reference = None
            risk = {**risk, "evidence": None}
        if verified["eligible"]:
            return {**risk, "status": "ELIGIBLE", "evidence": reference}
    if semantic_decision["code"] != "hit" and (plan["min_reviews"] or plan["full_suite_required"]):
        return {**risk, "status": "AWAITING_RISK"}
    try:
        receipt = risk_runtime.finalize(
            plan, request, affected_tests_passed=True, full_suite_admission=None,
            reviews=[], metrics={"model_calls": 0,
                                 "affected_test_runs": 0 if semantic_reference else 1,
                                 "full_suite_runs": 0}, policy=policy,
            previous=semantic_reference,
        )
        path = canonical_path
        risk_runtime.atomic_receipt(path, receipt, policy)
        reference = evidence_reference(path)
        write_semantic_sidecar(path, snapshot)
        verified = risk_runtime.verify_candidate_evidence(policy, request, flags, reference)
    except risk_runtime.RiskPolicyError as exc:
        raise MergeQueueError(f"candidate zero-review evidence refused: {exc}") from exc
    if not verified["eligible"]:
        raise MergeQueueError("candidate zero-review evidence is not eligible")
    assert_frozen_candidate(controller, task_runtime.load_config(controller), validation_root, candidate_sha)
    return {**risk, "status": "ELIGIBLE", "evidence": reference}


def resume_awaiting(controller: Path, config: dict[str, Any], repository: Path,
                    record: dict[str, Any]) -> dict[str, Any]:
    attempt = record.get("queue_attempt")
    if not isinstance(attempt, dict) or attempt.get("candidate_sha") is None:
        raise MergeQueueError("awaiting-risk task has no frozen candidate identity")
    candidate_sha, expected = attempt["candidate_sha"], attempt["expected_target_sha"]
    current = task_runtime.ref_sha(repository, config["target_ref"])
    if current != expected:
        return requeue_stale_candidate(controller, config, repository, record, current)
    checkout_value = attempt.get("candidate_checkout")
    root = (task_runtime.exact_root(Path(checkout_value), "awaiting risk candidate")
            if checkout_value else validate_record(config, repository, record))
    assert_frozen_candidate(controller, config, root, candidate_sha)
    decision = review_candidate(controller, record, candidate_sha, repository, expected, root, attempt)
    stored_risk = attempt.get("risk") if isinstance(attempt.get("risk"), dict) else {}
    preserved_progress = stored_risk.get("review_progress")
    merged_risk = {**stored_risk, **decision}
    if isinstance(preserved_progress, dict) and not isinstance(
            decision.get("review_progress"), dict):
        # An explicit resume must never drop the stored full-suite admission,
        # attempt counters, or completed reviewer steps: the immutable claim
        # files remain on disk and losing their reference wedges the candidate.
        merged_risk["review_progress"] = preserved_progress
    attempt = {**attempt, "risk": merged_risk, "review": merged_risk}
    if decision["status"] != "ELIGIBLE":
        attempt["outcome"] = decision["status"]
        persist_attempt(controller, attempt, state_name=decision["status"])
        return attempt
    persist_attempt(controller, attempt, state_name="MERGING")
    authority = compile_live_authority_snapshot(
        controller, config, repository, record["task_id"])
    require_live_authority(controller, config, repository, record["task_id"], authority,
                           boundary="before_target_cas")
    assert_target_unchecked_out(repository, config["target_ref"])
    attempt["integration_owner_authority"] = cas_target(
        repository, config["target_ref"], candidate_sha, expected
    )
    attempt = complete_post_integration(
        controller, repository, attempt, attempt["integration_owner_authority"])
    attempt = {**attempt, "outcome": "MERGED",
               "readback_sha": task_runtime.ref_sha(repository, config["target_ref"])}
    persist_attempt(controller, attempt, state_name="MERGED", remove_conflict=True)
    checkout = Path(checkout_value) if checkout_value else None
    return {**attempt, "cleanup": cleanup_candidate(
        controller, repository, checkout, config["target_ref"], candidate_sha,
        attempt.get("candidate_token"))}


def cas_target(repository: Path, target_ref: str, candidate_sha: str,
               expected_sha: str) -> dict[str, Any]:
    owner, owner_before = registered_owner_preflight(repository, expected_sha, candidate_sha)
    result = task_runtime.run(["git", "-C", str(repository), "update-ref", target_ref,
                               candidate_sha, expected_sha], repository, check=False)
    if result.returncode:
        raise MergeQueueError("target moved before compare-and-swap; no ref was changed")
    actual = task_runtime.git(repository, "rev-parse", f"{target_ref}^{{commit}}")
    if actual != candidate_sha:
        raise MergeQueueError("target compare-and-swap readback mismatch")
    expected_tree = task_runtime.git(repository, "rev-parse", f"{candidate_sha}^{{tree}}")
    actual_tree = task_runtime.git(repository, "rev-parse", f"{target_ref}^{{tree}}")
    if actual_tree != expected_tree:
        raise MergeQueueError("target tree readback mismatch")
    return advance_registered_owner(
        repository, expected_sha, candidate_sha, owner, owner_before)


def attempt_runtime_pin(repository: Path, target_sha: str) -> dict[str, Any]:
    """Freeze the executing lifecycle generation before any attempt mutation."""
    generation = task_runtime.runtime_generation(repository, target_sha)
    if not generation["current"]:
        raise MergeQueueError(
            "managed lifecycle runtime is incompatible with the current target; "
            "complete the reported runtime maintenance before starting new merge work")
    body = {
        "schema_version": "juno_merge_runtime_pin.v1",
        "target_sha": target_sha,
        "running_sha256": generation["running_sha256"],
        "target_sha256": generation["target_sha256"],
    }
    return {**body, "pin_sha256": digest(body)}


def verify_attempt_runtime_pin(repository: Path, attempt: dict[str, Any]) -> dict[str, Any]:
    pin = attempt.get("runtime_pin")
    if pin is None:
        # Finite legacy readback: an attempt persisted by the previous engine is
        # pinned to the exact pre-CAS target whose runtime is still executing.
        pin = attempt_runtime_pin(repository, attempt["expected_target_sha"])
        pin = {**pin, "legacy_import": True}
    body = {key: value for key, value in pin.items()
            if key not in {"pin_sha256", "legacy_import"}}
    if (not isinstance(pin, dict)
            or body.get("schema_version") != "juno_merge_runtime_pin.v1"
            or body.get("target_sha") != attempt.get("expected_target_sha")
            or body.get("running_sha256") != hashlib.sha256(
                Path(task_runtime.__file__).resolve().read_bytes()).hexdigest()
            or pin.get("pin_sha256") != digest(body)):
        raise MergeQueueError("merge attempt runtime pin is missing, stale, or tampered")
    return pin


def runtime_maintenance_projection(repository: Path, attempt: dict[str, Any]) -> dict[str, Any]:
    previous, candidate = attempt["expected_target_sha"], attempt["candidate_sha"]
    generation = task_runtime.runtime_generation(repository, candidate)
    previous_assets = integration_runtime.managed_script_assets(repository, previous)
    candidate_assets = integration_runtime.managed_script_assets(repository, candidate)
    changed_scripts = []
    for relative in sorted(set(previous_assets) | set(candidate_assets)):
        old = (integration_runtime.managed_script_source_bytes(
            repository, previous, previous_assets, relative)
               if relative in previous_assets else None)
        new = (integration_runtime.managed_script_source_bytes(
            repository, candidate, candidate_assets, relative)
               if relative in candidate_assets else None)
        if old != new:
            changed_scripts.append(relative)
    if generation["current"] and not changed_scripts:
        return {"status": "complete", "outcome": "not_required",
                "running_sha256": generation["running_sha256"]}
    command = ("yy integration runtime-refresh --previous-sha "
               f"{previous} --target-sha {candidate}")
    return {"status": "maintenance_needed", "outcome": "separate_authority_required",
            "running_sha256": generation["running_sha256"],
            "target_sha256": generation["target_sha256"],
            "changed_scripts": changed_scripts[:64],
            "safe_next_action": command}


def require_runtime_before_new_work(controller: Path, repository: Path,
                                    config: dict[str, Any]) -> None:
    target_sha = task_runtime.ref_sha(repository, config["target_ref"])
    generation = task_runtime.runtime_generation(repository, target_sha)
    with task_runtime.state_lock(controller):
        state = task_runtime.read_state(controller)
        attempts = [row.get("queue_attempt") for row in state.get("tasks", {}).values()
                    if isinstance(row, dict) and row.get("state") == "MERGED"]
    maintenance = next((attempt.get("runtime_maintenance") for attempt in attempts
                        if isinstance(attempt, dict) and attempt.get("candidate_sha") == target_sha
                        and isinstance(attempt.get("runtime_maintenance"), dict)), None)
    action = maintenance.get("safe_next_action") if isinstance(maintenance, dict) \
        and maintenance.get("status") == "maintenance_needed" else None
    evidence = (f"running_sha256={generation['running_sha256']} "
                f"target_sha256={generation['target_sha256']}")
    if isinstance(action, str) and action:
        raise MergeQueueError(
            f"runtime maintenance needed before incompatible new work; {evidence}; "
            f"supported action: {action}")
    if not generation["current"]:
        task_runtime.require_current_runtime(repository, target_sha, controller)


def post_integration_phases(attempt: dict[str, Any]) -> dict[str, Any]:
    existing = attempt.get("post_integration")
    if isinstance(existing, dict) and existing.get("schema_version") == "juno_post_integration.v2":
        return existing
    if isinstance(existing, dict) and existing.get("schema_version") == "juno_post_integration.v1":
        # Read old attempts, but never invoke their delivery-owned refresh phase.
        old_runtime = existing.get("managed_runtime_refresh", {})
        maintenance = ({"status": "complete", "outcome": "legacy_refresh_already_complete"}
                       if isinstance(old_runtime, dict) and old_runtime.get("status") == "complete"
                       else {"status": "pending", "outcome": "legacy_refresh_retired"})
        return {"schema_version": "juno_post_integration.v2",
                "target_advancement": existing.get("target_advancement", {"status": "complete"}),
                "integration_owner": existing.get("integration_owner", {"status": "pending"}),
                "kanban_finalization": existing.get("kanban_finalization", {"status": "pending"}),
                "runtime_maintenance": maintenance,
                "recovery_command": "yy merge next"}
    return {
        "schema_version": "juno_post_integration.v2",
        "target_advancement": {"status": "complete", "sha": attempt["candidate_sha"]},
        "integration_owner": {"status": "pending"},
        "kanban_finalization": {"status": "pending"},
        "runtime_maintenance": {"status": "pending"},
        "recovery_command": "yy merge next",
    }


def read_kanban_task(controller: Path, task_id: str) -> dict[str, Any]:
    wrapper = controller / ".juno_task/scripts/kanban.sh"
    if not wrapper.is_file():
        raise MergeQueueError("canonical Kanban wrapper is missing")
    result = subprocess.run(
        [str(wrapper), "-f", "json", "get", task_id], cwd=controller,
        stdin=subprocess.DEVNULL, text=True, capture_output=True,
    )
    if result.returncode:
        raise MergeQueueError(result.stderr.strip() or "Kanban task readback failed")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise MergeQueueError("Kanban task readback is not valid JSON") from exc
    if isinstance(payload, list) and len(payload) == 1:
        payload = payload[0]
    if not isinstance(payload, dict) or payload.get("id") != task_id:
        raise MergeQueueError("Kanban task readback identity mismatched")
    return payload


def _unmet_dependency_blockers(value: dict[str, Any]) -> list[str]:
    """Project canonical unmet dependency IDs, retaining fail-closed legacy fallback."""
    dependency_info = value.get("_dependency_info")
    if isinstance(dependency_info, dict) and "unmet_blockers" in dependency_info:
        rows = dependency_info.get("unmet_blockers")
    elif "unmet_blockers" in value:
        rows = value.get("unmet_blockers")
    else:
        fields = value.get("fields") if isinstance(value.get("fields"), dict) else {}
        rows = value.get("blocked_by") or fields.get("blocked_by") or []
    if not isinstance(rows, list):
        return ["<malformed>"]
    blockers: list[str] = []
    for row in rows:
        blocker = row.get("id") if isinstance(row, dict) else row
        if not isinstance(blocker, str) or not blocker:
            return ["<malformed>"]
        blockers.append(blocker)
    return sorted(set(blockers))


def _authority_task_projection(task: dict[str, Any]) -> dict[str, Any]:
    fields = task.get("fields") if isinstance(task.get("fields"), dict) else {}
    blockers = _unmet_dependency_blockers(task)
    withdrawal = {
        key: value for key, value in {
            "status": task.get("status"),
            "withdrawn": fields.get("withdrawn"),
            "withdrawn_at": fields.get("withdrawn_at"),
            "superseded_by_task_id": fields.get("superseded_by_task_id"),
            "continuation_task_id": fields.get("continuation_task_id"),
        }.items() if value is not None
    }
    canonical_task = {key: value for key, value in task.items()
                      if not key.startswith("_")}
    return {"revision_sha256": digest(canonical_task),
            "status": task.get("status"),
            "withdrawal_supersession": withdrawal,
            "blockers": blockers}


def _authority_fifo(state: dict[str, Any], target_ref: str,
                    task_id: str) -> dict[str, Any]:
    eligible_states = {"QUEUED", "AWAITING_RISK",
                       "REVIEW_FINDINGS", "CONFLICT_RESOLVED", "REQUEUING_STALE",
                       "MERGING"}
    rows = sorted((row for row in state.get("tasks", {}).values()
                   if isinstance(row, dict) and row.get("target_ref") == target_ref
                   and row.get("state") in eligible_states
                   and isinstance(row.get("enqueue_sequence"), int)),
                  key=lambda row: (row["enqueue_sequence"], row.get("task_id", "")))
    current = next((index for index, row in enumerate(rows)
                    if row.get("task_id") == task_id), None)
    return {"ordered_task_ids": [row.get("task_id") for row in rows],
            "predecessors": ([row.get("task_id") for row in rows[:current]]
                             if current is not None else []),
            "tail": rows[-1].get("task_id") if rows else None,
            "current_index": current}


def compile_live_authority_snapshot(controller: Path, config: dict[str, Any],
                                    repository: Path, task_id: str) -> dict[str, Any]:
    """Reread only live mutation authority; controller HEAD/dirt are excluded."""
    with task_runtime.state_lock(controller):
        state = task_runtime.read_state(controller)
        record = state.get("tasks", {}).get(task_id)
        if not isinstance(record, dict):
            raise MergeQueueError("live authority task record is missing")
        fifo = _authority_fifo(state, config["target_ref"], task_id)
        entry = state.get("queues", {}).get(target_key(repository, config["target_ref"]))
    task = read_kanban_task(controller, task_id)
    target_sha = task_runtime.ref_sha(repository, config["target_ref"])
    owner_raw = task_runtime.git(repository, "config", "--local", "--get",
                                 INTEGRATION_OWNER_CONFIG, check=False)
    if owner_raw:
        try:
            owner = Path(owner_raw).expanduser().resolve()
            observed = integration_owner_readback(owner)
            owner_authority = {"registered": True, "path": str(owner),
                               "observed": observed,
                               "ready": (observed["head"] == target_sha
                                         and observed["role_base"] == target_sha
                                         and observed["role"] == "integration-owner"
                                         and observed["authority"] == INTEGRATION_OWNER_AUTHORITY
                                         and observed["clean"] and observed["detached"]
                                         and observed["full_checkout"]
                                         and all(row["state"] == "exact"
                                                 for row in observed["submodules"]))}
        except (OSError, KeyError, MergeQueueError) as exc:
            owner_authority = {"registered": True, "path": owner_raw,
                               "ready": False, "error": str(exc)[:512]}
    else:
        owner_authority = {"registered": False, "path": None, "ready": True}
    creation = record.get("creation_receipt") if isinstance(
        record.get("creation_receipt"), dict) else {}
    record_blockers = _unmet_dependency_blockers(record)
    body = {
        "schema_version": AUTHORITY_SCHEMA, "task_id": task_id,
        "task": _authority_task_projection(task),
        "record": {"state": record.get("state"), "base_sha": record.get("base_sha"),
                   "tip_sha": record.get("tip_sha"), "branch_ref": record.get("branch_ref"),
                   "enqueue_sequence": record.get("enqueue_sequence"),
                   "blockers": record_blockers,
                   "ownership_handoff_sha256": digest({
                       "fencing": record.get("fencing"),
                       "fencing_history": record.get("fencing_history"),
                       "handoff": record.get("handoff"),
                       "owner": record.get("owner"),
                   })},
        "admission": {"allowed_paths": sorted(creation.get("allowed_paths") or []),
                      "changed_paths": sorted(record.get("changed_paths") or []),
                      "creation_receipt_sha256": digest(creation)},
        "fifo": fifo, "queue_entry_sha256": digest(entry),
        "target": {"ref": config["target_ref"], "expected_sha": target_sha},
        "integration_owner": owner_authority,
    }
    return {**body, "authority_sha256": digest(body)}


def require_live_authority(controller: Path, config: dict[str, Any], repository: Path,
                           task_id: str, expected: dict[str, Any], *,
                           boundary: str) -> dict[str, Any]:
    """Fail closed at dispatch/CAS without invalidating behavioral evidence."""
    current = compile_live_authority_snapshot(
        controller, config, repository, task_id)
    comparisons = {
        "TASK_REVISION_DRIFT": "task", "QUEUE_RECORD_DRIFT": "record",
        "ADMITTED_PATHS_DRIFT": "admission", "FIFO_DRIFT": "fifo",
        "QUEUE_AUTHORITY_DRIFT": "queue_entry_sha256", "TARGET_DRIFT": "target",
        "INTEGRATION_OWNER_DRIFT": "integration_owner",
    }
    reasons = [{"code": code, "boundary": boundary}
               for code, key in comparisons.items()
               if expected.get(key) != current.get(key)]
    if expected.get("task", {}).get("status") != current.get("task", {}).get("status"):
        reasons.append({"code": "STATUS_DRIFT", "boundary": boundary})
    if (expected.get("task", {}).get("withdrawal_supersession")
            != current.get("task", {}).get("withdrawal_supersession")):
        reasons.append({"code": "WITHDRAWAL_SUPERSESSION_DRIFT", "boundary": boundary})
    if (expected.get("task", {}).get("blockers") != current.get("task", {}).get("blockers")
            or expected.get("record", {}).get("blockers")
            != current.get("record", {}).get("blockers")):
        reasons.append({"code": "BLOCKERS_DRIFT", "boundary": boundary})
    if current.get("task", {}).get("status") != "in_progress":
        reasons.append({"code": "TASK_NOT_IN_PROGRESS", "boundary": boundary})
    withdrawal = current.get("task", {}).get("withdrawal_supersession", {})
    if (withdrawal.get("withdrawn") or withdrawal.get("superseded_by_task_id")
            or current.get("record", {}).get("state") == "WITHDRAWN"):
        reasons.append({"code": "TASK_WITHDRAWN_OR_SUPERSEDED", "boundary": boundary})
    if current.get("task", {}).get("blockers") or current.get("record", {}).get("blockers"):
        reasons.append({"code": "BLOCKERS_PRESENT", "boundary": boundary})
    if not current.get("integration_owner", {}).get("ready"):
        reasons.append({"code": "INTEGRATION_OWNER_NOT_READY", "boundary": boundary})
    # Keep bounded deterministic typed attribution and do not write queue state.
    unique = {(row["code"], row["boundary"]): row for row in reasons}
    reasons = [unique[key] for key in sorted(unique)]
    if reasons:
        raise AuthorityDriftError(boundary, reasons[:32])
    return current


def consolidate_review_findings(reviews: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate normalized findings while retaining every reviewer receipt."""
    consolidated: dict[str, dict[str, Any]] = {}
    severity_rank = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    for review in reviews:
        if not isinstance(review, dict) or not isinstance(review.get("findings"), list):
            raise MergeQueueError("verified review findings projection is malformed")
        reference = review.get("review_reference")
        if not isinstance(reference, dict):
            raise MergeQueueError("verified review provenance is missing")
        provenance = {"reviewer": review.get("reviewer"), "sequence": review.get("sequence"),
                      "receipt_path": reference.get("receipt_path"),
                      "receipt_sha256": reference.get("receipt_sha256")}
        for finding in review["findings"]:
            digest_value = finding.get("finding_digest") if isinstance(finding, dict) else None
            if not isinstance(digest_value, str) or not re.fullmatch(r"[0-9a-f]{64}", digest_value):
                raise MergeQueueError("verified finding identity is malformed")
            existing = consolidated.get(digest_value)
            if existing is None:
                consolidated[digest_value] = {**finding, "provenance": [provenance]}
                continue
            if severity_rank[finding["normalized_severity"]] > severity_rank[existing["normalized_severity"]]:
                existing["normalized_severity"] = finding["normalized_severity"]
                existing["blocking"] = finding["blocking"]
            if provenance not in existing["provenance"]:
                existing["provenance"].append(provenance)
    return [consolidated[key] for key in sorted(consolidated)]


def delivery_advisories(reviews: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return bounded nonblocking findings for the existing delivery evidence.

    The immutable review references remain the provenance.  This projection is
    deliberately side-effect free: review advisories never create Ledger tasks
    and an external follow-up write cannot become an integration gate.
    """
    return [finding for finding in consolidate_review_findings(reviews)
            if not finding["blocking"]]


def _kanban_protected_projection(task: dict[str, Any]) -> dict[str, Any]:
    fields = task.get("fields") if isinstance(task.get("fields"), dict) else {}
    return {"status": task.get("status"), "commit_hash": task.get("commit_hash"),
            "lifecycle_state": fields.get("lifecycle_state"),
            "lifecycle_projection": fields.get("lifecycle_projection")}


def prepare_kanban_finalization_intent(controller: Path,
                                       attempt: dict[str, Any]) -> dict[str, Any]:
    """Create/rebase one journaled outbox intent before the external Ledger CAS."""
    task_id, candidate = attempt["task_id"], attempt["candidate_sha"]
    task = read_kanban_task(controller, task_id)
    current_revision = task_runtime.kanban_board_revision(controller, task_id)
    intent = attempt.get("kanban_finalization_intent")
    identity = digest({"operation": "merge-finalization", "task_id": task_id,
                       "candidate_sha": candidate})
    if isinstance(intent, dict):
        if (intent.get("schema_version") != "juno_kanban_finalization_intent.v1"
                or intent.get("idempotency_key") != identity
                or intent.get("candidate_sha") != candidate):
            raise MergeQueueError("Kanban finalization intent is malformed or mismatched")
        if task.get("status") != "done" and (
                _kanban_protected_projection(task) != intent.get("expected_projection")):
            raise MergeQueueError(
                "relevant Kanban task fields changed after finalization intent; "
                f"preserve user edits and recover with: yy merge next")
        if intent.get("expected_revision") == current_revision:
            return attempt
        # Revision drift confined to response or unrelated fields is safe: keep
        # those bytes, bind the fresh whole-task CAS, and journal the rebase.
        intent = {**intent, "expected_revision": current_revision,
                  "revision_rebased_from": intent.get("expected_revision")}
    else:
        intent = {"schema_version": "juno_kanban_finalization_intent.v1",
                  "idempotency_key": identity, "task_id": task_id,
                  "candidate_sha": candidate, "expected_revision": current_revision,
                  "expected_projection": _kanban_protected_projection(task)}
    return {**attempt, "kanban_finalization_intent": intent}


def finalize_kanban_task(controller: Path, attempt: dict[str, Any]) -> dict[str, Any]:
    task_id, candidate = attempt["task_id"], attempt["candidate_sha"]
    task = read_kanban_task(controller, task_id)
    intent = attempt.get("kanban_finalization_intent")
    if not isinstance(intent, dict):
        raise MergeQueueError("Kanban finalization requires a persisted outbox intent")
    expected_revision = intent.get("expected_revision")
    idempotency_key = intent.get("idempotency_key")
    if (not isinstance(expected_revision, str)
            or not re.fullmatch(r"[0-9a-f]{16,128}", expected_revision)
            or not isinstance(idempotency_key, str)
            or not re.fullmatch(r"[0-9a-f]{64}", idempotency_key)):
        raise MergeQueueError("Kanban finalization intent revision or identity is malformed")
    if task.get("status") == "done":
        if task.get("commit_hash") != candidate:
            raise MergeQueueError("Kanban task is already done with a different commit")
        fields = task.get("fields") if isinstance(task.get("fields"), dict) else {}
        if (fields.get("lifecycle_state") == "MERGED"
                and fields.get("lifecycle_projection") == task_runtime.KANBAN_LIFECYCLE_PROJECTION):
            return {"outcome": "already_complete", "commit_hash": candidate,
                    "idempotency_key": idempotency_key}
        raise MergeQueueError("done task lacks exact merge finalization proof")
    response = task.get("agent_response")
    if not isinstance(response, str) or not response.strip():
        response = f"Integrated through the guarded merge queue at {candidate}."
    receipt_path = (controller / ".juno_task/runtime/merge-queue/finalization"
                    / task_id / f"{candidate}.json")
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    if receipt_path.exists():
        raise MergeQueueError("Kanban finalization receipt exists but task is not complete")
    fd, response_name = tempfile.mkstemp(prefix=".kanban-response-", dir=receipt_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(response)
        wrapper = controller / ".juno_task/scripts/kanban.sh"
        result = subprocess.run([
            str(wrapper), "-f", "json", "update", task_id,
            "--status", "done", "--response-file", response_name,
            "--commit", candidate,
            "--field", f"lifecycle_projection={json.dumps(task_runtime.KANBAN_LIFECYCLE_PROJECTION)}",
            "--field", f"lifecycle_state={json.dumps('MERGED')}",
            "--field", f"lifecycle_finalization_key={json.dumps(idempotency_key)}",
            "--expected-revision", expected_revision,
            "--receipt-file", str(receipt_path),
        ], cwd=controller, stdin=subprocess.DEVNULL, text=True, capture_output=True)
    finally:
        Path(response_name).unlink(missing_ok=True)
    if result.returncode:
        raise MergeQueueError(result.stderr.strip() or "Kanban finalization failed")
    readback = read_kanban_task(controller, task_id)
    if readback.get("status") != "done" or readback.get("commit_hash") != candidate:
        raise MergeQueueError("Kanban finalization readback mismatched")
    fields = readback.get("fields") if isinstance(readback.get("fields"), dict) else {}
    if (fields.get("lifecycle_state") != "MERGED"
            or fields.get("lifecycle_projection") != task_runtime.KANBAN_LIFECYCLE_PROJECTION
            or fields.get("lifecycle_finalization_key") != idempotency_key):
        raise MergeQueueError("Kanban finalization lifecycle readback mismatched")
    return {"outcome": "completed", "commit_hash": candidate,
            "idempotency_key": idempotency_key,
            "receipt": evidence_reference(receipt_path),
            "lifecycle_projection": "completed"}


def complete_post_integration(controller: Path, repository: Path,
                              attempt: dict[str, Any],
                              authority: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    pin = verify_attempt_runtime_pin(repository, attempt)
    phases = post_integration_phases(attempt)
    candidate = attempt["candidate_sha"]
    observed = task_runtime.ref_sha(repository, attempt["target_ref"])
    observed_tree = task_runtime.git(repository, "rev-parse", f"{observed}^{{tree}}")
    if observed != candidate or observed_tree != attempt.get("candidate_tree"):
        raise PostIntegrationError("landed target readback does not match the persisted candidate")
    landed = {"schema_version": "juno_landed_delivery.v1", "commit_sha": observed,
              "tree_sha": observed_tree, "runtime_pin_sha256": pin["pin_sha256"],
              "owner_registration": task_runtime.git(
                  repository, "config", "--local", "--get", INTEGRATION_OWNER_CONFIG,
                  check=False) or None}
    # Persist landed product truth before any projection. This intent boundary
    # makes a pending board write evidence to resume, never permission to redo CAS.
    phases = {**phases, "target_advancement": {"status": "complete", "result": landed}}
    attempt = {**attempt, "runtime_pin": pin, "landed_delivery": landed,
               "post_integration": phases,
               "outcome": "INTEGRATED_FINALIZATION_PENDING",
               "recovery_command": "yy merge next"}
    persist_attempt(controller, attempt, state_name="MERGING")
    if phases["integration_owner"].get("status") != "complete":
        if authority is None:
            owner, before = registered_owner_preflight(
                repository, attempt["expected_target_sha"], candidate)
            authority = advance_registered_owner(
                repository, attempt["expected_target_sha"], candidate, owner, before)
        phases = {**phases, "integration_owner": {
            "status": "complete" if authority.get("status") != "partial" else "failed",
            "result": authority,
        }}
        attempt = {**attempt, "post_integration": phases,
                   "integration_owner_authority": authority,
                   "outcome": ("INTEGRATED_FINALIZATION_PENDING"
                               if authority.get("status") != "partial"
                               else "POST_INTEGRATION_OWNER_FAILED")}
        persist_attempt(controller, attempt, state_name="MERGING")
        if authority.get("status") == "partial":
            raise PostIntegrationError(
                "target integrated but integration-owner advancement failed; recover with: yy merge next")
    if phases["kanban_finalization"].get("status") != "complete":
        try:
            attempt = prepare_kanban_finalization_intent(controller, attempt)
            phases = {**phases, "kanban_finalization": {
                "status": "pending", "intent": attempt["kanban_finalization_intent"]}}
            attempt = {**attempt, "post_integration": phases,
                       "outcome": "INTEGRATED_FINALIZATION_PENDING"}
            persist_attempt(controller, attempt, state_name="MERGING")
            kanban_result = finalize_kanban_task(controller, attempt)
        except MergeQueueError as exc:
            phases = {**phases, "kanban_finalization": {
                "status": "failed", "intent": attempt.get("kanban_finalization_intent"),
                "error": str(exc)}}
            attempt = {**attempt, "post_integration": phases,
                       "outcome": "INTEGRATED_FINALIZATION_PENDING"}
            persist_attempt(controller, attempt, state_name="MERGING")
            raise PostIntegrationError(
                f"post-integration Kanban finalization failed: {exc}; recover with: yy merge next") from exc
        phases = {**phases, "kanban_finalization": {
            "status": "complete", "intent": attempt["kanban_finalization_intent"],
            "result": kanban_result}}
        attempt = {**attempt, "post_integration": phases,
                   "kanban_finalization": kanban_result}
        persist_attempt(controller, attempt, state_name="MERGING")
    if phases["runtime_maintenance"].get("status") not in {
            "complete", "maintenance_needed"}:
        maintenance = runtime_maintenance_projection(repository, attempt)
        phases = {**phases, "runtime_maintenance": maintenance}
        attempt = {**attempt, "post_integration": phases,
                   "runtime_maintenance": maintenance,
                   "outcome": ("MERGED_MAINTENANCE_NEEDED"
                               if maintenance["status"] == "maintenance_needed" else "MERGED")}
        persist_attempt(controller, attempt, state_name="MERGING")
    return attempt


def cleanup_candidate(controller: Path, repository: Path, checkout: Optional[Path],
                      target_ref: str, candidate_sha: str, token: Optional[str]) -> dict[str, Any]:
    if checkout is None:
        return {"candidate_checkout": None, "outcome": "not_required"}
    try:
        if not token:
            raise MergeQueueError("candidate ownership token is absent")
        verify_candidate_owner(controller, repository, checkout, token)
    except MergeQueueError as exc:
        return {"candidate_checkout": str(checkout.resolve()), "outcome": "preserved",
                "reason": "ownership_mismatch", "detail": str(exc)}
    checkout = checkout.resolve()
    if task_runtime.git(checkout, "rev-parse", "HEAD", check=False) != candidate_sha:
        return {"candidate_checkout": str(checkout), "outcome": "preserved", "reason": "candidate_head_mismatch"}
    dirty = task_runtime.git(checkout, "status", "--porcelain=v1", "--untracked-files=all", check=False)
    reachable = task_runtime.run(["git", "-C", str(repository), "merge-base", "--is-ancestor",
                                  candidate_sha, target_ref], repository, check=False).returncode == 0
    if dirty or not reachable:
        return {"candidate_checkout": str(checkout), "outcome": "preserved",
                "reason": "dirty" if dirty else "candidate_unreachable_from_target"}
    result = task_runtime.run(["git", "-C", str(repository), "worktree", "remove", str(checkout)], repository, check=False)
    if result.returncode:
        return {"candidate_checkout": str(checkout), "outcome": "preserved", "reason": "worktree_remove_failed"}
    owner_marker(controller, checkout).unlink(missing_ok=True)
    return {"candidate_checkout": str(checkout), "outcome": "removed"}


def recover_incomplete(controller: Path, config: dict[str, Any], repository: Path) -> Optional[dict[str, Any]]:
    """Recover the small durable window between MERGING truth and target CAS."""
    with task_runtime.state_lock(controller):
        canonical_state = task_runtime.read_state(controller)
        tasks = canonical_state["tasks"]
        rows = [row for row in tasks.values() if isinstance(row, dict)
                and row.get("state") == "MERGING" and row.get("target_ref") == config["target_ref"]]
    if not rows:
        return None
    record = sorted(rows, key=lambda row: row["task_id"])[0]
    attempt = record.get("queue_attempt")
    if not isinstance(attempt, dict) or attempt.get("feature_sha") != record.get("tip_sha"):
        raise MergeQueueError("MERGING task has invalid recovery identity")
    current = task_runtime.ref_sha(repository, config["target_ref"])
    candidate = attempt.get("candidate_sha")
    if current == candidate:
        expected_tree = task_runtime.git(repository, "rev-parse", f"{candidate}^{{tree}}")
        if expected_tree != attempt.get("candidate_tree"):
            raise MergeQueueError("MERGING recovery candidate tree mismatch")
        attempt = complete_post_integration(controller, repository, attempt)
        attempt = {**attempt, "outcome": "MERGED", "readback_sha": current, "recovered": True}
        persist_attempt(controller, attempt, state_name="MERGED", remove_conflict=True)
        checkout_value = attempt.get("candidate_checkout")
        checkout = Path(checkout_value) if checkout_value and Path(checkout_value).is_dir() else None
        return {**attempt, "cleanup": cleanup_candidate(
            controller, repository, checkout, config["target_ref"], candidate, attempt.get("candidate_token")
        )}
    # CAS did not land (or another writer moved the target). Revalidate and
    # rebuild from the latest target rather than trusting pre-crash evidence.
    attempt = {**attempt, "outcome": "RECOVERED_RETRY", "observed_target_sha": current}
    conflict = target_entry(canonical_state, repository, config["target_ref"])["conflicts"].get(record["task_id"])
    if isinstance(conflict, dict) and conflict.get("resolved_candidate_sha") == candidate:
        persist_attempt(controller, attempt, state_name="CONFLICT_RESOLVED", conflict=conflict)
        # resolve is the explicit retry for a bound resolution candidate.
        raise MergeQueueError(f"resolved task {record['task_id']} recovered; retry with merge resolve")
    persist_attempt(controller, attempt, state_name="QUEUED")
    return None


def merge_next(controller: Path, task_id: Optional[str] = None,
               expected_plan_id: Optional[str] = None) -> dict[str, Any]:
    # An explicitly addressed ineligible task must fail before recovery,
    # runtime checks, candidate construction, validation, or queue mutation.
    if task_id is not None:
        if not task_runtime.TASK_RE.fullmatch(task_id):
            raise MergeQueueError("unsafe task id")
        with task_runtime.state_lock(controller):
            addressed = task_runtime.read_state(controller)["tasks"].get(task_id)
        if not isinstance(addressed, dict) or addressed.get("state") not in {
                "AWAITING_RISK", "REQUEUING_STALE"}:
            raise MergeQueueError("explicit next task is not awaiting risk or release evidence")
    config = task_runtime.load_config(controller)
    repository = task_runtime.product_repository(controller, config)
    with target_lock(controller, repository, config["target_ref"]):
        recovered = recover_incomplete(controller, config, repository)
        if recovered is not None:
            return recovered
        require_runtime_before_new_work(controller, repository, config)
        if task_id is not None:
            with task_runtime.state_lock(controller):
                record = task_runtime.read_state(controller)["tasks"].get(task_id)
            if not isinstance(record, dict) or record.get("state") not in {
                    "AWAITING_RISK", "REQUEUING_STALE"}:
                raise MergeQueueError("explicit next task changed eligibility before execution")
            assert_static_plan(controller, task_id, "next", expected_plan_id)
            return resume_awaiting(controller, config, repository, record)
        record = select_next(controller, config)
        feature_worktree = validate_record(config, repository, record)
        target_sha = task_runtime.ref_sha(repository, config["target_ref"])
        feature_sha = record["tip_sha"]
        feature_already_integrated = task_runtime.run(
            ["git", "-C", str(repository), "merge-base", "--is-ancestor",
             feature_sha, target_sha], repository, check=False).returncode == 0
        if feature_already_integrated:
            # Containment is useful diagnosis, not acceptance or integration
            # evidence. Only the explicit receipt-bound reconciliation path may
            # convert historical terminal proof into queue completion.
            raise MergeQueueError(
                f"queued tip {feature_sha} is already contained in {target_sha}, but ancestry "
                f"alone cannot prove delivery; recover with: yy merge reconcile plan {record['task_id']}")
        assert_static_plan(controller, record["task_id"], "next", expected_plan_id)
        runtime_pin = attempt_runtime_pin(repository, target_sha)
        attempt = {"schema_version": ATTEMPT_SCHEMA, "task_id": record["task_id"],
                   "target_ref": config["target_ref"], "expected_target_sha": target_sha,
                   "runtime_pin": runtime_pin,
                   "feature_sha": feature_sha, "strategy": None, "candidate_sha": None,
                   "candidate_tree": None, "candidate_checkout": None, "candidate_token": None, "validation": [],
                   "review": None, "outcome": "MERGING"}
        checkout: Optional[Path] = None
        direct = task_runtime.run(["git", "-C", str(repository), "merge-base", "--is-ancestor",
                                   target_sha, feature_sha], repository, check=False).returncode == 0
        if direct:
            attempt["strategy"] = "direct"
            candidate_sha = feature_sha
            validation_root = feature_worktree
        else:
            if task_runtime.run(["git", "-C", str(repository), "merge-base", "--is-ancestor",
                                 record["base_sha"], target_sha], repository, check=False).returncode:
                attempt["outcome"] = "STALE_TARGET"
                persist_attempt(controller, attempt, state_name="QUEUED")
                raise MergeQueueError("target no longer descends from the frozen feature base")
            attempt["strategy"] = "merge_both_parents"
            checkout, candidate_token = create_candidate_checkout(
                controller, repository, record["task_id"], config["target_ref"], target_sha, feature_sha
            )
            attempt["candidate_checkout"] = str(checkout)
            attempt["candidate_token"] = candidate_token
            merged = task_runtime.run(["git", "-C", str(checkout), "merge", "--no-ff", "--no-edit", feature_sha], checkout, check=False)
            if merged.returncode:
                conflicts = conflict_paths(checkout)
                if not conflicts:
                    attempt["outcome"] = "MERGE_FAILED"
                    rollback_unadmitted_candidate(controller, repository, checkout, candidate_token)
                    attempt.update({"candidate_checkout": None, "candidate_token": None})
                    persist_attempt(controller, attempt, state_name="QUEUED")
                    raise MergeQueueError(merged.stderr.strip() or "candidate merge failed")
                all_changed = changed_paths(checkout)
                guarded = sorted(set(all_changed) - set(conflicts))
                conflict = {"schema_version": ATTEMPT_SCHEMA, "task_id": record["task_id"],
                            "repository_identity": repository_identity(repository),
                            "target_ref": config["target_ref"], "expected_target_sha": target_sha,
                            "feature_sha": feature_sha, "candidate_checkout": str(checkout),
                            "candidate_token": candidate_token,
                            "candidate_head": task_runtime.git(checkout, "rev-parse", "HEAD"),
                            "merge_head": task_runtime.git(checkout, "rev-parse", "MERGE_HEAD"),
                            "conflict_paths": conflicts, "changed_paths": all_changed,
                            "guard_snapshot": guard_snapshot(checkout, guarded)}
                attempt["outcome"] = "CONFLICT"
                try:
                    persist_attempt(controller, attempt, state_name="CONFLICT", conflict=conflict)
                except Exception:
                    rollback_unadmitted_candidate(controller, repository, checkout, candidate_token)
                    raise
                return {**attempt, "conflict_paths": conflicts}
            candidate_sha = task_runtime.git(checkout, "rev-parse", "HEAD")
            parents = task_runtime.git(checkout, "show", "-s", "--format=%P", candidate_sha).split()
            if parents != [target_sha, feature_sha]:
                raise MergeQueueError("composed candidate does not have exact target/feature parents")
            validation_root = checkout
        attempt["candidate_sha"] = candidate_sha
        attempt["candidate_tree"] = task_runtime.git(repository, "rev-parse", f"{candidate_sha}^{{tree}}")
        try:
            attempt["validation"], attempt["command_evidence"] = authoritative_validation_rows(
                controller, config, repository, record, validation_root, candidate_sha,
                feature_worktree if checkout is not None else None,
            )
            assert_frozen_candidate(controller, config, validation_root, candidate_sha)
            if task_runtime.ref_sha(repository, config["target_ref"]) != target_sha:
                raise MergeQueueError("target moved before compare-and-swap; no ref was changed")
            decision = review_candidate(
                controller, record, candidate_sha, repository, target_sha,
                validation_root, attempt,
            )
            attempt["risk"] = decision
            attempt["review"] = decision
            assert_frozen_candidate(controller, config, validation_root, candidate_sha)
            if decision["status"] != "ELIGIBLE":
                attempt["outcome"] = decision["status"]
                persist_attempt(controller, attempt, state_name=decision["status"])
                return attempt
            try:
                persist_attempt(controller, attempt, state_name="MERGING")
            except Exception:
                if checkout is not None and attempt.get("candidate_token"):
                    rollback_unadmitted_candidate(
                        controller, repository, checkout, attempt["candidate_token"]
                    )
                raise
            authority = compile_live_authority_snapshot(
                controller, config, repository, record["task_id"])
            require_live_authority(
                controller, config, repository, record["task_id"], authority,
                boundary="before_target_cas")
            assert_target_unchecked_out(repository, config["target_ref"])
            attempt["integration_owner_authority"] = cas_target(
                repository, config["target_ref"], candidate_sha, target_sha
            )
            attempt = complete_post_integration(
                controller, repository, attempt, attempt["integration_owner_authority"])
        except MergeValidationError as exc:
            attempt["validation"] = exc.evidence
            attempt["outcome"] = "FAILED_TEST"
            if checkout is not None and attempt.get("candidate_token"):
                rollback_unadmitted_candidate(controller, repository, checkout, attempt["candidate_token"])
                attempt.update({"candidate_checkout": None, "candidate_token": None})
            persist_attempt(controller, attempt, state_name="QUEUED")
            raise
        except AuthorityDriftError:
            raise
        except PostIntegrationError:
            raise
        except MergeQueueError as exc:
            attempt["outcome"] = "STALE_TARGET" if "target moved" in str(exc) else "PRE_CAS_FAILED"
            if isinstance(exc, IntegrationOwnerAdvancementError):
                attempt["outcome"] = "MERGING_OWNER_ADVANCEMENT_FAILED"
                attempt["readback_sha"] = task_runtime.ref_sha(repository, config["target_ref"])
                attempt["recovery_command"] = "yy integration sync"
            elif attempt["outcome"] == "PRE_CAS_FAILED":
                # The task returned to QUEUED with no target mutation; record the
                # exact refusal and the safe retry entrypoint so recovery is
                # actionable instead of a bare requeue.
                attempt["failure"] = str(exc)
                attempt["recovery_command"] = "yy merge next"
            # MERGING is a crash-recovery window, not long-lived admission of a
            # clean composition checkout. Any ordinary pre-CAS refusal removes
            # the exact owned internal candidate before returning task truth to
            # QUEUED. Durable CONFLICT/CONFLICT_RESOLVED paths return elsewhere
            # and are intentionally never handled here.
            integrated = task_runtime.ref_sha(repository, config["target_ref"]) == candidate_sha
            if integrated:
                if not isinstance(exc, IntegrationOwnerAdvancementError):
                    attempt["outcome"] = "MERGING_READBACK_FAILED"
                persist_attempt(controller, attempt, state_name="MERGING")
                raise
            if checkout is not None and attempt.get("candidate_token"):
                rollback_unadmitted_candidate(controller, repository, checkout, attempt["candidate_token"])
                attempt.update({"candidate_checkout": None, "candidate_token": None})
            persist_attempt(controller, attempt, state_name="QUEUED")
            raise
        attempt["outcome"] = "MERGED"
        attempt["readback_sha"] = task_runtime.ref_sha(repository, config["target_ref"])
        persist_attempt(controller, attempt, state_name="MERGED", remove_conflict=True)
        cleanup = cleanup_candidate(
            controller, repository, checkout, config["target_ref"], candidate_sha, attempt.get("candidate_token")
        )
        return {**attempt, "cleanup": cleanup}


def verify_resolution(checkout: Path, conflict: dict[str, Any]) -> None:
    current = set(changed_paths(checkout))
    unexpected = current - set(conflict["changed_paths"])
    if unexpected:
        raise MergeQueueError(f"conflict checkout has unrelated drift: {', '.join(sorted(unexpected))}")
    for path, expected in conflict["guard_snapshot"].items():
        actual = {"worktree_sha256": file_digest(checkout / path),
                  "index": task_runtime.git(checkout, "ls-files", "-s", "--", path, check=False)}
        if actual != expected:
            raise MergeQueueError(f"conflict checkout changed outside conflict paths: {path}")
    unresolved = set(conflict_paths(checkout))
    if unresolved:
        raise MergeQueueError(f"conflict paths remain unresolved: {', '.join(sorted(unresolved))}")
    # A staged resolution that exactly selects the first parent legitimately
    # disappears from both diffs; the still-bound MERGE_HEAD proves it remains
    # an explicit merge resolution rather than an unrelated ordinary commit.


def verify_committed_resolution(checkout: Path, conflict: dict[str, Any], candidate_sha: str) -> None:
    if task_runtime.git(checkout, "status", "--porcelain=v1", "--untracked-files=all", check=False):
        raise MergeQueueError("committed resolution checkout is dirty")
    parents = task_runtime.git(checkout, "show", "-s", "--format=%P", candidate_sha).split()
    if parents != [conflict["expected_target_sha"], conflict["feature_sha"]]:
        raise MergeQueueError("committed resolution has unexpected parents")
    changed = set(filter(None, task_runtime.git(
        checkout, "diff", "--name-only", f"{conflict['expected_target_sha']}..{candidate_sha}"
    ).splitlines()))
    if changed - set(conflict["changed_paths"]):
        raise MergeQueueError("committed resolution changed unrelated paths")
    for path, expected in conflict["guard_snapshot"].items():
        index_parts = expected["index"].split()
        expected_blob = index_parts[1] if len(index_parts) >= 2 else ""
        actual_blob = task_runtime.git(checkout, "rev-parse", f"{candidate_sha}:{path}", check=False)
        if actual_blob != expected_blob:
            raise MergeQueueError(f"committed resolution changed outside conflict paths: {path}")


def merge_resolve(controller: Path, task_id: str,
                  expected_plan_id: Optional[str] = None) -> dict[str, Any]:
    if not task_runtime.TASK_RE.fullmatch(task_id):
        raise MergeQueueError("unsafe task id")
    with task_runtime.state_lock(controller):
        addressed = task_runtime.read_state(controller)["tasks"].get(task_id)
    if not isinstance(addressed, dict) or addressed.get("state") not in {
            "CONFLICT", "CONFLICT_RESOLVED"}:
        raise MergeQueueError("task has no bound CONFLICT candidate")
    initial_plan = assert_static_plan(controller, task_id, "resolve", expected_plan_id)
    config = task_runtime.load_config(controller)
    repository = task_runtime.product_repository(controller, config)
    with target_lock(controller, repository, config["target_ref"]):
        with task_runtime.state_lock(controller):
            state = task_runtime.read_state(controller)
            record = state["tasks"].get(task_id)
            conflict = target_entry(state, repository, config["target_ref"])["conflicts"].get(task_id)
        if (not isinstance(record, dict) or record.get("state") not in {"CONFLICT", "CONFLICT_RESOLVED"}
                or not isinstance(conflict, dict)):
            raise MergeQueueError("task has no bound CONFLICT candidate")
        if repository_identity(repository) != conflict["repository_identity"]:
            raise MergeQueueError("conflict repository identity drifted")
        if task_runtime.ref_sha(repository, config["target_ref"]) != conflict["expected_target_sha"]:
            raise MergeQueueError("target moved since conflict; preserve checkout and requeue explicitly")
        if task_runtime.git(repository, "rev-parse", record["branch_ref"], check=False) != conflict["feature_sha"]:
            raise MergeQueueError("feature tip moved since conflict")
        checkout = task_runtime.exact_root(Path(conflict["candidate_checkout"]), "conflict candidate checkout")
        if record["state"] == "CONFLICT":
            observed_head = task_runtime.git(checkout, "rev-parse", "HEAD", check=False)
            observed_merge_head = optional_revision(checkout, "MERGE_HEAD")
            if observed_head == conflict["candidate_head"] and observed_merge_head == conflict["merge_head"]:
                verify_resolution(checkout, conflict)
                task_runtime.run(["git", "-C", str(checkout), "commit", "--no-edit"], checkout)
                candidate_sha = task_runtime.git(checkout, "rev-parse", "HEAD")
            elif observed_merge_head is None:
                # The prior invocation may have committed the exact resolution
                # before an injected/crash-time atomic state-write failure.
                verify_committed_resolution(checkout, conflict, observed_head)
                candidate_sha = observed_head
            else:
                raise MergeQueueError("conflict candidate identity drifted")
        else:
            candidate_sha = conflict.get("resolved_candidate_sha")
            if (not isinstance(candidate_sha, str)
                    or task_runtime.git(checkout, "rev-parse", "HEAD", check=False) != candidate_sha
                    or task_runtime.git(checkout, "status", "--porcelain=v1", "--untracked-files=all", check=False)):
                raise MergeQueueError("resolved conflict candidate identity drifted")
        parents = task_runtime.git(checkout, "show", "-s", "--format=%P", candidate_sha).split()
        if parents != [conflict["expected_target_sha"], conflict["feature_sha"]]:
            raise MergeQueueError("resolved candidate does not have exact target/feature parents")
        attempt = {"schema_version": ATTEMPT_SCHEMA, "task_id": task_id,
                   "target_ref": config["target_ref"], "expected_target_sha": conflict["expected_target_sha"],
                   "runtime_pin": attempt_runtime_pin(repository, conflict["expected_target_sha"]),
                   "feature_sha": conflict["feature_sha"], "strategy": "resolved_merge",
                   "candidate_sha": candidate_sha,
                   "candidate_tree": task_runtime.git(checkout, "rev-parse", "HEAD^{tree}"),
                   "candidate_checkout": str(checkout), "candidate_token": conflict.get("candidate_token"),
                   "validation": [], "review": None,
                   "outcome": "MERGING"}
        resolved_conflict = {**conflict, "resolution_state": "RESOLVED",
                             "resolved_candidate_sha": candidate_sha,
                             "resolved_candidate_tree": attempt["candidate_tree"]}
        # The resolved commit is durable before validation. A failed test can
        # therefore retry this exact checkout/commit without a second merge.
        persist_attempt(controller, attempt, state_name="CONFLICT_RESOLVED", conflict=resolved_conflict)
        try:
            # Re-evaluate the same static implementation after the explicit
            # resolution commit, before any validation command. The caller's
            # reviewed pre-resolution identity was checked above.
            assert_static_plan(controller, task_id, "resolve-validation")
            attempt["feasibility_plan_id"] = initial_plan["plan_id"]
            attempt["validation"], attempt["command_evidence"] = authoritative_validation_rows(
                controller, config, repository, record, checkout, candidate_sha,
                task_runtime.exact_root(Path(record["worktree"]), "resolved feature worktree")
            )
            assert_frozen_candidate(controller, config, checkout, candidate_sha)
            if task_runtime.ref_sha(repository, config["target_ref"]) != conflict["expected_target_sha"]:
                raise MergeQueueError("target moved before compare-and-swap; no ref was changed")
            decision = review_candidate(
                controller, record, candidate_sha, repository,
                conflict["expected_target_sha"], checkout, attempt,
            )
            attempt["risk"] = decision
            attempt["review"] = decision
            assert_frozen_candidate(controller, config, checkout, candidate_sha)
            if decision["status"] != "ELIGIBLE":
                attempt["outcome"] = decision["status"]
                persist_attempt(controller, attempt, state_name=decision["status"],
                                conflict=resolved_conflict)
                return attempt
            persist_attempt(controller, attempt, state_name="MERGING")
            authority = compile_live_authority_snapshot(
                controller, config, repository, task_id)
            require_live_authority(controller, config, repository, task_id, authority,
                                   boundary="before_target_cas")
            assert_target_unchecked_out(repository, config["target_ref"])
            attempt["integration_owner_authority"] = cas_target(
                repository, config["target_ref"], candidate_sha,
                conflict["expected_target_sha"]
            )
            attempt = complete_post_integration(
                controller, repository, attempt, attempt["integration_owner_authority"])
        except MergeValidationError as exc:
            attempt["validation"] = exc.evidence
            attempt["outcome"] = "FAILED_TEST"
            persist_attempt(controller, attempt, state_name="CONFLICT_RESOLVED", conflict=resolved_conflict)
            raise
        except DependencyLockMismatchError as exc:
            attempt["outcome"] = "STALE_TARGET"
            attempt["dependency_lock_refusal"] = exc.evidence
            persist_attempt(controller, attempt, state_name="CONFLICT_RESOLVED", conflict=resolved_conflict)
            raise
        except AuthorityDriftError:
            raise
        except PostIntegrationError:
            raise
        except MergeQueueError as exc:
            integrated = task_runtime.ref_sha(repository, config["target_ref"]) == candidate_sha
            attempt["outcome"] = "MERGING_READBACK_FAILED" if integrated else "STALE_TARGET"
            if isinstance(exc, IntegrationOwnerAdvancementError):
                attempt["outcome"] = "MERGING_OWNER_ADVANCEMENT_FAILED"
                attempt["readback_sha"] = task_runtime.ref_sha(repository, config["target_ref"])
                attempt["recovery_command"] = "yy integration sync"
            persist_attempt(
                controller, attempt,
                state_name="MERGING" if integrated else "CONFLICT_RESOLVED",
                conflict=None if integrated else resolved_conflict,
            )
            raise
        attempt["outcome"] = "MERGED"
        attempt["readback_sha"] = task_runtime.ref_sha(repository, config["target_ref"])
        persist_attempt(controller, attempt, state_name="MERGED", remove_conflict=True)
        return {**attempt, "cleanup": cleanup_candidate(
            controller, repository, checkout, config["target_ref"], candidate_sha, attempt.get("candidate_token")
        )}


def dispatch_reviewer(controller: Path, candidate_root: Path, plan: dict[str, Any],
                      task_id: str, reviewer: str, sequence: int,
                      predecessor_receipt: Optional[Path],
                      attempt_number: int) -> dict[str, str]:
    """Launch one canonical reviewer. Tests replace this seam with a fake."""
    run_root = (controller / ".juno_task/runtime/merge-queue/reviews" / task_id
                / plan["candidate"]["candidate_sha"] / f"attempt-{attempt_number}"
                / f"{sequence}-{reviewer}")
    if run_root.exists():
        raise MergeQueueError(f"review output already exists; inspect before retry: {run_root}")
    binding_path = run_root.parent / f"{sequence}-{reviewer}.binding.json"
    prompt = render_managed_review_prompt(
        controller, candidate_root, plan, task_id, reviewer, sequence,
        run_root.parent / f"{sequence}-{reviewer}.prompt.md",
    )
    try:
        risk_runtime.write_review_binding(
            binding_path, candidate_sha=plan["candidate"]["candidate_sha"],
            policy_identity=plan["policy_identity"], reviewer=reviewer,
            predecessor_receipt=predecessor_receipt,
        )
        branch = task_runtime.git(controller, "symbolic-ref", "-q", "HEAD")
        command = risk_runtime.reviewer_command(
            Path(__file__).resolve().parent, controller_root=controller,
            controller_branch=branch, candidate_root=candidate_root,
            candidate_sha=plan["candidate"]["candidate_sha"], prompt_file=prompt,
            out_dir=run_root, reviewer=reviewer, task_id=task_id,
            review_binding_path=binding_path,
        )
        result = subprocess.run(command, cwd=controller, stdin=subprocess.DEVNULL,
                                text=True, capture_output=True)
        if result.returncode:
            raise MergeQueueError(
                f"managed {reviewer} failed: {(result.stderr or result.stdout)[-512:]}"
            )
        payload = json.loads(result.stdout)
        receipt = Path(payload["receipt"]).resolve()
        return {"runner_receipt_path": str(receipt),
                "runner_receipt_sha256": hashlib.sha256(receipt.read_bytes()).hexdigest()}
    except (risk_runtime.RiskPolicyError, OSError, KeyError, json.JSONDecodeError) as exc:
        raise MergeQueueError(f"managed {reviewer} evidence failed: {exc}") from exc


def managed_review_prompt(controller: Path) -> Path:
    """Resolve review guidance without materializing it in a sparse controller."""
    legacy = controller / ".juno_task/prompts/review_commit_parallel_runner.md"
    if legacy.is_file():
        return legacy.resolve()

    identity_path = controller / ".juno_task/runtime/identity.json"
    try:
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        executable = Path(identity["executable"]).expanduser().resolve()
        expected_sha = identity["executable_sha256"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise MergeQueueError("managed review prompt runtime identity is missing or invalid") from exc
    if not executable.is_file() or not isinstance(expected_sha, str):
        raise MergeQueueError("managed review prompt runtime identity is missing or invalid")
    if hashlib.sha256(executable.read_bytes()).hexdigest() != expected_sha:
        raise MergeQueueError("managed review prompt runtime executable hash drifted")

    prompt = executable.parent.parent / "templates/prompts/review_commit_parallel_runner.md"
    if not prompt.is_file():
        raise MergeQueueError("managed review prompt is missing from the installed runtime")
    return prompt.resolve()


def bounded_json_reference(reference: dict[str, Any], limit: int, label: str) -> dict[str, Any]:
    if (not isinstance(reference, dict)
            or set(reference) not in ({"path", "sha256"}, {"path", "sha256", "bytes"})):
        raise MergeQueueError(f"{label} reference is malformed")
    path = Path(str(reference["path"])).resolve()
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise MergeQueueError(f"{label} is missing") from exc
    if (not data or len(data) > limit or hashlib.sha256(data).hexdigest() != reference["sha256"]
            or ("bytes" in reference and reference["bytes"] != len(data))):
        raise MergeQueueError(f"{label} identity is invalid")
    try:
        value = json.loads(data)
    except json.JSONDecodeError as exc:
        raise MergeQueueError(f"{label} is not JSON") from exc
    if not isinstance(value, dict):
        raise MergeQueueError(f"{label} must be one object")
    return value


def prior_findings_summary(controller: Path, record: dict[str, Any],
                           plan: dict[str, Any]) -> tuple[str, str]:
    evidence_root = controller / ".juno_task/runtime/merge-queue/evidence" / record["task_id"]
    explicit_sha = record.get("prior_findings_candidate_sha")
    if explicit_sha is not None and not isinstance(explicit_sha, str):
        raise MergeQueueError("prior review findings candidate identity is malformed")
    prior_sha = explicit_sha
    paths = sorted(evidence_root.glob(f"{prior_sha}.attempt-*.json")) if prior_sha else []
    if explicit_sha and not paths:
        compact_findings = record.get("prior_review_findings")
        if not isinstance(compact_findings, list) or not compact_findings:
            raise MergeQueueError("prior review findings evidence is missing")
        summaries = []
        references = []
        for review in compact_findings:
            if (not isinstance(review, dict) or not isinstance(review.get("findings"), list)
                    or not isinstance(review.get("review_reference"), dict)):
                raise MergeQueueError("prior cancellation-bound findings are malformed")
            runner = review["review_reference"]
            references.append(
                f"{runner.get('receipt_path')} sha256={runner.get('receipt_sha256')}")
            for finding in review["findings"]:
                summaries.append(
                    f"- {finding.get('code', 'UNKNOWN')} "
                    f"[{finding.get('normalized_severity', 'unknown')}]: "
                    f"{finding.get('summary', finding.get('impact', ''))}")
        return (f"Immediate prior candidate: {explicit_sha}\n" + "\n".join(summaries),
                "; ".join(references))
    if not paths:
        legacy_sha = record.get("reopened_from_candidate_sha")
        legacy_paths = (sorted(evidence_root.glob(f"{legacy_sha}.attempt-*.json"))
                        if isinstance(legacy_sha, str) else [])
        candidates: list[tuple[str, str, list[Path]]] = []
        grouped: dict[str, list[Path]] = {}
        for evidence_path in evidence_root.glob("*.attempt-*.json"):
            grouped.setdefault(evidence_path.name.split(".attempt-", 1)[0], []).append(evidence_path)
        limit = plan["evidence_limits"]["max_receipt_bytes"]
        for candidate_sha, candidate_paths in grouped.items():
            newest = ""
            has_findings = False
            valid = True
            for evidence_path in sorted(candidate_paths):
                data = evidence_path.read_bytes()
                if not data or len(data) > limit:
                    valid = False
                    break
                evidence = json.loads(data)
                candidate = evidence.get("candidate", {})
                if (candidate.get("candidate_sha") != candidate_sha
                        or candidate.get("base_sha") != plan["candidate"]["base_sha"]
                        or candidate.get("target_ref") != plan["candidate"]["target_ref"]):
                    valid = False
                    break
                newest = max(newest, str(evidence.get("created_at", "")))
                has_findings = has_findings or any(
                    review.get("verdict") == "findings"
                    for review in evidence.get("reviews", []) if isinstance(review, dict)
                )
            if valid and has_findings:
                candidates.append((newest, candidate_sha, sorted(candidate_paths)))
        if candidates:
            legacy_candidates = [row for row in candidates if row[1] == legacy_sha]
            _, prior_sha, paths = max(
                legacy_candidates or candidates, key=lambda row: (row[0], row[1]))
    if not prior_sha or not paths:
        return ("No prior reviewed candidate is bound to this exact queue record.",
                "queue-state:none")
    summaries: list[str] = []
    evidence_refs: list[str] = []
    limit = plan["evidence_limits"]["max_receipt_bytes"]
    for evidence_path in paths:
        data = evidence_path.read_bytes()
        if not data or len(data) > limit:
            raise MergeQueueError("prior review evidence exceeds its bound")
        evidence = json.loads(data)
        if evidence.get("candidate", {}).get("candidate_sha") != prior_sha:
            raise MergeQueueError("prior review evidence candidate identity drifted")
        evidence_refs.append(
            f"{evidence_path.resolve()} sha256={hashlib.sha256(data).hexdigest()}"
        )
        for review in evidence.get("reviews", []):
            reference = review.get("review_reference", {})
            runner = bounded_json_reference(
                {"path": reference.get("receipt_path"),
                 "sha256": reference.get("receipt_sha256")},
                limit, "prior managed reviewer receipt",
            )
            response = bounded_json_reference(
                runner.get("artifacts", {}).get("response", {}), limit,
                "prior managed reviewer response",
            )
            for finding in response.get("findings", []):
                if not isinstance(finding, dict):
                    raise MergeQueueError("prior reviewer finding is malformed")
                summaries.append(
                    f"- {finding.get('code', 'UNKNOWN')} [{finding.get('severity', 'unknown')}]: "
                    f"{finding.get('summary', '')}"
                )
    if not summaries:
        summaries.append("- The immediate prior candidate had no recorded blocking finding text.")
    return (f"Immediate prior candidate: {prior_sha}\n" + "\n".join(summaries),
            "; ".join(evidence_refs))


def render_review_template(template: str, fields: dict[str, str]) -> str:
    names = set(REVIEW_PLACEHOLDER_RE.findall(template))
    if names != REVIEW_PROMPT_FIELDS or set(fields) != REVIEW_PROMPT_FIELDS:
        missing = sorted(REVIEW_PROMPT_FIELDS - names)
        unknown = sorted(names - REVIEW_PROMPT_FIELDS)
        raise MergeQueueError(
            f"managed review prompt placeholder contract drifted; missing={missing} unknown={unknown}"
        )
    rendered = REVIEW_PLACEHOLDER_RE.sub(lambda match: fields[match.group(1)], template)
    for name in REVIEW_PROMPT_FIELDS:
        if re.search(r"{{\s*" + re.escape(name) + r"\s*}}", rendered):
            raise MergeQueueError(f"managed review prompt retained placeholder {name}")
    return rendered


def render_managed_review_prompt(controller: Path, candidate_root: Path,
                                 plan: dict[str, Any], task_id: str,
                                 reviewer: str, sequence: int,
                                 output_path: Path) -> Path:
    template_path = managed_review_prompt(controller)
    template_data = template_path.read_bytes()
    if not template_data or len(template_data) > 65536:
        raise MergeQueueError("managed review prompt template is empty or unbounded")
    with task_runtime.state_lock(controller):
        record = task_runtime.read_state(controller)["tasks"].get(task_id)
    attempt = record.get("queue_attempt") if isinstance(record, dict) else None
    stored = attempt.get("risk") if isinstance(attempt, dict) else None
    if (not isinstance(record, dict) or record.get("state") != "AWAITING_RISK"
            or not isinstance(attempt, dict) or not isinstance(stored, dict)
            or attempt.get("candidate_sha") != plan["candidate"]["candidate_sha"]
            or stored.get("plan") != plan):
        raise MergeQueueError("review prompt queue binding changed before dispatch")
    task_path = task_runtime.task_file(controller, task_id).resolve()
    task_data = task_path.read_bytes()
    if not task_data or len(task_data) > 65536:
        raise MergeQueueError("canonical review task is empty or unbounded")
    progress = stored.get("review_progress", {})
    admission = progress.get("full_suite_admission") if isinstance(progress, dict) else None
    if plan["full_suite_required"]:
        receipts = admission.get("receipts") if isinstance(admission, dict) else None
        singular = admission.get("receipt") if isinstance(admission, dict) else None
        if isinstance(receipts, list) and receipts:
            # Package-local routing admits a bounded list of suite receipts;
            # every receipt must remain byte-identical to its bound digest.
            for row in receipts:
                if not isinstance(row, dict) or set(row) != {"receipt_path", "receipt_sha256"}:
                    raise MergeQueueError("review prompt full-suite evidence is missing")
                receipt_data = Path(row["receipt_path"]).read_bytes()
                if hashlib.sha256(receipt_data).hexdigest() != row["receipt_sha256"]:
                    raise MergeQueueError("review prompt full-suite evidence identity drifted")
            validation_path = "; ".join(
                f"{row['receipt_path']} sha256={row['receipt_sha256']}" for row in receipts)
        elif isinstance(singular, dict) and set(singular) == {"receipt_path", "receipt_sha256"}:
            receipt_path = Path(singular["receipt_path"]).resolve()
            receipt_data = receipt_path.read_bytes()
            if hashlib.sha256(receipt_data).hexdigest() != singular["receipt_sha256"]:
                raise MergeQueueError("review prompt full-suite evidence identity drifted")
            validation_path = f"{receipt_path} sha256={singular['receipt_sha256']}"
        else:
            raise MergeQueueError("review prompt full-suite evidence is missing")
    else:
        validation_path = "queue-state: affected validation embedded below"
    findings_summary, findings_path = prior_findings_summary(controller, record, plan)
    validation_bundle = canonical({
        "affected_validation": attempt.get("validation", []),
        "full_suite_admission": admission,
    })
    try:
        requirement_identity = task_runtime.canonical_requirement_identity(controller, task_id)
    except task_runtime.TaskWorkspaceError as exc:
        raise MergeQueueError(str(exc)) from exc
    requirements = (
        f"Canonical Kanban task ({task_path}, requirements identity "
        f"{requirement_identity['requirements_sha256']}):\n\n"
        + task_data.decode("utf-8")
        + "\n\nCanonical task/PDR revision identity:\n\n"
        + canonical(requirement_identity)
        + "\n\nQueue-bound risk plan:\n\n" + canonical(plan)
        + "\n\nQueue-bound validation summary:\n\n" + validation_bundle
    )
    fields = {
        "task_id": task_id,
        "review_kind": f"merge-queue-{plan['tier']}-risk",
        "reviewer_index": f"{sequence}:{reviewer}",
        "repository": str(candidate_root.resolve()),
        "base_sha": plan["candidate"]["base_sha"],
        "tip_sha": plan["candidate"]["candidate_sha"],
        "checklist_path": (f"{task_path} requirements_sha256="
                           f"{requirement_identity['requirements_sha256']}"),
        "findings_summary_path": findings_path,
        "validation_evidence_path": validation_path,
        "requirements_bundle": requirements,
        "findings_summary": findings_summary,
    }
    try:
        rendered = render_review_template(template_data.decode("utf-8"), fields).encode()
    except UnicodeDecodeError as exc:
        raise MergeQueueError("managed review prompt template is not UTF-8") from exc
    if not rendered or len(rendered) > 524288:
        raise MergeQueueError("rendered managed review prompt is empty or unbounded")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise MergeQueueError(f"rendered review prompt already exists: {output_path}") from exc
    with os.fdopen(fd, "wb") as handle:
        handle.write(rendered); handle.flush(); os.fsync(handle.fileno())
    return output_path.resolve()


def full_validation_identity(controller: Path, config: dict[str, Any],
                             record: dict[str, Any], candidate_root: Path,
                             candidate_sha: str) -> dict[str, Any]:
    policy_path = controller / ".juno_task/config/task-workspace.json"
    try:
        policy_bytes = policy_path.read_bytes()
    except OSError as exc:
        raise MergeQueueError("task-workspace policy is missing during review") from exc
    if not policy_bytes or len(policy_bytes) > 65536:
        raise MergeQueueError("task-workspace policy bytes are empty or unbounded")
    recorded = record.get("validation")
    selected_focused = task_runtime.selected_focused_rows(
        config, record.get("changed_paths"))
    if not isinstance(recorded, list) or len(recorded) != len(selected_focused):
        raise MergeQueueError("task record validation command evidence is missing")
    command_projection = []
    for row in recorded:
        if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
                or not isinstance(row.get("argv"), list)
                or not isinstance(row.get("timeout_seconds"), int)):
            raise MergeQueueError("task record validation command evidence is malformed")
        command_projection.append({"id": row["id"], "argv": row["argv"],
                                   "timeout_seconds": row["timeout_seconds"]})
    commands = canonical(command_projection).encode()
    suite_commands, routing = task_runtime.selected_full_suite_commands(
        config, record.get("changed_paths"))
    if routing["mode"] == "default":
        # Byte-compatible with pre-routing identities: the default suite row.
        full_config = canonical(config["full_suite_validation"]).encode()
    else:
        full_config = canonical([{"id": row["id"], "argv": row["argv"],
                                  "timeout_seconds": row["timeout_seconds"]}
                                 for row in suite_commands]).encode()
    if len(commands) > 65536 or len(full_config) > 65536:
        raise MergeQueueError("validation identity projection is unbounded")
    identity = {"task_workspace_config_sha256": hashlib.sha256(policy_bytes).hexdigest(),
                "full_suite_config_sha256": hashlib.sha256(full_config).hexdigest(),
                "task_validation_commands_sha256": hashlib.sha256(commands).hexdigest()}
    if routing["mode"] != "default":
        identity["validation_routing_sha256"] = digest(routing)
    return identity


def full_suite_attempt_paths(controller: Path, task_id: str, candidate_sha: str,
                             attempt_number: int) -> tuple[Path, Path]:
    root = (controller / ".juno_task/state/merge-queue/full-suite" / task_id
            / candidate_sha / f"attempt-{attempt_number}").resolve()
    return root / "claim.json", root / "receipt.json"


def create_full_suite_claim_legacy(controller: Path, task_id: str, plan: dict[str, Any],
                            identity: dict[str, str], command: dict[str, Any],
                            attempt_number: int) -> dict[str, Any]:
    claim_path, receipt_path = full_suite_attempt_paths(
        controller, task_id, plan["candidate"]["candidate_sha"], attempt_number)
    if claim_path.exists() or receipt_path.exists():
        raise MergeQueueError("queue admission canonical path already exists")
    token = secrets.token_hex(24)
    claim = {"schema_version": risk_runtime.FULL_SUITE_CLAIM_SCHEMA,
             "producer": {"schema_version": risk_runtime.FULL_SUITE_PRODUCER_SCHEMA,
                          "tool_id": risk_runtime.FULL_SUITE_TOOL_ID},
             "task_id": task_id,
             "candidate": {"candidate_sha": plan["candidate"]["candidate_sha"],
                           "candidate_tree": plan["candidate"]["candidate_tree"]},
             "policy_identity": plan["policy_identity"],
             "validation_identity": identity, "command": command,
             "token": token, "attempt_number": attempt_number,
             "expected_receipt_path": str(receipt_path)}
    write_canonical_exclusive(claim_path, claim,
                              plan["evidence_limits"]["max_receipt_bytes"])
    if receipt_path.exists():
        raise MergeQueueError("queue admission receipt path collided before suite execution")
    claim_ref = {"claim_path": str(claim_path),
                 "claim_sha256": hashlib.sha256(claim_path.read_bytes()).hexdigest()}
    return {"schema_version": risk_runtime.FULL_SUITE_ADMISSION_SCHEMA,
            "state": "CLAIMED", "attempt_number": attempt_number, "token": token,
            "claim": claim_ref, "expected_receipt_path": str(receipt_path)}


def persist_full_suite_claim(controller: Path, attempt: dict[str, Any],
                             suite_attempt_number: int,
                             create: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Create the exclusive claim and admit it in one brief state-lock section."""
    with task_runtime.state_lock(controller):
        state = task_runtime.read_state(controller)
        current = state["tasks"].get(attempt["task_id"])
        current_attempt = current.get("queue_attempt") if isinstance(current, dict) else None
        if (not isinstance(current_attempt, dict)
                or current.get("tip_sha") != attempt["feature_sha"]
                or any(current_attempt.get(key) != attempt.get(key) for key in
                       ("task_id", "feature_sha", "candidate_sha", "expected_target_sha"))):
            raise MergeQueueError("task review claim changed before full-suite admission")
        try:
            claimed = create()
        except MergeQueueError as exc:
            if "exists" not in str(exc) and "collided" not in str(exc):
                raise
            stored = {**attempt["risk"], "review_progress": {
                **attempt["risk"]["review_progress"],
                "collision_floor": suite_attempt_number}}
            updated = {**attempt, "risk": stored, "review": stored}
            state["tasks"][attempt["task_id"]] = {
                **current, "state": "AWAITING_RISK", "queue_attempt": updated,
                "last_queue_outcome": "FULL_SUITE_CLAIM_COLLISION"}
            config = task_runtime.load_config(controller)
            repository = task_runtime.product_repository(controller, config)
            target_entry(state, repository, config["target_ref"])["last_attempt"] = updated
            task_runtime.write_state(controller, state)
            raise
        stored = {**attempt["risk"], "review_progress": {
            **attempt["risk"]["review_progress"], "attempt_counter": suite_attempt_number,
            "full_suite_admission": claimed}}
        updated = {**attempt, "risk": stored, "review": stored, "outcome": "REVIEWING"}
        state["tasks"][attempt["task_id"]] = {
            **current, "state": "AWAITING_RISK", "queue_attempt": updated,
            "last_queue_outcome": updated["outcome"]}
        config = task_runtime.load_config(controller)
        repository = task_runtime.product_repository(controller, config)
        target_entry(state, repository, config["target_ref"])["last_attempt"] = updated
        task_runtime.write_state(controller, state)
    return claimed, updated


def verify_queue_claimed_admission_legacy(controller: Path, task_id: str, plan: dict[str, Any],
                                    identity: dict[str, str], command: dict[str, Any],
                                    admission: Any) -> dict[str, Any]:
    keys = {"schema_version", "state", "attempt_number", "token", "claim",
            "expected_receipt_path"}
    if (not isinstance(admission, dict) or set(admission) != keys
            or admission.get("schema_version") != risk_runtime.FULL_SUITE_ADMISSION_SCHEMA
            or admission.get("state") != "CLAIMED"
            or not isinstance(admission.get("attempt_number"), int)
            or isinstance(admission.get("attempt_number"), bool)
            or admission["attempt_number"] <= 0
            or not isinstance(admission.get("token"), str)
            or len(admission["token"]) != 48
            or not isinstance(admission.get("claim"), dict)
            or set(admission["claim"]) != {"claim_path", "claim_sha256"}):
        raise MergeQueueError("stored CLAIMED full-suite admission is malformed")
    claim_path, receipt_path = full_suite_attempt_paths(
        controller, task_id, plan["candidate"]["candidate_sha"], admission["attempt_number"])
    if (admission["claim"].get("claim_path") != str(claim_path)
            or admission.get("expected_receipt_path") != str(receipt_path)):
        raise MergeQueueError("stored CLAIMED full-suite admission is not canonical")
    try:
        claim = risk_runtime._bounded_object(
            admission["claim"]["claim_path"], admission["claim"]["claim_sha256"],
            plan, "full-suite claim")
    except risk_runtime.RiskPolicyError as exc:
        raise MergeQueueError(f"stored CLAIMED full-suite admission refused: {exc}") from exc
    expected = {"schema_version": risk_runtime.FULL_SUITE_CLAIM_SCHEMA,
                "producer": {"schema_version": risk_runtime.FULL_SUITE_PRODUCER_SCHEMA,
                             "tool_id": risk_runtime.FULL_SUITE_TOOL_ID},
                "task_id": task_id,
                "candidate": {"candidate_sha": plan["candidate"]["candidate_sha"],
                              "candidate_tree": plan["candidate"]["candidate_tree"]},
                "policy_identity": plan["policy_identity"],
                "validation_identity": identity, "command": command,
                "token": admission["token"], "attempt_number": admission["attempt_number"],
                "expected_receipt_path": str(receipt_path)}
    if claim != expected:
        raise MergeQueueError("stored CLAIMED full-suite claim identity drifted")
    return {**admission, "claim": {"claim_path": str(claim_path),
                                    "claim_sha256": admission["claim"]["claim_sha256"]},
            "expected_receipt_path": str(receipt_path)}


def verify_queue_full_suite_admission_legacy(controller: Path, task_id: str, plan: dict[str, Any],
                                      identity: dict[str, str], command: dict[str, Any],
                                      admission: Any) -> dict[str, Any]:
    if not isinstance(admission, dict) or admission.get("state") != "COMPLETE":
        raise MergeQueueError("full-suite admission is not complete")
    attempt_number = admission.get("attempt_number")
    if not isinstance(attempt_number, int) or isinstance(attempt_number, bool):
        raise MergeQueueError("full-suite admission attempt is invalid")
    claim_path, receipt_path = full_suite_attempt_paths(
        controller, task_id, plan["candidate"]["candidate_sha"], attempt_number)
    state_root = (controller / ".juno_task/state/merge-queue/full-suite").resolve()
    for path in (claim_path, receipt_path):
        try: path.resolve().relative_to(state_root)
        except ValueError as exc: raise MergeQueueError("full-suite admission escaped controller state") from exc
    if (admission.get("claim", {}).get("claim_path") != str(claim_path)
            or admission.get("receipt", {}).get("receipt_path") != str(receipt_path)):
        raise MergeQueueError("full-suite admission is not at its canonical queue-owned path")
    try:
        return risk_runtime.verify_full_suite_admission(
            admission, plan, identity, command)
    except risk_runtime.RiskPolicyError as exc:
        raise MergeQueueError(f"full-suite admission refused: {exc}") from exc


def failed_full_suite_admission_legacy(controller: Path, task_id: str, plan: dict[str, Any],
                                identity: dict[str, str], command: dict[str, Any],
                                claimed: dict[str, Any],
                                receipt_reference: dict[str, str]) -> dict[str, Any]:
    attempt_number = claimed.get("attempt_number")
    claim_path, receipt_path = full_suite_attempt_paths(
        controller, task_id, plan["candidate"]["candidate_sha"], attempt_number)
    if (claimed.get("claim", {}).get("claim_path") != str(claim_path)
            or claimed.get("expected_receipt_path") != str(receipt_path)
            or receipt_reference.get("receipt_path") != str(receipt_path)):
        raise MergeQueueError("failed full-suite admission is not at its canonical path")
    complete = {"schema_version": risk_runtime.FULL_SUITE_ADMISSION_SCHEMA,
                "state": "COMPLETE", "attempt_number": attempt_number,
                "token": claimed.get("token"), "claim": claimed.get("claim"),
                "receipt": receipt_reference}
    try:
        risk_runtime.verify_full_suite_admission(
            complete, plan, identity, command, False)
    except risk_runtime.RiskPolicyError as exc:
        raise MergeQueueError(f"failed full-suite receipt refused: {exc}") from exc
    receipt = json.loads(receipt_path.read_text())
    result = receipt["result"]
    if not result["timed_out"] and result["exit_code"] == 0:
        raise MergeQueueError("successful full-suite receipt cannot enter FAILED admission")
    failure = {"exit_code": result["exit_code"], "timed_out": result["timed_out"],
               "stdout_tail": result["stdout"]["tail"],
               "stderr_tail": result["stderr"]["tail"]}
    return {"schema_version": risk_runtime.FULL_SUITE_ADMISSION_SCHEMA,
            "state": "FAILED", "attempt_number": attempt_number,
            "token": claimed["token"], "claim": claimed["claim"],
            "receipt": receipt_reference, "failure": failure}


def verify_queue_failed_admission_legacy(controller: Path, task_id: str, plan: dict[str, Any],
                                   identity: dict[str, str], command: dict[str, Any],
                                   admission: Any) -> dict[str, Any]:
    keys = {"schema_version", "state", "attempt_number", "token", "claim",
            "receipt", "failure"}
    if (not isinstance(admission, dict) or set(admission) != keys
            or admission.get("schema_version") != risk_runtime.FULL_SUITE_ADMISSION_SCHEMA
            or admission.get("state") != "FAILED"):
        raise MergeQueueError("stored FAILED full-suite admission is malformed")
    attempt_number = admission.get("attempt_number")
    _, receipt_path = full_suite_attempt_paths(
        controller, task_id, plan["candidate"]["candidate_sha"], attempt_number)
    try:
        historical_claim = risk_runtime._bounded_object(
            admission.get("claim", {}).get("claim_path"),
            admission.get("claim", {}).get("claim_sha256"), plan, "failed full-suite claim")
        historical_identity = historical_claim["validation_identity"]
        historical_command = historical_claim["command"]
    except (risk_runtime.RiskPolicyError, KeyError, TypeError) as exc:
        raise MergeQueueError(f"stored FAILED full-suite claim refused: {exc}") from exc
    claimed = {"schema_version": risk_runtime.FULL_SUITE_ADMISSION_SCHEMA,
               "state": "CLAIMED", "attempt_number": attempt_number,
               "token": admission.get("token"), "claim": admission.get("claim"),
               "expected_receipt_path": str(receipt_path)}
    verify_queue_claimed_admission_legacy(
        controller, task_id, plan, historical_identity, historical_command, claimed)
    rebuilt = failed_full_suite_admission_legacy(
        controller, task_id, plan, historical_identity, historical_command,
        claimed, admission.get("receipt"))
    if rebuilt != admission:
        raise MergeQueueError("stored FAILED full-suite admission projection drifted")
    return rebuilt


def _terminal_receipt_claims_success(receipt_path: Path) -> bool:
    """True when one written terminal suite receipt is well-formed and claims
    a successful (exit 0, not timed out) result. Used to separate an
    unverifiable-but-successful complete attempt, which supersedes with a fresh
    attempt, from malformed or failing bytes that must keep failing closed."""
    try:
        receipt = json.loads(receipt_path.read_text())
    except (OSError, ValueError):
        return False
    result = receipt.get("result") if isinstance(receipt, dict) else None
    return (isinstance(result, dict)
            and result.get("exit_code") == 0
            and result.get("timed_out") is False)


def recover_claimed_full_suite_legacy(controller: Path, task_id: str, plan: dict[str, Any],
                               identity: dict[str, str], command: dict[str, Any],
                               admission: Any) -> Optional[dict[str, Any]]:
    admission = verify_queue_claimed_admission_legacy(
        controller, task_id, plan, identity, command, admission)
    attempt_number = admission["attempt_number"]
    claim_path, receipt_path = full_suite_attempt_paths(
        controller, task_id, plan["candidate"]["candidate_sha"], attempt_number)
    if (admission.get("claim", {}).get("claim_path") != str(claim_path)
            or admission.get("expected_receipt_path") != str(receipt_path)):
        raise MergeQueueError("stored CLAIMED full-suite admission path drifted")
    if not receipt_path.exists():
        return admission
    complete = {"schema_version": risk_runtime.FULL_SUITE_ADMISSION_SCHEMA,
                "state": "COMPLETE", "attempt_number": attempt_number,
                "token": admission.get("token"), "claim": admission.get("claim"),
                "receipt": evidence_reference(receipt_path)}
    try:
        return verify_queue_full_suite_admission_legacy(
            controller, task_id, plan, identity, command, complete)
    except MergeQueueError as success_error:
        try:
            return failed_full_suite_admission_legacy(
                controller, task_id, plan, identity, command, admission,
                complete["receipt"])
        except MergeQueueError:
            # Mirrors the v3 recovery contract: unverifiable-but-successful
            # complete receipts supersede with a fresh attempt; malformed or
            # failing bytes keep failing closed without a paid retry.
            if not _terminal_receipt_claims_success(receipt_path):
                raise success_error
            return {"schema_version": risk_runtime.FULL_SUITE_ADMISSION_SCHEMA,
                    "state": "UNVERIFIED", "attempt_number": attempt_number,
                    "token": admission.get("token"),
                    "claim": admission.get("claim"),
                    "receipt": complete["receipt"],
                    "reason": str(success_error)}


def full_suite_attempt_root(controller: Path, task_id: str, candidate_sha: str,
                            attempt_number: int) -> Path:
    return (controller / ".juno_task/state/merge-queue/full-suite" / task_id
            / candidate_sha / f"attempt-{attempt_number}").resolve()


def full_suite_producer_lock_path(controller: Path, task_id: str, candidate_sha: str,
                                  attempt_number: int) -> Path:
    return full_suite_attempt_root(controller, task_id, candidate_sha, attempt_number) / "producer.lock"


def full_suite_receipt_paths(root: Path, commands: list[dict[str, Any]]) -> list[Path]:
    return [root / f"receipt-{index}.json" for index in range(1, len(commands) + 1)]


@contextmanager
def full_suite_producer_lock(controller: Path, task_id: str, candidate_sha: str,
                             attempt_number: int) -> Iterator[None]:
    """Kernel-released liveness proof for one exact claim attempt.

    The producer holds this lock from before claim creation until the terminal
    admission is durably persisted; withdrawal must acquire it non-blocking.
    """
    lock_path = full_suite_producer_lock_path(controller, task_id, candidate_sha, attempt_number)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise MergeQueueError(
                    "a live full-suite producer owns this claim") from exc
            raise
        yield


def adopt_orphaned_full_suite_claim(controller: Path, task_id: str, plan: dict[str, Any],
                                     identity: dict[str, str], commands: list[dict[str, Any]],
                                     routing: dict[str, Any],
                                     attempt_number: int) -> Optional[dict[str, Any]]:
    """Adopt an identity-verified orphaned CLAIMED claim after state loss.

    A paused review can lose its stored full-suite admission reference (for
    example when an explicit ``next`` resumes the risk decision) while the
    immutable claim file remains on disk. Rather than failing closed forever
    on the canonical-path collision, strictly re-derive the expected claim
    identity and adopt it only when every bound field still matches exactly;
    the caller's suite runner re-verifies any receipt prefix before resuming.
    """
    root = full_suite_attempt_root(
        controller, task_id, plan["candidate"]["candidate_sha"], attempt_number)
    claim_path = root / "claim.json"
    if not claim_path.is_file():
        return None
    try:
        stored = json.loads(claim_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(stored, dict):
        return None
    receipt_paths = full_suite_receipt_paths(root, commands)
    expected_lock = {"path": str(root / "producer.lock"), "kind": "flock"}
    expected = {"schema_version": risk_runtime.FULL_SUITE_CLAIM_V2_SCHEMA,
                "producer": {"schema_version": risk_runtime.FULL_SUITE_PRODUCER_SCHEMA,
                             "tool_id": risk_runtime.FULL_SUITE_TOOL_ID},
                "task_id": task_id,
                "candidate": {"candidate_sha": plan["candidate"]["candidate_sha"],
                              "candidate_tree": plan["candidate"]["candidate_tree"]},
                "policy_identity": plan["policy_identity"],
                "validation_identity": identity, "commands": commands,
                "routing": routing, "producer_lock": expected_lock,
                "attempt_number": attempt_number,
                "expected_receipt_paths": [str(path) for path in receipt_paths]}
    comparison = {key: stored.get(key) for key in expected}
    if comparison != expected or not isinstance(stored.get("token"), str):
        return None
    return {"schema_version": risk_runtime.FULL_SUITE_ADMISSION_V2_SCHEMA,
            "state": "CLAIMED", "attempt_number": attempt_number,
            "token": stored["token"],
            "claim": {"claim_path": str(claim_path),
                      "claim_sha256": hashlib.sha256(claim_path.read_bytes()).hexdigest()},
            "expected_receipt_paths": [str(path) for path in receipt_paths],
            "producer_lock": expected_lock}


def create_full_suite_claim(controller: Path, task_id: str, plan: dict[str, Any],
                            identity: dict[str, str], commands: list[dict[str, Any]],
                            routing: dict[str, Any], attempt_number: int) -> dict[str, Any]:
    root = full_suite_attempt_root(
        controller, task_id, plan["candidate"]["candidate_sha"], attempt_number)
    claim_path = root / "claim.json"
    lock_path = root / "producer.lock"
    receipt_paths = full_suite_receipt_paths(root, commands)
    if claim_path.exists() or any(path.exists() for path in receipt_paths):
        adopted = adopt_orphaned_full_suite_claim(
            controller, task_id, plan, identity, commands, routing, attempt_number)
        if adopted is not None:
            return adopted
        raise MergeQueueError("queue admission canonical path already exists")
    token = secrets.token_hex(24)
    producer_lock = {"path": str(lock_path), "kind": "flock"}
    claim = {"schema_version": risk_runtime.FULL_SUITE_CLAIM_V2_SCHEMA,
             "producer": {"schema_version": risk_runtime.FULL_SUITE_PRODUCER_SCHEMA,
                          "tool_id": risk_runtime.FULL_SUITE_TOOL_ID},
             "task_id": task_id,
             "candidate": {"candidate_sha": plan["candidate"]["candidate_sha"],
                           "candidate_tree": plan["candidate"]["candidate_tree"]},
             "policy_identity": plan["policy_identity"],
             "validation_identity": identity, "commands": commands,
             "routing": routing, "producer_lock": producer_lock,
             "token": token, "attempt_number": attempt_number,
             "expected_receipt_paths": [str(path) for path in receipt_paths]}
    write_canonical_exclusive(claim_path, claim,
                              plan["evidence_limits"]["max_receipt_bytes"])
    if any(path.exists() for path in receipt_paths):
        raise MergeQueueError("queue admission receipt path collided before suite execution")
    claim_ref = {"claim_path": str(claim_path),
                 "claim_sha256": hashlib.sha256(claim_path.read_bytes()).hexdigest()}
    return {"schema_version": risk_runtime.FULL_SUITE_ADMISSION_V2_SCHEMA,
            "state": "CLAIMED", "attempt_number": attempt_number, "token": token,
            "claim": claim_ref,
            "expected_receipt_paths": [str(path) for path in receipt_paths],
            "producer_lock": producer_lock}


def verify_queue_claimed_admission(controller: Path, task_id: str, plan: dict[str, Any],
                                    identity: dict[str, str], commands: list[dict[str, Any]],
                                    routing: dict[str, Any],
                                    admission: Any) -> dict[str, Any]:
    keys = {"schema_version", "state", "attempt_number", "token", "claim",
            "expected_receipt_paths", "producer_lock"}
    if (not isinstance(admission, dict) or set(admission) != keys
            or admission.get("schema_version") != risk_runtime.FULL_SUITE_ADMISSION_V2_SCHEMA
            or admission.get("state") != "CLAIMED"
            or not isinstance(admission.get("attempt_number"), int)
            or isinstance(admission.get("attempt_number"), bool)
            or admission["attempt_number"] <= 0
            or not isinstance(admission.get("token"), str)
            or len(admission.get("token")) != 48
            or not isinstance(admission.get("claim"), dict)
            or set(admission["claim"]) != {"claim_path", "claim_sha256"}
            or not isinstance(admission.get("expected_receipt_paths"), list)
            or len(admission["expected_receipt_paths"]) != len(commands)
            or any(not isinstance(path, str) for path in admission["expected_receipt_paths"])
            or not isinstance(admission.get("producer_lock"), dict)):
        raise MergeQueueError("stored CLAIMED full-suite admission is malformed")
    root = full_suite_attempt_root(
        controller, task_id, plan["candidate"]["candidate_sha"], admission["attempt_number"])
    expected_paths = [str(path) for path in full_suite_receipt_paths(root, commands)]
    expected_lock = {"path": str(root / "producer.lock"), "kind": "flock"}
    if (admission["claim"].get("claim_path") != str(root / "claim.json")
            or admission.get("expected_receipt_paths") != expected_paths
            or admission.get("producer_lock") != expected_lock):
        raise MergeQueueError("stored CLAIMED full-suite admission is not canonical")
    try:
        claim = risk_runtime._bounded_object(
            admission["claim"]["claim_path"], admission["claim"]["claim_sha256"],
            plan, "full-suite claim")
    except risk_runtime.RiskPolicyError as exc:
        raise MergeQueueError(f"stored CLAIMED full-suite admission refused: {exc}") from exc
    expected = {"schema_version": risk_runtime.FULL_SUITE_CLAIM_V2_SCHEMA,
                "producer": {"schema_version": risk_runtime.FULL_SUITE_PRODUCER_SCHEMA,
                             "tool_id": risk_runtime.FULL_SUITE_TOOL_ID},
                "task_id": task_id,
                "candidate": {"candidate_sha": plan["candidate"]["candidate_sha"],
                              "candidate_tree": plan["candidate"]["candidate_tree"]},
                "policy_identity": plan["policy_identity"],
                "validation_identity": identity, "commands": commands,
                "routing": routing, "producer_lock": expected_lock,
                "token": admission["token"], "attempt_number": admission["attempt_number"],
                "expected_receipt_paths": expected_paths}
    if claim != expected:
        raise MergeQueueError("stored CLAIMED full-suite claim identity drifted")
    return {**admission, "claim": {"claim_path": str(root / "claim.json"),
                                    "claim_sha256": admission["claim"]["claim_sha256"]},
            "expected_receipt_paths": expected_paths,
            "producer_lock": expected_lock}


def verify_queue_full_suite_admission(controller: Path, task_id: str, plan: dict[str, Any],
                                      identity: dict[str, str], commands: list[dict[str, Any]],
                                      routing: dict[str, Any],
                                      admission: Any) -> dict[str, Any]:
    if not isinstance(admission, dict) or admission.get("state") != "COMPLETE":
        raise MergeQueueError("full-suite admission is not complete")
    attempt_number = admission.get("attempt_number")
    if not isinstance(attempt_number, int) or isinstance(attempt_number, bool):
        raise MergeQueueError("full-suite admission attempt is invalid")
    root = full_suite_attempt_root(
        controller, task_id, plan["candidate"]["candidate_sha"], attempt_number)
    state_root = (controller / ".juno_task/state/merge-queue/full-suite").resolve()
    try:
        root.relative_to(state_root)
    except ValueError as exc:
        raise MergeQueueError("full-suite admission escaped controller state") from exc
    expected_paths = [str(path) for path in full_suite_receipt_paths(root, commands)]
    if (admission.get("claim", {}).get("claim_path") != str(root / "claim.json")
            or admission.get("receipts") is None
            or [row.get("receipt_path") if isinstance(row, dict) else None
                for row in admission["receipts"]] != expected_paths):
        raise MergeQueueError("full-suite admission is not at its canonical queue-owned path")
    try:
        return risk_runtime.verify_full_suite_admission_v2(
            admission, plan, identity, commands)
    except risk_runtime.RiskPolicyError as exc:
        raise MergeQueueError(f"full-suite admission refused: {exc}") from exc


def failed_full_suite_admission(controller: Path, task_id: str, plan: dict[str, Any],
                                identity: dict[str, str], commands: list[dict[str, Any]],
                                routing: dict[str, Any], claimed: dict[str, Any],
                                receipt_reference: dict[str, str]) -> dict[str, Any]:
    attempt_number = claimed.get("attempt_number")
    root = full_suite_attempt_root(
        controller, task_id, plan["candidate"]["candidate_sha"], attempt_number)
    expected_paths = [str(path) for path in full_suite_receipt_paths(root, commands)]
    receipt_path = receipt_reference.get("receipt_path")
    if (claimed.get("claim", {}).get("claim_path") != str(root / "claim.json")
            or claimed.get("expected_receipt_paths") != expected_paths
            or receipt_path not in expected_paths):
        raise MergeQueueError("failed full-suite admission is not at its canonical path")
    failure_index = expected_paths.index(receipt_path)
    receipts = [evidence_reference(Path(path)) for path in expected_paths[:failure_index + 1]]
    failure_receipt = json.loads(Path(receipt_path).read_text())
    result = failure_receipt.get("result") if isinstance(failure_receipt, dict) else None
    if (not isinstance(result, dict)
            or not isinstance(result.get("exit_code"), int)
            or not isinstance(result.get("timed_out"), bool)
            or not isinstance(result.get("stdout"), dict)
            or not isinstance(result.get("stderr"), dict)):
        raise MergeQueueError("failed full-suite terminal receipt is malformed")
    if not result["timed_out"] and result["exit_code"] == 0:
        raise MergeQueueError("successful full-suite receipt cannot enter FAILED admission")
    failure = {"command_id": commands[failure_index]["id"],
               "command_index": failure_index,
               "exit_code": result["exit_code"], "timed_out": result["timed_out"],
               "stdout_tail": result["stdout"]["tail"],
               "stderr_tail": result["stderr"]["tail"]}
    failed = {"schema_version": risk_runtime.FULL_SUITE_ADMISSION_V2_SCHEMA,
              "state": "FAILED", "attempt_number": attempt_number,
              "token": claimed.get("token"), "claim": claimed.get("claim"),
              "receipts": receipts, "failure": failure}
    try:
        risk_runtime.verify_full_suite_admission_v2(
            failed, plan, identity, commands, require_success=False)
    except risk_runtime.RiskPolicyError as exc:
        raise MergeQueueError(f"failed full-suite receipt refused: {exc}") from exc
    return failed


def verify_queue_failed_admission(controller: Path, task_id: str, plan: dict[str, Any],
                                   identity: dict[str, str], commands: list[dict[str, Any]],
                                   routing: dict[str, Any],
                                   admission: Any) -> dict[str, Any]:
    keys = {"schema_version", "state", "attempt_number", "token", "claim",
            "receipts", "failure"}
    if (not isinstance(admission, dict) or set(admission) != keys
            or admission.get("schema_version") != risk_runtime.FULL_SUITE_ADMISSION_V2_SCHEMA
            or admission.get("state") != "FAILED"):
        raise MergeQueueError("stored FAILED full-suite admission is malformed")
    attempt_number = admission.get("attempt_number")
    root = full_suite_attempt_root(
        controller, task_id, plan["candidate"]["candidate_sha"], attempt_number)
    try:
        historical_claim = risk_runtime._bounded_object(
            admission.get("claim", {}).get("claim_path"),
            admission.get("claim", {}).get("claim_sha256"), plan, "failed full-suite claim")
    except risk_runtime.RiskPolicyError as exc:
        raise MergeQueueError(f"stored FAILED full-suite claim refused: {exc}") from exc
    claimed = {"schema_version": risk_runtime.FULL_SUITE_ADMISSION_V2_SCHEMA,
               "state": "CLAIMED", "attempt_number": attempt_number,
               "token": admission.get("token"), "claim": admission.get("claim"),
               "expected_receipt_paths": historical_claim["expected_receipt_paths"],
               "producer_lock": historical_claim["producer_lock"]}
    verified_claimed = verify_queue_claimed_admission(
        controller, task_id, plan, historical_claim["validation_identity"],
        historical_claim["commands"], historical_claim["routing"], claimed)
    receipts = admission.get("receipts")
    terminal = receipts[-1] if isinstance(receipts, list) and receipts else None
    rebuilt = failed_full_suite_admission(
        controller, task_id, plan, historical_claim["validation_identity"],
        historical_claim["commands"], historical_claim["routing"],
        verified_claimed, terminal)
    if rebuilt != admission:
        raise MergeQueueError("stored FAILED full-suite admission projection drifted")
    return rebuilt


def recover_claimed_full_suite(controller: Path, task_id: str, plan: dict[str, Any],
                               identity: dict[str, str], commands: list[dict[str, Any]],
                               routing: dict[str, Any],
                               admission: Any) -> Optional[dict[str, Any]]:
    admission = verify_queue_claimed_admission(
        controller, task_id, plan, identity, commands, routing, admission)
    root = full_suite_attempt_root(
        controller, task_id, plan["candidate"]["candidate_sha"],
        admission["attempt_number"])
    receipt_paths = full_suite_receipt_paths(root, commands)
    written = [path for path in receipt_paths if path.exists()]
    if not written:
        return admission
    terminal_reference = evidence_reference(written[-1])
    if len(written) == len(receipt_paths):
        complete = {"schema_version": risk_runtime.FULL_SUITE_ADMISSION_V2_SCHEMA,
                    "state": "COMPLETE", "attempt_number": admission["attempt_number"],
                    "token": admission.get("token"), "claim": admission.get("claim"),
                    "receipts": [evidence_reference(path) for path in receipt_paths]}
        try:
            return verify_queue_full_suite_admission(
                controller, task_id, plan, identity, commands, routing, complete)
        except MergeQueueError as success_error:
            try:
                return failed_full_suite_admission(
                    controller, task_id, plan, identity, commands, routing,
                    admission, terminal_reference)
            except MergeQueueError:
                # Complete immutable receipts that neither verify as a COMPLETE
                # admission nor classify as FAILED supersede only when the
                # terminal receipt is well-formed and successful (e.g. poisoned
                # pre-fix provenance on green bytes): the receipts stay
                # untouched as evidence and merge_review admits a fresh attempt
                # under the next attempt number. Malformed or failing bytes keep
                # failing closed without a paid retry.
                if not _terminal_receipt_claims_success(written[-1]):
                    raise success_error
                return {"schema_version": risk_runtime.FULL_SUITE_ADMISSION_V2_SCHEMA,
                        "state": "UNVERIFIED",
                        "attempt_number": admission["attempt_number"],
                        "token": admission.get("token"),
                        "claim": admission.get("claim"),
                        "receipts": [evidence_reference(path)
                                     for path in receipt_paths],
                        "reason": str(success_error)}
    terminal_receipt = json.loads(written[-1].read_text())
    result = terminal_receipt["result"]
    if result["timed_out"] or result["exit_code"]:
        return failed_full_suite_admission(
            controller, task_id, plan, identity, commands, routing,
            admission, terminal_reference)
    # A successful partial prefix resumes under the same claim; the runner
    # strictly re-verifies each prefix receipt before continuing.
    return admission


def review_target_checkpoint(controller: Path, config: dict[str, Any], repository: Path,
                             task_id: str, candidate_sha: str,
                             expected_target_sha: str) -> Optional[dict[str, Any]]:
    """Briefly validate the review claim and stop spending tokens after target movement."""
    with target_lock(controller, repository, config["target_ref"]):
        with task_runtime.state_lock(controller):
            record = task_runtime.read_state(controller)["tasks"].get(task_id)
        if not isinstance(record, dict) or record.get("state") != "AWAITING_RISK":
            raise MergeQueueError("review claim state changed while semantic review was active")
        attempt = record.get("queue_attempt")
        if (not isinstance(attempt, dict) or attempt.get("candidate_sha") != candidate_sha
                or attempt.get("expected_target_sha") != expected_target_sha):
            raise MergeQueueError("review claim candidate identity changed")
        current = task_runtime.ref_sha(repository, config["target_ref"])
    if current != expected_target_sha:
        return requeue_stale_candidate(controller, config, repository, record, current)
    return None


def verify_standing_validation(record: dict[str, Any],
                               controller: Optional[Path] = None) -> dict[str, Any]:
    """Re-verify task-finish standing evidence before expensive queue work."""
    closure = record.get("review_ready_closure")
    standing = closure.get("standing_validation") if isinstance(closure, dict) else None
    if not isinstance(standing, dict):
        return {"status": "legacy_task_validation"}
    if (standing.get("schema_version") != task_runtime.STANDING_EVIDENCE_SCHEMA
            or standing.get("outcome") != "PASSED"
            or standing.get("tip_sha") != record.get("tip_sha")
            or not isinstance(standing.get("plan_sha256"), str)
            or not isinstance(standing.get("receipts"), list)):
        raise MergeQueueError("standing validation closure is malformed or failed")
    documentation_route = standing.get("documentation_route")
    if documentation_route is None:
        documentation_route = {"mode": "legacy_focused", "active_paths": []}
    counters = standing.get("counters")
    if counters is None:
        counters = {"executed": len(standing["receipts"]), "reused": 0,
                    "invalidated": 0, "skipped": 0, "not_applicable": 0}
    if not isinstance(documentation_route, dict) or not isinstance(counters, dict):
        raise MergeQueueError("standing validation route/counters are malformed")
    verified: list[dict[str, str]] = []
    summary_parent: Optional[Path] = None
    repository = (task_runtime.product_repository(controller, task_runtime.load_config(controller))
                  if controller is not None else None)
    for reference in standing["receipts"]:
        if (not isinstance(reference, dict) or set(reference) != {"path", "sha256", "command_id"}
                or not isinstance(reference["path"], str)):
            raise MergeQueueError("standing validation reference is malformed")
        path = Path(reference["path"])
        try:
            if path.stat().st_size > 1024 * 1024:
                raise MergeQueueError("standing validation receipt exceeds the byte bound")
            data = path.read_bytes(); receipt = json.loads(data)
        except (OSError, json.JSONDecodeError) as exc:
            raise MergeQueueError("standing validation receipt is unavailable") from exc
        if receipt.get("schema_version") == lifecycle_runtime.COMMAND_RESULT_SCHEMA:
            if repository is None:
                raise MergeQueueError("canonical standing result requires its repository authority")
            try:
                lifecycle_runtime.verify_canonical_command_result(
                    receipt, repository, receipt.get("input_closure"))
            except lifecycle_runtime.LifecycleContractError as exc:
                raise MergeQueueError(str(exc)) from exc
            legacy_identity_valid = True
        else:
            closure_verification = lifecycle_runtime.verify_complete_input_closure(
                receipt.get("input_closure"), receipt.get("input_closure"),
                receipt.get("complete_input_identity"))
            legacy_identity_valid = (closure_verification["valid"]
                and receipt.get("schema_version") in {
                    task_runtime.STANDING_EVIDENCE_SCHEMA,
                    task_runtime.CANONICAL_VALIDATION_RECEIPT_SCHEMA}
                and receipt.get("task_id") == record.get("task_id")
                and receipt.get("tip_sha") == standing["tip_sha"]
                and receipt.get("plan_sha256") == standing["plan_sha256"])
            summary_parent = path.parent if summary_parent is None else summary_parent
            if path.parent != summary_parent:
                raise MergeQueueError("legacy standing validation receipts do not share one plan root")
        if (hashlib.sha256(data).hexdigest() != reference["sha256"]
                or not legacy_identity_valid
                or receipt.get("command", {}).get("id") != reference["command_id"]
                or receipt.get("result", {}).get("exit_code") != 0
                or receipt.get("result", {}).get("timed_out")
                or receipt.get("result", {}).get("result_integrity", {}).get("eligible_pass") is False):
            raise MergeQueueError("standing validation receipt identity or verdict is invalid")
        verified.append({"command_id": reference["command_id"], "sha256": reference["sha256"]})
    if not verified:
        root = None
        route = documentation_route
        zero_route = (route.get("mode") == "inert_zero_command"
                      and route.get("authored_path_count", 0) >= 1)
        no_matching_command = (counters.get("executed") == 0
                               and counters.get("not_applicable", 0) >= 1)
        if not zero_route and not no_matching_command:
            raise MergeQueueError("receipt-free standing validation has no exact zero/not-applicable proof")
        if controller is None:
            raise MergeQueueError("zero-command standing proof requires its canonical controller root")
        summary_path = (controller / task_runtime.STANDING_ROOT / str(record.get("task_id"))
                        / standing["plan_sha256"] / "summary.json")
        try:
            summary = json.loads(summary_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise MergeQueueError("zero-command standing summary is unavailable") from exc
        if task_runtime.stable_sha256(summary) != standing.get("summary_sha256"):
            raise MergeQueueError("zero-command standing summary identity is invalid")
    else:
        summary_path = ((summary_parent / "summary.json") if summary_parent is not None else
                        (controller / task_runtime.STANDING_ROOT / str(record.get("task_id"))
                         / standing["plan_sha256"] / "summary.json"))
        try:
            summary = json.loads(summary_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise MergeQueueError("standing validation summary is unavailable") from exc
        if task_runtime.stable_sha256(summary) != standing.get("summary_sha256"):
            raise MergeQueueError("standing validation summary identity is invalid")
    if (summary.get("outcome") != "PASSED"
            or summary.get("plan_sha256") != standing["plan_sha256"]):
        raise MergeQueueError("standing validation summary identity is invalid")
    return {"status": "verified", "commands": verified,
            "plan_sha256": standing["plan_sha256"],
            "documentation_route": documentation_route,
            "counters": counters}


def authoritative_validation_rows(controller: Path, config: dict[str, Any],
                                  repository: Path, record: dict[str, Any],
                                  candidate: Path, candidate_sha: str,
                                  dependency_source: Optional[Path] = None) -> tuple[
                                      list[dict[str, Any]], dict[str, Any]]:
    """Consume exact finish evidence and execute only invalid command closures."""
    closure = record.get("review_ready_closure")
    standing = closure.get("standing_validation") if isinstance(closure, dict) else None
    if not isinstance(standing, dict):
        validations = validation_rows(
            config, candidate, dependency_source, record.get("changed_paths"))
        decisions = [{"schema_version": lifecycle_runtime.COMMAND_DECISION_SCHEMA,
                      "command_id": row.get("id"), "decision": "executed",
                      "input_closure_sha256": None, "source": None,
                      "invalidation": [], "reason": "legacy task has no command closure"}
                     for row in validations]
        return validations, {"decisions": decisions,
                             "counters": lifecycle_runtime.evidence_counters(decisions),
                             "active_wall_ms": sum(max(0, int(
                                 row.get("timing", {}).get("wall_duration_ms",
                                                           row.get("duration_ms", 0))))
                                                   for row in validations),
                             "source": "legacy_conservative_execution"}
    verify_merge_operation_snapshot(standing.get("operation_snapshot"))
    route = standing.get("documentation_route", {})
    changed = record.get("changed_paths") or []
    active_paths = route.get("active_paths", []) if route.get("mode") == "active_audit" else []
    coherence = lifecycle_runtime.grouped_coherence(
        controller, repository, candidate_sha, changed, active_doc_paths=active_paths,
        documentation_policy=config["documentation_validation"])
    if coherence["outcome"] != "PASSED":
        raise MergeQueueError("grouped coherence failed before validation/review: " + ", ".join(
            finding["code"] for finding in coherence["findings"]))
    if route.get("mode") == "inert_zero_command":
        exact = candidate_sha == standing.get("tip_sha")
        decision = lifecycle_runtime.evidence_decision(
            "documentation-zero-command", "reused" if exact else "invalidated",
            closure={"input_closure_sha256": route.get("route_sha256")},
            source={"plan_sha256": standing.get("plan_sha256")},
            invalidation=[] if exact else [{"field": "tip_sha", "old": standing.get("tip_sha"),
                                            "new": candidate_sha}],
            reason="exact zero-command proof" if exact else "candidate identity changed; recomputed structural proof")
        # An invalidated inert proof is recomputed without launching an executable command.
        decisions = [decision]
        if not exact:
            decisions.append(lifecycle_runtime.evidence_decision(
                "documentation-zero-command", "skipped",
                closure={"input_closure_sha256": route.get("route_sha256")},
                reason="recomputed inert zero-command proof"))
        return [], {"decisions": decisions,
                    "counters": lifecycle_runtime.evidence_counters(decisions),
                    "active_wall_ms": 0,
                    "coherence": coherence, "source": "standing_zero_command"}
    rows = ([lifecycle_runtime.active_documentation_row()]
            if route.get("mode") == "active_audit"
            else task_runtime.selected_focused_rows(config, changed))
    source_receipts: dict[str, tuple[dict[str, Any], dict[str, str]]] = {}
    for reference in standing.get("receipts", []):
        try:
            path = Path(reference["path"]); raw = path.read_bytes(); receipt = json.loads(raw)
        except (OSError, KeyError, json.JSONDecodeError) as exc:
            raise MergeQueueError("standing validation receipt is unavailable") from exc
        if hashlib.sha256(raw).hexdigest() != reference.get("sha256"):
            raise MergeQueueError("standing validation receipt digest drifted")
        source_receipts[str(reference.get("command_id"))] = (receipt, reference)
    runtime = task_runtime.runtime_generation(
        repository, task_runtime.ref_sha(repository, config["target_ref"]))
    validations: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    active_wall_ms = 0
    for row in rows:
        current = task_runtime._command_input_closure(
            repository, candidate_sha, row, config, runtime)
        source = source_receipts.get(row["id"])
        if source is not None:
            receipt, reference = source
            closure_verification = lifecycle_runtime.verify_complete_input_closure(
                receipt.get("input_closure"), current,
                receipt.get("complete_input_identity"))
            invalidation = closure_verification["reasons"]
            result = receipt.get("result")
            if (closure_verification["valid"] and isinstance(result, dict)
                    and result.get("exit_code") == 0 and not result.get("timed_out")
                    and result.get("result_integrity", {}).get("eligible_pass") is not False):
                try:
                    terminal = lifecycle_runtime.consume_or_execute_command_result(
                        controller / CANONICAL_VALIDATION_ROOT, repository, current,
                        lambda: result, phase="merge_validation",
                        task_id=str(record.get("task_id")),
                        legacy=((receipt, reference)
                                if receipt.get("schema_version") != lifecycle_runtime.COMMAND_RESULT_SCHEMA
                                else None))
                except lifecycle_runtime.LifecycleContractError as exc:
                    raise MergeQueueError(str(exc)) from exc
                validations.append(terminal["receipt"]["result"])
                decisions.append(lifecycle_runtime.evidence_decision(
                    row["id"], "reused", closure=current,
                    source=terminal["reference"],
                    reason="exact canonical command result reuse"))
                continue
            decisions.append(lifecycle_runtime.evidence_decision(
                row["id"], "invalidated", closure=current, source=reference,
                invalidation=invalidation or [{"field": "result_integrity",
                                                "reason": "source is not reusable PASS"}]))
        cwd = (candidate / row["cwd"]).resolve()
        try:
            cwd.relative_to(candidate.resolve())
        except ValueError as exc:
            raise MergeQueueError("authoritative validation cwd escaped candidate") from exc
        def execute_terminal() -> dict[str, Any]:
            if row["argv"] == lifecycle_runtime.ACTIVE_DOC_ARGV:
                return task_runtime._active_documentation_validation(
                    repository, candidate_sha,
                    {"documentation_route": route}, row,
                    config["documentation_validation"])
            with validation_dependencies(candidate, cwd, dependency_source):
                return task_runtime.run_validation(row, cwd)
        try:
            terminal = lifecycle_runtime.consume_or_execute_command_result(
                controller / CANONICAL_VALIDATION_ROOT, repository, current,
                execute_terminal, phase="merge_validation",
                task_id=str(record.get("task_id")))
        except lifecycle_runtime.LifecycleContractError as exc:
            raise MergeQueueError(str(exc)) from exc
        result = terminal["receipt"]["result"]
        validations.append(result)
        if terminal["decision"] == "executed":
            active_wall_ms += max(0, int(
                result.get("timing", {}).get("wall_duration_ms", result.get("duration_ms", 0))))
        decisions.append(lifecycle_runtime.evidence_decision(
            row["id"], terminal["decision"], closure=current,
            source=terminal["reference"],
            reason="canonical command result index"))
        if result.get("timed_out") or result.get("exit_code"):
            detail = result.get("stderr_tail") or result.get("stdout_tail")
            raise MergeValidationError(
                f"affected validation failed ({row['id']}): {detail}", validations)
    return validations, {"decisions": decisions,
                         "counters": lifecycle_runtime.evidence_counters(decisions),
                         "active_wall_ms": active_wall_ms,
                         "replay_trace": lifecycle_runtime.evidence_replay_trace(
                             decisions, phase="merge_validation"),
                         "coherence": coherence, "source": "authoritative_cross_stage"}


def _overlapped_review_and_suite(
        controller: Path, task_id: str, config: dict[str, Any], repository: Path,
        record: dict[str, Any], attempt: dict[str, Any], stored: dict[str, Any],
        progress: dict[str, Any], policy: dict[str, Any], request: dict[str, str],
        plan: dict[str, Any], candidate_root: Path, candidate_sha: str,
        expected: str, claimed: dict[str, Any], validation_identity: dict[str, str],
        suite_commands: list[dict[str, Any]], suite_routing: dict[str, Any],
        review_attempt_number: int) -> dict[str, Any]:
    """Fresh-claim path: overlap A/suite while retaining durable A-before-B."""
    references: list[dict[str, str]] = []
    predecessor: Optional[Path] = None
    mutable = {"attempt": attempt, "stored": stored, "progress": progress}

    def reviewer_callback(sequence: int, reviewer: str) -> Any:
        def run_reviewer() -> dict[str, Any]:
            nonlocal predecessor
            authority = compile_live_authority_snapshot(
                controller, config, repository, task_id)
            require_live_authority(controller, config, repository, task_id, authority,
                                   boundary="before_review_dispatch")
            reference = dispatch_reviewer(
                controller, candidate_root, plan, task_id, reviewer, sequence,
                predecessor, review_attempt_number)
            references.append(reference)
            predecessor = Path(reference["runner_receipt_path"])
            assert_frozen_candidate(controller, config, candidate_root, candidate_sha)
            compact = risk_runtime._compact_review(
                reference, reviewer, sequence, candidate_sha,
                plan["policy_identity"], plan)
            compact["reference"] = reference
            if not compact["blocking_count"]:
                step = {"sequence": sequence, "reviewer": reviewer,
                        "reference": reference,
                        "verified": {key: value for key, value in compact.items()
                                     if key != "reference"}}
                current_progress = {**mutable["progress"],
                                    "steps": [*mutable["progress"]["steps"], step]}
                current_stored = {**mutable["stored"],
                                  "review_progress": current_progress}
                current_attempt = {**mutable["attempt"], "risk": current_stored,
                                   "review": current_stored,
                                   "outcome": "REVIEWING_OVERLAPPED"}
                # Reviewer A PASS is durable before Reviewer B dispatch begins.
                persist_attempt(controller, current_attempt, state_name="AWAITING_RISK")
                mutable.update(attempt=current_attempt, stored=current_stored,
                               progress=current_progress)
            return compact
        return run_reviewer

    callbacks = [reviewer_callback(index + 1, reviewer)
                 for index, reviewer in enumerate(plan["reviewer_sequence"])]
    claim_binding = {"claim_path": claimed["claim"]["claim_path"],
                     "claim_sha256": claimed["claim"]["claim_sha256"],
                     "token": claimed["token"],
                     "attempt_number": claimed["attempt_number"]}
    receipt_paths = [Path(path) for path in claimed["expected_receipt_paths"]]

    def suite(cancel_event: Any) -> Any:
        with full_suite_producer_lock(
                controller, task_id, candidate_sha, claimed["attempt_number"]):
            return full_suite_validation(
                suite_commands, candidate_root, plan, validation_identity,
                receipt_paths, claim_binding,
                task_runtime.exact_root(Path(record["worktree"]),
                                        "review feature worktree"),
                controller=controller, repository=repository,
                cancel_event=cancel_event)

    overlap = lifecycle_runtime.run_overlap(suite, callbacks)
    compact_reviews = [{key: value for key, value in row.items()
                        if key != "reference"} for row in overlap["reviews"]]
    blocking = next((row for row in overlap["reviews"]
                     if row.get("blocking_count")), None)
    if blocking is not None:
        cancellation_body = {
            "schema_version": "juno_review_suite_cancellation.v1",
            "task_id": task_id, "candidate_sha": candidate_sha,
            "policy_identity": plan["policy_identity"],
            "reason": "blocking_reviewer_a" if overlap["reviews"].index(blocking) == 0
                      else "blocking_selected_reviewer",
            "events": overlap["events"],
            "cancellation_requested": overlap["cancelled"],
            "suite_cancelled": overlap["cancelled"] and overlap["suite_error"] is not None,
            "suite_terminal": ("stopped" if overlap["suite_error"] is not None else "completed"),
            "suite_error": (str(overlap["suite_error"])[:512]
                            if overlap["suite_error"] is not None else None),
            "review_receipts": references,
            "written_suite_receipts": [evidence_reference(path)
                                       for path in receipt_paths if path.exists()],
        }
        cancellation_path = (controller / ".juno_task/state/merge-queue/cancellations"
                             / task_id / candidate_sha
                             / f"attempt-{claimed['attempt_number']}.json")
        cancellation = lifecycle_runtime.atomic_json(
            cancellation_path, cancellation_body, exclusive=True)
        advisories = delivery_advisories(compact_reviews)
        review_round = record.get("review_round", 1)
        outcome = ("REVIEW_FINDINGS_EXHAUSTED" if review_round >= 2
                   else "REVIEW_FINDINGS")
        risk_state = {**mutable["stored"], "status": outcome,
                      "suite_cancellation": cancellation,
                      "blocking_review": blocking["reference"]}
        updated = {**mutable["attempt"], "risk": risk_state,
                   "review": risk_state, "outcome": outcome,
                   "review_suite_overlap": {"events": overlap["events"],
                                            "elapsed_ms": overlap["elapsed_ms"]},
                   "blocking_findings": compact_reviews,
                   "delivery_advisories": advisories}
        persist_attempt(controller, updated, state_name=outcome)
        return updated
    if overlap["suite_error"] is not None:
        exc = overlap["suite_error"]
        failed = {**mutable["attempt"], "outcome": "FAILED_FULL_SUITE",
                  "review_suite_overlap": {"events": overlap["events"],
                                           "elapsed_ms": overlap["elapsed_ms"]}}
        persist_attempt(controller, failed, state_name="AWAITING_RISK")
        if isinstance(exc, BaseException):
            raise exc
        raise MergeQueueError("overlapped full suite failed")
    suite_references, reuse = overlap["suite_result"]
    complete = {"schema_version": risk_runtime.FULL_SUITE_ADMISSION_V2_SCHEMA,
                "state": "COMPLETE", "attempt_number": claimed["attempt_number"],
                "token": claimed["token"], "claim": claimed["claim"],
                "receipts": suite_references}
    suite_admission = verify_queue_full_suite_admission(
        controller, task_id, plan, validation_identity,
        suite_commands, suite_routing, complete)
    current_progress = {**mutable["progress"],
                        "full_suite_admission": suite_admission}
    current_stored = {**mutable["stored"], "review_progress": current_progress}
    current_attempt = {**mutable["attempt"], "risk": current_stored,
                       "review": current_stored,
                       "review_suite_overlap": {"events": overlap["events"],
                                                "elapsed_ms": overlap["elapsed_ms"]}}
    if reuse:
        current_attempt["evidence_reuse"] = reuse
    persist_attempt(controller, current_attempt, state_name="AWAITING_RISK")
    stale = review_target_checkpoint(
        controller, config, repository, task_id, candidate_sha, expected)
    if stale is not None:
        return stale
    receipt = risk_runtime.finalize(
        plan, request, affected_tests_passed=True,
        full_suite_admission=suite_admission, reviews=references,
        metrics={"model_calls": len(references), "affected_test_runs": 1,
                 "full_suite_runs": 1}, policy=policy)
    evidence_file = evidence_path(controller, task_id, candidate_sha,
                                  review_attempt_number)
    if evidence_file.exists():
        raise MergeQueueError("review evidence attempt path already exists")
    risk_runtime.atomic_receipt(evidence_file, receipt, policy)
    final_authority = compile_live_authority_snapshot(
        controller, config, repository, task_id)
    final_snapshot = semantic_snapshot(
        controller, record, plan, final_authority,
        commands=len(references) + (1 if plan["full_suite_required"] else 0),
        wall_ms=int(overlap.get("elapsed_ms", 0)))
    write_semantic_sidecar(evidence_file, final_snapshot)
    reference = evidence_reference(evidence_file)
    verified = risk_runtime.verify_candidate_evidence(
        policy, request, risk_flags(record), reference)
    advisories = delivery_advisories(compact_reviews)
    outcome = "RISK_EVIDENCE_READY" if verified["eligible"] else "REVIEW_FINDINGS"
    risk_state = {**current_stored, "status": outcome, "evidence": reference}
    updated = {**current_attempt, "risk": risk_state, "review": risk_state,
               "outcome": outcome, "delivery_advisories": advisories}
    persist_attempt(controller, updated,
                    state_name="AWAITING_RISK" if verified["eligible"] else outcome)
    return updated


def merge_review(controller: Path, task_id: str, *, overlap_suite: bool = False) -> dict[str, Any]:
    if not task_runtime.TASK_RE.fullmatch(task_id):
        raise MergeQueueError("unsafe task id")
    config = task_runtime.load_config(controller)
    repository = task_runtime.product_repository(controller, config)
    with review_lock(repository, task_id):
        with task_runtime.state_lock(controller):
            record = task_runtime.read_state(controller)["tasks"].get(task_id)
        if (isinstance(record, dict)
                and record.get("last_queue_outcome") == "FAILED_FULL_SUITE"
                and _record_has_deterministic_router_finding(record)):
            raise MergeQueueError(
                "unchanged deterministic full-suite failure cannot be rerun; use the "
                "typed safe_next_command from yy merge status")
        if not isinstance(record, dict) or record.get("state") not in {
                "AWAITING_RISK", "REQUEUING_STALE"}:
            raise MergeQueueError("task has no frozen candidate awaiting risk evidence")
        if record.get("state") == "REQUEUING_STALE":
            return requeue_stale_candidate(
                controller, config, repository, record,
                task_runtime.ref_sha(repository, config["target_ref"]))
        attempt = record.get("queue_attempt")
        if not isinstance(attempt, dict):
            raise MergeQueueError("awaiting task has no queue attempt")
        candidate_sha, expected = attempt.get("candidate_sha"), attempt.get("expected_target_sha")
        if task_runtime.ref_sha(repository, config["target_ref"]) != expected:
            return requeue_stale_candidate(
                controller, config, repository, record,
                task_runtime.ref_sha(repository, config["target_ref"]))
        checkout_value = attempt.get("candidate_checkout")
        candidate_root = (task_runtime.exact_root(Path(checkout_value), "review candidate")
                          if checkout_value else validate_record(config, repository, record))
        assert_frozen_candidate(controller, config, candidate_root, candidate_sha)
        standing_validation = verify_standing_validation(record, controller)
        claimed: Optional[dict[str, Any]] = None
        try:
            policy = risk_runtime.load_policy(risk_policy_path(controller))
            request = risk_request(repository, candidate_sha, config["target_ref"], expected)
            plan = risk_runtime.classify(policy, request, risk_flags(record))
            stored = attempt.get("risk")
            if (not isinstance(stored, dict) or stored.get("candidate_sha") != candidate_sha
                    or stored.get("policy_identity") != plan["policy_identity"]
                    or stored.get("plan") != plan):
                raise MergeQueueError("stored awaiting-risk plan does not match fresh Git policy")
            progress = stored.get("review_progress")
            if progress is None:
                prior_admission = None
                semantic_decision = stored.get("semantic_reuse_decision")
                prior_reference = stored.get("semantic_previous_evidence")
                if (isinstance(semantic_decision, dict)
                        and semantic_decision.get("restart_phase") == "review"
                        and isinstance(prior_reference, dict)):
                    try:
                        prior_receipt = json.loads(Path(prior_reference["receipt_path"]).read_text())
                        prior_admission = prior_receipt.get("validation", {}).get(
                            "full_suite_admission")
                    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
                        raise MergeQueueError(
                            "malformed_evidence: prior phase-local admission is unavailable") from exc
                progress = {"schema_version": "juno_merge_queue_review_progress.v4",
                            "attempt_counter": 0, "review_attempt_counter": 0,
                            "collision_floor": 0,
                            "full_suite_admission": prior_admission,
                            "steps": []}
            if isinstance(progress, dict) and progress.get("schema_version") in {
                    "juno_merge_queue_review_progress.v2", "juno_merge_queue_review_progress.v3"}:
                progress = {"schema_version": "juno_merge_queue_review_progress.v4",
                            "attempt_counter": progress.get("attempt_counter"),
                            "review_attempt_counter": progress.get("attempt_counter"),
                            "collision_floor": 0,
                            "full_suite_admission": None, "steps": progress.get("steps")}
            allowed_progress = {"schema_version", "attempt_counter", "review_attempt_counter",
                                "collision_floor",
                                "full_suite_admission", "steps",
                                "full_validation_passed", "validation_identity"}
            if (not isinstance(progress, dict) or not set(progress).issubset(allowed_progress)
                    or not {"schema_version", "attempt_counter", "review_attempt_counter",
                            "collision_floor", "full_suite_admission", "steps"}.issubset(progress)
                    or progress.get("schema_version") != "juno_merge_queue_review_progress.v4"
                    or not isinstance(progress.get("attempt_counter"), int)
                    or isinstance(progress.get("attempt_counter"), bool)
                    or not 0 <= progress["attempt_counter"] <= 10000
                    or not isinstance(progress.get("review_attempt_counter"), int)
                    or isinstance(progress.get("review_attempt_counter"), bool)
                    or not 0 <= progress["review_attempt_counter"] <= 10000
                    or not isinstance(progress.get("collision_floor"), int)
                    or isinstance(progress.get("collision_floor"), bool)
                    or not 0 <= progress["collision_floor"] <= 10000
                    or (progress.get("full_suite_admission") is not None
                        and not isinstance(progress.get("full_suite_admission"), dict))
                    or not isinstance(progress.get("steps"), list)):
                raise MergeQueueError("stored reviewer continuation is malformed")
            # Deprecated booleans/projections are explicitly non-authoritative.
            progress = {key: progress[key] for key in
                        ("schema_version", "attempt_counter", "review_attempt_counter",
                         "collision_floor", "full_suite_admission", "steps")}
            current_validation_identity = full_validation_identity(
                controller, config, record, candidate_root, candidate_sha)
            suite_commands, suite_routing = full_suite_selection(
                config, plan["candidate"]["changed_paths"])
            legacy_command = full_suite_command(config)
            stored = {**stored, "review_progress": progress}
            attempt = {**attempt, "risk": stored, "review": stored,
                       "standing_validation": standing_validation}
            suite_admission = None
            prior_attempt = max(progress["attempt_counter"], progress["collision_floor"])
            existing_admission = progress["full_suite_admission"]
            admission_schema = (existing_admission.get("schema_version")
                                if isinstance(existing_admission, dict) else None)
            legacy_admission = admission_schema == risk_runtime.FULL_SUITE_ADMISSION_SCHEMA
            if existing_admission is not None:
                admission_state = existing_admission.get("state")
                if (not isinstance(admission_state, str)
                        or admission_state not in {"COMPLETE", "FAILED", "CLAIMED"}
                        or admission_schema not in {
                            risk_runtime.FULL_SUITE_ADMISSION_SCHEMA,
                            risk_runtime.FULL_SUITE_ADMISSION_V2_SCHEMA}):
                    raise AdmissionStateError(
                        "stored full-suite admission state is malformed or unsupported")
            if plan["full_suite_required"] and existing_admission is not None:
                if admission_state == "COMPLETE":
                    try:
                        suite_admission = (
                            verify_queue_full_suite_admission_legacy(
                                controller, task_id, plan, current_validation_identity,
                                legacy_command, existing_admission)
                            if legacy_admission else
                            verify_queue_full_suite_admission(
                                controller, task_id, plan, current_validation_identity,
                                suite_commands, suite_routing, existing_admission))
                    except MergeQueueError:
                        suite_admission = None
                elif admission_state == "FAILED":
                    verified_failed = (
                        verify_queue_failed_admission_legacy(
                            controller, task_id, plan, current_validation_identity,
                            legacy_command, existing_admission)
                        if legacy_admission else
                        verify_queue_failed_admission(
                            controller, task_id, plan, current_validation_identity,
                            suite_commands, suite_routing, existing_admission))
                    prior_attempt = max(prior_attempt, verified_failed["attempt_number"])
                    progress = {**progress, "full_suite_admission": verified_failed}
                    stored = {**stored, "review_progress": progress}
                    attempt = {**attempt, "risk": stored, "review": stored}
                elif admission_state == "CLAIMED":
                    recovered = (
                        recover_claimed_full_suite_legacy(
                            controller, task_id, plan, current_validation_identity,
                            legacy_command, existing_admission)
                        if legacy_admission else
                        recover_claimed_full_suite(
                            controller, task_id, plan, current_validation_identity,
                            suite_commands, suite_routing, existing_admission))
                    prior_attempt = max(prior_attempt, recovered["attempt_number"])
                    if recovered["state"] == "COMPLETE":
                        suite_admission = recovered
                        progress = {**progress, "full_suite_admission": recovered}
                        stored = {**stored, "review_progress": progress}
                        attempt = {**attempt, "risk": stored, "review": stored}
                        persist_attempt(controller, attempt, state_name="AWAITING_RISK")
                    elif recovered["state"] == "FAILED":
                        progress = {**progress, "full_suite_admission": recovered,
                                    "attempt_counter": prior_attempt}
                        stored = {**stored, "review_progress": progress}
                        attempt = {**attempt, "risk": stored, "review": stored}
                        persist_attempt(controller, attempt, state_name="AWAITING_RISK")
                        failure = recovered["failure"]
                        detail = failure["stderr_tail"] or failure["stdout_tail"]
                        terminal = (recovered["receipt"] if legacy_admission
                                    else recovered["receipts"][-1])
                        raise MergeValidationError(
                            f"recovered full-suite attempt failed: {detail}", [failure],
                            terminal)
                    elif recovered["state"] == "UNVERIFIED":
                        # Complete receipts whose admission cannot be verified and
                        # cannot classify as FAILED supersede: consume the
                        # attempt number durably, keep the poisoned receipts as
                        # immutable evidence, and fall through to a fresh claim.
                        progress = {**progress, "attempt_counter": prior_attempt}
                        stored = {**stored, "review_progress": progress}
                        attempt = {**attempt, "risk": stored, "review": stored}
                        persist_attempt(controller, attempt, state_name="AWAITING_RISK")
                    else:
                        claimed = recovered
            if plan["full_suite_required"] and suite_admission is None and claimed is None:
                if prior_attempt >= 10000:
                    raise MergeQueueError("bounded full-suite attempt namespace is exhausted")
                suite_attempt_number = prior_attempt + 1
                with full_suite_producer_lock(
                        controller, task_id, plan["candidate"]["candidate_sha"],
                        suite_attempt_number):
                    claimed, attempt = persist_full_suite_claim(
                        controller, attempt, suite_attempt_number,
                        lambda: create_full_suite_claim(
                            controller, task_id, plan, current_validation_identity,
                            suite_commands, suite_routing, suite_attempt_number))
                stored = attempt["risk"]
                progress = stored["review_progress"]
            if (overlap_suite and plan["full_suite_required"]
                    and suite_admission is None and claimed is not None
                    and not progress["steps"] and plan["reviewer_sequence"]):
                if progress["review_attempt_counter"] >= 10000:
                    raise MergeQueueError("bounded reviewer attempt namespace is exhausted")
                review_attempt_number = progress["review_attempt_counter"] + 1
                progress = {**progress,
                            "review_attempt_counter": review_attempt_number}
                stored = {**stored, "review_progress": progress}
                attempt = {**attempt, "risk": stored, "review": stored,
                           "outcome": "REVIEWING_OVERLAPPED"}
                persist_attempt(controller, attempt, state_name="AWAITING_RISK")
                return _overlapped_review_and_suite(
                    controller, task_id, config, repository, record, attempt,
                    stored, progress, policy, request, plan, candidate_root,
                    candidate_sha, expected, claimed, current_validation_identity,
                    suite_commands, suite_routing, review_attempt_number)
            if plan["full_suite_required"] and suite_admission is None:
                claim_binding = {"claim_path": claimed["claim"]["claim_path"],
                                 "claim_sha256": claimed["claim"]["claim_sha256"],
                                 "token": claimed["token"],
                                 "attempt_number": claimed["attempt_number"]}
                receipt_paths = [Path(path) for path in claimed["expected_receipt_paths"]]
                with full_suite_producer_lock(
                        controller, task_id, plan["candidate"]["candidate_sha"],
                        claimed["attempt_number"]):
                    suite_references, evidence_reuse = full_suite_validation(
                        suite_commands, candidate_root, plan, current_validation_identity,
                        receipt_paths, claim_binding,
                        task_runtime.exact_root(
                            Path(record["worktree"]), "review feature worktree"),
                        controller=controller, repository=repository)
                attempt["evidence_reuse"] = evidence_reuse
                attempt["evidence_replay_trace"] = lifecycle_runtime.evidence_replay_trace(
                    evidence_reuse, phase="queue_full_suite")
                after_validation_identity = full_validation_identity(
                    controller, task_runtime.load_config(controller), record,
                    candidate_root, candidate_sha)
                if after_validation_identity != current_validation_identity:
                    raise MergeQueueError("validation identity changed during full validation")
                complete = {"schema_version": risk_runtime.FULL_SUITE_ADMISSION_V2_SCHEMA,
                            "state": "COMPLETE", "attempt_number": claimed["attempt_number"],
                            "token": claimed["token"], "claim": claimed["claim"],
                            "receipts": suite_references}
                suite_admission = verify_queue_full_suite_admission(
                    controller, task_id, plan, current_validation_identity,
                    suite_commands, suite_routing, complete)
                progress = {**progress, "full_suite_admission": suite_admission}
                stored = {**stored, "review_progress": progress}
                attempt = {**attempt, "risk": stored, "review": stored}
                persist_attempt(controller, attempt, state_name="AWAITING_RISK")
            stale = review_target_checkpoint(
                controller, config, repository, task_id, candidate_sha, expected)
            if stale is not None:
                return stale
            if progress["review_attempt_counter"] >= 10000:
                raise MergeQueueError("bounded reviewer attempt namespace is exhausted")
            review_attempt_number = progress["review_attempt_counter"] + 1
            progress = {**progress, "review_attempt_counter": review_attempt_number}
            stored = {**stored, "review_progress": progress}
            attempt = {**attempt, "risk": stored, "review": stored, "outcome": "REVIEWING"}
            persist_attempt(controller, attempt, state_name="AWAITING_RISK")
            reviews: list[dict[str, str]] = []
            predecessor: Optional[Path] = None
            steps = progress["steps"]
            if len(steps) > len(plan["reviewer_sequence"]):
                raise MergeQueueError("stored reviewer continuation exceeds the policy sequence")
            for index, step in enumerate(steps):
                reviewer = plan["reviewer_sequence"][index]
                sequence = index + 1
                if (not isinstance(step, dict) or set(step) != {
                        "sequence", "reviewer", "reference", "verified"}
                        or step.get("sequence") != sequence or step.get("reviewer") != reviewer):
                    raise MergeQueueError("stored reviewer continuation order is invalid")
                compact = risk_runtime._compact_review(
                    step.get("reference"), reviewer, sequence, candidate_sha,
                    plan["policy_identity"], plan,
                )
                if compact != step.get("verified") or compact["blocking_count"]:
                    raise MergeQueueError("stored reviewer continuation evidence is no longer valid")
                reviews.append(step["reference"])
                predecessor = Path(step["reference"]["runner_receipt_path"])
            for sequence, reviewer in enumerate(
                    plan["reviewer_sequence"][len(reviews):], len(reviews) + 1):
                authority = compile_live_authority_snapshot(
                    controller, config, repository, task_id)
                require_live_authority(
                    controller, config, repository, task_id, authority,
                    boundary="before_review_dispatch")
                reference = dispatch_reviewer(
                    controller, candidate_root, plan, task_id, reviewer,
                    sequence, predecessor, review_attempt_number,
                )
                reviews.append(reference)
                predecessor = Path(reference["runner_receipt_path"])
                assert_frozen_candidate(controller, config, candidate_root, candidate_sha)
                compact = risk_runtime._compact_review(  # canonical verifier; no receipt shortcut
                    reference, reviewer, sequence, candidate_sha,
                    plan["policy_identity"], plan,
                )
                if compact["blocking_count"]:
                    break
                step = {"sequence": sequence, "reviewer": reviewer,
                        "reference": reference, "verified": compact}
                progress = {**progress, "steps": [*progress["steps"], step]}
                stored = {**stored, "review_progress": progress}
                attempt = {**attempt, "risk": stored, "review": stored}
                # A PASS is durable before Reviewer B starts. A transport
                # failure therefore retries only the missing suffix.
                persist_attempt(controller, attempt, state_name="AWAITING_RISK")
                stale = review_target_checkpoint(
                    controller, config, repository, task_id, candidate_sha, expected)
                if stale is not None:
                    return stale
            with target_lock(controller, repository, config["target_ref"]):
                current_target = task_runtime.ref_sha(repository, config["target_ref"])
                if current_target != expected:
                    with task_runtime.state_lock(controller):
                        current_record = task_runtime.read_state(controller)["tasks"].get(task_id)
                    if not isinstance(current_record, dict):
                        raise MergeQueueError("review claim task disappeared before stale cleanup")
                    return requeue_stale_candidate(
                        controller, config, repository, current_record, current_target)
            receipt = risk_runtime.finalize(
                plan, request, affected_tests_passed=True,
                full_suite_admission=suite_admission,
                reviews=reviews,
                metrics={"model_calls": len(reviews), "affected_test_runs": 1,
                         "full_suite_runs": 1 if plan["full_suite_required"] else 0},
                policy=policy,
            )
            path = evidence_path(controller, task_id, candidate_sha, review_attempt_number)
            if path.exists():
                raise MergeQueueError("review evidence attempt path already exists")
            risk_runtime.atomic_receipt(path, receipt, policy)
            final_authority = compile_live_authority_snapshot(
                controller, config, repository, task_id)
            final_snapshot = semantic_snapshot(
                controller, record, plan, final_authority,
                commands=len(reviews) + (1 if plan["full_suite_required"] else 0))
            write_semantic_sidecar(path, final_snapshot)
            reference = evidence_reference(path)
            verified = risk_runtime.verify_candidate_evidence(
                policy, request, risk_flags(record), reference)
            compact_reviews = [risk_runtime._compact_review(
                review, plan["reviewer_sequence"][index], index + 1, candidate_sha,
                plan["policy_identity"], plan) for index, review in enumerate(reviews)]
            advisories = delivery_advisories(compact_reviews)
            attempt = {**attempt, "delivery_advisories": advisories}
        except MergeValidationError as exc:
            if claimed is not None and exc.receipt_reference is not None:
                failed_admission = (
                    failed_full_suite_admission_legacy(
                        controller, task_id, plan, current_validation_identity,
                        full_suite_command(config), claimed, exc.receipt_reference)
                    if legacy_admission else
                    failed_full_suite_admission(
                        controller, task_id, plan, current_validation_identity,
                        suite_commands, suite_routing, claimed, exc.receipt_reference))
                progress = {**progress, "full_suite_admission": failed_admission}
                stored = {**stored, "review_progress": progress}
                attempt = {**attempt, "risk": stored, "review": stored}
            # Full-suite failure truth lives in the immutable admission receipt.
            # Keep the separately admitted affected-validation rows intact so a
            # later successful retry cannot render superseded failure evidence
            # as the current reviewer input.
            failed = {**attempt, "outcome": "FAILED_FULL_SUITE"}
            persist_attempt(controller, failed, state_name="AWAITING_RISK")
            raise
        except (AdmissionStateError, AuthorityDriftError):
            raise
        except (risk_runtime.RiskPolicyError, MergeQueueError) as exc:
            if "queue admission canonical path already exists" in str(exc) \
                    or "queue admission receipt path collided" in str(exc):
                raise MergeQueueError(str(exc)) from exc
            failed = {**attempt, "outcome": "REVIEW_FAILED", "risk_failure": str(exc)[:512]}
            persist_attempt(controller, failed, state_name="AWAITING_RISK")
            raise MergeQueueError(str(exc)) from exc
        review_round = record.get("review_round", 1)
        if (not isinstance(review_round, int) or isinstance(review_round, bool)
                or review_round not in {1, 2}):
            raise MergeQueueError("task review round is malformed or exceeds the bounded policy")
        outcome = ("RISK_EVIDENCE_READY" if verified["eligible"]
                   else ("REVIEW_FINDINGS_EXHAUSTED" if review_round >= 2
                         else "REVIEW_FINDINGS"))
        risk_state = {**stored, "status": outcome, "evidence": reference}
        updated = {key: value for key, value in attempt.items() if key != "risk_failure"}
        updated.update({"risk": risk_state, "review": risk_state, "outcome": outcome})
        with target_lock(controller, repository, config["target_ref"]):
            with task_runtime.state_lock(controller):
                current_record = task_runtime.read_state(controller)["tasks"].get(task_id)
            current_target = task_runtime.ref_sha(repository, config["target_ref"])
            if current_target != expected:
                if not isinstance(current_record, dict):
                    raise MergeQueueError("review claim task disappeared before stale cleanup")
                return requeue_stale_candidate(
                    controller, config, repository, current_record, current_target)
            persist_attempt(controller, updated, state_name=(
                "AWAITING_RISK" if verified["eligible"] else outcome))
        return updated


def _reconciliation_receipt_root(controller: Path) -> Path:
    return (controller / ".juno_task/runtime/merge-queue/reconciliation").resolve()


def _kanban_terminal_identity(task: dict[str, Any]) -> dict[str, Any]:
    """Bind canonical task truth without expanded, independently mutable relations."""
    canonical_task = {key: value for key, value in task.items() if not key.startswith("_")}
    return {"task": canonical_task, "sha256": digest(canonical_task)}


def terminal_reconciliation_plan(controller: Path, task_id: str) -> dict[str, Any]:
    """Plan an evidence-only terminal transition for one exact contained tip."""
    if not task_runtime.TASK_RE.fullmatch(task_id):
        raise MergeQueueError("unsafe task id")
    config = task_runtime.load_config(controller)
    repository = task_runtime.product_repository(controller, config)
    with task_runtime.state_lock(controller):
        state = task_runtime.read_state(controller)
        record = state.get("tasks", {}).get(task_id)
        entry = state.get("queues", {}).get(target_key(repository, config["target_ref"]))
    if not isinstance(record, dict) or record.get("state") not in {
            "REVIEW_FINDINGS", "REVIEW_FINDINGS_EXHAUSTED"}:
        raise MergeQueueError("terminal reconciliation requires REVIEW_FINDINGS or REVIEW_FINDINGS_EXHAUSTED")
    tip_sha = record.get("tip_sha")
    if (record.get("task_id") != task_id
            or record.get("target_ref") != config["target_ref"]
            or not isinstance(record.get("repository"), str)
            or Path(record["repository"]).resolve() != repository
            or not isinstance(tip_sha, str)
            or optional_revision(repository, tip_sha) != tip_sha):
        raise MergeQueueError("terminal reconciliation queue identity is incomplete or drifted")
    target_sha = task_runtime.ref_sha(repository, config["target_ref"])
    if task_runtime.run(
            ["git", "-C", str(repository), "merge-base", "--is-ancestor", tip_sha, target_sha],
            repository, check=False).returncode:
        raise MergeQueueError("exact queued feature tip is not an ancestor of the protected target")
    kanban = read_kanban_task(controller, task_id)
    if kanban.get("status") != "done":
        raise MergeQueueError("canonical Kanban task is not terminal done")
    kanban_identity = _kanban_terminal_identity(kanban)
    body = {
        "schema_version": RECONCILE_SCHEMA,
        "operation": "terminal-already-in-target",
        "repository_identity": repository_identity(repository),
        "repository": str(repository),
        "target_ref": config["target_ref"],
        "target_sha": target_sha,
        "task_id": task_id,
        "tip_sha": tip_sha,
        "source_state": record["state"],
        "queue_record_sha256": digest(record),
        "queue_entry_sha256": digest(entry),
        "kanban_identity": kanban_identity,
    }
    return {**body, "plan_id": digest({"schema_version": RECONCILE_ID_SCHEMA, "plan": body})}


def persist_terminal_reconciliation_plan(controller: Path, task_id: str) -> dict[str, Any]:
    config = task_runtime.load_config(controller)
    repository = task_runtime.product_repository(controller, config)
    with target_lock(controller, repository, config["target_ref"]):
        plan = terminal_reconciliation_plan(controller, task_id)
        # Recheck state and target after the external Kanban read and before receipt durability.
        with task_runtime.state_lock(controller):
            record = task_runtime.read_state(controller).get("tasks", {}).get(task_id)
        if (not isinstance(record, dict) or digest(record) != plan["queue_record_sha256"]
                or task_runtime.ref_sha(repository, config["target_ref"]) != plan["target_sha"]):
            raise MergeQueueError("terminal reconciliation identity moved while planning")
        path = _reconciliation_receipt_root(controller) / task_id / f"{plan['plan_id']}.json"
        data = (canonical(plan) + "\n").encode()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if path.read_bytes() != data:
                raise MergeQueueError("terminal reconciliation receipt identity collided")
        else:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data); handle.flush(); os.fsync(handle.fileno())
        return {**plan, "receipt": {"path": str(path),
                                     "sha256": hashlib.sha256(data).hexdigest()}}


def apply_terminal_reconciliation(controller: Path, task_id: str, receipt_path: str,
                                  receipt_sha256: str) -> dict[str, Any]:
    """Apply one immutable plan under the target lock without executing product work."""
    config = task_runtime.load_config(controller)
    repository = task_runtime.product_repository(controller, config)
    path = Path(receipt_path).expanduser().resolve()
    root = _reconciliation_receipt_root(controller)
    try:
        path.relative_to(root)
        data = path.read_bytes()
        plan = json.loads(data)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        raise MergeQueueError("terminal reconciliation receipt is absent or unauthorized") from exc
    body = ({key: value for key, value in plan.items() if key != "plan_id"}
            if isinstance(plan, dict) else {})
    if (hashlib.sha256(data).hexdigest() != receipt_sha256
            or not isinstance(plan, dict) or plan.get("schema_version") != RECONCILE_SCHEMA
            or plan.get("task_id") != task_id
            or path != root / task_id / f"{plan.get('plan_id')}.json"
            or plan.get("plan_id") != digest({"schema_version": RECONCILE_ID_SCHEMA,
                                               "plan": body})
            or plan.get("repository_identity") != repository_identity(repository)
            or plan.get("repository") != str(repository)
            or plan.get("target_ref") != config["target_ref"]):
        raise MergeQueueError("terminal reconciliation receipt is forged or tampered")
    reference = {
        "schema_version": RECONCILE_REFERENCE_SCHEMA,
        "plan_id": plan["plan_id"], "receipt_path": str(path),
        "receipt_sha256": receipt_sha256, "repository_identity": plan["repository_identity"],
        "target_ref": plan["target_ref"], "target_sha": plan["target_sha"],
        "task_id": task_id, "tip_sha": plan["tip_sha"],
        "source_state": plan["source_state"],
        "queue_record_sha256": plan["queue_record_sha256"],
        "kanban_sha256": plan["kanban_identity"]["sha256"],
    }
    with target_lock(controller, repository, config["target_ref"]):
        if task_runtime.ref_sha(repository, config["target_ref"]) != plan["target_sha"]:
            raise MergeQueueError("terminal reconciliation protected target moved")
        kanban_identity = _kanban_terminal_identity(read_kanban_task(controller, task_id))
        if (kanban_identity != plan.get("kanban_identity")
                or kanban_identity["task"].get("status") != "done"):
            raise MergeQueueError("terminal reconciliation Kanban identity or terminal truth drifted")
        with task_runtime.state_lock(controller):
            state = task_runtime.read_state(controller)
            record = state.get("tasks", {}).get(task_id)
            entry = state.get("queues", {}).get(target_key(repository, config["target_ref"]))
            if digest(entry) != plan.get("queue_entry_sha256"):
                raise MergeQueueError("terminal reconciliation target queue identity drifted")
            if isinstance(record, dict) and record.get("state") == "MERGED":
                if record.get("terminal_reconciliation") != reference:
                    raise MergeQueueError("task is MERGED without this exact reconciliation identity")
                return {**record, "outcome": "TERMINAL_RECONCILIATION_ALREADY_APPLIED",
                        "terminal_reconciliation": reference}
            if (not isinstance(record, dict)
                    or record.get("state") != plan["source_state"]
                    or digest(record) != plan["queue_record_sha256"]):
                raise MergeQueueError("terminal reconciliation queue identity drifted")
            if record.get("tip_sha") != plan["tip_sha"]:
                raise MergeQueueError("terminal reconciliation exact feature tip drifted")
            if task_runtime.run(
                    ["git", "-C", str(repository), "merge-base", "--is-ancestor",
                     plan["tip_sha"], plan["target_sha"]], repository, check=False).returncode:
                raise MergeQueueError("exact queued feature tip is not an ancestor of the protected target")
            updated = {**record, "state": "MERGED",
                       "last_queue_outcome": "ALREADY_IN_TARGET_RECONCILED",
                       "terminal_reconciliation": reference}
            state["tasks"][task_id] = updated
            task_runtime.write_state(controller, state)
        return {**updated, "outcome": "TERMINAL_RECONCILIATION_APPLIED"}


def _refresh_receipt_root(controller: Path) -> Path:
    return (controller / ".juno_task/runtime/merge-queue/target-refresh").resolve()


def target_refresh_plan(controller: Path, task_id: str) -> dict[str, Any]:
    """Classify a committed target refresh without trusting its merge message."""
    if not task_runtime.TASK_RE.fullmatch(task_id):
        raise MergeQueueError("unsafe task id")
    config = task_runtime.load_config(controller)
    repository = task_runtime.product_repository(controller, config)
    with task_runtime.state_lock(controller):
        state = task_runtime.read_state(controller)
        record = state.get("tasks", {}).get(task_id)
        queue_entry = state.get("queues", {}).get(target_key(repository, config["target_ref"]))
    if not isinstance(record, dict) or record.get("state") not in {
            "QUEUED", "AWAITING_RISK", "REVIEW_FINDINGS", "CONFLICT_RESOLVED",
            "REOPENING"}:
        raise MergeQueueError("target refresh is not eligible in the current queue state")
    required = {"base_sha", "tip_sha", "branch_ref", "worktree", "repository",
                "target_ref", "changed_paths", "creation_receipt", "workspace_identity"}
    if not required.issubset(record):
        raise MergeQueueError("target refresh source record is incomplete")
    repository_path = Path(record["repository"]).resolve()
    worktree = task_runtime.exact_root(Path(record["worktree"]), "feature worktree")
    if (repository_path != repository or record["target_ref"] != config["target_ref"]
            or task_runtime.git(worktree, "symbolic-ref", "-q", "HEAD", check=False)
                != record["branch_ref"]):
        raise MergeQueueError("target refresh repository/branch/worktree identity drifted")
    if task_runtime.git(worktree, "status", "--porcelain=v1", "--untracked-files=all"):
        raise MergeQueueError("feature worktree must be clean before target refresh")
    new_tip = task_runtime.git(worktree, "rev-parse", "HEAD")
    if task_runtime.git(repository, "rev-parse", record["branch_ref"], check=False) != new_tip:
        raise MergeQueueError("target refresh branch tip identity drifted")
    base_sha, source_tip = record["base_sha"], record["tip_sha"]
    target_sha = task_runtime.ref_sha(repository, config["target_ref"])
    for ancestor, descendant, label in ((base_sha, source_tip, "original feature"),
                                        (source_tip, new_tip, "refreshed feature"),
                                        (target_sha, new_tip, "protected target"),
                                        (base_sha, target_sha, "protected target")):
        if task_runtime.run(["git", "-C", str(repository), "merge-base", "--is-ancestor",
                             ancestor, descendant], repository, check=False).returncode:
            raise MergeQueueError(f"{label} ancestry is forged or non-descendant")
    if new_tip == source_tip:
        raise MergeQueueError("target refresh requires a new committed tip")

    creation = record["creation_receipt"]
    identity = record["workspace_identity"]
    if (not isinstance(creation, dict) or not isinstance(identity, dict)
            or digest(creation) != identity.get("create_receipt_sha256")):
        # task_workspace uses the same canonical JSON SHA-256 projection.
        raise MergeQueueError("original immutable creation admission drifted")
    admitted = sorted(record.get("changed_paths", []))
    original_changed = sorted(set(task_runtime.git(
        repository, "diff", "--no-renames", "--name-only",
        f"{base_sha}..{source_tip}").splitlines()))
    frozen_allowed = creation.get("allowed_paths")
    if (not admitted or any(task_runtime.path_within(path, config["controller_private_paths"])
                            or not task_runtime.path_within(path, frozen_allowed)
                            for path in admitted)):
        raise MergeQueueError("original feature admission is empty, private, or disallowed")

    source_target_bases = task_runtime.git(
        repository, "merge-base", "--all", source_tip, target_sha).splitlines()
    if len(source_target_bases) != 1 or task_runtime.SHA_RE.fullmatch(source_target_bases[0]) is None:
        raise MergeQueueError("source and protected target require one exact merge base")
    trees = {name: _git_tree(repository, sha) for name, sha in {
        "base": base_sha, "source_target_base": source_target_bases[0],
        "source": source_tip, "target": target_sha, "refreshed": new_tip}.items()}
    admitted_set = set(admitted)
    original_set = set(original_changed)
    unchanged_tombstones = {path for path in admitted_set
                            if trees["base"].get(path) is None and trees["source"].get(path) is None}
    if not (admitted_set - unchanged_tombstones).issubset(original_set):
        raise MergeQueueError("original immutable queue path admission drifted")
    inherited_source_paths = original_set - admitted_set
    if any(trees["source"].get(path) != trees["source_target_base"].get(path)
           for path in inherited_source_paths):
        raise MergeQueueError("original immutable queue path admission drifted")
    paths = sorted(set().union(*(set(tree) for tree in trees.values())))
    rows: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    for path in paths:
        base_blob = trees["base"].get(path); source_blob = trees["source"].get(path)
        target_blob = trees["target"].get(path); refreshed_blob = trees["refreshed"].get(path)
        classification: Optional[str] = None
        if path in admitted_set:
            classification = "feature-authored"
        elif path in inherited_source_paths:
            classification = "inherited-target-derived"
            if refreshed_blob != target_blob:
                rejected.append({"path": path, "reason": "altered-target-derived-byte"})
        elif source_blob != base_blob:
            rejected.append({"path": path, "reason": "unadmitted-original-feature-byte"})
        elif target_blob != base_blob:
            classification = "unchanged-target-derived"
            if refreshed_blob != target_blob:
                rejected.append({"path": path, "reason": "altered-target-derived-byte"})
        elif refreshed_blob != source_blob:
            rejected.append({"path": path, "reason": "private-or-unauthorized-addition"})
        if classification or refreshed_blob != source_blob:
            rows.append({"path": path, "classification": classification or "rejected",
                         "base_blob": base_blob, "source_blob": source_blob,
                         "target_blob": target_blob, "refreshed_blob": refreshed_blob})
    if rejected:
        detail = ", ".join(f"{row['path']}:{row['reason']}" for row in rejected[:12])
        raise MergeQueueError(f"target refresh byte admission refused: {detail}")

    evidence = {"creation_receipt": creation,
                "creation_receipt_sha256": digest(creation),
                "queue_record_sha256": digest(record),
                "queue_entry_sha256": digest(queue_entry),
                "queue_attempt": record.get("queue_attempt"),
                "prior_queue_failure": record.get("prior_queue_failure")}
    origin_projection = task_runtime.decisions.project_path_origins(
        base_tree=trees["base"], source_tree=trees["refreshed"],
        target_tree=trees["target"], candidate_tree=trees["refreshed"],
        admitted_paths=admitted, generated_bindings=(
            creation.get("generated_output_admission", {}).get("bindings", [])
            if isinstance(creation.get("generated_output_admission"), dict) else []),
        conflict_paths=[])
    if origin_projection["ambiguous_paths"]:
        raise MergeQueueError("target refresh has ambiguous legacy changed_paths: "
                              + ", ".join(origin_projection["ambiguous_paths"][:12]))
    body = {"schema_version": REFRESH_SCHEMA, "task_id": task_id,
            "operation": "target-refresh", "repository_identity": repository_identity(repository),
            "repository": str(repository), "target_ref": config["target_ref"],
            "target_sha": target_sha, "base_sha": base_sha, "source_tip": source_tip,
            "refreshed_tip": new_tip, "branch_ref": record["branch_ref"],
            "worktree": str(worktree), "source_state": record["state"],
            "authored_paths": admitted, "classifications": rows,
            "origin_projection": origin_projection, "evidence": evidence}
    return {**body, "plan_id": digest({"schema_version": REFRESH_ID_SCHEMA, "plan": body})}


def persist_target_refresh_plan(controller: Path, task_id: str) -> dict[str, Any]:
    plan = target_refresh_plan(controller, task_id)
    path = _refresh_receipt_root(controller) / task_id / f"{plan['plan_id']}.json"
    data = (canonical(plan) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.read_bytes() != data:
            raise MergeQueueError("target refresh receipt identity collided")
    else:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data); handle.flush(); os.fsync(handle.fileno())
    return {**plan, "receipt": {"path": str(path),
                                "sha256": hashlib.sha256(data).hexdigest()}}


def _cleanup_refreshed_candidate(controller: Path, repository: Path,
                                 plan: dict[str, Any]) -> None:
    attempt = plan.get("evidence", {}).get("queue_attempt")
    if not isinstance(attempt, dict):
        return
    checkout_value, token = attempt.get("candidate_checkout"), attempt.get("candidate_token")
    if not checkout_value:
        return
    if not isinstance(token, str):
        raise MergeQueueError("target refresh historical candidate token is missing")
    checkout = Path(checkout_value)
    if checkout.exists():
        owner = verify_candidate_owner(controller, repository, checkout, token)
        if (owner.get("task_id") != plan.get("task_id")
                or owner.get("feature_sha") != plan.get("source_tip")):
            raise MergeQueueError("target refresh historical candidate ownership drifted")
        rollback_unadmitted_candidate(controller, repository, checkout, token)
        return
    marker = owner_marker(controller, checkout)
    if marker.exists():
        owner = read_candidate_owner(controller, checkout)
        if (owner.get("token") != token or owner.get("task_id") != plan.get("task_id")
                or owner.get("feature_sha") != plan.get("source_tip")):
            raise MergeQueueError("target refresh orphan candidate ownership drifted")
        marker.unlink()


def _target_refresh_review_ready_closure(
        controller: Path, repository: Path, record: dict[str, Any], plan: dict[str, Any],
        receipt_sha256: str, validations: list[dict[str, Any]],
        command_evidence: dict[str, Any]) -> dict[str, Any]:
    """Bind refreshed-tip validation without relabelling old-tip standing receipts."""
    source = record.get("review_ready_closure")
    source_kind = "review_ready_closure"
    if isinstance(source, dict):
        source_body = {key: value for key, value in source.items() if key != "closure_sha256"}
        if (source.get("schema_version") != "juno_task_review_ready_closure.v1"
                or source.get("closure_sha256") != task_runtime.stable_sha256(source_body)
                or source.get("task_id") != plan.get("task_id")
                or source.get("tip_sha") != plan.get("source_tip")
                or source.get("changed_paths") != record.get("changed_paths")):
            raise MergeQueueError("target refresh source review-ready closure is forged or stale")
    else:
        creation = record.get("creation_receipt")
        identity = record.get("workspace_identity")
        if (not isinstance(creation, dict) or not isinstance(identity, dict)
                or identity.get("create_receipt_sha256") != task_runtime.stable_sha256(creation)
                or identity.get("expected_paths_sha256") != creation.get("expected_paths_sha256")
                or not isinstance(creation.get("generated_output_admission"), dict)):
            raise MergeQueueError("target refresh immutable creation identity is missing or forged")
        source_kind = "immutable_creation_identity_regeneration"
        source = {
            "base_sha": record.get("base_sha"),
            "allowed_paths_sha256": identity["expected_paths_sha256"],
            "creation_receipt_sha256": identity["create_receipt_sha256"],
            "generated_output_admission_sha256": task_runtime.stable_sha256(
                creation["generated_output_admission"]),
            "unresolved_findings_candidate_sha": None,
        }
    if not isinstance(command_evidence, dict):
        raise MergeQueueError("target refresh command evidence is missing")
    counters = command_evidence.get("counters")
    decisions = command_evidence.get("decisions")
    if not isinstance(counters, dict) or not isinstance(decisions, list):
        raise MergeQueueError("target refresh command evidence is malformed")
    for result in validations:
        identity = result.get("identity") if isinstance(result, dict) else None
        if (not isinstance(result, dict) or result.get("exit_code") != 0
                or result.get("timed_out")
                or result.get("result_integrity", {}).get("eligible_pass") is False
                or (isinstance(identity, dict)
                    and identity.get("candidate_sha") != plan.get("refreshed_tip"))):
            raise MergeQueueError("target refresh validation is failed or stale")
    if not validations and not (counters.get("not_applicable", 0) or counters.get("skipped", 0)):
        raise MergeQueueError("target refresh has no exact validation proof")
    policy_path = controller / ".juno_task/config/risk-policy.json"
    try:
        risk_policy_sha256 = hashlib.sha256(policy_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise MergeQueueError("risk policy is missing during target refresh") from exc
    runtime = task_runtime.runtime_generation(repository, plan["target_sha"])
    body = {
        "schema_version": "juno_task_review_ready_closure.v1",
        "task_id": plan["task_id"],
        "base_sha": source.get("base_sha"),
        "tip_sha": plan["refreshed_tip"],
        "tree_sha": task_runtime.git(repository, "rev-parse", f"{plan['refreshed_tip']}^{{tree}}"),
        "changed_paths": plan["authored_paths"],
        "changed_paths_sha256": task_runtime.stable_sha256(plan["authored_paths"]),
        "allowed_paths_sha256": source.get("allowed_paths_sha256"),
        "creation_receipt_sha256": source.get("creation_receipt_sha256"),
        "generated_output_admission_sha256": source.get("generated_output_admission_sha256"),
        "risk_policy_sha256": risk_policy_sha256,
        "runtime_sha256": runtime["running_sha256"],
        "unresolved_findings_candidate_sha": source.get("unresolved_findings_candidate_sha"),
        "submission": source.get("submission"),
        "target_refresh": {
            "plan_id": plan["plan_id"],
            "receipt_sha256": receipt_sha256,
            "target_sha": plan["target_sha"],
            "source_tip": plan["source_tip"],
            "source_kind": source_kind,
            "source_identity_sha256": (source.get("closure_sha256")
                                       or task_runtime.stable_sha256(source)),
            "standing_evidence_decision": ("reused_lineage" if source_kind == "review_ready_closure"
                                             else "invalidated_missing_source_closure"),
        },
        "authoritative_validation": {
            "results_sha256": task_runtime.stable_sha256(validations),
            "command_evidence_sha256": task_runtime.stable_sha256(command_evidence),
            "counters": counters,
        },
    }
    required = ("base_sha", "allowed_paths_sha256", "creation_receipt_sha256",
                "generated_output_admission_sha256")
    if any(not isinstance(body[key], str) for key in required):
        raise MergeQueueError("target refresh source review-ready closure is incomplete")
    closure = {**body, "closure_sha256": task_runtime.stable_sha256(body)}
    if not closure["closure_sha256"]:
        raise MergeQueueError("target refresh review-ready closure could not be produced")
    return closure


def apply_target_refresh(controller: Path, task_id: str, receipt_path: str,
                         receipt_sha256: str) -> dict[str, Any]:
    """Apply only an exact canonical refresh receipt; retries are read-only."""
    config = task_runtime.load_config(controller)
    repository = task_runtime.product_repository(controller, config)
    path = Path(receipt_path).expanduser().resolve()
    root = _refresh_receipt_root(controller)
    try:
        path.relative_to(root)
        data = path.read_bytes()
        plan = json.loads(data)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        raise MergeQueueError("target refresh receipt is absent or unauthorized") from exc
    if (hashlib.sha256(data).hexdigest() != receipt_sha256
            or not isinstance(plan, dict) or plan.get("schema_version") != REFRESH_SCHEMA
            or plan.get("task_id") != task_id
            or path != root / task_id / f"{plan.get('plan_id')}.json"
            or plan.get("plan_id") != digest({"schema_version": REFRESH_ID_SCHEMA,
                                               "plan": {key: value for key, value in plan.items()
                                                        if key != "plan_id"}})):
        raise MergeQueueError("target refresh receipt is forged or tampered")
    with target_lock(controller, repository, config["target_ref"]):
        with task_runtime.state_lock(controller):
            state = task_runtime.read_state(controller)
            record = state.get("tasks", {}).get(task_id)
        if not isinstance(record, dict):
            raise MergeQueueError("target refresh task record disappeared")
        references = record.get("target_refreshes", [])
        prior = next((row for row in references if isinstance(row, dict)
                      and row.get("plan_id") == plan["plan_id"]), None)
        if prior is not None:
            _cleanup_refreshed_candidate(controller, repository, plan)
            return {**record, "outcome": "TARGET_REFRESH_ALREADY_APPLIED",
                    "target_refresh": prior}
        if (task_runtime.ref_sha(repository, config["target_ref"]) != plan.get("target_sha")
                or digest(record) != plan.get("evidence", {}).get("queue_record_sha256")):
            raise MergeQueueError("target refresh target, ancestry, queue, or plan identity drifted")
        current = target_refresh_plan(controller, task_id)
        if current != plan:
            raise MergeQueueError("target refresh target, ancestry, queue, or plan identity drifted")
        # Reuse the shared static authored/target-derived feasibility gate before
        # any validation process. It observes the refreshed branch tip while
        # the immutable queue record still binds the original admission.
        static = assert_static_plan(controller, task_id, "target-refresh")
        worktree = task_runtime.exact_root(Path(plan["worktree"]), "feature worktree")
        validations, command_evidence = authoritative_validation_rows(
            controller, config, repository, record, worktree, plan["refreshed_tip"])
        if (task_runtime.ref_sha(repository, config["target_ref"]) != plan["target_sha"]
                or task_runtime.git(worktree, "rev-parse", "HEAD") != plan["refreshed_tip"]
                or task_runtime.git(repository, "rev-parse", plan["branch_ref"], check=False)
                    != plan["refreshed_tip"]
                or task_runtime.git(worktree, "status", "--porcelain=v1", "--untracked-files=all")):
            raise MergeQueueError("target refresh identity drifted during validation")
        refreshed_closure = _target_refresh_review_ready_closure(
            controller, repository, record, plan, receipt_sha256, validations, command_evidence)
        reference = {"schema_version": "juno_merge_target_refresh_reference.v1",
                     "plan_id": plan["plan_id"], "receipt_path": str(path),
                     "receipt_sha256": receipt_sha256, "source_tip": plan["source_tip"],
                     "target_sha": plan["target_sha"], "refreshed_tip": plan["refreshed_tip"],
                     "feasibility_plan_id": static["plan_id"]}
        updated = {key: value for key, value in record.items()
                   if key not in {"queue_attempt", "last_queue_outcome", "reopen_attempt"}}
        updated.update({"state": "QUEUED", "tip_sha": plan["refreshed_tip"],
                        "changed_paths": plan["authored_paths"], "validation": validations,
                        "command_evidence": command_evidence,
                        "review_ready_closure": refreshed_closure,
                        "last_validation_outcome": "PASSED",
                        "target_refreshes": [*references, reference]})
        with task_runtime.state_lock(controller):
            state = task_runtime.read_state(controller)
            if state.get("tasks", {}).get(task_id) != record:
                raise MergeQueueError("target refresh queue identity drifted before apply")
            updated["enqueue_sequence"] = task_runtime.assign_enqueue_sequence(state)
            state["tasks"][task_id] = updated
            task_runtime.write_state(controller, state)
        # Durable QUEUED truth and its immutable receipt precede deletion. A
        # cleanup failure therefore retries this exact owner-bound step only.
        _cleanup_refreshed_candidate(controller, repository, plan)
        return {**updated, "outcome": "TARGET_REFRESH_APPLIED", "target_refresh": reference}


def _full_suite_repair_receipt_root(controller: Path) -> Path:
    return (controller / FULL_SUITE_REPAIR_ROOT).resolve()


def _deterministic_router_finding(receipt: dict[str, Any], command_cwd: str) -> dict[str, Any]:
    """Classify only the receipt shape proven deterministic by attempts 230/231."""
    result = receipt.get("result")
    retries = result.get("retries") if isinstance(result, dict) else None
    integrity = result.get("result_integrity") if isinstance(result, dict) else None
    files = retries.get("files") if isinstance(retries, dict) else None
    if (not isinstance(result, dict) or result.get("exit_code") == 0
            or result.get("timed_out") is True
            or not isinstance(integrity, dict) or integrity.get("contradiction") is not False
            or not isinstance(retries, dict) or retries.get("absorbed") is not False
            or not isinstance(files, list) or not files):
        raise MergeQueueError(
            "deterministic full-suite repair refused (environmental_only): "
            "receipt has no repeated deterministic test finding")
    identities: set[str] = set()
    failure_paths: set[str] = set()
    for row in files:
        attempts = row.get("attempts") if isinstance(row, dict) else None
        path = row.get("file") if isinstance(row, dict) else None
        tail = row.get("final_tail") if isinstance(row, dict) else None
        if (not isinstance(path, str) or Path(path).is_absolute() or ".." in Path(path).parts
                or not path.endswith("src/bin/__tests__/router-allowlist.test.ts")
                or row.get("passed") is not False or not isinstance(tail, str)
                or "route_registered_product_control" not in tail
                or not isinstance(attempts, list) or len(attempts) < 2
                or any(not isinstance(item, dict) or item.get("exit_code") == 0
                       or item.get("timed_out") is not False for item in attempts)):
            raise MergeQueueError(
                "deterministic full-suite repair refused (unsupported_finding): "
                "failure is not the repeated router-allowlist contract")
        failure_paths.add(str(Path(command_cwd) / path))
        identities.update(re.findall(r"['\"]((?:task|merge|evidence):[a-z0-9-]+)['\"]", tail))
    if not identities:
        raise MergeQueueError(
            "deterministic full-suite repair refused (finding_malformed): "
            "router finding has no exact missing command identity")
    surfaces = {identity.split(":", 1)[0] for identity in identities}
    allowed = set(failure_paths)
    allowed.add(str(Path(command_cwd) / "src/bin/yylo.sh"))
    for surface in surfaces:
        allowed.add(str(Path(command_cwd) / f"src/cli/commands/{surface}.ts"))
        allowed.add(str(Path(command_cwd) / f"src/cli/__tests__/{surface}-command.test.ts"))
    body = {"kind": "router_allowlist_missing_registered_command",
            "identities": sorted(identities), "failure_paths": sorted(failure_paths),
            "allowed_paths": sorted(allowed)}
    return {**body, "finding_sha256": digest(body)}


def _record_has_deterministic_router_finding(record: dict[str, Any]) -> bool:
    attempt = record.get("queue_attempt")
    risk = attempt.get("risk") if isinstance(attempt, dict) else None
    progress = risk.get("review_progress") if isinstance(risk, dict) else None
    admission = progress.get("full_suite_admission") if isinstance(progress, dict) else None
    references = admission.get("receipts") if isinstance(admission, dict) else None
    if admission.get("state") != "FAILED" or not isinstance(references, list) or not references:
        return False
    reference = references[-1]
    try:
        path = Path(reference["receipt_path"])
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != reference["receipt_sha256"]:
            return False
        receipt = json.loads(data)
        _deterministic_router_finding(receipt, receipt.get("command", {}).get("cwd", ""))
    except (KeyError, OSError, json.JSONDecodeError, MergeQueueError):
        return False
    return True


def _full_suite_repair_safe_next(controller: Path, repository: Path,
                                 config: dict[str, Any], task_id: str,
                                 record: dict[str, Any]) -> dict[str, Any]:
    if (record.get("state") != "AWAITING_RISK"
            or record.get("last_queue_outcome") != "FAILED_FULL_SUITE"):
        return {"reason_code": None, "safe_next_command": None}
    if not _record_has_deterministic_router_finding(record):
        return {"reason_code": "failed_full_suite_not_deterministic_repair",
                "safe_next_command": "yy merge arbiter run"}
    arbiter = _arbiter_state(_arbiter_root(controller, repository, config["target_ref"]))
    pointer_path = controller / MERGE_DRIVE_ROOT / "latest.json"
    try:
        pointer = json.loads(pointer_path.read_text())
        run_id = pointer["run_id"]
        journal_path = controller / MERGE_DRIVE_ROOT / run_id / "journal.json"
        journal_bytes = journal_path.read_bytes()
        journal = json.loads(journal_bytes)
        terminal = arbiter["terminal_receipt"]
        values_valid = (
            arbiter.get("state") == "FAILED" and isinstance(arbiter.get("attempt"), int)
            and isinstance(terminal, dict) and isinstance(terminal.get("path"), str)
            and isinstance(terminal.get("sha256"), str)
            and journal.get("run_id") == run_id
            and pointer.get("scope_sha256") == journal.get("scope_sha256"))
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        values_valid = False
    if not values_valid:
        return {"reason_code": "deterministic_full_suite_repair_evidence_incomplete",
                "safe_next_command": None}
    command = " ".join([
        "yy merge recover-full-suite-failure", shlex.quote(task_id),
        "--attempt", str(arbiter["attempt"]),
        "--terminal-receipt", shlex.quote(terminal["path"]),
        "--terminal-receipt-sha256", terminal["sha256"],
        "--expected-revision", digest(record),
        "--run-id", shlex.quote(run_id),
        "--scope-sha256", journal["scope_sha256"],
        "--journal-sha256", hashlib.sha256(journal_bytes).hexdigest(),
    ])
    return {"reason_code": "deterministic_full_suite_repair_available",
            "safe_next_command": command}


def recover_deterministic_full_suite_failure(
        controller: Path, task_id: str, arbiter_attempt: int,
        terminal_receipt_path: str, terminal_receipt_sha256: str,
        expected_record_revision: str, run_id: str, scope_sha256: str,
        journal_sha256: str) -> dict[str, Any]:
    """Authorize one queue-owned repair without replaying an unchanged suite."""
    hashes = (terminal_receipt_sha256, expected_record_revision,
              scope_sha256, journal_sha256)
    if (not task_runtime.TASK_RE.fullmatch(task_id)
            or not isinstance(arbiter_attempt, int) or isinstance(arbiter_attempt, bool)
            or arbiter_attempt < 2 or not all(re.fullmatch(r"[0-9a-f]{64}", x or "")
                                             for x in hashes)
            or not re.fullmatch(r"[0-9]+-[0-9a-f]+", run_id or "")):
        raise MergeQueueError("deterministic full-suite repair refused (malformed_request)")
    config = task_runtime.load_config(controller)
    repository = task_runtime.product_repository(controller, config)
    arbiter_root = _arbiter_root(controller, repository, config["target_ref"])
    supplied = Path(terminal_receipt_path).expanduser().resolve()
    expected_terminal = (arbiter_root / "receipts"
                         / f"attempt-{arbiter_attempt}-failed.json").resolve()
    try:
        terminal_bytes = supplied.read_bytes()
        terminal = json.loads(terminal_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise MergeQueueError(
            "deterministic full-suite repair refused (receipt_malformed)") from exc
    if (supplied != expected_terminal
            or hashlib.sha256(terminal_bytes).hexdigest() != terminal_receipt_sha256
            or terminal.get("schema_version") != TARGET_ARBITER_RECEIPT_SCHEMA
            or terminal.get("attempt") != arbiter_attempt
            or terminal.get("target_ref") != config["target_ref"]
            or terminal.get("state") != "FAILED"):
        raise MergeQueueError("deterministic full-suite repair refused (receipt_malformed)")
    with review_lock(repository, task_id):
        with _target_arbiter_claim(arbiter_root) as arbiter_claim:
            if arbiter_claim is None:
                raise MergeQueueError(
                    "deterministic full-suite repair refused (live_producer)")
            with target_lock(controller, repository, config["target_ref"]):
                arbiter = _arbiter_state(arbiter_root)
                predecessor = arbiter.get("successor_of") if isinstance(arbiter, dict) else None
                predecessor_path = (arbiter_root / "receipts"
                                    / f"attempt-{arbiter_attempt - 1}-failed.json").resolve()
                try:
                    predecessor_bytes = predecessor_path.read_bytes()
                    predecessor_value = json.loads(predecessor_bytes)
                except (OSError, json.JSONDecodeError) as exc:
                    raise MergeQueueError(
                        "deterministic full-suite repair refused (predecessor_malformed)") from exc
                if (not isinstance(arbiter, dict) or arbiter.get("state") != "FAILED"
                        or arbiter.get("attempt") != arbiter_attempt
                        or arbiter.get("terminal_receipt") != {
                            "path": str(supplied), "sha256": terminal_receipt_sha256}
                        or arbiter.get("target_sha_at_start")
                            != task_runtime.ref_sha(repository, config["target_ref"])
                        or predecessor != {"path": str(predecessor_path),
                                           "sha256": hashlib.sha256(predecessor_bytes).hexdigest()}
                        or predecessor_value.get("attempt") != arbiter_attempt - 1
                        or predecessor_value.get("state") != "FAILED"):
                    raise MergeQueueError(
                        "deterministic full-suite repair refused (arbiter_identity_moved)")
                pointer_path = controller / MERGE_DRIVE_ROOT / "latest.json"
                journal_path = controller / MERGE_DRIVE_ROOT / run_id / "journal.json"
                try:
                    pointer = json.loads(pointer_path.read_text())
                    journal_bytes = journal_path.read_bytes()
                    journal = json.loads(journal_bytes)
                except (OSError, json.JSONDecodeError) as exc:
                    raise MergeQueueError(
                        "deterministic full-suite repair refused (lifecycle_malformed)") from exc
                pending = journal.get("operations", [])[-1:] if isinstance(journal, dict) else []
                if (hashlib.sha256(journal_bytes).hexdigest() != journal_sha256
                        or pointer.get("run_id") != run_id
                        or pointer.get("scope_sha256") != scope_sha256
                        or journal.get("run_id") != run_id
                        or journal.get("scope_sha256") != scope_sha256
                        or journal.get("terminal") is True
                        or journal.get("state") != "CLAIMED"
                        or journal.get("initial_target_sha")
                            != task_runtime.ref_sha(repository, config["target_ref"])
                        or journal.get("attempts", {}).get("semantic_repairs") != 0
                        or journal.get("repairs") != []
                        or len(pending) != 1 or pending[0].get("task_id") != task_id
                        or pending[0].get("phase") != "review"
                        or pending[0].get("post_state") is not None):
                    raise MergeQueueError(
                        "deterministic full-suite repair refused (lifecycle_identity_moved)")
                with task_runtime.state_lock(controller):
                    state = task_runtime.read_state(controller)
                    record = state["tasks"].get(task_id)
                if (not isinstance(record, dict)
                        or digest(record) != expected_record_revision):
                    raise MergeQueueError(
                        "deterministic full-suite repair refused (revision_mismatch)")
                attempt = record.get("queue_attempt")
                risk = attempt.get("risk") if isinstance(attempt, dict) else None
                progress = risk.get("review_progress") if isinstance(risk, dict) else None
                admission = progress.get("full_suite_admission") if isinstance(progress, dict) else None
                if (record.get("state") != "AWAITING_RISK"
                        or record.get("last_queue_outcome") != "FAILED_FULL_SUITE"
                        or not isinstance(attempt, dict)
                        or attempt.get("outcome") != "FAILED_FULL_SUITE"
                        or record.get("review_round", 1) != 1
                        or record.get("full_suite_repair") is not None
                        or not isinstance(admission, dict) or admission.get("state") != "FAILED"
                        or risk.get("plan", {}).get("candidate", {}).get("changed_paths")
                            != record.get("changed_paths")):
                    raise MergeQueueError(
                        "deterministic full-suite repair refused (state_or_budget)")
                candidate_sha = attempt.get("candidate_sha")
                candidate_tree = attempt.get("candidate_tree")
                target_sha = task_runtime.ref_sha(repository, config["target_ref"])
                if (candidate_sha != risk.get("candidate_sha")
                        or candidate_sha != risk.get("plan", {}).get("candidate", {}).get("candidate_sha")
                        or task_runtime.git(repository, "rev-parse", f"{candidate_sha}^{{tree}}",
                                            check=False) != candidate_tree
                        or attempt.get("expected_target_sha") != target_sha):
                    raise MergeQueueError(
                        "deterministic full-suite repair refused (candidate_or_target_moved)")
                policy = risk_runtime.load_policy(risk_policy_path(controller))
                request = risk_request(repository, candidate_sha, config["target_ref"], target_sha)
                plan = risk_runtime.classify(policy, request, risk_flags(record))
                if plan != risk.get("plan"):
                    raise MergeQueueError(
                        "deterministic full-suite repair refused (policy_identity_moved)")
                candidate_root = (task_runtime.exact_root(Path(attempt["candidate_checkout"]),
                                                          "repair candidate")
                                  if attempt.get("candidate_checkout")
                                  else task_runtime.exact_root(Path(record["worktree"]),
                                                               "repair feature worktree"))
                validation_identity = full_validation_identity(
                    controller, config, record, candidate_root, candidate_sha)
                commands, routing = full_suite_selection(config, plan["candidate"]["changed_paths"])
                verified = verify_queue_failed_admission(
                    controller, task_id, plan, validation_identity, commands, routing, admission)
                receipt_ref = verified["receipts"][-1]
                receipt_path = Path(receipt_ref["receipt_path"])
                receipt_bytes = receipt_path.read_bytes()
                if hashlib.sha256(receipt_bytes).hexdigest() != receipt_ref["receipt_sha256"]:
                    raise MergeQueueError(
                        "deterministic full-suite repair refused (failed_receipt_moved)")
                failed_receipt = json.loads(receipt_bytes)
                finding = _deterministic_router_finding(
                    failed_receipt, failed_receipt.get("command", {}).get("cwd", ""))
                frozen_allowed = (record.get("creation_receipt") or {}).get(
                    "allowed_paths", config["allowed_paths"])
                if any(not task_runtime.path_within(path, frozen_allowed)
                       for path in finding["allowed_paths"]):
                    raise MergeQueueError(
                        "deterministic full-suite repair refused (repair_path_not_admitted)")
                producer_lock = receipt_path.parent / "producer.lock"
                if not producer_lock.is_file():
                    raise MergeQueueError(
                        "deterministic full-suite repair refused (producer_fencing_missing)")
                with producer_lock.open("r+b") as producer:
                    try:
                        fcntl.flock(producer.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError as exc:
                        raise MergeQueueError(
                            "deterministic full-suite repair refused (live_producer)") from exc
                    body = {
                        "schema_version": FULL_SUITE_REPAIR_SCHEMA, "task_id": task_id,
                        "record_revision": expected_record_revision,
                        "candidate_sha": candidate_sha, "candidate_tree": candidate_tree,
                        "target_ref": config["target_ref"], "target_sha": target_sha,
                        "failed_full_suite": receipt_ref, "finding": finding,
                        "arbiter": {"attempt": arbiter_attempt,
                                    "terminal_receipt": arbiter["terminal_receipt"],
                                    "predecessor_receipt": predecessor},
                        "lifecycle": {"run_id": run_id, "scope_sha256": scope_sha256,
                                      "journal_sha256": journal_sha256},
                        "budgets": {"repair_candidates": 1, "delta_review_groups": 1},
                    }
                    receipt_id = digest(body)
                    repair_path = (_full_suite_repair_receipt_root(controller) / task_id
                                   / f"{receipt_id}.json")
                    with task_runtime.state_lock(controller):
                        current = task_runtime.read_state(controller)
                        if current["tasks"].get(task_id) != record:
                            raise MergeQueueError(
                                "deterministic full-suite repair refused (revision_mismatch)")
                        reference = lifecycle_runtime.atomic_json(
                            repair_path, {**body, "receipt_id": receipt_id}, exclusive=True)
                        repair = {"schema_version": FULL_SUITE_REPAIR_SCHEMA,
                                  "status": "READY", "repair_count": 0,
                                  "delta_review_groups": 0,
                                  "authorization_receipt": reference,
                                  "finding": finding,
                                  "allowed_paths": finding["allowed_paths"]}
                        updated_risk = {**risk, "status": "REVIEW_FINDINGS",
                                        "full_suite_repair": repair}
                        updated_attempt = {**attempt, "risk": updated_risk,
                                           "review": updated_risk}
                        updated = {**record, "state": "REVIEW_FINDINGS",
                                   "queue_attempt": updated_attempt,
                                   "full_suite_repair": repair}
                        current["tasks"][task_id] = updated
                        task_runtime.write_state(controller, current)
                    return {**updated, "outcome": "FULL_SUITE_REPAIR_AUTHORIZED"}


def _repair_predispatch_evidence(controller: Path, repository: Path,
                                 config: dict[str, Any], task_id: str,
                                 record: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Project the exact pending semantic-repair no-provider incident, if present."""
    repair_authority = record.get("full_suite_repair")
    if (record.get("state") != "REVIEW_FINDINGS"
            or not isinstance(repair_authority, dict)
            or repair_authority.get("status") != "DISPATCHED"
            or repair_authority.get("repair_count") != 1
            or repair_authority.get("delta_review_groups") != 0):
        return None
    pointer_path = controller / MERGE_DRIVE_ROOT / "latest.json"
    arbiter_root = _arbiter_root(controller, repository, config["target_ref"])
    try:
        pointer = json.loads(pointer_path.read_text())
        run_id = pointer["run_id"]
        journal_path = controller / MERGE_DRIVE_ROOT / run_id / "journal.json"
        journal_bytes = journal_path.read_bytes(); journal = json.loads(journal_bytes)
        arbiter = _arbiter_state(arbiter_root)
        repairs = journal["repairs"]
        worker = repairs[0]
        worker_id = Path(worker["attempt_dir"]).name
        predispatch_path = Path(worker["attempt_dir"]) / "controller-predispatch-receipt.json"
        predispatch_bytes = predispatch_path.read_bytes()
        predispatch = json.loads(predispatch_bytes)
        terminal = arbiter["terminal_receipt"]
    except (OSError, KeyError, IndexError, TypeError, json.JSONDecodeError):
        return None
    if (not isinstance(arbiter, dict) or arbiter.get("state") != "FAILED"
            or not isinstance(arbiter.get("attempt"), int)
            or journal.get("run_id") != run_id
            or journal.get("scope_sha256") != pointer.get("scope_sha256")
            or len(repairs) != 1 or worker.get("task_id") != task_id
            or worker.get("kind") != "semantic_repair" or worker.get("index") != 1
            or worker.get("terminal_state") is not None
            or worker.get("predispatch_recovery") is not None
            or worker_id != "semantic-repair-0001"
            or predispatch.get("provider_launch_observed") is not False
            or predispatch.get("model_budget_consumed") is not False
            or ((Path(worker["attempt_dir"]) / "managed-agent").exists()
                and any((Path(worker["attempt_dir"]) / "managed-agent").iterdir()))
            or not isinstance(terminal, dict)
            or terminal.get("path") != str((arbiter_root / "receipts" /
                f"attempt-{arbiter['attempt']}-failed.json").resolve())):
        return None
    return {"run_id": run_id, "scope_sha256": journal["scope_sha256"],
            "journal_path": journal_path,
            "journal_sha256": hashlib.sha256(journal_bytes).hexdigest(),
            "worker_id": worker_id, "predispatch_path": predispatch_path,
            "predispatch_sha256": hashlib.sha256(predispatch_bytes).hexdigest(),
            "arbiter_attempt": arbiter["attempt"], "terminal": terminal}


def _repair_predispatch_safe_next(controller: Path, repository: Path,
                                  config: dict[str, Any], task_id: str,
                                  record: dict[str, Any]) -> Optional[dict[str, Any]]:
    evidence = _repair_predispatch_evidence(controller, repository, config, task_id, record)
    if evidence is None:
        return None
    command = " ".join([
        "yy merge recover-repair-predispatch", shlex.quote(task_id),
        "--attempt", str(evidence["arbiter_attempt"]),
        "--terminal-receipt", shlex.quote(evidence["terminal"]["path"]),
        "--terminal-receipt-sha256", evidence["terminal"]["sha256"],
        "--expected-revision", digest(record),
        "--run-id", shlex.quote(evidence["run_id"]),
        "--scope-sha256", evidence["scope_sha256"],
        "--journal-sha256", evidence["journal_sha256"],
        "--worker-id", evidence["worker_id"],
        "--predispatch-receipt", shlex.quote(str(evidence["predispatch_path"])),
        "--predispatch-receipt-sha256", evidence["predispatch_sha256"],
    ])
    return {"reason_code": "repair_predispatch_recovery_available",
            "safe_next_command": command}


def _repair_predispatch_refuse(code: str) -> None:
    raise MergeQueueError(f"repair pre-dispatch recovery refused ({code})")


def recover_repair_predispatch(
        controller: Path, task_id: str, arbiter_attempt: int,
        terminal_receipt_path: str, terminal_receipt_sha256: str,
        expected_record_revision: str, run_id: str, scope_sha256: str,
        journal_sha256: str, worker_id: str, predispatch_receipt_path: str,
        predispatch_receipt_sha256: str) -> dict[str, Any]:
    """Restore only the existing semantic-repair worker's dispatch eligibility."""
    hashes = (terminal_receipt_sha256, expected_record_revision, scope_sha256,
              journal_sha256, predispatch_receipt_sha256)
    if (not task_runtime.TASK_RE.fullmatch(task_id)
            or not isinstance(arbiter_attempt, int) or isinstance(arbiter_attempt, bool)
            or arbiter_attempt < 1
            or not re.fullmatch(r"[0-9]+-[0-9a-f]+", run_id or "")
            or worker_id != "semantic-repair-0001"
            or not all(re.fullmatch(r"[0-9a-f]{64}", value or "") for value in hashes)):
        _repair_predispatch_refuse("malformed_request")
    controller_status = task_runtime.git(
        controller, "status", "--porcelain=v1", "--untracked-files=all", check=False)
    # The CLI writes its append-only control-audit receipt before dispatching
    # the operation. That exact audit namespace is evidence of this call, not
    # pre-existing controller dirt; every other tracked or untracked byte is a
    # hard refusal.
    controller_dirty = "\n".join(
        line for line in controller_status.splitlines()
        if ".juno_task/runtime/control-audit/" not in line)
    config = task_runtime.load_config(controller)
    repository = task_runtime.product_repository(controller, config)
    arbiter_root = _arbiter_root(controller, repository, config["target_ref"])
    expected_terminal = (arbiter_root / "receipts"
                         / f"attempt-{arbiter_attempt}-failed.json").resolve()
    supplied_terminal = Path(terminal_receipt_path).expanduser().resolve()
    try:
        terminal_bytes = supplied_terminal.read_bytes(); terminal = json.loads(terminal_bytes)
    except (OSError, json.JSONDecodeError):
        _repair_predispatch_refuse("receipt_malformed")
    if (supplied_terminal != expected_terminal
            or hashlib.sha256(terminal_bytes).hexdigest() != terminal_receipt_sha256
            or terminal.get("schema_version") != TARGET_ARBITER_RECEIPT_SCHEMA
            or terminal.get("attempt") != arbiter_attempt
            or terminal.get("target_ref") != config["target_ref"]
            or terminal.get("state") != "FAILED"):
        _repair_predispatch_refuse("receipt_malformed")
    with review_lock(repository, task_id):
        with _target_arbiter_claim(arbiter_root) as claim:
            if claim is None:
                _repair_predispatch_refuse("live_producer")
            with target_lock(controller, repository, config["target_ref"]):
                arbiter = _arbiter_state(arbiter_root)
                if (not isinstance(arbiter, dict) or arbiter.get("attempt") != arbiter_attempt
                        or arbiter.get("state") != "FAILED"
                        or arbiter.get("terminal_receipt") != {
                            "path": str(supplied_terminal), "sha256": terminal_receipt_sha256}
                        or arbiter.get("producer") != terminal.get("producer")
                        or arbiter.get("detail") != terminal.get("detail")):
                    _repair_predispatch_refuse("arbiter_identity_moved")
                observation = task_runtime._observe_producer(arbiter.get("producer"))
                if observation.status != "dead":
                    _repair_predispatch_refuse("live_producer")
                error = terminal.get("detail", {}).get("error") \
                    if isinstance(terminal.get("detail"), dict) else None
                if error != "managed task worker was refused before provider dispatch":
                    _repair_predispatch_refuse("provider_evidence")
                run_dir = controller / MERGE_DRIVE_ROOT / run_id
                journal_path = run_dir / "journal.json"
                try:
                    initial_raw = journal_path.read_bytes(); journal = json.loads(initial_raw)
                    pointer = json.loads((controller / MERGE_DRIVE_ROOT / "latest.json").read_text())
                except (OSError, json.JSONDecodeError):
                    _repair_predispatch_refuse("lifecycle_identity_moved")
                repairs = journal.get("repairs") if isinstance(journal, dict) else None
                if isinstance(repairs, list) and len(repairs) == 1 \
                        and isinstance(repairs[0], dict) \
                        and repairs[0].get("predispatch_recovery") is not None:
                    _repair_predispatch_refuse("already_recovered")
                if controller_dirty:
                    _repair_predispatch_refuse("dirty_controller")
                controller_identity = {
                    "head": task_runtime.git(controller, "rev-parse", "HEAD"),
                    "tree": task_runtime.git(controller, "rev-parse", "HEAD^{tree}"),
                    "branch_ref": task_runtime.git(controller, "symbolic-ref", "-q", "HEAD"),
                    "clean": True, "operation_audit_excluded": True,
                }
                if (hashlib.sha256(initial_raw).hexdigest() != journal_sha256
                        or pointer.get("run_id") != run_id
                        or pointer.get("scope_sha256") != scope_sha256
                        or journal.get("run_id") != run_id
                        or journal.get("scope_sha256") != scope_sha256
                        or journal.get("state") != "CLAIMED" or journal.get("terminal") is True
                        or journal.get("attempts", {}).get("semantic_repairs") != 1
                        or not isinstance(repairs, list) or len(repairs) != 1
                        or any(row.get("post_state") == "MERGED" for row in
                               journal.get("operations", []) if isinstance(row, dict))):
                    _repair_predispatch_refuse("lifecycle_identity_moved")
                worker = repairs[0]
                worker_dir = (run_dir / "workers" / worker_id).resolve()
                if (worker.get("kind") != "semantic_repair" or worker.get("index") != 1
                        or worker.get("task_id") != task_id
                        or worker.get("terminal_state") is not None
                        or Path(str(worker.get("attempt_dir", ""))).resolve() != worker_dir
                        or worker_dir.name != worker_id):
                    _repair_predispatch_refuse("worker_identity_moved")
                with task_runtime.state_lock(controller):
                    state = task_runtime.read_state(controller); record = state["tasks"].get(task_id)
                if not isinstance(record, dict) or digest(record) != expected_record_revision:
                    _repair_predispatch_refuse("revision_mismatch")
                attempt = record.get("queue_attempt")
                risk = attempt.get("risk") if isinstance(attempt, dict) else None
                repair = record.get("full_suite_repair")
                if (record.get("state") in {"CONFLICT", "CONFLICT_RESOLVED", "MERGING", "MERGED"}
                        or not isinstance(attempt, dict)
                        or record.get("state") != "REVIEW_FINDINGS"
                        or attempt.get("outcome") == "MERGED"):
                    _repair_predispatch_refuse("conflict_or_post_cas")
                if (not isinstance(repair, dict)
                        or repair.get("schema_version") != FULL_SUITE_REPAIR_SCHEMA
                        or repair.get("status") != "DISPATCHED"
                        or repair.get("repair_count") != 1):
                    _repair_predispatch_refuse("repair_budget")
                if repair.get("delta_review_groups") != 0 or record.get("review_round", 1) != 1:
                    _repair_predispatch_refuse("delta_budget")
                if (risk.get("full_suite_repair") != repair
                        or attempt.get("review", {}).get("full_suite_repair") != repair
                        or worker.get("authorization_receipt") != repair.get("authorization_receipt")):
                    _repair_predispatch_refuse("repair_authorization_moved")
                candidate_sha = attempt.get("candidate_sha")
                candidate_tree = attempt.get("candidate_tree")
                target_sha = task_runtime.ref_sha(repository, config["target_ref"])
                if (arbiter.get("target_sha_at_start") != target_sha
                        or attempt.get("expected_target_sha") != target_sha
                        or task_runtime.git(repository, "rev-parse", f"{candidate_sha}^{{tree}}",
                                            check=False) != candidate_tree):
                    _repair_predispatch_refuse("conflict_or_post_cas")
                authorization = repair["authorization_receipt"]
                try:
                    authorization_path = Path(authorization["path"]).resolve()
                    authorization_bytes = authorization_path.read_bytes()
                    authorization_value = json.loads(authorization_bytes)
                except (KeyError, OSError, json.JSONDecodeError):
                    _repair_predispatch_refuse("repair_authorization_moved")
                if (hashlib.sha256(authorization_bytes).hexdigest() != authorization.get("sha256")
                        or authorization_value.get("schema_version") != FULL_SUITE_REPAIR_SCHEMA
                        or authorization_value.get("task_id") != task_id
                        or authorization_value.get("candidate_sha") != candidate_sha
                        or authorization_value.get("candidate_tree") != candidate_tree
                        or authorization_value.get("target_sha") != target_sha
                        or authorization_value.get("lifecycle", {}).get("run_id") != run_id
                        or authorization_value.get("lifecycle", {}).get("scope_sha256") != scope_sha256):
                    _repair_predispatch_refuse("repair_authorization_moved")
                before_sha = worker.get("before_sha"); worktree = Path(record["worktree"])
                if (before_sha != record.get("tip_sha")
                        or task_runtime.git(worktree, "rev-parse", "HEAD", check=False) != before_sha
                        or task_runtime.git(worktree, "status", "--porcelain=v1",
                                            "--untracked-files=all", check=False)):
                    _repair_predispatch_refuse("worker_identity_moved")
                supplied_predispatch = Path(predispatch_receipt_path).expanduser().resolve()
                expected_predispatch = worker_dir / "controller-predispatch-receipt.json"
                if supplied_predispatch != expected_predispatch:
                    _repair_predispatch_refuse("receipt_malformed")
                try:
                    reference = task_runtime._pending_predispatch_receipt(record, worker)
                except task_runtime.TaskWorkspaceError as exc:
                    _repair_predispatch_refuse(
                        "provider_evidence" if "provider launch evidence" in str(exc)
                        else "receipt_malformed")
                if (reference != {"path": str(supplied_predispatch),
                                  "sha256": predispatch_receipt_sha256}):
                    _repair_predispatch_refuse("receipt_malformed")
                predispatch = json.loads(supplied_predispatch.read_bytes())
                names = ("create-receipt.json", "verify-receipt.json",
                         "edit-preflight-receipt.json")
                refs = predispatch.get("admission_receipts")
                if (not isinstance(refs, list) or len(refs) != 3
                        or any(Path(ref["path"]).resolve() != worker_dir / name
                               for ref, name in zip(refs, names))):
                    _repair_predispatch_refuse("receipt_malformed")
                values = []
                for ref in refs:
                    try:
                        path = Path(ref["path"]); data = path.read_bytes(); value = json.loads(data)
                    except (KeyError, OSError, json.JSONDecodeError):
                        _repair_predispatch_refuse("receipt_malformed")
                    if hashlib.sha256(data).hexdigest() != ref.get("sha256"):
                        _repair_predispatch_refuse("receipt_malformed")
                    values.append(value)
                create, verify, edit = values
                if (create.get("schema_version") != "juno_managed_task_run_create.v1"
                        or create.get("task_id") != task_id
                        or Path(create.get("worktree", "")).resolve() != worktree.resolve()
                        or create.get("branch_ref") != record.get("branch_ref")
                        or create.get("clean_tip_sha") != before_sha
                        or verify.get("schema_version") != "juno_managed_task_run_verify.v1"
                        or verify.get("task_id") != task_id or verify.get("passed") is not True
                        or verify.get("tip_sha") != before_sha
                        or verify.get("create_receipt_sha256") != refs[0]["sha256"]
                        or edit.get("schema_version") != "juno_managed_task_run_edit_preflight.v1"
                        or edit.get("task_id") != task_id or edit.get("passed") is not True
                        or edit.get("tip_sha") != before_sha
                        or edit.get("create_receipt_sha256") != refs[0]["sha256"]
                        or edit.get("verify_receipt_sha256") != refs[1]["sha256"]):
                    _repair_predispatch_refuse("receipt_malformed")
                managed_dir = worker_dir / "managed-agent"
                if managed_dir.exists() and any(managed_dir.iterdir()):
                    _repair_predispatch_refuse("provider_evidence")
                if (predispatch.get("provider_launch_observed") is not False
                        or predispatch.get("model_budget_consumed") is not False):
                    _repair_predispatch_refuse("provider_evidence")
                body = {
                    "schema_version": REPAIR_PREDISPATCH_RECOVERY_SCHEMA,
                    "task_id": task_id, "record_revision": expected_record_revision,
                    "candidate_sha": candidate_sha, "candidate_tree": candidate_tree,
                    "target_ref": config["target_ref"], "target_sha": target_sha,
                    "repair_authorization": authorization,
                    "worker": {"id": worker_id, "path": str(worker_dir),
                               "before_sha": before_sha},
                    "admission_receipts": refs, "predispatch_receipt": reference,
                    "arbiter": {"attempt": arbiter_attempt,
                                "terminal_receipt": arbiter["terminal_receipt"]},
                    "lifecycle": {"run_id": run_id, "scope_sha256": scope_sha256,
                                  "journal_sha256": journal_sha256},
                    "controller_identity": controller_identity,
                    "provider_launch_observed": False, "model_budget_consumed": False,
                    "repair_count": 1, "delta_review_groups": 0,
                    "reason_code": "same_worker_redispatch_ready",
                    "safe_next_command": f"yy merge arbiter run --through {task_id}",
                }
                body["projection_sha256"] = digest(body)
                projection_index = len(journal.get("projections", [])) + 1
                projection_path = (run_dir / "projections" /
                                   f"{projection_index:04d}-repair-predispatch-recovered.json")
                expected_bytes = lifecycle_runtime.canonical_bytes(body)
                if projection_path.is_file():
                    if projection_path.read_bytes() != expected_bytes:
                        _repair_predispatch_refuse("projection_collision")
                    projection = {"path": str(projection_path.resolve()),
                                  "sha256": hashlib.sha256(expected_bytes).hexdigest()}
                else:
                    projection = lifecycle_runtime.atomic_json(
                        projection_path, body, exclusive=True)
                with lifecycle_runtime.lifecycle_claim(run_dir / ".claim.lock"):
                    if journal_path.read_bytes() != initial_raw:
                        _repair_predispatch_refuse("lifecycle_identity_moved")
                    with task_runtime.state_lock(controller):
                        current_record = task_runtime.read_state(controller)["tasks"].get(task_id)
                    if (not isinstance(current_record, dict)
                            or digest(current_record) != expected_record_revision
                            or task_runtime.ref_sha(repository, config["target_ref"]) != target_sha
                            or task_runtime.git(worktree, "rev-parse", "HEAD", check=False) != before_sha
                            or task_runtime.git(worktree, "status", "--porcelain=v1",
                                                "--untracked-files=all", check=False)
                            or hashlib.sha256(supplied_predispatch.read_bytes()).hexdigest()
                               != predispatch_receipt_sha256
                            or any(hashlib.sha256(Path(ref["path"]).read_bytes()).hexdigest()
                                   != ref["sha256"] for ref in refs)):
                        _repair_predispatch_refuse("worker_identity_moved")
                    current_repair = journal["repairs"][0]
                    current_repair["predispatch_recovery"] = {
                        "schema_version": REPAIR_PREDISPATCH_RECOVERY_SCHEMA,
                        "status": "READY", "projection": projection,
                        "predispatch_receipt": reference,
                    }
                    journal.setdefault("projections", []).append(projection)
                    journal.setdefault("events", []).append({
                        "schema_version": "juno_lifecycle_phase_checkpoint.v1",
                        "sequence": len(journal.get("events", [])) + 1,
                        "phase": "semantic-repair-1-predispatch-recovery", "boundary": "POST",
                        "recorded_at_unix_ns": time.time_ns(),
                        "detail": {"worker_id": worker_id, "projection": projection,
                                   "provider_launch_observed": False,
                                   "model_budget_consumed": False}})
                    lifecycle_runtime.lifecycle_journal_write(journal_path, journal)
                return {**body, "outcome": "REPAIR_PREDISPATCH_RECOVERED",
                        "projection": projection}


def merge_reopen(controller: Path, task_id: str,
                 expected_plan_id: Optional[str] = None) -> dict[str, Any]:
    """Recoverable two-phase requeue after a new validated feature tip."""
    if not task_runtime.TASK_RE.fullmatch(task_id):
        raise MergeQueueError("unsafe task id")
    with task_runtime.state_lock(controller):
        addressed = task_runtime.read_state(controller)["tasks"].get(task_id)
    if not isinstance(addressed, dict) or addressed.get("state") not in {
            "REVIEW_FINDINGS", "REOPENING", "REQUEUING_STALE", "AWAITING_RISK",
            "QUEUED", "CONFLICT_RESOLVED", "REVIEW_FINDINGS_EXHAUSTED"}:
        raise MergeQueueError("task has no review findings or failed queue repair to reopen")
    assert_static_plan(controller, task_id, "reopen", expected_plan_id)
    config = task_runtime.load_config(controller)
    repository = task_runtime.product_repository(controller, config)
    with target_lock(controller, repository, config["target_ref"]):
        with task_runtime.state_lock(controller):
            state = task_runtime.read_state(controller)
            record = state["tasks"].get(task_id)
            conflict = target_entry(state, repository, config["target_ref"])["conflicts"].get(task_id)
        source_state = record.get("state") if isinstance(record, dict) else None
        failure_outcome = record.get("last_queue_outcome") if isinstance(record, dict) else None
        if source_state == "REQUEUING_STALE":
            if record.get("task_id") != task_id:
                raise MergeQueueError("REQUEUING_STALE task identity drifted")
            stale = record.get("stale_requeue")
            observed = (stale.get("observed_target_sha") if isinstance(stale, dict)
                        else task_runtime.ref_sha(repository, config["target_ref"]))
            return requeue_stale_candidate(controller, config, repository, record, observed)
        queue_attempt = record.get("queue_attempt") if isinstance(record, dict) else None
        legacy_stale_resolved = (
            source_state == "CONFLICT_RESOLVED"
            and failure_outcome == "STALE_TARGET"
            and isinstance(queue_attempt, dict)
            and queue_attempt.get("outcome") == "STALE_TARGET"
            and "dependency_lock_refusal" not in queue_attempt
            and isinstance(conflict, dict)
            and conflict.get("resolution_state") == "RESOLVED"
        )
        if legacy_stale_resolved:
            current_target_sha = task_runtime.ref_sha(repository, config["target_ref"])
            if current_target_sha == queue_attempt.get("expected_target_sha"):
                raise MergeQueueError(
                    "legacy stale resolved candidate is ambiguous while target has not moved")
            return requeue_stale_candidate(
                controller, config, repository, record, current_target_sha)
        resolved_validation_repair = (
            source_state == "CONFLICT_RESOLVED"
            and failure_outcome in {"FAILED_TEST", "STALE_TARGET"}
            and isinstance(queue_attempt, dict)
            and queue_attempt.get("outcome") == failure_outcome
            and (failure_outcome == "FAILED_TEST"
                 or isinstance(queue_attempt.get("dependency_lock_refusal"), dict))
            and isinstance(conflict, dict)
            and conflict.get("resolution_state") == "RESOLVED"
        )
        failed_queue_repair = (
            (((source_state == "AWAITING_RISK"
               and failure_outcome in {"FAILED_FULL_SUITE", "REVIEW_FAILED"})
              or (source_state == "QUEUED" and failure_outcome == "FAILED_TEST"))
             and isinstance(record.get("queue_attempt"), dict)
             and record["queue_attempt"].get("outcome") == failure_outcome)
            or resolved_validation_repair
        )
        queued_tip_refresh = (
            source_state == "QUEUED"
            and isinstance(record.get("tip_sha"), str)
            and not failed_queue_repair
        )
        if source_state == "REVIEW_FINDINGS_EXHAUSTED":
            raise MergeQueueError(
                "bounded semantic review budget exhausted; inspect the consolidated evidence "
                "and create a newly authorized follow-up task"
            )
        if (source_state not in {"REVIEW_FINDINGS", "REOPENING"}
                and not failed_queue_repair and not queued_tip_refresh):
            raise MergeQueueError("task has no review findings or failed queue repair to reopen")
        if source_state in {"REVIEW_FINDINGS", "AWAITING_RISK", "QUEUED", "CONFLICT_RESOLVED"}:
            old_attempt = record.get("queue_attempt")
            if not isinstance(old_attempt, dict):
                if queued_tip_refresh:
                    old_attempt = {
                        "candidate_sha": record["tip_sha"],
                        "candidate_checkout": None,
                        "candidate_token": None,
                        "outcome": "QUEUED_TIP_REFRESH",
                    }
                else:
                    raise MergeQueueError("review finding task has no frozen queue attempt")
            worktree = task_runtime.exact_root(Path(record["worktree"]), "feature worktree")
            if (record.get("task_id") != task_id
                    or record.get("target_ref") != config["target_ref"]
                    or Path(record["repository"]).resolve() != repository):
                raise MergeQueueError("feature task/repository/target identity drifted")
            expected_target_sha = old_attempt.get("expected_target_sha")
            target_sha = task_runtime.ref_sha(repository, config["target_ref"])
            if resolved_validation_repair:
                if (conflict.get("repository_identity") != repository_identity(repository)
                        or conflict.get("target_ref") != config["target_ref"]
                        or conflict.get("task_id") != task_id
                        or conflict.get("feature_sha") != record.get("tip_sha")
                        or conflict.get("resolved_candidate_sha") != old_attempt.get("candidate_sha")
                        or expected_target_sha != conflict.get("expected_target_sha")):
                    raise MergeQueueError("resolved conflict repair identity drifted")
                if target_sha != expected_target_sha:
                    return requeue_stale_candidate(
                        controller, config, repository, record, target_sha)
            if task_runtime.git(worktree, "symbolic-ref", "-q", "HEAD", check=False) != record["branch_ref"]:
                raise MergeQueueError("feature worktree branch identity drifted")
            new_tip = task_runtime.git(worktree, "rev-parse", "HEAD")
            if task_runtime.git(repository, "rev-parse", record["branch_ref"], check=False) != new_tip:
                raise MergeQueueError("feature branch tip identity drifted")
            if new_tip == record["tip_sha"]:
                raise MergeQueueError("reopen requires a new committed feature tip")
            if task_runtime.git(worktree, "status", "--porcelain=v1", "--untracked-files=all"):
                raise MergeQueueError("feature worktree must be clean before reopen")
            if task_runtime.run(["git", "-C", str(repository), "merge-base", "--is-ancestor",
                                 record["tip_sha"], new_tip], repository, check=False).returncode:
                raise MergeQueueError("new feature tip must descend from the queued or reviewed tip")
            prior_failure = record.get("prior_queue_failure")
            if (queued_tip_refresh and isinstance(prior_failure, dict)
                    and prior_failure.get("outcome") == "STALE_TARGET"):
                legacy_attempt = prior_failure.get("legacy_queue_attempt")
                legacy_refusal = (
                    prior_failure.get("legacy_refusal_schema")
                        == "juno_merge_queue_legacy_unstructured_lock_refusal.v1"
                    and isinstance(legacy_attempt, dict)
                    and "dependency_lock_refusal" not in legacy_attempt
                    and legacy_attempt.get("schema_version") == "juno_merge_queue_attempt.v1"
                    and legacy_attempt.get("outcome") == "STALE_TARGET"
                    and legacy_attempt.get("task_id") == task_id
                    and legacy_attempt.get("target_ref") == config["target_ref"]
                    and legacy_attempt.get("feature_sha") == record.get("tip_sha")
                    and legacy_attempt.get("candidate_sha") == prior_failure.get("candidate_sha")
                    and legacy_attempt.get("expected_target_sha")
                        == prior_failure.get("expected_target_sha")
                    and isinstance(legacy_attempt.get("candidate_checkout"), str)
                    and isinstance(legacy_attempt.get("candidate_token"), str)
                    and task_runtime.SHA_RE.fullmatch(
                        str(legacy_attempt.get("candidate_tree"))) is not None
                )
                if not legacy_refusal:
                    refusal = prior_failure.get("dependency_lock_refusal")
                    lock_path = refusal.get("lock_path") if isinstance(refusal, dict) else None
                    if (not isinstance(lock_path, str) or Path(lock_path).is_absolute()
                            or ".." in Path(lock_path).parts):
                        raise MergeQueueError(
                            "dependency-lock refusal identity drifted before tip refresh")
                    candidate_blob = refusal.get("candidate_blob", "")
                    source_blob = task_runtime.git(
                        repository, "rev-parse", f"{record.get('tip_sha')}:{lock_path}", check=False)
                    expected_refusal = {
                        "schema_version": "juno_merge_queue_dependency_lock_refusal.v1",
                        "lock_path": lock_path,
                        "candidate_head": prior_failure.get("candidate_sha"),
                        "candidate_blob": candidate_blob,
                        "candidate_sha256": git_blob_digest(repository, candidate_blob),
                        "source_head": record.get("tip_sha"),
                        "source_blob": source_blob,
                        "source_sha256": git_blob_digest(repository, source_blob),
                    }
                    new_lock = worktree / lock_path
                    new_blob = task_runtime.git(
                        worktree, "rev-parse", f"HEAD:{lock_path}", check=False)
                    if (refusal != expected_refusal
                            or len(canonical(refusal).encode()) > 4096
                            or refusal.get("candidate_sha256") == refusal.get("source_sha256")
                            or file_digest(new_lock) != refusal.get("candidate_sha256")
                            or new_blob != candidate_blob):
                        raise MergeQueueError(
                            "dependency-lock refusal identity drifted before tip refresh")
            # A normal findings/validation repair remains authored against the
            # task's immutable creation base. A receipt-bound target refresh,
            # however, deliberately incorporated exact protected-target bytes;
            # exclude those inherited bytes from the repaired feature admission.
            refresh_evidence = _current_target_refresh_receipt(
                controller, task_id, record, record["tip_sha"], target_sha)
            target_refreshed_repair = (
                source_state == "REVIEW_FINDINGS" and refresh_evidence["valid"])
            if (source_state == "REVIEW_FINDINGS" and record.get("target_refreshes")
                    and not refresh_evidence["valid"]):
                raise MergeQueueError(
                    "review repair target-refresh identity is invalid: "
                    f"{refresh_evidence['reason']}")
            # A reopen-based tip refresh adopted a descendant tip that already
            # contained the protected target without recording a receipt-bound
            # reference (legacy queue states). When the reviewed tip still
            # contains the current target, inherit only bytes authored after
            # the target entered the tip's history: diff from the exact merge
            # base of the reviewed tip and the target.
            reviewed_tip_contains_target = (
                source_state == "REVIEW_FINDINGS"
                and not record.get("target_refreshes")
                and not task_runtime.run(
                    ["git", "-C", str(repository), "merge-base", "--is-ancestor",
                     target_sha, record["tip_sha"]], repository, check=False).returncode)
            changed_base = (target_sha
                            if queued_tip_refresh or target_refreshed_repair
                            else task_runtime.git(repository, "merge-base",
                                                  record["tip_sha"], target_sha)
                            if reviewed_tip_contains_target
                            else record["base_sha"])
            changed = sorted(set(task_runtime.git(
                worktree, "diff", "--name-only", f"{changed_base}..{new_tip}"
            ).splitlines()))
            full_suite_repair = record.get("full_suite_repair")
            if full_suite_repair is not None:
                delta_paths = sorted(set(task_runtime.git(
                    worktree, "diff", "--name-only", f"{record['tip_sha']}..{new_tip}"
                ).splitlines()))
                if (not isinstance(full_suite_repair, dict)
                        or full_suite_repair.get("schema_version") != FULL_SUITE_REPAIR_SCHEMA
                        or full_suite_repair.get("status") != "DISPATCHED"
                        or full_suite_repair.get("repair_count") != 1
                        or full_suite_repair.get("delta_review_groups") != 0
                        or not delta_paths
                        or any(path not in full_suite_repair.get("allowed_paths", [])
                               for path in delta_paths)):
                    raise MergeQueueError(
                        "full-suite repair delta is unrelated or its absolute budget is exhausted")
            forbidden = [path for path in changed
                         if task_runtime.path_within(path, config["controller_private_paths"])]
            frozen_allowed = (record.get("creation_receipt") or {}).get(
                "allowed_paths", config["allowed_paths"])
            outside = [path for path in changed
                       if not task_runtime.path_within(path, frozen_allowed)]
            if not changed or forbidden or outside:
                raise MergeQueueError("reopened feature tip has empty or disallowed product changes")
            validations, command_evidence = authoritative_validation_rows(
                controller, config, repository, record, worktree, new_tip)
            if task_runtime.git(worktree, "rev-parse", "HEAD") != new_tip \
                    or task_runtime.git(worktree, "status", "--porcelain=v1", "--untracked-files=all"):
                raise MergeQueueError("feature tip changed during reopen validation")
            target_refresh_reference: Optional[dict[str, Any]] = None
            if (queued_tip_refresh and target_sha != record["base_sha"]
                    and not task_runtime.run(
                        ["git", "-C", str(repository), "merge-base", "--is-ancestor",
                         target_sha, new_tip], repository, check=False).returncode):
                # The adopted tip already contains the advanced protected
                # target, so this reopen is a target refresh. Persist the
                # canonical receipt and record the reference so a later
                # review-repair reopen validates through
                # _current_target_refresh_receipt instead of classifying
                # inherited exact target bytes as authored changes.
                persisted = persist_target_refresh_plan(controller, task_id)
                static = assert_static_plan(controller, task_id, "target-refresh")
                target_refresh_reference = {
                    "schema_version": "juno_merge_target_refresh_reference.v1",
                    "plan_id": persisted["plan_id"],
                    "receipt_path": persisted["receipt"]["path"],
                    "receipt_sha256": persisted["receipt"]["sha256"],
                    "source_tip": persisted["source_tip"],
                    "target_sha": persisted["target_sha"],
                    "refreshed_tip": persisted["refreshed_tip"],
                    "feasibility_plan_id": static["plan_id"],
                }
            checkout_value, token = old_attempt.get("candidate_checkout"), old_attempt.get("candidate_token")
            owner = (read_candidate_owner(controller, Path(checkout_value))
                     if checkout_value else None)
            if resolved_validation_repair:
                checkout = task_runtime.exact_root(Path(str(checkout_value)), "resolved candidate checkout")
                if (not token or task_runtime.git(checkout, "rev-parse", "HEAD", check=False)
                        != old_attempt.get("candidate_sha")
                        or task_runtime.git(checkout, "status", "--porcelain=v1", "--untracked-files=all",
                                            check=False)):
                    raise MergeQueueError("resolved candidate identity drifted before reopen")
                if failure_outcome == "STALE_TARGET":
                    refusal = old_attempt.get("dependency_lock_refusal")
                    lock_path = refusal.get("lock_path") if isinstance(refusal, dict) else None
                    if (not isinstance(lock_path, str) or Path(lock_path).is_absolute()
                            or ".." in Path(lock_path).parts):
                        raise MergeQueueError("dependency-lock refusal identity drifted before reopen")
                    expected_refusal = {
                        "schema_version": "juno_merge_queue_dependency_lock_refusal.v1",
                        "lock_path": lock_path,
                        "candidate_head": old_attempt.get("candidate_sha"),
                        "candidate_blob": (task_runtime.git(
                            checkout, "rev-parse", f"HEAD:{lock_path}", check=False)
                            if isinstance(lock_path, str) else ""),
                        "candidate_sha256": (file_digest(checkout / lock_path)
                                             if isinstance(lock_path, str) else None),
                        "source_head": record.get("tip_sha"),
                        "source_blob": (task_runtime.git(
                            repository, "rev-parse", f"{record.get('tip_sha')}:{lock_path}", check=False)
                            if isinstance(lock_path, str) else ""),
                        "source_sha256": (git_blob_digest(repository, refusal.get("source_blob", ""))
                                          if isinstance(refusal, dict) else None),
                    }
                    new_lock = worktree / lock_path if isinstance(lock_path, str) else worktree
                    new_blob = (task_runtime.git(worktree, "rev-parse", f"HEAD:{lock_path}", check=False)
                                if isinstance(lock_path, str) else "")
                    if (refusal != expected_refusal
                            or len(canonical(refusal).encode()) > 4096
                            or refusal.get("candidate_sha256") == refusal.get("source_sha256")
                            or not isinstance(lock_path, str)
                            or Path(lock_path).is_absolute() or ".." in Path(lock_path).parts
                            or file_digest(new_lock) != refusal.get("candidate_sha256")
                            or new_blob != refusal.get("candidate_blob")):
                        raise MergeQueueError("dependency-lock refusal identity drifted before reopen")
                expected_owner = {
                    "task_id": task_id, "token": token,
                    "repository_identity": repository_identity(repository),
                    "target_ref": config["target_ref"], "target_sha": expected_target_sha,
                    "feature_sha": record["tip_sha"],
                    "candidate_checkout": str(checkout.resolve()),
                }
                if any(owner.get(key) != value for key, value in expected_owner.items()):
                    raise MergeQueueError("resolved candidate ownership mismatched")
                verify_candidate_owner(controller, repository, checkout, token)
            policy_bytes = (controller / ".juno_task/config/task-workspace.json").read_bytes()
            failure_evidence = None
            if resolved_validation_repair:
                failure_evidence = {
                    "schema_version": "juno_merge_queue_prior_failure.v1",
                    "outcome": failure_outcome,
                    "candidate_sha": old_attempt["candidate_sha"],
                    "expected_target_sha": expected_target_sha,
                    "validation": old_attempt.get("validation", []),
                }
                if failure_outcome == "STALE_TARGET":
                    failure_evidence["dependency_lock_refusal"] = old_attempt["dependency_lock_refusal"]
            reopen_attempt = {
                "schema_version": "juno_merge_queue_reopen.v1",
                "task_id": task_id,
                "old_candidate_sha": old_attempt["candidate_sha"],
                "old_candidate_checkout": checkout_value,
                "old_candidate_token": token,
                "old_candidate_owner": owner,
                "source_state": source_state,
                "source_outcome": old_attempt.get("outcome"),
                "source_failure_evidence": failure_evidence,
                "repository_identity": repository_identity(repository),
                "repository": str(repository),
                "target_ref": config["target_ref"],
                "expected_target_sha": expected_target_sha,
                "worktree": str(worktree),
                "branch_ref": record["branch_ref"],
                "new_feature_tip": new_tip,
                "changed_paths": changed,
                "validations": validations, "command_evidence": command_evidence,
                "validation_identity": digest({
                    "new_feature_tip": new_tip, "changed_paths": changed,
                    "task_workspace_policy_sha256": hashlib.sha256(policy_bytes).hexdigest(),
                    "focused_validation": task_runtime.selected_focused_rows(config, changed),
                }),
            }
            if target_refresh_reference is not None:
                reopen_attempt["target_refresh_reference"] = target_refresh_reference
            reopening = {**record, "state": "REOPENING", "reopen_attempt": reopen_attempt}
            with task_runtime.state_lock(controller):
                state = task_runtime.read_state(controller)
                if state["tasks"].get(task_id) != record:
                    raise MergeQueueError("task state changed before reopen admission")
                state["tasks"][task_id] = reopening
                task_runtime.write_state(controller, state)
            record = reopening
        reopen_attempt = record.get("reopen_attempt")
        if (not isinstance(reopen_attempt, dict)
                or reopen_attempt.get("schema_version") != "juno_merge_queue_reopen.v1"):
            raise MergeQueueError("REOPENING task has invalid recovery identity")
        worktree = task_runtime.exact_root(Path(record["worktree"]), "feature worktree")
        new_tip = reopen_attempt["new_feature_tip"]
        policy_bytes = (controller / ".juno_task/config/task-workspace.json").read_bytes()
        expected_validation_identity = digest({
            "new_feature_tip": new_tip, "changed_paths": reopen_attempt["changed_paths"],
            "task_workspace_policy_sha256": hashlib.sha256(policy_bytes).hexdigest(),
            "focused_validation": task_runtime.selected_focused_rows(
                config, reopen_attempt["changed_paths"]),
        })
        if (reopen_attempt.get("repository_identity") != repository_identity(repository)
                or reopen_attempt.get("repository") != str(repository)
                or reopen_attempt.get("target_ref") != config["target_ref"]
                or reopen_attempt.get("worktree") != str(worktree)
                or reopen_attempt.get("branch_ref") != record.get("branch_ref")
                or reopen_attempt.get("validation_identity") != expected_validation_identity
                or task_runtime.git(worktree, "symbolic-ref", "-q", "HEAD", check=False)
                    != record.get("branch_ref")
                or task_runtime.git(worktree, "rev-parse", "HEAD", check=False) != new_tip
                or task_runtime.git(repository, "rev-parse", record["branch_ref"], check=False) != new_tip
                or task_runtime.git(worktree, "status", "--porcelain=v1", "--untracked-files=all")):
            raise MergeQueueError("REOPENING feature identity drifted")
        if (reopen_attempt.get("source_state") == "CONFLICT_RESOLVED"
                and task_runtime.ref_sha(repository, config["target_ref"])
                    != reopen_attempt.get("expected_target_sha")):
            raise MergeQueueError("target moved during resolved candidate reopen")
        checkout_value = reopen_attempt.get("old_candidate_checkout")
        token = reopen_attempt.get("old_candidate_token")
        if checkout_value and record.get("full_suite_repair") is None:
            if not token:
                raise MergeQueueError("old candidate ownership token is missing")
            checkout = Path(checkout_value)
            if checkout.exists():
                if read_candidate_owner(controller, checkout) != reopen_attempt.get("old_candidate_owner"):
                    raise MergeQueueError("old candidate ownership drifted during reopen")
                if (reopen_attempt.get("source_state") == "CONFLICT_RESOLVED"
                        and (task_runtime.git(checkout, "rev-parse", "HEAD", check=False)
                             != reopen_attempt.get("old_candidate_sha")
                             or task_runtime.git(checkout, "status", "--porcelain=v1",
                                                 "--untracked-files=all", check=False))):
                    raise MergeQueueError("old resolved candidate drifted during reopen")
                rollback_unadmitted_candidate(controller, repository, checkout, token)
            else:
                marker = owner_marker(controller, checkout)
                registered = any(Path(row.get("worktree", "")).resolve() == checkout.resolve()
                                 for row in registered_worktrees(repository))
                if registered:
                    raise MergeQueueError("old candidate path is absent but remains registered")
                if marker.exists():
                    observed_owner = read_candidate_owner(controller, checkout)
                    expected_owner = reopen_attempt.get("old_candidate_owner")
                    if (observed_owner != expected_owner
                            or reopen_attempt.get("task_id") != task_id
                            or observed_owner.get("task_id") != task_id
                            or observed_owner.get("token") != token
                            or observed_owner.get("candidate_checkout") != str(checkout.resolve())
                            or observed_owner.get("repository_identity") != repository_identity(repository)):
                        raise MergeQueueError("orphaned old candidate marker ownership mismatched")
                    candidate_sha = reopen_attempt.get("old_candidate_sha")
                    parents = task_runtime.git(
                        repository, "show", "-s", "--format=%P", candidate_sha,
                        check=False,
                    ).split()
                    if parents != [observed_owner.get("target_sha"), observed_owner.get("feature_sha")]:
                        raise MergeQueueError("orphaned marker does not bind the persisted candidate SHA")
                    # Git removal already succeeded. Delete only the strictly
                    # matched marker; an unlink failure leaves REOPENING truth
                    # intact for another identical retry.
                    marker.unlink()
        queued = {key: value for key, value in record.items()
                  if key not in {"queue_attempt", "last_queue_outcome", "reopen_attempt",
                                 "review_ready_closure"}}
        next_review_round = record.get("review_round", 1)
        if reopen_attempt.get("source_state") == "REVIEW_FINDINGS":
            if (not isinstance(next_review_round, int) or isinstance(next_review_round, bool)
                    or next_review_round != 1):
                raise MergeQueueError("review repair round is malformed or already consumed")
            next_review_round = 2
        queued.update({"state": "QUEUED", "tip_sha": new_tip,
                       "changed_paths": reopen_attempt["changed_paths"],
                       "validation": reopen_attempt["validations"],
                       "command_evidence": reopen_attempt.get("command_evidence"),
                       "last_validation_outcome": "PASSED",
                       "review_round": next_review_round,
                       "reopened_from_candidate_sha": reopen_attempt["old_candidate_sha"]})
        full_suite_repair = record.get("full_suite_repair")
        if isinstance(full_suite_repair, dict):
            full_suite_repair = {**full_suite_repair, "status": "DELTA_REVIEW_PENDING",
                                 "delta_review_groups": 1}
            queued["full_suite_repair"] = full_suite_repair
        prior_findings_sha = record.get("prior_findings_candidate_sha")
        if reopen_attempt.get("source_state") == "REVIEW_FINDINGS":
            prior_findings_sha = reopen_attempt["old_candidate_sha"]
        if isinstance(prior_findings_sha, str):
            queued["prior_findings_candidate_sha"] = prior_findings_sha
        source_attempt = record.get("queue_attempt")
        if (reopen_attempt.get("source_state") == "REVIEW_FINDINGS"
                and isinstance(source_attempt, dict)
                and isinstance(source_attempt.get("blocking_findings"), list)):
            queued["prior_review_findings"] = source_attempt["blocking_findings"]
        failure_evidence = reopen_attempt.get("source_failure_evidence")
        if isinstance(failure_evidence, dict):
            queued["prior_queue_failure"] = failure_evidence
        reference = reopen_attempt.get("target_refresh_reference")
        if isinstance(reference, dict):
            # The receipt bound the exact protected target observed at reopen
            # admission. If the target advanced during the reopen window, drop
            # the reference: the queued candidate observes staleness itself and
            # the owner rebinds refresh identity through the refresh machinery.
            if task_runtime.ref_sha(repository, config["target_ref"]) \
                    == reference.get("target_sha"):
                queued["target_refreshes"] = [*record.get("target_refreshes", []),
                                              reference]
        with task_runtime.state_lock(controller):
            state = task_runtime.read_state(controller)
            if state["tasks"].get(task_id) != record:
                raise MergeQueueError("task state changed during reopen")
            queued["enqueue_sequence"] = task_runtime.assign_enqueue_sequence(state)
            state["tasks"][task_id] = queued
            entry = target_entry(state, repository, config["target_ref"])
            entry["conflicts"].pop(task_id, None)
            task_runtime.write_state(controller, state)
        source_outcome = reopen_attempt.get("source_outcome")
        outcome = ({"FAILED_FULL_SUITE": "REQUEUED_AFTER_FULL_SUITE_FAILURE",
                    "REVIEW_FAILED": "REQUEUED_AFTER_REVIEW_FAILURE",
                    "FAILED_TEST": ("REQUEUED_AFTER_RESOLVED_VALIDATION_FAILURE"
                                    if reopen_attempt.get("source_state") == "CONFLICT_RESOLVED"
                                    else "REQUEUED_AFTER_VALIDATION_FAILURE"),
                    "STALE_TARGET": "REQUEUED_AFTER_DEPENDENCY_LOCK_REFRESH",
                    "QUEUED_TIP_REFRESH": "REQUEUED_AFTER_TIP_REFRESH"}.get(
                        source_outcome, "REQUEUED_AFTER_FINDINGS"))
        return {**queued, "outcome": outcome}


def _withdraw_receipt_root(controller: Path) -> Path:
    return (controller / ".juno_task/runtime/merge-queue/withdraw").resolve()


def _record_claim_admission(record: dict[str, Any]) -> Optional[dict[str, Any]]:
    attempt = record.get("queue_attempt")
    risk = attempt.get("risk") if isinstance(attempt, dict) else None
    progress = risk.get("review_progress") if isinstance(risk, dict) else None
    admission = progress.get("full_suite_admission") if isinstance(progress, dict) else None
    return admission if isinstance(admission, dict) else None


def _withdraw_claim_binding(controller: Path, task_id: str,
                            admission: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Structurally bind one stored CLAIMED admission and prove its producer dead."""
    if admission.get("state") != "CLAIMED":
        return None
    schema = admission.get("schema_version")
    if schema not in {risk_runtime.FULL_SUITE_ADMISSION_SCHEMA,
                     risk_runtime.FULL_SUITE_ADMISSION_V2_SCHEMA}:
        raise MergeQueueError("stored full-suite admission schema is unsupported for withdraw")
    claim_ref = admission.get("claim")
    if (not isinstance(claim_ref, dict)
            or not isinstance(claim_ref.get("claim_path"), str)
            or not isinstance(claim_ref.get("claim_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", claim_ref["claim_sha256"])
            or not isinstance(admission.get("token"), str)
            or not isinstance(admission.get("attempt_number"), int)
            or isinstance(admission.get("attempt_number"), bool)):
        raise MergeQueueError("stored CLAIMED full-suite admission is malformed")
    claim_path = Path(claim_ref["claim_path"]).resolve()
    state_root = (controller / ".juno_task/state/merge-queue/full-suite").resolve()
    try:
        claim_path.relative_to(state_root / task_id)
    except ValueError as exc:
        raise MergeQueueError(
            "stored CLAIMED full-suite admission escaped controller state") from exc
    if not claim_path.is_file():
        raise MergeQueueError("stored CLAIMED full-suite claim artifact is missing")
    raw = claim_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != claim_ref["claim_sha256"]:
        raise MergeQueueError("stored CLAIMED full-suite claim digest drifted")
    try:
        claim = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MergeQueueError("stored CLAIMED full-suite claim is malformed") from exc
    if (not isinstance(claim, dict) or claim.get("task_id") != task_id
            or claim.get("token") != admission.get("token")
            or claim.get("attempt_number") != admission.get("attempt_number")):
        raise MergeQueueError("stored CLAIMED full-suite claim identity mismatch")
    if schema == risk_runtime.FULL_SUITE_ADMISSION_V2_SCHEMA:
        lock_value = claim.get("producer_lock")
        if (not isinstance(lock_value, dict) or lock_value.get("kind") != "flock"
                or not isinstance(lock_value.get("path"), str)):
            raise MergeQueueError("stored CLAIMED full-suite claim has no producer lock")
        lock_path = Path(lock_value["path"]).resolve()
        if lock_path != claim_path.parent / "producer.lock":
            raise MergeQueueError("stored CLAIMED producer lock path is not canonical")
    else:
        lock_path = claim_path.parent / "producer.lock"
    return {"schema_version": schema,
            "claim_path": str(claim_path),
            "claim_sha256": claim_ref["claim_sha256"],
            "token": admission.get("token"),
            "attempt_number": admission.get("attempt_number"),
            "producer_lock_path": str(lock_path)}


@contextmanager
def _withdraw_producer_liveness(controller: Path, task_id: str,
                                claim_binding: Optional[dict[str, Any]]) -> Iterator[None]:
    """Acquire the claim's flock non-blocking; a live producer fails closed."""
    if claim_binding is None:
        yield
        return
    lock_path = Path(claim_binding["producer_lock_path"])
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise MergeQueueError(
                    "a live full-suite producer owns this claim; retry after it "
                    "completes or its process exits") from exc
            raise
        yield


def merge_withdraw(controller: Path, task_id: str,
                   reason: Optional[str] = None) -> dict[str, Any]:
    """Withdraw one queued task through a public, fail-closed queue operation.

    Never hand-edits queue state: the transition runs under the task review and
    target locks, proves any claimed full-suite producer is dead via its flock,
    and records one durable withdraw receipt before the terminal state lands.
    """
    if not task_runtime.TASK_RE.fullmatch(task_id):
        raise MergeQueueError("unsafe task id")
    config = task_runtime.load_config(controller)
    repository = task_runtime.product_repository(controller, config)
    with review_lock(repository, task_id):
        with target_lock(controller, repository, config["target_ref"]):
            with task_runtime.state_lock(controller):
                state = task_runtime.read_state(controller)
                record = state["tasks"].get(task_id)
                if not isinstance(record, dict) or record.get("task_id") != task_id:
                    raise MergeQueueError("task has no queue record")
                if (record.get("target_ref") != config["target_ref"]
                        or Path(str(record.get("repository", ""))).resolve() != repository):
                    raise MergeQueueError("withdraw task repository/target identity drifted")
                source_state = record.get("state")
                if source_state == "WITHDRAWN":
                    raise MergeQueueError("task is already withdrawn")
                if source_state not in WITHDRAWABLE_STATES:
                    raise MergeQueueError(
                        f"task in terminal or in-flight state {source_state} cannot be withdrawn")
                frozen = json.loads(json.dumps(record))
            claim_binding = None
            admission = _record_claim_admission(frozen)
            if admission is not None:
                claim_binding = _withdraw_claim_binding(controller, task_id, admission)
            with _withdraw_producer_liveness(controller, task_id, claim_binding):
                target_sha = task_runtime.ref_sha(repository, config["target_ref"])
                policy_bytes = (controller / ".juno_task/config/task-workspace.json").read_bytes()
                body = {"schema_version": WITHDRAW_SCHEMA, "task_id": task_id,
                        "repository_identity": repository_identity(repository),
                        "repository": str(repository),
                        "target_ref": config["target_ref"], "target_sha": target_sha,
                        "source_state": source_state,
                        "tip_sha": frozen.get("tip_sha"),
                        "queue_record_sha256": digest(frozen),
                        "task_workspace_policy_sha256": hashlib.sha256(
                            policy_bytes).hexdigest(),
                        "claim": claim_binding,
                        "operator_reason": (reason[:512] if isinstance(reason, str)
                                            and reason.strip() else None)}
                receipt_name = digest({key: value for key, value in body.items()
                                       if key != "operator_reason"})
                path = _withdraw_receipt_root(controller) / task_id / f"{receipt_name}.json"
                receipt = {**body, "created_at": risk_runtime.utc_now()}
                data = canonical(receipt) + "\n"
                path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:
                    existing = json.loads(path.read_text())
                    if ({key: value for key, value in existing.items()
                            if key != "created_at"}
                            != {key: value for key, value in receipt.items()
                                if key != "created_at"}):
                        raise MergeQueueError("withdraw receipt identity collided")
                else:
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(data.encode()); handle.flush(); os.fsync(handle.fileno())
                # Clean the exact owned internal candidate before terminal truth.
                attempt = frozen.get("queue_attempt")
                checkout_value = attempt.get("candidate_checkout") if isinstance(attempt, dict) else None
                if checkout_value:
                    token = attempt.get("candidate_token")
                    if not isinstance(token, str) or not token:
                        raise MergeQueueError("withdraw candidate ownership token is missing")
                    checkout = Path(checkout_value)
                    if checkout.exists():
                        rollback_unadmitted_candidate(controller, repository, checkout, token)
                    else:
                        marker = owner_marker(controller, checkout)
                        if marker.exists():
                            owner = read_candidate_owner(controller, checkout)
                            if (owner.get("token") != token
                                    or owner.get("task_id") != task_id):
                                raise MergeQueueError(
                                    "withdraw orphan candidate ownership mismatched")
                            marker.unlink()
                with task_runtime.state_lock(controller):
                    current_state = task_runtime.read_state(controller)
                    current = current_state["tasks"].get(task_id)
                    if current != frozen:
                        raise MergeQueueError("task changed during withdraw admission")
                    reference = {"receipt_path": str(path),
                                 "receipt_sha256": hashlib.sha256(
                                     path.read_bytes()).hexdigest()}
                    updated = {**frozen, "state": "WITHDRAWN",
                               "last_queue_outcome": "WITHDRAWN",
                               "withdrawn_from_state": source_state,
                               "withdraw_receipt": reference}
                    current_state["tasks"][task_id] = updated
                    entry = target_entry(current_state, repository, config["target_ref"])
                    entry["conflicts"].pop(task_id, None)
                    task_runtime.write_state(controller, current_state)
            # Withdrawal is an explicit non-success disposition: never done,
            # with structured truth and any continuation binding on the board.
            try:
                withdraw_sync = task_runtime.ensure_kanban_sync(
                    controller, task_id, updated, phase="withdrawn")
            except task_runtime.KanbanSyncError as exc:
                try:
                    task_runtime._stamp_kanban_sync(controller, task_id, updated, exc.evidence)
                except task_runtime.TaskWorkspaceError:
                    pass
                raise MergeQueueError(
                    f"task withdrawn but its Kanban disposition failed: {exc}; "
                    f"recover with: {task_runtime.KANBAN_SYNC_RECOVERY.format(task=task_id)}") from exc
            if withdraw_sync.get("outcome") != "verified":
                try:
                    updated = task_runtime._stamp_kanban_sync(controller, task_id, updated, withdraw_sync)
                except task_runtime.TaskWorkspaceError:
                    pass
            return {**updated, "outcome": "WITHDRAWN"}


def status(controller: Path) -> dict[str, Any]:
    config = task_runtime.load_config(controller)
    repository = task_runtime.product_repository(controller, config)
    with task_runtime.state_lock(controller):
        state = task_runtime.read_state(controller)
        tasks = state["tasks"]
        entry = target_entry(state, repository, config["target_ref"])
        rows = [{"task_id": task_id, "state": row.get("state"), "tip_sha": row.get("tip_sha"),
                 "record_revision": digest(row),
                 "candidate_sha": ((row.get("queue_attempt") or {}).get("candidate_sha")
                                   if isinstance(row.get("queue_attempt"), dict) else None),
                 "candidate_checkout": ((row.get("queue_attempt") or {}).get("candidate_checkout")
                                        if isinstance(row.get("queue_attempt"), dict) else None),
                 "outcome": ((row.get("queue_attempt") or {}).get("outcome")
                             if isinstance(row.get("queue_attempt"), dict) else None),
                 "post_integration": ((row.get("queue_attempt") or {}).get("post_integration")
                                      if isinstance(row.get("queue_attempt"), dict) else None),
                 "recovery_command": ((row.get("queue_attempt") or {}).get("recovery_command")
                                      if isinstance(row.get("queue_attempt"), dict) else None),
                 "risk_status": (((row.get("queue_attempt") or {}).get("risk") or {}).get("status")
                                 if isinstance((row.get("queue_attempt") or {}).get("risk"), dict) else None),
                 "risk_policy_identity": (((row.get("queue_attempt") or {}).get("risk") or {}).get("policy_identity")
                                          if isinstance((row.get("queue_attempt") or {}).get("risk"), dict) else None),
                 "review_attempt_counter": (((((row.get("queue_attempt") or {}).get("risk") or {})
                                               .get("review_progress") or {}).get("review_attempt_counter"))
                                            if isinstance((((row.get("queue_attempt") or {}).get("risk") or {})
                                                           .get("review_progress")), dict) else None),
                 "review_round": row.get("review_round", 1),
                 "kanban_sync_required": (isinstance(row.get("kanban_sync"), dict)
                                          and row["kanban_sync"].get("status") == "required"),
                 "kanban_sync_recovery": (task_runtime.KANBAN_SYNC_RECOVERY.format(task=task_id)
                                           if isinstance(row.get("kanban_sync"), dict)
                                           and row["kanban_sync"].get("status") == "required" else None),
                 "completed_reviewer_count": len(((((row.get("queue_attempt") or {}).get("risk") or {})
                                                     .get("review_progress") or {}).get("steps", []))
                                                   if isinstance((((row.get("queue_attempt") or {}).get("risk") or {})
                                                                  .get("review_progress")), dict) else []),
                 "completed_reviewers": ([step.get("reviewer") for step in
                                            ((((row.get("queue_attempt") or {}).get("risk") or {})
                                              .get("review_progress") or {}).get("steps", []))]
                                           if isinstance((((row.get("queue_attempt") or {}).get("risk") or {})
                                                          .get("review_progress")), dict) else [])}
                for task_id, row in sorted(tasks.items()) if isinstance(row, dict)
                and row.get("target_ref") == config["target_ref"]
                and row.get("state") in {"QUEUED", "MERGING", "CONFLICT", "CONFLICT_RESOLVED",
                                         "AWAITING_RISK", "REVIEW_FINDINGS",
                                         "REVIEW_FINDINGS_EXHAUSTED",
                                         "REOPENING", "REQUEUING_STALE", "MERGED", "WITHDRAWN"}]
    for projection in rows:
        record = tasks[projection["task_id"]]
        projection.update(_full_suite_repair_safe_next(
            controller, repository, config, projection["task_id"], record))
        predispatch = _repair_predispatch_safe_next(
            controller, repository, config, projection["task_id"], record)
        if predispatch is not None:
            projection.update(predispatch)
        repair = record.get("full_suite_repair")
        if isinstance(repair, dict):
            projection["repair_status"] = repair.get("status")
            projection["repair_count"] = repair.get("repair_count")
            projection["delta_review_groups"] = repair.get("delta_review_groups")
            if repair.get("status") == "READY":
                projection["reason_code"] = "deterministic_full_suite_repair_ready"
                projection["safe_next_command"] = "yy merge arbiter run"
        contract = _status_task_row(
            controller, repository, config, projection["task_id"], record,
            detail=False, action=True)
        for key in ("producer_fence", "mutation_eligibility", "prior_terminal_evidence"):
            projection[key] = contract[key]
    return {"schema_version": QUEUE_SCHEMA, "repository_identity": repository_identity(repository),
            "target_ref": config["target_ref"], "target_sha": task_runtime.ref_sha(repository, config["target_ref"]),
            "tasks": rows, "last_attempt": entry["last_attempt"],
            "conflict_task_ids": sorted(entry["conflicts"])}


def _status_cursor(row: dict[str, Any]) -> str:
    sequence = row.get("enqueue_sequence")
    return f"{sequence if isinstance(sequence, int) else 'none'}:{row.get('task_id', '')}"


def _bounded_status_value(value: Any, *, depth: int = 0) -> Any:
    """Bound diagnostic values without serializing an exhaustive queue attempt."""
    if depth >= 4:
        return "<depth-limit>"
    if isinstance(value, str):
        return value[:MERGE_STATUS_STRING_CHARS]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, list):
        return [_bounded_status_value(item, depth=depth + 1)
                for item in value[:MERGE_STATUS_DETAIL_ITEMS]]
    if isinstance(value, dict):
        keys = sorted(value)[:MERGE_STATUS_DETAIL_ITEMS]
        return {str(key)[:MERGE_STATUS_STRING_CHARS]:
                _bounded_status_value(value[key], depth=depth + 1) for key in keys}
    return str(value)[:MERGE_STATUS_STRING_CHARS]


def _merge_phase_timing(validation: Any) -> dict[str, Any]:
    rows = validation if isinstance(validation, list) else []
    totals = {"resource_wait_ms": 0, "execution_ms": 0, "settlement_ms": 0,
              "overall_elapsed_ms": 0}
    first_failure_ms: Optional[int] = None
    elapsed_before = 0
    for result in rows:
        if not isinstance(result, dict):
            continue
        timing = result.get("timing") if isinstance(result.get("timing"), dict) else {}
        wall = max(0, int(timing.get("overall_elapsed_ms",
                                     timing.get("wall_duration_ms", result.get("duration_ms", 0))) or 0))
        totals["resource_wait_ms"] += max(0, int(timing.get("resource_wait_ms", 0) or 0))
        totals["execution_ms"] += max(0, int(timing.get("execution_ms", 0) or 0))
        totals["settlement_ms"] += max(0, int(timing.get("settlement_ms", 0) or 0))
        if first_failure_ms is None and (result.get("timed_out") or result.get("exit_code")):
            first_failure_ms = elapsed_before + max(0, int(timing.get("first_failure_ms", wall) or wall))
        elapsed_before += wall
    totals["overall_elapsed_ms"] = elapsed_before
    return {"schema_version": "juno_lifecycle_phase_timing.v1", **totals,
            "first_failure_ms": first_failure_ms}


def _merge_mutation_contract(task_id: str, state: Any) -> dict[str, Any]:
    contracts = {
        "QUEUED": ("arbiter-run", True, "queued_candidate", "FIFO and live authority must still admit the task", "yy merge arbiter run"),
        "AWAITING_RISK": ("arbiter-run", True, "risk_evidence_pending", "candidate, policy, and evidence identity must change to invalidate prior findings", "yy merge arbiter run"),
        "REQUEUING_STALE": ("arbiter-run", True, "target_refresh_pending", "compose against the current protected target", "yy merge arbiter run"),
        "CONFLICT": ("resolve", True, "conflict_requires_resolution", "commit only the preserved conflict paths", f"yy merge resolve {task_id}"),
        "CONFLICT_RESOLVED": ("resolve", True, "resolved_candidate_pending", "the resolved candidate or protected target identity must change", f"yy merge resolve {task_id}"),
        "REVIEW_FINDINGS": ("reopen", True, "review_findings_require_delta", "append one admitted descendant repair commit", f"yy merge reopen {task_id}"),
        "REOPENING": ("reopen", True, "reopen_incomplete", "complete the exact existing reopen transition", f"yy merge reopen {task_id}"),
        "MERGING": ("resume", True, "finalization_pending", "live target/readback authority must settle", "yy merge resume"),
        "REVIEW_FINDINGS_EXHAUSTED": (None, False, "review_findings_exhausted", "a newly authorized task with changed requirements/bytes is required", "operator stop: inspect consolidated findings"),
        "MERGED": (None, False, "already_merged", "none; terminal evidence is immutable", "none"),
        "WITHDRAWN": (None, False, "withdrawn", "explicitly create or authorize different work", "operator stop: task is withdrawn"),
    }
    operation, eligible, reason, invalidating, action = contracts.get(
        state, (None, False, "unsupported_legacy_state", f"migrate or explicitly recover unsupported state {state}", "operator stop: inspect merge status --full"))
    return {"operation": operation, "eligible": eligible, "reason_code": reason,
            "invalidating_change": invalidating, "safe_next_action": action,
            "operator_stop": not eligible,
            "authority_checked_live_by_executor": True}


def _status_task_row(controller: Path, repository: Path, config: dict[str, Any],
                     task_id: str, record: dict[str, Any], *, detail: bool,
                     action: bool = False) -> dict[str, Any]:
    attempt = record.get("queue_attempt") if isinstance(record.get("queue_attempt"), dict) else {}
    risk = attempt.get("risk") if isinstance(attempt.get("risk"), dict) else {}
    progress = risk.get("review_progress") if isinstance(risk.get("review_progress"), dict) else {}
    row = {"task_id": task_id, "state": record.get("state"),
           "enqueue_sequence": record.get("enqueue_sequence"),
           "tip_sha": record.get("tip_sha"), "candidate_sha": attempt.get("candidate_sha"),
           "outcome": attempt.get("outcome") or record.get("last_queue_outcome"),
           "risk_status": risk.get("status"),
           "advisory_count": (len(attempt.get("delivery_advisories"))
                              if isinstance(attempt.get("delivery_advisories"), list) else 0),
           "review_attempt_counter": progress.get("review_attempt_counter"),
           "recovery_command": (attempt.get("recovery_command") if action or detail else None),
           "kanban_sync_required": (isinstance(record.get("kanban_sync"), dict)
                                    and record["kanban_sync"].get("status") == "required")}
    if (action or detail):
        row.update(_full_suite_repair_safe_next(controller, repository, config, task_id, record))
    if row["kanban_sync_required"]:
        row["safe_next_command"] = task_runtime.KANBAN_SYNC_RECOVERY.format(task=task_id)
        row["reason_code"] = "kanban_sync_required"
    elif (action or detail) and record.get("state") in {"QUEUED", "AWAITING_RISK", "REQUEUING_STALE"}:
        # Do not recommend an unchanged arbiter rerun when the current immutable
        # plan already proves that a moved-target package pair needs refresh.
        current_plan = merge_plan(controller, task_id)
        package_blocker = next((finding for finding in current_plan["findings"]
                                if finding["severity"] == "error"
                                and finding["code"] == "package.lock_diverged"), None)
        if package_blocker is not None:
            row["safe_next_command"] = package_blocker["repair_command"]
            row["reason_code"] = "package_lock_refresh_required"
    eligibility = _merge_mutation_contract(task_id, record.get("state"))
    if row.get("safe_next_command"):
        eligibility.update({"operation": row["safe_next_command"].split()[2]
                            if len(row["safe_next_command"].split()) > 2 else "recovery",
                            "eligible": True, "reason_code": row.get("reason_code") or "typed_recovery_available",
                            "safe_next_action": row["safe_next_command"], "operator_stop": False})
    lease = task_runtime._lease_view(record)
    observation = (task_runtime._observe_producer(lease.get("producer"))
                   if isinstance(lease, dict) and lease.get("state") == task_runtime.decisions.LEASE_ACTIVE
                   else task_runtime.decisions.LeaseObservation("inactive", "no active task producer"))
    row["producer_fence"] = {"task_lease_state": lease.get("state") if isinstance(lease, dict) else "NONE",
                             "task_lease_attempt": lease.get("attempt") if isinstance(lease, dict) else None,
                             "producer_status": observation.status, "detail": observation.detail}
    row["mutation_eligibility"] = eligibility
    row["prior_terminal_evidence"] = _bounded_status_value(
        record.get("prior_queue_failure") or attempt.get("failure")
        or attempt.get("blocking_findings") or record.get("last_queue_outcome"))
    if detail:
        plan = risk.get("plan") if isinstance(risk.get("plan"), dict) else {}
        steps = progress.get("steps") if isinstance(progress.get("steps"), list) else []
        post = attempt.get("post_integration") if isinstance(attempt.get("post_integration"), dict) else {}
        validation = attempt.get("validation") if isinstance(attempt.get("validation"), list) else []
        repair = record.get("full_suite_repair") if isinstance(record.get("full_suite_repair"), dict) else {}
        row.update({
            "record_revision": digest(record),
            "phase_timing": _merge_phase_timing(validation),
            "base_sha": record.get("base_sha"),
            "branch_ref": record.get("branch_ref"),
            "candidate_checkout": attempt.get("candidate_checkout"),
            "expected_target_sha": attempt.get("expected_target_sha"),
            "review_round": record.get("review_round", 1),
            "risk": _bounded_status_value({
                "status": risk.get("status"), "policy_identity": risk.get("policy_identity"),
                "tier": plan.get("tier"), "reasons": plan.get("reasons"),
                "reviewer_sequence": plan.get("reviewer_sequence"),
                "review_attempt_counter": progress.get("review_attempt_counter"),
                "steps": [{"reviewer": step.get("reviewer"), "status": step.get("status")}
                          for step in steps[:MERGE_STATUS_DETAIL_ITEMS] if isinstance(step, dict)],
            }),
            "delivery_advisories": _bounded_status_value(
                attempt.get("delivery_advisories", [])),
            "post_integration": {str(key)[:MERGE_STATUS_STRING_CHARS]: ({"status": value.get("status"),
                                         "outcome": value.get("outcome")}
                                        if isinstance(value, dict) else _bounded_status_value(value))
                                 for key, value in sorted(post.items())[:MERGE_STATUS_DETAIL_ITEMS]},
            "validation": [{"id": value.get("id"), "exit_code": value.get("exit_code"),
                            "timed_out": value.get("timed_out")}
                           for value in validation[:MERGE_STATUS_DETAIL_ITEMS]
                           if isinstance(value, dict)],
            "full_suite_repair": _bounded_status_value({
                key: repair.get(key) for key in
                ("status", "repair_count", "delta_review_groups", "finding")}),
        })
    return row


def _status_arbiter(controller: Path, repository: Path, target_ref: str) -> dict[str, Any]:
    state = _arbiter_state(_arbiter_root(controller, repository, target_ref))
    observation = _arbiter_observation(state)
    return {"status": observation["status"], "detail": observation["detail"],
            "attempt": state.get("attempt") if isinstance(state, dict) else None,
            "state": state.get("state") if isinstance(state, dict) else None,
            "outcome": state.get("outcome") if isinstance(state, dict) else None}


def _status_metadata(level: str, *, truncated: bool, cursor: Optional[str],
                     row_limit: Optional[int]) -> dict[str, Any]:
    return {"level": level, "identifier": f"merge-status.{level}.v1",
            "truncated": truncated, "cursor": cursor,
            "limits": {"max_bytes": (None if level == "full" else MERGE_STATUS_MAX_BYTES),
                       "max_rows": row_limit}}


def status_projection(controller: Path, *, level: str = "summary",
                      task_id: Optional[str] = None) -> dict[str, Any]:
    """Return summary/detail without constructing legacy exhaustive task payloads."""
    if level == "full":
        legacy = status(controller)
        return {**legacy, "status_schema_version": MERGE_STATUS_SCHEMA,
                "projection": _status_metadata("full", truncated=False, cursor=None,
                                               row_limit=None)}
    config = task_runtime.load_config(controller)
    repository = task_runtime.product_repository(controller, config)
    with task_runtime.state_lock(controller):
        state = task_runtime.read_state(controller)
        records = [(key, value) for key, value in state["tasks"].items()
                   if isinstance(value, dict)
                   and value.get("target_ref") == config["target_ref"]
                   and value.get("state") in MERGE_STATUS_VISIBLE_STATES]
        entry = target_entry(state, repository, config["target_ref"])
    ordered = sorted(records, key=lambda item: (
        item[1].get("enqueue_sequence") if isinstance(item[1].get("enqueue_sequence"), int)
        else -1, item[0]), reverse=True)
    active = sorted((item for item in records if item[1].get("state") in MERGE_STATUS_ACTIVE_STATES),
                    key=lambda item: (item[1].get("enqueue_sequence", 2**63 - 1), item[0]))
    selected = None
    if level == "detail":
        if task_id is not None:
            selected = next((item for item in records if item[0] == task_id), None)
            if selected is None:
                raise MergeQueueError(f"detail task is not in merge queue history: {task_id}")
        elif active:
            selected = active[0]
        else:
            raise MergeQueueError("detail requires TASK_ID when there is no active merge attempt")
    arbiter = _status_arbiter(controller, repository, config["target_ref"])
    state_counts: dict[str, int] = {}
    for _, record in records:
        state_name = str(record.get("state"))
        state_counts[state_name] = state_counts.get(state_name, 0) + 1
    target = {"repository_identity": repository_identity(repository),
              "target_ref": config["target_ref"],
              "target_sha": task_runtime.ref_sha(repository, config["target_ref"])}
    if level == "detail" and selected is not None:
        detail_row = _status_task_row(controller, repository, config, selected[0], selected[1],
                                      detail=True)
        return {"schema_version": MERGE_STATUS_SCHEMA,
                "projection": _status_metadata("detail", truncated=True, cursor=None,
                                               row_limit=MERGE_STATUS_DETAIL_ITEMS),
                "target": target, "arbiter": arbiter, "task": detail_row,
                "next_action": (detail_row.get("safe_next_command")
                                or detail_row.get("recovery_command")
                                or ("yy merge arbiter run" if active else "none"))}
    blockers_all = [item for item in active
                    if item[1].get("state") in MERGE_STATUS_BLOCKER_STATES
                    or (item[1].get("state") == "AWAITING_RISK"
                        and item[1].get("last_queue_outcome") == "FAILED_FULL_SUITE")
                    or (isinstance(item[1].get("kanban_sync"), dict)
                        and item[1]["kanban_sync"].get("status") == "required")]
    active_task_id = active[0][0] if active else None
    blockers = [_status_task_row(controller, repository, config, key, record, detail=False,
                                 action=(key == active_task_id))
                for key, record in blockers_all[:MERGE_STATUS_BLOCKER_ROWS]]
    recent_source = ordered[:MERGE_STATUS_SUMMARY_ROWS]
    recent = [{"task_id": key, "state": record.get("state"),
               "enqueue_sequence": record.get("enqueue_sequence"),
               "outcome": ((record.get("queue_attempt") or {}).get("outcome")
                           if isinstance(record.get("queue_attempt"), dict) else None)
                          or record.get("last_queue_outcome")}
              for key, record in recent_source]
    active_row = (_status_task_row(controller, repository, config, active[0][0], active[0][1],
                                   detail=False, action=True) if active else None)
    active_blocker_action = ((active_row.get("safe_next_command")
                              or active_row.get("recovery_command"))
                             if active_row is not None
                             and (active_row.get("state") in MERGE_STATUS_BLOCKER_STATES
                                  or active_row.get("reason_code") is not None)
                             else None)
    next_action = ("observe with: yy merge arbiter status" if arbiter["status"] == "alive"
                   else active_blocker_action
                   or ("yy merge arbiter run" if active else "none"))
    truncated = (len(ordered) > len(recent_source)
                 or len(blockers_all) > len(blockers))
    cursor = _status_cursor(recent_source[-1][1]) if len(ordered) > len(recent_source) else None
    return {"schema_version": MERGE_STATUS_SCHEMA,
            "projection": _status_metadata(
                "summary", truncated=truncated, cursor=cursor,
                row_limit=MERGE_STATUS_SUMMARY_ROWS + MERGE_STATUS_BLOCKER_ROWS + 1),
            "target": target, "arbiter": arbiter,
            "active_task": active_row,
            "candidate_counts": {"total": len(records), "active": len(active),
                                 "by_state": dict(sorted(state_counts.items()))},
            "blockers": blockers,
            "recent_transitions": recent,
            "conflict_task_ids": sorted(entry["conflicts"])[:MERGE_STATUS_BLOCKER_ROWS],
            "next_action": next_action,
            "more": ({"command": "yy merge status --full"} if truncated else None)}


def human_status(report: dict[str, Any]) -> str:
    projection = report["projection"]
    lines = [f"merge status [{projection['identifier']}]",
             f"target: {report['target']['target_ref']} @ {report['target']['target_sha']}",
             f"arbiter: {report['arbiter']['status']} ({report['arbiter']['detail']})"]
    if projection["level"] == "detail":
        task = report["task"]
        lines.append(f"task: {task['task_id']} state={task['state']} outcome={task.get('outcome')}")
    else:
        active = report.get("active_task")
        lines.append("active: " + (f"{active['task_id']} ({active['state']})" if active else "none"))
        counts = report["candidate_counts"]
        lines.append(f"candidates: total={counts['total']} active={counts['active']}")
        lines.extend(f"blocker: {row['task_id']} state={row['state']} reason={row.get('reason_code')}"
                     for row in report["blockers"])
        lines.extend(f"recent: {row['task_id']} {row['state']} {row.get('outcome')}"
                     for row in report["recent_transitions"])
    lines.append(f"truncated: {str(projection['truncated']).lower()} cursor={projection['cursor']}")
    lines.append(f"next: {report['next_action']}")
    return "\n".join(lines)


MERGE_DRIVE_ROOT = ".juno_task/runtime/lifecycle-runs/merge"
# Queue states a merge-drive scope may legally contain. REVIEW_FINDINGS_EXHAUSTED
# is terminal queue history with no drive-legal transition: the loop can only
# pause on it, so freezing it re-reports a historical blocker on every later
# drive instead of a clean empty-scope completion. Mid-drive exhaustion still
# pauses through the loop's own state check.
MERGE_DRIVE_ELIGIBLE_STATES = frozenset({
    "QUEUED", "AWAITING_RISK", "REQUEUING_STALE",
    "CONFLICT", "CONFLICT_RESOLVED", "REVIEW_FINDINGS",
    "REOPENING", "MERGING", "MERGED",
})


def _drive_scope(controller: Path, config: dict[str, Any], through: Optional[str]) -> list[dict[str, Any]]:
    with task_runtime.state_lock(controller):
        tasks = task_runtime.read_state(controller)["tasks"]
    eligible = MERGE_DRIVE_ELIGIBLE_STATES
    rows = [row for row in tasks.values() if isinstance(row, dict)
            and row.get("target_ref") == config["target_ref"]
            and row.get("state") in eligible]
    rows.sort(key=lambda row: (row.get("enqueue_sequence", 2**63 - 1), row["task_id"]))
    if through is not None:
        positions = [index for index, row in enumerate(rows) if row.get("task_id") == through]
        if not positions:
            raise MergeQueueError("--through task is not in the current FIFO-authorized scope")
        rows = rows[:positions[0] + 1]
    if not rows:
        raise MergeQueueError("merge drive has no FIFO-authorized tasks")
    return [{"task_id": row["task_id"], "enqueue_sequence": row.get("enqueue_sequence"),
             "initial_state": row.get("state"), "initial_tip_sha": row.get("tip_sha"),
             "record_sha256": digest(row)} for row in rows]


def current_fifo_identity(controller: Path, config: dict[str, Any],
                          through: Optional[str]) -> dict[str, Any]:
    """Return the exact actionable FIFO read-set used by lifecycle recovery."""
    repository = task_runtime.product_repository(controller, config)
    rows = [row for row in _drive_scope(controller, config, through)
            if row.get("initial_state") != "MERGED"]
    body = {"schema_version": "juno_merge_current_fifo_identity.v1",
            "target_ref": config["target_ref"],
            "target_sha": task_runtime.ref_sha(repository, config["target_ref"]),
            "tasks": rows}
    return {**body, "sha256": digest(body)}


def _merge_plan_execution_identity(plan: dict[str, Any]) -> str:
    return lifecycle_runtime.digest({key: value for key, value in plan.items()
                                     if key not in {"controller_commit", "compiled_plan_sha256"}})


def _merge_drive_projection(controller: Path, repository: Path, config: dict[str, Any],
                            through: Optional[str], run_dir: Path, latest_paths: list[Path],
                            journal_path: Path, journal: dict[str, Any], plan: dict[str, Any],
                            scope: list[dict[str, Any]], *, blocker: Optional[dict[str, Any]],
                            terminal: bool) -> dict[str, Any]:
    counters = {name: 0 for name in
                ("executed", "reused", "invalidated", "skipped", "not_applicable")}
    artifacts = [journal["compiled_plan"], journal["fifo_scope"]]
    with task_runtime.state_lock(controller):
        final_tasks = task_runtime.read_state(controller)["tasks"]
    completed = []
    reviewer_attempts = 0
    for frozen in scope:
        row = final_tasks.get(frozen["task_id"], {})
        if row.get("state") == "MERGED":
            completed.append(frozen["task_id"])
        attempt = row.get("queue_attempt") if isinstance(row, dict) else None
        evidence = attempt.get("command_evidence") if isinstance(attempt, dict) else None
        if isinstance(evidence, dict):
            for name, count in (evidence.get("counters") or {}).items():
                if name in counters and isinstance(count, int):
                    counters[name] += count
        risk = attempt.get("risk") if isinstance(attempt, dict) else None
        if isinstance(risk, dict):
            progress = risk.get("review_progress")
            if isinstance(progress, dict):
                reviewer_attempts += int(progress.get("review_attempt_counter", 0))
            reference = risk.get("evidence")
            if isinstance(reference, dict):
                artifacts.append({"path": reference.get("receipt_path"),
                                  "sha256": reference.get("receipt_sha256")})
    state_name = "MERGED_THROUGH" if terminal else "PAUSED"
    projection = lifecycle_runtime.compact_projection(
        kind="merge-drive", run_id=journal["run_id"], task_id=through, state=state_name,
        plan=plan, started=lifecycle_runtime.lifecycle_elapsed_started(journal),
        counters=counters,
        attempts={"transitions": journal["attempts"]["transitions"],
                  "semantic_repairs": journal["attempts"]["semantic_repairs"],
                  "reviewer_attempts": reviewer_attempts},
        blocker=blocker,
        next_action=("none: frozen FIFO scope integrated" if terminal else
                     "resolve the reported blocker, then resume the same yy merge drive"
                     + (f" --through {through}" if through else "")),
        artifacts=artifacts,
        identities={"scope_sha256": journal["scope_sha256"],
                    "target_ref": config["target_ref"],
                    "initial_target_sha": journal["initial_target_sha"],
                    "current_target_sha": task_runtime.ref_sha(repository, config["target_ref"]),
                    "completed_task_ids": completed,
                    "deadline_unix_ns": journal["deadline_unix_ns"]})
    projection_index = len(journal.get("projections", [])) + 1
    # Crash recovery: adopt an exact preexisting projection left between the
    # numbered write and the journal append; wall-time fields advance, so the
    # immutable identity fields bind the adoption instead of recomputed bytes.
    adopted = lifecycle_runtime.adopt_interrupted_projection(
        run_dir, journal, projection_index, state_name, kind="merge-drive",
        expected=projection)
    if adopted is not None:
        projection = adopted
        adopted_path = run_dir / "projections" / f"{projection_index:04d}-{state_name.lower()}.json"
        projection_ref = {"path": str(adopted_path.resolve()),
                          "sha256": hashlib.sha256(adopted_path.read_bytes()).hexdigest()}
    else:
        projection_ref = lifecycle_runtime.atomic_json(
            run_dir / "projections" / f"{projection_index:04d}-{state_name.lower()}.json",
            projection, exclusive=True)
    journal.setdefault("projections", []).append(projection_ref)
    journal["state"] = state_name; journal["terminal"] = terminal; journal["blocker"] = blocker
    lifecycle_runtime.lifecycle_journal_write(journal_path, journal)
    summary_ref = None
    if terminal:
        # The summary derives deterministically from the published projection,
        # so an interrupted publication is repaired by an exact rewrite.
        summary = lifecycle_runtime.deterministic_summary(projection)
        summary_ref = lifecycle_runtime.atomic_json(run_dir / "summary.json", summary)
    pointer = {"schema_version": "juno_managed_merge_drive_latest.v2",
               "run_id": journal["run_id"], "scope_sha256": journal["scope_sha256"],
               "compiled_plan_sha256": plan["compiled_plan_sha256"],
               "execution_identity_sha256": journal["execution_identity_sha256"],
               "projection_path": projection_ref["path"], "summary": summary_ref,
               "terminal": terminal}
    for path in latest_paths:
        lifecycle_runtime.atomic_json(path, pointer)
    return projection


def _merge_drive_claimed(controller: Path, through: Optional[str] = None) -> dict[str, Any]:
    """Resume one durably claimed frozen FIFO scope within cumulative budgets."""
    if through is not None and not task_runtime.TASK_RE.fullmatch(through):
        raise MergeQueueError("unsafe --through task id")
    current_plan = lifecycle_runtime.compile_lifecycle_template(
        controller, "merge-drive", through, model_identity=os.environ.get("JUNO_MODEL"))
    execution_identity = _merge_plan_execution_identity(current_plan)
    config = task_runtime.load_config(controller)
    repository = task_runtime.product_repository(controller, config)
    root = controller / MERGE_DRIVE_ROOT
    selector_identity = digest({"repository_identity": repository_identity(repository),
                                "target_ref": config["target_ref"], "through": through})
    selector_root = root / "scopes" / selector_identity
    selector_latest = selector_root / "latest.json"
    global_latest = root / "latest.json"
    latest_paths = [selector_latest, global_latest]
    with lifecycle_runtime.lifecycle_claim(selector_root / ".claim.lock"):
        if selector_latest.is_file():
            try:
                pointer = json.loads(selector_latest.read_text())
                run_dir = root / pointer["run_id"]
                journal_path = run_dir / "journal.json"
                journal = json.loads(journal_path.read_text())
                plan = json.loads((run_dir / "compiled-plan.json").read_text())
                scope_value = json.loads((run_dir / "fifo-scope.json").read_text())
                scope = scope_value["tasks"]
            except (OSError, KeyError, json.JSONDecodeError) as exc:
                raise MergeQueueError(
                    "frozen merge-drive claim is malformed; explicit reviewed replacement required") from exc
            if (journal.get("schema_version") != "juno_managed_merge_drive_journal.v2"
                    or journal.get("selector_identity_sha256") != selector_identity
                    or journal.get("execution_identity_sha256") != execution_identity
                    or journal.get("scope_sha256") != scope_value.get("scope_sha256")):
                raise MergeQueueError(
                    "frozen merge-drive identity is incompatible; explicit reviewed replacement required")
            with task_runtime.state_lock(controller):
                live_states = task_runtime.read_state(controller)["tasks"]
            left_scope = [row for row in scope if isinstance(row, dict)
                          and (live_states.get(row.get("task_id"), {}) or {}).get("state")
                          not in MERGE_DRIVE_ELIGIBLE_STATES]
            if left_scope and not journal.get("terminal"):
                # A paused lineage whose frozen authorization set still contains
                # tasks that left the FIFO-eligible set (for example terminal
                # exhausted or withdrawn history, or a task pulled back to
                # WORKING) replays a dead blocker on every resume. Retire the
                # stale lineage and open a fresh immutable scope instead; the
                # old run's artifacts remain on disk as immutable history.
                selector_latest.unlink(missing_ok=True)
            projection_path = Path(str(pointer.get("projection_path", "")))
            if journal.get("terminal"):
                # SUPERSEDED is terminal history, never a resumable successful
                # drive. Validate its receipt-backed terminal artifacts before
                # retiring the pointer, including when the frozen/current IDs
                # happen to be equal (the attempt-229 incident shape).
                if journal.get("state") == "SUPERSEDED":
                    supersession = journal.get("supersession")
                    journal_refs = [ref for ref in journal.get("projections", [])
                                    if isinstance(ref, dict) and ref.get("path")]
                    if (not isinstance(supersession, dict) or not journal_refs
                            or supersession.get("projection") != journal_refs[-1]):
                        raise MergeQueueError(
                            "terminal SUPERSEDED merge-drive evidence is malformed")
                    final_ref = journal_refs[-1]
                    projection_value = lifecycle_runtime.verified_projection_bytes(
                        Path(str(final_ref["path"])),
                        expected_sha256=final_ref.get("sha256"),
                        kind="merge-drive", run_id=journal.get("run_id"))
                    summary_ref = supersession.get("summary")
                    summary_path = Path(str(
                        summary_ref.get("path", "") if isinstance(summary_ref, dict) else ""))
                    expected_summary = lifecycle_runtime.deterministic_summary(projection_value)
                    if (projection_value.get("state") != "SUPERSEDED"
                            or not isinstance(summary_ref, dict)
                            or not summary_path.is_file()
                            or hashlib.sha256(summary_path.read_bytes()).hexdigest()
                            != summary_ref.get("sha256")
                            or summary_path.read_bytes()
                            != lifecycle_runtime.canonical_bytes(expected_summary)):
                        raise MergeQueueError(
                            "terminal SUPERSEDED merge-drive evidence is malformed")
                    selector_latest.unlink(missing_ok=True)
                # Terminal MERGED_THROUGH reuse is bound to the current requested
                # FIFO scope. A changed scope opens a fresh immutable lineage.
                frozen_ids = [row.get("task_id") for row in scope if isinstance(row, dict)]
                current_scope = (_drive_scope(controller, config, through)
                                 if through is None else scope)
                current_ids = [row.get("task_id") for row in current_scope if isinstance(row, dict)]
                if journal.get("state") != "SUPERSEDED" and frozen_ids == current_ids:
                    # The journal is the authority: derive the authoritative
                    # terminal projection from it and repair stale pointers
                    # (crash between the terminal journal write and publication)
                    # before returning.
                    # The journal's final reference is the sole authority: a
                    # missing or malformed final artifact fails closed instead
                    # of promoting whatever the pointer happens to reference.
                    journal_refs = [ref for ref in journal.get("projections", [])
                                    if isinstance(ref, dict) and ref.get("path")]
                    if not journal_refs:
                        raise MergeQueueError(
                            "terminal merge-drive journal has no final projection reference")
                    final_ref = journal_refs[-1]
                    candidate = Path(str(final_ref["path"]))
                    if not candidate.is_file():
                        raise MergeQueueError(
                            "terminal merge-drive journal projection artifact is missing")
                    projection_value = lifecycle_runtime.verified_projection_bytes(
                        candidate, expected_sha256=final_ref.get("sha256"),
                        kind="merge-drive", run_id=journal.get("run_id"))
                    if projection_value.get("state") != "MERGED_THROUGH":
                        raise MergeQueueError(
                            "terminal merge-drive journal projection is not terminal")
                    authoritative = (candidate, projection_value)
                    if authoritative is not None:
                        path, projection_value = authoritative
                        # The expected canonical summary derives from the
                        # verified projection; existing bytes are verified.
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
                            summary_ref = {
                                "path": str(summary_path.resolve()),
                                "sha256": hashlib.sha256(
                                    expected_summary_bytes).hexdigest()}
                        pointer_repaired = {
                            "schema_version": "juno_managed_merge_drive_latest.v2",
                            "run_id": journal["run_id"], "scope_sha256": journal["scope_sha256"],
                            "compiled_plan_sha256": plan["compiled_plan_sha256"],
                            "execution_identity_sha256": journal["execution_identity_sha256"],
                            "projection_path": str(path.resolve()), "summary": summary_ref,
                            "terminal": True}
                        # Each latest pointer is compared and repaired
                        # independently: an interruption between the selector
                        # and global writes must not leave either stale.
                        for latest_path in latest_paths:
                            current = (json.loads(latest_path.read_text())
                                       if latest_path.is_file() else None)
                            if current != pointer_repaired:
                                lifecycle_runtime.atomic_json(latest_path, pointer_repaired)
                        return projection_value
                # A new immutable lineage is required for the changed FIFO scope:
                # archive the completed pointer and fall through to creation.
                selector_latest.unlink(missing_ok=True)
        if not selector_latest.is_file():
            scope = _drive_scope(controller, config, through)
            target_sha = task_runtime.ref_sha(repository, config["target_ref"])
            scope_identity = digest({"target_ref": config["target_ref"],
                                     "target_sha": target_sha, "tasks": scope})
            run_id = f"{time.time_ns()}-{secrets.token_hex(8)}"
            run_dir = root / run_id
            run_dir.mkdir(parents=True, exist_ok=False)
            plan = current_plan
            plan_ref = lifecycle_runtime.atomic_json(
                run_dir / "compiled-plan.json", plan, exclusive=True)
            scope_ref = lifecycle_runtime.atomic_json(run_dir / "fifo-scope.json", {
                "schema_version": "juno_merge_drive_fifo_scope.v1",
                "scope_sha256": scope_identity, "target_ref": config["target_ref"],
                "target_sha": target_sha, "through": through, "tasks": scope}, exclusive=True)
            prompt_source = controller / plan["prompts"][0]["path"]
            prompt_target = run_dir / "frozen-prompts/semantic-repair.md"
            prompt_target.parent.mkdir(parents=True, exist_ok=True)
            prompt_bytes = prompt_source.read_bytes()
            fd = os.open(prompt_target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(prompt_bytes); stream.flush(); os.fsync(stream.fileno())
            started_ns = time.time_ns()
            journal = {"schema_version": "juno_managed_merge_drive_journal.v2",
                       "run_id": run_id, "selector_identity_sha256": selector_identity,
                       "execution_identity_sha256": execution_identity,
                       "scope_sha256": scope_identity, "initial_target_sha": target_sha,
                       "compiled_plan": plan_ref, "fifo_scope": scope_ref,
                       "frozen_prompt": {"path": str(prompt_target.resolve()),
                                         "sha256": hashlib.sha256(prompt_bytes).hexdigest()},
                       "started_at_unix_ns": started_ns,
                       "deadline_unix_ns": started_ns + int(plan["budgets"]["total_wall_seconds"]) * 1_000_000_000,
                       "attempts": {"transitions": 0, "semantic_repairs": 0},
                       "events": [], "operations": [], "repairs": [], "projections": [],
                       "state": "CLAIMED", "terminal": False, "blocker": None}
            journal_path = run_dir / "journal.json"
            lifecycle_runtime.lifecycle_journal_write(journal_path, journal)
            pointer = {"schema_version": "juno_managed_merge_drive_latest.v2",
                       "run_id": run_id, "scope_sha256": scope_identity,
                       "compiled_plan_sha256": plan["compiled_plan_sha256"],
                       "execution_identity_sha256": execution_identity,
                       "projection_path": None, "summary": None, "terminal": False}
            # The scope pointer is durable before the first queue transition.
            lifecycle_runtime.atomic_json(selector_latest, pointer)
            lifecycle_runtime.atomic_json(global_latest, pointer)
            lifecycle_runtime.lifecycle_checkpoint(
                journal_path, journal, phase="claim", boundary="POST",
                detail={"scope_sha256": scope_identity, "task_count": len(scope)})
        blocker: Optional[dict[str, Any]] = None
        max_transitions = int(plan["budgets"]["max_transitions"])
        try:
            pending_operation = next((item for item in reversed(journal["operations"])
                                      if item.get("post_state") is None), None)
            if pending_operation is not None:
                with task_runtime.state_lock(controller):
                    recovered_record = task_runtime.read_state(controller)["tasks"].get(
                        pending_operation["task_id"], {})
                pending_operation["post_state"] = recovered_record.get("state")
                lifecycle_runtime.lifecycle_checkpoint(
                    journal_path, journal, phase=pending_operation["phase"],
                    boundary="RECOVERED",
                    detail={"task_id": pending_operation["task_id"],
                            "post_state": pending_operation["post_state"],
                            "transition": journal["attempts"]["transitions"]})
            lifecycle_runtime.lifecycle_remaining_seconds(journal)
            for frozen in scope:
                task_id = frozen["task_id"]
                while True:
                    lifecycle_runtime.lifecycle_remaining_seconds(journal)
                    with task_runtime.state_lock(controller):
                        record = task_runtime.read_state(controller)["tasks"].get(task_id)
                    if not isinstance(record, dict):
                        raise MergeQueueError("frozen merge-drive task disappeared")
                    state = record.get("state")
                    if state == "MERGED":
                        break
                    if state == "CONFLICT":
                        blocker = {"category": "conflict", "task_id": task_id, "state": state,
                                   "authority_required": "explicit conflict resolution"}; break
                    if state == "CONFLICT_RESOLVED":
                        # An explicitly resolved conflict is not terminal for the
                        # frozen scope: verify the durable resolution state and
                        # schedule the authorized continuation within the same
                        # cumulative transition budget.
                        with task_runtime.state_lock(controller):
                            conflict_entry = target_entry(
                                task_runtime.read_state(controller), repository,
                                config["target_ref"])["conflicts"].get(task_id)
                        if (not isinstance(conflict_entry, dict)
                                or conflict_entry.get("resolution_state") != "RESOLVED"):
                            blocker = {"category": "conflict", "task_id": task_id,
                                       "state": state,
                                       "authority_required": "explicit conflict resolution"}; break
                        operation = {"phase": "resolve-continue", "task_id": task_id,
                                     "pre_state": state, "post_state": None}
                    elif state == "REVIEW_FINDINGS_EXHAUSTED":
                        blocker = {"category": "review_findings_exhausted", "task_id": task_id}; break
                    elif state == "REVIEW_FINDINGS":
                        # Gate semantic-repair recovery and launch on verified
                        # hydration/dependency evidence BEFORE any recovery
                        # attempt, budget increment, or launch checkpoint; the
                        # gate heals the exact-lock tree when recoverable and
                        # preserves the queue-owned REVIEW_FINDINGS state.
                        semantic_gate = task_runtime._managed_hydration_gate(controller, record)
                        record = semantic_gate["record"]
                        repair_authorization = record.get("full_suite_repair")
                        repair = journal["repairs"][0] if journal["repairs"] else None
                        if isinstance(repair_authorization, dict) and repair is None:
                            if (repair_authorization.get("status") != "READY"
                                    or repair_authorization.get("repair_count") != 0
                                    or repair_authorization.get("delta_review_groups") != 0):
                                blocker = {"category": "review_findings_exhausted",
                                           "task_id": task_id}; break
                            claimed_authorization = {**repair_authorization,
                                                     "status": "DISPATCHED",
                                                     "repair_count": 1}
                            with task_runtime.state_lock(controller):
                                claim_state = task_runtime.read_state(controller)
                                current = claim_state["tasks"].get(task_id)
                                if current != record:
                                    raise MergeQueueError(
                                        "full-suite repair authority moved before dispatch")
                                current = {**record,
                                           "full_suite_repair": claimed_authorization}
                                attempt = current.get("queue_attempt")
                                risk = attempt.get("risk") if isinstance(attempt, dict) else None
                                if isinstance(risk, dict):
                                    risk = {**risk, "full_suite_repair": claimed_authorization}
                                    current["queue_attempt"] = {**attempt, "risk": risk,
                                                                "review": risk}
                                claim_state["tasks"][task_id] = current
                                task_runtime.write_state(controller, claim_state)
                            record = current
                            semantic_gate = {**semantic_gate, "record": record}
                        repaired = (task_runtime._recover_task_worker(record, repair)
                                    if repair and not repair.get("terminal_state") else repair)
                        predispatch_recovery = (repair.get("predispatch_recovery")
                                                if isinstance(repair, dict) else None)
                        if repaired is None and isinstance(predispatch_recovery, dict) \
                                and predispatch_recovery.get("status") == "READY":
                            repair_dir = Path(repair["attempt_dir"])
                            predispatch_recovery["status"] = "DISPATCHED"
                            lifecycle_runtime.lifecycle_checkpoint(
                                journal_path, journal, phase="semantic-repair-1-redispatch",
                                boundary="PRE", detail={"task_id": task_id,
                                    "attempt_dir": repair["attempt_dir"],
                                    "projection": predispatch_recovery.get("projection")})
                            repaired = task_runtime._launch_task_worker(
                                controller, task_id, record, repair_dir,
                                Path(journal["frozen_prompt"]["path"]), repair=True,
                                timeout_seconds=lifecycle_runtime.lifecycle_remaining_seconds(journal),
                                context_bytes=lifecycle_runtime.canonical_bytes({
                                    "candidate_sha": (record.get("queue_attempt") or {}).get("candidate_sha"),
                                    "risk": (record.get("queue_attempt") or {}).get("risk")})[:32768],
                                hydration_gate=semantic_gate, reuse_existing_admission=True)
                        elif repaired is None:
                            if journal["attempts"]["semantic_repairs"] >= int(
                                    plan["budgets"]["semantic_repairs"]):
                                blocker = {"category": "review_findings_exhausted", "task_id": task_id}; break
                            repair_dir = run_dir / "workers/semantic-repair-0001"
                            repair = {"kind": "semantic_repair", "index": 1,
                                      "attempt_dir": str(repair_dir.resolve()),
                                      "before_sha": task_runtime.git(Path(record["worktree"]),
                                                                     "rev-parse", "HEAD"),
                                      "task_id": task_id, "terminal_state": None,
                                      "authorization_receipt": (
                                          repair_authorization.get("authorization_receipt")
                                          if isinstance(repair_authorization, dict) else None)}
                            journal["repairs"].append(repair)
                            journal["attempts"]["semantic_repairs"] += 1
                            lifecycle_runtime.lifecycle_checkpoint(
                                journal_path, journal, phase="semantic-repair-1", boundary="PRE",
                                detail={"task_id": task_id, "attempt_dir": repair["attempt_dir"],
                                        "before_sha": repair["before_sha"]})
                            repaired = task_runtime._launch_task_worker(
                                controller, task_id, record, repair_dir,
                                Path(journal["frozen_prompt"]["path"]), repair=True,
                                timeout_seconds=lifecycle_runtime.lifecycle_remaining_seconds(journal),
                                context_bytes=lifecycle_runtime.canonical_bytes({
                                    "candidate_sha": (record.get("queue_attempt") or {}).get("candidate_sha"),
                                    "risk": (record.get("queue_attempt") or {}).get("risk")})[:32768],
                                hydration_gate=semantic_gate)
                        assert repair is not None
                        if isinstance(repaired, dict) and "terminal_state" in repaired:
                            repair.update(repaired)
                        lifecycle_runtime.lifecycle_checkpoint(
                            journal_path, journal, phase="semantic-repair-1",
                            boundary="RECOVERED" if repaired.get("recovered") else "POST",
                            detail={"task_id": task_id,
                                    "terminal_state": repaired.get("terminal_state"),
                                    "after_sha": repaired.get("after_sha"),
                                    "receipt": repaired.get("receipt")})
                        if repaired.get("terminal_state") != "completed":
                            blocker = {"category": "semantic_repair", "task_id": task_id,
                                       "terminal_state": repaired.get("terminal_state")}; break
                        # The exact one-commit readback above is durable before reopen.
                        operation = {"phase": "repair-reopen", "task_id": task_id,
                                     "pre_state": state, "post_state": None}
                    elif state == "AWAITING_RISK":
                        attempt = record.get("queue_attempt")
                        risk = attempt.get("risk") if isinstance(attempt, dict) else None
                        operation = {"phase": "risk-ready-next" if isinstance(risk, dict)
                                     and risk.get("status") == "RISK_EVIDENCE_READY" else "review",
                                     "task_id": task_id, "pre_state": state, "post_state": None}
                    elif state == "REQUEUING_STALE":
                        operation = {"phase": "stale-next", "task_id": task_id,
                                     "pre_state": state, "post_state": None}
                    elif state == "MERGING":
                        operation = {"phase": "cas-finalize", "task_id": task_id,
                                     "pre_state": state, "post_state": None}
                    elif state == "QUEUED":
                        selected = select_next(controller, config)
                        if selected.get("task_id") != task_id:
                            raise MergeQueueError("frozen FIFO scope no longer owns the next legal task")
                        operation = {"phase": "compose", "task_id": task_id,
                                     "pre_state": state, "post_state": None}
                    else:
                        blocker = {"category": "unsupported_state", "task_id": task_id,
                                   "state": state}; break
                    if journal["attempts"]["transitions"] >= max_transitions:
                        blocker = {"category": "transition_budget",
                                   "max_transitions": max_transitions}; break
                    journal["attempts"]["transitions"] += 1
                    journal["operations"].append(operation)
                    lifecycle_runtime.lifecycle_checkpoint(
                        journal_path, journal, phase=operation["phase"], boundary="PRE",
                        detail={"task_id": task_id, "pre_state": state,
                                "transition": journal["attempts"]["transitions"]})
                    if operation["phase"] == "repair-reopen":
                        merge_reopen(controller, task_id)
                    elif operation["phase"] == "resolve-continue":
                        merge_resolve(controller, task_id)
                    elif operation["phase"] == "review":
                        # The managed review prompt binds full-suite receipts at
                        # render time, so Reviewer A can only dispatch after the
                        # suite admits. Overlap would dispatch A while the
                        # receipts cannot exist yet, so the drive never requests
                        # it; suite-first keeps the durable A-before-B ordering.
                        merge_review(controller, task_id, overlap_suite=False)
                    elif operation["phase"] in {"risk-ready-next", "stale-next"}:
                        merge_next(controller, task_id)
                    elif operation["phase"] == "cas-finalize":
                        merge_next(controller)
                    elif operation["phase"] == "compose":
                        merge_next(controller)
                    with task_runtime.state_lock(controller):
                        post = task_runtime.read_state(controller)["tasks"].get(task_id, {})
                    operation["post_state"] = post.get("state")
                    lifecycle_runtime.lifecycle_checkpoint(
                        journal_path, journal, phase=operation["phase"], boundary="POST",
                        detail={"task_id": task_id, "post_state": operation["post_state"],
                                "transition": journal["attempts"]["transitions"]})
                if blocker is not None:
                    break
            terminal = blocker is None
            return _merge_drive_projection(
                controller, repository, config, through, run_dir, latest_paths,
                journal_path, journal, plan, scope, blocker=blocker, terminal=terminal)
        except (MergeQueueError, task_runtime.TaskWorkspaceError,
                lifecycle_runtime.LifecycleContractError, OSError) as exc:
            lifecycle_runtime.lifecycle_checkpoint(
                journal_path, journal, phase="merge-drive", boundary="ERROR",
                detail={"error_type": type(exc).__name__, "error": str(exc)[:1024]})
            raise MergeQueueError(str(exc)) from exc


# One on-demand deterministic owner per protected target. The kernel lock is
# liveness truth; the durable attempt token fences delayed writers after a dead
# producer yields a successor. This is intentionally not a daemon: drive starts
# it only when FIFO work exists and the worker exits at idle or a typed blocker.
TARGET_ARBITER_SCHEMA = "juno_target_arbiter_attempt.v1"
TARGET_ARBITER_RECEIPT_SCHEMA = "juno_target_arbiter_receipt.v1"
TARGET_ARBITER_ROOT = ".juno_task/runtime/target-arbiters"
TARGET_ARBITER_WORK_STATES = MERGE_DRIVE_ELIGIBLE_STATES - {"MERGED"}


def _arbiter_root(controller: Path, repository: Path, target_ref: str) -> Path:
    identity = digest({"repository_identity": repository_identity(repository),
                       "target_ref": target_ref})
    return controller / TARGET_ARBITER_ROOT / identity


def _arbiter_state(root: Path) -> Optional[dict[str, Any]]:
    path = root / "state.json"
    if not path.is_file():
        return None
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or value.get("schema_version") != TARGET_ARBITER_SCHEMA:
        raise MergeQueueError("target arbiter state is malformed")
    return value


def _arbiter_observation(state: Optional[dict[str, Any]]) -> dict[str, str]:
    if not isinstance(state, dict) or state.get("state") != "ACTIVE":
        return {"status": "inactive", "detail": "no active target arbiter"}
    observation = task_runtime._observe_producer(state.get("producer"))
    return {"status": observation.status, "detail": observation.detail}


def target_arbiter_status(controller: Path) -> dict[str, Any]:
    config = task_runtime.load_config(controller)
    repository = task_runtime.product_repository(controller, config)
    root = _arbiter_root(controller, repository, config["target_ref"])
    state = _arbiter_state(root)
    observation = _arbiter_observation(state)
    with task_runtime.state_lock(controller):
        tasks = task_runtime.read_state(controller)["tasks"]
    eligible = [{"task_id": task_id, "state": row.get("state")}
                for task_id, row in tasks.items() if isinstance(row, dict)
                and row.get("target_ref") == config["target_ref"]
                and row.get("state") in TARGET_ARBITER_WORK_STATES]
    eligible.sort(key=lambda item: item["task_id"])
    conflict = next((row for row in eligible if row["state"] == "CONFLICT"), None)
    resume = task_runtime.decisions.plan_resume(task_runtime.decisions.ResumeFacts(
        owner="target", producer_status=observation["status"],
        launch_observed=state is not None,
        exact_terminal=isinstance(state, dict) and state.get("state") != "ACTIVE",
        resumable_stage="FINALIZING" if isinstance(state, dict)
        and state.get("outcome") == "POST_INTEGRATION_PENDING" else "FIFO",
        conflict=conflict is not None))
    if resume.classification == task_runtime.decisions.RESUME_LIVE_AUTHORITY:
        reason_code, next_action = "arbiter_running", "observe with: yy merge arbiter status"
    elif resume.classification == task_runtime.decisions.RESUME_UNKNOWN_OUTCOME:
        reason_code, next_action = resume.reason_code, "inspect target arbiter process-instance evidence"
    elif conflict is not None:
        reason_code = resume.reason_code
        next_action = f"yy merge resolve {conflict['task_id']}"
    elif eligible:
        reason_code, next_action = "eligible_work", "yy merge resume"
    else:
        reason_code, next_action = "queue_idle", "none: worker exits while target queue is idle"
    return {"schema_version": TARGET_ARBITER_SCHEMA,
            "target_ref": config["target_ref"],
            "target_sha": task_runtime.ref_sha(repository, config["target_ref"]),
            "state": state, "producer_observation": observation,
            "current_fifo": (current_fifo_identity(controller, config, None)
                             if eligible else None),
            "eligible_task_ids": [row["task_id"] for row in eligible],
            "resume_decision": {
                "classification": resume.classification, "admitted": resume.admitted,
                "owner_command": resume.owner_command,
                "restart_stage": resume.restart_stage,
                "reason_code": resume.reason_code},
            "reason_code": reason_code, "next_action": next_action}


def _arbiter_transition(root: Path, attempt: int, token: str, state_name: str,
                        *, outcome: str, detail: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Fenced terminal write; delayed predecessor tokens always fail closed."""
    current = _arbiter_state(root)
    token_sha256 = hashlib.sha256(token.encode()).hexdigest()
    if (not isinstance(current, dict) or current.get("attempt") != attempt
            or current.get("token_sha256") != token_sha256
            or current.get("state") != "ACTIVE"):
        raise MergeQueueError("target arbiter write refused (arbiter_fence_stale)")
    receipt_body = {"schema_version": TARGET_ARBITER_RECEIPT_SCHEMA,
                    "attempt": attempt, "target_ref": current["target_ref"],
                    "state": state_name, "outcome": outcome,
                    "producer": current["producer"], "detail": detail or {}}
    receipt_path = root / "receipts" / f"attempt-{attempt}-{state_name.lower()}.json"
    receipt = lifecycle_runtime.atomic_json(receipt_path, receipt_body, exclusive=True)
    terminal = {**current, "state": state_name, "outcome": outcome,
                "terminal_receipt": receipt, "detail": detail or {}}
    lifecycle_runtime.atomic_json(root / "state.json", terminal)
    return terminal


@contextmanager
def _target_arbiter_claim(root: Path) -> Iterator[Optional[Any]]:
    root.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(root / "owner.lock", os.O_RDWR | os.O_CREAT, 0o600)
    stream = os.fdopen(descriptor, "a+")
    try:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield None
            return
        yield stream
    finally:
        stream.close()


def _pre_cas_recovery_refuse(code: str, detail: str) -> None:
    raise MergeQueueError(f"pre-CAS edit recovery refused ({code}): {detail}")


def recover_pre_cas_authority_drift(controller: Path, task_id: str, arbiter_attempt: int,
                                     terminal_receipt_path: str,
                                     terminal_receipt_sha256: str,
                                     expected_record_revision: str) -> dict[str, Any]:
    """Return one receipt-proven pre-CAS authority failure to fenced WORKING.

    This operation is deliberately narrower than reopen: it performs no
    composition, validation, review, worker dispatch, cleanup, or ref mutation.
    The failed candidate and complete queue attempt remain immutable evidence.
    """
    if not task_runtime.TASK_RE.fullmatch(task_id):
        _pre_cas_recovery_refuse("task_mismatch", "task id is unsafe")
    if (not isinstance(arbiter_attempt, int) or isinstance(arbiter_attempt, bool)
            or arbiter_attempt < 1):
        _pre_cas_recovery_refuse("attempt_mismatch", "arbiter attempt is malformed")
    if not re.fullmatch(r"[0-9a-f]{64}", terminal_receipt_sha256 or ""):
        _pre_cas_recovery_refuse("receipt_malformed", "terminal receipt digest is malformed")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_record_revision or ""):
        _pre_cas_recovery_refuse("revision_mismatch", "lifecycle revision is malformed")

    config = task_runtime.load_config(controller)
    repository = task_runtime.product_repository(controller, config)
    arbiter_root = _arbiter_root(controller, repository, config["target_ref"])
    expected_receipt_path = (arbiter_root / "receipts"
                             / f"attempt-{arbiter_attempt}-failed.json").resolve()
    supplied_receipt_path = Path(terminal_receipt_path).expanduser().resolve()
    try:
        supplied_receipt_path.relative_to((arbiter_root / "receipts").resolve())
    except ValueError:
        _pre_cas_recovery_refuse("receipt_malformed", "terminal receipt escaped arbiter evidence")
    if not supplied_receipt_path.is_file():
        _pre_cas_recovery_refuse("receipt_malformed", "terminal receipt path is absent")
    terminal_bytes = supplied_receipt_path.read_bytes()
    if hashlib.sha256(terminal_bytes).hexdigest() != terminal_receipt_sha256:
        _pre_cas_recovery_refuse("receipt_malformed", "terminal receipt bytes do not match")
    try:
        terminal = json.loads(terminal_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError):
        _pre_cas_recovery_refuse("receipt_malformed", "terminal receipt is not valid JSON")

    with _target_arbiter_claim(arbiter_root) as arbiter_claim:
        if arbiter_claim is None:
            _pre_cas_recovery_refuse("live_arbiter", "target arbiter ownership is active")
        with target_lock(controller, repository, config["target_ref"]):
            arbiter = _arbiter_state(arbiter_root)
            if (not isinstance(arbiter, dict) or arbiter.get("attempt") != arbiter_attempt
                    or terminal.get("attempt") != arbiter_attempt):
                _pre_cas_recovery_refuse("attempt_mismatch", "terminal arbiter attempt moved")
            if supplied_receipt_path != expected_receipt_path:
                _pre_cas_recovery_refuse("receipt_malformed", "terminal receipt path is not canonical")
            if (arbiter.get("state") != "FAILED" or terminal.get("state") != "FAILED"
                    or terminal.get("schema_version") != TARGET_ARBITER_RECEIPT_SCHEMA):
                _pre_cas_recovery_refuse("receipt_malformed", "arbiter is not terminal FAILED")
            if (arbiter.get("terminal_receipt") != {
                    "path": str(supplied_receipt_path), "sha256": terminal_receipt_sha256}):
                _pre_cas_recovery_refuse("receipt_malformed", "arbiter receipt reference mismatched")
            if (terminal.get("target_ref") != config["target_ref"]
                    or arbiter.get("target_ref") != config["target_ref"]
                    or terminal.get("producer") != arbiter.get("producer")
                    or terminal.get("detail") != arbiter.get("detail")):
                _pre_cas_recovery_refuse("identity_mismatch", "terminal arbiter identity drifted")
            producer = task_runtime._observe_producer(arbiter.get("producer"))
            if producer.status != "dead":
                _pre_cas_recovery_refuse(
                    "live_producer", f"failed arbiter producer is {producer.status}: {producer.detail}")
            error = terminal.get("detail", {}).get("error") \
                if isinstance(terminal.get("detail"), dict) else None
            prefix = "live authority drift at before_target_cas: "
            if not isinstance(error, str) or not error.startswith(prefix):
                _pre_cas_recovery_refuse(
                    "not_pre_cas_authority_drift", "terminal failure is not the supported boundary")
            reason_codes = sorted(set(part.strip() for part in error[len(prefix):].split(",")
                                      if part.strip()))
            supported = {"BLOCKERS_PRESENT", "BLOCKERS_DRIFT", "TASK_REVISION_DRIFT",
                         "QUEUE_RECORD_DRIFT", "ADMITTED_PATHS_DRIFT",
                         "FIFO_DRIFT", "QUEUE_AUTHORITY_DRIFT"}
            if not reason_codes or any(code not in supported for code in reason_codes):
                _pre_cas_recovery_refuse(
                    "not_deterministic_policy_drift", "authority reason is outside the narrow policy set")

            with task_runtime.state_lock(controller):
                state = task_runtime.read_state(controller)
                record = state.get("tasks", {}).get(task_id)
                if isinstance(record, dict) and record.get("state") == "WORKING" \
                        and isinstance(record.get("pre_cas_authority_drift_recovery"), dict):
                    _pre_cas_recovery_refuse(
                        "already_recovered", "the failed attempt already issued editable authority")
                if not isinstance(record, dict):
                    _pre_cas_recovery_refuse("task_mismatch", "task lifecycle record is missing")
                source_state = record.get("state")
                if source_state in {"REVIEW_FINDINGS", "REVIEW_FINDINGS_EXHAUSTED"}:
                    _pre_cas_recovery_refuse("review_findings", "review findings cannot use this recovery")
                if source_state in {"CONFLICT", "CONFLICT_RESOLVED"}:
                    _pre_cas_recovery_refuse("conflict_state", "conflict recovery remains separately owned")
                if source_state != "MERGING":
                    _pre_cas_recovery_refuse("state_mismatch", "task is not in MERGING")
                if digest(record) != expected_record_revision:
                    _pre_cas_recovery_refuse("revision_mismatch", "lifecycle record changed")
                attempt = record.get("queue_attempt")
                if (not isinstance(attempt, dict) or attempt.get("schema_version") != ATTEMPT_SCHEMA
                        or attempt.get("task_id") != task_id):
                    _pre_cas_recovery_refuse("task_mismatch", "queue attempt identity is malformed")
                if attempt.get("outcome") != "MERGING":
                    _pre_cas_recovery_refuse("state_mismatch", "queue attempt is not pre-CAS MERGING")
                if attempt.get("target_ref") != config["target_ref"]:
                    _pre_cas_recovery_refuse("identity_mismatch", "target ref identity drifted")
                if (attempt.get("review", {}).get("reviews")
                        or attempt.get("risk", {}).get("reviews")
                        or attempt.get("blocking_findings")):
                    _pre_cas_recovery_refuse("review_findings", "review evidence is not empty")

                target_sha = task_runtime.ref_sha(repository, config["target_ref"])
                expected_target = attempt.get("expected_target_sha")
                candidate_sha = attempt.get("candidate_sha")
                source_tip = attempt.get("feature_sha")
                if (target_sha != expected_target or arbiter.get("target_sha_at_start") != target_sha
                        or candidate_sha == target_sha):
                    _pre_cas_recovery_refuse(
                        "post_cas_or_target_moved", "no-CAS target identity cannot be proven")
                if (not isinstance(candidate_sha, str)
                        or optional_revision(repository, candidate_sha) != candidate_sha
                        or task_runtime.git(repository, "rev-parse", f"{candidate_sha}^{{tree}}",
                                            check=False) != attempt.get("candidate_tree")):
                    _pre_cas_recovery_refuse("candidate_mismatch", "candidate commit/tree drifted")
                if (source_tip != record.get("tip_sha")
                        or optional_revision(repository, source_tip) != source_tip
                        or task_runtime.git(repository, "rev-parse", record.get("branch_ref", ""),
                                            check=False) != source_tip):
                    _pre_cas_recovery_refuse("source_mismatch", "source branch/tip drifted")

                worktree_value = record.get("worktree")
                checkout_value = attempt.get("candidate_checkout")
                try:
                    worktree = task_runtime.exact_root(Path(worktree_value), "feature worktree")
                    checkout = task_runtime.exact_root(Path(checkout_value), "candidate checkout")
                except (TypeError, task_runtime.TaskWorkspaceError):
                    _pre_cas_recovery_refuse("ambiguous_worktree", "worktree identity is absent or ambiguous")
                if (task_runtime.git(worktree, "symbolic-ref", "-q", "HEAD", check=False)
                        != record.get("branch_ref")
                        or task_runtime.git(worktree, "rev-parse", "HEAD", check=False) != source_tip):
                    _pre_cas_recovery_refuse("source_mismatch", "feature worktree identity drifted")
                if task_runtime.git(worktree, "status", "--porcelain=v1", "--untracked-files=all",
                                    check=False):
                    _pre_cas_recovery_refuse("dirty_worktree", "feature worktree is dirty")
                registered = [Path(row.get("worktree", "")).resolve()
                              for row in registered_worktrees(repository)
                              if row.get("worktree")]
                if registered.count(checkout.resolve()) != 1:
                    _pre_cas_recovery_refuse("ambiguous_worktree", "candidate worktree registration drifted")
                if (task_runtime.git(checkout, "rev-parse", "HEAD", check=False) != candidate_sha
                        or task_runtime.git(checkout, "symbolic-ref", "-q", "HEAD", check=False)
                        or task_runtime.git(checkout, "status", "--porcelain=v1", "--untracked-files=all",
                                            check=False)):
                    _pre_cas_recovery_refuse("dirty_worktree", "candidate worktree is not exact and clean")
                if task_runtime.git(checkout, "show", "-s", "--format=%P", candidate_sha).split() \
                        != [expected_target, source_tip]:
                    _pre_cas_recovery_refuse("candidate_mismatch", "candidate parents drifted")
                token = attempt.get("candidate_token")
                try:
                    verify_candidate_owner(controller, repository, checkout, token)
                except (MergeQueueError, TypeError):
                    _pre_cas_recovery_refuse("candidate_mismatch", "candidate ownership drifted")
                lease = task_runtime._lease_view(record)
                if not isinstance(lease, dict) or lease.get("state") != "RELEASED":
                    _pre_cas_recovery_refuse("live_producer", "task edit producer is not terminally released")

                receipt_body = {
                    "schema_version": PRE_CAS_EDIT_RECOVERY_SCHEMA,
                    "task_id": task_id, "source_tip": source_tip,
                    "source_tree": task_runtime.git(repository, "rev-parse", f"{source_tip}^{{tree}}"),
                    "candidate_sha": candidate_sha, "candidate_tree": attempt["candidate_tree"],
                    "target_ref": config["target_ref"], "target_sha": target_sha,
                    "arbiter_attempt": arbiter_attempt,
                    "terminal_receipt": {"path": str(supplied_receipt_path),
                                         "sha256": terminal_receipt_sha256},
                    "terminal_reason_codes": reason_codes, "no_cas_proven": True,
                    "producer_observation": {"status": producer.status,
                                             "detail": producer.detail},
                    "feature_worktree": str(worktree), "candidate_worktree": str(checkout),
                    "worktrees_clean": True,
                    "expected_record_revision": expected_record_revision,
                    "preserved_queue_attempt_sha256": digest(attempt),
                }
                recovery_path = (controller / PRE_CAS_EDIT_RECOVERY_ROOT / task_id
                                 / f"attempt-{arbiter_attempt}-{candidate_sha}.json")
                data = (canonical(receipt_body) + "\n").encode()
                if recovery_path.is_file():
                    if recovery_path.read_bytes() != data:
                        _pre_cas_recovery_refuse("receipt_collision", "recovery evidence path collided")
                else:
                    write_canonical_exclusive(recovery_path, receipt_body, 65536)
                recovery_receipt = evidence_reference(recovery_path)
                lease_authority_receipt = {
                    "path": recovery_receipt["receipt_path"],
                    "sha256": recovery_receipt["receipt_sha256"],
                }
                lease_next, lease_token = task_runtime._new_lease(
                    task_id, int(lease.get("attempt") or 0) + 1, "process",
                    "pre_cas_authority_drift_recovery", lease_authority_receipt,
                    reason="receipt-proven pre-CAS authority drift", producer_pid=os.getpid(),
                    recovery={"classification": "clean_resume",
                              "preserved_candidate_sha": candidate_sha})
                recovered = {key: value for key, value in record.items()
                             if key not in {"queue_attempt", "last_queue_outcome",
                                            "enqueue_sequence", "review_ready_closure"}}
                recovered.update({
                    "state": "WORKING",
                    "pre_cas_authority_drift_recovery": {
                        "schema_version": PRE_CAS_EDIT_RECOVERY_SCHEMA,
                        "receipt": recovery_receipt,
                        "preserved_queue_attempt": attempt,
                        "safe_next_command": f"append one repair commit, then yy task preflight {task_id}",
                    },
                })
                recovered = task_runtime._apply_lease(recovered, lease_next)
                state["tasks"][task_id] = recovered
                task_runtime.write_state(controller, state)
            _project_queue_board_state(controller, task_id, "WORKING")
            return {"schema_version": PRE_CAS_EDIT_RECOVERY_SCHEMA, "task_id": task_id,
                    "outcome": "PRE_CAS_AUTHORITY_DRIFT_RECOVERED",
                    "source_tip": source_tip, "candidate_sha": candidate_sha,
                    "target_sha": target_sha, "arbiter_attempt": arbiter_attempt,
                    "receipt": recovery_receipt, "lease_token": lease_token,
                    "safe_next_command": f"append one repair commit, then yy task preflight {task_id}"}



def _lifecycle_supersession_refuse(code: str, detail: str) -> None:
    raise MergeQueueError(f"lifecycle journal supersession refused ({code}): {detail}")


def _verified_supersession_artifact(path_value: str, sha256: str, root: Path,
                                    schema: str, code: str) -> tuple[Path, dict[str, Any]]:
    if not re.fullmatch(r"[0-9a-f]{64}", sha256 or ""):
        _lifecycle_supersession_refuse(code, "artifact digest is malformed")
    try:
        path = Path(path_value).expanduser().resolve()
        path.relative_to(root.resolve())
        raw = path.read_bytes()
        value = json.loads(raw)
    except (TypeError, ValueError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        _lifecycle_supersession_refuse(code, "artifact path or bytes are malformed")
    if hashlib.sha256(raw).hexdigest() != sha256:
        _lifecycle_supersession_refuse(code, "artifact bytes do not match the bound digest")
    if not isinstance(value, dict) or value.get("schema_version") != schema:
        _lifecycle_supersession_refuse(code, "artifact schema is not supported")
    return path, value


def supersede_stale_lifecycle_journal(
        controller: Path, run_id: str, expected_journal_revision: int,
        expected_journal_sha256: str, scope_sha256: str, arbiter_attempt: int,
        terminal_receipt_path: str, terminal_receipt_sha256: str,
        recovered_task_id: str, recovery_receipt_path: str,
        recovery_receipt_sha256: str, expected_target_sha: str,
        expected_current_fifo_sha256: str) -> dict[str, Any]:
    """Terminalize one receipt-recovered, pre-CAS stale merge-drive lineage."""
    if not re.fullmatch(r"[0-9]{10,}-[0-9a-f]{16}", run_id or ""):
        _lifecycle_supersession_refuse("malformed_evidence", "run id is malformed")
    if not task_runtime.TASK_RE.fullmatch(recovered_task_id or ""):
        _lifecycle_supersession_refuse("malformed_evidence", "recovered task id is unsafe")
    if (not isinstance(expected_journal_revision, int)
            or isinstance(expected_journal_revision, bool) or expected_journal_revision < 1):
        _lifecycle_supersession_refuse("changed_revision", "journal revision is malformed")
    for value, label in ((expected_journal_sha256, "journal"),
                         (scope_sha256, "scope"), (expected_target_sha, "target"),
                         (expected_current_fifo_sha256, "current FIFO")):
        width = 40 if label == "target" else 64
        if not re.fullmatch(rf"[0-9a-f]{{{width}}}", value or ""):
            _lifecycle_supersession_refuse("malformed_evidence", f"{label} identity is malformed")
    if not isinstance(arbiter_attempt, int) or arbiter_attempt < 1:
        _lifecycle_supersession_refuse("failed_arbiter_mismatch", "arbiter attempt is malformed")

    config = task_runtime.load_config(controller)
    repository = task_runtime.product_repository(controller, config)
    run_dir = controller / MERGE_DRIVE_ROOT / run_id
    journal_path = run_dir / "journal.json"
    if not journal_path.is_file():
        _lifecycle_supersession_refuse("malformed_evidence", "canonical lifecycle journal is absent")
    initial_raw = journal_path.read_bytes()
    try:
        journal = json.loads(initial_raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        _lifecycle_supersession_refuse("malformed_evidence", "journal bytes are malformed")
    supplied_binding = {
        "run_id": run_id, "journal_revision": expected_journal_revision,
        "journal_sha256": expected_journal_sha256, "scope_sha256": scope_sha256,
        "arbiter_attempt": arbiter_attempt,
        "terminal_receipt_sha256": terminal_receipt_sha256,
        "recovered_task_id": recovered_task_id,
        "recovery_receipt_sha256": recovery_receipt_sha256,
        "target_ref": config["target_ref"], "target_sha": expected_target_sha,
        "current_fifo_sha256": expected_current_fifo_sha256,
    }
    existing = journal.get("supersession") if isinstance(journal, dict) else None
    if journal.get("terminal") and journal.get("state") == "SUPERSEDED" \
            and isinstance(existing, dict):
        if existing.get("binding") != supplied_binding:
            _lifecycle_supersession_refuse("changed_revision", "terminal supersession binding differs")
        projection_ref = existing.get("projection")
        summary_ref = existing.get("summary")
        if not isinstance(projection_ref, dict) or not isinstance(summary_ref, dict):
            _lifecycle_supersession_refuse("malformed_evidence", "terminal projection references are absent")
        projection = lifecycle_runtime.verified_projection_bytes(
            Path(projection_ref["path"]), expected_sha256=projection_ref.get("sha256"),
            kind="merge-drive", run_id=run_id)
        summary_path = Path(summary_ref.get("path", ""))
        if (not summary_path.is_file()
                or hashlib.sha256(summary_path.read_bytes()).hexdigest() != summary_ref.get("sha256")):
            _lifecycle_supersession_refuse("malformed_evidence", "terminal summary bytes drifted")
        return {"schema_version": LIFECYCLE_SUPERSESSION_SCHEMA, "run_id": run_id,
                "state": "SUPERSEDED", "projection": projection_ref,
                "summary": summary_ref, "idempotent": True,
                "safe_next_command": "yy merge arbiter run"}
    if journal.get("terminal") or journal.get("state") != "CLAIMED" \
            or journal.get("schema_version") != "juno_managed_merge_drive_journal.v2":
        _lifecycle_supersession_refuse("malformed_evidence", "journal is not nonterminal CLAIMED")
    if (hashlib.sha256(initial_raw).hexdigest() != expected_journal_sha256
            or journal.get("journal_revision") != expected_journal_revision):
        _lifecycle_supersession_refuse("changed_revision", "journal revision or digest changed")
    if journal.get("run_id") != run_id or journal.get("scope_sha256") != scope_sha256:
        _lifecycle_supersession_refuse("scope_mismatch", "journal run or frozen scope mismatched")

    if journal.get("projections"):
        _lifecycle_supersession_refuse(
            "malformed_evidence", "nonterminal journal already has an ambiguous projection")
    stale_errors = [event.get("detail", {}).get("error") for event in journal.get("events", [])
                    if isinstance(event, dict) and event.get("boundary") == "ERROR"
                    and isinstance(event.get("detail"), dict)]
    if not stale_errors or stale_errors[-1] != "frozen FIFO scope no longer owns the next legal task":
        _lifecycle_supersession_refuse("scope_mismatch", "journal does not end in stale FIFO refusal")
    plan_ref = journal.get("compiled_plan")
    if not isinstance(plan_ref, dict):
        _lifecycle_supersession_refuse("malformed_evidence", "compiled plan reference is absent")
    plan_path, plan = _verified_supersession_artifact(
        str(plan_ref.get("path", "")), str(plan_ref.get("sha256", "")), run_dir,
        "juno_compiled_lifecycle_plan.v1", "malformed_evidence")
    if plan_path != (run_dir / "compiled-plan.json").resolve():
        _lifecycle_supersession_refuse("malformed_evidence", "compiled plan path is not canonical")
    scope_ref = journal.get("fifo_scope")
    if not isinstance(scope_ref, dict):
        _lifecycle_supersession_refuse("malformed_evidence", "frozen scope reference is absent")
    scope_path, scope = _verified_supersession_artifact(
        str(scope_ref.get("path", "")), str(scope_ref.get("sha256", "")), run_dir,
        "juno_merge_drive_fifo_scope.v1", "malformed_evidence")
    if (scope_path != (run_dir / "fifo-scope.json").resolve()
            or scope.get("scope_sha256") != scope_sha256
            or scope.get("target_ref") != config["target_ref"]):
        _lifecycle_supersession_refuse("scope_mismatch", "frozen scope identity is not exact")
    frozen_actionable = [row.get("task_id") for row in scope.get("tasks", [])
                         if isinstance(row, dict) and row.get("initial_state") != "MERGED"]
    if frozen_actionable != [recovered_task_id]:
        _lifecycle_supersession_refuse("scope_mismatch", "recovered task is not the sole frozen action")
    selector_root = controller / MERGE_DRIVE_ROOT / "scopes" / journal["selector_identity_sha256"]
    pointer_paths = (controller / MERGE_DRIVE_ROOT / "latest.json", selector_root / "latest.json")
    try:
        pointers = [json.loads(path.read_text()) for path in pointer_paths]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        _lifecycle_supersession_refuse("malformed_evidence", "lifecycle latest pointer is absent or malformed")
    if any(not isinstance(pointer, dict)
           or pointer.get("schema_version") != "juno_managed_merge_drive_latest.v2"
           or pointer.get("run_id") != run_id or pointer.get("scope_sha256") != scope_sha256
           or pointer.get("terminal") is not False for pointer in pointers):
        _lifecycle_supersession_refuse("scope_mismatch", "stale run is not the exact active lifecycle operation")

    arbiter_root = _arbiter_root(controller, repository, config["target_ref"])
    expected_terminal = (arbiter_root / "receipts"
                         / f"attempt-{arbiter_attempt}-failed.json").resolve()
    terminal_path, terminal = _verified_supersession_artifact(
        terminal_receipt_path, terminal_receipt_sha256, arbiter_root / "receipts",
        TARGET_ARBITER_RECEIPT_SCHEMA, "failed_arbiter_mismatch")
    arbiter = _arbiter_state(arbiter_root)
    if (terminal_path != expected_terminal or not isinstance(arbiter, dict)
            or arbiter.get("attempt") != arbiter_attempt or arbiter.get("state") != "FAILED"
            or arbiter.get("terminal_receipt") != {"path": str(terminal_path),
                                                    "sha256": terminal_receipt_sha256}
            or terminal.get("attempt") != arbiter_attempt or terminal.get("state") != "FAILED"
            or terminal.get("target_ref") != config["target_ref"]
            or terminal.get("producer") != arbiter.get("producer")
            or terminal.get("detail") != arbiter.get("detail")):
        _lifecycle_supersession_refuse("failed_arbiter_mismatch", "failed arbiter evidence is ambiguous")
    error = terminal.get("detail", {}).get("error") \
        if isinstance(terminal.get("detail"), dict) else None
    if error != "frozen FIFO scope no longer owns the next legal task":
        _lifecycle_supersession_refuse("failed_arbiter_mismatch", "arbiter did not fail on stale scope")
    producer = task_runtime._observe_producer(arbiter.get("producer"))
    if producer.status != "dead":
        _lifecycle_supersession_refuse("live_producer", producer.detail)

    recovery_root = controller / PRE_CAS_EDIT_RECOVERY_ROOT / recovered_task_id
    recovery_path, recovery = _verified_supersession_artifact(
        recovery_receipt_path, recovery_receipt_sha256, recovery_root,
        PRE_CAS_EDIT_RECOVERY_SCHEMA, "missing_recovery_lineage")
    with task_runtime.state_lock(controller):
        state = task_runtime.read_state(controller)
        recovered = state.get("tasks", {}).get(recovered_task_id)
    lineage = recovered.get("pre_cas_authority_drift_recovery") \
        if isinstance(recovered, dict) else None
    lineage_ref = lineage.get("receipt") if isinstance(lineage, dict) else None
    if (not isinstance(recovered, dict) or recovered.get("state") != "QUEUED"
            or not isinstance(lineage_ref, dict)
            or lineage_ref.get("receipt_path") != str(recovery_path)
            or lineage_ref.get("receipt_sha256") != recovery_receipt_sha256
            or recovery.get("task_id") != recovered_task_id
            or recovery.get("target_ref") != config["target_ref"]
            or recovery.get("no_cas_proven") is not True):
        _lifecycle_supersession_refuse("missing_recovery_lineage", "recovered/requeued task lineage is absent")

    current_target = task_runtime.ref_sha(repository, config["target_ref"])
    if current_target != expected_target_sha:
        _lifecycle_supersession_refuse("target_drift", "protected target moved from the supplied identity")
    if (journal.get("initial_target_sha") != expected_target_sha
            or scope.get("target_sha") != expected_target_sha
            or arbiter.get("target_sha_at_start") != expected_target_sha
            or recovery.get("target_sha") != expected_target_sha
            or any(row.get("post_state") == "MERGED" for row in journal.get("operations", [])
                   if isinstance(row, dict))):
        _lifecycle_supersession_refuse("post_cas", "no-CAS lineage cannot be proven")
    fifo = current_fifo_identity(controller, config, None)
    if fifo["sha256"] != expected_current_fifo_sha256:
        _lifecycle_supersession_refuse("current_fifo_changed", "current FIFO identity changed")
    current_ids = [row.get("task_id") for row in fifo["tasks"]]
    if current_ids == frozen_actionable:
        _lifecycle_supersession_refuse("current_valid_scope", "frozen scope still matches current FIFO")
    if recovered_task_id not in current_ids or not current_ids or current_ids[0] == recovered_task_id:
        _lifecycle_supersession_refuse("current_fifo_mismatch", "recovered task/current predecessor order is invalid")

    with lifecycle_runtime.lifecycle_claim(selector_root / ".claim.lock"):
        # Final compare-and-append closes races with another recovery or resume.
        if journal_path.read_bytes() != initial_raw:
            _lifecycle_supersession_refuse("changed_revision", "journal changed before terminal append")
        if current_fifo_identity(controller, config, None)["sha256"] != expected_current_fifo_sha256:
            _lifecycle_supersession_refuse("current_fifo_changed", "current FIFO changed before append")
        elapsed_ms = max(0, (int(journal.get("updated_at_unix_ns", 0))
                             - int(journal.get("started_at_unix_ns", 0))) // 1_000_000)
        projection = lifecycle_runtime.compact_projection(
            kind="merge-drive", run_id=run_id, task_id=recovered_task_id,
            state="SUPERSEDED", plan=plan,
            started=time.monotonic(), counters={name: 0 for name in
                ("executed", "reused", "invalidated", "skipped", "not_applicable")},
            attempts={"transitions": journal["attempts"]["transitions"],
                      "semantic_repairs": journal["attempts"]["semantic_repairs"],
                      "reviewer_attempts": 0}, blocker=None,
            next_action="run a fresh FIFO compiler with: yy merge arbiter run",
            artifacts=[journal["compiled_plan"], journal["fifo_scope"],
                       {"path": str(terminal_path), "sha256": terminal_receipt_sha256},
                       {"path": str(recovery_path), "sha256": recovery_receipt_sha256}],
            identities={"supersession_schema": LIFECYCLE_SUPERSESSION_SCHEMA,
                        **supplied_binding, "current_fifo_task_ids": current_ids})
        projection["elapsed_ms"] = elapsed_ms
        body = {key: value for key, value in projection.items() if key != "projection_sha256"}
        projection["projection_sha256"] = lifecycle_runtime.digest(body)
        projection_path = run_dir / "projections" / "0001-superseded.json"
        expected_projection_bytes = lifecycle_runtime.canonical_bytes(projection)
        if projection_path.is_file():
            if projection_path.read_bytes() != expected_projection_bytes:
                _lifecycle_supersession_refuse(
                    "malformed_evidence", "stranded supersession projection collided")
            projection_ref = {"path": str(projection_path.resolve()),
                              "sha256": hashlib.sha256(expected_projection_bytes).hexdigest()}
        else:
            projection_ref = lifecycle_runtime.atomic_json(
                projection_path, projection, exclusive=True)
        summary = lifecycle_runtime.deterministic_summary(projection)
        summary_path = run_dir / "summary.json"
        expected_summary_bytes = lifecycle_runtime.canonical_bytes(summary)
        if summary_path.is_file():
            if summary_path.read_bytes() != expected_summary_bytes:
                _lifecycle_supersession_refuse(
                    "malformed_evidence", "stranded supersession summary collided")
            summary_ref = {"path": str(summary_path.resolve()),
                           "sha256": hashlib.sha256(expected_summary_bytes).hexdigest()}
        else:
            summary_ref = lifecycle_runtime.atomic_json(summary_path, summary, exclusive=True)
        event = {"schema_version": "juno_lifecycle_phase_checkpoint.v1",
                 "sequence": len(journal.get("events", [])) + 1,
                 "phase": "lifecycle-supersession", "boundary": "POST",
                 "recorded_at_unix_ns": time.time_ns(),
                 "detail": {"schema_version": LIFECYCLE_SUPERSESSION_SCHEMA,
                            "recovered_task_id": recovered_task_id,
                            "current_fifo_sha256": expected_current_fifo_sha256,
                            "failed_arbiter_receipt_sha256": terminal_receipt_sha256,
                            "no_cas_proven": True}}
        journal.setdefault("events", []).append(event)
        journal.setdefault("projections", []).append(projection_ref)
        journal["state"] = "SUPERSEDED"; journal["terminal"] = True; journal["blocker"] = None
        journal["supersession"] = {"schema_version": LIFECYCLE_SUPERSESSION_SCHEMA,
                                   "binding": supplied_binding, "projection": projection_ref,
                                   "summary": summary_ref}
        lifecycle_runtime.lifecycle_journal_write(journal_path, journal)
        pointer = {"schema_version": "juno_managed_merge_drive_latest.v2",
                   "run_id": run_id, "scope_sha256": scope_sha256,
                   "compiled_plan_sha256": plan["compiled_plan_sha256"],
                   "execution_identity_sha256": journal["execution_identity_sha256"],
                   "projection_path": projection_ref["path"], "summary": summary_ref,
                   "terminal": True}
        lifecycle_runtime.atomic_json(selector_root / "latest.json", pointer)
        lifecycle_runtime.atomic_json(controller / MERGE_DRIVE_ROOT / "latest.json", pointer)
    return {"schema_version": LIFECYCLE_SUPERSESSION_SCHEMA, "run_id": run_id,
            "state": "SUPERSEDED", "projection": projection_ref, "summary": summary_ref,
            "idempotent": False, "queue_mutated": False,
            "safe_next_command": "yy merge arbiter run"}


def merge_drive(controller: Path, through: Optional[str] = None) -> dict[str, Any]:
    """Run the on-demand per-target arbiter until idle or one typed blocker."""
    config = task_runtime.load_config(controller)
    repository = task_runtime.product_repository(controller, config)
    # Admission is read-only. No worker attempt is created for an idle queue.
    admission = target_arbiter_status(controller)
    if not admission["eligible_task_ids"]:
        # Preserve immutable terminal-drive replay for observers without
        # creating a new arbiter attempt. A never-used empty queue is plain IDLE.
        if not (controller / MERGE_DRIVE_ROOT / "latest.json").is_file():
            return {**admission, "outcome": "IDLE"}
        try:
            return _merge_drive_claimed(controller, through)
        except MergeQueueError as exc:
            if "has no FIFO-authorized tasks" not in str(exc):
                raise
            return {**admission, "outcome": "IDLE"}
    _drive_scope(controller, config, through)
    root = _arbiter_root(controller, repository, config["target_ref"])
    with _target_arbiter_claim(root) as claim:
        if claim is None:
            return {**target_arbiter_status(controller), "outcome": "ALREADY_RUNNING",
                    "reason_code": "arbiter_owned"}
        # Recheck after ownership acquisition so two arrivals cannot create an
        # idle attempt after the first worker drains the queue.
        admission = target_arbiter_status(controller)
        if not admission["eligible_task_ids"]:
            if not (controller / MERGE_DRIVE_ROOT / "latest.json").is_file():
                return {**admission, "outcome": "IDLE"}
            try:
                return _merge_drive_claimed(controller, through)
            except MergeQueueError as exc:
                if "has no FIFO-authorized tasks" not in str(exc):
                    raise
                return {**admission, "outcome": "IDLE"}
        _drive_scope(controller, config, through)
        previous = _arbiter_state(root)
        observation = _arbiter_observation(previous)
        resume = task_runtime.decisions.plan_resume(task_runtime.decisions.ResumeFacts(
            owner="target", producer_status=observation["status"],
            launch_observed=previous is not None,
            exact_terminal=isinstance(previous, dict) and previous.get("state") != "ACTIVE",
            resumable_stage="FIFO"))
        if previous and previous.get("state") == "ACTIVE" and not resume.admitted:
            raise MergeQueueError(
                f"target arbiter resume refused ({resume.reason_code}); "
                "expiry alone never grants takeover")
        attempt = int((previous or {}).get("attempt") or 0) + 1
        token = secrets.token_urlsafe(32)
        active = {"schema_version": TARGET_ARBITER_SCHEMA,
                  "attempt": attempt, "state": "ACTIVE",
                  "target_ref": config["target_ref"],
                  "target_sha_at_start": task_runtime.ref_sha(repository, config["target_ref"]),
                  "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                  "producer": {"pid": os.getpid(),
                               "lstart": task_runtime._producer_lstart(os.getpid())},
                  "successor_of": ((previous or {}).get("terminal_receipt")
                                   or ({"attempt": previous.get("attempt"),
                                        "authority": "producer_death",
                                        "observation": observation}
                                       if previous else None))}
        lifecycle_runtime.atomic_json(root / "state.json", active)
        try:
            result = _merge_drive_claimed(controller, through)
            terminal_state = "IDLE" if result.get("state") == "MERGED_THROUGH" else "PAUSED"
            terminal = _arbiter_transition(
                root, attempt, token, terminal_state,
                outcome=str(result.get("state") or result.get("outcome") or terminal_state),
                detail={"projection_schema": result.get("schema_version"),
                        "run_id": result.get("run_id"), "blocker": result.get("blocker")})
            return {**result, "arbiter": terminal}
        except Exception as exc:
            _arbiter_transition(root, attempt, token, "FAILED", outcome=type(exc).__name__,
                                detail={"error": str(exc)[:1024]})
            raise


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    sub = value.add_subparsers(dest="operation", required=True)
    status_command = sub.add_parser("status")
    status_mode = status_command.add_mutually_exclusive_group()
    status_mode.add_argument("--detail", nargs="?", const="")
    status_mode.add_argument("--full", action="store_true")
    status_command.add_argument("--human", action="store_true", help=argparse.SUPPRESS)
    drive = sub.add_parser("drive")
    drive.add_argument("--through")
    resume = sub.add_parser("resume")
    resume.add_argument("--through")
    arbiter = sub.add_parser("arbiter")
    arbiter_sub = arbiter.add_subparsers(dest="arbiter_operation", required=True)
    arbiter_sub.add_parser("status")
    arbiter_run = arbiter_sub.add_parser("run")
    arbiter_run.add_argument("--through")
    plan = sub.add_parser("plan")
    plan.add_argument("task_id")
    plan.add_argument("--against")
    plan.add_argument("--json", action="store_true")
    next_command = sub.add_parser("next")
    next_command.add_argument("task_id", nargs="?")
    next_command.add_argument("--plan-id")
    resolve = sub.add_parser("resolve")
    resolve.add_argument("task_id")
    resolve.add_argument("--plan-id")
    review = sub.add_parser("review")
    review.add_argument("task_id")
    reopen = sub.add_parser("reopen")
    reopen.add_argument("task_id")
    reopen.add_argument("--plan-id")
    recover_suite = sub.add_parser("recover-full-suite-failure")
    recover_suite.add_argument("task_id")
    recover_suite.add_argument("--attempt", required=True, type=int)
    recover_suite.add_argument("--terminal-receipt", required=True)
    recover_suite.add_argument("--terminal-receipt-sha256", required=True)
    recover_suite.add_argument("--expected-revision", required=True)
    recover_suite.add_argument("--run-id", required=True)
    recover_suite.add_argument("--scope-sha256", required=True)
    recover_suite.add_argument("--journal-sha256", required=True)
    recover_repair = sub.add_parser("recover-repair-predispatch")
    recover_repair.add_argument("task_id")
    recover_repair.add_argument("--attempt", required=True, type=int)
    recover_repair.add_argument("--terminal-receipt", required=True)
    recover_repair.add_argument("--terminal-receipt-sha256", required=True)
    recover_repair.add_argument("--expected-revision", required=True)
    recover_repair.add_argument("--run-id", required=True)
    recover_repair.add_argument("--scope-sha256", required=True)
    recover_repair.add_argument("--journal-sha256", required=True)
    recover_repair.add_argument("--worker-id", required=True)
    recover_repair.add_argument("--predispatch-receipt", required=True)
    recover_repair.add_argument("--predispatch-receipt-sha256", required=True)
    recover_drift = sub.add_parser("recover-authority-drift")
    recover_drift.add_argument("task_id")
    recover_drift.add_argument("--attempt", required=True, type=int)
    recover_drift.add_argument("--terminal-receipt", required=True)
    recover_drift.add_argument("--terminal-receipt-sha256", required=True)
    recover_drift.add_argument("--expected-revision", required=True)
    supersede = sub.add_parser("supersede-lifecycle-journal")
    supersede.add_argument("--run-id", required=True)
    supersede.add_argument("--expected-journal-revision", required=True, type=int)
    supersede.add_argument("--expected-journal-sha256", required=True)
    supersede.add_argument("--scope-sha256", required=True)
    supersede.add_argument("--arbiter-attempt", required=True, type=int)
    supersede.add_argument("--terminal-receipt", required=True)
    supersede.add_argument("--terminal-receipt-sha256", required=True)
    supersede.add_argument("--recovered-task", required=True)
    supersede.add_argument("--recovery-receipt", required=True)
    supersede.add_argument("--recovery-receipt-sha256", required=True)
    supersede.add_argument("--expected-target-sha", required=True)
    supersede.add_argument("--expected-current-fifo-sha256", required=True)
    withdraw = sub.add_parser("withdraw")
    withdraw.add_argument("task_id")
    withdraw.add_argument("--reason")
    reconcile = sub.add_parser("reconcile")
    reconcile_sub = reconcile.add_subparsers(dest="reconcile_operation", required=True)
    reconcile_plan = reconcile_sub.add_parser("plan")
    reconcile_plan.add_argument("task_id")
    reconcile_apply = reconcile_sub.add_parser("apply")
    reconcile_apply.add_argument("task_id")
    reconcile_apply.add_argument("--receipt", required=True)
    reconcile_apply.add_argument("--receipt-sha256", required=True)
    refresh = sub.add_parser("refresh")
    refresh_sub = refresh.add_subparsers(dest="refresh_operation", required=True)
    refresh_plan = refresh_sub.add_parser("plan")
    refresh_plan.add_argument("task_id")
    refresh_apply = refresh_sub.add_parser("apply")
    refresh_apply.add_argument("task_id")
    refresh_apply.add_argument("--receipt", required=True)
    refresh_apply.add_argument("--receipt-sha256", required=True)
    value.add_argument("--controller", type=Path, default=Path.cwd(), help=argparse.SUPPRESS)
    return value


def human_plan(report: dict[str, Any]) -> str:
    lines = [f"Merge feasibility plan {report['plan_id']}",
             f"task: {report['task_id']}",
             f"ready: {'yes' if report['ready'] else 'no'}"]
    findings = report.get("findings", [])
    lines.append(f"findings: {len(findings)}")
    for row in findings:
        lines.append(f"- [{row['severity']}] {row['code']} ({row['phase']})")
        lines.append(f"  repair: {row['repair_command']}")
    lines.append("validation commands:")
    for row in report.get("validation_commands", []):
        lines.append(f"- ({row['cwd']}) {row['command']}")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    args = parser().parse_args(argv)
    try:
        controller = task_runtime.exact_root(args.controller, "controller")
        if args.operation == "plan":
            result = merge_plan(controller, args.task_id, args.against)
            print(canonical(result) if args.json else human_plan(result))
            return 0
        audit_operation = "drive" if args.operation == "resume" else args.operation
        if args.operation == "arbiter":
            audit_operation = "status" if args.arbiter_operation == "status" else "drive"
        audit_task_id = getattr(args, "task_id", None)
        if args.operation == "supersede-lifecycle-journal":
            audit_task_id = args.recovered_task
        audit = task_runtime.record_control_audit(
            controller, "merge", audit_operation, audit_task_id)
        if args.operation == "status":
            level = "full" if args.full else "detail" if args.detail is not None else "summary"
            result = status_projection(controller, level=level, task_id=(args.detail or None))
        elif args.operation in {"drive", "resume"}:
            result = merge_drive(controller, args.through)
            if args.operation == "resume":
                result = {**result, "resume_owner": "target-arbiter"}
        elif args.operation == "arbiter":
            result = (target_arbiter_status(controller) if args.arbiter_operation == "status"
                      else merge_drive(controller, args.through))
        elif args.operation == "next":
            result = merge_next(controller, args.task_id, args.plan_id)
        elif args.operation == "resolve":
            result = merge_resolve(controller, args.task_id, args.plan_id)
        elif args.operation == "review":
            result = merge_review(controller, args.task_id)
        elif args.operation == "reopen":
            result = merge_reopen(controller, args.task_id, args.plan_id)
        elif args.operation == "recover-full-suite-failure":
            result = recover_deterministic_full_suite_failure(
                controller, args.task_id, args.attempt, args.terminal_receipt,
                args.terminal_receipt_sha256, args.expected_revision, args.run_id,
                args.scope_sha256, args.journal_sha256)
        elif args.operation == "recover-repair-predispatch":
            result = recover_repair_predispatch(
                controller, args.task_id, args.attempt, args.terminal_receipt,
                args.terminal_receipt_sha256, args.expected_revision, args.run_id,
                args.scope_sha256, args.journal_sha256, args.worker_id,
                args.predispatch_receipt, args.predispatch_receipt_sha256)
        elif args.operation == "recover-authority-drift":
            result = recover_pre_cas_authority_drift(
                controller, args.task_id, args.attempt, args.terminal_receipt,
                args.terminal_receipt_sha256, args.expected_revision)
        elif args.operation == "supersede-lifecycle-journal":
            result = supersede_stale_lifecycle_journal(
                controller, args.run_id, args.expected_journal_revision,
                args.expected_journal_sha256, args.scope_sha256, args.arbiter_attempt,
                args.terminal_receipt, args.terminal_receipt_sha256,
                args.recovered_task, args.recovery_receipt,
                args.recovery_receipt_sha256, args.expected_target_sha,
                args.expected_current_fifo_sha256)
        elif args.operation == "withdraw":
            result = merge_withdraw(controller, args.task_id, args.reason)
        elif args.operation == "reconcile":
            if args.reconcile_operation == "plan":
                result = persist_terminal_reconciliation_plan(controller, args.task_id)
            else:
                result = apply_terminal_reconciliation(
                    controller, args.task_id, args.receipt, args.receipt_sha256)
        elif args.refresh_operation == "plan":
            result = persist_target_refresh_plan(controller, args.task_id)
        else:
            result = apply_target_refresh(
                controller, args.task_id, args.receipt, args.receipt_sha256)
        result = {**result, "control_audit": audit}
        if (args.operation == "status" and args.human and not args.full):
            rendered = human_status(result)
        else:
            rendered = canonical(result)
        if (args.operation == "status" and not args.full
                and len((rendered + "\n").encode()) > MERGE_STATUS_MAX_BYTES):
            raise MergeQueueError("bounded merge status exceeded its enforced byte limit")
        print(rendered)
        return 0
    except (MergeQueueError, task_runtime.TaskWorkspaceError, risk_runtime.RiskPolicyError,
            OSError, json.JSONDecodeError) as exc:
        print(f"merge queue: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
