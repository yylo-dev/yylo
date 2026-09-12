#!/usr/bin/env python3
"""Focused real-Git tests for the one-task delivery adapter."""
from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[1] / "merge_queue.py"


def command(root: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], text=True,
                            capture_output=True, check=False)
    if check and result.returncode:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def commit(root: Path, message: str, relative: str, content: str) -> str:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    command(root, "add", ".")
    command(root, "commit", "-m", message)
    return command(root, "rev-parse", "HEAD")


class RuntimeFixture:
    def __init__(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="yylo-native-delivery-")
        self.root = Path(self.temp.name)
        self.repository = self.root / "repository"
        self.controller = self.root / "controller"
        self.repository.mkdir()
        (self.controller / ".juno_task/scripts").mkdir(parents=True)
        command(self.repository, "init", "-q")
        command(self.repository, "config", "user.name", "Fixture")
        command(self.repository, "config", "user.email", "fixture@invalid")
        self.base = commit(self.repository, "base", "shared.txt", "base\n")
        command(self.repository, "branch", "target", self.base)
        self.state = {"schema_version": "juno_task_workspace_state.v1",
                      "tasks": {}, "queues": {}}
        fake = types.ModuleType("task_workspace")
        fake.TaskWorkspaceError = type("TaskWorkspaceError", (RuntimeError,), {})
        fake.KanbanSyncError = type("KanbanSyncError", (RuntimeError,), {})
        fake.KANBAN_LIFECYCLE_PROJECTION = "juno_lifecycle_kanban_projection.v1"
        fake.load_config = lambda _controller: {"target_ref": "refs/heads/target"}
        fake.product_repository = lambda _controller, _config: self.repository
        fake.read_state = lambda _controller: json.loads(json.dumps(self.state))
        fake.write_state = self.write_state
        fake.state_lock = lambda _controller: contextlib.nullcontext()
        self.board = {"status": "in_progress", "agent_response": "",
                      "commit_hash": None, "fields": {}}
        fake.read_kanban_task = lambda _controller, _task: json.loads(json.dumps(self.board))
        fake.kanban_board_revision = lambda _controller, _task: "a" * 64
        fake.project_kanban_lifecycle = self.project_kanban_lifecycle
        sys.modules["task_workspace"] = fake
        spec = importlib.util.spec_from_file_location(f"merge_queue_fixture_{id(self)}", RUNTIME)
        self.runtime = importlib.util.module_from_spec(spec)
        assert spec.loader
        spec.loader.exec_module(self.runtime)

    def write_state(self, _controller: Path, state: dict) -> None:
        self.state = json.loads(json.dumps(state))

    def project_kanban_lifecycle(self, _controller: Path, _task: str, lifecycle_state: str,
                                 **kwargs: object) -> dict[str, object]:
        expected = "done" if lifecycle_state == "MERGED" else "in_progress"
        commit_hash = kwargs.get("commit_hash")
        if lifecycle_state == "MERGED" and not kwargs.get("allow_done"):
            raise self.runtime.task_runtime.KanbanSyncError("done projection refused")
        self.board["status"] = expected
        self.board["fields"] = {
            "lifecycle_projection": "juno_lifecycle_kanban_projection.v1",
            "lifecycle_state": lifecycle_state,
        }
        if isinstance(commit_hash, str):
            self.board["commit_hash"] = commit_hash
        return {"schema_version": "juno_task_kanban_sync.v1", "outcome": "projected",
                "board_status": expected, "receipt": {"path": "ledger", "sha256": "a" * 64}}

    def branch(self, name: str, *, start: str | None = None) -> Path:
        worktree = self.root / name
        command(self.repository, "worktree", "add", "-q", "-b", name,
                str(worktree), start or self.base)
        return worktree

    def target(self, sha: str, old: str | None = None) -> None:
        args = ["update-ref", "refs/heads/target", sha]
        if old:
            args.append(old)
        command(self.repository, *args)

    def queue(self, task: str, tip: str) -> None:
        self.state["tasks"][task] = {"task_id": task, "state": "QUEUED",
                                     "tip_sha": tip, "branch_ref": f"refs/heads/{task}"}

    def close(self) -> None:
        self.temp.cleanup()


class NativeDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.f = RuntimeFixture()

    def tearDown(self) -> None:
        self.f.close()

    def test_clean_divergent_merge_preserves_both_sides_and_projects_ledger(self) -> None:
        source = self.f.branch("TASK")
        tip = commit(source, "source", "source.txt", "source\n")
        target = self.f.branch("target-side")
        target_tip = commit(target, "target", "target.txt", "target\n")
        self.f.target(target_tip, self.f.base)
        self.f.queue("TASK", tip)

        result = self.f.runtime.land(self.f.controller, "TASK")

        self.assertEqual(result["outcome"], "GIT_INTEGRATED")
        self.assertEqual(result["ledger"]["status"], "complete")
        self.assertEqual(self.f.state["tasks"]["TASK"]["state"], "MERGED")
        self.assertEqual(self.f.board["status"], "done")
        tree = command(self.f.repository, "ls-tree", "-r", "--name-only", "target")
        self.assertIn("source.txt", tree)
        self.assertIn("target.txt", tree)

    def test_conflict_is_private_and_does_not_block_unrelated_task(self) -> None:
        x = self.f.branch("X")
        x_tip = commit(x, "x", "shared.txt", "x\n")
        y = self.f.branch("Y")
        y_tip = commit(y, "y", "y.txt", "y\n")
        target = self.f.branch("target-side")
        target_tip = commit(target, "target", "shared.txt", "target\n")
        self.f.target(target_tip, self.f.base)
        self.f.queue("X", x_tip)
        self.f.queue("Y", y_tip)

        conflict = self.f.runtime.land(self.f.controller, "X")
        landed = self.f.runtime.land(self.f.controller, "Y")

        self.assertEqual(conflict["outcome"], "CONFLICT")
        self.assertTrue(Path(conflict["candidate_path"]).is_dir())
        self.assertEqual(landed["outcome"], "GIT_INTEGRATED")
        self.assertEqual(self.f.state["tasks"]["X"]["state"], "CONFLICT")
        self.assertEqual(self.f.state["tasks"]["Y"]["state"], "MERGED")

    def test_competing_expected_old_update_rejects_stale_candidate(self) -> None:
        x = self.f.branch("X")
        x_tip = commit(x, "x", "x.txt", "x\n")
        y = self.f.branch("Y")
        y_tip = commit(y, "y", "y.txt", "y\n")
        self.f.queue("X", x_tip)
        self.f.queue("Y", y_tip)
        observed = command(self.f.repository, "rev-parse", "target")
        x_candidate = self.f.runtime.compose if hasattr(self.f.runtime, "compose") else None
        first = self.f.runtime.land(self.f.controller, "X")
        with self.assertRaisesRegex(self.f.runtime.DeliveryError, "TARGET_MOVED"):
            self.f.runtime.land(self.f.controller, "Y", candidate_sha=y_tip,
                                expected_target=observed)
        self.assertEqual(command(self.f.repository, "rev-parse", "target"),
                         first["git"]["integrated_sha"])
        self.assertIsNone(x_candidate)

    def test_target_move_requires_recomposition(self) -> None:
        source = self.f.branch("TASK")
        tip = commit(source, "source", "source.txt", "source\n")
        mover = self.f.branch("mover")
        moved = commit(mover, "move", "moved.txt", "moved\n")
        self.f.queue("TASK", tip)
        observed = command(self.f.repository, "rev-parse", "target")
        self.f.target(moved, observed)
        with self.assertRaisesRegex(self.f.runtime.DeliveryError, "TARGET_MOVED"):
            self.f.runtime.land(self.f.controller, "TASK", candidate_sha=tip,
                                expected_target=observed)
        self.assertEqual(command(self.f.repository, "rev-parse", "target"), moved)

    def test_dirty_source_bytes_and_already_contained_retry_are_preserved(self) -> None:
        source = self.f.branch("TASK")
        tip = commit(source, "source", "source.txt", "source\n")
        dirty = source / "dirty.txt"
        dirty.write_text("uncommitted\n")
        before = command(source, "status", "--porcelain=v1")
        self.f.queue("TASK", tip)
        first = self.f.runtime.land(self.f.controller, "TASK")
        second = self.f.runtime.land(self.f.controller, "TASK")
        self.assertEqual(first["outcome"], "GIT_INTEGRATED")
        self.assertEqual(second["outcome"], "ALREADY_PROJECTED")
        self.assertEqual(command(source, "status", "--porcelain=v1"), before)
        self.assertEqual(dirty.read_text(), "uncommitted\n")

    def test_ledger_failure_occurs_after_git_and_does_not_repeat_integration(self) -> None:
        source = self.f.branch("TASK")
        tip = commit(source, "source", "source.txt", "source\n")
        self.f.queue("TASK", tip)
        original = self.f.runtime.task_runtime.project_kanban_lifecycle

        def fail_projection(*_args: object, **_kwargs: object) -> dict[str, object]:
            raise self.f.runtime.task_runtime.KanbanSyncError("ledger-unavailable")

        self.f.runtime.task_runtime.project_kanban_lifecycle = fail_projection
        with self.assertRaisesRegex(self.f.runtime.DeliveryError, "ledger-unavailable"):
            self.f.runtime.land(self.f.controller, "TASK")
        target = command(self.f.repository, "rev-parse", "target")
        self.assertEqual(self.f.state["tasks"]["TASK"]["state"], "GIT_INTEGRATED")
        self.assertEqual(self.f.runtime.status(self.f.controller, "TASK")["tasks"][0]["next_command"],
                         "yy merge project TASK")
        self.f.runtime.task_runtime.project_kanban_lifecycle = original
        repaired = self.f.runtime.project(self.f.controller, "TASK")
        self.assertEqual(repaired["ledger"]["status"], "complete")
        self.assertEqual(command(self.f.repository, "rev-parse", "target"), target)

    def test_project_repairs_stale_merged_board_with_exact_integration_commit(self) -> None:
        source = self.f.branch("TASK")
        tip = commit(source, "source", "source.txt", "source\n")
        self.f.queue("TASK", tip)
        landed = self.f.runtime.land(self.f.controller, "TASK")
        integrated = landed["git"]["integrated_sha"]
        later = self.f.branch("later", start=integrated)
        later_tip = commit(later, "later", "later.txt", "later\n")
        self.f.target(later_tip, integrated)
        self.f.board.update({"status": "in_progress", "commit_hash": None, "fields": {}})

        repaired = self.f.runtime.project(self.f.controller, "TASK")

        self.assertEqual(repaired["git"]["integrated_sha"], integrated)
        self.assertEqual(self.f.board["commit_hash"], integrated)
        self.assertEqual(self.f.board["status"], "done")

    def test_checked_out_target_is_refused_without_mutation(self) -> None:
        source = self.f.branch("TASK")
        tip = commit(source, "source", "source.txt", "source\n")
        holder = self.root_holder = self.f.root / "holder"
        command(self.f.repository, "worktree", "add", "-q", str(holder), "target")
        self.f.queue("TASK", tip)
        before = command(self.f.repository, "rev-parse", "target")
        with self.assertRaisesRegex(self.f.runtime.DeliveryError, "target ref is checked out"):
            self.f.runtime.land(self.f.controller, "TASK")
        self.assertEqual(command(self.f.repository, "rev-parse", "target"), before)


if __name__ == "__main__":
    unittest.main()
