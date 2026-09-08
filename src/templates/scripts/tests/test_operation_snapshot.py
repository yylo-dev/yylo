#!/usr/bin/env python3
"""Focused contracts for immutable lifecycle operation snapshots."""
from __future__ import annotations

import importlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class OperationSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for relative, body in {
            "runtime/merge.py": "merge-v1\n",
            "runtime/validate.py": "validate-v1\n",
            "config/routing.json": '{"route":"focused"}\n',
            "config/risk.json": '{"tier":"normal"}\n',
            "prompts/reviewer.md": "review prompt v1\n",
            "prompts/findings.json": '{"blocking":["correctness"]}\n',
            "docs/README.md": "active documentation v1\n",
            "runtime/integrate.py": "integrate-v1\n",
            "metadata/task.json": "unrelated task v1\n",
            "wiki/PDR.md": "unrelated pdr v1\n",
        }.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")

    def module(self):
        try:
            return importlib.import_module("operation_snapshot")
        except ModuleNotFoundError as exc:
            self.fail(f"operation_snapshot behavior is absent: {exc}")

    def submission(self):
        return {
            "task_id": "Task1", "requirements_sha256": "1" * 64,
            "admitted_scope_sha256": "2" * 64, "generated_scope_sha256": "3" * 64,
            "base_sha": "c" * 40, "tip_sha": "a" * 40, "tree_sha": "d" * 40,
            "origin_projection_sha256": "4" * 64, "hydration_sha256": "5" * 64,
            "dependency_sha256": "6" * 64, "runtime_sha256": "7" * 64,
            "validation_sha256": "8" * 64, "risk_sha256": "9" * 64,
        }

    def spec(self, *, root: Path | None = None, reverse: bool = False):
        units = [
            {"phase": "validation", "id": "focused-a", "paths": ["runtime/validate.py", "config/routing.json"]},
            {"phase": "validation", "id": "focused-b", "paths": ["runtime/merge.py"]},
            {"phase": "risk", "id": "risk", "paths": ["config/risk.json"]},
            {"phase": "review", "id": "semantic", "paths": ["prompts/reviewer.md", "prompts/findings.json"]},
            {"phase": "documentation", "id": "active-docs", "paths": ["docs/README.md"]},
            {"phase": "integration", "id": "target-cas", "paths": ["runtime/integrate.py"]},
        ]
        if reverse:
            units.reverse()
            for unit in units:
                unit["paths"] = list(reversed(unit["paths"]))
        return {
            "root": root or self.root,
            "candidate": "a" * 40,
            "target": "b" * 40,
            "commands": [
                {"id": "focused-b", "cwd": ".", "argv": ["python3", "b.py"]},
                {"id": "focused-a", "cwd": ".", "argv": ["python3", "a.py"]},
            ][::-1] if reverse else [
                {"id": "focused-b", "cwd": ".", "argv": ["python3", "b.py"]},
                {"id": "focused-a", "cwd": ".", "argv": ["python3", "a.py"]},
            ],
            "routing": {"focused-a": "focused", "focused-b": "focused"},
            "environment": {"TZ": "UTC", "CI": "1"},
            "read_sets": units,
            "managed_outputs": {"runtime/merge.py": "managed-v1"},
            "submission": self.submission(),
            "discovery": {"complete": True, "kind": "exact-import-closure"},
        }

    def compile(self, **overrides):
        values = self.spec()
        values.update(overrides)
        return self.module().compile_operation_snapshot(**values)

    def test_snapshot_identity_is_deterministic_across_checkout_paths_and_enumeration_order(self) -> None:
        other = self.root.parent / (self.root.name + "-copy")
        self.addCleanup(lambda: __import__("shutil").rmtree(other, ignore_errors=True))
        __import__("shutil").copytree(self.root, other)
        first = self.module().compile_operation_snapshot(**self.spec())
        second = self.module().compile_operation_snapshot(**self.spec(root=other, reverse=True))
        self.assertEqual(first["snapshot_sha256"], second["snapshot_sha256"])
        self.assertNotIn(str(self.root), repr(first))
        self.assertTrue(self.module().verify_operation_snapshot(first)["valid"])

    def test_controller_head_movement_does_not_invalidate_an_active_snapshot(self) -> None:
        snapshot = self.compile(observed_controller_head="1" * 40)
        moved = self.compile(observed_controller_head="2" * 40)
        self.assertEqual(snapshot["snapshot_sha256"], moved["snapshot_sha256"])
        self.assertEqual([], self.module().phase_invalidation(snapshot, moved))

    def test_unrelated_dirty_staged_and_untracked_controller_paths_are_not_read_inputs(self) -> None:
        snapshot = self.compile(controller_status=[" M metadata/task.json"])
        (self.root / "metadata/task.json").write_text("dirty and staged\n", encoding="utf-8")
        (self.root / "wiki/PDR.md").write_text("new pdr\n", encoding="utf-8")
        (self.root / "untracked.log").write_text("untracked\n", encoding="utf-8")
        current = self.compile(controller_status=["M  metadata/task.json", " M wiki/PDR.md", "?? untracked.log"])
        self.assertEqual(snapshot["snapshot_sha256"], current["snapshot_sha256"])
        self.assertEqual([], self.module().phase_invalidation(snapshot, current))

    def test_live_target_movement_preserves_submission_and_invalidates_only_composition(self) -> None:
        snapshot = self.compile()
        current = self.compile(target="e" * 40)
        self.assertEqual(snapshot["submission_sha256"], current["submission_sha256"])
        self.assertEqual(
            [{"phase": "integration", "unit_id": "live-target", "reason": "target_drift"}],
            self.module().phase_invalidation(snapshot, current),
        )

    def test_validation_runtime_and_routing_drift_invalidates_only_affected_shard(self) -> None:
        snapshot = self.compile()
        (self.root / "runtime/validate.py").write_text("validate-v2\n", encoding="utf-8")
        current = self.compile()
        self.assertEqual(
            [{"phase": "validation", "unit_id": "focused-a", "reason": "read_set_drift", "changed_paths": ["runtime/validate.py"]}],
            self.module().phase_invalidation(snapshot, current),
        )
        (self.root / "runtime/validate.py").write_text("validate-v1\n", encoding="utf-8")
        routing = {"focused-a": "full", "focused-b": "focused"}
        rerouted = self.compile(routing=routing)
        self.assertEqual(
            [{"phase": "validation", "unit_id": "focused-a", "reason": "command_or_routing_drift"}],
            self.module().phase_invalidation(snapshot, rerouted),
        )

    def test_risk_config_and_reviewer_prompt_changes_restart_only_their_phases(self) -> None:
        snapshot = self.compile()
        (self.root / "config/risk.json").write_text('{"tier":"high"}\n', encoding="utf-8")
        risk = self.compile()
        self.assertEqual(["risk"], [row["phase"] for row in self.module().phase_invalidation(snapshot, risk)])
        (self.root / "config/risk.json").write_text('{"tier":"normal"}\n', encoding="utf-8")
        (self.root / "prompts/reviewer.md").write_text("review prompt v2\n", encoding="utf-8")
        review = self.compile()
        rows = self.module().phase_invalidation(snapshot, review)
        self.assertEqual(["review"], [row["phase"] for row in rows])
        self.assertNotIn("validation", {row["phase"] for row in rows})
        (self.root / "prompts/reviewer.md").write_text("review prompt v1\n", encoding="utf-8")
        (self.root / "prompts/findings.json").write_text('{"blocking":["security"]}\n', encoding="utf-8")
        findings = self.compile()
        self.assertEqual(["review"], [row["phase"] for row in self.module().phase_invalidation(snapshot, findings)])

    def test_active_documentation_change_restarts_its_audit_and_dependent_review_only(self) -> None:
        snapshot = self.compile()
        (self.root / "docs/README.md").write_text("active documentation v2\n", encoding="utf-8")
        current = self.compile()
        rows = self.module().phase_invalidation(snapshot, current)
        self.assertEqual(["documentation", "review"], [row["phase"] for row in rows])
        self.assertNotIn("validation", {row["phase"] for row in rows})
        self.assertNotIn("risk", {row["phase"] for row in rows})

    def test_unknown_dynamic_tampered_or_ambiguous_read_sets_fail_closed_with_bounded_attribution(self) -> None:
        module = self.module()
        for discovery in (
            {"complete": False, "reason": "dynamic_import"},
            {"complete": True, "kind": "ambiguous"},
        ):
            with self.subTest(discovery=discovery):
                with self.assertRaisesRegex(module.OperationSnapshotError, "read set discovery"):
                    self.compile(discovery=discovery)
        bad = self.spec()
        bad["read_sets"][0]["paths"].append("../escape")
        with self.assertRaisesRegex(module.OperationSnapshotError, "unsafe read-set path"):
            module.compile_operation_snapshot(**bad)
        snapshot = self.compile()
        snapshot["read_sets"][0]["inputs"]["runtime/validate.py"] = "0" * 64
        verification = module.verify_operation_snapshot(snapshot)
        self.assertFalse(verification["valid"])
        self.assertLessEqual(len(verification["reasons"]), module.MAX_ATTRIBUTION_ROWS)

    def test_task_and_merge_lifecycle_call_sites_consume_identity_snapshots(self) -> None:
        task_runtime = importlib.import_module("task_workspace")
        merge_runtime = importlib.import_module("merge_queue")
        kwargs = {
            "candidate": "a" * 40, "target": "b" * 40,
            "commands": [{"id": "focused", "cwd": ".", "argv": ["npm", "test"]}],
            "routing": {"focused": "focused"}, "environment": {"CI": "1"},
            "phase_units": [
                {"phase": "validation", "id": "focused", "inputs": {"closure": "v1"}},
                {"phase": "risk", "id": "policy", "inputs": {"policy": "v1"}},
                {"phase": "review", "id": "prompt", "inputs": {"prompt": "v1"}},
                {"phase": "documentation", "id": "active", "inputs": {"docs": "v1"}},
                {"phase": "integration", "id": "runtime", "inputs": {"runtime": "v1"}},
            ],
            "managed_outputs": {"queue": "v1"},
            "submission": self.submission(),
            "discovery": {"complete": True, "kind": "exact-import-closure"},
        }
        task_snapshot = task_runtime.compile_standing_operation_snapshot(**kwargs)
        merge_snapshot = merge_runtime.compile_merge_operation_snapshot(**kwargs)
        self.assertEqual(task_snapshot["snapshot_sha256"], merge_snapshot["snapshot_sha256"])
        self.assertTrue(self.module().verify_operation_snapshot(task_snapshot)["valid"])

    def test_mixed_benchmark_and_juno_code_routing_drops_only_planner_command_fields(self) -> None:
        module = self.module()
        kwargs = {
            "candidate": "a" * 40, "target": "b" * 40,
            "commands": [
                {"id": "benchmark-test", "cwd": "juno-benchmark",
                 "argv": ["npm", "test"], "timeout_seconds": 900,
                 "max_output_bytes": 32768,
                 "input_paths": ["juno-benchmark/package.json", "juno-benchmark/src"]},
                {"id": "juno-code-tests", "cwd": "juno-code",
                 "argv": ["npm", "test"], "timeout_seconds": 900,
                 "max_output_bytes": 32768},
            ],
            "routing": {"benchmark-test": "standing", "juno-code-tests": "standing"},
            "environment": {"CI": "1"},
            "phase_units": [
                {"phase": "validation", "id": "benchmark-test",
                 "inputs": {"input_closure_sha256": "1" * 64}},
                {"phase": "validation", "id": "juno-code-tests",
                 "inputs": {"input_closure_sha256": "2" * 64}},
                {"phase": "risk", "id": "policy", "inputs": {"policy": "3" * 64}},
                {"phase": "review", "id": "prompt", "inputs": {"prompt": "4" * 64}},
                {"phase": "documentation", "id": "active", "inputs": {"docs": "5" * 64}},
                {"phase": "integration", "id": "runtime", "inputs": {"runtime": "6" * 64}},
            ],
            "managed_outputs": {"task_workspace_runtime": "7" * 64},
            "submission": self.submission(),
            "discovery": {"complete": True, "kind": "exact-import-closure"},
        }
        snapshot = module.compile_identity_operation_snapshot(**kwargs)
        self.assertNotIn("input_paths", snapshot["commands"][0])
        changed = {**kwargs, "commands": [dict(row) for row in kwargs["commands"]]}
        changed["commands"][0]["argv"] = ["npm", "run", "test:full"]
        self.assertNotEqual(
            snapshot["snapshot_sha256"],
            module.compile_identity_operation_snapshot(**changed)["snapshot_sha256"],
        )
        malformed = {**kwargs, "commands": [dict(row) for row in kwargs["commands"]]}
        malformed["commands"][0]["unrecognized"] = True
        with self.assertRaisesRegex(module.OperationSnapshotError, "unknown fields"):
            module.compile_identity_operation_snapshot(**malformed)


if __name__ == "__main__":
    unittest.main()
