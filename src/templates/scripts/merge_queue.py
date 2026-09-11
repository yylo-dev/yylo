#!/usr/bin/env python3
"""One-task native Git delivery adapter.

Git owns composition, conflicts, ancestry, and expected-old ref updates. Ledger
projection is a separate idempotent command and never gates another task's land.
Historical queue receipts remain inert data; this module does not interpret them.
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
        state["tasks"][task_id] = updated
        task_runtime.write_state(controller, state)


def integrated_result(controller: Path, task_id: str, record: dict[str, Any],
                      target_ref: str, integrated_sha: str, source: str,
                      outcome: str, receipt: Optional[dict[str, str]] = None) -> dict[str, Any]:
    updated = {**record, "state": "GIT_INTEGRATED", "integrated_sha": integrated_sha,
               "git_delivery": {"schema_version": SCHEMA, "outcome": outcome,
                                "source_sha": source, "target_ref": target_ref,
                                "integrated_sha": integrated_sha}}
    persist_task(controller, task_id, record, updated)
    return {"schema_version": SCHEMA, "task_id": task_id, "outcome": outcome,
            "git": {"status": "integrated", "source_sha": source,
                    "target_ref": target_ref, "integrated_sha": integrated_sha},
            "ledger": {"status": "pending", "next_command": f"yy merge project {task_id}"},
            "receipt": receipt}


def land(controller: Path, task_id: str, *, candidate_sha: Optional[str] = None,
         expected_target: Optional[str] = None) -> dict[str, Any]:
    _config, repository, target_ref = roots(controller)
    record = task_record(controller, task_id)
    if record.get("state") == "MERGED":
        return {"schema_version": SCHEMA, "task_id": task_id, "outcome": "ALREADY_PROJECTED"}
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
            persist_task(controller, task_id, record,
                         {**record, "state": "CONFLICT", "git_delivery": conflict,
                          "git_delivery_receipt": receipt})
            return {**conflict, "receipt": receipt}
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
    record = task_record(controller, task_id)
    if record.get("state") == "MERGED":
        return {"schema_version": SCHEMA, "task_id": task_id, "outcome": "ALREADY_PROJECTED"}
    source = record.get("tip_sha")
    target = ref_sha(repository, target_ref)
    if record.get("state") != "GIT_INTEGRATED" or not isinstance(source, str) or not is_ancestor(repository, source, target):
        raise DeliveryError("Git integration is not proven; run `yy merge land TASK_ID` first")
    board = task_runtime.read_kanban_task(controller, task_id)
    if board.get("status") == "done":
        if board.get("commit_hash") != target:
            raise DeliveryError("task is done with a different integration commit")
    else:
        revision = task_runtime.kanban_board_revision(controller, task_id)
        finalization = controller / ".juno_task/runtime/native-merge" / task_id / f"projection-{target}.json"
        finalization.parent.mkdir(parents=True, exist_ok=True)
        fd, response_path = tempfile.mkstemp(prefix=".response-", dir=finalization.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(f"Merged into the protected target as {target}.")
            result = subprocess.run([
                str(controller / ".juno_task/scripts/kanban.sh"), "-f", "json", "update", task_id,
                "--status", "done", "--response-file", response_path, "--commit", target,
                "--field", f"lifecycle_projection={json.dumps(task_runtime.KANBAN_LIFECYCLE_PROJECTION)}",
                "--field", "lifecycle_state=\"MERGED\"", "--expected-revision", revision,
                "--receipt-file", str(finalization),
            ], cwd=controller, text=True, stdin=subprocess.DEVNULL, capture_output=True)
        finally:
            Path(response_path).unlink(missing_ok=True)
        if result.returncode:
            raise DeliveryError(result.stderr.strip() or "Ledger projection failed; Git remains integrated")
    updated = {**record, "state": "MERGED", "integrated_sha": target,
               "git_delivery": {**record.get("git_delivery", {}), "projection": "complete"}}
    persist_task(controller, task_id, record, updated)
    return {"schema_version": SCHEMA, "task_id": task_id, "outcome": "PROJECTED",
            "git": {"status": "integrated", "integrated_sha": target},
            "ledger": {"status": "complete"}}


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
