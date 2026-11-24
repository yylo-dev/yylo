#!/usr/bin/env python3
"""One-task native Git delivery adapter.

Git owns composition, conflicts, ancestry, and expected-old ref updates. Ledger
projection is a separate idempotent command and never gates another task's land.
Historical queue receipts remain inert data; this module does not interpret them.
Retired compatibility vocabulary (non-executable): target_key(repository, target_ref),
juno-locks/merge-queue, fcntl.LOCK_EX, candidate_sha, expected_sha.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

import task_workspace as task_runtime

SCHEMA = "juno_native_git_delivery.v1"
TASK_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
ACTIVE_STATES = {"QUEUED", "CONFLICT", "GIT_INTEGRATED"}


class DeliveryError(RuntimeError):
    pass


def git(repository: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", "-C", str(repository), *args], text=True,
                            stdin=subprocess.DEVNULL, capture_output=True)
    if check and result.returncode:
        raise DeliveryError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def git_result(repository: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repository), *args], text=True,
                          stdin=subprocess.DEVNULL, capture_output=True)


def evidence(path: Path) -> dict[str, str]:
    return {"path": str(path.resolve()),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def atomic_json(path: Path, value: dict[str, Any]) -> dict[str, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
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
    return evidence(path)


def roots(controller: Path) -> tuple[dict[str, Any], Path, str]:
    config = task_runtime.load_config(controller)
    repository = task_runtime.product_repository(controller, config)
    target_ref = config["target_ref"]
    return config, repository, target_ref


def task_record(controller: Path, task_id: str) -> dict[str, Any]:
    if not TASK_RE.fullmatch(task_id):
        raise DeliveryError("unsafe task id")
    record = task_runtime.read_state(controller)["tasks"].get(task_id)
    if not isinstance(record, dict):
        raise DeliveryError(f"task {task_id} has no workspace record")
    return record


def ref_sha(repository: Path, ref: str) -> str:
    value = git(repository, "rev-parse", "--verify", ref)
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise DeliveryError(f"invalid ref identity for {ref}")
    return value


def is_ancestor(repository: Path, older: str, newer: str) -> bool:
    return git_result(repository, "merge-base", "--is-ancestor", older, newer).returncode == 0


def target_holders(repository: Path, target_ref: str) -> list[str]:
    rows = git(repository, "worktree", "list", "--porcelain").splitlines()
    holders, current = [], None
    for row in [*rows, ""]:
        if row.startswith("worktree "):
            current = row.removeprefix("worktree ")
        elif row == f"branch {target_ref}" and current:
            holders.append(current)
    return holders


def operation_root(controller: Path, task_id: str, target: str, source: str) -> Path:
    return (controller / ".juno_task/runtime/native-merge" / task_id
            / f"{target[:12]}-{source[:12]}")


def persist_task(controller: Path, task_id: str, expected: dict[str, Any],
                 updated: dict[str, Any]) -> None:
    with task_runtime.state_lock(controller):
        state = task_runtime.read_state(controller)
        if state["tasks"].get(task_id) != expected:
            raise DeliveryError("task record changed; result was not overwritten")
        if (state.get("schema_version") == task_runtime.BOUNDED_STATE_SCHEMA
                and updated.get("state") in task_runtime.TERMINAL_LIFECYCLE_STATES):
            updated = task_runtime.archive_terminal_transition(
                controller, state, task_id, updated)
        state["tasks"][task_id] = updated
        task_runtime.write_state(controller, state)


def cleanup_policy(config: dict[str, Any]) -> bool:
    value = os.environ.get("YYLO_WORKTREE_CLEANUP_AFTER_MERGE")
    if value is not None:
        if value not in {"true", "false"}:
            raise DeliveryError("YYLO_WORKTREE_CLEANUP_AFTER_MERGE must be true or false")
        return value == "true"
    return config.get("cleanup", {}).get("after_merge", False)


def full_task_record(controller: Path, task_id: str, record: dict[str, Any]) -> dict[str, Any]:
    if record.get("schema_version") == "juno_task_terminal_tombstone.v1":
        return task_runtime.state_archive_get(
            controller, task_id, record["archive"]["cold_ref"], tombstone=record)["record"]
    return record


def cleanup_worktree(repository: Path, path: Path, target: str,
                     disposable: list[str], apply: bool) -> dict[str, Any]:
    row: dict[str, Any] = {"path": str(path), "status": "skipped"}
    def skip(reason: str) -> dict[str, Any]:
        return {**row, "reason": reason}
    # Lexical and physical paths must agree, including parent components.
    if not path.is_absolute() or path.resolve() != path or path.is_symlink():
        return skip("path_identity_changed")
    registrations = git(repository, "worktree", "list", "--porcelain", "-z").split("\0\0")
    entries = [entry.split("\0") for entry in registrations]
    selected = next((entry for entry in entries if f"worktree {path}" in entry), None)
    if selected is None:
        return skip("unregistered_path" if path.exists() else "already_removed")
    if any(field.startswith("locked") or field.startswith("prunable") for field in selected):
        return skip("locked_or_prunable")
    if f"branch {target}" in selected:
        return skip("protected_target_worktree")
    if git(path, "config", "--worktree", "--get", "juno.workspace.role", check=False) in {"controller", "integration-owner"}:
        return skip("protected_workspace_role")
    if not (path / ".git").is_file():
        return skip("not_linked_worktree")
    if Path(git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")) != Path(
            git(repository, "rev-parse", "--path-format=absolute", "--git-common-dir")):
        return skip("repository_identity_changed")
    # Initialized submodules need a separate reviewed retention policy.
    if any(line and line[0] != "-" for line in git(path, "submodule", "status").splitlines()):
        return skip("initialized_submodules")
    for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG", "rebase-merge", "rebase-apply", "sequencer"):
        if Path(git(path, "rev-parse", "--path-format=absolute", "--git-path", marker)).exists():
            return skip("git_operation_in_progress")
    head = ref_sha(path, "HEAD")
    if not is_ancestor(repository, head, target):
        return skip("current_head_not_in_target")
    if git(path, "status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none"):
        return skip("dirty_worktree")
    ignored = git(path, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z")
    for item in filter(None, ignored.split("\0")):
        relative = item.rstrip("/")
        if not any(relative == allowed or relative.startswith(allowed + "/") for allowed in disposable):
            return skip("unknown_ignored_content")
        # Do not let a declared disposable directory follow a substituted link.
        current = path
        for part in Path(relative).parts:
            current /= part
            if current.is_symlink():
                return skip("ignored_path_symlink")
    if not apply:
        return {**row, "status": "eligible", "head": head}
    if ref_sha(path, "HEAD") != head or not is_ancestor(repository, head, ref_sha(repository, target)):
        return skip("identity_changed_before_removal")
    removed = git_result(repository, "worktree", "remove", str(path))
    return {**row, "status": "removed" if removed.returncode == 0 else "failed",
            "reason": None if removed.returncode == 0 else "git_worktree_remove_refused"}


def cleanup(controller: Path, task_id: str, *, apply: bool = False) -> dict[str, Any]:
    config, repository, target_ref = roots(controller)
    cleanup_policy(config)  # Validate overrides even for explicit cleanup.
    results: list[dict[str, Any]] = []
    with task_runtime.state_lock(controller):
        record = full_task_record(controller, task_id, task_record(controller, task_id))
        source, integrated = record.get("tip_sha"), record.get("integrated_sha")
        target = ref_sha(repository, target_ref)
        if (record.get("state") != "MERGED" or not isinstance(source, str)
                or not isinstance(integrated, str) or not is_ancestor(repository, source, integrated)
                or not is_ancestor(repository, integrated, target)):
            raise DeliveryError("cleanup requires completed delivery and current target ancestry")
        if record.get("fencing", {}).get("state") != "RELEASED":
            raise DeliveryError("cleanup requires a released task lease; unknown ownership is preserved")
        worktree = Path(record.get("worktree", ""))
        if worktree.exists():
            task_runtime.observe_task_identity(record, repository, config, task_id, expected_state="MERGED")
        elif worktree != task_runtime.worktree_path(config, task_id):
            raise DeliveryError("recorded task path differs from configured task path")
        protected = {controller.resolve(), repository.resolve(), Path.cwd().resolve()}
        paths = [(worktree, config.get("cleanup", {}).get("disposable_paths", []))]
        # Only exact successful native composition receipts authorize candidates.
        candidate_root = controller / ".juno_task/runtime/native-merge" / task_id
        receipts = sorted(candidate_root.glob("*/result.json"))
        if len(receipts) > 100:
            raise DeliveryError("too many candidate receipts; explicit review required")
        for receipt in receipts:
            if receipt.resolve() != receipt:
                continue
            data = json.loads(receipt.read_text())
            expected = data.get("expected_target", "")
            if (data.get("task_id") == task_id and data.get("outcome") == "GIT_INTEGRATED"
                    and data.get("source_sha") == source and data.get("target_ref") == target_ref
                    and re.fullmatch(r"[0-9a-f]{40}", expected)
                    and receipt.parent == operation_root(controller, task_id, expected, source)):
                candidate = receipt.parent / "candidate"
                if candidate.exists() and ref_sha(candidate, "HEAD") != data.get("candidate_sha"):
                    results.append({"path": str(candidate), "status": "skipped", "reason": "candidate_head_changed"})
                else:
                    paths.append((candidate, []))
        for path, disposable in paths:
            if any(path == item or path in item.parents for item in protected):
                results.append({"path": str(path), "status": "skipped", "reason": "protected_worktree"})
            else:
                results.append(cleanup_worktree(repository, path, target_ref, disposable, apply))
    result = {"schema_version": "juno_worktree_cleanup.v1", "task_id": task_id,
              "operation": "apply" if apply else "preview", "worktrees": results}
    if apply:
        directory = controller / ".juno_task/runtime/worktree-cleanup" / task_id
        directory.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="cleanup-", suffix=".json", dir=directory)
        os.close(fd)
        result["receipt"] = atomic_json(Path(name), result)
    return result


def cleanup_failure_receipt(controller: Path, task_id: str, result: dict[str, Any]) -> dict[str, Any]:
    directory = controller / ".juno_task/runtime/worktree-cleanup" / task_id
    directory.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="cleanup-", suffix=".json", dir=directory)
    os.close(fd)
    result["receipt"] = atomic_json(Path(name), result)
    return result


def automatic_cleanup(controller: Path, task_id: str) -> dict[str, Any]:
    try:
        config, _repository, _target = roots(controller)
        if not cleanup_policy(config):
            return {"status": "disabled"}
        return cleanup(controller, task_id, apply=True)
    except Exception as exc:
        # Optional cleanup must also contain malformed metadata and unexpected
        # runtime failures. Delivery is already complete; never fail the merge.
        result = {"status": "failed", "reason": str(exc)[:512], "task_id": task_id,
                  "schema_version": "juno_worktree_cleanup.v1", "operation": "apply"}
        try:
            return cleanup_failure_receipt(controller, task_id, result)
        except OSError:
            return {**result, "receipt_error": "cleanup receipt could not be written"}


def integrated_result(controller: Path, task_id: str, record: dict[str, Any],
                      target_ref: str, integrated_sha: str, source: str,
                      outcome: str, receipt: Optional[dict[str, str]] = None) -> dict[str, Any]:
    updated = {**record, "state": "GIT_INTEGRATED", "integrated_sha": integrated_sha,
               "git_delivery": {"schema_version": SCHEMA, "outcome": outcome,
                                "source_sha": source, "target_ref": target_ref,
                                "integrated_sha": integrated_sha}}
    persist_task(controller, task_id, record, updated)
    try:
        transition = task_runtime.project_kanban_lifecycle(
            controller, task_id, "GIT_INTEGRATED", phase="git-integrated",
            record=updated)
    except task_runtime.KanbanSyncError as exc:
        raise DeliveryError(
            f"Git is integrated but its Ledger transition failed: {exc}; "
            f"retry `yy merge project {task_id}`") from exc
    persisted = {**updated, "kanban_sync": transition}
    persist_task(controller, task_id, updated, persisted)
    projected = project(controller, task_id)
    return {"schema_version": SCHEMA, "task_id": task_id, "outcome": outcome,
            "git": {"status": "integrated", "source_sha": source,
                    "target_ref": target_ref, "integrated_sha": integrated_sha},
            "ledger": projected["ledger"], "projection": projected,
            "cleanup": projected["cleanup"],
            "receipt": receipt}


def land(controller: Path, task_id: str, *, candidate_sha: Optional[str] = None,
         expected_target: Optional[str] = None) -> dict[str, Any]:
    _config, repository, target_ref = roots(controller)
    cleanup_policy(_config)
    record = task_record(controller, task_id)
    if record.get("state") == "MERGED":
        return {"schema_version": SCHEMA, "task_id": task_id, "outcome": "ALREADY_PROJECTED",
                "cleanup": automatic_cleanup(controller, task_id)}
    if record.get("state") == "GIT_INTEGRATED":
        integrated = record.get("integrated_sha")
        if isinstance(integrated, str) and is_ancestor(repository, integrated, ref_sha(repository, target_ref)):
            return integrated_result(controller, task_id, record, target_ref, integrated,
                                     record["tip_sha"], "ALREADY_IN_TARGET")
    if record.get("state") not in {"QUEUED", "CONFLICT"}:
        raise DeliveryError(f"task {task_id} is {record.get('state')}, expected QUEUED or CONFLICT")
    source = record.get("tip_sha")
    if not isinstance(source, str) or ref_sha(repository, source) != source:
        raise DeliveryError("task source commit is missing or malformed")
    holders = target_holders(repository, target_ref)
    if holders:
        raise DeliveryError(f"target ref is checked out; detach it before landing: {holders}")
    observed = ref_sha(repository, target_ref)
    if is_ancestor(repository, source, observed):
        return integrated_result(controller, task_id, record, target_ref, observed,
                                 source, "ALREADY_IN_TARGET")

    root = operation_root(controller, task_id, expected_target or observed, source)
    receipt_path = root / "result.json"
    if candidate_sha is not None:
        if expected_target is None:
            raise DeliveryError("--candidate requires --expected-target")
        if observed != expected_target:
            raise DeliveryError("TARGET_MOVED: recompose and rerun project checks")
        if not is_ancestor(repository, expected_target, candidate_sha):
            raise DeliveryError("resolved candidate does not descend from expected target")
        if not is_ancestor(repository, source, candidate_sha):
            raise DeliveryError("resolved candidate does not contain the task source")
        candidate = candidate_sha
    else:
        root.mkdir(parents=True, exist_ok=True)
        candidate_path = root / "candidate"
        if candidate_path.exists():
            raise DeliveryError(f"candidate already exists; inspect preserved state at {candidate_path}")
        git(repository, "worktree", "add", "--detach", str(candidate_path), observed)
        merge = git_result(candidate_path, "-c", "user.name=YYLO native delivery",
                           "-c", "user.email=delivery@invalid", "merge", "--no-edit", source)
        if merge.returncode:
            conflict = {"schema_version": SCHEMA, "task_id": task_id, "outcome": "CONFLICT",
                        "source_sha": source, "target_ref": target_ref,
                        "expected_target": observed, "candidate_path": str(candidate_path),
                        "unmerged_paths": git(candidate_path, "diff", "--name-only", "--diff-filter=U").splitlines(),
                        "continue_command": (f"yy merge land {task_id} --candidate <resolved-sha> "
                                             f"--expected-target {observed}")}
            receipt = atomic_json(receipt_path, conflict)
            conflicted = {**record, "state": "CONFLICT", "git_delivery": conflict,
                          "git_delivery_receipt": receipt}
            persist_task(controller, task_id, record, conflicted)
            try:
                transition = task_runtime.project_kanban_lifecycle(
                    controller, task_id, "CONFLICT", phase="conflict",
                    record=conflicted)
            except task_runtime.KanbanSyncError as exc:
                raise DeliveryError(
                    f"conflict is preserved but its Ledger transition failed: {exc}; "
                    f"retry `yy task sync {task_id}`") from exc
            persist_task(controller, task_id, conflicted,
                         {**conflicted, "kanban_sync": transition})
            return {**conflict, "ledger": {"status": "complete",
                                            "board_status": transition["board_status"]},
                    "receipt": receipt}
        candidate = ref_sha(candidate_path, "HEAD")

    update = git_result(repository, "update-ref", target_ref, candidate, observed)
    if update.returncode:
        moved = {"schema_version": SCHEMA, "task_id": task_id, "outcome": "TARGET_MOVED",
                 "source_sha": source, "candidate_sha": candidate,
                 "expected_target": observed, "observed_target": ref_sha(repository, target_ref),
                 "retry": "recompose and rerun exact-candidate project checks"}
        receipt = atomic_json(receipt_path, moved)
        return {**moved, "receipt": receipt}
    landed = {"schema_version": SCHEMA, "task_id": task_id, "outcome": "GIT_INTEGRATED",
              "source_sha": source, "candidate_sha": candidate,
              "target_ref": target_ref, "expected_target": observed}
    receipt = atomic_json(receipt_path, landed)
    return integrated_result(controller, task_id, record, target_ref, candidate,
                             source, "GIT_INTEGRATED", receipt)


def project(controller: Path, task_id: str) -> dict[str, Any]:
    _config, repository, target_ref = roots(controller)
    cleanup_policy(_config)
    hot_record = task_record(controller, task_id)
    record = full_task_record(controller, task_id, hot_record)
    source = record.get("tip_sha")
    integrated = record.get("integrated_sha")
    target = ref_sha(repository, target_ref)
    if (record.get("state") not in {"GIT_INTEGRATED", "MERGED"}
            or not isinstance(source, str)
            or not isinstance(integrated, str)
            or not re.fullmatch(r"[0-9a-f]{40}", integrated)
            or not is_ancestor(repository, source, integrated)
            or not is_ancestor(repository, integrated, target)):
        raise DeliveryError("Git integration is not proven; run `yy merge land TASK_ID` first")
    try:
        projection = task_runtime.project_kanban_lifecycle(
            controller, task_id, "MERGED", phase="native-project", record=record,
            allow_done=True, commit_hash=integrated,
            response=f"Merged into the protected target as {integrated}.")
    except task_runtime.KanbanSyncError as exc:
        raise DeliveryError(f"Ledger projection failed; Git remains integrated: {exc}") from exc
    updated = {**record, "state": "MERGED", "integrated_sha": integrated,
               "kanban_sync": projection,
               "git_delivery": {**record.get("git_delivery", {}), "projection": "complete"}}
    if updated != record:
        persist_task(controller, task_id, hot_record, updated)
    return {"schema_version": SCHEMA, "task_id": task_id,
            "cleanup": automatic_cleanup(controller, task_id),
            "outcome": ("ALREADY_PROJECTED" if projection.get("outcome") == "verified"
                        and record.get("state") == "MERGED" else "PROJECTED"),
            "git": {"status": "integrated", "integrated_sha": integrated},
            "ledger": {"status": "complete", "board_status": "done",
                       "receipt": projection.get("receipt")}}


def status(controller: Path, task_id: Optional[str] = None) -> dict[str, Any]:
    _config, repository, target_ref = roots(controller)
    state = task_runtime.read_state(controller)
    selected = ([task_id] if task_id else sorted(
        key for key, value in state["tasks"].items()
        if isinstance(value, dict) and value.get("state") in ACTIVE_STATES))
    tasks = []
    for current in selected:
        record = task_record(controller, current)
        tasks.append({"task_id": current, "state": record.get("state"),
                      "source_sha": record.get("tip_sha"),
                      "integrated_sha": record.get("integrated_sha"),
                      "next_command": (f"yy merge project {current}" if record.get("state") == "GIT_INTEGRATED"
                                       else f"yy merge land {current}")})
    return {"schema_version": SCHEMA, "operation": "status", "target_ref": target_ref,
            "target_sha": ref_sha(repository, target_ref), "tasks": tasks,
            "fifo": None, "model_calls": 0}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="one-task native Git delivery")
    result.add_argument("--controller", type=Path, default=Path.cwd())
    commands = result.add_subparsers(dest="operation", required=True)
    observe = commands.add_parser("status")
    observe.add_argument("task", nargs="?")
    deliver = commands.add_parser("land")
    deliver.add_argument("task")
    deliver.add_argument("--candidate")
    deliver.add_argument("--expected-target")
    projection = commands.add_parser("project")
    projection.add_argument("task")
    clean = commands.add_parser("cleanup")
    clean.add_argument("task")
    mode = clean.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    return result


def main(argv: Optional[list[str]] = None) -> int:
    args = parser().parse_args(argv)
    controller = args.controller.resolve()
    try:
        if args.operation == "status":
            result = status(controller, args.task)
        elif args.operation == "land":
            result = land(controller, args.task, candidate_sha=args.candidate,
                          expected_target=args.expected_target)
        elif args.operation == "cleanup":
            result = cleanup(controller, args.task, apply=args.apply)
        else:
            result = project(controller, args.task)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (DeliveryError, task_runtime.TaskWorkspaceError,
            task_runtime.KanbanSyncError) as exc:
        print(f"native Git delivery: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
