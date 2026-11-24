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
from unittest.mock import patch
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
        fake.BOUNDED_STATE_SCHEMA = "juno_task_workspace_state.v2"
        fake.TERMINAL_LIFECYCLE_STATES = {"MERGED", "WITHDRAWN"}
        fake.archive_terminal_transition = self.archive_terminal_transition
        self.archived = []
        self.config = {"target_ref": "refs/heads/target", "cleanup": {"after_merge": False, "disposable_paths": []}}
        fake.load_config = lambda _controller: self.config
        fake.worktree_path = lambda _config, task: self.root / task
        fake.observe_task_identity = self.observe_identity
        self.cold = {}
        fake.state_archive_get = lambda _controller, task, _ref, **_kw: {"record": self.cold[task]}
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

    def archive_terminal_transition(self, _controller: Path, _state: dict,
                                    task_id: str, record: dict) -> dict:
        self.archived.append(task_id)
        self.cold[task_id] = json.loads(json.dumps(record))
        return {"schema_version": "juno_task_terminal_tombstone.v1",
                "task_id": task_id, "state": record["state"], "tip_sha": record.get("tip_sha"),
                "archive": {"cold_ref": "refs/cold"}}

    def observe_identity(self, record, repository, config, task, *, expected_state="WORKING"):
        if record.get("state") != expected_state:
            raise self.runtime.task_runtime.TaskWorkspaceError("state drift")
        path = self.root / task
        if record.get("worktree") != str(path) or command(path, "symbolic-ref", "HEAD") != f"refs/heads/{task}":
            raise self.runtime.task_runtime.TaskWorkspaceError("identity drift")
        return repository, path, command(path, "rev-parse", "HEAD")

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
                                     "tip_sha": tip, "branch_ref": f"refs/heads/{task}",
                                     "worktree": str(self.root / task), "fencing": {"state": "RELEASED"}}

    def close(self) -> None:
        self.temp.cleanup()


class NativeDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.f = RuntimeFixture()

    def tearDown(self) -> None:
        self.f.close()

    def cleanup_source(self):
        source = self.f.branch("TASK")
        tip = commit(source, "source", ".gitignore", "node_modules\nlocal.env\n")
        self.f.queue("TASK", tip)
        return source, tip

    def test_cleanup_default_preserves_and_preview_apply_are_idempotent(self):
        source, tip = self.cleanup_source()
        result = self.f.runtime.land(self.f.controller, "TASK")
        self.assertEqual(result["cleanup"], {"status": "disabled"})
        preview = self.f.runtime.cleanup(self.f.controller, "TASK")
        self.assertEqual([row["status"] for row in preview["worktrees"]], ["eligible", "eligible"])
        self.assertTrue(source.exists())
        applied = self.f.runtime.cleanup(self.f.controller, "TASK", apply=True)
        self.assertTrue(all(row["status"] == "removed" for row in applied["worktrees"]))
        self.assertTrue(Path(applied["receipt"]["path"]).exists())
        self.assertEqual(command(self.f.repository, "rev-parse", "TASK"), tip)
        again = self.f.runtime.cleanup(self.f.controller, "TASK", apply=True)
        self.assertTrue(all(row["reason"] == "already_removed" for row in again["worktrees"]))

    def test_cleanup_enabled_removes_only_declared_ignored_dependencies(self):
        source, _ = self.cleanup_source()
        dependencies = source / "node_modules"
        dependencies.mkdir(); (dependencies / "package.js").write_text("dependency")
        self.f.config["cleanup"] = {"after_merge": True, "disposable_paths": ["node_modules"]}
        result = self.f.runtime.land(self.f.controller, "TASK")
        self.assertEqual(result["cleanup"]["worktrees"][0]["status"], "removed")
        self.assertFalse(source.exists())

    def test_cleanup_environment_overrides_config_and_invalid_refuses_before_merge(self):
        source, _ = self.cleanup_source()
        self.f.config["cleanup"]["after_merge"] = True
        with patch.dict("os.environ", {"YYLO_WORKTREE_CLEANUP_AFTER_MERGE": "invalid"}):
            with self.assertRaisesRegex(self.f.runtime.DeliveryError, "true or false"):
                self.f.runtime.land(self.f.controller, "TASK")
        self.assertEqual(command(self.f.repository, "rev-parse", "target"), self.f.base)
        with patch.dict("os.environ", {"YYLO_WORKTREE_CLEANUP_AFTER_MERGE": "false"}):
            self.assertEqual(self.f.runtime.land(self.f.controller, "TASK")["cleanup"]["status"], "disabled")
        self.assertTrue(source.exists())
        self.f.config["cleanup"]["after_merge"] = False
        with patch.dict("os.environ", {"YYLO_WORKTREE_CLEANUP_AFTER_MERGE": "true"}):
            self.f.runtime.land(self.f.controller, "TASK")
        self.assertFalse(source.exists())

    def test_cleanup_preserves_unknown_ignored_dirty_and_new_commits(self):
        source, _ = self.cleanup_source()
        self.f.runtime.land(self.f.controller, "TASK")
        local = source / "local.env"; local.write_text("valuable")
        def reason():
            return self.f.runtime.cleanup(self.f.controller, "TASK", apply=True)["worktrees"][0]["reason"]
        self.assertEqual(reason(), "unknown_ignored_content")
        self.assertEqual(local.read_text(), "valuable")
        local.unlink()
        (source / "shared.txt").write_text("uncommitted")
        self.assertEqual(reason(), "dirty_worktree")
        commit(source, "new work", "new.txt", "new")
        self.assertEqual(reason(), "current_head_not_in_target")

    def test_cleanup_preserves_live_lease_locked_worktree_and_changed_candidate(self):
        source, _ = self.cleanup_source()
        self.f.runtime.land(self.f.controller, "TASK")
        record = self.f.state["tasks"]["TASK"]
        record["fencing"]["state"] = "ACTIVE"
        with self.assertRaisesRegex(self.f.runtime.DeliveryError, "released"):
            self.f.runtime.cleanup(self.f.controller, "TASK", apply=True)
        record["fencing"]["state"] = "RELEASED"
        command(self.f.repository, "worktree", "lock", str(source))
        result = self.f.runtime.cleanup(self.f.controller, "TASK")
        self.assertEqual(result["worktrees"][0]["reason"], "locked_or_prunable")
        candidate = Path(result["worktrees"][1]["path"])
        commit(candidate, "candidate new work", "extra.txt", "new")
        result = self.f.runtime.cleanup(self.f.controller, "TASK", apply=True)
        self.assertIn("candidate_head_changed", [row.get("reason") for row in result["worktrees"]])
        self.assertTrue(candidate.exists())

    def test_cleanup_uses_archived_record_and_project_retry_does_not_remerge(self):
        source, _ = self.cleanup_source()
        self.f.state["schema_version"] = "juno_task_workspace_state.v2"
        self.f.runtime.land(self.f.controller, "TASK")
        before = command(self.f.repository, "rev-parse", "target")
        self.f.config["cleanup"]["after_merge"] = True
        self.f.runtime.project(self.f.controller, "TASK")
        self.assertFalse(source.exists())
        self.assertEqual(command(self.f.repository, "rev-parse", "target"), before)

    def test_cleanup_preserves_untracked_operation_symlink_and_protected_paths(self):
        source, _ = self.cleanup_source()
        self.f.runtime.land(self.f.controller, "TASK")
        def reason():
            return self.f.runtime.cleanup(self.f.controller, "TASK")["worktrees"][0]["reason"]
        extra = source / "untracked.txt"; extra.write_text("keep")
        self.assertEqual(reason(), "dirty_worktree")
        extra.unlink()
        marker = Path(command(source, "rev-parse", "--path-format=absolute", "--git-path", "MERGE_HEAD"))
        marker.write_text(self.f.base + "\n")
        self.assertEqual(reason(), "git_operation_in_progress")
        marker.unlink()
        with patch.object(Path, "cwd", return_value=source):
            self.assertEqual(reason(), "protected_worktree")
        outside = self.f.root / "outside"; outside.mkdir()
        (source / "node_modules").symlink_to(outside, target_is_directory=True)
        self.f.config["cleanup"]["disposable_paths"] = ["node_modules"]
        self.assertEqual(reason(), "ignored_path_symlink")
        self.assertTrue(outside.exists())

    def test_cleanup_preserves_candidate_with_protected_role(self):
        self.cleanup_source()
        self.f.runtime.land(self.f.controller, "TASK")
        preview = self.f.runtime.cleanup(self.f.controller, "TASK")
        candidate = Path(preview["worktrees"][1]["path"])
        command(self.f.repository, "config", "extensions.worktreeConfig", "true")
        command(candidate, "config", "--worktree", "juno.workspace.role", "integration-owner")
        result = self.f.runtime.cleanup(self.f.controller, "TASK", apply=True)
        self.assertEqual(result["worktrees"][1]["reason"], "protected_workspace_role")
        self.assertTrue(candidate.exists())

    def test_cleanup_projection_failure_preserves_worktrees_until_retry(self):
        source, _ = self.cleanup_source()
        self.f.config["cleanup"]["after_merge"] = True
        original = self.f.runtime.task_runtime.project_kanban_lifecycle
        def fail_done(*args, **kwargs):
            if args[2] == "MERGED":
                raise self.f.runtime.task_runtime.KanbanSyncError("projection unavailable")
            return original(*args, **kwargs)
        with patch.object(self.f.runtime.task_runtime, "project_kanban_lifecycle", side_effect=fail_done):
            with self.assertRaises(self.f.runtime.DeliveryError):
                self.f.runtime.land(self.f.controller, "TASK")
        self.assertTrue(source.exists())
        self.assertEqual(self.f.state["tasks"]["TASK"]["state"], "GIT_INTEGRATED")
        self.f.runtime.project(self.f.controller, "TASK")
        self.assertFalse(source.exists())

    def test_cleanup_refuses_target_rewind_and_preserves_initialized_submodules(self):
        source, _ = self.cleanup_source()
        self.f.runtime.land(self.f.controller, "TASK")
        integrated = command(self.f.repository, "rev-parse", "target")
        command(self.f.repository, "update-ref", "refs/heads/target", self.f.base)
        with self.assertRaisesRegex(self.f.runtime.DeliveryError, "ancestry"):
            self.f.runtime.cleanup(self.f.controller, "TASK", apply=True)
        self.assertTrue(source.exists())
        command(self.f.repository, "update-ref", "refs/heads/target", integrated)
        original = self.f.runtime.git
        def with_submodule(repository, *args, **kwargs):
            if repository == source and args == ("submodule", "status"):
                return " " + self.f.base + " dependency (heads/main)"
            return original(repository, *args, **kwargs)
        with patch.object(self.f.runtime, "git", side_effect=with_submodule):
            result = self.f.runtime.cleanup(self.f.controller, "TASK", apply=True)
        self.assertEqual(result["worktrees"][0]["reason"], "initialized_submodules")
        self.assertTrue(source.exists())

    def test_cleanup_malformed_receipt_does_not_fail_completed_delivery(self):
        source, _ = self.cleanup_source()
        landed = self.f.runtime.land(self.f.controller, "TASK")
        Path(landed["receipt"]["path"]).write_text("[]")
        self.f.config["cleanup"]["after_merge"] = True
        retried = self.f.runtime.land(self.f.controller, "TASK")
        self.assertEqual(retried["outcome"], "ALREADY_PROJECTED")
        self.assertEqual(retried["cleanup"]["status"], "failed")
        self.assertTrue(Path(retried["cleanup"]["receipt"]["path"]).exists())
        self.assertEqual(self.f.state["tasks"]["TASK"]["state"], "MERGED")
        self.assertTrue(source.exists())

    def test_cleanup_failure_does_not_fail_delivery(self):
        self.cleanup_source()
        self.f.config["cleanup"]["after_merge"] = True
        with patch.object(self.f.runtime, "cleanup", side_effect=OSError("disk busy")):
            result = self.f.runtime.land(self.f.controller, "TASK")
        self.assertEqual(result["ledger"]["status"], "complete")
        self.assertEqual(result["cleanup"]["status"], "failed")
        self.assertTrue(Path(result["cleanup"]["receipt"]["path"]).exists())

    def test_bounded_terminal_projection_archives_before_tombstone(self) -> None:
        self.f.state["schema_version"] = "juno_task_workspace_state.v2"
        expected = {"task_id": "TASK", "state": "GIT_INTEGRATED", "tip_sha": "a" * 40}
        self.f.state["tasks"]["TASK"] = expected
        self.f.runtime.persist_task(
            self.f.controller, "TASK", expected,
            {**expected, "state": "MERGED", "integrated_sha": "b" * 40})
        self.assertEqual(self.f.archived, ["TASK"])
        self.assertEqual(self.f.state["tasks"]["TASK"]["schema_version"],
                         "juno_task_terminal_tombstone.v1")

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
