#!/usr/bin/env python3
"""Scoped hydration contracts using the real runner and task-local empty locks."""
import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import task_workspace as runtime


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.STDOUT).decode().strip()


class ScopedHydrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.worktree = self.root / "product"
        self.controller = self.root / "controller"
        self.worktree.mkdir()
        environment = {key: value for key, value in os.environ.items() if not key.startswith("JUNO_")}
        patch = mock.patch.dict(os.environ, environment, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        (self.worktree / ".juno_task").mkdir()
        git(self.worktree, "init", "-q")
        git(self.worktree, "config", "user.email", "fixture@example.invalid")
        git(self.worktree, "config", "user.name", "Fixture")
        (self.worktree / ".gitignore").write_text("**/node_modules/\n.juno_task/\n")
        for root in ("cli", "bench"):
            package = self.worktree / root
            package.mkdir()
            (package / "package.json").write_text(json.dumps({"name": root, "version": "1.0.0"}))
            (package / "package-lock.json").write_text(json.dumps({
                "name": root, "version": "1.0.0", "lockfileVersion": 3,
                "requires": True, "packages": {"": {"name": root, "version": "1.0.0"}}}))
        self.config = {
            "allowed_paths": ["cli", "bench", "docs"],
            "hydration_workflow": "hydration.json",
            "focused_validation": [{"id": "focused", "cwd": "cli", "argv": ["npm", "test"]}],
            "full_suite_validation": {"id": "full", "cwd": "cli", "argv": ["npm", "test"]},
            "validation_profiles": [{"id": "bench", "path_roots": ["bench"], "commands": [
                {"id": "bench-test", "cwd": "bench", "argv": ["npm", "test"]}]}],
        }
        self.workflow = {"schema_version": "v1", "workflow_id": "scoped-fixture",
                         "workflow_class": "task_hydration", "hydration_selection": "admitted_scope_v1",
                         "steps": [self.step(root) for root in ("cli", "bench")] + [self.clean_step()]}
        self.save_workflow()

    def step(self, root):
        # Real task-local preparation without downloads or borrowed dependency directories.
        install = ("import pathlib,hashlib,json; p=pathlib.Path(" + repr(root) + "); "
                   "n=p/'node_modules'; n.mkdir(exist_ok=True); "
                   "(n/'.package-lock.json').write_text(json.dumps({'lockfileVersion':3,'packages':{}})); "
                   "(n/'.yylo-package-lock.sha256').write_text(hashlib.sha256((p/'package-lock.json').read_bytes()).hexdigest())")
        probe = ("import pathlib,hashlib,sys; p=pathlib.Path(" + repr(root) + "); "
                 "s=p/'node_modules/.yylo-package-lock.sha256'; "
                 "sys.exit(0 if s.is_file() and s.read_text()==hashlib.sha256((p/'package-lock.json').read_bytes()).hexdigest() else 1)")
        return {"id": root, "dependency_root": root, "command": [sys.executable, "-c", install],
                "probe": [sys.executable, "-c", probe], "timeout_seconds": 30,
                "non_interactive": True, "fail_workflow": True, "network": False,
                "sensitive": False, "outputs": [root + "/node_modules"]}

    def clean_step(self):
        return {"id": "clean", "command": [sys.executable, "-c", "pass"],
                "probe": [sys.executable, "-c", "pass"], "timeout_seconds": 30,
                "non_interactive": True, "fail_workflow": True, "network": False,
                "sensitive": False, "outputs": []}

    def save_workflow(self):
        (self.worktree / "hydration.json").write_text(json.dumps(self.workflow))
        git(self.worktree, "add", ".")
        git(self.worktree, "commit", "-qm", "fixture", "--allow-empty")
        self.frozen = runtime.hydration_identity(self.worktree, "HEAD", self.config)

    def hydrate(self, scope):
        started = time.monotonic()
        # Exercise the real runner APIs, isolating only installed-controller Python
        # bootstrap. No fake step execution, registry downloads, or borrowed trees.
        real_run = subprocess.run
        def runner_api(argv, **kwargs):
            if len(argv) > 1 and str(argv[1]).endswith("workflow_runner.sh"):
                code = ("import importlib.machinery,sys; "
                        "m=importlib.machinery.SourceFileLoader('runner',sys.argv[1]).load_module(); "
                        "a=sys.argv[2:]; "
                        "sys.exit(m.run_lint_command(a[1:]) if a[0]=='lint' else m.run_workflow(m.build_parser().parse_args(a)))")
                argv = [argv[0], "-c", code, *argv[1:]]
            return real_run(argv, **kwargs)
        try:
            with mock.patch.object(subprocess, "run", side_effect=runner_api):
                evidence = runtime.run_task_hydration(self.controller, self.worktree, "SCOPED",
                                                       self.frozen, self.config, scope)
        except runtime.HydrationFailure as exc:
            artifact = Path(exc.evidence.get("artifact_dir", self.root))
            for name in ("lint.stdout", "lint.stderr"):
                if (artifact / name).is_file():
                    print((artifact / name).read_text(), file=sys.stderr)
            raise
        print(json.dumps({"fixture_scope": scope, "wall_seconds": round(time.monotonic()-started, 3),
                          "dependency_steps": len(evidence["selection"]["dependency_roots"])
                          if "selection" in evidence else len(self.workflow["steps"]) - 1,
                          "steps_avoided": len(evidence.get("selection", {}).get("omitted_step_ids", []))}))
        return {"creation_receipt": {"allowed_paths": scope, "hydration_workflow": self.frozen},
                "hydration": evidence}

    def verify(self, record):
        runtime.verify_hydration_evidence(record, self.worktree, self.config)
        runtime._verify_dependency_tree(self.worktree, self.config, record["hydration"])

    def test_cli_only_ignores_unrelated_failure_and_missing_benchmark(self):
        self.workflow["steps"][1]["command"] = [sys.executable, "-c", "raise SystemExit(7)"]
        self.save_workflow()
        record = self.hydrate(["cli"])
        self.assertEqual(record["hydration"]["selection"]["step_ids"], ["cli", "clean"])
        self.assertFalse((self.worktree / "bench/node_modules").exists())
        self.verify(record)
        with self.assertRaisesRegex(runtime.HydrationFailure, "hydration run failed"):
            self.hydrate(["cli", "bench"])
        # Unrelated metadata and unselected lock movement must not invalidate readiness.
        record["task_title"] = "metadata only"
        self.config["validation_profiles"][0]["commands"][0]["argv"] = ["npm", "run", "unrelated"]
        (self.worktree / "bench/package-lock.json").write_text("unrelated change")
        self.verify(record)

    def test_benchmark_only_omits_default_validation_dependencies(self):
        record = self.hydrate(["bench"])
        self.assertEqual(record["hydration"]["selection"]["dependency_roots"], ["bench"])
        self.assertFalse((self.worktree / "cli/node_modules").exists())
        self.verify(record)
        stamp = self.worktree / "bench/node_modules/.yylo-package-lock.sha256"
        before = stamp.stat().st_mtime_ns
        self.verify(self.hydrate(["bench"]))
        self.assertEqual(stamp.stat().st_mtime_ns, before)

    def test_multi_root_and_broad_scope_keep_all_required_steps(self):
        for scope in (["cli", "bench"], self.config["allowed_paths"]):
            record = self.hydrate(scope)
            self.assertEqual(record["hydration"]["selection"]["dependency_roots"], ["bench", "cli"])
            self.verify(record)

    def test_validation_requirements_add_roots_and_parent_scope_is_broad(self):
        selected = runtime.hydration_selection(self.workflow, self.config, ["docs"])
        self.assertEqual(selected["dependency_roots"], ["cli"])
        config = copy.deepcopy(self.config)
        config["focused_validation"][0]["input_paths"] = ["bench/package.json"]
        selected = runtime.hydration_selection(self.workflow, config, ["cli"])
        self.assertEqual(selected["dependency_roots"], ["bench", "cli"])
        workflow = copy.deepcopy(self.workflow)
        for step in workflow["steps"][:2]:
            step["dependency_root"] = "packages/" + step["dependency_root"]
        config["validation_profiles"][0]["path_roots"] = ["packages/bench"]
        selected = runtime.hydration_selection(workflow, config, ["packages"])
        self.assertEqual(selected["dependency_roots"], ["packages/bench", "packages/cli"])

    def test_scope_expansion_requires_explicit_preparation(self):
        record = self.hydrate(["cli"])
        record["creation_receipt"]["allowed_paths"] = ["cli", "bench"]
        with self.assertRaisesRegex(runtime.TaskWorkspaceError, "scope or validation requirements changed"):
            self.verify(record)
        # This is the hydration primitive used by explicit hydrate after admitted expansion.
        self.verify(self.hydrate(["cli", "bench"]))

    def test_validation_policy_movement_requires_preparation(self):
        record = self.hydrate(["cli"])
        self.config["focused_validation"][0]["input_paths"] = ["bench"]
        with self.assertRaisesRegex(runtime.TaskWorkspaceError, "scope or validation requirements changed"):
            self.verify(record)
        self.verify(self.hydrate(["cli"]))

    def test_missing_stale_tampered_dependencies_and_selection_refuse_finish(self):
        record = self.hydrate(["cli"])
        stamp = self.worktree / "cli/node_modules/.yylo-package-lock.sha256"
        original = stamp.read_text()
        stamp.write_text("stale")
        with self.assertRaisesRegex(runtime.TaskWorkspaceError, "missing or stale"):
            self.verify(record)
        stamp.write_text(original)
        sentinel = self.worktree / "cli/node_modules/.package-lock.json"
        sentinel_bytes = sentinel.read_bytes()
        sentinel.unlink()
        with self.assertRaisesRegex(runtime.TaskWorkspaceError, "node_modules is absent"):
            self.verify(record)
        sentinel.write_bytes(sentinel_bytes)
        lock = self.worktree / "cli/package-lock.json"
        original_lock = lock.read_text()
        lock.write_text(original_lock + "\n")
        with self.assertRaisesRegex(runtime.TaskWorkspaceError, "locks are missing or stale"):
            self.verify(record)
        lock.unlink()
        with self.assertRaisesRegex(runtime.TaskWorkspaceError, "package-lock.json is absent"):
            self.verify(record)
        lock.write_text(original_lock)
        (self.worktree / "cli/node_modules/extra.js").write_text("tampered")
        with self.assertRaisesRegex(runtime.TaskWorkspaceError, "contents drifted"):
            self.verify(record)
        record["hydration"].pop("selection")
        with self.assertRaisesRegex(runtime.TaskWorkspaceError, "selection is absent"):
            self.verify(record)

    def test_workflow_movement_invalidates_frozen_readiness(self):
        record = self.hydrate(["cli"])
        with (self.worktree / "hydration.json").open("a") as handle:
            handle.write("\n")
        with self.assertRaisesRegex(runtime.TaskWorkspaceError, "workflow moved"):
            self.verify(record)

    def test_missing_required_install_refuses_agent_ready(self):
        self.workflow["steps"][0]["command"] = [sys.executable, "-c", "pass"]
        self.save_workflow()
        with self.assertRaisesRegex(runtime.HydrationFailure, "node_modules is absent"):
            self.hydrate(["cli"])

    def test_stale_required_install_refuses_agent_ready(self):
        self.workflow["steps"][0]["command"][2] += "; (n/'.yylo-package-lock.sha256').write_text('stale')"
        self.save_workflow()
        with self.assertRaisesRegex(runtime.HydrationFailure, "missing or stale"):
            self.hydrate(["cli"])

    def test_existing_workflows_keep_all_steps_and_retry_probes(self):
        self.workflow.pop("hydration_selection")
        for step in self.workflow["steps"]:
            step.pop("dependency_root", None)
        self.save_workflow()
        record = self.hydrate(["cli"])
        self.assertNotIn("selection", record["hydration"])
        self.assertTrue((self.worktree / "bench/node_modules").is_dir())
        before = (self.worktree / "cli/node_modules/.yylo-package-lock.sha256").stat().st_mtime_ns
        self.hydrate(["cli"])
        self.assertEqual(before, (self.worktree / "cli/node_modules/.yylo-package-lock.sha256").stat().st_mtime_ns)

    def test_unknown_policy_and_unsafe_root_refuse(self):
        for field, value in (("hydration_selection", "guess-from-title"), ("dependency_root", "../outside")):
            workflow = copy.deepcopy(self.workflow)
            if field == "dependency_root":
                workflow["steps"][0][field] = value
            else:
                workflow[field] = value
            with self.assertRaises(runtime.TaskWorkspaceError):
                runtime._hydration_workflow(json.dumps(workflow).encode())


if __name__ == "__main__":
    unittest.main()
