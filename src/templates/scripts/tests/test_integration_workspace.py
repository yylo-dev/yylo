#!/usr/bin/env python3
"""Real-Git contracts for guarded integration-owner synchronization."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "integration_workspace.py"
sys.path.insert(0, str(SCRIPT.parent))
import integration_workspace as runtime  # noqa: E402
try:
    _fixture = runtime.task_workspace.load_package_bound_test_fixture(
        __file__, "real_git_fixture.py")
except runtime.task_workspace.TaskWorkspaceError as exc:
    print(f"integration workspace test setup: {exc}", file=sys.stderr)
    raise SystemExit(2)
install_juno_admission_fixture = _fixture.install_juno_admission_fixture


def run(argv: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, cwd=cwd, text=True, capture_output=True)
    if check and result.returncode:
        raise AssertionError(result.stderr or result.stdout)
    return result


def git(root: Path, *args: str) -> str:
    return run(["git", "-C", str(root), *args], root).stdout.strip()


class IntegrationWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_refresh_patcher = mock.patch.object(
            runtime, "managed_runtime_refresh",
            return_value={"schema_version": "juno_managed_controller_runtime.v1",
                          "outcome": "completed"},
        )
        self.runtime_inspect_patcher = mock.patch.object(
            runtime, "managed_runtime_inspect",
            return_value={"schema_version": "juno_managed_controller_runtime.v1",
                          "operation": "doctor", "healthy": True, "findings": []},
        )
        self.runtime_refresh = self.runtime_refresh_patcher.start()
        self.runtime_inspect = self.runtime_inspect_patcher.start()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.remote = self.root / "remote.git"
        self.repo = self.root / "repo"
        self.controller = self.root / "controller"
        self.owner = self.root / "integration"
        git(self.root, "init", "--bare", str(self.remote))
        git(self.root, "init", "-b", "product", str(self.repo))
        git(self.repo, "config", "user.email", "test@example.com")
        git(self.repo, "config", "user.name", "Test")
        (self.repo / "src").mkdir()
        (self.repo / "src/base.txt").write_text("base\n")
        task_runtime = SCRIPT.parent / "task_workspace.py"
        install_juno_admission_fixture(self.repo, task_runtime.read_bytes())
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "base")
        self.base = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "remote", "add", "origin", str(self.remote))
        git(self.repo, "push", "-u", "origin", "product")
        git(self.repo, "branch", "controller")
        git(self.repo, "worktree", "add", str(self.controller), "controller")
        git(self.repo, "switch", "--detach")
        git(self.repo, "worktree", "add", "--detach", str(self.owner), self.base)
        git(self.repo, "config", "extensions.worktreeConfig", "true")
        git(self.owner, "config", "--worktree", "juno.workspace.role", "integration-owner")
        git(self.owner, "config", "--worktree", "juno.workspace.roleAuthority", runtime.AUTHORITY)
        git(self.owner, "config", "--worktree", "juno.workspace.roleBase", self.base)
        config = self.controller / ".juno_task/config"
        config.mkdir(parents=True)
        (config / "task-workspace.json").write_text(json.dumps({
            "schema_version": "juno_task_workspace_config.v1", "repository": ".",
            "target_ref": "refs/heads/product", "workspace_root": str(self.root / "tasks"),
            "branch_prefix": "refs/heads/task-", "allowed_paths": ["src"],
            "selectable_paths": [],
            "controller_private_paths": [".juno_task/tasks"],
            "focused_validation": [{"id": "ok", "cwd": "src", "argv": ["true"],
                                    "timeout_seconds": 5, "max_output_bytes": 1024}],
            "full_suite_validation": {"id": "all", "cwd": "src", "argv": ["true"],
                                      "timeout_seconds": 5, "max_output_bytes": 1024},
        }))
        (config / "integration-workspace.json").write_text(json.dumps({
            "schema_version": runtime.POLICY_SCHEMA, "remote": "origin",
            "owner_role_authority": runtime.AUTHORITY,
            "receipt_root": ".juno_task/runtime/integration/receipts",
        }))

    def tearDown(self) -> None:
        self.runtime_refresh_patcher.stop()
        self.runtime_inspect_patcher.stop()
        self.temporary.cleanup()

    def remote_advance(self, text: str = "remote") -> str:
        clone = self.root / f"clone-{text}"
        git(self.root, "clone", str(self.remote), str(clone))
        git(clone, "switch", "product")
        git(clone, "config", "user.email", "test@example.com")
        git(clone, "config", "user.name", "Test")
        (clone / "src/remote.txt").write_text(text + "\n")
        git(clone, "add", "src/remote.txt")
        git(clone, "commit", "-m", text)
        git(clone, "push", "origin", "product")
        return git(clone, "rev-parse", "HEAD")

    def local_advance(self) -> str:
        worktree = self.root / "local-advance"
        git(self.repo, "worktree", "add", str(worktree), "product")
        git(worktree, "config", "user.email", "test@example.com")
        git(worktree, "config", "user.name", "Test")
        (worktree / "src/local.txt").write_text("local\n")
        git(worktree, "add", "src/local.txt")
        git(worktree, "commit", "-m", "local")
        value = git(worktree, "rev-parse", "HEAD")
        git(self.repo, "worktree", "remove", str(worktree))
        return value

    def legacy_cache_migration_fixture(
            self, *, retain_writer: bool = False,
            retain_cache: bool = False) -> tuple[str, str, Path]:
        source = self.root / f"legacy-migration-{len(list(self.root.glob('legacy-migration-*')))}"
        git(self.repo, "worktree", "add", str(source), "product")
        script = source / runtime.INSTALL_REQUIREMENTS_PATH
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(
            'VERSION_CHECK_CACHE_DIR="${VERSION_CHECK_CACHE_DIR:-${PWD}/.juno_task}"\n')
        cache = source / runtime.VERSION_CACHE_PATH
        cache.write_text("checked_at=1\n")
        git(source, "add", runtime.INSTALL_REQUIREMENTS_PATH, runtime.VERSION_CACHE_PATH)
        git(source, "commit", "-m", "legacy checkout cache")
        old = git(source, "rev-parse", "HEAD")
        git(self.owner, "switch", "--detach", old)
        git(self.owner, "config", "--worktree", "juno.workspace.roleBase", old)
        if not retain_writer:
            script.write_text("VERSION_CHECK_CACHE_DIR=/external/cache\n")
        if not retain_cache:
            git(source, "rm", runtime.VERSION_CACHE_PATH)
        git(source, "add", runtime.INSTALL_REQUIREMENTS_PATH)
        git(source, "commit", "-m", "remove checkout cache")
        target = git(source, "rev-parse", "HEAD")
        git(source, "switch", "--detach", target)
        runtime.register(self.controller, self.owner)
        return old, target, source

    def legacy_cache_submodule_migration_fixture(
            self, *, admit_target_object: bool) -> tuple[str, str, str]:
        child_remote = self.root / "migration-child.git"
        child_source = self.root / "migration-child-source"
        git(self.root, "init", "--bare", str(child_remote))
        git(self.root, "init", "-b", "main", str(child_source))
        git(child_source, "config", "user.email", "test@example.com")
        git(child_source, "config", "user.name", "Test")
        (child_source / "value.txt").write_text("old\n")
        git(child_source, "add", "value.txt")
        git(child_source, "commit", "-m", "child old")
        child_old = git(child_source, "rev-parse", "HEAD")
        git(child_source, "remote", "add", "origin", str(child_remote))
        git(child_source, "push", "-u", "origin", "main")
        git(child_remote, "symbolic-ref", "HEAD", "refs/heads/main")

        source = self.root / "legacy-submodule-migration"
        git(self.repo, "worktree", "add", str(source), "product")
        run(["git", "-c", "protocol.file.allow=always", "-C", str(source),
             "submodule", "add", str(child_remote), "vendor/child"], source)
        script = source / runtime.INSTALL_REQUIREMENTS_PATH
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(
            'VERSION_CHECK_CACHE_DIR="${VERSION_CHECK_CACHE_DIR:-${PWD}/.juno_task}"\n')
        (source / runtime.VERSION_CACHE_PATH).write_text("checked_at=1\n")
        git(source, "add", ".")
        git(source, "commit", "-m", "legacy cache with child")
        old = git(source, "rev-parse", "HEAD")
        git(self.owner, "switch", "--detach", old)
        run(["git", "-c", "protocol.file.allow=always", "-C", str(self.owner),
             "submodule", "update", "--init", "--recursive"], self.owner)
        git(self.owner, "config", "--worktree", "juno.workspace.roleBase", old)

        (child_source / "value.txt").write_text("target\n")
        git(child_source, "commit", "-am", "child target")
        child_target = git(child_source, "rev-parse", "HEAD")
        git(source / "vendor/child", "fetch", str(child_source), child_target)
        git(source / "vendor/child", "checkout", "--detach", child_target)
        git(source, "add", "vendor/child")
        script.write_text("VERSION_CHECK_CACHE_DIR=/external/cache\n")
        git(source, "rm", runtime.VERSION_CACHE_PATH)
        git(source, "add", runtime.INSTALL_REQUIREMENTS_PATH)
        git(source, "commit", "-m", "advance child and remove checkout cache")
        target = git(source, "rev-parse", "HEAD")
        git(source, "switch", "--detach", target)
        if admit_target_object:
            git(self.owner / "vendor/child", "fetch", str(child_source), child_target)
        runtime.register(self.controller, self.owner)
        self.assertEqual(git(self.owner / "vendor/child", "rev-parse", "HEAD"), child_old)
        return old, target, child_target

    def unpublished_submodule_fixture(self) -> tuple[Path, str, str, str]:
        child_remote = self.root / "child.git"
        child_source = self.root / "child-source"
        git(self.root, "init", "--bare", str(child_remote))
        git(self.root, "init", "-b", "main", str(child_source))
        git(child_source, "config", "user.email", "test@example.com")
        git(child_source, "config", "user.name", "Test")
        (child_source / "value.txt").write_text("base\n")
        git(child_source, "add", "value.txt")
        git(child_source, "commit", "-m", "child base")
        child_base = git(child_source, "rev-parse", "HEAD")
        git(child_source, "remote", "add", "origin", str(child_remote))
        git(child_source, "push", "-u", "origin", "main")
        git(child_remote, "symbolic-ref", "HEAD", "refs/heads/main")
        (child_source / "value.txt").write_text("advanced\n")
        git(child_source, "commit", "-am", "child advance")
        child_advanced = git(child_source, "rev-parse", "HEAD")
        git(child_source, "push", "origin", "main")

        root_worktree = self.root / "add-push-submodule"
        git(self.repo, "worktree", "add", str(root_worktree), "product")
        git(root_worktree, "config", "user.email", "test@example.com")
        git(root_worktree, "config", "user.name", "Test")
        run(["git", "-c", "protocol.file.allow=always", "-C", str(root_worktree),
             "submodule", "add", str(child_remote), "vendor/child"], root_worktree)
        git(root_worktree, "commit", "-am", "add advanced child")
        root_target = git(root_worktree, "rev-parse", "HEAD")
        git(root_worktree, "switch", "--detach")
        git(self.repo, "config", "protocol.file.allow", "always")
        runtime.register(self.controller, self.owner)
        with mock.patch.dict(os.environ, {"GIT_ALLOW_PROTOCOL": "file"}, clear=False):
            synced, sync_code = runtime.sync(self.controller)
        self.assertEqual(sync_code, 0, synced)
        self.assertEqual(git(self.owner / "vendor/child", "rev-parse", "HEAD"), child_advanced)
        git(child_remote, "update-ref", "refs/heads/main", child_base, child_advanced)
        return child_remote, child_base, child_advanced, root_target

    def test_status_is_offline_and_reports_stale_owner_as_data(self) -> None:
        remote_before = runtime.sha(self.repo, "refs/remotes/origin/product")
        self.remote_advance()
        status = runtime.status_payload(self.controller)
        self.assertTrue(status["offline"])
        self.assertEqual(status["remote"]["sha"], remote_before)
        self.assertEqual(status["integration"]["status"], "unique")
        self.assertTrue(status["integration"]["owner"]["full_checkout"])

    def test_sync_fast_forwards_target_and_detached_owner_with_receipt(self) -> None:
        advanced = self.remote_advance("fast-forward")
        result, code = runtime.sync(self.controller)
        self.assertEqual(code, 0)
        self.assertEqual(result["outcome"], "completed")
        self.assertEqual(git(self.repo, "rev-parse", "product"), advanced)
        self.assertEqual(git(self.owner, "rev-parse", "HEAD"), advanced)
        self.assertEqual(git(self.owner, "config", "--worktree", "--get",
                             "juno.workspace.roleBase"), advanced)
        self.assertNotEqual(run(["git", "-C", str(self.owner), "symbolic-ref", "-q", "HEAD"],
                                self.owner, False).returncode, 0)
        receipt = json.loads(Path(result["receipt"]["path"]).read_text())
        self.assertEqual(receipt["outcome"], "completed")
        self.assertEqual(receipt["phase"], "complete")
        self.runtime_refresh.assert_called_once_with(
            self.controller.resolve(), self.controller.resolve(), self.base, advanced,
            task_id="integration-sync")
        managed_phase = next(row for row in receipt["phases"]
                             if row["phase"] == "managed_runtime")
        self.assertEqual(managed_phase["result"]["outcome"], "completed")

    def test_sync_preserves_local_ahead_target(self) -> None:
        local = self.local_advance()
        result, code = runtime.sync(self.controller)
        self.assertEqual(code, 0)
        self.assertEqual(git(self.repo, "rev-parse", "product"), local)
        target_phase = next(row for row in json.loads(
            Path(result["receipt"]["path"]).read_text())["phases"] if row["phase"] == "target")
        self.assertEqual(target_phase["outcome"], "preserved_local_ahead")
        self.assertEqual(git(self.owner, "rev-parse", "HEAD"), local)
        self.assertEqual(git(self.owner, "config", "--worktree", "--get",
                             "juno.workspace.roleBase"), local)
        self.assertTrue(result["status"]["ready"])
        authority_phase = next(row for row in json.loads(
            Path(result["receipt"]["path"]).read_text())["phases"]
            if row["phase"] == "authority")
        self.assertEqual(authority_phase["after"], local)

    def test_sync_refuses_divergence_and_target_holder(self) -> None:
        self.local_advance()
        self.remote_advance("diverged")
        result, code = runtime.sync(self.controller)
        self.assertEqual(code, 2)
        self.assertIn("diverged", result["error"])
        receipt = json.loads(Path(result["receipt"]["path"]).read_text())
        self.assertEqual(receipt["outcome"], "failed")

        git(self.repo, "update-ref", "refs/heads/product", self.base)
        holder = self.root / "holder"
        git(self.repo, "worktree", "add", str(holder), "product")
        blocked, blocked_code = runtime.sync(self.controller)
        self.assertEqual(blocked_code, 2)
        self.assertIn("target_checked_out", blocked["error"])

    def test_sync_initializes_exact_submodule_and_refuses_dirty_submodule(self) -> None:
        sub_remote = self.root / "sub.git"
        sub_source = self.root / "sub-source"
        git(self.root, "init", "--bare", str(sub_remote))
        git(self.root, "init", "-b", "main", str(sub_source))
        git(sub_source, "config", "user.email", "test@example.com")
        git(sub_source, "config", "user.name", "Test")
        (sub_source / "value.txt").write_text("submodule\n")
        git(sub_source, "add", "value.txt")
        git(sub_source, "commit", "-m", "submodule base")
        sub_sha = git(sub_source, "rev-parse", "HEAD")
        git(sub_source, "remote", "add", "origin", str(sub_remote))
        git(sub_source, "push", "origin", "main")
        git(sub_remote, "symbolic-ref", "HEAD", "refs/heads/main")

        worktree = self.root / "add-submodule"
        git(self.repo, "worktree", "add", str(worktree), "product")
        git(worktree, "config", "user.email", "test@example.com")
        git(worktree, "config", "user.name", "Test")
        git(worktree, "config", "protocol.file.allow", "always")
        run(["git", "-c", "protocol.file.allow=always", "-C", str(worktree), "submodule",
             "add", str(sub_remote), "vendor/sub"], worktree)
        git(worktree, "commit", "-am", "add submodule")
        product_sha = git(worktree, "rev-parse", "HEAD")
        git(worktree, "push", "origin", "product")
        git(worktree, "switch", "--detach")
        git(self.repo, "config", "protocol.file.allow", "always")

        with mock.patch.dict(os.environ, {"GIT_ALLOW_PROTOCOL": "file"}, clear=False):
            result, code = runtime.sync(self.controller)
        self.assertEqual((code, result["outcome"]), (0, "completed"), json.dumps(result))
        self.assertEqual(git(self.repo, "rev-parse", "product"), product_sha)
        self.assertEqual(git(self.owner / "vendor/sub", "rev-parse", "HEAD"), sub_sha)
        self.assertEqual(result["status"]["integration"]["owner"]["submodules"], [{
            "path": "vendor/sub", "sha": sub_sha, "state": "exact",
        }])
        (self.owner / "vendor/sub/value.txt").write_text("dirty\n")
        refused, refused_code = runtime.sync(self.controller)
        self.assertEqual(refused_code, 2)
        self.assertIn("integration_owner_dirty", refused["error"])
        repair, repair_code = runtime.repair(self.controller, dry_run=True, apply=None)
        self.assertEqual((repair_code, repair["outcome"]), (2, "refused"), repair)
        self.assertIn("canonical_integration_owner_not_safe", repair["blockers"])

    def test_sync_preflights_unpublished_gitlink_before_owner_mutation_and_retries(self) -> None:
        child_remote = self.root / "closure-child.git"
        child_source = self.root / "closure-child-source"
        git(self.root, "init", "--bare", str(child_remote))
        git(self.root, "init", "-b", "main", str(child_source))
        git(child_source, "config", "user.email", "test@example.com")
        git(child_source, "config", "user.name", "Test")
        (child_source / "value.txt").write_text("base\n")
        git(child_source, "add", "value.txt")
        git(child_source, "commit", "-m", "child base")
        git(child_source, "remote", "add", "origin", str(child_remote))
        git(child_source, "push", "-u", "origin", "main")
        git(child_remote, "symbolic-ref", "HEAD", "refs/heads/main")
        (child_source / "value.txt").write_text("unpublished\n")
        git(child_source, "commit", "-am", "unpublished child")
        unpublished = git(child_source, "rev-parse", "HEAD")

        target_worktree = self.root / "unpublished-root-source"
        git(self.repo, "worktree", "add", "--detach", str(target_worktree), self.base)
        git(target_worktree, "config", "user.email", "test@example.com")
        git(target_worktree, "config", "user.name", "Test")
        run(["git", "-c", "protocol.file.allow=always", "-C", str(target_worktree),
             "submodule", "add", str(child_remote), "vendor/child"], target_worktree)
        git(target_worktree / "vendor/child", "fetch", str(child_source), unpublished)
        git(target_worktree / "vendor/child", "checkout", "--detach", unpublished)
        git(target_worktree, "add", ".gitmodules", "vendor/child")
        git(target_worktree, "commit", "-m", "root references unpublished child")
        target = git(target_worktree, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/heads/product", target, self.base)
        before_owner = git(self.owner, "rev-parse", "HEAD")

        with mock.patch.dict(os.environ, {"GIT_ALLOW_PROTOCOL": "file"}, clear=False):
            refused, refused_code = runtime.sync(self.controller)
        self.assertEqual((refused_code, refused["outcome"]), (2, "failed"), refused)
        self.assertIn("nested_gitlink_unavailable", refused["error"])
        self.assertIn(unpublished, refused["error"])
        self.assertEqual(git(self.owner, "rev-parse", "HEAD"), before_owner)
        self.assertEqual(git(self.owner, "status", "--porcelain"), "")
        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/product"), target)
        receipt = json.loads(Path(refused["receipt"]["path"]).read_text())
        closure = next(row for row in receipt["phases"]
                       if row["phase"] == "nested_gitlink_closure")
        self.assertFalse(closure["result"]["available"])
        self.assertEqual(receipt["recovery"]["retry"], "yy integration sync")
        self.assertTrue(receipt["recovery"]["owner_unchanged"])

        # Simulate the separately authorized child integration/publication. The
        # supported retry now hydrates the owner without raw object transfer.
        git(child_source, "push", "origin", "main")
        with mock.patch.dict(os.environ, {"GIT_ALLOW_PROTOCOL": "file"}, clear=False):
            retried, retry_code = runtime.sync(self.controller)
        self.assertEqual((retry_code, retried["outcome"]), (0, "completed"), retried)
        self.assertTrue(retried["status"]["ready"])
        self.assertEqual(git(self.owner / "vendor/child", "rev-parse", "HEAD"), unpublished)
        planned, plan_code = runtime.push(self.controller, dry_run=True, apply=None)
        self.assertEqual((plan_code, planned["outcome"]), (0, "planned"), planned)
        self.assertEqual([row["kind"] for row in planned["actions"]], ["push_root"])

    def test_failed_fetch_persists_the_last_completed_phase(self) -> None:
        original = runtime.run

        def fail_fetch(argv: list[str], cwd: Path, *, check: bool = True):
            if "fetch" in argv:
                raise runtime.IntegrationError("injected fetch interruption")
            return original(argv, cwd, check=check)

        with mock.patch.object(runtime, "run", side_effect=fail_fetch):
            result, code = runtime.sync(self.controller)
        self.assertEqual(code, 2)
        receipt = json.loads(Path(result["receipt"]["path"]).read_text())
        self.assertEqual(receipt["outcome"], "failed")
        self.assertEqual(receipt["phase"], "preflight")
        self.assertIn("injected fetch interruption", receipt["error"])

    def test_explicit_registration_selects_canonical_owner_and_reports_extra(self) -> None:
        extra = self.root / "stale-extra"
        git(self.repo, "worktree", "add", "--detach", str(extra), self.base)
        git(extra, "config", "--worktree", "juno.workspace.role", "integration-owner")
        git(extra, "config", "--worktree", "juno.workspace.roleAuthority", runtime.AUTHORITY)
        ambiguous = runtime.status_payload(self.controller)
        self.assertEqual(ambiguous["integration"]["status"], "multiple")
        self.assertIn("integration_owner_multiple",
                      {item["code"] for item in ambiguous["findings"]})

        registered, code = runtime.register(self.controller, self.owner)
        self.assertEqual((code, registered["outcome"]), (0, "completed"))
        status = registered["status"]
        self.assertEqual(status["integration"]["status"], "registered")
        self.assertEqual(status["integration"]["registered_path"], str(self.owner.resolve()))
        self.assertEqual(status["integration"]["owner"]["path"], str(self.owner.resolve()))
        self.assertIn("integration_owner_extra", {item["code"] for item in status["findings"]})
        self.assertTrue(status["healthy"])

        foreign, foreign_code = runtime.register(self.controller, self.root / "not-a-worktree")
        self.assertEqual(foreign_code, 2)
        self.assertEqual(foreign["outcome"], "failed")

    def test_first_run_registration_seeds_exact_identity_routing_and_runtime_idempotently(self) -> None:
        for key in ("juno.workspace.role", "juno.workspace.roleAuthority",
                    "juno.workspace.roleBase"):
            git(self.owner, "config", "--worktree", "--unset-all", key)
        executable = self.root / "cli.mjs"
        executable.write_text("// package runtime\n")

        first, code = runtime.register(
            self.controller, self.owner, runtime_executable=executable,
            runtime_version="0.2.2")
        self.assertEqual((code, first["outcome"]), (0, "completed"), first)
        self.assertTrue(first["status"]["healthy"], first["status"])
        self.assertEqual(set(first["seeded"]), {
            "owner:role", "owner:roleAuthority", "owner:roleBase",
            "controller:role", "controller:roleBase",
            "repository:controller-routing", "controller:runtime",
        })
        self.assertEqual(worktree := git(
            self.owner, "config", "--worktree", "--get", "juno.workspace.role"),
            "integration-owner")
        self.assertEqual(git(self.owner, "config", "--worktree", "--get",
                             "juno.workspace.roleBase"), self.base)
        self.assertEqual(git(self.controller, "config", "--worktree", "--get",
                             "juno.workspace.role"), "controller")
        self.assertEqual(git(self.repo, "config", "--local", "--get",
                             "juno.controller.path"), str(self.controller.resolve()))
        self.assertEqual(git(self.repo, "config", "--local", "--get",
                             "juno.controller.branch"), "refs/heads/controller")
        self.assertEqual(git(self.controller, "config", "--worktree", "--get",
                             "juno.controller.runtimeExecutable"), str(executable.resolve()))
        resolver = SCRIPT.parent / "controller_resolver.py"
        resolver_env = {key: value for key, value in os.environ.items() if key not in {
            "JUNO_TASK_ROOT", "JUNO_CONTROLLER_BRANCH", "JUNO_WORKSPACE_ROLE"}}
        routed = subprocess.run(
            ["python3", str(resolver), "--cwd", str(self.owner), "--format", "json"],
            cwd=self.owner, text=True, capture_output=True, env=resolver_env)
        self.assertEqual(routed.returncode, 0, routed.stderr or routed.stdout)
        routing = json.loads(routed.stdout)
        self.assertTrue(routing["valid"], routing)
        self.assertEqual(routing["path"], str(self.controller.resolve()))

        second, second_code = runtime.register(
            self.controller, self.owner, runtime_executable=executable,
            runtime_version="0.2.2")
        self.assertEqual((second_code, second["seeded"]), (0, []), second)
        self.assertEqual(worktree, "integration-owner")

        git(self.owner, "config", "--worktree", "juno.workspace.roleBase", "HEAD^")
        refused, refused_code = runtime.register(
            self.controller, self.owner, runtime_executable=executable,
            runtime_version="0.2.2")
        self.assertEqual(refused_code, 2)
        self.assertIn("partial, tampered, or stale", refused["error"])
        self.assertEqual(git(self.owner, "config", "--worktree", "--get",
                             "juno.workspace.roleBase"), "HEAD^")
        git(self.owner, "config", "--worktree", "juno.workspace.roleBase", self.base)
        git(self.owner, "config", "--worktree", "juno.workspace.roleAuthority", "unprotected")
        refused, refused_code = runtime.register(
            self.controller, self.owner, runtime_executable=executable,
            runtime_version="0.2.2")
        self.assertEqual(refused_code, 2)
        self.assertIn("partial, tampered, or stale", refused["error"])

    def test_repair_detaches_exact_attached_canonical_owner(self) -> None:
        runtime.register(self.controller, self.owner)
        git(self.owner, "switch", "product")
        before_refs = git(self.repo, "show-ref")

        planned, plan_code = runtime.repair(self.controller, dry_run=True, apply=None)
        self.assertEqual((plan_code, planned["outcome"]), (0, "planned"), planned)
        self.assertEqual([row["kind"] for row in planned["actions"]],
                         ["detach_target_holder"])
        self.assertEqual(git(self.owner, "symbolic-ref", "HEAD"), "refs/heads/product")
        self.assertEqual(git(self.repo, "show-ref"), before_refs)

        applied, apply_code = runtime.repair(
            self.controller, dry_run=False, apply=Path(planned["receipt"]["path"]))
        self.assertEqual((apply_code, applied["outcome"]), (0, "completed"), applied)
        self.assertNotEqual(run(["git", "-C", str(self.owner), "symbolic-ref", "-q", "HEAD"],
                                self.owner, False).returncode, 0)
        self.assertTrue(applied["status"]["healthy"])
        self.assertTrue(applied["status"]["ready"])

    def test_repair_refuses_dirty_or_wrong_base_attached_owner(self) -> None:
        runtime.register(self.controller, self.owner)
        git(self.owner, "switch", "product")
        (self.owner / "src/base.txt").write_text("dirty\n")
        dirty, dirty_code = runtime.repair(self.controller, dry_run=True, apply=None)
        self.assertEqual((dirty_code, dirty["outcome"]), (2, "refused"), dirty)
        self.assertIn("canonical_integration_owner_not_safe", dirty["blockers"])
        git(self.owner, "restore", "src/base.txt")
        git(self.owner, "config", "--worktree", "juno.workspace.roleBase", "HEAD^")
        wrong, wrong_code = runtime.repair(self.controller, dry_run=True, apply=None)
        self.assertEqual((wrong_code, wrong["outcome"]), (2, "refused"), wrong)
        self.assertIn("canonical_integration_owner_not_safe", wrong["blockers"])

    def test_repair_refuses_extra_target_holder(self) -> None:
        advanced = self.local_advance()
        runtime.register(self.controller, self.owner)
        holder = self.root / "target-holder"
        git(self.repo, "worktree", "add", str(holder), "product")
        planned, plan_code = runtime.repair(self.controller, dry_run=True, apply=None)
        self.assertEqual((plan_code, planned["outcome"]), (2, "refused"), planned)
        self.assertIn(f"extra_target_holder:{holder.resolve()}", planned["blockers"])
        self.assertEqual(git(self.owner, "rev-parse", "HEAD"), self.base)
        self.assertEqual(git(holder, "rev-parse", "HEAD"), advanced)

    def test_repair_refuses_tampered_receipt(self) -> None:
        runtime.register(self.controller, self.owner)
        planned, code = runtime.repair(self.controller, dry_run=True, apply=None)
        self.assertEqual(code, 0)
        receipt = Path(planned["receipt"]["path"])
        value = json.loads(receipt.read_text())
        value["registered_owner"] = str(self.root / "attacker")
        receipt.write_text(json.dumps(value))
        applied, apply_code = runtime.repair(self.controller, dry_run=False, apply=receipt)
        self.assertEqual(apply_code, 2)
        self.assertIn("not eligible", applied["error"])

    def test_repair_refuses_plan_identity_drift(self) -> None:
        runtime.register(self.controller, self.owner)
        planned, code = runtime.repair(self.controller, dry_run=True, apply=None)
        self.assertEqual(code, 0)
        self.local_advance()
        applied, apply_code = runtime.repair(
            self.controller, dry_run=False, apply=Path(planned["receipt"]["path"]))
        self.assertEqual(apply_code, 2)
        self.assertIn("identity drifted", applied["error"])

    def test_repair_migrates_clean_stale_owner_when_exact_target_removes_legacy_cache(self) -> None:
        old, target, _ = self.legacy_cache_migration_fixture()

        planned, plan_code = runtime.repair(self.controller, dry_run=True, apply=None)

        self.assertEqual((plan_code, planned["outcome"]), (0, "planned"), planned)
        migration = planned["migration"]
        self.assertTrue(migration["eligible"], migration)
        self.assertEqual(migration["old"]["commit"], old)
        self.assertEqual(migration["target"]["commit"], target)
        self.assertTrue(migration["old"]["legacy_writer"]["present"])
        self.assertTrue(migration["old"]["tracked_cache"]["present"])
        self.assertFalse(migration["target"]["legacy_writer"]["present"])
        self.assertFalse(migration["target"]["tracked_cache"]["present"])
        applied, apply_code = runtime.repair(
            self.controller, dry_run=False, apply=Path(planned["receipt"]["path"]))
        self.assertEqual((apply_code, applied["outcome"]), (0, "completed"), applied)
        self.assertEqual(applied["final_readback"], {
            "head": target, "tree": git(self.repo, "rev-parse", f"{target}^{{tree}}"),
            "role_base": target, "role": "integration-owner",
            "authority": runtime.AUTHORITY, "clean": True, "detached": True,
            "full_checkout": True, "gitlinks": [], "legacy_findings": [], "ready": True,
        })
        self.assertEqual(git(self.owner, "status", "--porcelain"), "")
        self.assertEqual(git(self.owner, "config", "--worktree", "--get",
                             "juno.workspace.roleBase"), target)

    def test_repair_legacy_cache_migration_refuses_stale_owner_or_target_receipt(self) -> None:
        old, target, _ = self.legacy_cache_migration_fixture()
        planned, code = runtime.repair(self.controller, dry_run=True, apply=None)
        self.assertEqual(code, 0, planned)
        git(self.owner, "switch", "--detach", f"{old}^")
        changed, changed_code = runtime.repair(
            self.controller, dry_run=False, apply=Path(planned["receipt"]["path"]))
        self.assertEqual(changed_code, 2)
        self.assertIn("identity drifted", changed["error"])

        git(self.owner, "switch", "--detach", old)
        tree = git(self.repo, "rev-parse", f"{target}^{{tree}}")
        moved = git(self.repo, "commit-tree", tree, "-p", target, "-m", "target drift")
        git(self.repo, "update-ref", "refs/heads/product", moved, target)
        drifted, drifted_code = runtime.repair(
            self.controller, dry_run=False, apply=Path(planned["receipt"]["path"]))
        self.assertEqual(drifted_code, 2)
        self.assertIn("identity drifted", drifted["error"])
        self.assertEqual(git(self.owner, "rev-parse", "HEAD"), old)

    def test_repair_legacy_cache_migration_refuses_dirty_owner(self) -> None:
        self.legacy_cache_migration_fixture()
        (self.owner / "src/base.txt").write_text("dirty\n")
        planned, code = runtime.repair(self.controller, dry_run=True, apply=None)
        self.assertEqual((code, planned["outcome"]), (2, "refused"), planned)
        self.assertIn("canonical_integration_owner_not_safe", planned["blockers"])
        self.assertFalse(planned["migration"]["eligible"])

    def test_repair_legacy_cache_migration_requires_target_to_remove_each_finding(self) -> None:
        for retain_writer, retain_cache, expected in (
                (True, False, "legacy_checkout_local_version_cache_writer"),
                (False, True, "tracked_worktree_version_cache")):
            with self.subTest(expected=expected):
                # Each subcase needs its own real-Git fixture because the product ref advances.
                temporary = IntegrationWorkspaceTests(methodName="runTest")
                temporary.setUp()
                try:
                    temporary.legacy_cache_migration_fixture(
                        retain_writer=retain_writer, retain_cache=retain_cache)
                    planned, code = runtime.repair(
                        temporary.controller, dry_run=True, apply=None)
                    self.assertEqual((code, planned["outcome"]), (2, "refused"), planned)
                    self.assertFalse(planned["migration"]["eligible"])
                    self.assertIn(expected, planned["blockers"])
                finally:
                    temporary.tearDown()

    def test_repair_legacy_cache_migration_hydrates_advanced_local_gitlink_without_fetch(self) -> None:
        _, target, child_target = self.legacy_cache_submodule_migration_fixture(
            admit_target_object=True)
        planned, code = runtime.repair(self.controller, dry_run=True, apply=None)
        self.assertEqual((code, planned["outcome"]), (0, "planned"), planned)
        self.assertEqual(planned["migration"]["submodules"]["target"], [{
            "path": "vendor/child", "sha": child_target,
        }])
        with mock.patch.dict(os.environ, {"GIT_ALLOW_PROTOCOL": "file"}, clear=False):
            applied, apply_code = runtime.repair(
                self.controller, dry_run=False, apply=Path(planned["receipt"]["path"]))
        self.assertEqual((apply_code, applied["outcome"]), (0, "completed"), applied)
        self.assertEqual(git(self.owner, "rev-parse", "HEAD"), target)
        self.assertEqual(git(self.owner / "vendor/child", "rev-parse", "HEAD"), child_target)
        self.assertEqual(applied["final_readback"]["gitlinks"], [{
            "path": "vendor/child", "sha": child_target,
        }])

    def test_repair_legacy_cache_migration_refuses_unavailable_target_gitlink(self) -> None:
        old, _, _ = self.legacy_cache_submodule_migration_fixture(
            admit_target_object=False)
        planned, code = runtime.repair(self.controller, dry_run=True, apply=None)
        self.assertEqual((code, planned["outcome"]), (2, "refused"), planned)
        self.assertFalse(planned["migration"]["eligible"])
        self.assertFalse(planned["migration"]["submodules"]["local_objects_available"])
        self.assertEqual(git(self.owner, "rev-parse", "HEAD"), old)

    def test_repair_legacy_cache_migration_refuses_additional_owner_finding(self) -> None:
        old, _, _ = self.legacy_cache_migration_fixture()
        extra = self.root / "extra-protected-owner"
        git(self.repo, "worktree", "add", "--detach", str(extra), old)
        git(extra, "config", "--worktree", "juno.workspace.role", "integration-owner")
        git(extra, "config", "--worktree", "juno.workspace.roleAuthority", runtime.AUTHORITY)
        planned, code = runtime.repair(self.controller, dry_run=True, apply=None)
        self.assertEqual((code, planned["outcome"]), (2, "refused"), planned)
        self.assertFalse(planned["migration"]["eligible"])
        self.assertIn("integration_owner_extra", planned["blockers"])

    def test_repair_legacy_cache_migration_refuses_role_or_authority_mismatch(self) -> None:
        self.legacy_cache_migration_fixture()
        git(self.owner, "config", "--worktree", "juno.workspace.roleAuthority", "unprotected")
        planned, code = runtime.repair(self.controller, dry_run=True, apply=None)
        self.assertEqual((code, planned["outcome"]), (2, "refused"), planned)
        self.assertIn("integration_owner_wrong_authority", planned["blockers"])
        self.assertFalse(planned["migration"]["eligible"])

        git(self.owner, "config", "--worktree", "juno.workspace.roleAuthority", runtime.AUTHORITY)
        git(self.owner, "config", "--worktree", "juno.workspace.role", "task")
        role_plan, role_code = runtime.repair(self.controller, dry_run=True, apply=None)
        self.assertEqual((role_code, role_plan["outcome"]), (2, "refused"), role_plan)
        self.assertIn("canonical_integration_owner_unavailable", role_plan["blockers"])

    def test_repair_clears_only_a_stale_legacy_integration_registration(self) -> None:
        runtime.register(self.controller, self.owner)
        missing = self.root / "missing-legacy-owner"
        git(self.repo, "config", runtime.LEGACY_OWNER_CONFIG, str(missing))

        planned, plan_code = runtime.repair(self.controller, dry_run=True, apply=None)
        self.assertEqual((plan_code, planned["outcome"]), (0, "planned"), planned)
        self.assertEqual(planned["actions"], [{
            "kind": "clear_legacy_integration_registration",
            "repository": str(self.controller.resolve()),
            "key": runtime.LEGACY_OWNER_CONFIG,
            "before": str(missing.resolve()),
        }])
        self.assertEqual(git(self.repo, "config", "--get", runtime.LEGACY_OWNER_CONFIG),
                         str(missing))

        applied, apply_code = runtime.repair(
            self.controller, dry_run=False, apply=Path(planned["receipt"]["path"]))
        self.assertEqual((apply_code, applied["outcome"]), (0, "completed"), applied)
        self.assertNotEqual(run(["git", "-C", str(self.repo), "config", "--get",
                                 runtime.LEGACY_OWNER_CONFIG], self.repo, False).returncode, 0)

    def test_push_dry_run_is_non_mutating_and_apply_is_idempotent(self) -> None:
        advanced = self.local_advance()
        runtime.register(self.controller, self.owner)
        synced, sync_code = runtime.sync(self.controller)
        self.assertEqual(sync_code, 0, synced)
        before_refs = git(self.repo, "show-ref")
        planned, plan_code = runtime.push(self.controller, dry_run=True, apply=None)
        self.assertEqual((plan_code, planned["outcome"]), (0, "planned"), planned)
        self.assertEqual(planned["actions"], [{
            "kind": "push_root", "repository": str(self.owner.resolve()), "remote": "origin",
            "ref": "refs/heads/product", "before": self.base, "after": advanced,
        }])
        self.assertEqual(git(self.repo, "show-ref"), before_refs)
        applied, apply_code = runtime.push(
            self.controller, dry_run=False, apply=Path(planned["receipt"]["path"]))
        self.assertEqual((apply_code, applied["outcome"]), (0, "completed"), applied)
        self.assertEqual(git(self.remote, "rev-parse", "refs/heads/product"), advanced)
        self.assertEqual([row["outcome"] for row in applied["phases"]], ["pushed"])

        retried, retry_code = runtime.push(
            self.controller, dry_run=False, apply=Path(planned["receipt"]["path"]))
        self.assertEqual((retry_code, retried["outcome"]), (0, "completed"), retried)
        self.assertEqual([row["outcome"] for row in retried["phases"]],
                         ["already_complete"])

    def test_push_root_rechecks_remote_gitlinks_without_local_tracking_refs(self) -> None:
        child_remote, child_base, child_advanced, root_target = (
            self.unpublished_submodule_fixture()
        )
        git(child_remote, "update-ref", "refs/heads/main", child_advanced, child_base)
        git(self.owner / "vendor/child", "update-ref", "refs/remotes/origin/main",
            child_base)
        self.assertEqual(
            git(self.owner / "vendor/child", "rev-parse", "refs/remotes/origin/main"),
            child_base)

        planned, plan_code = runtime.push(self.controller, dry_run=True, apply=None)
        self.assertEqual((plan_code, planned["outcome"]), (0, "planned"), planned)
        self.assertEqual([row["kind"] for row in planned["actions"]], ["push_root"])

        applied, apply_code = runtime.push(
            self.controller, dry_run=False, apply=Path(planned["receipt"]["path"]))
        self.assertEqual((apply_code, applied["outcome"]), (0, "completed"), applied)
        self.assertEqual([row["kind"] for row in applied["phases"]],
                         ["verify_nested_gitlink_remote_closure", "push_root"])
        self.assertEqual([row["outcome"] for row in applied["phases"]],
                         ["verified", "pushed"])
        self.assertEqual(git(self.remote, "rev-parse", "refs/heads/product"), root_target)

    def test_push_root_refuses_when_planned_child_remote_closure_changes(self) -> None:
        child_remote, child_base, child_advanced, _ = self.unpublished_submodule_fixture()
        git(child_remote, "update-ref", "refs/heads/main", child_advanced, child_base)
        planned, plan_code = runtime.push(self.controller, dry_run=True, apply=None)
        self.assertEqual((plan_code, planned["outcome"]), (0, "planned"), planned)
        self.assertEqual([row["kind"] for row in planned["actions"]], ["push_root"])
        git(child_remote, "update-ref", "refs/heads/main", child_base, child_advanced)

        applied, apply_code = runtime.push(
            self.controller, dry_run=False, apply=Path(planned["receipt"]["path"]))
        self.assertEqual((apply_code, applied["outcome"]), (2, "failed"), applied)
        self.assertIn("nested gitlink remote closure changed", applied["error"])
        self.assertEqual(applied["phases"][-1]["outcome"], "refused")
        self.assertEqual(git(self.remote, "rev-parse", "refs/heads/product"), self.base)

    def test_bare_push_plans_and_applies_with_bound_terminal_receipts(self) -> None:
        advanced = self.local_advance()
        runtime.register(self.controller, self.owner)
        synced, sync_code = runtime.sync(self.controller)
        self.assertEqual(sync_code, 0, synced)

        published, code = runtime.push(self.controller, dry_run=False, apply=None)

        self.assertEqual((code, published["outcome"]), (0, "completed"), published)
        self.assertEqual(published["mode"], "plan-and-apply")
        self.assertEqual(published["final_status"], "completed")
        self.assertEqual(git(self.remote, "rev-parse", "refs/heads/product"), advanced)
        plan_path = Path(published["plan_receipt"]["path"])
        outcome_path = Path(published["outcome_receipt"]["path"])
        self.assertNotEqual(plan_path, outcome_path)
        plan = json.loads(plan_path.read_text())
        outcome = json.loads(outcome_path.read_text())
        self.assertEqual(plan["outcome"], "planned")
        self.assertEqual(outcome["plan_sha256"], plan["plan_sha256"])
        self.assertEqual(outcome["plan_receipt"], published["plan_receipt"])

    def test_bare_push_blockers_emit_plan_and_terminal_refusal_without_mutation(self) -> None:
        runtime.register(self.controller, self.owner)
        (self.owner / "dirty.txt").write_text("preserve\n")
        before = git(self.remote, "rev-parse", "refs/heads/product")

        refused, code = runtime.push(self.controller, dry_run=False, apply=None)

        self.assertEqual((code, refused["outcome"]), (2, "refused"), refused)
        self.assertEqual(refused["final_status"], "refused")
        self.assertTrue(Path(refused["plan_receipt"]["path"]).is_file())
        self.assertTrue(Path(refused["outcome_receipt"]["path"]).is_file())
        self.assertNotEqual(refused["plan_receipt"]["path"],
                            refused["outcome_receipt"]["path"])
        self.assertIn("integration_owner_dirty", refused["blockers"])
        self.assertEqual(git(self.remote, "rev-parse", "refs/heads/product"), before)
        self.assertEqual((self.owner / "dirty.txt").read_text(), "preserve\n")

    def test_push_apply_refuses_remote_race_without_overwrite(self) -> None:
        local = self.local_advance()
        runtime.register(self.controller, self.owner)
        synced, code = runtime.sync(self.controller)
        self.assertEqual(code, 0, synced)
        planned, plan_code = runtime.push(self.controller, dry_run=True, apply=None)
        self.assertEqual(plan_code, 0, planned)
        remote = self.remote_advance("push-race")

        applied, apply_code = runtime.push(
            self.controller, dry_run=False, apply=Path(planned["receipt"]["path"]))
        self.assertEqual((apply_code, applied["outcome"]), (2, "failed"), applied)
        self.assertIn("remote changed", applied["error"])
        self.assertEqual(git(self.remote, "rev-parse", "refs/heads/product"), remote)
        self.assertNotEqual(remote, local)

    def test_push_persists_child_success_root_failure_and_retries_safely(self) -> None:
        child_remote, child_base, child_advanced, root_target = (
            self.unpublished_submodule_fixture()
        )
        planned, plan_code = runtime.push(self.controller, dry_run=True, apply=None)
        self.assertEqual((plan_code, planned["outcome"]), (0, "planned"), planned)
        self.assertEqual([row["kind"] for row in planned["actions"]],
                         ["push_submodule", "push_root"])
        self.assertEqual(planned["actions"][0]["before"], child_base)
        self.assertEqual(planned["actions"][0]["after"], child_advanced)
        self.assertEqual(planned["actions"][1]["after"], root_target)

        original = runtime.run

        def fail_root_push(argv: list[str], cwd: Path, *, check: bool = True):
            if argv[:4] == ["git", "-C", str(self.owner.resolve()), "push"]:
                raise runtime.IntegrationError("injected root publication failure")
            return original(argv, cwd, check=check)

        with mock.patch.object(runtime, "run", side_effect=fail_root_push):
            failed, failed_code = runtime.push(
                self.controller, dry_run=False, apply=Path(planned["receipt"]["path"]))
        self.assertEqual((failed_code, failed["outcome"]), (2, "failed"), failed)
        self.assertIn("injected root publication failure", failed["error"])
        self.assertEqual(git(child_remote, "rev-parse", "refs/heads/main"), child_advanced)
        self.assertEqual(git(self.remote, "rev-parse", "refs/heads/product"), self.base)
        persisted = json.loads(Path(failed["receipt"]["path"]).read_text())
        self.assertEqual([row["kind"] for row in persisted["phases"]],
                         ["push_submodule", "verify_nested_gitlink_remote_closure"])

        retried, retry_code = runtime.push(
            self.controller, dry_run=False, apply=Path(planned["receipt"]["path"]))
        self.assertEqual((retry_code, retried["outcome"]), (0, "completed"), retried)
        self.assertEqual([row["outcome"] for row in retried["phases"]],
                         ["already_complete", "verified", "pushed"])
        self.assertEqual(git(self.remote, "rev-parse", "refs/heads/product"), root_target)


    def test_managed_provenance_accepts_controller_asset_class(self) -> None:
        declaration = self.repo / runtime.MANAGED_MANIFEST_PATH
        value = json.loads(declaration.read_text())
        (self.repo / "juno-code/src/templates/workflows").mkdir(parents=True, exist_ok=True)
        (self.repo / "juno-code/src/templates/workflows/yy-task-run.yaml").write_text(
            "schema_version: yy_task_run.v1\n")
        (self.repo / ".juno_task/workflows").mkdir(parents=True, exist_ok=True)
        value["assets"].append({"source": "workflows/yy-task-run.yaml",
                                "destination": ".juno_task/workflows/yy-task-run.yaml",
                                "installClass": "controller", "type": "workflow"})
        declaration.write_text(json.dumps(value, indent=2) + "\n")
        git(self.repo, "add", "juno-code/src/templates")
        git(self.repo, "commit", "-m", "declare controller workflow asset")
        target = git(self.repo, "rev-parse", "HEAD")
        provenance = runtime.managed_target_provenance(self.repo, target)
        self.assertNotIn(".juno_task/workflows/yy-task-run.yaml", provenance["assets"],
                         "controller-class assets are not runtime scripts")
        # An admitted class with an unadmitted type still fails closed.
        value["assets"][-1]["type"] = "unexpected"
        declaration.write_text(json.dumps(value, indent=2) + "\n")
        git(self.repo, "add", runtime.MANAGED_MANIFEST_PATH)
        git(self.repo, "commit", "-m", "malformed controller asset type")
        malformed = git(self.repo, "rev-parse", "HEAD")
        with self.assertRaisesRegex(runtime.ManagedRuntimeError,
                                    "target managed asset entry is invalid"):
            runtime.managed_target_provenance(self.repo, malformed)

    def test_shipped_template_declaration_matches_the_strict_contract(self) -> None:
        # This suite runs from the shipped template tree (six levels below the
        # repository root) and from installed runtime trees (three levels
        # below), so locate the declaration by walking up instead of assuming
        # a fixed depth.
        declaration = next(
            (base / runtime.MANAGED_MANIFEST_PATH
             for base in Path(__file__).resolve().parents
             if (base / runtime.MANAGED_MANIFEST_PATH).is_file()),
            None)
        self.assertIsNotNone(declaration, "shipped template declaration is missing")
        value = json.loads(declaration.read_text())
        self.assertEqual(value.get("schemaVersion"), 2)
        self.assertEqual(value.get("instructionBundle"), {
            "schemaVersion": "juno_instruction_bundle_declaration.v1",
            "semanticVersion": "1.0.0"})
        allowed_keys = [{"source", "destination", "installClass", "type"},
                        {"source", "destination", "installClass", "type", "macro"}]
        for asset in value["assets"]:
            self.assertIn(set(asset), allowed_keys,
                          f"declaration entry carries unexpected keys: {asset}")
            self.assertIn(asset.get("installClass"), {"project", "script", "controller"},
                          f"declaration entry carries an unadmitted class: {asset}")
            if asset.get("installClass") == "controller":
                self.assertIn(asset.get("type"), {"workflow", "prompt"},
                              f"controller entry carries an unadmitted type: {asset}")


class InstalledConsumerManagedRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "installed consumer fixture"
        self.root.mkdir()
        self.repo = self.root / "product"
        git(self.root, "init", "-b", "product", str(self.repo))
        git(self.repo, "config", "user.email", "test@example.com")
        git(self.repo, "config", "user.name", "Test")
        self.policy = {"schema_version": "juno_task_workspace_config.v1",
                       "repository": ".", "workspace_root": "/tmp/default",
                       "allowed_paths": ["src"]}
        self.write(runtime.MANAGED_POLICY_PATH, self.policy)
        self.write(".juno_task/scripts/one.py", "installed old one\n")
        self.write(".juno_task/scripts/two.py", "installed two\n")
        self.write_manifest("8.0.0")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "installed consumer old generation")
        self.previous = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "branch", "controller")
        self.controller = self.root / "controller"
        git(self.repo, "worktree", "add", str(self.controller), "controller")
        controller_policy = dict(self.policy)
        controller_policy["workspace_root"] = "/private/controller-tasks"
        (self.controller / runtime.MANAGED_POLICY_PATH).write_text(
            json.dumps(controller_policy) + "\n")
        git(self.controller, "commit", "-am", "controller customization")

        self.write(".juno_task/scripts/one.py", "installed new one\n")
        target_policy = dict(self.policy)
        target_policy["selectable_paths"] = ["frontend"]
        self.write(runtime.MANAGED_POLICY_PATH, target_policy)
        self.write_manifest("9.0.0")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "installed consumer target generation")
        self.target = git(self.repo, "rev-parse", "HEAD")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write(self, relative: str, value: object) -> None:
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text((json.dumps(value) + "\n") if not isinstance(value, str) else value)

    def write_manifest(self, version: str) -> None:
        assets = {}
        for destination, kind in ((".juno_task/scripts/one.py", "script"),
                                  (".juno_task/scripts/two.py", "script"),
                                  (runtime.MANAGED_POLICY_PATH, "config")):
            digest = runtime.managed_sha256((self.repo / destination).read_bytes())
            assets[destination] = {"type": kind, "templateVersion": version,
                                   "sourceSha256": digest, "installedSha256": digest}
        self.write(runtime.MANAGED_INSTALLED_MANIFEST_PATH, {
            "schemaVersion": 1, "packageName": "@yylo/cli",
            "packageVersion": version, "assets": assets,
        })

    def commit_manifest_mutation(self, mutate) -> str:
        git(self.repo, "reset", "--hard", self.target)
        path = self.repo / runtime.MANAGED_INSTALLED_MANIFEST_PATH
        manifest = json.loads(path.read_text())
        mutate(manifest)
        path.write_text(json.dumps(manifest) + "\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "invalid installed provenance")
        return git(self.repo, "rev-parse", "HEAD")

    def policyless_generations(self) -> tuple[str, str]:
        git(self.repo, "reset", "--hard", self.previous)
        (self.repo / runtime.MANAGED_POLICY_PATH).unlink()
        git(self.repo, "add", "-u")
        git(self.repo, "commit", "-m", "installed consumer without controller-private policy")
        previous = git(self.repo, "rev-parse", "HEAD")

        script = self.repo / ".juno_task/scripts/one.py"
        script.write_text("installed new one\n")
        manifest_path = self.repo / runtime.MANAGED_INSTALLED_MANIFEST_PATH
        manifest = json.loads(manifest_path.read_text())
        manifest["packageVersion"] = "9.0.0"
        for record in manifest["assets"].values():
            record["templateVersion"] = "9.0.0"
        digest = runtime.managed_sha256(script.read_bytes())
        manifest["assets"][".juno_task/scripts/one.py"].update(
            {"sourceSha256": digest, "installedSha256": digest})
        manifest_path.write_text(json.dumps(manifest) + "\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "policyless installed consumer target")
        return previous, git(self.repo, "rev-parse", "HEAD")

    def legacy_policyless_generations(self, version: str) -> tuple[str, str]:
        """Commit two real installed generations with the private asset wholly absent."""
        git(self.repo, "reset", "--hard", self.previous)
        (self.repo / runtime.MANAGED_POLICY_PATH).unlink()
        manifest_path = self.repo / runtime.MANAGED_INSTALLED_MANIFEST_PATH
        manifest = json.loads(manifest_path.read_text())
        manifest["packageVersion"] = version
        manifest["assets"].pop(runtime.MANAGED_POLICY_PATH)
        for record in manifest["assets"].values():
            record["templateVersion"] = version
        manifest_path.write_text(json.dumps(manifest) + "\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", f"installed {version} policyless generation")
        previous = git(self.repo, "rev-parse", "HEAD")

        self.write("src/product-only.txt", f"feature on {version}\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "product change keeps installed package generation")
        return previous, git(self.repo, "rev-parse", "HEAD")

    def test_installed_consumer_refresh_doctor_and_retry_use_only_committed_bytes(self) -> None:
        self.assertFalse((self.repo / "juno-code").exists())
        self.assertNotEqual(run(["git", "-C", str(self.repo), "cat-file", "-e",
                                 f"{self.target}:{runtime.MANAGED_MANIFEST_PATH}"],
                                self.repo, False).returncode, 0)

        result = runtime.managed_runtime_refresh(
            self.controller, self.repo, self.previous, self.target, task_id="consumer-post-cas")

        self.assertEqual(result["package_version"], "9.0.0")
        self.assertEqual((self.controller / ".juno_task/scripts/one.py").read_text(),
                         "installed new one\n")
        doctor = runtime.managed_runtime_inspect(self.controller, self.repo, self.target)
        self.assertTrue(doctor["healthy"], doctor)
        retried = runtime.managed_runtime_refresh(
            self.controller, self.repo, self.previous, self.target,
            task_id="consumer-post-cas-retry")
        self.assertEqual(retried["outcome"], "completed")
        self.assertTrue(retried["doctor"]["healthy"])

    def test_installed_consumer_recovers_when_product_shas_lack_controller_policy(self) -> None:
        previous, target = self.policyless_generations()
        before_policy = (self.controller / runtime.MANAGED_POLICY_PATH).read_bytes()

        result = runtime.managed_runtime_refresh(
            self.controller, self.repo, previous, target, task_id="consumer-policyless-post-cas")

        self.assertEqual(result["package_version"], "9.0.0")
        self.assertEqual((self.controller / runtime.MANAGED_POLICY_PATH).read_bytes(), before_policy)
        self.assertEqual((self.controller / ".juno_task/scripts/one.py").read_text(),
                         "installed new one\n")
        self.assertTrue(runtime.managed_runtime_inspect(
            self.controller, self.repo, target)["healthy"])
        retried = runtime.managed_runtime_refresh(
            self.controller, self.repo, previous, target,
            task_id="consumer-policyless-post-cas-retry")
        self.assertEqual(retried["outcome"], "completed")

    def test_exact_policyless_package_generations_preserve_clean_controller_policy(self) -> None:
        for version in ("2.1.3-rc.0.22", "2.1.3-rc.0.24", "2.1.3-rc.0.32"):
            with self.subTest(version=version):
                previous, target = self.legacy_policyless_generations(version)
                before = (self.controller / runtime.MANAGED_POLICY_PATH).read_bytes()

                result = runtime.managed_runtime_refresh(
                    self.controller, self.repo, previous, target,
                    task_id=f"legacy-{version}-post-cas")
                retry = runtime.managed_runtime_refresh(
                    self.controller, self.repo, previous, target,
                    task_id=f"legacy-{version}-post-cas-retry")

                self.assertEqual(result["package_version"], version)
                self.assertEqual(retry["outcome"], "completed")
                self.assertEqual((self.controller / runtime.MANAGED_POLICY_PATH).read_bytes(), before)
                self.assertTrue(runtime.managed_runtime_inspect(
                    self.controller, self.repo, target)["healthy"])

    def test_policyless_compatibility_fails_closed_on_identity_dirt_and_delta(self) -> None:
        previous, target = self.legacy_policyless_generations("2.1.3-rc.0.24")
        manifest_path = self.repo / runtime.MANAGED_INSTALLED_MANIFEST_PATH

        cases = []
        manifest = json.loads(manifest_path.read_text())
        manifest["assets"][runtime.MANAGED_POLICY_PATH] = {
            "type": "config", "templateVersion": "2.1.3-rc.0.24",
            "sourceSha256": "a" * 64, "installedSha256": "a" * 64,
        }
        cases.append(("mixed policy record", manifest))

        manifest = json.loads(manifest_path.read_text())
        manifest["packageVersion"] = "2.1.3-rc.0.25"
        for record in manifest["assets"].values():
            record["templateVersion"] = "2.1.3-rc.0.25"
        cases.append(("package identity transition", manifest))

        manifest = json.loads(manifest_path.read_text())
        script = self.repo / ".juno_task/scripts/one.py"
        script.write_text("same-version policy capability ambiguity\n")
        digest = runtime.managed_sha256(script.read_bytes())
        manifest["assets"][".juno_task/scripts/one.py"].update(
            {"sourceSha256": digest, "installedSha256": digest})
        cases.append(("same-version manifest delta", manifest))

        for label, changed_manifest in cases:
            with self.subTest(label=label):
                git(self.repo, "reset", "--hard", target)
                if label == "same-version manifest delta":
                    script.write_text("same-version policy capability ambiguity\n")
                manifest_path.write_text(json.dumps(changed_manifest) + "\n")
                git(self.repo, "add", ".")
                git(self.repo, "commit", "-m", label)
                changed = git(self.repo, "rev-parse", "HEAD")
                with self.assertRaisesRegex(runtime.ManagedRuntimeError,
                                            "installed task policy provenance is invalid"):
                    runtime.managed_runtime_plan(self.controller, self.repo, previous, changed)

        git(self.repo, "reset", "--hard", target)
        policy = self.controller / runtime.MANAGED_POLICY_PATH
        policy.write_text(policy.read_text() + " ")
        with self.assertRaisesRegex(runtime.ManagedRuntimeError,
                                    "installed task policy provenance is invalid"):
            runtime.managed_runtime_plan(self.controller, self.repo, previous, target)
        git(self.controller, "checkout", "--", runtime.MANAGED_POLICY_PATH)

    def test_installed_consumer_policyless_recovery_fails_closed_on_ambiguous_provenance(self) -> None:
        previous, target = self.policyless_generations()
        manifest_path = self.repo / runtime.MANAGED_INSTALLED_MANIFEST_PATH

        manifest = json.loads(manifest_path.read_text())
        manifest["assets"][runtime.MANAGED_POLICY_PATH]["sourceSha256"] = "f" * 64
        manifest_path.write_text(json.dumps(manifest) + "\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "changed unavailable policy source")
        changed = git(self.repo, "rev-parse", "HEAD")
        with self.assertRaisesRegex(runtime.ManagedRuntimeError,
                                    "source generation changed without immutable bytes"):
            runtime.managed_runtime_plan(self.controller, self.repo, previous, changed)

        git(self.repo, "reset", "--hard", target)
        self.write(runtime.MANAGED_POLICY_PATH, self.policy)
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "only target contains controller policy")
        one_sided = git(self.repo, "rev-parse", "HEAD")
        with self.assertRaisesRegex(runtime.ManagedRuntimeError, "provenance is ambiguous"):
            runtime.managed_runtime_plan(self.controller, self.repo, previous, one_sided)

    def test_installed_consumer_provenance_failures_are_closed(self) -> None:
        script = ".juno_task/scripts/one.py"
        cases = [
            ("malformed manifest shape", lambda value: value.update({"unexpected": True}),
             "manifest/package identity"),
            ("wrong package name", lambda value: value.update({"packageName": "other"}),
             "manifest/package identity"),
            ("wrong package version", lambda value: value["assets"][script].update(
                {"templateVersion": "8.0.0"}), "package version mismatch"),
            ("source installed mismatch", lambda value: value["assets"][script].update(
                {"sourceSha256": "f" * 64}), "source hash mismatch"),
            ("undeclared script", lambda value: value["assets"][script].update(
                {"type": "config"}), "script is undeclared"),
        ]
        for label, mutate, message in cases:
            with self.subTest(label=label):
                target = self.commit_manifest_mutation(mutate)
                with self.assertRaisesRegex(runtime.ManagedRuntimeError, message):
                    runtime.managed_runtime_plan(
                        self.controller, self.repo, self.previous, target)

    def test_installed_consumer_missing_hash_drift_and_mixed_provenance_fail_closed(self) -> None:
        git(self.repo, "reset", "--hard", self.target)
        (self.repo / ".juno_task/scripts/one.py").unlink()
        git(self.repo, "add", "-u")
        git(self.repo, "commit", "-m", "missing installed destination")
        missing = git(self.repo, "rev-parse", "HEAD")
        with self.assertRaisesRegex(runtime.ManagedRuntimeError, "destination is missing"):
            runtime.managed_runtime_plan(self.controller, self.repo, self.previous, missing)

        git(self.repo, "reset", "--hard", self.target)
        (self.repo / ".juno_task/scripts/one.py").write_text("drift after manifest\n")
        git(self.repo, "commit", "-am", "installed destination hash drift")
        drift = git(self.repo, "rev-parse", "HEAD")
        with self.assertRaisesRegex(runtime.ManagedRuntimeError, "manifest drift"):
            runtime.managed_runtime_plan(self.controller, self.repo, self.previous, drift)

        git(self.repo, "reset", "--hard", self.target)
        self.write(runtime.MANAGED_MANIFEST_PATH, {"schemaVersion": 1, "assets": []})
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "partial source provenance")
        mixed = git(self.repo, "rev-parse", "HEAD")
        with self.assertRaisesRegex(runtime.ManagedRuntimeError, "mixed ambiguous"):
            runtime.managed_runtime_plan(self.controller, self.repo, self.previous, mixed)


class ManagedRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "fixture with spaces"
        self.root.mkdir()
        self.repo = self.root / "repo"
        git(self.root, "init", "-b", "product", str(self.repo))
        git(self.repo, "config", "user.email", "test@example.com")
        git(self.repo, "config", "user.name", "Test")
        assets = {"schemaVersion": 1, "assets": [
            {"source": "scripts/one.py", "destination": ".juno_task/scripts/one.py",
             "installClass": "script", "type": "script"},
            {"source": "scripts/two.py", "destination": ".juno_task/scripts/two.py",
             "installClass": "script", "type": "script"},
            {"source": "config/task-workspace.json", "destination": runtime.MANAGED_POLICY_PATH,
             "installClass": "project", "type": "config"},
        ]}
        self.policy = {"schema_version": "juno_task_workspace_config.v1",
                       "repository": ".", "workspace_root": "/tmp/default",
                       "allowed_paths": ["src"]}
        self.write("juno-code/src/templates/managed-assets.json", assets)
        self.write("juno-code/package.json", {"name": "@yylo/cli", "version": "9.0.0"})
        self.write(runtime.MANAGED_POLICY_PATH, self.policy)
        self.write(".juno_task/scripts/one.py", "old one\n")
        self.write(".juno_task/scripts/two.py", "old two\n")
        self.write("juno-code/src/templates/scripts/one.py", "installed prior one\n")
        self.write("juno-code/src/templates/scripts/two.py", "old two\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "old generation")
        self.previous = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "branch", "controller")
        self.controller = self.root / "controller"
        git(self.repo, "worktree", "add", str(self.controller), "controller")
        controller_policy = dict(self.policy)
        controller_policy["workspace_root"] = "/private/controller-tasks"
        (self.controller / runtime.MANAGED_POLICY_PATH).write_text(json.dumps(controller_policy) + "\n")
        git(self.controller, "commit", "-am", "controller customization")

        self.write(".juno_task/scripts/one.py", "new one\n")
        target_policy = dict(self.policy)
        target_policy["selectable_paths"] = ["frontend"]
        self.write(runtime.MANAGED_POLICY_PATH, target_policy)
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "new generation")
        self.target = git(self.repo, "rev-parse", "HEAD")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write(self, relative: str, value: object) -> None:
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text((json.dumps(value) + "\n") if not isinstance(value, str) else value)

    def test_doctor_consumes_installed_release_manifest_without_source_templates(self) -> None:
        shutil.rmtree(self.repo / "juno-code")
        assets = {}
        for relative, kind in ((".juno_task/scripts/one.py", "script"),
                               (".juno_task/scripts/two.py", "script"),
                               (runtime.MANAGED_POLICY_PATH, "config")):
            data = (self.repo / relative).read_bytes()
            digest = runtime.managed_sha256(data)
            assets[relative] = {"type": kind, "templateVersion": "9.1.0",
                                "sourceSha256": digest, "installedSha256": digest}
        self.write(runtime.MANAGED_INSTALLED_MANIFEST_PATH, {
            "schemaVersion": 1, "packageName": "@yylo/cli",
            "packageVersion": "9.1.0", "assets": assets,
        })
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "consumer installed release generation")
        consumer_target = git(self.repo, "rev-parse", "HEAD")

        generation_scripts = {}
        for relative in (".juno_task/scripts/one.py", ".juno_task/scripts/two.py"):
            source = (self.repo / relative).read_bytes()
            destination = self.controller / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source)
            digest = runtime.managed_sha256(source)
            generation_scripts[relative] = {"classification": "exact",
                                            "source_sha256": digest,
                                            "actual_sha256": digest}
        policy_hash = runtime.managed_sha256(
            (self.controller / runtime.MANAGED_POLICY_PATH).read_bytes())
        generation = {"schema_version": runtime.MANAGED_RUNTIME_SCHEMA,
                      "target_sha": consumer_target, "package_version": "9.1.0",
                      "scripts": generation_scripts, "policy_sha256": policy_hash}
        generation_path = self.controller / runtime.MANAGED_GENERATION_PATH
        generation_path.parent.mkdir(parents=True, exist_ok=True)
        generation_path.write_text(json.dumps(generation) + "\n")

        doctor = runtime.managed_runtime_inspect(self.controller, self.repo, consumer_target)
        self.assertTrue(doctor["healthy"], doctor)
        self.assertEqual(doctor["package_version"], "9.1.0")
        self.assertFalse((self.repo / runtime.MANAGED_MANIFEST_PATH).exists())

    def test_managed_asset_prompt_macro_shape_is_strict(self) -> None:
        manifest_path = self.repo / runtime.MANAGED_MANIFEST_PATH

        def commit_asset(asset: dict[str, object], label: str) -> str:
            git(self.repo, "reset", "--hard", self.target)
            manifest = json.loads(manifest_path.read_text())
            manifest["assets"].append(asset)
            manifest_path.write_text(json.dumps(manifest) + "\n")
            git(self.repo, "add", runtime.MANAGED_MANIFEST_PATH)
            git(self.repo, "commit", "-m", label)
            return git(self.repo, "rev-parse", "HEAD")

        legacy = runtime.managed_target_provenance(self.repo, self.target)
        prompt = {
            "source": "prompts/reflect.md",
            "destination": ".juno_task/prompts/reflect.md",
            "installClass": "project",
            "type": "prompt",
            "macro": "reflect",
        }
        accepted = runtime.managed_target_provenance(
            self.repo, commit_asset(prompt, "valid prompt macro"))
        self.assertEqual(accepted["assets"], legacy["assets"])

        invalid_assets = [
            (dict(prompt, unexpected=True), "unknown key"),
            (dict(prompt, macro=""), "empty macro"),
            (dict(prompt, macro=7), "non-string macro"),
            (dict(prompt, installClass="script"), "non-project macro"),
            (dict(prompt, type="config"), "non-prompt macro"),
        ]
        for asset, label in invalid_assets:
            with self.subTest(label=label):
                commit = commit_asset(asset, label)
                with self.assertRaisesRegex(
                        runtime.ManagedRuntimeError, "asset entry is invalid"):
                    runtime.managed_target_provenance(self.repo, commit)

    def test_managed_destination_race_refuses_overwrite_and_preserves_both_byte_sets(self) -> None:
        destination = self.controller / ".juno_task/scripts/one.py"
        expected = destination.read_bytes()
        racer = b"concurrent controller writer\n"
        original_write = runtime.managed_atomic_write
        raced = False

        def race_before_first_managed_write(path: Path, data: bytes,
                                            mode: int | None = None, **kwargs: object) -> None:
            nonlocal raced
            if path.resolve() == destination.resolve() and not raced:
                raced = True
                path.write_bytes(racer)
            original_write(path, data, mode, **kwargs)

        with mock.patch.object(runtime, "managed_atomic_write",
                               side_effect=race_before_first_managed_write):
            with self.assertRaises(runtime.ManagedRuntimeError) as caught:
                runtime.managed_runtime_refresh(
                    self.controller, self.repo, self.previous, self.target,
                    task_id="managed-destination-race")

        self.assertEqual(destination.read_bytes(), racer)
        self.assertIsNotNone(caught.exception.receipt)
        receipt = json.loads(Path(caught.exception.receipt["path"]).read_text())
        self.assertEqual(receipt["outcome"], "collision")
        self.assertEqual(receipt["collision"]["schema_version"],
                         runtime.MANAGED_COLLISION_SCHEMA)
        collision = receipt["collision"]["destinations"][0]
        self.assertEqual(collision["expected_old_sha256"], runtime.managed_sha256(expected))
        self.assertEqual(collision["observed_sha256"], runtime.managed_sha256(racer))
        for byte_set in collision["preserved_byte_sets"]:
            preserved = Path(byte_set["path"])
            self.assertTrue(preserved.is_file())
            self.assertEqual(runtime.managed_sha256(preserved.read_bytes()),
                             byte_set["sha256"])

    def test_refresh_uses_exact_target_preserves_policy_customization_and_receipts_log(self) -> None:
        result = runtime.managed_runtime_refresh(self.controller, self.repo, self.previous, self.target,
                                 task_id="UOsd11")
        self.assertEqual(result["outcome"], "completed")
        self.assertEqual((self.controller / ".juno_task/scripts/one.py").read_text(), "new one\n")
        self.assertEqual((self.controller / ".juno_task/scripts/two.py").read_text(), "old two\n")
        policy = json.loads((self.controller / runtime.MANAGED_POLICY_PATH).read_text())
        self.assertEqual(policy["workspace_root"], "/private/controller-tasks")
        self.assertEqual(policy["selectable_paths"], ["frontend"])
        self.assertEqual(result["policy"]["changed_fields"], ["selectable_paths"])
        self.assertFalse(result["timed_out"])
        log = Path(result["log"]["path"])
        self.assertTrue(log.is_file())
        self.assertEqual(runtime.managed_sha256(log.read_bytes()), result["log"]["sha256"])
        receipt = Path(result["receipt"]["path"])
        self.assertEqual(runtime.managed_sha256(receipt.read_bytes()), result["receipt"]["sha256"])
        self.assertTrue(runtime.managed_runtime_inspect(self.controller, self.repo, self.target)["healthy"])
        # A crash after the generation marker but before the outer terminal
        # checkpoint can retry the exact transition despite expected policy dirt.
        retried = runtime.managed_runtime_refresh(self.controller, self.repo, self.previous, self.target,
                                  task_id="UOsd11-retry")
        self.assertEqual(retried["outcome"], "completed")

    def test_refresh_repair_and_doctor_use_packaged_source_for_untracked_destination(self) -> None:
        manifest_path = self.repo / runtime.MANAGED_MANIFEST_PATH
        manifest = json.loads(manifest_path.read_text())
        manifest["assets"].append({
            "source": "scripts/template-only.py",
            "destination": ".juno_task/scripts/template-only.py",
            "installClass": "script",
            "type": "script",
        })
        manifest_path.write_text(json.dumps(manifest) + "\n")
        self.write("juno-code/src/templates/scripts/template-only.py", "template only\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "add template-only managed runtime")
        target = git(self.repo, "rev-parse", "HEAD")
        self.assertFalse((self.repo / ".juno_task/scripts/template-only.py").exists())

        result = runtime.managed_runtime_refresh(
            self.controller, self.repo, self.previous, target, task_id="template-only")

        destination = self.controller / ".juno_task/scripts/template-only.py"
        self.assertEqual(destination.read_text(), "template only\n")
        row = next(item for item in result["scripts"]
                   if item["path"] == ".juno_task/scripts/template-only.py")
        self.assertEqual(row["outcome"], "installed")
        self.assertEqual(row["classification"], "exact")
        doctor = runtime.managed_runtime_inspect(self.controller, self.repo, target)
        self.assertTrue(doctor["healthy"], doctor)

        destination.write_text("owner template-only customization\n")
        self.write("juno-code/src/templates/scripts/template-only.py", "template only v2\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "update template-only managed runtime")
        updated_target = git(self.repo, "rev-parse", "HEAD")
        repair = runtime.managed_runtime_repair_plan(
            self.controller, self.repo, target, updated_target, task_id="template-only-repair")
        action = next(item for item in repair["actions"]
                      if item["path"] == ".juno_task/scripts/template-only.py")
        self.assertEqual(action["resolution"], "conflict")

    def test_newly_admitted_historical_script_refreshes_only_exact_history(self) -> None:
        legacy = b'VERSION_CHECK_CACHE_DIR="${VERSION_CHECK_CACHE_DIR:-${PWD}/.juno_task}"\n'
        destination = self.controller / runtime.INSTALL_REQUIREMENTS_PATH
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(legacy)
        self.write(runtime.INSTALL_REQUIREMENTS_PATH, legacy.decode())
        git(self.repo, "add", runtime.INSTALL_REQUIREMENTS_PATH)
        git(self.repo, "commit", "-m", "historical install requirements")
        previous = git(self.repo, "rev-parse", "HEAD")
        manifest = json.loads((self.repo / runtime.MANAGED_MANIFEST_PATH).read_text())
        manifest["assets"].append({
            "source": "scripts/install_requirements.sh",
            "destination": runtime.INSTALL_REQUIREMENTS_PATH,
            "installClass": "script", "type": "script",
        })
        self.write(runtime.MANAGED_MANIFEST_PATH, manifest)
        self.write(runtime.INSTALL_REQUIREMENTS_PATH, "new external cache\n")
        self.write("juno-code/src/templates/scripts/install_requirements.sh", "new external cache\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "manage install requirements")
        target = git(self.repo, "rev-parse", "HEAD")

        result = runtime.managed_runtime_refresh(
            self.controller, self.repo, previous, target, task_id="legacy-cache-writer")

        row = next(item for item in result["scripts"]
                   if item["path"] == runtime.INSTALL_REQUIREMENTS_PATH)
        self.assertEqual(row["outcome"], "updated")
        self.assertEqual(row["prior_generation_classification"],
                         "immutable_historical_generation")
        self.assertEqual(destination.read_text(), "new external cache\n")

    def test_doctor_reports_exact_legacy_writer_and_tracked_cache_without_cleanup(self) -> None:
        runtime.managed_runtime_refresh(
            self.controller, self.repo, self.previous, self.target, task_id="doctor-cache-base")
        owner = self.root / "legacy-owner"
        git(self.repo, "worktree", "add", "--detach", str(owner), self.target)
        script = owner / runtime.INSTALL_REQUIREMENTS_PATH
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(
            'VERSION_CHECK_CACHE_DIR="${VERSION_CHECK_CACHE_DIR:-${PWD}/.juno_task}"\n')
        cache = owner / runtime.VERSION_CACHE_PATH
        cache.write_text("checked_at=1\n")
        git(owner, "add", runtime.VERSION_CACHE_PATH)
        git(self.repo, "config", runtime.OWNER_CONFIG, str(owner))

        doctor = runtime.managed_runtime_inspect(self.controller, self.repo, self.target)

        by_code = {item["code"]: item for item in doctor["findings"]}
        self.assertEqual(by_code["legacy_checkout_local_version_cache_writer"]["path"],
                         str(script.resolve()))
        self.assertEqual(by_code["tracked_worktree_version_cache"]["path"],
                         str(cache.resolve()))
        self.assertEqual(by_code["tracked_worktree_version_cache"]["state"], "modified")
        self.assertEqual(cache.read_text(), "checked_at=1\n")
        self.assertFalse(doctor["healthy"])

    def test_unchanged_source_customization_is_preserved_while_changed_runtime_refreshes(self) -> None:
        customized = self.controller / ".juno_task/scripts/two.py"
        customized.write_text("owner controller registration customization\n")
        actual_hash = runtime.managed_sha256(customized.read_bytes())

        result = runtime.managed_runtime_refresh(
            self.controller, self.repo, self.previous, self.target, task_id="live-shape")

        self.assertEqual((self.controller / ".juno_task/scripts/one.py").read_text(), "new one\n")
        self.assertEqual(customized.read_text(), "owner controller registration customization\n")
        rows = {row["path"]: row for row in result["scripts"]}
        self.assertEqual(rows[".juno_task/scripts/one.py"]["classification"], "exact")
        self.assertEqual(rows[".juno_task/scripts/two.py"]["outcome"], "preserved_customization")
        self.assertEqual(rows[".juno_task/scripts/two.py"]["actual_sha256"], actual_hash)
        generation = json.loads((self.controller / runtime.MANAGED_GENERATION_PATH).read_text())
        preserved = generation["scripts"][".juno_task/scripts/two.py"]
        self.assertEqual(preserved["classification"], "preserved_customization")
        self.assertEqual(preserved["actual_sha256"], actual_hash)
        self.assertNotEqual(preserved["actual_sha256"], preserved["source_sha256"])
        doctor = runtime.managed_runtime_inspect(self.controller, self.repo, self.target)
        self.assertTrue(doctor["healthy"], doctor)
        self.assertEqual(doctor["scripts"][".juno_task/scripts/two.py"]["classification"],
                         "preserved_customization")
        # Exact-transition recovery remains idempotent and does not overwrite it.
        retried = runtime.managed_runtime_refresh(
            self.controller, self.repo, self.previous, self.target, task_id="live-shape-retry")
        self.assertEqual(retried["outcome"], "completed")
        self.assertEqual(customized.read_text(), "owner controller registration customization\n")

    def test_doctor_detects_drift_from_bound_preserved_customization(self) -> None:
        customized = self.controller / ".juno_task/scripts/two.py"
        customized.write_text("intentional owner customization\n")
        runtime.managed_runtime_refresh(
            self.controller, self.repo, self.previous, self.target, task_id="doctor-drift")
        customized.write_text("later unreviewed drift\n")

        doctor = runtime.managed_runtime_inspect(self.controller, self.repo, self.target)

        self.assertFalse(doctor["healthy"])
        finding = next(row for row in doctor["findings"]
                       if row["code"] == "managed_preserved_customization_drift")
        self.assertEqual(finding["path"], ".juno_task/scripts/two.py")
        self.assertEqual(finding["classification"], "preserved_customization")
        self.assertNotEqual(finding["expected_sha256"], finding["actual_sha256"])
        with self.assertRaisesRegex(runtime.ManagedRuntimeError, "existing managed generation drift"):
            runtime.managed_runtime_refresh(
                self.controller, self.repo, self.previous, self.target, task_id="drift-retry")
        self.assertEqual(customized.read_text(), "later unreviewed drift\n")

    def test_receipt_bound_installed_prior_template_updates_when_admitted_source_changes(self) -> None:
        installed = self.controller / ".juno_task/scripts/one.py"
        installed.write_text("installed prior one\n")
        generation = {
            "schema_version": runtime.MANAGED_RUNTIME_SCHEMA,
            "target_sha": self.previous,
            "package_version": "9.0.0",
            "scripts": {
                ".juno_task/scripts/one.py": {
                    "classification": "preserved_customization",
                    "source_sha256": runtime.managed_sha256(b"old one\n"),
                    "actual_sha256": runtime.managed_sha256(installed.read_bytes()),
                },
                ".juno_task/scripts/two.py": {
                    "classification": "exact",
                    "source_sha256": runtime.managed_sha256(b"old two\n"),
                    "actual_sha256": runtime.managed_sha256(b"old two\n"),
                },
            },
            "policy_sha256": runtime.managed_sha256(
                (self.controller / runtime.MANAGED_POLICY_PATH).read_bytes()),
        }
        generation_path = self.controller / runtime.MANAGED_GENERATION_PATH
        generation_path.parent.mkdir(parents=True, exist_ok=True)
        generation_path.write_text(json.dumps(generation) + "\n")

        result = runtime.managed_runtime_refresh(
            self.controller, self.repo, self.previous, self.target, task_id="bound-prior")

        self.assertEqual(installed.read_text(), "new one\n")
        row = next(item for item in result["scripts"] if item["path"].endswith("one.py"))
        self.assertEqual(row["outcome"], "updated")
        self.assertEqual(row["classification"], "exact")
        self.assertEqual(row["prior_generation_classification"],
                         "receipt_bound_installed_template")

    def test_obsolete_exact_generation_restored_by_bootstrap_is_reactivated(self) -> None:
        # The admitted middle generation installed "new one", but a stale
        # bootstrap later restored bytes that an older successful receipt proves
        # were the exact managed source at self.previous.
        receipt_root = self.controller / runtime.MANAGED_RECEIPT_ROOT
        receipt_root.mkdir(parents=True, exist_ok=True)
        historical = {
            "schema_version": runtime.MANAGED_RUNTIME_SCHEMA,
            "operation": "refresh",
            "outcome": "completed",
            "target_sha": self.previous,
            "scripts": [{
                "path": ".juno_task/scripts/one.py",
                "classification": "exact",
                "source_sha256": runtime.managed_sha256(b"old one\n"),
                "actual_sha256": runtime.managed_sha256(b"old one\n"),
            }],
        }
        (receipt_root / "100-old.json").write_text(json.dumps(historical) + "\n")
        git(self.repo, "commit", "--allow-empty", "-m", "unchanged next generation")
        final = git(self.repo, "rev-parse", "HEAD")
        runtime.managed_runtime_refresh(
            self.controller, self.repo, self.target, final, task_id="installed-middle")
        installed = self.controller / ".juno_task/scripts/one.py"
        installed.write_text("old one\n")

        result = runtime.managed_runtime_refresh(
            self.controller, self.repo, self.target, final, task_id="obsolete-bootstrap")

        self.assertEqual(installed.read_text(), "new one\n")
        row = next(item for item in result["scripts"] if item["path"].endswith("one.py"))
        self.assertEqual(row["outcome"], "updated")
        self.assertEqual(row["classification"], "exact")
        self.assertEqual(row["prior_generation_classification"],
                         "receipt_bound_obsolete_generation")
        self.assertEqual(row["prior_generation_target_sha"], self.previous)
        self.assertEqual(Path(row["prior_generation_receipt"]),
                         (receipt_root / "100-old.json").resolve())

    def test_failed_or_incomplete_receipts_cannot_authorize_obsolete_generation(self) -> None:
        receipt_root = self.controller / runtime.MANAGED_RECEIPT_ROOT
        receipt_root.mkdir(parents=True, exist_ok=True)
        source_hash = runtime.managed_sha256(b"old one\n")
        for outcome in ("failed", "running"):
            with self.subTest(outcome=outcome):
                receipt = receipt_root / f"{outcome}.json"
                receipt.write_text(json.dumps({
                    "schema_version": runtime.MANAGED_RUNTIME_SCHEMA,
                    "operation": "refresh", "outcome": outcome,
                    "target_sha": self.previous,
                    "scripts": [{"path": ".juno_task/scripts/one.py", "classification": "exact",
                                 "source_sha256": source_hash, "actual_sha256": source_hash}],
                }) + "\n")
                self.assertIsNone(runtime.managed_obsolete_generation_binding(
                    self.controller, self.repo, ".juno_task/scripts/one.py",
                    b"old one\n", self.target))
                receipt.unlink()

    def test_receipt_target_outside_admitted_ancestry_cannot_authorize_replacement(self) -> None:
        receipt_root = self.controller / runtime.MANAGED_RECEIPT_ROOT
        receipt_root.mkdir(parents=True, exist_ok=True)
        tree = git(self.repo, "rev-parse", f"{self.previous}^{{tree}}")
        unrelated = git(self.repo, "commit-tree", tree, "-m", "unrelated exact source")
        source_hash = runtime.managed_sha256(b"old one\n")
        (receipt_root / "unrelated.json").write_text(json.dumps({
            "schema_version": runtime.MANAGED_RUNTIME_SCHEMA,
            "operation": "refresh", "outcome": "completed", "target_sha": unrelated,
            "scripts": [{"path": ".juno_task/scripts/one.py", "classification": "exact",
                         "source_sha256": source_hash, "actual_sha256": source_hash}],
        }) + "\n")

        self.assertIsNone(runtime.managed_obsolete_generation_binding(
            self.controller, self.repo, ".juno_task/scripts/one.py",
            b"old one\n", self.target))

    def test_preserved_customization_receipt_row_can_bind_immutable_historical_source(self) -> None:
        receipt_root = self.controller / runtime.MANAGED_RECEIPT_ROOT
        receipt_root.mkdir(parents=True, exist_ok=True)
        source_hash = runtime.managed_sha256(b"old one\n")
        (receipt_root / "preserved.json").write_text(json.dumps({
            "schema_version": runtime.MANAGED_RUNTIME_SCHEMA,
            "operation": "refresh", "outcome": "completed", "target_sha": self.previous,
            "scripts": [{"path": ".juno_task/scripts/one.py",
                         "classification": "preserved_customization",
                         "source_sha256": "f" * 64, "actual_sha256": source_hash}],
        }) + "\n")

        binding = runtime.managed_obsolete_generation_binding(
            self.controller, self.repo, ".juno_task/scripts/one.py",
            b"old one\n", self.target)
        self.assertEqual(binding["classification"], "receipt_bound_historical_generation")
        self.assertEqual(binding["target_sha"], self.previous)

    def test_preserved_receipt_recognizes_historical_template_bytes(self) -> None:
        receipt_root = self.controller / runtime.MANAGED_RECEIPT_ROOT
        receipt_root.mkdir(parents=True, exist_ok=True)
        current = b"installed prior one\n"
        current_hash = runtime.managed_sha256(current)
        (receipt_root / "template-history.json").write_text(json.dumps({
            "schema_version": runtime.MANAGED_RUNTIME_SCHEMA,
            "operation": "refresh", "outcome": "completed", "target_sha": self.previous,
            "scripts": [{"path": ".juno_task/scripts/one.py",
                         "classification": "preserved_customization",
                         "source_sha256": runtime.managed_sha256(b"old one\n"),
                         "actual_sha256": current_hash}],
        }) + "\n")

        binding = runtime.managed_obsolete_generation_binding(
            self.controller, self.repo, ".juno_task/scripts/one.py", current, self.target)

        self.assertEqual(binding["classification"], "receipt_bound_historical_generation")
        self.assertEqual(binding["source_path"], "juno-code/src/templates/scripts/one.py")
        self.assertEqual(binding["target_sha"], self.previous)
        installed = self.controller / ".juno_task/scripts/one.py"
        installed.write_bytes(current)
        result = runtime.managed_runtime_refresh(
            self.controller, self.repo, self.previous, self.target, task_id="historical-supersede")
        self.assertEqual(installed.read_bytes(), b"new one\n")
        row = next(item for item in result["scripts"] if item["path"].endswith("one.py"))
        self.assertEqual(row["prior_generation_classification"],
                         "receipt_bound_historical_generation")

    def test_preserved_receipt_cannot_bind_content_that_only_appears_later(self) -> None:
        receipt_root = self.controller / runtime.MANAGED_RECEIPT_ROOT
        receipt_root.mkdir(parents=True, exist_ok=True)
        unique = b"unique operator bytes that appear later\n"
        unique_hash = runtime.managed_sha256(unique)
        (receipt_root / "before-unique.json").write_text(json.dumps({
            "schema_version": runtime.MANAGED_RUNTIME_SCHEMA,
            "operation": "refresh", "outcome": "completed", "target_sha": self.previous,
            "scripts": [{"path": ".juno_task/scripts/one.py",
                         "classification": "preserved_customization",
                         "source_sha256": runtime.managed_sha256(b"old one\n"),
                         "actual_sha256": unique_hash}],
        }) + "\n")
        self.write("juno-code/src/templates/scripts/one.py", unique.decode())
        git(self.repo, "add", "juno-code/src/templates/scripts/one.py")
        git(self.repo, "commit", "-m", "later coincidental content")
        self.write(".juno_task/scripts/one.py", "final managed one\n")
        self.write("juno-code/src/templates/scripts/one.py", "final template one\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "final generation")
        final = git(self.repo, "rev-parse", "HEAD")

        binding = runtime.managed_obsolete_generation_binding(
            self.controller, self.repo, ".juno_task/scripts/one.py", unique, final)

        self.assertIsNone(binding)

    def test_overlap_repair_plan_apply_preserves_prior_bytes_and_is_retry_safe(self) -> None:
        self.write(".juno_task/scripts/one.py", "first\na\nb\nc\nlast\n")
        git(self.repo, "add", ".juno_task/scripts/one.py")
        git(self.repo, "commit", "-m", "repair base")
        previous = git(self.repo, "rev-parse", "HEAD")
        self.write(".juno_task/scripts/one.py", "first\na\nb\nc\nnew last\n")
        git(self.repo, "add", ".juno_task/scripts/one.py")
        git(self.repo, "commit", "-m", "repair target")
        target = git(self.repo, "rev-parse", "HEAD")
        customized = self.controller / ".juno_task/scripts/one.py"
        customized.write_text("owner first\na\nb\nc\nlast\n")
        prior = customized.read_bytes()

        plan = runtime.managed_runtime_repair_plan(
            self.controller, self.repo, previous, target, task_id="repair")

        self.assertEqual(plan["outcome"], "planned", plan)
        row = next(item for item in plan["actions"] if item["path"].endswith("one.py"))
        self.assertEqual(row["resolution"], "preserve")
        self.assertEqual(row["current_sha256"], runtime.managed_sha256(prior))
        self.assertIn("old-source", row["semantic_diff"]["old_to_new"])
        result = runtime.managed_runtime_refresh(
            self.controller, self.repo, previous, target, task_id="repair-apply",
            repair_receipt=Path(plan["receipt"]["path"]))
        self.assertEqual(result["outcome"], "completed")
        self.assertEqual(result["repair_plan"]["sha256"], plan["receipt"]["sha256"])
        self.assertIn("owner first", customized.read_text())
        backups = list((self.controller / runtime.MANAGED_BACKUP_ROOT).rglob("*.bin"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), prior)
        retried = runtime.managed_runtime_refresh(
            self.controller, self.repo, previous, target, task_id="repair-retry")
        self.assertEqual(retried["outcome"], "completed")

    def test_overlap_repair_requires_exact_current_action_set(self) -> None:
        self.write(".juno_task/scripts/one.py", "first\na\nb\nc\nlast\n")
        git(self.repo, "add", ".juno_task/scripts/one.py")
        git(self.repo, "commit", "-m", "exact set base")
        previous = git(self.repo, "rev-parse", "HEAD")
        self.write(".juno_task/scripts/one.py", "first\na\nb\nc\nnew last\n")
        git(self.repo, "add", ".juno_task/scripts/one.py")
        git(self.repo, "commit", "-m", "exact set target")
        target = git(self.repo, "rev-parse", "HEAD")
        customized = self.controller / ".juno_task/scripts/one.py"
        customized.write_text("owner first\na\nb\nc\nlast\n")
        plan = runtime.managed_runtime_repair_plan(
            self.controller, self.repo, previous, target, task_id="exact-set")
        source = json.loads(Path(plan["receipt"]["path"]).read_text())

        def mutated(actions: list[dict[str, object]]) -> Path:
            value = {**source, "actions": actions}
            data = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
            path = (self.controller / runtime.MANAGED_RECEIPT_ROOT /
                    f"{runtime.managed_sha256(data)}-repair-plan.json")
            path.write_bytes(data)
            return path

        extra = dict(source["actions"][0])
        extra["path"] = ".juno_task/scripts/two.py"
        with self.assertRaisesRegex(runtime.ManagedRuntimeError, "action set mismatch"):
            runtime.managed_runtime_refresh(
                self.controller, self.repo, previous, target,
                repair_receipt=mutated([source["actions"][0], extra]))
        with self.assertRaisesRegex(runtime.ManagedRuntimeError, "action set mismatch"):
            runtime.managed_runtime_refresh(
                self.controller, self.repo, previous, target, repair_receipt=mutated([]))
        with self.assertRaisesRegex(runtime.ManagedRuntimeError, "malformed managed runtime repair action"):
            runtime.managed_runtime_refresh(
                self.controller, self.repo, previous, target,
                repair_receipt=mutated([source["actions"][0], source["actions"][0]]))
        self.assertEqual(customized.read_text(), "owner first\na\nb\nc\nlast\n")
        self.assertFalse((self.controller / runtime.MANAGED_BACKUP_ROOT).exists())

    def test_overlap_repair_conflict_stale_and_malformed_receipts_fail_closed(self) -> None:
        customized = self.controller / ".juno_task/scripts/one.py"
        customized.write_text("owner replacement\n")
        plan = runtime.managed_runtime_repair_plan(
            self.controller, self.repo, self.previous, self.target, task_id="conflict")
        self.assertEqual(plan["outcome"], "conflict")
        with self.assertRaisesRegex(runtime.ManagedRuntimeError, "malformed or mismatched"):
            runtime.managed_runtime_refresh(
                self.controller, self.repo, self.previous, self.target,
                repair_receipt=Path(plan["receipt"]["path"]))

        self.write(".juno_task/scripts/one.py", "first\na\nb\nc\nlast\n")
        git(self.repo, "add", ".juno_task/scripts/one.py")
        git(self.repo, "commit", "-m", "stale repair base")
        previous = git(self.repo, "rev-parse", "HEAD")
        self.write(".juno_task/scripts/one.py", "first\na\nb\nc\nnew last\n")
        git(self.repo, "add", ".juno_task/scripts/one.py")
        git(self.repo, "commit", "-m", "stale repair target")
        target = git(self.repo, "rev-parse", "HEAD")
        customized.write_text("owner first\na\nb\nc\nlast\n")
        valid = runtime.managed_runtime_repair_plan(
            self.controller, self.repo, previous, target, task_id="stale")
        customized.write_text("changed after approval\n")
        with self.assertRaisesRegex(runtime.ManagedRuntimeError, "stale managed runtime repair identity"):
            runtime.managed_runtime_refresh(
                self.controller, self.repo, previous, target,
                repair_receipt=Path(valid["receipt"]["path"]))
        malformed = self.controller / runtime.MANAGED_RECEIPT_ROOT / "malformed.json"
        malformed.write_text("{}\n")
        with self.assertRaisesRegex(runtime.ManagedRuntimeError, "repair receipt"):
            runtime.managed_runtime_refresh(
                self.controller, self.repo, self.previous, self.target,
                repair_receipt=malformed)
        self.assertEqual(customized.read_text(), "changed after approval\n")

    def test_obsolete_receipt_cannot_authorize_bytes_that_do_not_match_git_source(self) -> None:
        receipt_root = self.controller / runtime.MANAGED_RECEIPT_ROOT
        receipt_root.mkdir(parents=True, exist_ok=True)
        customized = self.controller / ".juno_task/scripts/one.py"
        customized.write_text("genuine owner customization\n")
        forged_hash = runtime.managed_sha256(customized.read_bytes())
        (receipt_root / "forged.json").write_text(json.dumps({
            "schema_version": runtime.MANAGED_RUNTIME_SCHEMA,
            "operation": "refresh", "outcome": "completed", "target_sha": self.previous,
            "scripts": [{"path": ".juno_task/scripts/one.py", "classification": "exact",
                         "source_sha256": forged_hash, "actual_sha256": forged_hash}],
        }) + "\n")

        with self.assertRaisesRegex(runtime.ManagedRuntimeError, "customized managed runtime"):
            runtime.managed_runtime_refresh(
                self.controller, self.repo, self.previous, self.target, task_id="forged-history")
        self.assertEqual(customized.read_text(), "genuine owner customization\n")

    def test_receipt_binding_never_authorizes_unrelated_customization(self) -> None:
        customized = self.controller / ".juno_task/scripts/one.py"
        customized.write_text("genuine owner customization\n")
        generation_path = self.controller / runtime.MANAGED_GENERATION_PATH
        generation_path.parent.mkdir(parents=True, exist_ok=True)
        generation_path.write_text(json.dumps({
            "schema_version": runtime.MANAGED_RUNTIME_SCHEMA,
            "target_sha": self.previous,
            "scripts": {".juno_task/scripts/one.py": {
                "classification": "preserved_customization",
                "source_sha256": runtime.managed_sha256(b"old one\n"),
                "actual_sha256": runtime.managed_sha256(customized.read_bytes()),
            }},
        }) + "\n")
        with self.assertRaisesRegex(runtime.ManagedRuntimeError, "customized managed runtime"):
            runtime.managed_runtime_refresh(
                self.controller, self.repo, self.previous, self.target, task_id="genuine-custom")
        self.assertEqual(customized.read_text(), "genuine owner customization\n")

    def test_refresh_refuses_changed_source_customization_and_rolls_back(self) -> None:
        customized = self.controller / ".juno_task/scripts/one.py"
        customized.write_text("owner customization\n")
        unchanged = self.controller / ".juno_task/scripts/two.py"
        before_unchanged = unchanged.read_bytes()
        before_policy = (self.controller / runtime.MANAGED_POLICY_PATH).read_bytes()
        with self.assertRaisesRegex(runtime.ManagedRuntimeError, "customized managed runtime") as caught:
            runtime.managed_runtime_refresh(self.controller, self.repo, self.previous, self.target, task_id="custom")
        self.assertEqual(customized.read_text(), "owner customization\n")
        self.assertEqual(unchanged.read_bytes(), before_unchanged)
        self.assertEqual((self.controller / runtime.MANAGED_POLICY_PATH).read_bytes(), before_policy)
        self.assertFalse((self.controller / runtime.MANAGED_GENERATION_PATH).exists())
        self.assertIsNotNone(caught.exception.receipt)
        persisted = json.loads(Path(caught.exception.receipt["path"]).read_text())
        self.assertEqual(persisted["outcome"], "failed")
        self.assertEqual(persisted["exit_code"], 2)

    def test_log_allocation_is_unique_for_concurrent_runs_and_fails_closed(self) -> None:
        def allocate(_: int) -> str:
            path, handle = runtime.managed_allocate_log("workflow with spaces", "task id")
            handle.close()
            return str(path)

        with ThreadPoolExecutor(max_workers=4) as pool:
            paths = list(pool.map(allocate, range(8)))
        self.assertEqual(len(paths), len(set(paths)))
        self.assertTrue(all(path.startswith("/tmp/yy-workflow-with-spaces-task-id-")
                            for path in paths))
        for value in paths:
            Path(value).unlink()
        with mock.patch.object(Path, "open", side_effect=OSError("read-only log root")):
            with self.assertRaisesRegex(runtime.ManagedRuntimeError, "log allocation failed"):
                runtime.managed_allocate_log("workflow", "task")

    def test_interruption_is_terminal_and_receipted(self) -> None:
        with mock.patch.object(runtime, "managed_runtime_plan", side_effect=KeyboardInterrupt()):
            with self.assertRaises(runtime.ManagedRuntimeError) as caught:
                runtime.managed_runtime_refresh(self.controller, self.repo, self.previous, self.target,
                                task_id="interrupt")
        persisted = json.loads(Path(caught.exception.receipt["path"]).read_text())
        self.assertEqual(persisted["termination"], "interrupted")
        self.assertIsNone(persisted["signal"])
        self.assertFalse(persisted["timed_out"])

    def test_refresh_refuses_dirty_or_overlapping_tracked_policy(self) -> None:
        policy_path = self.controller / runtime.MANAGED_POLICY_PATH
        value = json.loads(policy_path.read_text())
        value["workspace_root"] = "/tmp/uncommitted"
        policy_path.write_text(json.dumps(value) + "\n")
        with self.assertRaisesRegex(runtime.ManagedRuntimeError, "uncommitted dirt"):
            runtime.managed_runtime_plan(self.controller, self.repo, self.previous, self.target)
        git(self.controller, "checkout", "--", runtime.MANAGED_POLICY_PATH)
        value = json.loads(policy_path.read_text())
        value["selectable_paths"] = ["different"]
        policy_path.write_text(json.dumps(value) + "\n")
        git(self.controller, "add", runtime.MANAGED_POLICY_PATH)
        git(self.controller, "commit", "-m", "overlap")
        with self.assertRaisesRegex(runtime.ManagedRuntimeError, "overlapping manual change"):
            runtime.managed_runtime_plan(self.controller, self.repo, self.previous, self.target)


if __name__ == "__main__":
    unittest.main()
