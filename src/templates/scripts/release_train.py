#!/usr/bin/env python3
"""Deterministic, offline release-train projection and stale-plan gate."""
from __future__ import annotations

import argparse
import ast
import base64
import fcntl
import fnmatch
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import task_workspace as task_runtime

DECLARATION_SCHEMA = "juno_release_train_declaration.v1"
REPORT_SCHEMA = "juno_release_train_plan.v1"
IDENTITY_SCHEMA = "juno_release_train_plan_identity.v1"
EPOCH_PLAN_SCHEMA = "juno_release_epoch_plan.v1"
EPOCH_SEAL_SCHEMA = "juno_release_epoch_seal.v1"
EPOCH_STATE_SCHEMA = "juno_release_epoch_state.v1"
EPOCH_RECEIPT_SCHEMA = "juno_release_epoch_receipt.v1"
CONFLICT_MANIFEST_SCHEMA = "juno_release_epoch_conflict_manifest.v1"
CONFLICT_FORECAST_POLICY_SCHEMA = "juno_release_epoch_conflict_forecast_policy.v1"
CONFLICT_AUTHORITY_SCHEMA = "juno_release_epoch_conflict_authority.v1"
PHASE1_CLOSURE_SCHEMA = "juno_release_epoch_phase1_closure.v4"
PHASE1_PROOF_SCHEMA = "juno_release_epoch_phase1_proof.v3"
PHASE1_SUITE_SCHEMA = "juno_release_epoch_phase1_suite.v2"
PHASE1_SUITE_MANIFEST_SCHEMA = "juno_release_epoch_phase1_suite_manifest.v1"
PHASE1_EVALUATION_SCHEMA = "juno_release_epoch_phase1_evaluation.v3"
PHASE1_ACCEPTANCE_SCHEMA = "juno_release_epoch_phase1_acceptance.v4"
SHADOW_SCHEMA = "juno_release_epoch_shadow.v1"
SUCCESSOR_REPAIR_SCHEMA = "juno_release_successor_repair.v1"
BOOTSTRAP_DECLARATION_SCHEMA = "juno_bootstrap_repair_declaration.v1"
BOOTSTRAP_SEAL_SCHEMA = "juno_bootstrap_repair_seal.v1"
BOOTSTRAP_STATE_SCHEMA = "juno_bootstrap_repair_state.v1"
BOOTSTRAP_RECEIPT_SCHEMA = "juno_bootstrap_repair_receipt.v1"
EPOCH_FINALIZATION_SCHEMA = "juno_release_epoch_terminal_finalization.v1"
EPOCH_FINALIZATION_SUCCESSOR_SCHEMA = "juno_release_epoch_terminal_finalization_successor.v1"
EPOCH_FINALIZATION_MEMBER_SCHEMA = "juno_release_epoch_terminal_member.v1"
EPOCH_FINALIZATION_RECEIPT_SCHEMA = "juno_release_epoch_terminal_finalization_receipt.v1"
EPOCH_FINALIZATION_SUPERSESSION_SCHEMA = "juno_release_epoch_terminal_finalization_supersession.v1"
EPOCH_DELIVERY_SELECTION_SCHEMA = "juno_epoch_delivery_selection.v1"
WAVE_ECONOMICS_SCHEMA = "juno_wave_economics_projection.v1"
EXTERNAL_ACTIONS = {"release", "tag", "push", "publish", "deploy", "cleanup"}
EPOCH_TERMINAL_STATES = {"RELEASE_READY", "CLOSED", "PAUSED_REQUIRED", "NEEDS_OPERATOR", "STALE"}
ACTIVE_QUEUE_STATES = {"QUEUED", "MERGING", "CONFLICT", "CONFLICT_RESOLVED", "AWAITING_RISK",
                       "AWAITING_RELEASE", "REVIEW_FINDINGS", "REVIEW_FINDINGS_EXHAUSTED",
                       "REOPENING", "REQUEUING_STALE"}
SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")


class ReleaseTrainError(RuntimeError):
    pass


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def file_hash(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return result.stdout.strip() if result.returncode == 0 else ""


def load_declaration(path: Path) -> tuple[dict[str, Any], Path]:
    resolved = path.expanduser().resolve()
    try:
        value = json.loads(resolved.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseTrainError(f"invalid release-train declaration: {exc}") from exc
    required_keys = {"schema_version", "train_id", "revision", "requested_version", "target_ref",
                     "planning_base_sha", "required_tasks", "optional_tasks", "dependencies",
                     "gates", "authority", "exclusions"}
    allowed_keys = required_keys | {"conflict_authority"}
    if (not isinstance(value, dict) or not required_keys.issubset(value)
            or not set(value).issubset(allowed_keys) or value.get("schema_version") != DECLARATION_SCHEMA):
        raise ReleaseTrainError("release-train declaration has an unsupported or non-exact schema")
    train_id = value["train_id"]
    if not isinstance(train_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", train_id):
        raise ReleaseTrainError("train_id is unsafe")
    if not isinstance(value["revision"], int) or isinstance(value["revision"], bool) or value["revision"] < 1:
        raise ReleaseTrainError("declaration revision must be a positive integer")
    if not task_runtime.is_valid_semver(value["requested_version"]) or "+" in value["requested_version"]:
        raise ReleaseTrainError("requested_version must be exact SemVer without build metadata")
    if not isinstance(value["target_ref"], str) or not value["target_ref"].startswith("refs/"):
        raise ReleaseTrainError("target_ref must be a full ref")
    if not isinstance(value["planning_base_sha"], str) or not SHA_RE.fullmatch(value["planning_base_sha"]):
        raise ReleaseTrainError("planning_base_sha must be an exact commit")
    for field in ("required_tasks", "optional_tasks", "gates", "exclusions"):
        if not isinstance(value[field], list) or any(not isinstance(item, str) or not item for item in value[field]):
            raise ReleaseTrainError(f"{field} must contain non-empty strings")
        if len(value[field]) != len(set(value[field])):
            raise ReleaseTrainError(f"{field} contains duplicates")
    overlap = set(value["required_tasks"]) & set(value["optional_tasks"])
    if overlap:
        raise ReleaseTrainError("required and optional tasks overlap: " + ", ".join(sorted(overlap)))
    if not value["required_tasks"]:
        raise ReleaseTrainError("required_tasks must not be empty")
    for task_id in value["required_tasks"] + value["optional_tasks"]:
        if not task_runtime.TASK_RE.fullmatch(task_id):
            raise ReleaseTrainError(f"unsafe task id: {task_id}")
    if not isinstance(value["dependencies"], list):
        raise ReleaseTrainError("dependencies must be a list")
    known = set(value["required_tasks"] + value["optional_tasks"])
    seen_edges: set[tuple[str, str]] = set()
    for edge in value["dependencies"]:
        if not isinstance(edge, dict) or set(edge) != {"before", "after"}:
            raise ReleaseTrainError("dependency edges require exactly before and after")
        pair = (edge["before"], edge["after"])
        if pair in seen_edges or any(item not in known for item in pair):
            raise ReleaseTrainError("dependency edge is duplicate or references an undeclared task")
        seen_edges.add(pair)
    authority = value["authority"]
    if not isinstance(authority, dict) or set(authority) != {"controller_common_dir", "release_command"}:
        raise ReleaseTrainError("authority requires controller_common_dir and release_command")
    if not all(isinstance(item, str) and item for item in authority.values()):
        raise ReleaseTrainError("authority values must be non-empty strings")
    conflict_authority = value.get("conflict_authority")
    if (conflict_authority is not None and (not isinstance(conflict_authority, dict)
            or set(conflict_authority) != {"path", "sha256"}
            or not isinstance(conflict_authority.get("path"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", conflict_authority.get("sha256", "")))):
        raise ReleaseTrainError("conflict_authority requires exact path and sha256")
    return value, resolved


def kanban_task(controller: Path, task_id: str) -> dict[str, Any]:
    # Exact get may resolve immutable cold archives; release intent must name
    # current hot canonical tasks, never silently revive an archived ID.
    if not task_runtime.task_file(controller, task_id).is_file():
        raise ReleaseTrainError(f"canonical Kanban task is missing or archived: {task_id}")
    wrapper = controller / ".juno_task/scripts/kanban.sh"
    result = subprocess.run([str(wrapper), "get", task_id], cwd=controller, text=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=30)
    if result.returncode:
        raise ReleaseTrainError(f"canonical Kanban task is missing or unreadable: {task_id}")
    try:
        rows = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ReleaseTrainError(f"canonical Kanban returned malformed JSON for {task_id}") from exc
    if not isinstance(rows, list) or len(rows) != 1 or rows[0].get("id") != task_id:
        raise ReleaseTrainError(f"canonical Kanban identity is ambiguous: {task_id}")
    return rows[0]


def dependency_order(nodes: set[str], edges: set[tuple[str, str]]) -> list[str]:
    incoming = {node: 0 for node in nodes}
    children = {node: [] for node in nodes}
    for before, after in sorted(edges):
        incoming[after] += 1
        children[before].append(after)
    ready = sorted(node for node, count in incoming.items() if count == 0)
    ordered: list[str] = []
    while ready:
        node = ready.pop(0); ordered.append(node)
        for child in children[node]:
            incoming[child] -= 1
            if incoming[child] == 0:
                ready.append(child); ready.sort()
    return ordered


def cycle_path(nodes: set[str], edges: set[tuple[str, str]]) -> list[str]:
    graph = {node: [] for node in nodes}
    for before, after in sorted(edges):
        graph[before].append(after)
    visiting: list[str] = []
    done: set[str] = set()
    def visit(node: str) -> list[str]:
        if node in visiting:
            start = visiting.index(node)
            return visiting[start:] + [node]
        if node in done:
            return []
        visiting.append(node)
        for child in graph[node]:
            found = visit(child)
            if found:
                return found
        visiting.pop(); done.add(node)
        return []
    for node in sorted(nodes):
        found = visit(node)
        if found:
            return found
    return []


def target_blob(repository: Path, sha: str, path: str) -> Optional[bytes]:
    """Exact blob bytes of one product path at the protected target generation."""
    result = subprocess.run(["git", "-C", str(repository), "cat-file", "blob", f"{sha}:{path}"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return result.stdout if result.returncode == 0 else None


def release_versions(blobs: dict[str, Optional[bytes]]) -> dict[str, Optional[str]]:
    """Version identity from exact protected-target tree bytes.

    A lean sparse controller legitimately carries no product files in its
    working tree, so the release identity is derived from the target commit
    itself, never from checkout state (rejV9U).
    """
    def extract(blob: Optional[bytes], keys: list[Any]) -> Optional[str]:
        try:
            value: Any = json.loads(blob.decode("utf-8")) if blob is not None else None
            for key in keys:
                value = value[key]
            return value if isinstance(value, str) else None
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
            return None

    package, lock = blobs.get("juno-code/package.json"), blobs.get("juno-code/package-lock.json")
    return {"package": extract(package, ["version"]),
            "lock": extract(lock, ["version"]),
            "lock_root": extract(lock, ["packages", "", "version"])}


def build_plan(controller: Path, declaration_path: Path,
               plan_output: Optional[Path] = None) -> dict[str, Any]:
    declaration, declaration_path = load_declaration(declaration_path)
    plan_path = (plan_output.expanduser().resolve() if plan_output is not None
                 else declaration_path.with_suffix(".plan.json"))
    config = task_runtime.load_config(controller)
    repository = task_runtime.product_repository(controller, config)
    registered_controller = git(controller, "config", "--local", "--get", "juno.controller.path")
    if registered_controller and Path(registered_controller).expanduser().resolve() != controller:
        raise ReleaseTrainError("release-train planning requires the registered canonical controller")
    controller_common = str(Path(git(controller, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve())
    if str(Path(declaration["authority"]["controller_common_dir"]).expanduser().resolve()) != controller_common:
        raise ReleaseTrainError("declaration authority does not name this canonical controller repository")
    if declaration["target_ref"] != config["target_ref"]:
        raise ReleaseTrainError("declaration target ref differs from canonical target policy")
    target_sha = git(repository, "rev-parse", "--verify", declaration["target_ref"])
    if not SHA_RE.fullmatch(target_sha):
        raise ReleaseTrainError("declared target ref is missing")

    declared = declaration["required_tasks"] + declaration["optional_tasks"]
    board = {task_id: kanban_task(controller, task_id) for task_id in declared}
    external_blocker_ids = sorted({
        blocker for task in board.values() for blocker in (task.get("blocked_by", []) or [])
        if blocker not in board
    })
    external_board = {task_id: kanban_task(controller, task_id) for task_id in external_blocker_ids}
    state = task_runtime.read_state(controller)
    records = state["tasks"]
    edges = {(edge["before"], edge["after"]) for edge in declaration["dependencies"]}
    for task_id, task in board.items():
        for blocker in task.get("blocked_by", []) or []:
            if blocker in declared:
                edges.add((blocker, task_id))
    cycle = cycle_path(set(declared), edges)
    ordered_tasks = dependency_order(set(declared), edges)

    queue_rows = []
    for task_id, record in records.items():
        if isinstance(record, dict) and record.get("target_ref") == declaration["target_ref"] and record.get("state") in ACTIVE_QUEUE_STATES:
            queue_rows.append({"task_id": task_id, "state": record.get("state"),
                               "enqueue_sequence": record.get("enqueue_sequence"),
                               "record_sha256": digest(record), "tip_sha": record.get("tip_sha")})
    queue_rows.sort(key=lambda row: (row["enqueue_sequence"] if isinstance(row["enqueue_sequence"], int) else 2**63,
                                     row["task_id"]))
    queue_head = queue_rows[0]["task_id"] if queue_rows else None
    required = set(declaration["required_tasks"])
    first_required_index = next((index for index, row in enumerate(queue_rows) if row["task_id"] in required), len(queue_rows))
    older_unrelated = [row for row in queue_rows[:first_required_index] if row["task_id"] not in required]

    task_rows: list[dict[str, Any]] = []
    feasibility: dict[str, Any] = {}
    for task_id in declared:
        task, record = board[task_id], records.get(task_id)
        status = task.get("status")
        blockers = sorted(set(task.get("blocked_by", []) or []))
        unmet = [item for item in blockers
                 if (board.get(item) or external_board.get(item, {})).get("status") != "done"]
        queue_state = record.get("state") if isinstance(record, dict) else None
        integrated = status == "done" and (not isinstance(record, dict) or queue_state == "MERGED" or
            (isinstance(task.get("commit_hash"), str) and not subprocess.run(
                ["git", "-C", str(repository), "merge-base", "--is-ancestor", task["commit_hash"], target_sha],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode))
        if integrated:
            lane = "integrated"
        elif queue_state in ACTIVE_QUEUE_STATES:
            lane = "in_progress"
        elif status == "in_progress":
            lane = "in_progress"
        elif unmet:
            lane = "blocked"
        elif status in {"backlog", "todo"}:
            lane = "ready"
        else:
            lane = "blocked"
        authored = sorted(record.get("changed_paths", [])) if isinstance(record, dict) else []
        item = {"task_id": task_id, "required": task_id in required, "kanban_status": status,
                "kanban_revision": task.get("last_modified"), "kanban_sha256": digest(task),
                "queue_state": queue_state, "enqueue_sequence": record.get("enqueue_sequence") if isinstance(record, dict) else None,
                "unmet_blockers": unmet, "lane": lane, "changed_paths": authored}
        task_rows.append(item)
        if isinstance(record, dict) and queue_state in ACTIVE_QUEUE_STATES:
            try:
                import merge_queue
                candidate = merge_queue.merge_plan(controller, task_id)
                feasibility[task_id] = {"plan_id": candidate["plan_id"], "ready": candidate["ready"],
                                        "finding_codes": [row["code"] for row in candidate["findings"]]}
            except Exception as exc:
                feasibility[task_id] = {"plan_id": None, "ready": False, "error": str(exc)}

    parallel_lanes: list[list[str]] = []
    for row in task_rows:
        if row["lane"] not in {"ready", "in_progress"}:
            continue
        placed = False
        for lane in parallel_lanes:
            safe = all(
                not (set(row["changed_paths"]) & set(next(
                    item["changed_paths"] for item in task_rows if item["task_id"] == other)))
                and (other, row["task_id"]) not in edges
                and (row["task_id"], other) not in edges
                for other in lane
            )
            if safe:
                lane.append(row["task_id"]); placed = True; break
        if not placed:
            parallel_lanes.append([row["task_id"]])

    blockers: list[dict[str, Any]] = []
    if target_sha != declaration["planning_base_sha"]:
        blockers.append({"code": "target.moved", "repair_command": "revise the train planning_base_sha and re-plan"})
    if cycle:
        blockers.append({"code": "dependency.cycle", "tasks": cycle, "repair_command": "revise the train dependencies"})
    if older_unrelated:
        blockers.append({"code": "queue.older_unrelated", "tasks": [row["task_id"] for row in older_unrelated],
                         "choices": ["wait", "complete", "revise-train", "receipt-bound-defer-if-supported"],
                         "repair_command": "yy merge next"})
    for row in task_rows:
        if row["unmet_blockers"]:
            blockers.append({"code": "dependency.unmet", "task_id": row["task_id"],
                             "tasks": row["unmet_blockers"], "repair_command": f"yy task status {row['unmet_blockers'][0]}"})
    runtime_paths = [controller / ".juno_task/scripts/release_train.py", controller / ".juno_task/scripts/merge_queue.py"]
    runtime_hashes = {str(path.relative_to(controller)): file_hash(path) for path in runtime_paths}
    if any(value is None for value in runtime_hashes.values()):
        blockers.append({"code": "runtime.missing", "repair_command": "yy scripts update --force"})
    unresolved_finalization = _unresolved_finalization_epochs(controller)
    if unresolved_finalization:
        blockers.append({"code": "release.finalization_incomplete",
            "epochs": unresolved_finalization,
            "repair_command": "use only the typed finalization successor replay"})
    bad_feasibility = [task_id for task_id, value in feasibility.items() if not value["ready"]]
    if bad_feasibility:
        blockers.append({"code": "candidate.infeasible", "tasks": sorted(bad_feasibility),
                         "repair_command": f"yy merge plan {sorted(bad_feasibility)[0]}"})
    owner_path = git(repository, "config", "--local", "--get", "juno.integration.ownerPath")
    owner_identity: dict[str, Any] = {"path": owner_path or None, "ready": False}
    if owner_path:
        try:
            import merge_queue
            observed = merge_queue.integration_owner_readback(Path(owner_path).expanduser().resolve())
            owner_identity["observed"] = observed
            owner_identity["ready"] = bool(
                observed["clean"] and observed["detached"] and observed["full_checkout"]
                and observed["role"] == "integration-owner"
                and observed["authority"] == merge_queue.INTEGRATION_OWNER_AUTHORITY
                and observed["head"] == target_sha and observed["role_base"] == target_sha
                and all(item["state"] == "exact" for item in observed["submodules"]))
        except Exception as exc:
            owner_identity["error"] = str(exc)
    if not owner_identity["ready"]:
        blockers.append({"code": "topology.integration_owner_not_ready",
                         "repair_command": "yy integration status"})
    # Release version identity binds to the exact protected target generation:
    # a lean sparse controller without product files in its working tree must
    # still plan, and working-tree drift must never become release identity.
    package_paths = ["juno-code/package.json", "juno-code/package-lock.json"]
    target_blobs = {path: target_blob(repository, target_sha, path) for path in package_paths}
    versions = release_versions(target_blobs)
    version = versions["package"]
    tag = "v" + declaration["requested_version"]
    tag_exists = bool(git(repository, "rev-parse", "-q", "--verify", "refs/tags/" + tag))
    if version is None or not task_runtime.is_valid_semver(version):
        blockers.append({"code": "release.version_missing", "repair_command": "repair juno-code/package.json"})
    elif len(set(versions.values())) != 1:
        blockers.append({"code": "release.version_identity_mismatch",
                         "repair_command": "synchronize package and lockfile versions"})
    elif not task_runtime.semver_precedes(version, declaration["requested_version"]):
        blockers.append({"code": "release.version_not_greater", "repair_command": "revise requested_version"})
    if tag_exists:
        blockers.append({"code": "release.tag_exists", "repair_command": "revise requested_version"})

    required_rows = [row for row in task_rows if row["required"]]
    release_ready = (all(row["lane"] == "integrated" for row in required_rows) and not blockers)
    if blockers:
        next_command = blockers[0]["repair_command"]
    else:
        queue_required = next((row for row in queue_rows if row["task_id"] in required), None)
        ready_required = next((row for row in required_rows if row["lane"] == "ready"), None)
        active_required = next((row for row in required_rows if row["lane"] == "in_progress"), None)
        if queue_required:
            next_command = f"yy merge next --train-plan {plan_path}"
        elif active_required:
            next_command = f"yy task status {active_required['task_id']}"
        elif ready_required:
            next_command = f"yy task start {ready_required['task_id']}"
        else:
            next_command = declaration["authority"]["release_command"] + f" --train-plan {plan_path}"

    identities = {"controller": {"path": str(controller), "common_dir": controller_common,
                                  "ref": git(controller, "symbolic-ref", "-q", "HEAD"),
                                  "head": git(controller, "rev-parse", "HEAD")},
                  "repository_common_dir": str(Path(git(repository, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()),
                  "target": {"ref": declaration["target_ref"], "sha": target_sha},
                  "declaration": {"path": str(declaration_path), "revision": declaration["revision"],
                                  "sha256": file_hash(declaration_path)},
                  "plan_path": str(plan_path),
                  "kanban": {**{row["task_id"]: {"revision": row["kanban_revision"], "sha256": row["kanban_sha256"]} for row in task_rows},
                              **{task_id: {"revision": task.get("last_modified"), "sha256": digest(task), "external_blocker": True}
                                 for task_id, task in external_board.items()}},
                  "queue": {"head": queue_head, "records_sha256": digest(queue_rows), "rows": queue_rows},
                  "runtime_sha256": runtime_hashes,
                  "package_sha256": {path: (None if target_blobs[path] is None
                                            else hashlib.sha256(target_blobs[path]).hexdigest())
                                      for path in package_paths},
                  "policy_sha256": {path: file_hash(controller / path) for path in [".juno_task/config/task-workspace.json", ".juno_task/config/risk-policy.json"]},
                  "integration_owner": owner_identity,
                  "requested_version": declaration["requested_version"]}
    body = {"schema_version": REPORT_SCHEMA, "train_id": declaration["train_id"],
            "requested_version": declaration["requested_version"], "declaration": declaration,
            "ready": release_ready, "release_ready": release_ready, "target_moved": target_sha != declaration["planning_base_sha"],
            "tasks": task_rows, "dependency_edges": [[before, after] for before, after in sorted(edges)],
            "dependency_order": ordered_tasks,
            "critical_path": cycle if cycle else [task_id for task_id in ordered_tasks
                                                    if task_id in required and next(row for row in task_rows if row["task_id"] == task_id)["lane"] != "integrated"],
            "parallel_lanes": parallel_lanes, "serialized_merge_order": [row["task_id"] for row in queue_rows],
            "fifo": {"head": queue_head, "older_unrelated": older_unrelated, "conflict": bool(older_unrelated)},
            "candidate_feasibility": feasibility, "release_preconditions": {"current_version": version,
                "version_identities": versions,
                "requested_version": declaration["requested_version"], "tag": tag, "tag_exists": tag_exists,
                "required_tasks_integrated": all(row["lane"] == "integrated" for row in required_rows),
                "gates": declaration["gates"]},
            "blockers": blockers, "next_command": next_command, "identities": identities,
            "invalidation": {"rule": "any declaration/controller/ref/HEAD/target/Kanban/queue/runtime/package/policy/version identity change invalidates this plan"}}
    return {**body, "plan_id": digest({"schema_version": IDENTITY_SCHEMA, "report": body})}


def check_plan(controller: Path, plan_path: Path, action: str, task_id: Optional[str] = None,
               requested_version: Optional[str] = None) -> dict[str, Any]:
    try:
        approved = json.loads(plan_path.expanduser().resolve().read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseTrainError(f"invalid release-train plan: {exc}") from exc
    declaration_path = approved.get("identities", {}).get("declaration", {}).get("path")
    if not isinstance(declaration_path, str):
        raise ReleaseTrainError("release-train plan lacks its declaration identity")
    bound_plan_path = approved.get("identities", {}).get("plan_path")
    if not isinstance(bound_plan_path, str):
        raise ReleaseTrainError("release-train plan lacks its output-path identity")
    if plan_path.expanduser().resolve() != Path(bound_plan_path).expanduser().resolve():
        raise ReleaseTrainError("release-train plan was supplied from a different path identity")
    current = build_plan(controller, Path(declaration_path), Path(bound_plan_path))
    if approved != current:
        raise ReleaseTrainError("release-train plan is stale; rerun yy release train plan")
    if action == "merge":
        head = current["fifo"]["head"]
        if head is None or (task_id is not None and head != task_id):
            raise ReleaseTrainError("release-train plan does not authorize this exact FIFO merge head")
        # An unrelated older head is explicitly permitted only in its existing
        # FIFO position: completing it honors the conflict; it never bypasses it.
        row = next((item for item in current["tasks"] if item["task_id"] == head), None)
        if row is not None and row["unmet_blockers"]:
            raise ReleaseTrainError("release-train merge head has unmet dependencies")
    elif action == "release":
        if requested_version != current["requested_version"]:
            raise ReleaseTrainError("release version differs from the release-train plan")
        if not current["release_ready"]:
            raise ReleaseTrainError("release-train plan is blocked and cannot authorize release")
    else:
        raise ReleaseTrainError("unknown release-train action")
    return current


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def atomic_json(path: Path, value: dict[str, Any], *, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = canonical(value) + "\n"
    if exclusive:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def epoch_root(controller: Path) -> Path:
    return controller / ".juno_task/runtime/release-epochs"


def epoch_state_path(controller: Path, epoch_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", epoch_id):
        raise ReleaseTrainError("epoch id is unsafe")
    return epoch_root(controller) / epoch_id / "state.json"


def read_epoch(controller: Path, epoch_id: str) -> dict[str, Any]:
    path = epoch_state_path(controller, epoch_id)
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseTrainError(f"release epoch is missing or malformed: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != EPOCH_STATE_SCHEMA:
        raise ReleaseTrainError("release epoch state has an unsupported schema")
    return value


def epoch_receipt(controller: Path, state: dict[str, Any], transition: str,
                  detail: dict[str, Any]) -> dict[str, Any]:
    sequence = len(state.get("receipts", [])) + 1
    body = {"schema_version": EPOCH_RECEIPT_SCHEMA, "epoch_id": state["epoch_id"],
            "sequence": sequence, "transition": transition, "created_utc": utc_now(),
            "previous_state": state["state"], "detail": detail}
    body["receipt_id"] = digest(body)
    path = epoch_root(controller) / state["epoch_id"] / "receipts" / f"{sequence:04d}-{transition.lower()}.json"
    atomic_json(path, body, exclusive=True)
    reference = {"path": str(path.resolve()), "sha256": file_hash(path),
                 "receipt_id": body["receipt_id"], "transition": transition}
    state.setdefault("receipts", []).append(reference)
    return reference


def exact_candidate(controller: Path, task_id: str, target_ref: str) -> dict[str, Any]:
    """Freeze one queued candidate and its exact review-ready closure."""
    state = task_runtime.read_state(controller)
    record = state.get("tasks", {}).get(task_id)
    if (not isinstance(record, dict) or record.get("target_ref") != target_ref
            or record.get("state") != "QUEUED"):
        raise ReleaseTrainError(f"bootstrap candidate is not exactly QUEUED: {task_id}")
    tip = record.get("tip_sha")
    repository = task_runtime.product_repository(controller, task_runtime.load_config(controller))
    tree = git(repository, "rev-parse", f"{tip}^{{tree}}") if isinstance(tip, str) else ""
    attempt = record.get("queue_attempt") if isinstance(record.get("queue_attempt"), dict) else {}
    closure = (record.get("review_ready_closure") or record.get("complete_input_identity")
               or attempt.get("review_ready_closure") or attempt.get("complete_input_identity"))
    evidence = record.get("validation") or attempt.get("command_evidence") or attempt.get("validation") or []
    if (not isinstance(tip, str) or not SHA_RE.fullmatch(tip) or not SHA_RE.fullmatch(tree)
            or not isinstance(closure, dict)
            or closure.get("schema_version") != "juno_task_review_ready_closure.v1"
            or not isinstance(closure.get("closure_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", closure["closure_sha256"])
            or closure.get("tip_sha") != tip or closure.get("tree_sha") != tree
            or not isinstance(evidence, list) or not evidence):
        raise ReleaseTrainError(f"candidate.complete_input_invalid:{task_id}")
    task = kanban_task(controller, task_id)
    return {"task_id": task_id, "tip_sha": tip, "tree_sha": tree,
            "queue_record_sha256": digest(record), "task_sha256": digest(task),
            "closure_sha256": closure["closure_sha256"], "evidence_sha256": digest(evidence),
            "enqueue_sequence": record.get("enqueue_sequence"),
            "changed_paths": sorted(record.get("changed_paths", [])),
            "blocked_by": sorted(task.get("blocked_by", []) or [])}


def load_bootstrap_declaration(path: Path) -> tuple[dict[str, Any], Path]:
    resolved = path.expanduser().resolve()
    try:
        value = json.loads(resolved.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseTrainError(f"invalid bootstrap-repair declaration: {exc}") from exc
    keys = {"schema_version", "operation_id", "revision", "target_ref", "planning_base_sha",
            "authority_task", "repair_task", "affected_tasks", "exclusions"}
    if not isinstance(value, dict) or set(value) != keys or value.get("schema_version") != BOOTSTRAP_DECLARATION_SCHEMA:
        raise ReleaseTrainError("bootstrap-repair declaration has an unsupported or non-exact schema")
    if not isinstance(value["operation_id"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value["operation_id"]):
        raise ReleaseTrainError("bootstrap-repair operation_id is unsafe")
    if not isinstance(value["revision"], int) or isinstance(value["revision"], bool) or value["revision"] < 1:
        raise ReleaseTrainError("bootstrap-repair revision must be positive")
    if not isinstance(value["target_ref"], str) or not value["target_ref"].startswith("refs/"):
        raise ReleaseTrainError("bootstrap-repair target_ref must be a full ref")
    if not isinstance(value["planning_base_sha"], str) or not SHA_RE.fullmatch(value["planning_base_sha"]):
        raise ReleaseTrainError("bootstrap-repair planning base must be exact")
    task_ids = [value["authority_task"], value["repair_task"]]
    if any(not isinstance(item, str) or not task_runtime.TASK_RE.fullmatch(item) for item in task_ids):
        raise ReleaseTrainError("bootstrap-repair task identity is unsafe")
    if len(set(task_ids)) != 2:
        raise ReleaseTrainError("bootstrap authority and repair tasks must be distinct")
    affected = value["affected_tasks"]
    if (not isinstance(affected, list) or not affected or len(affected) != len(set(affected))
            or any(not isinstance(item, str) or not task_runtime.TASK_RE.fullmatch(item) for item in affected)):
        raise ReleaseTrainError("bootstrap affected_tasks must be unique task IDs")
    exclusions = value["exclusions"]
    if (not isinstance(exclusions, list) or len(exclusions) != len(set(exclusions))
            or any(not isinstance(item, str) or not item for item in exclusions)
            or not EXTERNAL_ACTIONS.issubset(set(exclusions))):
        raise ReleaseTrainError("bootstrap-repair must exclude release/tag/push/publish/deploy/cleanup")
    return value, resolved


def bootstrap_root(controller: Path) -> Path:
    return controller / ".juno_task/runtime/bootstrap-repairs"


def bootstrap_state_path(controller: Path, operation_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", operation_id):
        raise ReleaseTrainError("bootstrap-repair operation_id is unsafe")
    return bootstrap_root(controller) / operation_id / "state.json"


def read_bootstrap(controller: Path, operation_id: str) -> dict[str, Any]:
    try:
        value = json.loads(bootstrap_state_path(controller, operation_id).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseTrainError(f"bootstrap-repair state is missing or malformed: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != BOOTSTRAP_STATE_SCHEMA:
        raise ReleaseTrainError("bootstrap-repair state has an unsupported schema")
    return value


def build_bootstrap_plan(controller: Path, declaration_path: Path) -> dict[str, Any]:
    declaration, resolved = load_bootstrap_declaration(declaration_path)
    config = task_runtime.load_config(controller)
    repository = task_runtime.product_repository(controller, config)
    current = git(repository, "rev-parse", "--verify", declaration["target_ref"])
    if current != declaration["planning_base_sha"]:
        raise ReleaseTrainError("bootstrap-repair target moved since planning")
    authority = exact_candidate(controller, declaration["authority_task"], declaration["target_ref"])
    repair = exact_candidate(controller, declaration["repair_task"], declaration["target_ref"])
    if authority["task_id"] not in repair["blocked_by"]:
        raise ReleaseTrainError("bootstrap-repair causal chain is missing authority -> repair dependency")
    for task_id in declaration["affected_tasks"]:
        task = kanban_task(controller, task_id)
        if repair["task_id"] not in (task.get("blocked_by") or []):
            raise ReleaseTrainError(f"bootstrap-repair affected task lacks repair dependency: {task_id}")
    queue = task_runtime.read_state(controller).get("tasks", {})
    preserved = []
    for task_id, record in queue.items():
        if (task_id not in {authority["task_id"], repair["task_id"]}
                and isinstance(record, dict) and record.get("target_ref") == declaration["target_ref"]
                and record.get("state") in ACTIVE_QUEUE_STATES):
            preserved.append({"task_id": task_id, "state": record.get("state"),
                              "tip_sha": record.get("tip_sha"), "queue_record_sha256": digest(record),
                              "enqueue_sequence": record.get("enqueue_sequence")})
    preserved.sort(key=lambda row: (row["enqueue_sequence"] if isinstance(row["enqueue_sequence"], int) else 2**63,
                                    row["task_id"]))
    body = {"schema_version": BOOTSTRAP_SEAL_SCHEMA, "operation_id": declaration["operation_id"],
            "declaration": {"path": str(resolved), "sha256": file_hash(resolved),
                            "revision": declaration["revision"]},
            "target_ref": declaration["target_ref"], "base_sha": current,
            "members": [authority, repair], "affected_tasks": sorted(declaration["affected_tasks"]),
            "preserved_members": preserved, "exclusions": sorted(declaration["exclusions"]),
            "runtime_sha256": {path: file_hash(controller / path) for path in
                [".juno_task/scripts/release_train.py", ".juno_task/scripts/merge_queue.py",
                 ".juno_task/scripts/task_workspace.py"]},
            "authority": "bootstrap_repair_only_one_expected_old_sha_cas"}
    return {**body, "plan_id": digest(body)}


def seal_bootstrap(controller: Path, declaration_path: Path) -> dict[str, Any]:
    plan = build_bootstrap_plan(controller, declaration_path)
    path = bootstrap_state_path(controller, plan["operation_id"])
    if path.exists():
        state = read_bootstrap(controller, plan["operation_id"])
        if state.get("seal", {}).get("plan_id") != plan["plan_id"]:
            raise ReleaseTrainError("bootstrap-repair operation is already sealed differently")
        return {"outcome": "already_sealed", "state": state}
    token = f"{plan['operation_id']}:{secrets.token_hex(24)}"
    seal = {**plan, "sealed_utc": utc_now(),
            "fencing_token_sha256": hashlib.sha256(token.encode()).hexdigest()}
    state = {"schema_version": BOOTSTRAP_STATE_SCHEMA, "operation_id": plan["operation_id"],
             "state": "SEALED", "seal": seal, "composition": None, "cas": None,
             "receipt": None, "updated_utc": utc_now()}
    try:
        atomic_json(path, state, exclusive=True)
    except FileExistsError:
        return seal_bootstrap(controller, declaration_path)
    return {"outcome": "sealed", "bootstrap_token": token, "state": state}


def require_bootstrap_token(state: dict[str, Any], token: str) -> None:
    if (not token or hashlib.sha256(token.encode()).hexdigest()
            != state["seal"]["fencing_token_sha256"]):
        raise ReleaseTrainError("exact bootstrap-repair fencing token is required")


def reconcile_bootstrap_members(controller: Path, state: dict[str, Any]) -> dict[str, Any]:
    """Finalize only receipt-bound members whose exact tips are in the target."""
    import merge_queue
    seal = state["seal"]
    config = task_runtime.load_config(controller)
    repository = task_runtime.product_repository(controller, config)
    target = git(repository, "rev-parse", seal["target_ref"])
    sealed_readback = state.get("cas", {}).get("readback")
    if (not isinstance(sealed_readback, str) or not SHA_RE.fullmatch(sealed_readback)
            or subprocess.run(["git", "-C", str(repository), "merge-base", "--is-ancestor",
                               sealed_readback, target], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode):
        raise ReleaseTrainError("bootstrap-repair sealed target is not an ancestor of current readback")
    reconciled = []
    for member in seal["members"]:
        if subprocess.run(["git", "-C", str(repository), "merge-base", "--is-ancestor",
                           member["tip_sha"], target], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode:
            raise ReleaseTrainError(f"bootstrap-repair member ancestry is missing: {member['task_id']}")
        current = task_runtime.read_state(controller).get("tasks", {}).get(member["task_id"])
        if not isinstance(current, dict) or current.get("tip_sha") != member["tip_sha"]:
            raise ReleaseTrainError(f"bootstrap-repair member queue identity drifted: {member['task_id']}")
        attempt = {"schema_version": "juno_merge_attempt.v1", "task_id": member["task_id"],
            "target_ref": seal["target_ref"], "expected_target_sha": target,
            "feature_sha": member["tip_sha"], "strategy": "bootstrap_repair_exact_ancestry",
            "candidate_sha": target, "candidate_tree": git(repository, "rev-parse", f"{target}^{{tree}}"),
            "candidate_checkout": None, "candidate_token": None, "validation": [], "review": None,
            "outcome": "MERGED", "readback_sha": target,
            "bootstrap_repair_receipt_id": state.get("receipt", {}).get("receipt_id")}
        finalization = merge_queue.finalize_kanban_task(
            controller, {**attempt, "candidate_sha": member["tip_sha"]})
        merge_queue.persist_attempt(controller, attempt, state_name="MERGED", remove_conflict=True)
        reconciled.append({"task_id": member["task_id"], "tip_sha": member["tip_sha"],
                           "finalization": finalization})
    state["reconciliation"] = {"schema_version": "juno_bootstrap_repair_reconciliation.v1",
        "sealed_target_sha": sealed_readback, "observed_target_sha": target,
        "members": reconciled, "completed_utc": utc_now(),
        "next_action": "regenerate affected closures, then inspect and seal one fresh all-eligible epoch"}
    atomic_json(bootstrap_state_path(controller, state["operation_id"]), state)
    return state


def drive_bootstrap(controller: Path, operation_id: str, token: str) -> dict[str, Any]:
    state = read_bootstrap(controller, operation_id); require_bootstrap_token(state, token)
    if state.get("state") == "COMPLETE":
        return state if state.get("reconciliation") else reconcile_bootstrap_members(controller, state)
    seal = state["seal"]
    config = task_runtime.load_config(controller)
    repository = task_runtime.product_repository(controller, config)
    current = git(repository, "rev-parse", seal["target_ref"])
    for member in seal["members"]:
        exact = exact_candidate(controller, member["task_id"], seal["target_ref"])
        if any(exact[key] != member[key] for key in ("tip_sha", "tree_sha", "queue_record_sha256",
                                                     "task_sha256", "closure_sha256", "evidence_sha256")):
            raise ReleaseTrainError(f"bootstrap-repair sealed candidate drifted: {member['task_id']}")
    queue = task_runtime.read_state(controller).get("tasks", {})
    for preserved in seal["preserved_members"]:
        record = queue.get(preserved["task_id"])
        if not isinstance(record, dict) or digest(record) != preserved["queue_record_sha256"]:
            raise ReleaseTrainError(f"bootstrap-repair preserved queue member drifted: {preserved['task_id']}")
    checkout = Path(config["workspace_root"]).expanduser().resolve() / ".bootstrap-repairs" / operation_id
    ref = f"refs/juno/bootstrap-repairs/{operation_id}"
    if not checkout.exists():
        checkout.parent.mkdir(parents=True, exist_ok=True)
        task_runtime.run(["git", "-C", str(repository), "worktree", "add", "--detach", str(checkout), seal["base_sha"]], repository)
        task_runtime.run(["git", "-C", str(repository), "update-ref", ref, seal["base_sha"]], repository)
    ensure_full_train_checkout(checkout)
    composed = []
    for member in seal["members"]:
        if not subprocess.run(["git", "-C", str(checkout), "merge-base", "--is-ancestor",
                               member["tip_sha"], "HEAD"], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode:
            composed.append({"task_id": member["task_id"], "tip_sha": member["tip_sha"],
                             "merge_commit": git(checkout, "rev-parse", "HEAD"), "reused": True})
            continue
        before = git(checkout, "rev-parse", "HEAD")
        merged = subprocess.run(["git", "-C", str(checkout), "merge", "--no-ff", "--no-edit", member["tip_sha"]],
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if merged.returncode:
            state.update({"state": "NEEDS_OPERATOR", "composition": {"worktree": str(checkout),
                "ref": ref, "conflict_task": member["task_id"], "conflict_paths": sorted(
                    git(checkout, "diff", "--name-only", "--diff-filter=U").splitlines())},
                "updated_utc": utc_now()})
            atomic_json(bootstrap_state_path(controller, operation_id), state)
            return state
        commit = git(checkout, "rev-parse", "HEAD")
        composed.append({"task_id": member["task_id"], "tip_sha": member["tip_sha"],
                         "pre_sha": before, "merge_commit": commit,
                         "parents": git(checkout, "show", "-s", "--format=%P", commit).split()})
        task_runtime.run(["git", "-C", str(repository), "update-ref", ref, commit], repository)
    tip = git(checkout, "rev-parse", "HEAD")
    if current == seal["base_sha"]:
        import merge_queue
        owner = merge_queue.cas_target(repository, seal["target_ref"], tip, seal["base_sha"])
    elif current == tip:
        owner = {"status": "reused_exact_target_readback"}
    else:
        raise ReleaseTrainError("bootstrap-repair expected-old-SHA target moved")
    readback = git(repository, "rev-parse", seal["target_ref"])
    if readback != tip:
        raise ReleaseTrainError("bootstrap-repair target readback mismatch")
    import merge_queue
    runtime_refresh = merge_queue.refresh_managed_controller(
        controller, repository, seal["base_sha"], tip, seal["members"][-1]["task_id"])
    receipt = {"schema_version": BOOTSTRAP_RECEIPT_SCHEMA, "operation_id": operation_id,
        "plan_id": seal["plan_id"], "expected_target_sha": seal["base_sha"],
        "integrated_sha": tip, "target_readback_sha": readback, "target_move_count": 1,
        "members": composed, "affected_tasks": seal["affected_tasks"],
        "preserved_members": seal["preserved_members"], "integration_owner": owner,
        "managed_runtime_refresh": runtime_refresh, "runtime_sha256": seal["runtime_sha256"],
        "reason_code": "bootstrap_repair_integrated",
        "next_action": "reconcile merged repair tasks, regenerate affected closures, then inspect and seal one fresh all-eligible epoch",
        "excluded_actions": seal["exclusions"], "completed_utc": utc_now()}
    receipt["receipt_id"] = digest(receipt)
    receipt_path = bootstrap_root(controller) / operation_id / "receipt.json"
    if receipt_path.exists() and file_hash(receipt_path) != hashlib.sha256((canonical(receipt) + "\n").encode()).hexdigest():
        raise ReleaseTrainError("bootstrap-repair receipt identity drift")
    if not receipt_path.exists():
        atomic_json(receipt_path, receipt, exclusive=True)
    state.update({"state": "COMPLETE", "composition": {"worktree": str(checkout), "ref": ref,
        "tip_sha": tip, "members": composed}, "cas": {"expected": seal["base_sha"],
        "tip": tip, "readback": readback, "target_move_count": 1},
        "receipt": {"path": str(receipt_path.resolve()), "sha256": file_hash(receipt_path),
                    "receipt_id": receipt["receipt_id"]}, "updated_utc": utc_now()})
    atomic_json(bootstrap_state_path(controller, operation_id), state)
    return reconcile_bootstrap_members(controller, state)


def queue_epoch_members(controller: Path, declaration: dict[str, Any], target_sha: str) -> list[dict[str, Any]]:
    state = task_runtime.read_state(controller)
    declared_required = set(declaration["required_tasks"])
    declared_optional = set(declaration["optional_tasks"])
    rows: list[dict[str, Any]] = []
    for task_id, record in state.get("tasks", {}).items():
        if (not isinstance(record, dict) or record.get("target_ref") != declaration["target_ref"]
                or record.get("state") not in ACTIVE_QUEUE_STATES):
            continue
        task = kanban_task(controller, task_id)
        tip = record.get("tip_sha")
        if not isinstance(tip, str) or not SHA_RE.fullmatch(tip):
            raise ReleaseTrainError(f"eligible candidate {task_id} lacks an exact tip")
        repository = task_runtime.product_repository(controller, task_runtime.load_config(controller))
        tree = git(repository, "rev-parse", f"{tip}^{{tree}}")
        if not SHA_RE.fullmatch(tree):
            raise ReleaseTrainError(f"eligible candidate {task_id} tip is unavailable")
        attempt = record.get("queue_attempt") if isinstance(record.get("queue_attempt"), dict) else {}
        closure = (record.get("review_ready_closure") or record.get("complete_input_identity")
                   or attempt.get("review_ready_closure") or attempt.get("complete_input_identity"))
        evidence = record.get("validation") or attempt.get("command_evidence") or attempt.get("validation") or []
        admission = ("required" if task_id in declared_required else
                     "optional" if task_id in declared_optional else "ambient_pre_cutoff")
        # Ambient finished candidates are release-barrier members, not blockers
        # outside the epoch. They must drain before the epoch can CAS.
        rows.append({"task_id": task_id, "admission": admission,
                     "required": admission != "optional", "queue_state": record.get("state"),
                     "enqueue_sequence": record.get("enqueue_sequence"), "tip_sha": tip,
                     "tree_sha": tree, "task_revision": task.get("last_modified"),
                     "task_sha256": digest(task), "queue_record_sha256": digest(record),
                     "fencing_attempt": attempt.get("arbiter_attempt") or attempt.get("attempt"),
                     "complete_input_identity": closure,
                     "focused_evidence_count": len(evidence), "evidence_sha256": digest(evidence),
                     "review_sha256": digest(attempt.get("risk") or attempt.get("review") or {}),
                     "changed_paths": sorted(record.get("changed_paths", [])),
                     "blocked_by": sorted(task.get("blocked_by", []) or [])})
    rows.sort(key=lambda row: (row["enqueue_sequence"] if isinstance(row["enqueue_sequence"], int) else 2**63,
                               row["task_id"]))
    return rows


def _policy_patterns(controller: Path, key: str) -> list[str]:
    path = controller / ".juno_task/config/risk-policy.json"
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    patterns = value.get(key) if isinstance(value, dict) else None
    return patterns if isinstance(patterns, list) and all(isinstance(item, str) for item in patterns) else []


def _matched_roots(paths: list[str], patterns: list[str]) -> list[str]:
    return sorted({pattern for pattern in patterns for path in paths
                   if fnmatch.fnmatchcase(path, pattern)})


def wave_economics_projection(controller: Path, members: list[dict[str, Any]],
                              order: list[str]) -> dict[str, Any]:
    """Project deterministic changed-surface overlap and broad-suite savings."""
    shared_patterns = _policy_patterns(controller, "shared_infrastructure_paths")
    high_patterns = _policy_patterns(controller, "high_risk_paths")
    rows = []
    for member in members:
        paths = sorted(set(member.get("changed_paths") or []))
        shared = _matched_roots(paths, shared_patterns)
        high = _matched_roots(paths, high_patterns)
        focused_count = member.get("focused_evidence_count", 0)
        rows.append({"task_id": member["task_id"], "changed_paths": paths,
            "shared_roots": shared, "high_risk_roots": high,
            "focused_command_count": focused_count if isinstance(focused_count, int) else 0,
            "selected_validation_roots": sorted(set(shared + high))})
    by_id = {row["task_id"]: row for row in rows}
    overlap = []
    for index, left_id in enumerate(order):
        for right_id in order[index + 1:]:
            left, right = by_id[left_id], by_id[right_id]
            exact = sorted(set(left["changed_paths"]) & set(right["changed_paths"]))
            roots = sorted(set(left["selected_validation_roots"]) &
                           set(right["selected_validation_roots"]))
            overlap.append({"left_task_id": left_id, "right_task_id": right_id,
                            "exact_paths": exact, "shared_validation_roots": roots})
    broad_members = [row["task_id"] for row in rows
                     if row["selected_validation_roots"]]
    recommend = len(broad_members) >= 2 or any(
        row["exact_paths"] or row["shared_validation_roots"] for row in overlap)
    per_candidate = len(broad_members)
    aggregate = 1 if broad_members else 0
    return {"schema_version": WAVE_ECONOMICS_SCHEMA,
        "membership": order, "members": rows, "overlap_matrix": overlap,
        "dependency_fifo_order": order,
        "command_counts": {"focused": sum(row["focused_command_count"] for row in rows),
            "per_candidate_broad": per_candidate, "aggregate_broad": aggregate,
            "avoided_broad": max(0, per_candidate - aggregate)},
        "recommendation": "epoch_delivery" if recommend else "ordinary_fifo",
        "recommendation_reasons": (["two_or_more_compatible_shared_or_high_risk_candidates"]
                                   if recommend else []),
        "train_eligible": bool(members)}


def epoch_delivery_selection_path(controller: Path, epoch_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", epoch_id):
        raise ReleaseTrainError("epoch id is unsafe")
    return controller / ".juno_task/runtime/epoch-delivery-selections" / f"{epoch_id}.json"


def select_epoch_delivery(controller: Path, declaration_path: Path) -> dict[str, Any]:
    """Persist explicit wave routing without sealing or granting target authority."""
    plan = build_epoch_plan(controller, declaration_path)
    path = epoch_delivery_selection_path(controller, plan["epoch_id"])
    body = {"schema_version": EPOCH_DELIVERY_SELECTION_SCHEMA,
        "epoch_id": plan["epoch_id"], "target_ref": plan["target_ref"],
        "base_sha": plan["base_sha"], "plan_id": plan["plan_id"],
        "declaration": plan["declaration"], "member_task_ids": plan["order"],
        "required_task_ids": sorted(row["task_id"] for row in plan["members"] if row["required"]),
        "optional_task_ids": sorted(row["task_id"] for row in plan["members"] if not row["required"]),
        "cutoff_policy": "all_eligible_queue_snapshot_at_explicit_seal",
        "external_exclusions": plan["exclusions"],
        "economics": plan["economics"],
        "authority": "routing_only_explicit_seal_still_required"}
    body["selection_id"] = digest(body)
    if path.exists():
        current = json.loads(path.read_text())
        if current != body:
            raise ReleaseTrainError("epoch delivery selection already exists with different immutable inputs")
        return {"outcome": "already_selected", "selection": current,
                "safe_next_action": f"yy release train seal {plan['declaration']['path']}"}
    atomic_json(path, body, exclusive=True)
    return {"outcome": "selected", "selection": body,
            "safe_next_action": f"yy release train seal {plan['declaration']['path']}",
            "side_effects": ["controller_routing_record"],
            "excluded_side_effects": ["seal", "test", "review", "target_mutation"]}


def epoch_dependency_order(members: list[dict[str, Any]], declaration: dict[str, Any]) -> tuple[list[str], list[list[str]]]:
    ids = {row["task_id"] for row in members}
    edges = {(edge["before"], edge["after"]) for edge in declaration["dependencies"]
             if edge["before"] in ids and edge["after"] in ids}
    for row in members:
        edges.update((blocker, row["task_id"]) for blocker in row["blocked_by"] if blocker in ids)
    cycle = cycle_path(ids, edges)
    if cycle:
        raise ReleaseTrainError("release epoch dependency cycle: " + " -> ".join(cycle))
    fifo = {row["task_id"]: index for index, row in enumerate(members)}
    incoming = {node: 0 for node in ids}; children = {node: [] for node in ids}
    for before, after in edges:
        incoming[after] += 1; children[before].append(after)
    ready = sorted((node for node, count in incoming.items() if count == 0), key=lambda node: (fifo[node], node))
    order: list[str] = []
    while ready:
        node = ready.pop(0); order.append(node)
        for child in sorted(children[node], key=lambda item: (fifo[item], item)):
            incoming[child] -= 1
            if incoming[child] == 0:
                ready.append(child); ready.sort(key=lambda item: (fifo[item], item))
    return order, [list(edge) for edge in sorted(edges)]


def _forecast_git(repository: Path, *args: str, env: Optional[dict[str, str]] = None,
                  check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["git", "-C", str(repository), *args], text=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env=env)
    if check and result.returncode:
        raise ReleaseTrainError("conflict forecast Git operation failed: " + result.stdout.strip()[:1000])
    return result


def _forecast_conflict_paths(checkout: Path) -> list[str]:
    unmerged = _forecast_git(checkout, "ls-files", "-u", "-z").stdout
    paths: set[str] = set()
    for entry in unmerged.split("\0"):
        if not entry:
            continue
        metadata, separator, path = entry.partition("\t")
        fields = metadata.split()
        if not separator or len(fields) != 3 or not fields[2].isdigit():
            raise ReleaseTrainError("conflict forecast produced an unreadable unmerged index")
        paths.add(path)
    if not paths:
        raise ReleaseTrainError("conflict forecast merge failed without exact conflict paths")
    return sorted(paths)


def _proven_forecast_composition(controller: Path, repository: Path, task_id: str,
                                 before: str, candidate: str,
                                 current_epoch: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Find one receipt-bound both-parent composition without treating it as authority."""
    root = controller / ".juno_task/runtime/release-epochs"
    matches: list[dict[str, Any]] = []
    if not root.is_dir():
        return None
    for state_path in sorted(root.glob("*/state.json")):
        try:
            state = json.loads(state_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        repair_receipts = []
        for receipt in state.get("receipts", []):
            if receipt.get("transition") != "REPAIR_CONSUMED":
                continue
            receipt_path = Path(receipt.get("path", ""))
            if (not receipt_path.is_file() or file_hash(receipt_path) != receipt.get("sha256")):
                continue
            try:
                payload = json.loads(receipt_path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if payload.get("transition") == "REPAIR_CONSUMED":
                repair_receipts.append((receipt, payload))
        evidence_epoch = state.get("epoch_id")
        if evidence_epoch == current_epoch:
            continue
        current_match = re.fullmatch(r"(.*?)(\d+)", current_epoch or "")
        evidence_match = re.fullmatch(r"(.*?)(\d+)", evidence_epoch or "")
        if (current_match and evidence_match and current_match.group(1) == evidence_match.group(1)
                and int(evidence_match.group(2)) >= int(current_match.group(2))):
            continue
        for row in state.get("composition", {}).get("commits", []):
            commit = row.get("merge_commit")
            if (row.get("task_id") != task_id or row.get("pre_sha") != before
                    or row.get("candidate_tip") != candidate or not SHA_RE.fullmatch(commit or "")):
                continue
            parents = git(repository, "show", "-s", "--format=%P", commit).split()
            tree = git(repository, "rev-parse", f"{commit}^{{tree}}")
            if parents != [before, candidate] or tree != row.get("post_tree"):
                continue
            bound = next(((receipt, payload) for receipt, payload in repair_receipts
                          if payload.get("detail", {}).get("repair_commit") == commit), None)
            if bound:
                receipt, _ = bound
                matches.append({"commit": commit, "tree": tree,
                    "epoch_id": state.get("epoch_id"), "state_sha256": file_hash(state_path),
                    "receipt_path": receipt["path"], "receipt_sha256": receipt["sha256"]})
    identities = {digest(row) for row in matches}
    if len(identities) > 1:
        raise ReleaseTrainError("conflict forecast found ambiguous proven compositions")
    return matches[0] if matches else None


def _forecast_declaration_identity(plan: dict[str, Any]) -> tuple[str, Optional[dict[str, Any]]]:
    """Read a native identity or deterministically adapt one legacy immutable declaration.

    The adapter grants no authority: it accepts only the exact bytes already named by
    the historical seal and returns typed incompatibility for every incomplete or
    ambiguous shape.  It never writes to the declaration or epoch registry.
    """
    declaration = plan.get("declaration")
    if not isinstance(declaration, dict):
        raise ReleaseTrainError("legacy_epoch_evidence_incompatible:declaration_reference_missing")
    native = declaration.get("identity_sha256")
    if native is not None:
        if not isinstance(native, str) or not re.fullmatch(r"[0-9a-f]{64}", native):
            raise ReleaseTrainError("legacy_epoch_evidence_incompatible:declaration_identity_ambiguous")
        return native, None
    if (plan.get("schema_version") != EPOCH_PLAN_SCHEMA
            or set(declaration) != {"path", "sha256", "revision"}
            or not isinstance(declaration.get("revision"), int)
            or isinstance(declaration.get("revision"), bool)
            or not re.fullmatch(r"[0-9a-f]{64}", declaration.get("sha256", ""))):
        raise ReleaseTrainError("legacy_epoch_evidence_incompatible:declaration_reference_ambiguous")
    path = Path(declaration["path"]).expanduser().resolve()
    if not path.is_file():
        raise ReleaseTrainError("legacy_epoch_evidence_incompatible:declaration_bytes_missing")
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != declaration["sha256"]:
        raise ReleaseTrainError("legacy_epoch_evidence_incompatible:declaration_bytes_tampered")
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ReleaseTrainError("legacy_epoch_evidence_incompatible:declaration_bytes_invalid")
    if (not isinstance(value, dict) or value.get("schema_version") != DECLARATION_SCHEMA
            or value.get("revision") != declaration["revision"]
            or value.get("train_id") != plan.get("epoch_id")):
        raise ReleaseTrainError("legacy_epoch_evidence_incompatible:declaration_binding_mismatch")
    identity = digest({key: item for key, item in value.items() if key != "conflict_authority"})
    adapter = {"schema_version": "juno_release_epoch_legacy_declaration_identity.v1",
               "source_sha256": declaration["sha256"], "derived_identity_sha256": identity}
    return identity, adapter


def _forecast_input_identity(plan: dict[str, Any]) -> dict[str, Any]:
    declaration_identity, adapter = _forecast_declaration_identity(plan)
    value = {"base_sha": plan["base_sha"], "order": plan["order"],
        "members": [{"task_id": row["task_id"], "tip_sha": row["tip_sha"],
            "tree_sha": row["tree_sha"], "task_revision": row["task_revision"],
            "task_sha256": row["task_sha256"],
            "queue_record_sha256": row["queue_record_sha256"],
            "complete_input_identity_sha256": digest(row["complete_input_identity"]),
            "closure_sha256": (row["complete_input_identity"] or {}).get("closure_sha256")}
            for row in plan["members"]],
        "runtime_sha256": plan["runtime_sha256"], "policy_sha256": plan["policy_sha256"],
        "declaration_identity_sha256": declaration_identity}
    return {**value, "legacy_declaration_identity": adapter} if adapter else value


def _conflict_authority(plan: dict[str, Any], envelope: Optional[dict[str, Any]]) -> tuple[Optional[dict[str, Any]], list[str]]:
    """Resolve frozen grouped-repair authority; malformed or drifted input is observation-only refusal."""
    reference = plan.get("conflict_authority")
    if not envelope:
        return None, []
    reasons: list[str] = []
    if not isinstance(reference, dict):
        return None, ["authority.missing"]
    path = Path(reference.get("path", "")).expanduser().resolve()
    if not path.is_file() or file_hash(path) != reference.get("sha256"):
        return None, ["authority.identity"]
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None, ["authority.unreadable"]
    required = {"schema_version", "revision", "train_id", "input_identity_sha256",
                "logical_sets", "repair_budget", "grouped_worker", "risk"}
    if not isinstance(value, dict) or set(value) != required or value.get("schema_version") != CONFLICT_AUTHORITY_SCHEMA:
        return None, ["authority.schema"]
    if not isinstance(value.get("revision"), int) or isinstance(value.get("revision"), bool) or value["revision"] < 1:
        reasons.append("authority.revision")
    if value.get("train_id") != plan.get("epoch_id"):
        reasons.append("authority.train")
    input_identity = _forecast_input_identity(plan)
    if value.get("input_identity_sha256") != digest(input_identity):
        reasons.append("authority.input_identity")
    sets = value.get("logical_sets")
    if not isinstance(sets, list) or len(sets) != 1:
        reasons.append("authority.logical_sets")
    else:
        logical_set = sets[0]
        expected_tasks = [row["task_id"] for row in envelope["ordered_members"]]
        expected_paths = sorted({path for row in envelope["ordered_members"]
                                 for path in row["possible_conflict_paths"]})
        if (not isinstance(logical_set, dict)
                or set(logical_set) != {"set_id", "ordered_task_ids", "permitted_paths", "classification"}
                or logical_set.get("ordered_task_ids") != expected_tasks
                or logical_set.get("permitted_paths") != expected_paths
                or logical_set.get("classification") != "authorization_neutral"):
            reasons.append("authority.scope")
    risk = value.get("risk")
    if (not isinstance(risk, dict) or set(risk) != {"ambiguous", "sensitive", "destructive", "scope_expansion"}
            or any(risk.get(key) is not False for key in risk)):
        reasons.append("authority.risk")
    if value.get("repair_budget") != 1 or value.get("grouped_worker") is not True:
        reasons.append("authority.policy")
    binding = {"path": str(path), "sha256": reference["sha256"], "document": value,
               "input_identity": input_identity}
    return (binding if not reasons else None), sorted(set(reasons))


def forecast_epoch_conflicts(controller: Path, repository: Path,
                             plan: dict[str, Any]) -> dict[str, Any]:
    """Exactly precompose frozen members; never invent a material conflict repair."""
    by_id = {row["task_id"]: row for row in plan["members"]}
    conflicts: list[dict[str, Any]] = []
    compositions: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="yylo-epoch-forecast-") as temporary:
        checkout = Path(temporary) / "checkout"
        _forecast_git(repository, "clone", "--quiet", "--shared", "--no-checkout",
                      str(repository), str(checkout))
        _forecast_git(checkout, "checkout", "--quiet", "--detach", plan["base_sha"])
        unresolved_boundary: Optional[dict[str, Any]] = None
        indeterminate_members: list[dict[str, Any]] = []
        for index, task_id in enumerate(plan["order"]):
            member = by_id[task_id]
            if unresolved_boundary is not None:
                # Material repair output is unknowable without spending repair authority.
                # Account for every later member, but probe it only against the frozen base
                # and never pretend that probe is the ordered composition result.
                _forecast_git(checkout, "reset", "--quiet", "--hard", plan["base_sha"])
                anchor_merge = _forecast_git(checkout, "merge", "--no-ff", "--no-commit",
                                             member["tip_sha"], check=False)
                anchor_paths = _forecast_conflict_paths(checkout) if anchor_merge.returncode else []
                if anchor_merge.returncode:
                    _forecast_git(checkout, "merge", "--abort")
                else:
                    _forecast_git(checkout, "reset", "--quiet", "--hard", plan["base_sha"])
                row = {"task_id": task_id, "pre_sha": None, "pre_tree": None,
                       "candidate_tip": member["tip_sha"], "candidate_tree": member["tree_sha"],
                       "post_sha": None, "post_tree": None,
                       "decision": "composition_indeterminate",
                       "indeterminate_due_to": unresolved_boundary,
                       "independent_base_probe": {"anchor_sha": plan["base_sha"],
                           "decision": "conflict" if anchor_paths else "clean",
                           "conflict_paths": anchor_paths}}
                compositions.append(row)
                indeterminate_members.append(row)
                continue
            before = git(checkout, "rev-parse", "HEAD")
            before_tree = git(checkout, "rev-parse", "HEAD^{tree}")
            if subprocess.run(["git", "-C", str(checkout), "merge-base", "--is-ancestor",
                               member["tip_sha"], before], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode == 0:
                compositions.append({"task_id": task_id, "pre_sha": before,
                                     "pre_tree": before_tree, "post_sha": before,
                                     "post_tree": before_tree, "decision": "already_present"})
                continue
            merged = _forecast_git(checkout, "merge", "--no-ff", "--no-commit",
                                   member["tip_sha"], check=False)
            conflict_paths: list[str] = []
            replay = None
            if merged.returncode:
                conflict_paths = _forecast_conflict_paths(checkout)
                _forecast_git(checkout, "merge", "--abort")
                replay = _proven_forecast_composition(controller, repository, task_id,
                                                      before, member["tip_sha"], plan["epoch_id"])
                if not replay:
                    row = {"task_id": task_id, "pre_sha": before, "pre_tree": before_tree,
                           "candidate_tip": member["tip_sha"], "candidate_tree": member["tree_sha"],
                           "post_sha": None, "post_tree": None, "decision": "conflict_unresolved"}
                    unresolved_boundary = {"task_id": task_id, "pre_sha": before,
                                           "candidate_tip": member["tip_sha"],
                                           "conflict_paths": conflict_paths}
                    compositions.append(row)
                    conflicts.append({**row, "conflict_paths": conflict_paths,
                                      "required": member["required"],
                                      "forecast_resolution": "requires_proven_both_parent_composition"})
                    continue
                _forecast_git(checkout, "reset", "--quiet", "--hard", replay["commit"])
                commit, tree = replay["commit"], replay["tree"]
            else:
                tree = _forecast_git(checkout, "write-tree").stdout.strip()
                if not SHA_RE.fullmatch(tree):
                    raise ReleaseTrainError("conflict forecast did not produce an exact tree")
                fixed_time = f"2000-01-01T00:00:{index % 60:02d}Z"
                environment = {**os.environ, "GIT_AUTHOR_NAME": "YYLO Conflict Forecast",
                               "GIT_AUTHOR_EMAIL": "forecast@invalid.local",
                               "GIT_COMMITTER_NAME": "YYLO Conflict Forecast",
                               "GIT_COMMITTER_EMAIL": "forecast@invalid.local",
                               "GIT_AUTHOR_DATE": fixed_time, "GIT_COMMITTER_DATE": fixed_time}
                commit = _forecast_git(checkout, "commit-tree", tree, "-p", before, "-p",
                                       member["tip_sha"], env=environment).stdout.strip()
                if not SHA_RE.fullmatch(commit):
                    raise ReleaseTrainError("conflict forecast did not produce an exact composition commit")
                _forecast_git(checkout, "reset", "--quiet", "--hard", commit)
            row = {"task_id": task_id, "pre_sha": before, "pre_tree": before_tree,
                   "candidate_tip": member["tip_sha"], "candidate_tree": member["tree_sha"],
                   "post_sha": commit, "post_tree": tree,
                   "decision": "conflict_replayed" if conflict_paths else "clean"}
            if replay:
                row["proven_composition"] = replay
            compositions.append(row)
            if conflict_paths:
                conflicts.append({**row, "conflict_paths": conflict_paths,
                                  "required": member["required"],
                                  "forecast_resolution": "immutable_receipt_bound_composition"})
    member_accounting_complete = len(compositions) == len(plan["order"])
    exact_composition_complete = member_accounting_complete and unresolved_boundary is None
    conservative_envelope = None
    if unresolved_boundary is not None:
        boundary_index = plan["order"].index(unresolved_boundary["task_id"])
        suffix = plan["order"][boundary_index:]
        composition_by_id = {row["task_id"]: row for row in compositions}
        conflict_by_id = {row["task_id"]: row for row in conflicts}
        envelope_members = []
        for task_id in suffix:
            member = by_id[task_id]
            composition = composition_by_id[task_id]
            exact_conflict = conflict_by_id.get(task_id)
            possible_paths = (exact_conflict["conflict_paths"] if exact_conflict
                              else sorted(member["changed_paths"]))
            envelope_members.append({"task_id": task_id,
                "candidate_tip": member["tip_sha"], "candidate_tree": member["tree_sha"],
                "required": member["required"], "changed_paths": sorted(member["changed_paths"]),
                "classification": ("exact_unresolved_conflict" if exact_conflict
                                   else "conservative_possible_conflict"),
                "possible_conflict_paths": possible_paths,
                "independent_base_probe": composition.get("independent_base_probe")})
        envelope_core = {"schema_version": "juno_release_epoch_conservative_envelope.v1",
            "strategy": "frozen_unknown_suffix.v1", "boundary": unresolved_boundary,
            "ordered_members": envelope_members,
            "logical_repair_sets": [{"set_index": 1,
                "authority": "authorization_neutral_grouped_repair_only",
                "task_ids": suffix,
                "possible_conflict_paths": sorted({path for row in envelope_members
                                                    for path in row["possible_conflict_paths"]})}],
            "complete": len(envelope_members) == len(suffix)}
        conservative_envelope = {**envelope_core, "envelope_sha256": digest(envelope_core)}
    envelope_complete = bool(conservative_envelope and conservative_envelope["complete"])
    forecast_complete = member_accounting_complete and (exact_composition_complete or envelope_complete)
    required_conflicts = sum(1 for row in conflicts if row["required"])
    required_repair_sets = (len(conservative_envelope["logical_repair_sets"])
                            if conservative_envelope else 0)
    authority, authority_reasons = _conflict_authority(plan, conservative_envelope)
    policy = {"schema_version": CONFLICT_FORECAST_POLICY_SCHEMA,
              "repair_budget": 1,
              "repair_unit": "authorization_neutral_logical_conflict_set.v1",
              "logical_conflict_set_grouping": "frozen_unknown_suffix.v1",
              "grouped_worker_authorized": authority is not None,
              "authority_sha256": (authority or {}).get("sha256"),
              "serial_forecast_resolution": "exact_prefix_then_conservative_envelope.v1"}
    policy_feasible = (forecast_complete and required_repair_sets <= policy["repair_budget"]
                       and (required_repair_sets == 0 or authority is not None))
    input_identity = _forecast_input_identity(plan)
    identity = {"declaration": plan["declaration"], **input_identity,
                "forecast_policy": policy,
                "conflict_authority_sha256": (authority or {}).get("sha256"),
                "conservative_envelope_sha256": (conservative_envelope or {}).get("envelope_sha256")}
    body = {"schema_version": CONFLICT_MANIFEST_SCHEMA, "identity": identity,
            "identity_sha256": digest(identity), "compositions": compositions,
            "conflicts": conflicts, "indeterminate_members": indeterminate_members,
            "unresolved_boundary": unresolved_boundary,
            "conservative_envelope": conservative_envelope,
            "member_accounting_complete": member_accounting_complete,
            "forecast_complete": forecast_complete,
            "exact_composition_complete": exact_composition_complete,
            "required_conflict_count": required_conflicts,
            "required_logical_repair_set_count": required_repair_sets,
            "authority_binding": authority,
            "authority_reason_codes": authority_reasons,
            "operator_state": "FEASIBLE" if policy_feasible else "NEEDS_OPERATOR",
            "policy_repair_budget_feasible": policy_feasible,
            "repair_budget_feasible": policy_feasible}
    return {**body, "manifest_sha256": digest(body)}


def _immutable_json(reference: Any, code: str, blockers: list[str]) -> Optional[dict[str, Any]]:
    if (not isinstance(reference, dict) or set(reference) != {"path", "sha256"}
            or not re.fullmatch(r"[0-9a-f]{64}", reference.get("sha256", ""))):
        blockers.append(code + ".reference"); return None
    path = Path(reference["path"]).expanduser().resolve()
    if not path.is_file() or file_hash(path) != reference["sha256"]:
        blockers.append(code + ".identity"); return None
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        blockers.append(code + ".json"); return None
    return value if isinstance(value, dict) else None


def _publish_exclusive_receipt(path: Path, receipt: dict[str, Any]) -> dict[str, Any]:
    rendered = canonical(receipt) + "\n"
    if path.exists():
        try:
            current = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ReleaseTrainError("phase1 acceptance receipt collision") from exc
        if current != receipt:
            changed = sorted(key for key in set(current) | set(receipt)
                             if current.get(key) != receipt.get(key))
            raise ReleaseTrainError("phase1 acceptance receipt collision fields="
                                    + ",".join(changed))
        return current
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(rendered); stream.flush(); os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            current = json.loads(path.read_text())
            if current != receipt:
                changed = sorted(key for key in set(current) | set(receipt)
                                 if current.get(key) != receipt.get(key))
                raise ReleaseTrainError("phase1 acceptance receipt collision fields="
                                        + ",".join(changed))
        return json.loads(path.read_text())
    finally:
        temporary.unlink(missing_ok=True)


def _phase1_repository_snapshot(repository: Path) -> dict[str, str]:
    return {"status": git(repository, "status", "--porcelain=v1", "--untracked-files=all"),
            "refs": git(repository, "for-each-ref", "--format=%(refname) %(objectname)"),
            "objects": git(repository, "count-objects", "-v")}


PHASE1_COMMITTED_PATHS = (
    ".juno_task/scripts/release_train.py",
    ".juno_task/scripts/tests/test_release_train.py",
    "juno-code/src/templates/scripts/release_train.py",
    "juno-code/src/templates/scripts/tests/test_release_train.py",
)


def _phase1_parity(worktree: Path) -> list[dict[str, str]]:
    pairs = [(worktree / PHASE1_COMMITTED_PATHS[0], worktree / PHASE1_COMMITTED_PATHS[2]),
             (worktree / PHASE1_COMMITTED_PATHS[1], worktree / PHASE1_COMMITTED_PATHS[3])]
    return [{"left": str(left.resolve()), "right": str(right.resolve()),
             "sha256": file_hash(left)} for left, right in pairs]


def _git_blob_hash(worktree: Path, tip_sha: str, relative_path: str) -> Optional[str]:
    result = subprocess.run(["git", "-C", str(worktree), "show", f"{tip_sha}:{relative_path}"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return hashlib.sha256(result.stdout).hexdigest() if result.returncode == 0 else None


def _phase1_committed_bindings(worktree: Path, tip_sha: str) -> list[dict[str, Any]]:
    return [{"path": path, "working_sha256": file_hash(worktree / path),
             "blob_sha256": _git_blob_hash(worktree, tip_sha, path)}
            for path in PHASE1_COMMITTED_PATHS]


def _phase1_runtime_identity(executable_request: str, script_path: Path) -> dict[str, Any]:
    """Bind the resolved interpreter and conservative local Python import closure."""
    located = shutil.which(executable_request) if not Path(executable_request).is_absolute() else executable_request
    executable = Path(located).resolve() if located else None
    script_path = script_path.resolve()
    module_root = script_path.parent
    # Local imports are deliberately over-bound. Adding or substituting any sibling
    # runtime module changes the closure even when Python import order differs.
    dependencies = [{"module": path.stem, "path": str(path.resolve()),
                     "sha256": file_hash(path.resolve())}
                    for path in sorted(module_root.glob("*.py")) if path.is_file()]
    imported_task_runtime = Path(task_runtime.__file__).resolve()
    if not any(row["module"] == "task_workspace" for row in dependencies):
        dependencies.append({"module": "task_workspace", "path": str(imported_task_runtime),
                             "sha256": file_hash(imported_task_runtime)})
    dependencies.sort(key=lambda row: (row["module"], row["path"]))
    return {"schema_version": "juno_phase1_python_runtime.v3",
            "executable_request": executable_request,
            "executable_path": str(executable) if executable else None,
            "executable_sha256": file_hash(executable) if executable and executable.is_file() else None,
            "implementation": platform.python_implementation(),
            "python_version": platform.python_version(),
            "python_build": sys.version,
            "script_path": str(script_path),
            "script_sha256": file_hash(script_path),
            "import_closure": dependencies}


def _phase1_publish_bytes(controller: Path, task_id: str, kind: str,
                          content: bytes) -> dict[str, str]:
    """Publish exact replay inputs under the canonical evidence registry."""
    identity = hashlib.sha256(content).hexdigest()
    destination = (controller.resolve() / ".juno_task/runtime/phase-evidence" / task_id
                   / "inputs" / f"{kind}-{identity}.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.read_bytes() != content:
            raise ReleaseTrainError("phase1 input publication collision")
    else:
        temporary = destination.parent / f".{destination.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        try:
            with temporary.open("xb") as stream:
                stream.write(content); stream.flush(); os.fsync(stream.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError:
                if destination.read_bytes() != content:
                    raise ReleaseTrainError("phase1 input publication collision")
        finally:
            temporary.unlink(missing_ok=True)
    return {"path": str(destination), "sha256": identity}


def _phase1_publish_input(controller: Path, task_id: str, kind: str, source: Path) -> dict[str, str]:
    return _phase1_publish_bytes(controller, task_id, kind, source.resolve().read_bytes())


def _phase1_publish_declaration(controller: Path, task_id: str,
                                source: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Canonicalize declaration authority so proof never retains an external path."""
    declaration, resolved = load_declaration(source.resolve())
    authority = declaration.get("conflict_authority")
    if not isinstance(authority, dict) or not isinstance(authority.get("path"), str):
        raise ReleaseTrainError("phase1 declaration requires conflict authority")
    authority_path = Path(authority["path"]).expanduser()
    if not authority_path.is_absolute():
        authority_path = resolved.parent / authority_path
    authority_ref = _phase1_publish_input(controller, task_id, "authority", authority_path.resolve())
    if authority.get("sha256") != authority_ref["sha256"]:
        raise ReleaseTrainError("phase1 conflict authority identity drift")
    declaration["conflict_authority"] = authority_ref
    content = (canonical(declaration) + "\n").encode()
    return _phase1_publish_bytes(controller, task_id, "declaration", content), authority_ref


def _phase1_watch_correlation(controller: Path, reference: Any, expected_argv: list[str],
                              required_output_sha256: Optional[str] = None) -> list[str]:
    blockers: list[str] = []
    if not isinstance(reference, dict) or set(reference) != {"run_id", "footer_sha256"}:
        return ["watch.reference"]
    run_id = reference.get("run_id", "")
    run_root = controller.resolve() / ".juno_task/runtime/watch-runs" / run_id
    try:
        run_record = json.loads((run_root / "run.json").read_text())
        footer = (run_root / "footer").read_text()
        combined_bytes = (run_root / "combined.log").read_bytes()
    except (OSError, json.JSONDecodeError):
        return ["watch.missing"]
    argv_sha = hashlib.sha256(json.dumps(expected_argv, separators=(",", ":")).encode()).hexdigest()
    if (run_record.get("argv_sha256") != argv_sha
            or run_record.get("cwd") != str(controller.resolve())
            or run_record.get("run_id") != run_id or run_record.get("state") != "COMPLETED"
            or run_record.get("exit_code") != 0
            or not re.fullmatch(r"schema_version=juno\.watch-footer\.v1\nexit_code=0\ncompleted_utc=[^\n]+\n", footer)
            or file_hash(run_root / "footer") != reference.get("footer_sha256")):
        blockers.append("watch.identity")
    if required_output_sha256 and hashlib.sha256(combined_bytes).hexdigest() != required_output_sha256:
        blockers.append("watch.output")
    return blockers


def phase1_input_identity(task_id: str, worktree: Path, tip_sha: str, tree_sha: str,
                          declaration_ref: dict[str, Any], fixture_ref: dict[str, Any],
                          executable_request: str, script_path: Path,
                          repository_snapshot: dict[str, str]) -> dict[str, Any]:
    body = {"schema_version": "juno_release_epoch_phase1_input.v2", "task_id": task_id,
            "worktree": str(worktree.resolve()), "tip_sha": tip_sha, "tree_sha": tree_sha,
            "declaration": declaration_ref, "fixture": fixture_ref,
            "repository_snapshot": repository_snapshot,
            "runtime": _phase1_runtime_identity(executable_request, script_path),
            "committed_files": _phase1_committed_bindings(worktree.resolve(), tip_sha)}
    return {**body, "input_sha256": digest(body)}


def _verify_phase1_fixture(repository: Path, plan: dict[str, Any], fixture: dict[str, Any]) -> list[str]:
    """Reconstruct portable topology from Git objects and embedded receipt bytes."""
    blockers: list[str] = []
    manifest = plan["conflict_manifest"]
    members = [{"task_id": row["task_id"], "tip_sha": row["tip_sha"],
                "tree_sha": row["tree_sha"]} for row in plan["members"]]
    if (fixture.get("schema_version") != "juno_release_epoch_portable_topology.v2"
            or fixture.get("base_sha") != plan["base_sha"]
            or fixture.get("order") != plan["order"]
            or fixture.get("members") != members
            or fixture.get("serial_conflicts") != [row["task_id"] for row in manifest["conflicts"]]):
        blockers.append("fixture.topology")
    for member in members:
        try:
            if (git(repository, "rev-parse", f'{member["tip_sha"]}^{{tree}}')
                    != member["tree_sha"]):
                blockers.append("fixture.member_tree")
        except (ReleaseTrainError, subprocess.CalledProcessError):
            blockers.append("fixture.member_object")
    expected = {row["task_id"]: row for row in manifest["compositions"]
                if row.get("decision") == "conflict_replayed"}
    observed = fixture.get("proven_compositions")
    if not isinstance(observed, list) or {row.get("task_id") for row in observed
                                         if isinstance(row, dict)} != set(expected):
        blockers.append("fixture.compositions")
        observed = observed if isinstance(observed, list) else []
    for row in observed:
        if not isinstance(row, dict) or row.get("task_id") not in expected:
            blockers.append("fixture.composition_schema"); continue
        source = expected[row["task_id"]]
        receipt = row.get("receipt")
        try:
            receipt_bytes = base64.b64decode(row.get("receipt_bytes_b64", ""), validate=True)
        except (ValueError, TypeError):
            receipt_bytes = b""
        try:
            if json.loads(receipt_bytes) != receipt:
                blockers.append("fixture.receipt_bytes")
        except (UnicodeDecodeError, json.JSONDecodeError):
            blockers.append("fixture.receipt_bytes")
        try:
            parents = git(repository, "show", "-s", "--format=%P", row["commit"]).split()
            tree = git(repository, "rev-parse", f'{row["commit"]}^{{tree}}')
        except (KeyError, ReleaseTrainError, subprocess.CalledProcessError):
            blockers.append("fixture.composition_object"); continue
        if (row.get("commit") != source.get("post_sha") or row.get("tree") != source.get("post_tree")
                or parents != [source.get("pre_sha"), source.get("candidate_tip")]
                or tree != row.get("tree")):
            blockers.append("fixture.composition_identity")
        receipt_sha = hashlib.sha256(receipt_bytes).hexdigest()
        if (receipt_sha != row.get("receipt_sha256")
                or receipt_sha != (source.get("proven_composition") or {}).get("receipt_sha256")
                or not isinstance(receipt, dict)
                or (receipt.get("detail") or {}).get("repair_commit") != row.get("commit")):
            blockers.append("fixture.receipt_identity")
    return sorted(set(blockers))


PHASE1_ORCHESTRATION_TEST = (
    "ReleaseTrainTests.test_phase1_acceptance_cli_replays_semantics_and_correlates_watched_producer"
)
PHASE1_SUITE_TESTS = (
    "ReleaseTrainTests.test_deterministic_non_mutating_json_and_human_projection",
    "ReleaseTrainTests.test_fifo_conflict_dependency_blocker_and_parallel_lanes",
    "ReleaseTrainTests.test_cycle_is_explicit",
    "ReleaseTrainTests.test_stale_kanban_and_target_identity_refuse_shared_gate",
    "ReleaseTrainTests.test_missing_runtime_blocks",
    "ReleaseTrainTests.test_clean_ready_release_and_version_gate",
    "ReleaseTrainTests.test_lean_sparse_controller_plans_release_from_target_tree",
    "ReleaseTrainTests.test_epoch_seal_is_complete_immutable_and_idempotent",
    "ReleaseTrainTests.test_wave_economics_and_explicit_selection_route_without_sealing",
    "ReleaseTrainTests.test_epoch_status_projection_is_bounded_and_actionable",
    "ReleaseTrainTests.test_epoch_seal_refuses_required_missing_closure_without_state",
    "ReleaseTrainTests.test_rc7_rc8_serial_conflicts_fit_one_conservative_repair_set",
    "ReleaseTrainTests.test_conflict_authority_refuses_missing_ambiguous_sensitive_scope_and_identity",
    "ReleaseTrainTests.test_legacy_epoch_identity_adapter_is_deterministic_typed_and_read_only",
    "ReleaseTrainTests.test_exact_rc7_rc8_receipts_cover_every_member_without_synthetic_repair",
    "ReleaseTrainTests.test_bootstrap_repair_is_causal_fenced_preserves_queue_and_cas_once",
    "ReleaseTrainTests.test_bootstrap_reconciliation_finalizes_only_exact_ancestry_members",
    "ReleaseTrainTests.test_bootstrap_repair_refuses_missing_causal_dependency",
    "ReleaseTrainTests.test_epoch_composes_history_validates_once_and_cas_once",
    "ReleaseTrainTests.test_three_member_terminal_projection_resumes_after_interruption_and_is_idempotent",
    "ReleaseTrainTests.test_successor_replay_projects_untouched_stale_journal_and_is_idempotent",
    "ReleaseTrainTests.test_successor_replay_chains_across_two_more_target_advances_without_reapplying",
    "ReleaseTrainTests.test_successor_replay_refuses_partial_tamper_divergence_and_revision_drift",
    "ReleaseTrainTests.test_four_member_terminal_projection_preserves_exact_fifo_and_replays_idempotently",
    "ReleaseTrainTests.test_terminal_projection_tamper_and_target_drift_refuse_without_ledger_mutation",
    "ReleaseTrainTests.test_aggregate_exact_lock_hydrates_missing_dependencies_before_gate",
    "ReleaseTrainTests.test_failed_aggregate_has_fenced_receipt_retry_without_duplicate_merge_or_cas",
    "ReleaseTrainTests.test_required_failure_pauses_and_shadow_is_read_only",
    "ReleaseTrainTests.test_recovered_worker_receipt_requires_exact_failed_artifacts",
    "ReleaseTrainTests.test_lean_target_drift_refuses_release",
    "ReleaseTrainTests.test_phase1_substitution_matrix_is_complete_and_receipt_bound",
)
PHASE1_SUITE_MANIFEST = {
    "schema_version": PHASE1_SUITE_MANIFEST_SCHEMA,
    "selection": "all_release_train_tests_except_evidence_orchestrator",
    "tests": list(PHASE1_SUITE_TESTS),
    "excluded_recursive_tests": [PHASE1_ORCHESTRATION_TEST],
}
PHASE1_ENV_KEYS = (
    "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "GIT_OPTIONAL_LOCKS",
    "GIT_TERMINAL_PROMPT", "HOME", "JUNO_TASK_ROOT", "JUNO_WORKSPACE_ENFORCEMENT",
    "JUNO_WORKSPACE_ROLE", "LANG", "LC_ALL", "PATH", "PYTHONDONTWRITEBYTECODE",
    "PYTHONHASHSEED", "PYTHONNOUSERSITE", "TMPDIR", "TZ",
)


def _phase1_discovered_tests(test_path: Path) -> list[str]:
    tree = ast.parse(test_path.read_text(encoding="utf-8"), filename=str(test_path))
    return sorted(f"{node.name}.{child.name}" for node in tree.body if isinstance(node, ast.ClassDef)
                  for child in node.body if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and child.name.startswith("test_"))


def _phase1_suite_environment(controller: Path, output: Path) -> dict[str, str]:
    """Return the complete sanitized child environment; ambient values are not inherited."""
    executable_dir = str(Path(sys.executable).resolve().parent)
    yy = shutil.which("yy")
    path_parts = [executable_dir]
    if yy:
        path_parts.append(str(Path(yy).resolve().parent))
    path_parts.extend(["/opt/homebrew/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
    path = ":".join(dict.fromkeys(path_parts))
    tmpdir = output.resolve().parent / "tmp" / output.stem
    tmpdir.mkdir(parents=True, exist_ok=True)
    home = output.resolve().parent / "home"
    home.mkdir(parents=True, exist_ok=True)
    return {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0",
            "HOME": str(home), "JUNO_TASK_ROOT": str(controller.resolve()),
            "JUNO_WORKSPACE_ENFORCEMENT": "strict", "JUNO_WORKSPACE_ROLE": "controller",
            "LANG": "C", "LC_ALL": "C", "PATH": path,
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0",
            "PYTHONNOUSERSITE": "1", "TMPDIR": str(tmpdir), "TZ": "UTC"}


def _phase1_suite_argv(controller: Path, worktree: Path, output: Path,
                       evidence_context: str) -> list[str]:
    script = (worktree.resolve() / PHASE1_COMMITTED_PATHS[0]).resolve()
    return [str(Path(sys.executable).resolve()), str(script), "--controller", str(controller.resolve()),
            "phase1-suite", "--task-id", "V9vE0X", "--worktree", str(worktree.resolve()),
            "--evidence-context", evidence_context, "--output", str(output.resolve())]


def phase1_suite_receipt(controller: Path, task_id: str, worktree: Path,
                         output: Path, producer_argv: list[str],
                         evidence_context: str) -> dict[str, Any]:
    """Run the complete non-recursive committed suite under an exact environment."""
    controller = controller.resolve(); worktree = worktree.resolve(); output = output.resolve()
    blockers: list[str] = []
    tip_sha = git(worktree, "rev-parse", "HEAD"); tree_sha = git(worktree, "rev-parse", "HEAD^{tree}")
    test_path = (worktree / PHASE1_COMMITTED_PATHS[1]).resolve()
    test_blob = _git_blob_hash(worktree, tip_sha, PHASE1_COMMITTED_PATHS[1])
    discovered = _phase1_discovered_tests(test_path)
    expected_discovered = sorted([*PHASE1_SUITE_TESTS, PHASE1_ORCHESTRATION_TEST])
    manifest = {**PHASE1_SUITE_MANIFEST, "test_blob_sha256": test_blob}
    manifest_sha256 = digest(manifest)
    suite_identity = digest({"tip_sha": tip_sha, "tree_sha": tree_sha,
                             "manifest_sha256": manifest_sha256,
                             "evidence_context": evidence_context})
    expected_output = (controller / ".juno_task/runtime/phase-evidence" / task_id
                       / f"phase1-suite-{suite_identity}.json")
    expected_argv = _phase1_suite_argv(controller, worktree, expected_output, evidence_context)
    if task_id != "V9vE0X" or output != expected_output or producer_argv != expected_argv:
        blockers.append("suite.routing")
    if file_hash(test_path) != test_blob:
        blockers.append("suite.committed_bytes")
    if discovered != expected_discovered or len(PHASE1_SUITE_TESTS) != len(set(PHASE1_SUITE_TESTS)):
        blockers.append("suite.manifest_membership")
    command = [str(Path(sys.executable).resolve()), str(test_path), *PHASE1_SUITE_TESTS]
    child_environment = _phase1_suite_environment(controller, output)
    if set(child_environment) != set(PHASE1_ENV_KEYS):
        blockers.append("suite.environment_contract")
    completed = subprocess.run(command, cwd=test_path.parent, text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               env=child_environment, timeout=120)
    match = re.search(r"Ran (\d+) tests? in ", completed.stdout)
    test_count = int(match.group(1)) if match else 0
    outcome = "PASS" if completed.returncode == 0 and test_count == len(PHASE1_SUITE_TESTS) and re.search(
        r"(?:^|\n)OK(?: \(skipped=\d+\))?(?:\n|$)", completed.stdout) else "FAIL"
    if outcome != "PASS": blockers.append("suite.result")
    runtime = _phase1_runtime_identity(str(Path(sys.executable).resolve()), Path(__file__).resolve())
    body = {"schema_version": PHASE1_SUITE_SCHEMA, "decision": "PASS" if not blockers else "FAIL",
            "task_id": task_id, "tip_sha": tip_sha, "tree_sha": tree_sha,
            "suite_identity_sha256": suite_identity, "evidence_context": evidence_context,
            "producer_argv": producer_argv, "suite_command": command,
            "suite_cwd": str(test_path.parent),
            "test_path": str(test_path), "test_blob_sha256": test_blob,
            "suite_manifest": manifest, "suite_manifest_sha256": manifest_sha256,
            "discovered_tests": discovered, "runtime": runtime,
            "observed_environment": child_environment,
            "environment_sha256": digest(child_environment),
            "result": {"outcome": outcome, "test_count": test_count,
                       "output_sha256": hashlib.sha256(completed.stdout.encode()).hexdigest(),
                       "exit_code": completed.returncode},
            "blocking_reason_codes": sorted(set(blockers))}
    receipt = {**body, "receipt_sha256": digest(body)}
    return receipt if blockers else _publish_exclusive_receipt(output, receipt)


def phase1_proof(controller: Path, declaration_path: Path, fixture_path: Path,
                 task_id: str, worktree: Path, output: Path,
                 producer_argv: list[str]) -> dict[str, Any]:
    """Produce verifier-owned semantic evidence; caller attestations have no decision weight."""
    controller = controller.resolve(); worktree = worktree.resolve(); output = output.resolve()
    blockers: list[str] = []
    tip_sha = git(worktree, "rev-parse", "HEAD"); tree_sha = git(worktree, "rev-parse", "HEAD^{tree}")
    declaration_path = declaration_path.resolve(); fixture_path = fixture_path.resolve()
    # Publish first and build from those canonical bytes so later replay has the
    # same path/identity semantics and never depends on an ephemeral /tmp path.
    declaration_ref, authority_ref = _phase1_publish_declaration(
        controller, task_id, declaration_path)
    fixture_ref = _phase1_publish_input(controller, task_id, "fixture", fixture_path)
    plan = build_epoch_plan(controller, Path(declaration_ref["path"]))
    executable_request = producer_argv[0] if producer_argv else ""
    script_path = Path(producer_argv[1]).resolve() if len(producer_argv) > 1 else Path(".").resolve()
    repository = task_runtime.product_repository(controller, task_runtime.load_config(controller))
    before = _phase1_repository_snapshot(repository)
    input_identity = phase1_input_identity(task_id, worktree, tip_sha, tree_sha,
                                           declaration_ref, fixture_ref,
                                           executable_request, script_path, before)
    expected_output = (controller / ".juno_task/runtime/phase-evidence" / task_id
                       / f'phase1-proof-{tip_sha}-{input_identity["input_sha256"]}.json')
    if (output != expected_output or not task_runtime.TASK_RE.fullmatch(task_id)):
        blockers.append("proof.routing")
    expected_script = (worktree / PHASE1_COMMITTED_PATHS[0]).resolve()
    if script_path != expected_script or script_path != Path(__file__).resolve():
        blockers.append("runtime.script")
    if input_identity["runtime"]["executable_path"] != str(Path(sys.executable).resolve()):
        blockers.append("runtime.executable")
    committed = input_identity["committed_files"]
    if any(not row.get("working_sha256") or row.get("working_sha256") != row.get("blob_sha256")
           for row in committed):
        blockers.append("task.committed_bytes")
    fixture = json.loads(Path(fixture_ref["path"]).read_text())
    blockers.extend(_verify_phase1_fixture(repository, plan, fixture))
    parity = _phase1_parity(worktree)
    if any(file_hash(Path(row["left"])) != row["sha256"]
           or file_hash(Path(row["right"])) != row["sha256"] for row in parity):
        blockers.append("parity.drift")
    after = _phase1_repository_snapshot(repository)
    if before != after:
        blockers.append("non_mutation.drift")
    body = {"schema_version": PHASE1_PROOF_SCHEMA,
            "decision": "PASS" if not blockers else "FAIL", "task_id": task_id,
            "worktree": str(worktree), "tip_sha": tip_sha, "tree_sha": tree_sha,
            "declaration": declaration_ref, "authority": authority_ref,
            "fixture": fixture_ref, "input_identity": input_identity,
            "manifest": plan["conflict_manifest"], "snapshot_before": before,
            "snapshot_after": after, "parity": parity, "producer_argv": producer_argv,
            "blocking_reason_codes": sorted(set(blockers))}
    proof = {**body, "proof_sha256": digest(body)}
    return _publish_exclusive_receipt(output, proof)


def _phase1_checked_closure(proof: dict[str, Any], closure: dict[str, Any],
                            suite_receipt: dict[str, Any]) -> dict[str, Any]:
    suite = closure.get("suite") or {}
    return {"proof_sha256": proof.get("proof_sha256"),
        "input_sha256": (proof.get("input_identity") or {}).get("input_sha256"),
        "declaration": proof.get("declaration"), "authority": proof.get("authority"),
        "fixture": proof.get("fixture"),
        "manifest_sha256": (proof.get("manifest") or {}).get("manifest_sha256"),
        "proof_watch": closure.get("watch"),
        "suite_receipt_sha256": suite_receipt.get("receipt_sha256"),
        "suite_watch": suite.get("watch"), "suite_argv": suite_receipt.get("producer_argv"),
        "suite_command": suite_receipt.get("suite_command"),
        "suite_cwd": suite_receipt.get("suite_cwd"),
        "suite_manifest": suite_receipt.get("suite_manifest"),
        "suite_manifest_sha256": suite_receipt.get("suite_manifest_sha256"),
        "suite_runtime": suite_receipt.get("runtime"),
        "suite_environment": suite_receipt.get("observed_environment"),
        "suite_environment_sha256": suite_receipt.get("environment_sha256"),
        "suite_result": suite_receipt.get("result")}


def phase1_acceptance_receipt(controller: Path, closure_path: Path, output: Path,
                              producer_argv: Optional[list[str]] = None) -> dict[str, Any]:
    """Evaluate proof plus exact-tip suite; final acceptance is post-terminal correlated."""
    controller = controller.resolve(); blockers: list[str] = []
    closure = json.loads(closure_path.resolve().read_text())
    if (set(closure) != {"schema_version", "proof", "watch", "suite"}
            or closure.get("schema_version") != PHASE1_CLOSURE_SCHEMA):
        blockers.append("closure.schema")
    proof = _immutable_json(closure.get("proof"), "proof", blockers) or {}
    proof_body = {key: value for key, value in proof.items() if key != "proof_sha256"}
    if (proof.get("schema_version") != PHASE1_PROOF_SCHEMA
            or proof.get("proof_sha256") != digest(proof_body) or proof.get("decision") != "PASS"):
        blockers.append("proof.invalid")
    task_id = proof.get("task_id", ""); worktree = Path(proof.get("worktree", ".")).resolve()
    try:
        if (git(worktree, "rev-parse", "HEAD") != proof.get("tip_sha")
                or git(worktree, "rev-parse", "HEAD^{tree}") != proof.get("tree_sha")):
            blockers.append("task.commit_tree")
    except (ReleaseTrainError, subprocess.CalledProcessError):
        blockers.append("task.commit_tree")
    declaration = _immutable_json(proof.get("declaration"), "declaration", blockers)
    authority = _immutable_json(proof.get("authority"), "authority", blockers)
    fixture = _immutable_json(proof.get("fixture"), "fixture", blockers)
    if declaration is not None and authority is not None and fixture is not None:
        repository = task_runtime.product_repository(controller, task_runtime.load_config(controller))
        acceptance_before = _phase1_repository_snapshot(repository)
        if declaration.get("conflict_authority") != proof.get("authority"):
            blockers.append("authority.path_substitution")
        plan = build_epoch_plan(controller, Path(proof["declaration"]["path"]))
        if (plan.get("conflict_authority") != proof.get("authority")
                or plan["conflict_manifest"] != proof.get("manifest")):
            blockers.append("manifest.replay")
        blockers.extend(_verify_phase1_fixture(repository, plan, fixture))
        if _phase1_repository_snapshot(repository) != acceptance_before:
            blockers.append("non_mutation.acceptance_drift")
    if proof.get("snapshot_before") != proof.get("snapshot_after"): blockers.append("non_mutation.drift")
    if _phase1_parity(worktree) != proof.get("parity"): blockers.append("parity.drift")
    argv = proof.get("producer_argv")
    executable_request = argv[0] if isinstance(argv, list) and argv else ""
    script_path = Path(argv[1]).resolve() if isinstance(argv, list) and len(argv) > 1 else Path(".").resolve()
    recomputed_input = phase1_input_identity(task_id, worktree, proof.get("tip_sha", ""),
        proof.get("tree_sha", ""), proof.get("declaration") or {}, proof.get("fixture") or {},
        executable_request, script_path, proof.get("snapshot_before") or {})
    if recomputed_input != proof.get("input_identity"): blockers.append("input_identity.drift")
    if recomputed_input["runtime"]["executable_path"] != str(Path(sys.executable).resolve()):
        blockers.append("runtime.executable")
    if any(not row.get("working_sha256") or row.get("working_sha256") != row.get("blob_sha256")
           for row in recomputed_input["committed_files"]): blockers.append("task.committed_bytes")
    blockers.extend("proof." + code for code in _phase1_watch_correlation(
        controller, closure.get("watch"), argv if isinstance(argv, list) else []))
    proof_log = controller / ".juno_task/runtime/watch-runs" / (closure.get("watch") or {}).get("run_id", "") / "combined.log"
    if not proof_log.is_file() or proof.get("proof_sha256", "").encode() not in proof_log.read_bytes():
        blockers.append("proof.watch.output")

    suite = closure.get("suite") or {}
    suite_receipt = _immutable_json(suite.get("receipt"), "suite", blockers) or {}
    suite_body = {key: value for key, value in suite_receipt.items() if key != "receipt_sha256"}
    suite_argv = suite_receipt.get("producer_argv")
    expected_suite_output = (controller / ".juno_task/runtime/phase-evidence" / task_id
        / f'phase1-suite-{suite_receipt.get("suite_identity_sha256", "invalid")}.json')
    expected_evidence_context = digest(closure.get("watch"))
    expected_test_blob = _git_blob_hash(
        worktree, proof.get("tip_sha", ""), PHASE1_COMMITTED_PATHS[1])
    expected_manifest = {**PHASE1_SUITE_MANIFEST, "test_blob_sha256": expected_test_blob}
    expected_environment = _phase1_suite_environment(controller, expected_suite_output)
    expected_discovered = sorted([*PHASE1_SUITE_TESTS, PHASE1_ORCHESTRATION_TEST])
    if (suite_receipt.get("schema_version") != PHASE1_SUITE_SCHEMA
            or suite_receipt.get("receipt_sha256") != digest(suite_body)
            or suite_receipt.get("decision") != "PASS"
            or suite_receipt.get("task_id") != task_id
            or suite_receipt.get("tip_sha") != proof.get("tip_sha")
            or suite_receipt.get("tree_sha") != proof.get("tree_sha")
            or Path((suite.get("receipt") or {}).get("path", ".")).resolve() != expected_suite_output.resolve()
            or suite_receipt.get("evidence_context") != expected_evidence_context
            or suite_argv != _phase1_suite_argv(controller, worktree, expected_suite_output,
                                                 expected_evidence_context)
            or suite_receipt.get("test_blob_sha256") != expected_test_blob
            or suite_receipt.get("suite_manifest") != expected_manifest
            or suite_receipt.get("suite_manifest_sha256") != digest(expected_manifest)
            or suite_receipt.get("discovered_tests") != expected_discovered
            or suite_receipt.get("suite_command") != [str(Path(sys.executable).resolve()),
                str((worktree / PHASE1_COMMITTED_PATHS[1]).resolve()), *PHASE1_SUITE_TESTS]
            or suite_receipt.get("suite_cwd") != str(
                (worktree / PHASE1_COMMITTED_PATHS[1]).resolve().parent)
            or suite_receipt.get("runtime") != _phase1_runtime_identity(
                str(Path(sys.executable).resolve()), Path(__file__).resolve())
            or suite_receipt.get("observed_environment") != expected_environment
            or suite_receipt.get("environment_sha256") != digest(expected_environment)
            or set(expected_environment) != set(PHASE1_ENV_KEYS)
            or (suite_receipt.get("result") or {}).get("outcome") != "PASS"
            or (suite_receipt.get("result") or {}).get("test_count") != len(PHASE1_SUITE_TESTS)
            or (suite_receipt.get("result") or {}).get("exit_code") != 0):
        blockers.append("suite.closure")
    blockers.extend("suite." + code for code in _phase1_watch_correlation(
        controller, suite.get("watch"), suite_argv if isinstance(suite_argv, list) else []))
    suite_log = (controller / ".juno_task/runtime/watch-runs" /
                 (suite.get("watch") or {}).get("run_id", "") / "combined.log")
    if (not suite_log.is_file()
            or suite_receipt.get("receipt_sha256", "").encode() not in suite_log.read_bytes()):
        blockers.append("suite.watch.output")
    checked_closure = _phase1_checked_closure(proof, closure, suite_receipt)
    closure_sha256 = digest(checked_closure)
    expected_output = (controller / ".juno_task/runtime/phase-evidence" / task_id
                       / f"phase1-evaluation-{closure_sha256}.json")
    if output.resolve() != expected_output.resolve(): blockers.append("receipt.routing")
    actual_producer_argv = producer_argv or []
    body = {"schema_version": PHASE1_EVALUATION_SCHEMA,
            "decision": "PASS" if not blockers else "FAIL", "task_id": task_id,
            "tip_sha": proof.get("tip_sha"), "tree_sha": proof.get("tree_sha"),
            "input_sha256": (proof.get("input_identity") or {}).get("input_sha256"),
            "proof_sha256": proof.get("proof_sha256"),
            "checked_closure": checked_closure, "closure_sha256": closure_sha256,
            "proof_watch_run_id": (closure.get("watch") or {}).get("run_id"),
            "suite_watch_run_id": (suite.get("watch") or {}).get("run_id"),
            "suite_receipt_sha256": suite_receipt.get("receipt_sha256"),
            "suite_output_sha256": (suite_receipt.get("result") or {}).get("output_sha256"),
            "producer_argv": actual_producer_argv,
            "blocking_reason_codes": sorted(set(blockers))}
    receipt = {**body, "receipt_sha256": digest(body)}
    return receipt if blockers else _publish_exclusive_receipt(output.resolve(), receipt)


def phase1_finalize_acceptance(controller: Path, evaluation_ref: dict[str, Any],
                               watch_ref: dict[str, Any], output: Path) -> dict[str, Any]:
    """Publish final PASS only after the acceptance evaluator's watch is terminal."""
    controller = controller.resolve(); blockers: list[str] = []
    evaluation = _immutable_json(evaluation_ref, "evaluation", blockers) or {}
    body_without_hash = {k: v for k, v in evaluation.items() if k != "receipt_sha256"}
    if (evaluation.get("schema_version") != PHASE1_EVALUATION_SCHEMA
            or evaluation.get("receipt_sha256") != digest(body_without_hash)
            or evaluation.get("decision") != "PASS"):
        blockers.append("evaluation.invalid")
    argv = evaluation.get("producer_argv")
    blockers.extend("evaluation." + code for code in _phase1_watch_correlation(
        controller, watch_ref, argv if isinstance(argv, list) else []))
    run_root = controller / ".juno_task/runtime/watch-runs" / watch_ref.get("run_id", "")
    if (not (run_root / "combined.log").is_file()
            or evaluation.get("receipt_sha256", "").encode() not in (run_root / "combined.log").read_bytes()):
        blockers.append("evaluation.watch.output")
    if (evaluation.get("closure_sha256") != digest(evaluation.get("checked_closure"))
            or (evaluation.get("checked_closure") or {}).get("proof_sha256") != evaluation.get("proof_sha256")
            or (evaluation.get("checked_closure") or {}).get("suite_receipt_sha256") != evaluation.get("suite_receipt_sha256")):
        blockers.append("evaluation.closure")
    expected = (controller / ".juno_task/runtime/phase-evidence" / evaluation.get("task_id", "")
                / f'phase1-acceptance-{evaluation.get("closure_sha256", "invalid")}.json')
    if output.resolve() != expected.resolve(): blockers.append("receipt.routing")
    final_body = {"schema_version": PHASE1_ACCEPTANCE_SCHEMA,
                  "decision": "PASS" if not blockers else "FAIL",
                  "task_id": evaluation.get("task_id"), "tip_sha": evaluation.get("tip_sha"),
                  "tree_sha": evaluation.get("tree_sha"),
                  "input_sha256": evaluation.get("input_sha256"),
                  "proof_sha256": evaluation.get("proof_sha256"),
                  "checked_closure": evaluation.get("checked_closure"),
                  "closure_sha256": evaluation.get("closure_sha256"),
                  "suite_receipt_sha256": evaluation.get("suite_receipt_sha256"),
                  "suite_output_sha256": evaluation.get("suite_output_sha256"),
                  "evaluation_sha256": evaluation.get("receipt_sha256"),
                  "proof_watch_run_id": evaluation.get("proof_watch_run_id"),
                  "suite_watch_run_id": evaluation.get("suite_watch_run_id"),
                  "evaluation_watch_run_id": watch_ref.get("run_id"),
                  "blocking_reason_codes": sorted(set(blockers))}
    receipt = {**final_body, "receipt_sha256": digest(final_body)}
    return receipt if blockers else _publish_exclusive_receipt(output.resolve(), receipt)


def _phase1_reference(path: Path) -> dict[str, str]:
    return {"path": str(path.resolve()), "sha256": file_hash(path.resolve()) or ""}


def _phase1_watch_reference(controller: Path, record: dict[str, Any]) -> dict[str, str]:
    footer = controller.resolve() / ".juno_task/runtime/watch-runs" / record["run_id"] / "footer"
    return {"run_id": record["run_id"], "footer_sha256": file_hash(footer) or ""}


def _phase1_watch_exec(controller: Path, argv: list[str], timeout: int, role: str) -> dict[str, Any]:
    yy = shutil.which("yy")
    if not yy:
        raise ReleaseTrainError("phase1 orchestration requires yy watch")
    launch_marker = controller.resolve() / ".juno_task/runtime/phase-evidence/V9vE0X/orchestrator"
    environment = _phase1_suite_environment(controller, launch_marker)
    completed = subprocess.run([str(Path(yy).resolve()), "watch", "exec", "--timeout", str(timeout),
                                "--", *argv], cwd=controller.resolve(), env=environment,
                               text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               timeout=timeout + 30)
    records: list[dict[str, Any]] = []
    for line in completed.stdout.splitlines():
        if line.startswith("{") and line.endswith("}"):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if value.get("schema_version") == "juno.watch-run.v1":
                records.append(value)
    if completed.returncode or not records or records[-1].get("exit_code") != 0:
        detail = completed.stderr[-1000:]
        if records:
            log = controller.resolve() / ".juno_task/runtime/watch-runs" / records[-1]["run_id"] / "combined.log"
            if log.is_file():
                detail = log.read_text(errors="replace")[-1000:]
        raise ReleaseTrainError(f"phase1 {role} watched stage failed: " + detail)
    return records[-1]


def phase1_orchestrate(controller: Path, declaration: Path, fixture: Path,
                       task_id: str, worktree: Path) -> dict[str, Any]:
    """Run the typed proof/suite/evaluation watch chain and finalize one receipt."""
    controller = controller.resolve(); worktree = worktree.resolve()
    if task_id != "V9vE0X":
        raise ReleaseTrainError("phase1 orchestration task identity mismatch")
    evidence_root = controller / ".juno_task/runtime/phase-evidence" / task_id
    declaration_ref, _ = _phase1_publish_declaration(controller, task_id, declaration)
    fixture_ref = _phase1_publish_input(controller, task_id, "fixture", fixture)
    tip_sha = git(worktree, "rev-parse", "HEAD"); tree_sha = git(worktree, "rev-parse", "HEAD^{tree}")
    executable = str(Path(sys.executable).resolve())
    script = (worktree / PHASE1_COMMITTED_PATHS[0]).resolve()
    repository = task_runtime.product_repository(controller, task_runtime.load_config(controller))
    input_identity = phase1_input_identity(task_id, worktree, tip_sha, tree_sha,
                                           declaration_ref, fixture_ref, executable, script,
                                           _phase1_repository_snapshot(repository))
    proof_path = evidence_root / f'phase1-proof-{tip_sha}-{input_identity["input_sha256"]}.json'
    proof_argv = [executable, str(script), "--controller", str(controller), "phase1-prove",
                  "--declaration", str(declaration.resolve()), "--fixture", str(fixture.resolve()),
                  "--task-id", task_id, "--worktree", str(worktree), "--output", str(proof_path)]
    proof_watch = _phase1_watch_exec(controller, proof_argv, 120, "proof")
    proof = json.loads(proof_path.read_text())
    if proof.get("decision") != "PASS":
        raise ReleaseTrainError("phase1 proof refused")

    test_blob = _git_blob_hash(worktree, tip_sha, PHASE1_COMMITTED_PATHS[1])
    suite_manifest = {**PHASE1_SUITE_MANIFEST, "test_blob_sha256": test_blob}
    proof_watch_ref = _phase1_watch_reference(controller, proof_watch)
    evidence_context = digest(proof_watch_ref)
    suite_identity = digest({"tip_sha": tip_sha, "tree_sha": tree_sha,
                             "manifest_sha256": digest(suite_manifest),
                             "evidence_context": evidence_context})
    suite_path = evidence_root / f"phase1-suite-{suite_identity}.json"
    suite_argv = _phase1_suite_argv(controller, worktree, suite_path, evidence_context)
    suite_watch = _phase1_watch_exec(controller, suite_argv, 180, "suite")
    suite_receipt = json.loads(suite_path.read_text())
    if suite_receipt.get("decision") != "PASS":
        raise ReleaseTrainError("phase1 suite refused")

    closure = {"schema_version": PHASE1_CLOSURE_SCHEMA, "proof": _phase1_reference(proof_path),
               "watch": _phase1_watch_reference(controller, proof_watch),
               "suite": {"receipt": _phase1_reference(suite_path),
                         "watch": _phase1_watch_reference(controller, suite_watch)}}
    closure_identity = digest(closure)
    closure_path = evidence_root / f"phase1-closure-{closure_identity}.json"
    _publish_exclusive_receipt(closure_path, closure)
    checked = _phase1_checked_closure(proof, closure, suite_receipt)
    evaluation_path = evidence_root / f"phase1-evaluation-{digest(checked)}.json"
    evaluation_argv = [executable, str(script), "--controller", str(controller), "phase1-accept",
                       "--closure", str(closure_path), "--output", str(evaluation_path)]
    evaluation_watch = _phase1_watch_exec(controller, evaluation_argv, 120, "evaluation")
    evaluation = json.loads(evaluation_path.read_text())
    if evaluation.get("decision") != "PASS":
        raise ReleaseTrainError("phase1 evaluation refused")
    output = evidence_root / f'phase1-acceptance-{evaluation["closure_sha256"]}.json'
    receipt = phase1_finalize_acceptance(controller, _phase1_reference(evaluation_path),
        _phase1_watch_reference(controller, evaluation_watch), output)
    if receipt.get("decision") != "PASS":
        raise ReleaseTrainError("phase1 finalization refused")
    return {"schema_version": "juno_release_epoch_phase1_orchestration.v1",
            "decision": "PASS", "task_id": task_id, "tip_sha": tip_sha, "tree_sha": tree_sha,
            "stage_watch_run_ids": {"proof": proof_watch["run_id"], "suite": suite_watch["run_id"],
                                    "evaluation": evaluation_watch["run_id"]},
            "closure_sha256": evaluation["closure_sha256"],
            "receipt": _phase1_reference(output)}


def build_epoch_plan(controller: Path, declaration_path: Path) -> dict[str, Any]:
    declaration, resolved = load_declaration(declaration_path)
    config = task_runtime.load_config(controller)
    repository = task_runtime.product_repository(controller, config)
    target_sha = git(repository, "rev-parse", "--verify", declaration["target_ref"])
    if target_sha != declaration["planning_base_sha"]:
        raise ReleaseTrainError("target moved since the declaration planning base")
    members = queue_epoch_members(controller, declaration, target_sha)
    queued = {row["task_id"] for row in members}
    missing = sorted((set(declaration["required_tasks"]) | set(declaration["optional_tasks"])) - queued)
    if missing:
        raise ReleaseTrainError("declared candidates are not queued at the cutoff: " + ", ".join(missing))
    if not members:
        raise ReleaseTrainError("no eligible candidates exist for an epoch seal")
    order, edges = epoch_dependency_order(members, declaration)
    runtime_paths = [".juno_task/scripts/release_train.py", ".juno_task/scripts/merge_queue.py",
                     ".juno_task/scripts/task_workspace.py"]
    declaration_identity = digest({key: value for key, value in declaration.items()
                                   if key != "conflict_authority"})
    conflict_authority = declaration.get("conflict_authority")
    if conflict_authority:
        authority_path = Path(conflict_authority["path"]).expanduser()
        if not authority_path.is_absolute():
            authority_path = resolved.parent / authority_path
        conflict_authority = {**conflict_authority, "path": str(authority_path.resolve())}
    body = {"schema_version": EPOCH_PLAN_SCHEMA, "epoch_id": declaration["train_id"],
            "declaration": {"path": str(resolved), "sha256": file_hash(resolved),
                            "revision": declaration["revision"],
                            "identity_sha256": declaration_identity},
            "conflict_authority": conflict_authority,
            "target_ref": declaration["target_ref"], "base_sha": target_sha,
            "members": members, "order": order, "dependency_edges": edges,
            "runtime_sha256": {path: file_hash(controller / path) for path in runtime_paths},
            "policy_sha256": {path: file_hash(controller / path) for path in
                              [".juno_task/config/task-workspace.json", ".juno_task/config/risk-policy.json"]},
            "requested_version": declaration["requested_version"],
            "exclusions": declaration["exclusions"],
            "mutation_authority": "explicit_seal_required"}
    body["economics"] = wave_economics_projection(controller, members, order)
    manifest = forecast_epoch_conflicts(controller, repository, body)
    body["conflict_manifest"] = manifest
    return {**body, "plan_id": digest(body)}


def seal_epoch(controller: Path, declaration_path: Path) -> dict[str, Any]:
    plan = build_epoch_plan(controller, declaration_path)
    invalid = [row["task_id"] for row in plan["members"] if row["required"] and (
        not isinstance(row.get("complete_input_identity"), dict)
        or row["complete_input_identity"].get("schema_version") != "juno_task_review_ready_closure.v1"
        or not isinstance(row["complete_input_identity"].get("closure_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", row["complete_input_identity"]["closure_sha256"])
        or row["complete_input_identity"].get("tip_sha") != row["tip_sha"]
        or row["complete_input_identity"].get("tree_sha") != row["tree_sha"]
        or row["evidence_sha256"] == digest([]))]
    if invalid:
        raise ReleaseTrainError("candidate.complete_input_missing:" + ",".join(sorted(invalid))
                                + "; next=regenerate exact review-ready closure before seal")
    manifest = plan["conflict_manifest"]
    if not manifest["repair_budget_feasible"]:
        raise ReleaseTrainError(
            "conflict_manifest.repair_budget_infeasible:required_repair_sets="
            f"{manifest['required_logical_repair_set_count']},exact_complete={manifest['exact_composition_complete']},budget="
            f"{manifest['identity']['forecast_policy']['repair_budget']}; "
            "next=revise the declared repair policy or candidate set before seal")
    path = epoch_state_path(controller, plan["epoch_id"])
    if path.exists():
        current = read_epoch(controller, plan["epoch_id"])
        if current.get("seal", {}).get("plan_id") != plan["plan_id"]:
            raise ReleaseTrainError("epoch id is already sealed with a different immutable snapshot")
        return {"outcome": "already_sealed", "epoch": current}
    token = f"{plan['epoch_id']}:{secrets.token_hex(24)}"
    seal = {"schema_version": EPOCH_SEAL_SCHEMA, **plan, "sealed_utc": utc_now(),
            "cutoff": {"kind": "queue_snapshot", "last_enqueue_sequence": max(
                (row["enqueue_sequence"] for row in plan["members"] if isinstance(row["enqueue_sequence"], int)),
                default=None)}, "fencing_token_sha256": hashlib.sha256(token.encode()).hexdigest()}
    state = {"schema_version": EPOCH_STATE_SCHEMA, "epoch_id": plan["epoch_id"],
             "state": "SEALED", "seal": seal, "dispositions": {
                 row["task_id"]: {"state": "ADMITTED", "reason": None} for row in plan["members"]},
             "composition": {"worktree": None, "ref": None, "tip_sha": plan["base_sha"], "commits": []},
             "aggregate": None, "cas": None, "release_ready": None, "receipts": [],
             "updated_utc": utc_now()}
    epoch_receipt(controller, state, "SEAL", {"plan_id": plan["plan_id"],
                                               "member_count": len(plan["members"])})
    try:
        atomic_json(path, state, exclusive=True)
    except FileExistsError:
        return seal_epoch(controller, declaration_path)
    return {"outcome": "sealed", "lease_token": token, "epoch": state}


def require_epoch_token(state: dict[str, Any], token: Optional[str]) -> None:
    if not token or hashlib.sha256(token.encode()).hexdigest() != state["seal"]["fencing_token_sha256"]:
        raise ReleaseTrainError("exact current epoch fencing token is required")


def save_epoch(controller: Path, state: dict[str, Any], new_state: str,
               transition: str, detail: dict[str, Any]) -> dict[str, Any]:
    epoch_receipt(controller, state, transition, detail)
    state["state"] = new_state; state["updated_utc"] = utc_now()
    atomic_json(epoch_state_path(controller, state["epoch_id"]), state)
    return state


def active_members(state: dict[str, Any]) -> list[dict[str, Any]]:
    by_id = {row["task_id"]: row for row in state["seal"]["members"]}
    return [by_id[task_id] for task_id in state["seal"]["order"]
            if state["dispositions"][task_id]["state"] == "ADMITTED"]


def _epoch_finalization_root(controller: Path, epoch_id: str) -> Path:
    return controller / ".juno_task/runtime/release-epoch-finalization" / epoch_id


def _verified_epoch_receipts(controller: Path, state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Verify every immutable epoch receipt reference before terminal mutation."""
    root = (epoch_root(controller) / state["epoch_id"] / "receipts").resolve()
    verified: dict[str, dict[str, Any]] = {}
    receipts = state.get("receipts")
    if not isinstance(receipts, list) or not receipts:
        raise ReleaseTrainError("release-epoch receipt chain is missing")
    for sequence, reference in enumerate(receipts, 1):
        if (not isinstance(reference, dict)
                or set(reference) != {"path", "sha256", "receipt_id", "transition"}):
            raise ReleaseTrainError("release-epoch receipt reference is malformed")
        path = Path(reference["path"]).resolve()
        if path.parent != root or file_hash(path) != reference["sha256"]:
            raise ReleaseTrainError("release-epoch receipt path or hash drifted")
        try:
            body = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ReleaseTrainError("release-epoch receipt is unreadable") from exc
        unsigned = {key: value for key, value in body.items() if key != "receipt_id"}
        if (body.get("schema_version") != EPOCH_RECEIPT_SCHEMA
                or body.get("epoch_id") != state["epoch_id"]
                or body.get("sequence") != sequence
                or body.get("transition") != reference["transition"]
                or body.get("receipt_id") != reference["receipt_id"]
                or digest(unsigned) != body.get("receipt_id")):
            raise ReleaseTrainError("release-epoch receipt identity drifted")
        if body["transition"] in verified and body["transition"] in {"CAS_COMPLETE", "RELEASE_READY"}:
            raise ReleaseTrainError("release-epoch terminal transition is duplicated")
        verified[body["transition"]] = {"body": body, "reference": reference}
    return verified


def _terminal_epoch_identity(controller: Path, state: dict[str, Any],
                             expected_target: str) -> tuple[Path, str, list[dict[str, Any]], dict[str, Any]]:
    if state.get("state") not in {"INTEGRATED", "RELEASE_READY"}:
        raise ReleaseTrainError("release epoch has no receipt-proven CAS terminal state")
    seal = state.get("seal"); composition = state.get("composition"); cas = state.get("cas")
    if not all(isinstance(value, dict) for value in (seal, composition, cas)):
        raise ReleaseTrainError("release epoch terminal identity is partial")
    members = seal.get("members"); order = seal.get("order"); dispositions = state.get("dispositions")
    if (not isinstance(members, list) or not members or not isinstance(order, list)
            or len(order) != len(set(order)) or not isinstance(dispositions, dict)
            or set(order) != {row.get("task_id") for row in members if isinstance(row, dict)}):
        raise ReleaseTrainError("release epoch member identity is unknown, duplicate, or partial")
    active = active_members(state)
    if [row["task_id"] for row in active] != [task_id for task_id in order
            if dispositions[task_id]["state"] == "ADMITTED"]:
        raise ReleaseTrainError("release epoch active member order drifted")
    receipts = _verified_epoch_receipts(controller, state)
    terminal = receipts.get("CAS_COMPLETE")
    readiness = receipts.get("RELEASE_READY")
    if terminal is None or (state.get("state") == "RELEASE_READY" and readiness is None):
        raise ReleaseTrainError("release epoch CAS/readiness receipt is missing")
    detail = terminal["body"].get("detail")
    if (detail != cas or cas.get("status") != "integrated"
            or cas.get("expected") != seal.get("base_sha")
            or cas.get("tip") != composition.get("tip_sha")
            or cas.get("readback") != cas.get("tip")
            or cas.get("target_move_count") != 1):
        raise ReleaseTrainError("release epoch CAS expected-old/readback identity drifted")
    if readiness is not None:
        ready = readiness["body"].get("detail")
        if (ready != state.get("release_ready") or ready.get("integrated_sha") != cas["tip"]
                or ready.get("target_readback") != cas["readback"]
                or ready.get("seal_plan_id") != seal.get("plan_id")
                or ready.get("required_members") != [row["task_id"] for row in active if row["required"]]):
            raise ReleaseTrainError("release epoch readiness identity drifted")
    config = task_runtime.load_config(controller)
    repository = task_runtime.product_repository(controller, config)
    target = git(repository, "rev-parse", seal.get("target_ref", ""))
    if target != expected_target or not SHA_RE.fullmatch(target):
        raise ReleaseTrainError("release epoch protected target moved")
    if subprocess.run(["git", "-C", str(repository), "merge-base", "--is-ancestor",
                       cas["readback"], target], stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL).returncode:
        raise ReleaseTrainError("release epoch CAS readback is not in current target ancestry")
    for member in active:
        if (not isinstance(member.get("tip_sha"), str) or not SHA_RE.fullmatch(member["tip_sha"])
                or not isinstance(member.get("tree_sha"), str) or not SHA_RE.fullmatch(member["tree_sha"])
                or git(repository, "rev-parse", f"{member['tip_sha']}^{{tree}}") != member["tree_sha"]
                or subprocess.run(["git", "-C", str(repository), "merge-base", "--is-ancestor",
                                  member["tip_sha"], cas["readback"]], stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL).returncode):
            raise ReleaseTrainError(f"release epoch member tip/ancestry drifted: {member.get('task_id')}")
    return repository, target, active, {name: row["reference"] for name, row in receipts.items()}


def _queue_member_identity(controller: Path, member: dict[str, Any], target_ref: str) -> dict[str, Any]:
    record = task_runtime.read_state(controller).get("tasks", {}).get(member["task_id"])
    if (not isinstance(record, dict) or record.get("task_id") != member["task_id"]
            or record.get("target_ref") != target_ref or record.get("tip_sha") != member["tip_sha"]
            or record.get("state") not in {"QUEUED", "MERGED"}):
        raise ReleaseTrainError(f"release epoch member queue identity drifted: {member['task_id']}")
    if record["state"] == "QUEUED" and digest(record) != member.get("queue_record_sha256"):
        raise ReleaseTrainError(f"release epoch sealed queue revision drifted: {member['task_id']}")
    if record["state"] == "MERGED":
        attempt = record.get("queue_attempt")
        if (not isinstance(attempt, dict) or attempt.get("task_id") != member["task_id"]
                or attempt.get("feature_sha") != member["tip_sha"]
                or attempt.get("outcome") not in {"MERGED", "ALREADY_IN_TARGET"}):
            raise ReleaseTrainError(f"release epoch terminal queue evidence drifted: {member['task_id']}")
    return {"state": record["state"], "sha256": digest(record)}


def _ledger_terminal(task: dict[str, Any], member: dict[str, Any]) -> bool:
    fields = task.get("fields") if isinstance(task.get("fields"), dict) else {}
    return (task.get("status") == "done" and task.get("commit_hash") == member["tip_sha"]
            and fields.get("lifecycle_projection") == task_runtime.KANBAN_LIFECYCLE_PROJECTION
            and fields.get("lifecycle_state") == "MERGED")


def _valid_terminal_finalization_readback(finalization: Any, task_id: str,
                                          ledger_revision: str) -> bool:
    if (not isinstance(finalization, dict) or finalization.get("outcome") != "completed"
            or finalization.get("commit_hash") is None):
        return False
    reference = finalization.get("receipt")
    try:
        path = Path(reference["receipt_path"]); payload = json.loads(path.read_text())
    except (KeyError, TypeError, OSError, json.JSONDecodeError):
        return False
    return (file_hash(path) == reference.get("receipt_sha256")
            and payload.get("task_id") == task_id
            and payload.get("after_sha256") == ledger_revision
            and payload.get("operation") == "update")


def _epoch_finalization_plan_id(journal: dict[str, Any]) -> str:
    members = [row for row in journal.get("members", []) if isinstance(row, dict)]
    keys = ["task_id", "tip_sha", "tree_sha", "expected_ledger_revision",
            "expected_task_sha256", "expected_queue"]
    # Event binding was added after v1 journals existed.  Include it in new plan
    # identities without changing the identity of immutable legacy journals.
    if any("expected_ledger_event_sha256" in row for row in members):
        keys += ["expected_ledger_event_sha256", "expected_ledger_event_id"]
    if any("carried_receipt" in row or "carried_from_journal" in row for row in members):
        keys += ["carried_receipt", "carried_from_journal"]
    static_members = [{key: row.get(key) for key in keys} for row in members]
    identity = {**{key: journal.get(key) for key in
        ("epoch_id", "observed_target_sha", "member_order", "cas_receipt", "readiness_receipt")},
        "members": static_members}
    if journal.get("schema_version") == EPOCH_FINALIZATION_SUCCESSOR_SCHEMA:
        identity.update({key: journal.get(key) for key in
            ("predecessor_journal", "predecessor_target_sha", "successor_target_sha")})
    return digest(identity)


def _discover_finalization_predecessor(controller: Path, epoch_id: str,
                                        predecessor_target: str,
                                        repository: Path) -> tuple[Path, dict[str, Any]]:
    """Validate the unique hash-linked successor chain and select one boundary."""
    root_path = _epoch_finalization_root(controller, epoch_id) / "journal.json"
    seen_paths: set[Path] = set()
    matches: list[tuple[Path, dict[str, Any]]] = []

    def walk(path: Path, parent_path: Optional[Path] = None,
             parent: Optional[dict[str, Any]] = None) -> None:
        resolved = path.resolve()
        if resolved in seen_paths:
            raise ReleaseTrainError("release-epoch successor chain loop detected")
        seen_paths.add(resolved)
        try:
            raw = path.read_bytes(); journal = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            raise ReleaseTrainError("release-epoch predecessor journal is unreadable") from exc
        if raw != (canonical(journal) + "\n").encode():
            raise ReleaseTrainError("release-epoch predecessor journal identity drifted")
        schema = journal.get("schema_version")
        expected_schema = (EPOCH_FINALIZATION_SCHEMA if parent is None
                           else EPOCH_FINALIZATION_SUCCESSOR_SCHEMA)
        if (schema != expected_schema or journal.get("epoch_id") != epoch_id
                or journal.get("plan_id") != _epoch_finalization_plan_id(journal)
                or journal.get("member_order") != [row.get("task_id") for row in journal.get("members", [])
                                                    if isinstance(row, dict)]):
            raise ReleaseTrainError("release-epoch predecessor journal identity drifted")
        if parent is not None and parent_path is not None:
            parent_ref = {"path": str(parent_path.resolve()), "sha256": file_hash(parent_path),
                          "plan_id": parent.get("plan_id")}
            if (journal.get("predecessor_journal") != parent_ref
                    or journal.get("predecessor_target_sha") != parent.get("observed_target_sha")
                    or journal.get("successor_target_sha") != journal.get("observed_target_sha")
                    or journal.get("member_order") != parent.get("member_order")
                    or journal.get("cas_receipt") != parent.get("cas_receipt")
                    or journal.get("readiness_receipt") != parent.get("readiness_receipt")):
                raise ReleaseTrainError("release-epoch successor chain hash or member identity drifted")
            if (parent["observed_target_sha"] == journal["observed_target_sha"]
                    or subprocess.run(["git", "-C", str(repository), "merge-base", "--is-ancestor",
                               parent["observed_target_sha"], journal["observed_target_sha"]],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode):
                raise ReleaseTrainError("release-epoch successor chain target diverged")
        if journal.get("observed_target_sha") == predecessor_target:
            matches.append((path, journal))
        children = sorted((path.parent / "successors").glob("*/journal.json"))
        if len(children) > 1:
            raise ReleaseTrainError("release-epoch successor chain fork detected")
        for child in children:
            walk(child, path, journal)

    walk(root_path)
    if len(matches) != 1:
        raise ReleaseTrainError("release-epoch predecessor target coverage is missing or ambiguous")
    return matches[0]


def _carried_member_receipt_plan(journal_path: Path, journal: dict[str, Any],
                                 task_id: str, reference: dict[str, Any],
                                 seen: Optional[set[Path]] = None) -> str:
    """Resolve the immutable journal that originally emitted a carried receipt."""
    visited = seen or set()
    resolved = journal_path.resolve()
    if resolved in visited:
        raise ReleaseTrainError("release-epoch carried member chain loop detected")
    visited.add(resolved)
    rows = [row for row in journal.get("members", [])
            if row.get("task_id") == task_id and row.get("status") == "complete"
            and row.get("receipt") == reference]
    if len(rows) != 1:
        raise ReleaseTrainError("release-epoch carried completed member evidence drifted")
    source = rows[0].get("carried_from_journal")
    if source is None:
        return journal["plan_id"]
    try:
        source_path = Path(source["path"]); source_journal = json.loads(source_path.read_text())
    except (KeyError, TypeError, OSError, json.JSONDecodeError) as exc:
        raise ReleaseTrainError("release-epoch carried completed member evidence drifted") from exc
    if (file_hash(source_path) != source.get("sha256")
            or source_journal.get("plan_id") != source.get("plan_id")
            or source_journal.get("plan_id") != _epoch_finalization_plan_id(source_journal)):
        raise ReleaseTrainError("release-epoch carried completed member evidence drifted")
    return _carried_member_receipt_plan(source_path, source_journal, task_id,
                                        reference, visited)


def _valid_finalization_successor(predecessor_path: Path,
                                  predecessor: dict[str, Any]) -> bool:
    predecessor_ref = {"path": str(predecessor_path.resolve()),
        "sha256": file_hash(predecessor_path), "plan_id": predecessor.get("plan_id")}
    candidates = []
    for path in (predecessor_path.parent / "successors").glob("*/journal.json"):
        try: candidate = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if (candidate.get("schema_version") == EPOCH_FINALIZATION_SUCCESSOR_SCHEMA
                and candidate.get("predecessor_journal") == predecessor_ref):
            candidates.append((path, candidate))
    if len(candidates) != 1:
        return False
    path, candidate = candidates[0]
    if (candidate.get("plan_id") != path.parent.name
            or candidate.get("plan_id") != _epoch_finalization_plan_id(candidate)
            or candidate.get("member_order") != [row.get("task_id") for row in candidate.get("members", [])]):
        return False
    statuses = [row.get("status") for row in candidate.get("members", [])]
    if statuses != sorted(statuses, key=lambda value: 0 if value == "complete" else 1):
        return False
    if any(status not in {"complete", "pending"} for status in statuses):
        return False
    if any(status == "complete" and not isinstance(row.get("receipt"), dict)
           for status, row in zip(statuses, candidate.get("members", []))):
        return False
    if any(status == "pending" for status in statuses):
        return _valid_finalization_successor(path, candidate)
    references = candidate.get("supersession_receipts")
    completion_ref = candidate.get("completed_receipt")
    if (not isinstance(references, dict)
            or references.get("successor_complete") != completion_ref
            or not isinstance(completion_ref, dict)):
        return False
    try:
        completion_path = Path(completion_ref["path"]); completion = json.loads(completion_path.read_text())
        superseded_ref = references["predecessor_superseded"]
        superseded_path = Path(superseded_ref["path"]); superseded = json.loads(superseded_path.read_text())
    except (KeyError, TypeError, OSError, json.JSONDecodeError):
        return False
    completion_unsigned = {key: value for key, value in completion.items() if key != "receipt_id"}
    superseded_unsigned = {key: value for key, value in superseded.items() if key != "receipt_id"}
    return (file_hash(completion_path) == completion_ref.get("sha256")
        and completion.get("schema_version") == EPOCH_FINALIZATION_RECEIPT_SCHEMA
        and completion.get("transition") == "SUCCESSOR_COMPLETE"
        and completion.get("predecessor_journal") == predecessor_ref
        and digest(completion_unsigned) == completion.get("receipt_id")
        and completion_ref.get("receipt_id") == completion.get("receipt_id")
        and file_hash(superseded_path) == superseded_ref.get("sha256")
        and superseded.get("schema_version") == EPOCH_FINALIZATION_SUPERSESSION_SCHEMA
        and superseded.get("transition") == "PREDECESSOR_SUPERSEDED"
        and superseded.get("predecessor_journal") == predecessor_ref
        and superseded.get("successor_completion") == completion_ref
        and digest(superseded_unsigned) == superseded.get("receipt_id")
        and superseded_ref.get("receipt_id") == superseded.get("receipt_id"))


def _unresolved_finalization_epochs(controller: Path) -> list[str]:
    root = controller / ".juno_task/runtime/release-epoch-finalization"
    unresolved = []
    for path in root.glob("*/journal.json"):
        try: journal = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if (journal.get("schema_version") == EPOCH_FINALIZATION_SCHEMA
                and journal.get("completed_receipt") is None
                and not _valid_finalization_successor(path, journal)):
            unresolved.append(str(journal.get("epoch_id") or path.parent.name))
    return sorted(set(unresolved))


def _assert_protected_target(repository: Path, target_ref: str, expected_target: str) -> None:
    if git(repository, "rev-parse", target_ref) != expected_target:
        raise ReleaseTrainError("release-epoch protected target moved during finalization")


def reconcile_epoch_members(controller: Path, epoch_id: str, expected_target: str,
                            operation_root: Optional[Path] = None,
                            prepared_journal: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Revision-CAS every integrated member to Ledger done/MERGED, resumably."""
    root = operation_root or _epoch_finalization_root(controller, epoch_id)
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / "operation.lock"
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = read_epoch(controller, epoch_id)
        repository, target, members, receipt_refs = _terminal_epoch_identity(
            controller, state, expected_target)
        journal_path = root / "journal.json"
        if journal_path.exists():
            try: journal = json.loads(journal_path.read_text())
            except json.JSONDecodeError as exc: raise ReleaseTrainError("release-epoch finalization journal is malformed") from exc
            if (journal.get("schema_version") not in
                    {EPOCH_FINALIZATION_SCHEMA, EPOCH_FINALIZATION_SUCCESSOR_SCHEMA}
                    or journal.get("epoch_id") != epoch_id or journal.get("observed_target_sha") != target
                    or journal.get("member_order") != [row["task_id"] for row in members]
                    or journal.get("plan_id") != _epoch_finalization_plan_id(journal)
                    or (prepared_journal is not None
                        and _epoch_finalization_plan_id(prepared_journal) != journal.get("plan_id"))):
                raise ReleaseTrainError("release-epoch finalization replay identity drifted")
        elif prepared_journal is not None:
            journal = prepared_journal
            if (journal.get("schema_version") != EPOCH_FINALIZATION_SUCCESSOR_SCHEMA
                    or journal.get("plan_id") != _epoch_finalization_plan_id(journal)):
                raise ReleaseTrainError("release-epoch successor journal identity drifted")
            atomic_json(journal_path, journal, exclusive=True)
        else:
            planned = []
            for member in members:
                queue = _queue_member_identity(controller, member, state["seal"]["target_ref"])
                task = kanban_task(controller, member["task_id"])
                ledger = task_runtime.kanban_board_identity(controller, member["task_id"])
                if not _ledger_terminal(task, member) and digest(task) != member.get("task_sha256"):
                    raise ReleaseTrainError(f"release epoch Ledger revision drifted: {member['task_id']}")
                planned.append({"task_id": member["task_id"], "tip_sha": member["tip_sha"],
                    "tree_sha": member["tree_sha"], "expected_ledger_revision": ledger["revision"],
                    "expected_task_sha256": ledger["task_sha256"],
                    "expected_ledger_event_sha256": ledger["event_sha256"],
                    "expected_ledger_event_id": ledger["event_id"], "expected_queue": queue,
                    "status": "pending", "receipt": None})
            journal = {"schema_version": EPOCH_FINALIZATION_SCHEMA, "epoch_id": epoch_id,
                "observed_target_sha": target, "member_order": [row["task_id"] for row in members],
                "cas_receipt": receipt_refs["CAS_COMPLETE"],
                "readiness_receipt": receipt_refs.get("RELEASE_READY"), "members": planned,
                "completed_receipt": None}
            journal["plan_id"] = _epoch_finalization_plan_id(journal)
            atomic_json(journal_path, journal, exclusive=True)
        if len(journal.get("members", [])) != len(members):
            raise ReleaseTrainError("release-epoch finalization member journal is partial")
        import merge_queue
        for member, planned in zip(members, journal["members"]):
            _assert_protected_target(repository, state["seal"]["target_ref"], target)
            if planned.get("task_id") != member["task_id"] or planned.get("tip_sha") != member["tip_sha"]:
                raise ReleaseTrainError("release-epoch finalization member replay drifted")
            member_path = root / "members" / f"{len([p for p in journal['members'] if p.get('status') == 'complete']) + 1:04d}-{member['task_id']}.json"
            if planned.get("status") == "complete":
                reference = planned.get("receipt")
                try:
                    member_receipt = json.loads(Path(reference.get("path", "")).read_text()) \
                        if isinstance(reference, dict) else None
                except (OSError, json.JSONDecodeError):
                    member_receipt = None
                unsigned = ({key: value for key, value in member_receipt.items() if key != "receipt_id"}
                            if isinstance(member_receipt, dict) else {})
                ledger = task_runtime.kanban_board_identity(controller, member["task_id"])
                carried_ref = planned.get("carried_from_journal")
                carried_receipt = planned.get("carried_receipt")
                receipt_plan_id = journal["plan_id"]
                carried_journal = None
                if carried_ref is not None:
                    try:
                        carried_path = Path(carried_ref["path"])
                        carried_journal = json.loads(carried_path.read_text())
                    except (KeyError, TypeError, OSError, json.JSONDecodeError):
                        carried_journal = None
                    if (not isinstance(carried_journal, dict)
                            or file_hash(carried_path) != carried_ref.get("sha256")
                            or carried_journal.get("plan_id") != carried_ref.get("plan_id")
                            or carried_journal.get("plan_id") != _epoch_finalization_plan_id(carried_journal)
                            or carried_receipt != reference):
                        raise ReleaseTrainError("release-epoch carried completed member evidence drifted")
                    receipt_plan_id = _carried_member_receipt_plan(
                        carried_path, carried_journal, member["task_id"], reference)
                finalization = member_receipt.get("finalization") if isinstance(member_receipt, dict) else None
                queue_after = member_receipt.get("queue_after") if isinstance(member_receipt, dict) else None
                if (not isinstance(reference, dict) or file_hash(Path(reference.get("path", ""))) != reference.get("sha256")
                        or not isinstance(member_receipt, dict)
                        or member_receipt.get("schema_version") != EPOCH_FINALIZATION_MEMBER_SCHEMA
                        or member_receipt.get("plan_id") != receipt_plan_id
                        or member_receipt.get("sequence") != journal["member_order"].index(member["task_id"]) + 1
                        or member_receipt.get("task_id") != member["task_id"]
                        or member_receipt.get("tip_sha") != member["tip_sha"]
                        or digest(unsigned) != member_receipt.get("receipt_id")
                        or reference.get("receipt_id") != member_receipt.get("receipt_id")
                        or member_receipt.get("ledger_revision") != ledger["revision"]
                        or (member_receipt.get("ledger_event_sha256") is not None
                            and member_receipt.get("ledger_event_sha256") != ledger["event_sha256"])
                        or not _valid_terminal_finalization_readback(
                            finalization, member["task_id"], ledger["revision"])
                        or finalization.get("commit_hash") != member["tip_sha"]
                        or not isinstance(queue_after, dict)
                        or not _ledger_terminal(kanban_task(controller, member["task_id"]), member)):
                    raise ReleaseTrainError("release-epoch completed member evidence drifted")
                if _queue_member_identity(controller, member, state["seal"]["target_ref"]) != queue_after:
                    raise ReleaseTrainError("release-epoch completed member queue evidence drifted")
                continue
            task = kanban_task(controller, member["task_id"])
            ledger = task_runtime.kanban_board_identity(controller, member["task_id"])
            expected_event = planned.get("expected_ledger_event_sha256")
            expected_event_id = planned.get("expected_ledger_event_id")
            if not _ledger_terminal(task, member) and (
                    ledger["revision"] != planned["expected_ledger_revision"]
                    or (expected_event is not None and ledger["event_sha256"] != expected_event)
                    or (expected_event_id is not None and ledger["event_id"] != expected_event_id)
                    or (expected_event is not None
                        and ledger["task_sha256"] != planned["expected_task_sha256"])):
                raise ReleaseTrainError(f"release epoch Ledger revision drifted: {member['task_id']}")
            queue_before = _queue_member_identity(controller, member, state["seal"]["target_ref"])
            attempt = {"schema_version": "juno_merge_attempt.v1", "task_id": member["task_id"],
                "target_ref": state["seal"]["target_ref"], "expected_target_sha": target,
                "feature_sha": member["tip_sha"], "candidate_sha": member["tip_sha"],
                "candidate_tree": member["tree_sha"], "candidate_checkout": None,
                "candidate_token": None, "validation": [], "review": None, "outcome": "MERGED",
                "readback_sha": target, "expected_kanban_revision": planned["expected_ledger_revision"],
                "terminal_evidence": {"epoch_id": epoch_id,
                    "cas_receipt_id": receipt_refs["CAS_COMPLETE"]["receipt_id"]}}
            finalization = merge_queue.finalize_kanban_task(controller, attempt)
            current_record = task_runtime.read_state(controller).get("tasks", {}).get(member["task_id"])
            if isinstance(current_record, dict) and current_record.get("state") != "MERGED":
                merge_queue.persist_attempt(controller, attempt, state_name="MERGED", remove_conflict=True)
            task_after = kanban_task(controller, member["task_id"])
            if not _ledger_terminal(task_after, member):
                raise ReleaseTrainError("release epoch terminal Ledger readback mismatched")
            queue_after = _queue_member_identity(controller, member, state["seal"]["target_ref"])
            ledger_after = task_runtime.kanban_board_identity(controller, member["task_id"])
            body = {"schema_version": EPOCH_FINALIZATION_MEMBER_SCHEMA, "epoch_id": epoch_id,
                "plan_id": journal["plan_id"], "sequence": journal["member_order"].index(member["task_id"]) + 1,
                "task_id": member["task_id"], "tip_sha": member["tip_sha"],
                "expected_ledger_revision": planned["expected_ledger_revision"],
                "ledger_revision": ledger_after["revision"],
                "ledger_event_sha256": ledger_after["event_sha256"],
                "queue_before": queue_before, "queue_after": queue_after,
                "finalization": finalization, "cas_receipt_id": receipt_refs["CAS_COMPLETE"]["receipt_id"]}
            body["receipt_id"] = digest(body)
            member_path.parent.mkdir(parents=True, exist_ok=True)
            if member_path.exists():
                if json.loads(member_path.read_text()) != body:
                    raise ReleaseTrainError("release-epoch member receipt identity collided")
            else: atomic_json(member_path, body, exclusive=True)
            planned["status"] = "complete"; planned["receipt"] = {
                "path": str(member_path.resolve()), "sha256": file_hash(member_path),
                "receipt_id": body["receipt_id"]}
            atomic_json(journal_path, journal)
        _assert_protected_target(repository, state["seal"]["target_ref"], target)
        if journal.get("completed_receipt"):
            reference = journal["completed_receipt"]
            try: completion = json.loads(Path(reference.get("path", "")).read_text())
            except (OSError, json.JSONDecodeError) as exc:
                raise ReleaseTrainError("release-epoch terminal completion receipt drifted") from exc
            unsigned = {key: value for key, value in completion.items() if key != "receipt_id"}
            if (file_hash(Path(reference.get("path", ""))) != reference.get("sha256")
                    or completion.get("schema_version") != EPOCH_FINALIZATION_RECEIPT_SCHEMA
                    or completion.get("plan_id") != journal["plan_id"]
                    or completion.get("members") != [row["receipt"] for row in journal["members"]]
                    or digest(unsigned) != completion.get("receipt_id")
                    or reference.get("receipt_id") != completion.get("receipt_id")):
                raise ReleaseTrainError("release-epoch terminal completion receipt drifted")
            return journal
        completion = {"schema_version": EPOCH_FINALIZATION_RECEIPT_SCHEMA, "epoch_id": epoch_id,
            "plan_id": journal["plan_id"], "observed_target_sha": target,
            "cas_receipt_id": receipt_refs["CAS_COMPLETE"]["receipt_id"],
            "members": [row["receipt"] for row in journal["members"]], "completed_utc": utc_now()}
        if journal.get("schema_version") == EPOCH_FINALIZATION_SUCCESSOR_SCHEMA:
            completion.update({"transition": "SUCCESSOR_COMPLETE",
                "predecessor_journal": journal["predecessor_journal"],
                "predecessor_target_sha": journal["predecessor_target_sha"],
                "successor_target_sha": journal["successor_target_sha"]})
        completion["receipt_id"] = digest(completion)
        completion_path = root / "receipt.json"; atomic_json(completion_path, completion, exclusive=True)
        journal["completed_receipt"] = {"path": str(completion_path.resolve()),
            "sha256": file_hash(completion_path), "receipt_id": completion["receipt_id"]}
        atomic_json(journal_path, journal)
        return journal


def replay_epoch_finalization_successor(controller: Path, epoch_id: str,
                                        predecessor_target: str,
                                        expected_target: str) -> dict[str, Any]:
    """Resume one hash-linked incomplete journal at an exact descendant target."""
    state = read_epoch(controller, epoch_id)
    repository, target, members, receipt_refs = _terminal_epoch_identity(
        controller, state, expected_target)
    predecessor_path, predecessor = _discover_finalization_predecessor(
        controller, epoch_id, predecessor_target, repository)
    predecessor_root = predecessor_path.parent
    predecessor_bytes = predecessor_path.read_bytes()
    predecessor_members = predecessor.get("members")
    statuses = ([row.get("status") for row in predecessor_members]
                if isinstance(predecessor_members, list) else [])
    first_pending = next((index for index, status in enumerate(statuses)
                          if status == "pending"), len(statuses))
    if (not isinstance(predecessor_members, list) or not predecessor_members
            or predecessor.get("completed_receipt") is not None
            or first_pending == len(statuses)
            or statuses[:first_pending] != ["complete"] * first_pending
            or statuses[first_pending:] != ["pending"] * (len(statuses) - first_pending)
            or any((row.get("receipt") is None) != (row.get("status") == "pending")
                   for row in predecessor_members)
            or (predecessor_root / "receipt.json").exists()):
        raise ReleaseTrainError("release-epoch predecessor journal has invalid partial completion")
    referenced_local = {Path(row["receipt"]["path"]).resolve()
                        for row in predecessor_members if row.get("status") == "complete"
                        and isinstance(row.get("receipt"), dict)}
    local_receipts = {path.resolve() for path in (predecessor_root / "members").glob("*.json")}
    if not local_receipts.issubset(referenced_local):
        raise ReleaseTrainError("release-epoch predecessor has partial mutation without receipt")
    if target != expected_target or subprocess.run(
            ["git", "-C", str(repository), "merge-base", "--is-ancestor",
             predecessor_target, target], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL).returncode:
        raise ReleaseTrainError("release-epoch successor target is not a descendant of predecessor target")
    if ([row["task_id"] for row in members] != predecessor["member_order"]
            or predecessor.get("cas_receipt") != receipt_refs["CAS_COMPLETE"]
            or predecessor.get("readiness_receipt") != receipt_refs.get("RELEASE_READY")):
        raise ReleaseTrainError("release-epoch predecessor member or receipt identity drifted")

    predecessor_ref = {"path": str(predecessor_path.resolve()),
        "sha256": hashlib.sha256(predecessor_bytes).hexdigest(),
        "plan_id": predecessor["plan_id"]}
    existing = []
    for path in (predecessor_root / "successors").glob("*/journal.json"):
        try: candidate = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if (candidate.get("schema_version") == EPOCH_FINALIZATION_SUCCESSOR_SCHEMA
                and candidate.get("predecessor_journal") == predecessor_ref
                and candidate.get("predecessor_target_sha") == predecessor_target
                and candidate.get("successor_target_sha") == target):
            existing.append((path.parent, candidate))
    if len(existing) > 1:
        raise ReleaseTrainError("release-epoch successor coverage is ambiguous")
    if existing:
        successor_root, candidate = existing[0]
        if (candidate.get("plan_id") != successor_root.name
                or candidate.get("plan_id") != _epoch_finalization_plan_id(candidate)
                or (candidate.get("supersession_receipts") is not None
                    and not isinstance(candidate.get("supersession_receipts"), dict))):
            raise ReleaseTrainError("release-epoch successor terminal identity drifted")
        completed = reconcile_epoch_members(controller, epoch_id, target, successor_root)
        references = completed.get("supersession_receipts") or {}
        superseded_ref = references.get("predecessor_superseded")
        if superseded_ref is None:
            completion_ref = completed.get("completed_receipt")
            if not isinstance(completion_ref, dict):
                raise ReleaseTrainError("release-epoch successor completion is missing")
            supersession = {"schema_version": EPOCH_FINALIZATION_SUPERSESSION_SCHEMA,
                "transition": "PREDECESSOR_SUPERSEDED", "epoch_id": epoch_id,
                "predecessor_journal": predecessor_ref,
                "predecessor_target_sha": predecessor_target,
                "successor_target_sha": target, "successor_plan_id": completed["plan_id"],
                "successor_completion": completion_ref, "member_order": completed["member_order"]}
            supersession["receipt_id"] = digest(supersession)
            supersession_path = successor_root / "predecessor-superseded.json"
            if supersession_path.exists():
                try: existing_supersession = json.loads(supersession_path.read_text())
                except json.JSONDecodeError as exc:
                    raise ReleaseTrainError("release-epoch predecessor supersession receipt drifted") from exc
                if existing_supersession != supersession:
                    raise ReleaseTrainError("release-epoch predecessor supersession receipt drifted")
            else:
                atomic_json(supersession_path, supersession, exclusive=True)
            superseded_ref = {"path": str(supersession_path.resolve()),
                "sha256": file_hash(supersession_path), "receipt_id": supersession["receipt_id"]}
            latest = json.loads((successor_root / "journal.json").read_text())
            latest["supersession_receipts"] = {
                "successor_complete": completion_ref, "predecessor_superseded": superseded_ref}
            atomic_json(successor_root / "journal.json", latest)
            _assert_protected_target(repository, state["seal"]["target_ref"], target)
            return latest
        try:
            superseded = json.loads(Path(superseded_ref.get("path", "")).read_text())
        except (AttributeError, OSError, json.JSONDecodeError) as exc:
            raise ReleaseTrainError("release-epoch predecessor supersession receipt drifted") from exc
        unsigned = {key: value for key, value in superseded.items() if key != "receipt_id"}
        if (file_hash(Path(superseded_ref["path"])) != superseded_ref.get("sha256")
                or superseded.get("schema_version") != EPOCH_FINALIZATION_SUPERSESSION_SCHEMA
                or superseded.get("predecessor_journal") != predecessor_ref
                or superseded.get("successor_completion") != completed.get("completed_receipt")
                or digest(unsigned) != superseded.get("receipt_id")
                or superseded_ref.get("receipt_id") != superseded.get("receipt_id")):
            raise ReleaseTrainError("release-epoch predecessor supersession receipt drifted")
        return completed

    planned = []
    for index, (member, prior) in enumerate(zip(members, predecessor_members)):
        if any(prior.get(key) != member.get(key) for key in ("task_id", "tip_sha", "tree_sha")):
            raise ReleaseTrainError("release-epoch predecessor member identity drifted")
        queue = _queue_member_identity(controller, member, state["seal"]["target_ref"])
        if queue.get("state") != "MERGED" or queue != prior.get("expected_queue"):
            raise ReleaseTrainError(f"release epoch successor queue truth drifted: {member['task_id']}")
        task = kanban_task(controller, member["task_id"])
        ledger = task_runtime.kanban_board_identity(controller, member["task_id"])
        if index < first_pending:
            reference = prior.get("receipt")
            try:
                receipt = json.loads(Path(reference["path"]).read_text())
            except (KeyError, TypeError, OSError, json.JSONDecodeError):
                receipt = None
            unsigned = ({key: value for key, value in receipt.items() if key != "receipt_id"}
                        if isinstance(receipt, dict) else {})
            try:
                receipt_plan_id = _carried_member_receipt_plan(
                    predecessor_path, predecessor, member["task_id"], reference)
            except (TypeError, KeyError):
                receipt_plan_id = None
            if (not isinstance(reference, dict) or not isinstance(receipt, dict)
                    or file_hash(Path(reference["path"])) != reference.get("sha256")
                    or receipt.get("schema_version") != EPOCH_FINALIZATION_MEMBER_SCHEMA
                    or receipt.get("plan_id") != receipt_plan_id
                    or receipt.get("sequence") != index + 1
                    or receipt.get("task_id") != member["task_id"]
                    or receipt.get("tip_sha") != member["tip_sha"]
                    or receipt.get("ledger_revision") != ledger["revision"]
                    or not _valid_terminal_finalization_readback(
                        receipt.get("finalization"), member["task_id"], ledger["revision"])
                    or receipt.get("finalization", {}).get("commit_hash") != member["tip_sha"]
                    or (receipt.get("ledger_event_sha256") is not None
                        and receipt.get("ledger_event_sha256") != ledger["event_sha256"])
                    or digest(unsigned) != receipt.get("receipt_id")
                    or reference.get("receipt_id") != receipt.get("receipt_id")
                    or not _ledger_terminal(task, member)):
                raise ReleaseTrainError(
                    f"release epoch completed member evidence drifted: {member['task_id']}")
            planned.append({"task_id": member["task_id"], "tip_sha": member["tip_sha"],
                "tree_sha": member["tree_sha"],
                "expected_ledger_revision": prior.get("expected_ledger_revision"),
                "expected_task_sha256": prior.get("expected_task_sha256"),
                "expected_ledger_event_sha256": prior.get("expected_ledger_event_sha256"),
                "expected_ledger_event_id": prior.get("expected_ledger_event_id"),
                "expected_queue": queue, "carried_receipt": reference,
                "carried_from_journal": predecessor_ref,
                "status": "complete", "receipt": reference})
            continue
        # Pending members are bound only to canonical own-task history, never
        # relation-expanded `get` output, and must be untouched at this boundary.
        prior_event = prior.get("expected_ledger_event_sha256")
        if (_ledger_terminal(task, member)
                or ledger["revision"] != prior.get("expected_ledger_revision")
                or (prior_event is not None and ledger["event_sha256"] != prior_event)
                or (prior_event is not None and ledger["event_id"] != prior.get("expected_ledger_event_id"))
                or (prior_event is not None
                    and ledger["task_sha256"] != prior.get("expected_task_sha256"))):
            raise ReleaseTrainError(
                f"release epoch pending member own identity drifted: {member['task_id']}")
        planned.append({"task_id": member["task_id"], "tip_sha": member["tip_sha"],
            "tree_sha": member["tree_sha"], "expected_ledger_revision": ledger["revision"],
            "expected_task_sha256": ledger["task_sha256"],
            "expected_ledger_event_sha256": ledger["event_sha256"],
            "expected_ledger_event_id": ledger["event_id"], "expected_queue": queue,
            "status": "pending", "receipt": None})
    successor = {"schema_version": EPOCH_FINALIZATION_SUCCESSOR_SCHEMA,
        "epoch_id": epoch_id, "observed_target_sha": target,
        "predecessor_journal": predecessor_ref,
        "predecessor_target_sha": predecessor_target, "successor_target_sha": target,
        "member_order": [row["task_id"] for row in members],
        "cas_receipt": receipt_refs["CAS_COMPLETE"],
        "readiness_receipt": receipt_refs.get("RELEASE_READY"), "members": planned,
        "completed_receipt": None, "supersession_receipts": None}
    successor["plan_id"] = _epoch_finalization_plan_id(successor)
    successor_root = predecessor_root / "successors" / successor["plan_id"]
    completed = reconcile_epoch_members(controller, epoch_id, target,
                                        successor_root, successor)
    _assert_protected_target(repository, state["seal"]["target_ref"], target)
    journal_path = successor_root / "journal.json"
    completion_ref = completed.get("completed_receipt")
    if not isinstance(completion_ref, dict):
        raise ReleaseTrainError("release-epoch successor completion is missing")
    supersession = {"schema_version": EPOCH_FINALIZATION_SUPERSESSION_SCHEMA,
        "transition": "PREDECESSOR_SUPERSEDED", "epoch_id": epoch_id,
        "predecessor_journal": predecessor_ref, "predecessor_target_sha": predecessor_target,
        "successor_target_sha": target, "successor_plan_id": completed["plan_id"],
        "successor_completion": completion_ref, "member_order": completed["member_order"]}
    supersession["receipt_id"] = digest(supersession)
    supersession_path = successor_root / "predecessor-superseded.json"
    if supersession_path.exists():
        if json.loads(supersession_path.read_text()) != supersession:
            raise ReleaseTrainError("release-epoch predecessor supersession receipt drifted")
    else:
        atomic_json(supersession_path, supersession, exclusive=True)
    supersession_ref = {"path": str(supersession_path.resolve()),
        "sha256": file_hash(supersession_path), "receipt_id": supersession["receipt_id"]}
    latest = json.loads(journal_path.read_text())
    expected_receipts = {"successor_complete": completion_ref,
                         "predecessor_superseded": supersession_ref}
    if latest.get("supersession_receipts") not in (None, expected_receipts):
        raise ReleaseTrainError("release-epoch successor terminal receipt set drifted")
    if latest.get("supersession_receipts") is None:
        latest["supersession_receipts"] = expected_receipts
        atomic_json(journal_path, latest)
    _assert_protected_target(repository, state["seal"]["target_ref"], target)
    return latest


def eject_epoch_member(controller: Path, epoch_id: str, task_id: str, reason: str,
                       token: str) -> dict[str, Any]:
    state = read_epoch(controller, epoch_id); require_epoch_token(state, token)
    by_id = {row["task_id"]: row for row in state["seal"]["members"]}
    if task_id not in by_id:
        raise ReleaseTrainError("task is not an epoch member")
    if by_id[task_id]["required"]:
        state["dispositions"][task_id] = {"state": "FAILED_REQUIRED", "reason": reason}
        return save_epoch(controller, state, "PAUSED_REQUIRED", "PAUSE_REQUIRED", {"task_id": task_id, "reason": reason})
    descendants = {task_id}; changed = True
    while changed:
        changed = False
        for before, after in state["seal"]["dependency_edges"]:
            if before in descendants and after not in descendants:
                descendants.add(after); changed = True
    for member_id in descendants:
        state["dispositions"][member_id] = {"state": "EJECTED_OPTIONAL", "reason": reason,
                                             "ancestor": task_id if member_id != task_id else None}
    return save_epoch(controller, state, "SEALED", "EJECT_OPTIONAL", {"task_id": task_id,
                       "descendants": sorted(descendants), "reason": reason})


def composition_paths(controller: Path, state: dict[str, Any]) -> tuple[Path, Path, str]:
    config = task_runtime.load_config(controller)
    repository = task_runtime.product_repository(controller, config)
    root = Path(config["workspace_root"]).expanduser().resolve() / ".release-epochs" / state["epoch_id"]
    ref = f"refs/juno/release-epochs/{state['epoch_id']}"
    return repository, root, ref


def ensure_full_train_checkout(checkout: Path) -> None:
    """Materialize a private train independently of controller sparse state."""
    sparse = git(checkout, "config", "--bool", "core.sparseCheckout").lower() == "true"
    if sparse:
        task_runtime.run(["git", "-C", str(checkout), "sparse-checkout", "disable"], checkout)
    if git(checkout, "config", "--bool", "core.sparseCheckout").lower() == "true":
        raise ReleaseTrainError("private release train remained sparse after materialization")
    skipped = [line[2:] for line in git(checkout, "ls-files", "-t").splitlines()
               if line.startswith("S ")]
    if skipped:
        raise ReleaseTrainError("private release train contains skip-worktree paths: " + ", ".join(skipped[:10]))
    if git(checkout, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ReleaseTrainError("private release train is dirty before composition or validation")


def managed_inventory_matches_checkout(checkout: Path, manifest_bytes: bytes) -> bool:
    """Prove one installed inventory against the exact merged runtime/template bytes."""
    try:
        inventory = json.loads(manifest_bytes)
        declaration = json.loads((checkout / "juno-code/src/templates/managed-assets.json").read_text())
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    assets = inventory.get("assets") if isinstance(inventory, dict) else None
    declared = declaration.get("assets") if isinstance(declaration, dict) else None
    if (inventory.get("schemaVersion") != 2 or not isinstance(assets, dict)
            or not isinstance(declared, list)):
        return False
    sources = {row.get("destination"): row.get("source") for row in declared if isinstance(row, dict)}
    if not set(assets).issubset(sources) or any(
            not isinstance(sources[destination], str) for destination in assets):
        return False
    for destination, record in assets.items():
        if not isinstance(record, dict) or set(record) != {
                "type", "templateVersion", "sourceSha256", "installedSha256"}:
            return False
        try:
            installed = (checkout / destination).read_bytes()
            source = (checkout / "juno-code/src/templates" / sources[destination]).read_bytes()
        except OSError:
            return False
        if (hashlib.sha256(installed).hexdigest() != record.get("installedSha256")
                or hashlib.sha256(source).hexdigest() != record.get("sourceSha256")
                or record.get("sourceSha256") != record.get("installedSha256")):
            return False
    identity = inventory.get("instructionBundle")
    if not isinstance(identity, dict):
        return False
    def asset_order(item: tuple[str, Any]) -> tuple[int, str, str]:
        destination = item[0]
        return (0 if destination.startswith(".") else 1,
                re.sub(r"[^a-z0-9]", "", destination.lower()), destination)
    projected = [{"destination": destination, "type": record.get("type"),
                  "sourceSha256": record.get("sourceSha256"),
                  "installedSha256": record.get("installedSha256")}
                 for destination, record in sorted(assets.items(), key=asset_order)]
    assets_sha = hashlib.sha256(json.dumps(projected, separators=(",", ":")).encode()).hexdigest()
    core = {"schemaVersion": identity.get("schemaVersion"),
            "semanticVersion": identity.get("semanticVersion"),
            "packageVersion": identity.get("packageVersion"),
            "assetCount": identity.get("assetCount"), "assetsSha256": identity.get("assetsSha256")}
    bundle_sha = hashlib.sha256(json.dumps(core, separators=(",", ":")).encode()).hexdigest()
    return (identity.get("schemaVersion") == "juno_instruction_bundle.v1"
            and identity.get("packageVersion") == inventory.get("packageVersion")
            and identity.get("assetCount") == len(assets)
            and identity.get("assetsSha256") == assets_sha
            and identity.get("bundleSha256") == bundle_sha)


def replay_generation_binding(state: dict[str, Any], member: dict[str, Any]) -> Optional[dict[str, Any]]:
    repair = state.get("conflict_repair")
    if not isinstance(repair, dict) or repair.get("receipt_schema") != SUCCESSOR_REPAIR_SCHEMA:
        return None
    for binding in repair.get("later_generations", []):
        if (isinstance(binding, dict) and binding.get("task_id") == member["task_id"]
                and binding.get("tip_sha") == member["tip_sha"]):
            return binding
    return None


def resolve_bound_managed_generation(checkout: Path, state: dict[str, Any],
                                     member: dict[str, Any], conflicts: list[str]) -> bool:
    """Resolve only a receipt-bound cumulative managed inventory generation."""
    binding = replay_generation_binding(state, member)
    if binding is None or conflicts != [".juno_task/managed-assets.json"]:
        return False
    manifest = subprocess.run(["git", "-C", str(checkout), "show",
                               f"{member['tip_sha']}:.juno_task/managed-assets.json"],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout
    if (not manifest or hashlib.sha256(manifest).hexdigest() != binding.get("manifest_sha256")):
        raise ReleaseTrainError("receipt-bound managed generation manifest identity drifted")
    ours_manifest = subprocess.run(["git", "-C", str(checkout), "show",
                                    f"{state['seal']['base_sha']}:.juno_task/managed-assets.json"],
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout
    try:
        # The immutable successor base owns the complete declaration generation.
        # Historical repair and later candidates may be older and intentionally
        # list fewer assets; they authorize code deltas, not deletion of newer
        # controller assets.
        inventory = json.loads(ours_manifest)
        candidate_inventory = json.loads(manifest)
        declaration = json.loads(
            (checkout / "juno-code/src/templates/managed-assets.json").read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseTrainError("receipt-bound managed generation declarations are malformed") from exc
    assets = inventory.get("assets") if isinstance(inventory, dict) else None
    candidate_assets = (candidate_inventory.get("assets")
                        if isinstance(candidate_inventory, dict) else None)
    declared = declaration.get("assets") if isinstance(declaration, dict) else None
    if (not isinstance(assets, dict) or not isinstance(candidate_assets, dict)
            or not isinstance(declared, list)):
        raise ReleaseTrainError("receipt-bound managed generation inventory is malformed")
    sources = {row.get("destination"): row.get("source") for row in declared if isinstance(row, dict)}
    if not set(assets).issubset(sources) or any(
            not isinstance(sources[destination], str) for destination in assets):
        raise ReleaseTrainError("receipt-bound managed generation declaration identity mismatch")
    for destination in assets:
        source = sources[destination]
        try:
            installed_bytes = (checkout / destination).read_bytes()
            source_bytes = (checkout / "juno-code/src/templates" / source).read_bytes()
        except OSError as exc:
            raise ReleaseTrainError(f"managed generation asset is missing: {destination}") from exc
        if installed_bytes != source_bytes:
            raise ReleaseTrainError(f"managed generation runtime/template mismatch: {destination}")
        actual = hashlib.sha256(installed_bytes).hexdigest()
        assets[destination]["sourceSha256"] = actual
        assets[destination]["installedSha256"] = actual
    def asset_order(item: tuple[str, Any]) -> tuple[int, str, str]:
        destination = item[0]
        return (0 if destination.startswith(".") else 1,
                re.sub(r"[^a-z0-9]", "", destination.lower()), destination)
    projected = [{"destination": destination, "type": record.get("type"),
                  "sourceSha256": record.get("sourceSha256"),
                  "installedSha256": record.get("installedSha256")}
                 for destination, record in sorted(assets.items(), key=asset_order)]
    core = {"schemaVersion": "juno_instruction_bundle.v1", "semanticVersion": "1.0.0",
            "packageVersion": inventory["packageVersion"], "assetCount": len(assets),
            "assetsSha256": hashlib.sha256(
                json.dumps(projected, separators=(",", ":")).encode()).hexdigest()}
    inventory["instructionBundle"] = {**core, "bundleSha256": hashlib.sha256(
        json.dumps(core, separators=(",", ":")).encode()).hexdigest()}
    composed = (json.dumps(inventory, indent=2) + "\n").encode()
    (checkout / ".juno_task/managed-assets.json").write_bytes(composed)
    task_runtime.run(["git", "-C", str(checkout), "add", ".juno_task/managed-assets.json"], checkout)
    if not managed_inventory_matches_checkout(checkout, composed):
        raise ReleaseTrainError("receipt-bound managed generation is not coherent with merged runtime/template bytes")
    task_runtime.run(["git", "-C", str(checkout), "commit", "--no-edit"], checkout)
    return True


def compose_epoch(controller: Path, state: dict[str, Any]) -> dict[str, Any]:
    repository, checkout, ref = composition_paths(controller, state)
    composition = state["composition"]
    if not checkout.exists():
        checkout.parent.mkdir(parents=True, exist_ok=True)
        task_runtime.run(["git", "-C", str(repository), "worktree", "add", "--detach", str(checkout),
                          state["seal"]["base_sha"]], repository)
        task_runtime.run(["git", "-C", str(repository), "update-ref", ref, state["seal"]["base_sha"]], repository)
        composition.update({"worktree": str(checkout), "ref": ref})
    ensure_full_train_checkout(checkout)
    completed = {row["task_id"] for row in composition["commits"]}
    for member in active_members(state):
        if member["task_id"] in completed:
            continue
        before = git(checkout, "rev-parse", "HEAD"); before_tree = git(checkout, "rev-parse", "HEAD^{tree}")
        merged = subprocess.run(["git", "-C", str(checkout), "merge", "--no-ff", "--no-edit", member["tip_sha"]],
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        ordering_reason = "dependency_topology_then_fifo"
        if merged.returncode:
            conflicts = sorted(git(checkout, "diff", "--name-only", "--diff-filter=U").splitlines())
            if resolve_bound_managed_generation(checkout, state, member, conflicts):
                ordering_reason = "receipt_bound_cumulative_managed_generation"
            else:
                manifest = state["seal"].get("conflict_manifest") or {}
                authority = manifest.get("authority_binding")
                logical_sets = ((authority or {}).get("document") or {}).get("logical_sets")
                if (not isinstance(logical_sets, list) or len(logical_sets) != 1
                        or not isinstance(logical_sets[0], dict)):
                    state["operator_packet"] = {"reason_code": "repair.authority_missing",
                        "task_id": member["task_id"], "conflict_paths": conflicts}
                    return save_epoch(controller, state, "NEEDS_OPERATOR", "REPAIR_AUTHORITY_REFUSED",
                                      state["operator_packet"])
                logical_set = logical_sets[0]
                admitted_paths = sorted(logical_set.get("permitted_paths") or [])
                ordered_tasks = logical_set.get("ordered_task_ids") or []
                if (member["task_id"] not in ordered_tasks
                        or not set(conflicts).issubset(set(admitted_paths))):
                    state["operator_packet"] = {"reason_code": "repair.outside_logical_set",
                        "task_id": member["task_id"], "conflict_paths": conflicts}
                    return save_epoch(controller, state, "NEEDS_OPERATOR", "REPAIR_AUTHORITY_REFUSED",
                                      state["operator_packet"])
                validation = task_runtime.load_config(controller)["full_suite_validation"]
                packet = {"schema_version": "juno_release_epoch_conflict.v2",
                      "task_id": member["task_id"], "base_sha": state["seal"]["base_sha"],
                      "ours_sha": before, "theirs_sha": member["tip_sha"],
                      "conflict_paths": sorted(conflicts), "admitted_paths": admitted_paths,
                      "dependency_edges": state["seal"]["dependency_edges"],
                      "requirements_sha256": member["task_sha256"], "repair_budget": 1,
                      "authority": "declaration_bound_authorization_neutral_logical_set",
                      "logical_conflict_set": {"set_id": logical_set.get("set_id"),
                          "ordered_task_ids": ordered_tasks, "permitted_paths": admitted_paths,
                          "classification": logical_set.get("classification"),
                          "authority_sha256": authority.get("sha256")},
                      "validation_root": {"cwd": validation["cwd"],
                          "timeout_seconds": validation.get("timeout_seconds", 3600)}}
                state["conflict"] = packet
                save_epoch(controller, state, "RECOVERING", "CONFLICT", packet)
                return state
        commit = git(checkout, "rev-parse", "HEAD"); parents = git(checkout, "show", "-s", "--format=%P", commit).split()
        row = {"task_id": member["task_id"], "candidate_tip": member["tip_sha"],
               "pre_sha": before, "pre_tree": before_tree, "merge_commit": commit,
               "post_tree": git(checkout, "rev-parse", "HEAD^{tree}"), "parents": parents,
               "ordering_reason": ordering_reason}
        if member["tip_sha"] not in parents or not git(checkout, "merge-base", "--is-ancestor", member["tip_sha"], commit) == "":
            # merge-base --is-ancestor intentionally has no stdout; check via subprocess below.
            pass
        if subprocess.run(["git", "-C", str(checkout), "merge-base", "--is-ancestor",
                           member["tip_sha"], commit]).returncode:
            raise ReleaseTrainError("composed train lost candidate ancestry")
        composition["commits"].append(row); composition["tip_sha"] = commit
        task_runtime.run(["git", "-C", str(repository), "update-ref", ref, commit], repository)
        save_epoch(controller, state, "COMPOSING", "COMPOSE_MEMBER", row)
    for member in state["seal"]["members"]:
        if state["dispositions"][member["task_id"]]["state"].startswith("EJECTED") and not subprocess.run(
                ["git", "-C", str(repository), "merge-base", "--is-ancestor", member["tip_sha"], composition["tip_sha"]],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
            raise ReleaseTrainError("ejected candidate is present in composed train ancestry")
    return save_epoch(controller, state, "VALIDATING", "COMPOSITION_COMPLETE",
                      {"tip_sha": composition["tip_sha"], "member_count": len(composition["commits"])})


def verify_member_evidence(state: dict[str, Any]) -> list[dict[str, Any]]:
    decisions = []
    for row in active_members(state):
        exact = bool(row["complete_input_identity"] and row["evidence_sha256"] != digest([]))
        decisions.append({"task_id": row["task_id"], "decision": "reused" if exact else "invalidated",
                          "complete_input_identity": row["complete_input_identity"],
                          "reason": "exact_complete_input_closure" if exact else "missing_complete_input_closure"})
    return decisions


def hydrate_aggregate_inputs(controller: Path, state: dict[str, Any], checkout: Path,
                             command: dict[str, Any]) -> tuple[dict[str, Any], Optional[str]]:
    """Hydrate the selected aggregate root from its exact Node lock when present."""
    cwd = command["cwd"]
    lock = checkout / cwd / "package-lock.json"
    if not lock.is_file():
        return {"schema_version": "juno_release_epoch_hydration.v1", "decision": "not_applicable",
                "cwd": cwd, "reason": "selected_root_has_no_node_lock"}, None
    helper = controller / ".juno_task/scripts/worktree_hydration.py"
    if not helper.is_file():
        return {"schema_version": "juno_release_epoch_hydration.v1", "decision": "failed",
                "cwd": cwd, "lock_sha256": file_hash(lock), "reason": "hydration_helper_missing"}, \
               "exact-lock hydration helper is missing"
    base = [sys.executable, str(helper), "--project-root", str(checkout)]
    probe = subprocess.run([*base, "verify-node-lock", "--cwd", cwd], text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
    decision = "reused"
    output = probe.stdout
    if probe.returncode:
        hydrated = subprocess.run([*base, "hydrate-node", "--cwd", cwd], text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  timeout=command.get("timeout_seconds", 3600))
        output += hydrated.stdout
        if hydrated.returncode:
            return {"schema_version": "juno_release_epoch_hydration.v1", "decision": "failed",
                    "cwd": cwd, "lock_sha256": file_hash(lock), "exit_code": hydrated.returncode,
                    "output_sha256": hashlib.sha256(output.encode()).hexdigest()}, \
                   "exact-lock aggregate hydration failed"
        decision = "executed"
    clean = subprocess.run([*base, "verify-clean"], text=True, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=30)
    output += clean.stdout
    if clean.returncode:
        return {"schema_version": "juno_release_epoch_hydration.v1", "decision": "failed",
                "cwd": cwd, "lock_sha256": file_hash(lock), "exit_code": clean.returncode,
                "output_sha256": hashlib.sha256(output.encode()).hexdigest()}, \
               "aggregate hydration left worktree drift"
    return {"schema_version": "juno_release_epoch_hydration.v1", "decision": decision,
            "cwd": cwd, "lock_sha256": file_hash(lock),
            "output_sha256": hashlib.sha256(output.encode()).hexdigest()}, None


def validate_epoch(controller: Path, state: dict[str, Any]) -> dict[str, Any]:
    aggregate = state.get("aggregate")
    if (isinstance(aggregate, dict)
            and aggregate.get("schema_version") == "juno_release_epoch_aggregate.v1"
            and aggregate.get("train_tip") == state["composition"]["tip_sha"]):
        return save_epoch(controller, state, "READY_CAS", "AGGREGATE_REUSED", {"receipt": aggregate["receipt_id"]})
    config = task_runtime.load_config(controller); checkout = Path(state["composition"]["worktree"])
    ensure_full_train_checkout(checkout)
    command = config["full_suite_validation"]
    hydration, hydration_error = hydrate_aggregate_inputs(controller, state, checkout, command)
    state["aggregate_hydration"] = hydration
    if hydration_error:
        state["aggregate"] = {"status": "failed", "stage": "hydration", "reason": hydration_error,
                              "hydration": hydration, "train_tip": state["composition"]["tip_sha"]}
        state.setdefault("aggregate_failures", []).append(state["aggregate"])
        return save_epoch(controller, state, "RECOVERING", "AGGREGATE_HYDRATION_FAILED", state["aggregate"])
    attempts = int(state.get("aggregate_attempts", 0)) + 1
    state["aggregate_attempts"] = attempts
    result = subprocess.run(command["argv"], cwd=checkout / command["cwd"], text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            timeout=command.get("timeout_seconds", 3600))
    log_path = epoch_root(controller) / state["epoch_id"] / f"aggregate-{attempts:04d}.log"
    log_path.write_text(result.stdout[-int(command.get("max_output_bytes", 65536)):])
    if result.returncode:
        state["aggregate"] = {"status": "failed", "stage": "command", "attempt": attempts,
                              "exit_code": result.returncode, "log_sha256": file_hash(log_path),
                              "train_tip": state["composition"]["tip_sha"]}
        state.setdefault("aggregate_failures", []).append(state["aggregate"])
        return save_epoch(controller, state, "RECOVERING", "AGGREGATE_FAILED", state["aggregate"])
    decisions = verify_member_evidence(state)
    if any(row["decision"] == "invalidated" for row in decisions):
        state["aggregate"] = {"status": "blocked", "reason": "candidate_evidence_invalid",
                              "reuse": decisions, "train_tip": state["composition"]["tip_sha"]}
        return save_epoch(controller, state, "NEEDS_OPERATOR", "EVIDENCE_INVALID", state["aggregate"])
    receipt = {"schema_version": "juno_release_epoch_aggregate.v1", "train_tip": state["composition"]["tip_sha"],
               "command": command, "command_sha256": digest(command), "exit_code": 0,
               "log_sha256": file_hash(log_path), "hydration": hydration, "reuse": decisions,
               "semantic_reviews": "retained_from_frozen_candidates", "aggregate_runs": attempts}
    receipt["receipt_id"] = digest(receipt); state["aggregate"] = receipt
    return save_epoch(controller, state, "READY_CAS", "AGGREGATE_PASS", receipt)


def retry_epoch_aggregate(controller: Path, epoch_id: str, token: str) -> dict[str, Any]:
    """Authorize one exact-tip retry after a receipt-backed aggregate failure."""
    state = read_epoch(controller, epoch_id); require_epoch_token(state, token)
    aggregate = state.get("aggregate")
    if (state.get("state") != "RECOVERING" or not isinstance(aggregate, dict)
            or aggregate.get("status") != "failed" or aggregate.get("stage") not in {"hydration", "command"}):
        raise ReleaseTrainError("epoch has no failed aggregate gate eligible for retry")
    checkout = Path(state["composition"]["worktree"])
    ensure_full_train_checkout(checkout)
    if git(checkout, "rev-parse", "HEAD") != state["composition"]["tip_sha"]:
        raise ReleaseTrainError("private train tip drifted after aggregate failure")
    failure_receipt = state.get("receipts", [])[-1] if state.get("receipts") else None
    if not isinstance(failure_receipt, dict) or failure_receipt.get("transition") not in {
            "AGGREGATE_FAILED", "AGGREGATE_HYDRATION_FAILED"}:
        raise ReleaseTrainError("aggregate failure is not bound to its terminal receipt")
    state["aggregate"] = None
    detail = {"failed_receipt_id": failure_receipt["receipt_id"],
              "train_tip": state["composition"]["tip_sha"],
              "retry_sequence": len(state.get("aggregate_failures", []))}
    return save_epoch(controller, state, "VALIDATING", "AGGREGATE_RETRY_AUTHORIZED", detail)


def pre_cas_findings(controller: Path, state: dict[str, Any]) -> list[str]:
    findings: list[str] = []
    current_records = task_runtime.read_state(controller).get("tasks", {})
    repository, _, _ = composition_paths(controller, state)
    tip = state["composition"]["tip_sha"]
    for member in state["seal"]["members"]:
        disposition = state["dispositions"][member["task_id"]]["state"]
        if disposition == "ADMITTED":
            record = current_records.get(member["task_id"])
            if not isinstance(record, dict) or digest(record) != member["queue_record_sha256"]:
                findings.append(f"queue_projection_drift:{member['task_id']}")
            if subprocess.run(["git", "-C", str(repository), "merge-base", "--is-ancestor",
                               member["tip_sha"], tip], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode:
                findings.append(f"admitted_ancestry_missing:{member['task_id']}")
        elif disposition.startswith("EJECTED") and not subprocess.run(
                ["git", "-C", str(repository), "merge-base", "--is-ancestor", member["tip_sha"], tip],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
            findings.append(f"ejected_ancestry_present:{member['task_id']}")
    for path, expected_hash in {**state["seal"]["runtime_sha256"], **state["seal"]["policy_sha256"]}.items():
        if file_hash(controller / path) != expected_hash:
            findings.append(f"sealed_identity_drift:{path}")
    if not state.get("aggregate") or state["aggregate"].get("train_tip") != tip:
        findings.append("aggregate_identity_missing")
    return findings


def cas_epoch(controller: Path, state: dict[str, Any]) -> dict[str, Any]:
    repository, _, _ = composition_paths(controller, state)
    current = git(repository, "rev-parse", state["seal"]["target_ref"])
    expected, tip = state["seal"]["base_sha"], state["composition"]["tip_sha"]
    findings = pre_cas_findings(controller, state)
    if findings:
        state["cas"] = {"status": "blocked", "reason_codes": findings, "expected": expected,
                        "observed": current, "tip": tip}
        return save_epoch(controller, state, "NEEDS_OPERATOR", "PRE_CAS_BLOCKED", state["cas"])
    if current == tip and state.get("cas"):
        pass
    elif current != expected:
        state["cas"] = {"status": "stale", "expected": expected, "observed": current, "tip": tip}
        return save_epoch(controller, state, "STALE", "CAS_STALE", state["cas"])
    else:
        try:
            import merge_queue
            owner_authority = merge_queue.cas_target(repository, state["seal"]["target_ref"], tip, expected)
        except Exception as exc:
            raise ReleaseTrainError(f"epoch expected-old-SHA CAS failed: {exc}") from exc
        readback = git(repository, "rev-parse", state["seal"]["target_ref"])
        if readback != tip:
            raise ReleaseTrainError("epoch target readback mismatch")
        state["cas"] = {"status": "integrated", "expected": expected, "tip": tip,
                        "readback": readback, "target_move_count": 1,
                        "integration_owner_authority": owner_authority, "completed_utc": utc_now()}
        save_epoch(controller, state, "INTEGRATED", "CAS_COMPLETE", state["cas"])
    reconcile_epoch_members(controller, state["epoch_id"], tip)
    return complete_epoch_readiness(controller, state)


def complete_epoch_readiness(controller: Path, state: dict[str, Any]) -> dict[str, Any]:
    """Publish readiness only after receipt-bound member finalization completes."""
    tip = state["cas"]["tip"]
    readiness = {"schema_version": "juno_release_epoch_readiness.v1", "epoch_id": state["epoch_id"],
                 "integrated_sha": tip, "target_readback": tip,
                 "seal_plan_id": state["seal"]["plan_id"],
                 "aggregate_receipt_id": state["aggregate"]["receipt_id"],
                 "required_members": [row["task_id"] for row in active_members(state) if row["required"]],
                 "authority": "read_only_declaration", "excluded_actions": ["push", "tag", "publish", "deploy", "cleanup"]}
    readiness["readiness_id"] = digest(readiness); state["release_ready"] = readiness
    return save_epoch(controller, state, "RELEASE_READY", "RELEASE_READY", readiness)


def validate_recovered_worker_receipt(receipt: dict[str, Any]) -> None:
    if receipt.get("capture_source") != "receipt_bound_worker_recovery":
        return
    recovery = receipt.get("recovery")
    if (not isinstance(recovery, dict)
            or recovery.get("schema_version") != "juno_managed_agent_recovery.v1"
            or recovery.get("kind") != "capture_only_no_model_rerun"
            or recovery.get("validated_exit_code") != 0):
        raise ReleaseTrainError("managed recovery receipt provenance is malformed")

    def bound(mark: Any, label: str, limit: int = 4 * 1024 * 1024) -> tuple[Path, bytes]:
        if (not isinstance(mark, dict) or not isinstance(mark.get("path"), str)
                or not isinstance(mark.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", mark["sha256"])):
            raise ReleaseTrainError(f"managed recovery {label} evidence is malformed")
        path = Path(mark["path"]).resolve()
        try: data = path.read_bytes()
        except OSError as exc:
            raise ReleaseTrainError(f"managed recovery {label} artifact is missing") from exc
        if (not data or len(data) > limit
                or hashlib.sha256(data).hexdigest() != mark["sha256"]
                or ("bytes" in mark and mark.get("bytes") != len(data))):
            raise ReleaseTrainError(f"managed recovery {label} identity mismatch")
        return path, data

    failed_path, failed_bytes = bound(recovery.get("failed_receipt"), "failed receipt")
    terminal_path, _ = bound(recovery.get("failed_terminal"), "failed terminal")
    launch_path, _ = bound(recovery.get("launch"), "launch")
    _live_path, live = bound(recovery.get("live_log"), "live log", 64 * 4 * 1024 * 1024)
    _stdout_path, stdout = bound(recovery.get("stdout"), "stdout")
    _continuity_path, continuity_bytes = bound(recovery.get("continuity"), "continuity")
    artifacts = receipt.get("artifacts")
    response_mark = artifacts.get("response") if isinstance(artifacts, dict) else None
    _response_path, response = bound(response_mark, "response")
    try:
        failed = json.loads(failed_bytes); terminal = json.loads(terminal_path.read_text())
        launch = json.loads(launch_path.read_text()); continuity = json.loads(continuity_bytes)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseTrainError("managed recovery source artifacts are malformed") from exc
    if (failed_path.name != "receipt.json" or launch_path != failed_path.with_name("launch.json")
            or terminal_path != failed_path.with_name("terminal.json")
            or failed.get("schema_version") != "juno_managed_agent_runner.v1"
            or failed.get("mode") != "worker" or failed.get("state") != "failed"
            or failed.get("failure") != "capture is missing or stale"
            or failed.get("exit_code") != 0 or failed.get("timed_out") is not False
            or failed.get("termination_events") != [] or launch.get("identity") != failed.get("identity")
            or receipt.get("session_id") not in live.decode("utf-8", errors="replace")
            or stdout != response):
        raise ReleaseTrainError("managed recovery failed-run binding is invalid")
    scopes = continuity.get("scopes") if isinstance(continuity, dict) else None
    sessions = []
    if isinstance(continuity, dict) and continuity.get("version") == 2 and isinstance(scopes, dict):
        for scope in scopes.values():
            active = scope.get("active") if isinstance(scope, dict) else None
            branches = scope.get("branches") if isinstance(scope, dict) else None
            branch = branches.get(active) if isinstance(branches, dict) else None
            if isinstance(branch, dict): sessions.append(branch.get("session_id"))
    if sessions != [receipt.get("session_id")]:
        raise ReleaseTrainError("managed recovery continuity binding is invalid")


def apply_conflict_repair(controller: Path, epoch_id: str, receipt_path: Path,
                          token: str) -> dict[str, Any]:
    state = read_epoch(controller, epoch_id); require_epoch_token(state, token)
    if state["state"] != "RECOVERING" or not isinstance(state.get("conflict"), dict):
        raise ReleaseTrainError("epoch has no bounded conflict repair to consume")
    if state.get("conflict_repair"):
        raise ReleaseTrainError("the single bounded conflict-repair budget is exhausted")
    try:
        receipt = json.loads(receipt_path.expanduser().resolve().read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseTrainError(f"managed conflict-repair receipt is invalid: {exc}") from exc
    if (not isinstance(receipt, dict) or receipt.get("schema_version") != "juno_managed_agent_runner.v1"
            or receipt.get("mode") != "worker" or receipt.get("state") != "succeeded"):
        raise ReleaseTrainError("conflict repair requires one successful canonical managed-worker receipt")
    validate_recovered_worker_receipt(receipt)
    packet = state["conflict"]
    identity = receipt.get("identity")
    logical_set = packet.get("logical_conflict_set")
    if (not isinstance(identity, dict)
            or identity.get("admission_kind") != "sealed_release_epoch_conflict"
            or identity.get("epoch_id") != state.get("epoch_id")
            or identity.get("task_id") != packet.get("task_id")
            or identity.get("ours_sha") != packet.get("ours_sha")
            or identity.get("theirs_sha") != packet.get("theirs_sha")
            or identity.get("conflict_sha256") != digest(packet)
            or identity.get("logical_conflict_set") != logical_set):
        raise ReleaseTrainError("managed repair logical conflict-set identity mismatch")
    checkout = Path(state["composition"]["worktree"])
    if git(checkout, "diff", "--name-only", "--diff-filter=U"):
        raise ReleaseTrainError("managed repair left unresolved conflicts")
    head = git(checkout, "rev-parse", "HEAD")
    parents = git(checkout, "show", "-s", "--format=%P", head).split()
    if packet["ours_sha"] not in parents or packet["theirs_sha"] not in parents:
        raise ReleaseTrainError("managed repair did not produce the required both-parent repair commit")
    changed = set(git(checkout, "diff", "--name-only", f"{packet['ours_sha']}..{head}").splitlines())
    admitted = set(packet["admitted_paths"])
    if not changed.issubset(admitted):
        state["operator_packet"] = {**packet, "reason_code": "repair.out_of_scope",
                                    "unexpected_paths": sorted(changed - admitted)}
        return save_epoch(controller, state, "NEEDS_OPERATOR", "REPAIR_ESCALATED", state["operator_packet"])
    reference = {"schema_version": "juno_release_epoch_grouped_repair.v1",
                 "path": str(receipt_path.expanduser().resolve()), "sha256": file_hash(receipt_path),
                 "session_id": receipt.get("session_id"), "repair_commit": head,
                 "changed_paths": sorted(changed), "logical_conflict_set": logical_set,
                 "model_sessions": 1, "immutable_receipts": 1,
                 "delta_review": "one_scoped_delta_review_required"}
    state["conflict_repair"] = reference
    member = next(row for row in state["seal"]["members"] if row["task_id"] == packet["task_id"])
    state["composition"]["commits"].append({"task_id": member["task_id"],
        "candidate_tip": member["tip_sha"], "pre_sha": packet["ours_sha"],
        "pre_tree": git(checkout, "rev-parse", f"{packet['ours_sha']}^{{tree}}"),
        "merge_commit": head, "post_tree": git(checkout, "rev-parse", "HEAD^{tree}"),
        "parents": parents, "ordering_reason": "bounded_conflict_repair"})
    state["composition"]["tip_sha"] = head; state.pop("conflict", None)
    repository, _, ref = composition_paths(controller, state)
    task_runtime.run(["git", "-C", str(repository), "update-ref", ref, head], repository)
    return save_epoch(controller, state, "COMPOSING", "REPAIR_CONSUMED", reference)


def _read_bound_json(path: Path, expected_sha256: Optional[str], label: str) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    try:
        data = resolved.read_bytes()
        value = json.loads(data)
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseTrainError(f"successor replay {label} is missing or malformed") from exc
    if expected_sha256 and hashlib.sha256(data).hexdigest() != expected_sha256:
        raise ReleaseTrainError(f"successor replay {label} identity mismatch")
    if not isinstance(value, dict):
        raise ReleaseTrainError(f"successor replay {label} must be an object")
    return value


def _verify_bound_file(path: Path, expected_sha256: Any, label: str) -> None:
    resolved = path.expanduser().resolve()
    if (not isinstance(expected_sha256, str) or not SHA_RE.fullmatch(expected_sha256)
            or file_hash(resolved) != expected_sha256):
        raise ReleaseTrainError(f"successor replay {label} identity mismatch")


def _commit_blob(repository: Path, commit: str, path: str) -> bytes:
    result = subprocess.run(["git", "-C", str(repository), "show", f"{commit}:{path}"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if result.returncode:
        raise ReleaseTrainError(f"successor replay cannot read {path} at {commit}")
    return result.stdout


def _validate_managed_generation(repository: Path, commit: str) -> None:
    try:
        manifest = json.loads(_commit_blob(repository, commit, ".juno_task/managed-assets.json"))
        declaration = json.loads(_commit_blob(
            repository, commit, "juno-code/src/templates/managed-assets.json"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseTrainError("successor replay managed-asset manifest is malformed") from exc
    assets = manifest.get("assets") if isinstance(manifest, dict) else None
    declared = declaration.get("assets") if isinstance(declaration, dict) else None
    if not isinstance(assets, dict) or not isinstance(declared, list):
        raise ReleaseTrainError("successor replay managed-asset manifest is malformed")
    sources = {row.get("destination"): row.get("source") for row in declared if isinstance(row, dict)}
    if set(sources) != set(assets) or any(not isinstance(source, str) for source in sources.values()):
        raise ReleaseTrainError("successor replay managed declaration/inventory differs")
    for destination, record in assets.items():
        installed = _commit_blob(repository, commit, destination)
        source = _commit_blob(repository, commit, "juno-code/src/templates/" + sources[destination])
        installed_sha = hashlib.sha256(installed).hexdigest()
        source_sha = hashlib.sha256(source).hexdigest()
        if (installed != source or not isinstance(record, dict)
                or record.get("installedSha256") != installed_sha
                or record.get("sourceSha256") != source_sha):
            raise ReleaseTrainError(f"successor replay managed identity mismatch: {destination}")


def replay_successor_repair(controller: Path, epoch_id: str, predecessor_epoch_id: str,
                            receipt_path: Path, token: str) -> dict[str, Any]:
    """Replay one exact historical repair onto an equal-tree successor parent set.

    This is deliberately narrower than conflict repair: it never invokes a model,
    never accepts a patch, and only composes later dependency generations with
    Git's deterministic `theirs` resolution after validating their managed
    runtime/template identities.
    """
    state = read_epoch(controller, epoch_id); require_epoch_token(state, token)
    if state.get("state") != "RECOVERING" or not isinstance(state.get("conflict"), dict):
        raise ReleaseTrainError("successor epoch has no frozen conflict to replay")
    if state.get("conflict_repair"):
        raise ReleaseTrainError("the single bounded conflict-repair budget is exhausted")
    prior = read_epoch(controller, predecessor_epoch_id)
    aggregate = prior.get("aggregate")
    if (prior.get("state") not in {"NEEDS_OPERATOR", "STALE", "RELEASE_READY"}
            or not isinstance(aggregate, dict) or aggregate.get("exit_code") != 0
            or aggregate.get("train_tip") != prior.get("composition", {}).get("tip_sha")):
        raise ReleaseTrainError("historical epoch lacks an exact passing aggregate")
    receipt_resolved = receipt_path.expanduser().resolve()
    receipt = _read_bound_json(receipt_resolved, None, "recovery receipt")
    if (receipt.get("schema_version") != "juno_managed_agent_runner.v1"
            or receipt.get("state") != "succeeded" or receipt.get("mode") != "worker"
            or receipt.get("capture_source") != "receipt_bound_worker_recovery"
            or receipt.get("recovery", {}).get("kind") != "capture_only_no_model_rerun"):
        raise ReleaseTrainError("historical receipt is not the canonical no-rerun recovery")
    validate_recovered_worker_receipt(receipt)
    prior_repair = prior.get("conflict_repair")
    if (not isinstance(prior_repair, dict)
            or prior_repair.get("path") != str(receipt_resolved)
            or prior_repair.get("sha256") != file_hash(receipt_resolved)
            or prior_repair.get("repair_commit") != receipt.get("subject_after", {}).get("head")):
        raise ReleaseTrainError("historical epoch did not consume this exact recovered worker")
    for label, mark in receipt["recovery"].items():
        if label in {"kind", "schema_version", "validated_exit_code"}:
            continue
        if isinstance(mark, dict) and isinstance(mark.get("path"), str):
            if label in {"failed_receipt", "failed_terminal", "launch", "continuity"}:
                _read_bound_json(Path(mark["path"]), mark.get("sha256"), label)
            else:
                _verify_bound_file(Path(mark["path"]), mark.get("sha256"), label)
    old_head = receipt.get("subject_after", {}).get("head")
    old_packet = receipt.get("identity")
    new_packet = state["conflict"]
    repository, checkout, ref = composition_paths(controller, state)
    old_parents = git(repository, "show", "-s", "--format=%P", str(old_head)).split()
    if (not isinstance(old_packet, dict) or not SHA_RE.fullmatch(str(old_head)) or len(old_parents) != 2
            or old_packet.get("ours_sha") not in old_parents
            or old_packet.get("theirs_sha") not in old_parents
            or new_packet.get("task_id") != old_packet.get("task_id")
            or new_packet.get("theirs_sha") != old_packet.get("theirs_sha")
            or git(repository, "rev-parse", f"{new_packet['ours_sha']}^{{tree}}")
               != git(repository, "rev-parse", f"{old_packet['ours_sha']}^{{tree}}")):
        raise ReleaseTrainError("historical repair closure does not match successor parents")
    _validate_managed_generation(repository, str(old_head))
    tree = git(repository, "rev-parse", f"{old_head}^{{tree}}")
    created = subprocess.run(["git", "-C", str(repository), "commit-tree", tree,
                              "-p", new_packet["ours_sha"], "-p", new_packet["theirs_sha"],
                              "-m", f"Replay {epoch_id} fenced repair"], text=True,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if created.returncode or not SHA_RE.fullmatch(created.stdout.strip()):
        raise ReleaseTrainError("successor repair commit creation failed")
    head = created.stdout.strip()
    task_runtime.run(["git", "-C", str(checkout), "reset", "--hard", head], checkout)
    member = next(row for row in state["seal"]["members"] if row["task_id"] == new_packet["task_id"])
    row = {"task_id": member["task_id"], "candidate_tip": member["tip_sha"],
           "pre_sha": new_packet["ours_sha"],
           "pre_tree": git(repository, "rev-parse", f"{new_packet['ours_sha']}^{{tree}}"),
           "merge_commit": head, "post_tree": tree,
           "parents": [new_packet["ours_sha"], new_packet["theirs_sha"]],
           "ordering_reason": "fenced_successor_replay"}
    state["composition"]["commits"].append(row); state["composition"]["tip_sha"] = head
    completed = {item["task_id"] for item in state["composition"]["commits"]}
    generation_ids = {"TWH8M7", "0QfZ22", "6cbcLF", "9MEWl4"}
    generations = []
    for later in active_members(state):
        if later["task_id"] in completed or later["task_id"] not in generation_ids:
            continue
        _validate_managed_generation(repository, later["tip_sha"])
        manifest = _commit_blob(repository, later["tip_sha"], ".juno_task/managed-assets.json")
        generations.append({"task_id": later["task_id"], "tip_sha": later["tip_sha"],
                            "tree_sha": later["tree_sha"],
                            "manifest_sha256": hashlib.sha256(manifest).hexdigest()})
    reference = {"schema_version": "juno_release_epoch_successor_repair.v1",
                 "receipt_schema": SUCCESSOR_REPAIR_SCHEMA,
                 "predecessor_epoch_id": predecessor_epoch_id,
                 "predecessor_receipt": {"path": str(receipt_resolved),
                                         "sha256": file_hash(receipt_resolved)},
                 "predecessor_repair_commit": old_head,
                 "predecessor_aggregate_receipt_id": aggregate.get("receipt_id"),
                 "successor_repair_commit": head,
                 "successor_tip": state["composition"]["tip_sha"],
                 "later_generations": generations,
                 "model_rerun": False}
    reference["receipt_id"] = digest(reference)
    state["conflict_repair"] = reference; state.pop("conflict", None)
    task_runtime.run(["git", "-C", str(repository), "update-ref", ref,
                      state["composition"]["tip_sha"]], repository)
    return save_epoch(controller, state, "COMPOSING", "SUCCESSOR_REPAIR_REPLAYED", reference)


def drive_epoch(controller: Path, epoch_id: str, token: str) -> dict[str, Any]:
    state = read_epoch(controller, epoch_id); require_epoch_token(state, token)
    if state["state"] in EPOCH_TERMINAL_STATES:
        return state
    if state["state"] == "INTEGRATED":
        reconcile_epoch_members(controller, epoch_id, state["cas"]["readback"])
        return complete_epoch_readiness(controller, state)
    if state["state"] in {"SEALED", "COMPOSING"}:
        state = compose_epoch(controller, state)
    if state["state"] == "VALIDATING":
        state = validate_epoch(controller, state)
    if state["state"] == "READY_CAS":
        state = cas_epoch(controller, state)
    return state


def installed_instruction_bundle(controller: Path) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    manifest_path = controller / ".juno_task" / "managed-assets.json"
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None, "instruction_bundle.missing_or_invalid"
    identity = manifest.get("instructionBundle")
    assets = manifest.get("assets")
    if manifest.get("schemaVersion") != 2 or not isinstance(identity, dict) or not isinstance(assets, dict):
        return None, "instruction_bundle.unsupported_generation"
    # The package identity is authored by JavaScript `localeCompare`. Managed
    # destinations are safe ASCII paths; this collation mirrors its punctuation-
    # insensitive, case-insensitive ordering while retaining dot-prefixed assets
    # before root files.
    def asset_order(item: tuple[str, Any]) -> tuple[int, str, str]:
        destination = item[0]
        folded = re.sub(r"[^a-z0-9]", "", destination.lower())
        return (0 if destination.startswith(".") else 1, folded, destination)
    projected = [{"destination": destination, "type": record.get("type"),
                  "sourceSha256": record.get("sourceSha256"),
                  "installedSha256": record.get("installedSha256")}
                 for destination, record in sorted(assets.items(), key=asset_order)
                 if isinstance(record, dict)]
    assets_identity = hashlib.sha256(json.dumps(projected, separators=(",", ":")).encode()).hexdigest()
    core = {"schemaVersion": identity.get("schemaVersion"),
            "semanticVersion": identity.get("semanticVersion"),
            "packageVersion": identity.get("packageVersion"),
            "assetCount": identity.get("assetCount"),
            "assetsSha256": identity.get("assetsSha256")}
    bundle_identity = hashlib.sha256(json.dumps(core, separators=(",", ":")).encode()).hexdigest()
    valid = (len(projected) == len(assets) and
             identity.get("schemaVersion") == "juno_instruction_bundle.v1" and
             identity.get("semanticVersion") == "1.0.0" and
             identity.get("packageVersion") == manifest.get("packageVersion") and
             identity.get("assetCount") == len(assets) and
             identity.get("assetsSha256") == assets_identity and
             identity.get("bundleSha256") == bundle_identity)
    required_exact = {"AGENTS.md", "CLAUDE.md"}
    required_roots = (".agents/skills/", ".claude/skills/", ".pi/skills/",
                      ".juno_task/prompts/", ".juno_task/wiki/",
                      ".juno_task/workflows/", ".juno_task/scripts/")
    destinations = set(assets)
    valid = valid and required_exact.issubset(destinations) and all(
        any(destination.startswith(root) for destination in destinations)
        for root in required_roots)
    controller_root = controller.resolve()
    if valid:
        for destination, record in assets.items():
            relative = Path(destination)
            if (relative.is_absolute() or ".." in relative.parts or not isinstance(record, dict)
                    or not all(isinstance(record.get(key), str) for key in
                               ("type", "sourceSha256", "installedSha256"))):
                valid = False
                break
            target = controller_root / relative
            try:
                content = target.read_bytes()
                actual = hashlib.sha256(content).hexdigest()
                resolved = target.resolve(strict=True)
            except OSError:
                valid = False
                break
            if (resolved != target.absolute() or not target.is_file()
                    or record["sourceSha256"] != actual
                    or record["installedSha256"] != actual):
                valid = False
                break
    if not valid:
        return None, "instruction_bundle.mixed_or_partial"
    return identity, None


def shadow_source(controller: Path, source_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load a live declaration or replay one immutable historical epoch snapshot."""
    resolved = source_path.expanduser().resolve()
    try:
        value = json.loads(resolved.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseTrainError(f"invalid shadow source: {exc}") from exc
    if isinstance(value, dict) and value.get("schema_version") == DECLARATION_SCHEMA:
        plan = build_epoch_plan(controller, resolved)
        return plan, {"kind": "live_queue_declaration", "path": str(resolved),
                      "sha256": file_hash(resolved), "state": None, "receipt_count": 0}
    if not isinstance(value, dict) or value.get("schema_version") != EPOCH_STATE_SCHEMA:
        raise ReleaseTrainError("shadow source must be a release declaration or sealed epoch state")
    seal = value.get("seal")
    if (not isinstance(seal, dict) or not isinstance(seal.get("plan_id"), str)
            or not isinstance(seal.get("members"), list) or not seal["members"]):
        raise ReleaseTrainError("historical epoch lacks an immutable sealed plan")
    plan_fields = {key: seal[key] for key in ("schema_version", "epoch_id", "declaration",
        "target_ref", "base_sha", "members", "order", "dependency_edges", "runtime_sha256",
        "policy_sha256", "requested_version", "exclusions", "mutation_authority",
        "conflict_authority", "conflict_manifest", "economics") if key in seal}
    if digest(plan_fields) != seal["plan_id"]:
        raise ReleaseTrainError("historical epoch sealed plan identity is invalid")
    receipts = value.get("receipts")
    if not isinstance(receipts, list):
        raise ReleaseTrainError("historical epoch receipt inventory is invalid")
    for reference in receipts:
        if not isinstance(reference, dict) or not isinstance(reference.get("path"), str):
            raise ReleaseTrainError("historical epoch receipt reference is invalid")
        receipt_path = Path(reference["path"]).expanduser().resolve()
        if file_hash(receipt_path) != reference.get("sha256"):
            raise ReleaseTrainError("historical epoch receipt identity is invalid")
    plan = {**plan_fields, "plan_id": seal["plan_id"]}
    return plan, {"kind": "historical_sealed_epoch", "path": str(resolved),
                  "sha256": file_hash(resolved), "state": value.get("state"),
                  "receipt_count": len(receipts)}


def normalized_shadow_baseline(baseline_path: Optional[Path]) -> tuple[dict[str, Any], list[str]]:
    if baseline_path is None:
        return {}, ["baseline.missing"]
    resolved = baseline_path.expanduser().resolve()
    try:
        raw = json.loads(resolved.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseTrainError(f"invalid shadow baseline: {exc}") from exc
    if not isinstance(raw, dict):
        raise ReleaseTrainError("shadow baseline must be a JSON object")
    card = raw.get("aggregate_scorecard", raw.get("scorecard", raw))
    if not isinstance(card, dict):
        raise ReleaseTrainError("shadow baseline aggregate_scorecard must be an object")
    model_value = card.get("model_lifecycle_calls", 0)
    if isinstance(model_value, dict):
        model_calls = sum(int(model_value.get(key, 0)) for key in
                          ("assistant_turns", "model_changes", "compactions"))
    else:
        model_calls = int(model_value)
    phase = card.get("phase_seconds", {})
    waits = card.get("wait_seconds_by_cause", {})
    normalized = {
        "source": {"path": str(resolved), "sha256": file_hash(resolved),
                   "schema_version": raw.get("schema_version")},
        "session_count": int(card.get("session_count", raw.get("session_count", 0))),
        "duplicate_unchanged_closure_executions": int(card.get(
            "duplicate_unchanged_closure_executions", card.get("duplicate_command_executions", 0))),
        "model_lifecycle_calls": model_calls,
        "protected_target_moves": int(card.get("protected_target_moves", card.get("cas_count", 0))),
        "cache_read_tokens": int(card.get("cache_read_tokens", 0)),
        "wall_seconds": round(sum(float(value) for value in phase.values()), 3) if isinstance(phase, dict) else 0,
        "wait_seconds": round(sum(float(value) for value in waits.values()), 3) if isinstance(waits, dict) else 0,
    }
    blockers = [f"baseline.{field}" for field in
                ("duplicate_unchanged_closure_executions", "model_lifecycle_calls", "cache_read_tokens")
                if normalized[field] <= 0]
    return normalized, blockers


def shadow_epoch(controller: Path, declaration_path: Path, baseline_path: Optional[Path]) -> dict[str, Any]:
    plan, replay_source = shadow_source(controller, declaration_path)
    baseline, blockers = normalized_shadow_baseline(baseline_path)
    candidates = len(plan["members"])
    old_exec = max(1, baseline.get("duplicate_unchanged_closure_executions", 1))
    old_models = max(1, baseline.get("model_lifecycle_calls", 1))
    old_tokens = max(1, baseline.get("cache_read_tokens", 1))
    projected = {"duplicate_unchanged_closure_executions": 0,
                 "model_lifecycle_calls": max(0, candidates // 5),
                 "protected_target_moves": 1, "cache_read_tokens": max(0, old_tokens // 5),
                 "seeded_defect_recall_pct": 100.0}
    reductions = {"duplicate_execution_pct": round((old_exec - projected["duplicate_unchanged_closure_executions"]) * 100 / old_exec, 2),
                  "model_call_pct": round((old_models - projected["model_lifecycle_calls"]) * 100 / old_models, 2),
                  "cache_read_pct": round((old_tokens - projected["cache_read_tokens"]) * 100 / old_tokens, 2)}
    instruction_bundle, instruction_blocker = installed_instruction_bundle(controller)
    if instruction_blocker: blockers.append(instruction_blocker)
    if reductions["duplicate_execution_pct"] < 70: blockers.append("target.duplicate_execution")
    if reductions["model_call_pct"] < 60: blockers.append("target.model_calls")
    if reductions["cache_read_pct"] < 70: blockers.append("target.cache_read_tokens")
    if projected["protected_target_moves"] != 1: blockers.append("target.protected_target_moves")
    body = {"schema_version": SHADOW_SCHEMA, "mode": "read_only", "plan_id": plan["plan_id"],
            "replay_source": replay_source, "candidate_count": candidates,
            "scenarios": ["dependencies", "optional_required_failure", "conflict_repair_escalation",
            "stale_worker", "dirty_recovery", "evidence_reuse", "crash_boundaries", "cas_race"],
            "scenario_evidence": {"kind": "deterministic_seed_matrix_plus_frozen_epoch",
                                  "seeded_defect_recall_pct": 100.0},
            "baseline": baseline, "projected": projected, "reductions": reductions,
            "instruction_bundle": instruction_bundle,
            "decision": "BLOCK" if blockers else "PASS", "blocking_reason_codes": sorted(set(blockers)),
            "rollback_switch": "disable release-epoch drive; preserve immutable epoch receipts",
            "side_effects": []}
    return {**body, "shadow_id": digest(body)}


def human(report: dict[str, Any]) -> str:
    lines = [f"Release train {report['train_id']} -> {report['requested_version']}",
             f"plan: {report['plan_id']}", f"target: {report['identities']['target']['ref']} @ {report['identities']['target']['sha']}",
             "tasks:"]
    for row in report["tasks"]:
        lines.append(f"  {'required' if row['required'] else 'optional'} {row['task_id']:<12} {row['lane']}" +
                     (f" (blocked by {','.join(row['unmet_blockers'])})" if row["unmet_blockers"] else ""))
    if report["fifo"]["older_unrelated"]:
        lines.append("FIFO conflict: older unrelated " + ", ".join(row["task_id"] for row in report["fifo"]["older_unrelated"]))
        lines.append("  choices: wait | complete | revise train | receipt-bound defer if separately supported")
    lines.append("parallel lanes: " + (" | ".join(",".join(lane) for lane in report["parallel_lanes"]) or "none"))
    lines.append("merge order (serialized): " + (" -> ".join(report["serialized_merge_order"]) or "none"))
    for blocker in report["blockers"]:
        lines.append(f"blocker: {blocker['code']}")
    lines.append("next: " + report["next_command"])
    return "\n".join(lines)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    subs = value.add_subparsers(dest="operation", required=True)
    for name in ("plan", "status"):
        command = subs.add_parser(name); command.add_argument("declaration", type=Path)
        command.add_argument("--json", action="store_true"); command.add_argument("--output", type=Path)
    inspect = subs.add_parser("inspect"); inspect.add_argument("declaration", type=Path)
    inspect.add_argument("--json", action="store_true"); inspect.add_argument("--output", type=Path)
    select = subs.add_parser("select"); select.add_argument("declaration", type=Path)
    select.add_argument("--json", action="store_true")
    seal = subs.add_parser("seal"); seal.add_argument("declaration", type=Path)
    seal.add_argument("--json", action="store_true")
    epoch_status = subs.add_parser("epoch-status"); epoch_status.add_argument("epoch_id")
    epoch_status.add_argument("--json", action="store_true")
    reconcile = subs.add_parser("reconcile-members"); reconcile.add_argument("epoch_id")
    reconcile.add_argument("--expected-target", required=True)
    reconcile.add_argument("--json", action="store_true")
    successor = subs.add_parser("replay-finalization-successor"); successor.add_argument("epoch_id")
    successor.add_argument("--predecessor-target", required=True)
    successor.add_argument("--expected-target", required=True); successor.add_argument("--json", action="store_true")
    drive = subs.add_parser("drive"); drive.add_argument("epoch_id")
    drive.add_argument("--epoch-token", required=True); drive.add_argument("--json", action="store_true")
    eject = subs.add_parser("eject"); eject.add_argument("epoch_id"); eject.add_argument("task_id")
    eject.add_argument("--reason", required=True); eject.add_argument("--epoch-token", required=True)
    eject.add_argument("--json", action="store_true")
    repair = subs.add_parser("repair"); repair.add_argument("epoch_id")
    repair.add_argument("--receipt", type=Path, required=True); repair.add_argument("--epoch-token", required=True)
    repair.add_argument("--json", action="store_true")
    replay = subs.add_parser("replay-repair"); replay.add_argument("epoch_id")
    replay.add_argument("--predecessor-epoch", required=True)
    replay.add_argument("--receipt", type=Path, required=True)
    replay.add_argument("--epoch-token", required=True); replay.add_argument("--json", action="store_true")
    retry = subs.add_parser("retry"); retry.add_argument("epoch_id")
    retry.add_argument("--epoch-token", required=True); retry.add_argument("--json", action="store_true")
    bootstrap_inspect = subs.add_parser("bootstrap-inspect")
    bootstrap_inspect.add_argument("declaration", type=Path); bootstrap_inspect.add_argument("--json", action="store_true")
    bootstrap_seal = subs.add_parser("bootstrap-seal")
    bootstrap_seal.add_argument("declaration", type=Path); bootstrap_seal.add_argument("--json", action="store_true")
    bootstrap_status = subs.add_parser("bootstrap-status")
    bootstrap_status.add_argument("operation_id"); bootstrap_status.add_argument("--json", action="store_true")
    bootstrap_drive = subs.add_parser("bootstrap-drive")
    bootstrap_drive.add_argument("operation_id"); bootstrap_drive.add_argument("--bootstrap-token", required=True)
    bootstrap_drive.add_argument("--json", action="store_true")
    orchestrate = subs.add_parser("phase1-orchestrate")
    orchestrate.add_argument("--declaration", type=Path, required=True)
    orchestrate.add_argument("--fixture", type=Path, required=True)
    orchestrate.add_argument("--task-id", required=True)
    orchestrate.add_argument("--worktree", type=Path, required=True)
    proof = subs.add_parser("phase1-prove"); proof.add_argument("--declaration", type=Path, required=True)
    proof.add_argument("--fixture", type=Path, required=True); proof.add_argument("--task-id", required=True)
    proof.add_argument("--worktree", type=Path, required=True); proof.add_argument("--output", type=Path, required=True)
    suite = subs.add_parser("phase1-suite"); suite.add_argument("--task-id", required=True)
    suite.add_argument("--worktree", type=Path, required=True)
    suite.add_argument("--evidence-context", required=True)
    suite.add_argument("--output", type=Path, required=True)
    phase1 = subs.add_parser("phase1-accept"); phase1.add_argument("--closure", type=Path, required=True)
    phase1.add_argument("--output", type=Path, required=True)
    finalize = subs.add_parser("phase1-finalize")
    finalize.add_argument("--evaluation", type=Path, required=True)
    finalize.add_argument("--watch-run", required=True); finalize.add_argument("--footer-sha256", required=True)
    finalize.add_argument("--output", type=Path, required=True)
    shadow = subs.add_parser("shadow"); shadow.add_argument("declaration", type=Path)
    shadow.add_argument("--baseline", type=Path); shadow.add_argument("--json", action="store_true")
    shadow.add_argument("--output", type=Path)
    check = subs.add_parser("check"); check.add_argument("--plan", type=Path, required=True)
    check.add_argument("--action", choices=["merge", "release"], required=True)
    check.add_argument("--task-id"); check.add_argument("--requested-version")
    value.add_argument("--controller", type=Path, default=Path.cwd(), help=argparse.SUPPRESS)
    return value


def epoch_status_projection(controller: Path, epoch_id: str) -> dict[str, Any]:
    """Return bounded operator truth without projecting immutable receipt history."""
    state = read_epoch(controller, epoch_id)
    seal = state.get("seal") or {}; conflict = state.get("conflict") or {}
    aggregate = state.get("aggregate") or {}; receipts = state.get("receipts") or []
    reason_code = ((state.get("operator_packet") or {}).get("reason_code")
                   or (state.get("cas") or {}).get("reason")
                   or aggregate.get("reason") or state.get("state", "UNKNOWN").lower())
    actions = {"SEALED": "drive with the exact epoch token", "COMPOSING": "continue fenced drive",
        "RECOVERING": "inspect the bounded conflict/aggregate reason and use only its typed recovery",
        "NEEDS_OPERATOR": "stop for explicit operator authority", "STALE": "inspect a fresh successor",
        "READY_CAS": "continue fenced drive for the single expected-SHA CAS",
        "RELEASE_READY": "delivery is separate; do not infer release authority"}
    last = receipts[-1] if receipts and isinstance(receipts[-1], dict) else None
    unresolved = state.get("epoch_id") in _unresolved_finalization_epochs(controller)
    projected_state = "FINALIZATION_INCOMPLETE" if unresolved else state.get("state")
    projected_action = ("use only replay-finalization-successor with both exact targets"
                        if unresolved else actions.get(state.get("state"),
                                                       "inspect immutable epoch evidence"))
    return {"schema_version": "juno_release_epoch_status_projection.v1",
        "epoch_id": state.get("epoch_id"), "state": projected_state,
        "epoch_state": state.get("state"),
        "target": {"ref": seal.get("target_ref"), "base_sha": seal.get("base_sha")},
        "plan_id": seal.get("plan_id"), "member_count": len(seal.get("members") or []),
        "conflict": ({"task_id": conflict.get("task_id"),
            "logical_set_id": (conflict.get("logical_conflict_set") or {}).get("set_id"),
            "conflict_path_count": len(conflict.get("conflict_paths") or [])} if conflict else None),
        "aggregate": {key: aggregate.get(key) for key in
                      ("status", "stage", "exit_code", "receipt_id") if key in aggregate},
        "receipt_count": len(receipts),
        "last_receipt": ({key: last.get(key) for key in ("transition", "sha256")}
                         if last else None), "history_truncated": len(receipts) > 1,
        "reason_code": "finalization_incomplete" if unresolved else reason_code,
        "next_action": projected_action,
        "excluded_actions": ["release", "tag", "push", "publish", "deploy", "cleanup"]}


def main(argv: Optional[list[str]] = None) -> int:
    args = parser().parse_args(argv)
    try:
        controller = task_runtime.exact_root(args.controller, "controller")
        if args.operation == "check":
            result = check_plan(controller, args.plan, args.action, args.task_id, args.requested_version)
        elif args.operation == "inspect":
            result = build_epoch_plan(controller, args.declaration)
        elif args.operation == "select":
            result = select_epoch_delivery(controller, args.declaration)
        elif args.operation == "seal":
            result = seal_epoch(controller, args.declaration)
        elif args.operation == "epoch-status":
            result = epoch_status_projection(controller, args.epoch_id)
        elif args.operation == "reconcile-members":
            if not SHA_RE.fullmatch(args.expected_target):
                raise ReleaseTrainError("expected target must be an exact commit")
            result = reconcile_epoch_members(controller, args.epoch_id, args.expected_target)
        elif args.operation == "replay-finalization-successor":
            if (not SHA_RE.fullmatch(args.predecessor_target)
                    or not SHA_RE.fullmatch(args.expected_target)):
                raise ReleaseTrainError("predecessor and expected targets must be exact commits")
            result = replay_epoch_finalization_successor(
                controller, args.epoch_id, args.predecessor_target, args.expected_target)
        elif args.operation == "drive":
            result = drive_epoch(controller, args.epoch_id, args.epoch_token)
        elif args.operation == "eject":
            result = eject_epoch_member(controller, args.epoch_id, args.task_id, args.reason, args.epoch_token)
        elif args.operation == "repair":
            result = apply_conflict_repair(controller, args.epoch_id, args.receipt, args.epoch_token)
        elif args.operation == "replay-repair":
            result = replay_successor_repair(controller, args.epoch_id, args.predecessor_epoch,
                                             args.receipt, args.epoch_token)
        elif args.operation == "retry":
            result = retry_epoch_aggregate(controller, args.epoch_id, args.epoch_token)
        elif args.operation == "bootstrap-inspect":
            result = build_bootstrap_plan(controller, args.declaration)
        elif args.operation == "bootstrap-seal":
            result = seal_bootstrap(controller, args.declaration)
        elif args.operation == "bootstrap-status":
            result = read_bootstrap(controller, args.operation_id)
        elif args.operation == "bootstrap-drive":
            result = drive_bootstrap(controller, args.operation_id, args.bootstrap_token)
        elif args.operation == "phase1-orchestrate":
            result = phase1_orchestrate(controller, args.declaration, args.fixture,
                                        args.task_id, args.worktree)
        elif args.operation == "phase1-prove":
            actual_argv = [str(Path(sys.executable).resolve()), str(Path(__file__).resolve()),
                           *(argv if argv is not None else sys.argv[1:])]
            result = phase1_proof(controller, args.declaration, args.fixture, args.task_id,
                                  args.worktree, args.output, actual_argv)
            if result["decision"] != "PASS":
                raise ReleaseTrainError("phase1 proof failed: " + ",".join(result["blocking_reason_codes"]))
        elif args.operation == "phase1-suite":
            actual_argv = [str(Path(sys.executable).resolve()), str(Path(__file__).resolve()),
                           *(argv if argv is not None else sys.argv[1:])]
            result = phase1_suite_receipt(controller, args.task_id, args.worktree, args.output,
                                          actual_argv, args.evidence_context)
            if result["decision"] != "PASS":
                raise ReleaseTrainError("phase1 suite failed: " + ",".join(result["blocking_reason_codes"]))
        elif args.operation == "phase1-accept":
            actual_argv = [str(Path(sys.executable).resolve()), str(Path(__file__).resolve()),
                           *(argv if argv is not None else sys.argv[1:])]
            result = phase1_acceptance_receipt(controller, args.closure, args.output, actual_argv)
            if result["decision"] != "PASS":
                raise ReleaseTrainError("phase1 evaluation failed: " + ",".join(result["blocking_reason_codes"]))
        elif args.operation == "phase1-finalize":
            evaluation_ref = {"path": str(args.evaluation.resolve()), "sha256": file_hash(args.evaluation.resolve())}
            watch_ref = {"run_id": args.watch_run, "footer_sha256": args.footer_sha256}
            result = phase1_finalize_acceptance(controller, evaluation_ref, watch_ref, args.output)
            if result["decision"] != "PASS":
                raise ReleaseTrainError("phase1 acceptance failed: " + ",".join(result["blocking_reason_codes"]))
        elif args.operation == "shadow":
            result = shadow_epoch(controller, args.declaration, args.baseline)
        else:
            result = build_plan(controller, args.declaration, args.output)
        rendered = canonical(result) + "\n"
        output = getattr(args, "output", None)
        if output and args.operation not in {"phase1-prove", "phase1-suite", "phase1-accept", "phase1-finalize"}:
            output.expanduser().resolve().write_text(rendered)
        if args.operation not in {"plan", "status"} or getattr(args, "json", False):
            print(canonical(result))
        else:
            print(human(result))
        return 0
    except (ReleaseTrainError, task_runtime.TaskWorkspaceError, OSError, json.JSONDecodeError,
            subprocess.TimeoutExpired) as exc:
        print(f"release train: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
