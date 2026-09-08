#!/usr/bin/env python3
"""Independent EVYb6o lifecycle evidence reuse/recovery dogfood matrix."""
from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import operation_snapshot as snapshots
import risk_policy


class LifecycleEvidenceReuseMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.matrix = json.loads((Path(__file__).parent / "fixtures" /
                                  "lifecycle-evidence-reuse-matrix.v1.json").read_text())
        files = {
            "juno-benchmark/src/evaluate.ts": "benchmark source v1\n",
            "juno-benchmark/config/evaluation.json": '{"mode":"isolated"}\n',
            "juno-benchmark/package-lock.json": '{"lockfileVersion":3}\n',
            "juno-code/test/benchmark-fixture.ts": "fixture v1\n",
            "policy/risk.json": '{"tier":"normal"}\n',
            "runtime/merge.py": "runtime v1\n",
            "prompts/reviewer.md": "review prompt v1\n",
            "juno-code/README.md": "active docs v1\n",
            "integration/write-cas.py": "expected-old v1\n",
            "README.md": "unrelated product readme v1\n",
            ".pi/skills/generated/SKILL.md": "unrelated generated skill v1\n",
            ".juno_task/tasks/unrelated.md": "unrelated task v1\n",
            ".juno_task/specs/unrelated-pdr.md": "unrelated pdr v1\n",
            ".juno_task/wiki/unrelated.md": "unrelated wiki v1\n",
        }
        for relative, body in files.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)

    def compile(self, **overrides: object) -> dict:
        values = {
            "root": self.root, "candidate": "a" * 40, "target": "b" * 40,
            "commands": [
                {"id": "benchmark-source", "cwd": "juno-benchmark", "argv": ["npm", "test"]},
                {"id": "benchmark-config-lock", "cwd": "juno-benchmark", "argv": ["npm", "run", "typecheck"]},
                {"id": "juno-code-fixture", "cwd": "juno-code", "argv": ["npm", "test", "--", "fixture"]},
            ],
            "routing": {"benchmark-source": "benchmark", "benchmark-config-lock": "benchmark",
                        "juno-code-fixture": "juno-code"},
            "environment": {"CI": "1", "TZ": "UTC"},
            "read_sets": [
                {"phase": "validation", "id": "benchmark-source", "paths": ["juno-benchmark/src/evaluate.ts"]},
                {"phase": "validation", "id": "benchmark-config-lock", "paths": ["juno-benchmark/config/evaluation.json", "juno-benchmark/package-lock.json"]},
                {"phase": "validation", "id": "juno-code-fixture", "paths": ["juno-code/test/benchmark-fixture.ts"]},
                {"phase": "risk", "id": "policy", "paths": ["policy/risk.json"]},
                {"phase": "review", "id": "reviewer", "paths": ["prompts/reviewer.md"]},
                {"phase": "documentation", "id": "active-docs", "paths": ["juno-code/README.md"]},
                {"phase": "integration", "id": "managed-write", "paths": ["integration/write-cas.py", "runtime/merge.py"]},
            ],
            "managed_outputs": {"integration/write-cas.py": "expected-old-v1"},
            "submission": {
                "task_id": "matrix", "requirements_sha256": "1" * 64,
                "admitted_scope_sha256": "2" * 64, "generated_scope_sha256": "3" * 64,
                "base_sha": "4" * 40, "tip_sha": "5" * 40, "tree_sha": "6" * 40,
                "origin_projection_sha256": "7" * 64, "hydration_sha256": "8" * 64,
                "dependency_sha256": "9" * 64, "runtime_sha256": "a" * 64,
                "validation_sha256": "b" * 64, "risk_sha256": "c" * 64,
            },
            "discovery": {"complete": True, "kind": "exact-import-closure"},
        }
        values.update(overrides)
        return snapshots.compile_operation_snapshot(**values)

    def test_evyb6o_topology_is_phase_local_during_concurrent_controller_movement(self) -> None:
        baseline = self.compile()
        unrelated = ["README.md", ".pi/skills/generated/SKILL.md",
                     ".juno_task/tasks/unrelated.md", ".juno_task/specs/unrelated-pdr.md",
                     ".juno_task/wiki/unrelated.md"]

        def move_unrelated() -> None:
            for index in range(25):
                for relative in unrelated:
                    (self.root / relative).write_text(f"concurrent metadata {index}\n")

        with ThreadPoolExecutor(max_workers=2) as pool:
            movement = pool.submit(move_unrelated)
            observed = [self.compile(observed_controller_head=str(index % 10) * 40,
                                     controller_status=[f" M {path}" for path in unrelated])
                        for index in range(25)]
            movement.result()
        self.assertTrue(all(item["snapshot_sha256"] == baseline["snapshot_sha256"]
                            for item in observed))
        self.assertEqual([], snapshots.phase_invalidation(baseline, self.compile()))

        cases = {
            "juno-benchmark/src/evaluate.ts": ("validation", "benchmark-source"),
            "juno-benchmark/config/evaluation.json": ("validation", "benchmark-config-lock"),
            "juno-benchmark/package-lock.json": ("validation", "benchmark-config-lock"),
            "juno-code/test/benchmark-fixture.ts": ("validation", "juno-code-fixture"),
            "policy/risk.json": ("risk", "policy"),
            "prompts/reviewer.md": ("review", "reviewer"),
        }
        for relative, expected in cases.items():
            with self.subTest(relative=relative):
                path = self.root / relative
                original = path.read_text(); path.write_text(original + "drift\n")
                rows = snapshots.phase_invalidation(baseline, self.compile())
                self.assertEqual([expected], [(row["phase"], row["unit_id"]) for row in rows])
                path.write_text(original)

        docs = self.root / "juno-code/README.md"
        original = docs.read_text(); docs.write_text("reconciled active docs v2\n")
        self.assertEqual(["documentation", "review"],
                         [row["phase"] for row in snapshots.phase_invalidation(baseline, self.compile())])
        docs.write_text(original)
        refreshed = self.compile(target="c" * 40)
        self.assertEqual("target_drift",
                         snapshots.phase_invalidation(baseline, refreshed)[0]["reason"])
        managed = self.compile(managed_outputs={"integration/write-cas.py": "unexpected-old"})
        self.assertEqual(["integration"],
                         [row["phase"] for row in snapshots.phase_invalidation(baseline, managed)])

    def semantic_snapshot(self, **changes: object) -> dict:
        baseline = self.matrix["historical_baseline"]
        value = {
            "schema_version": "juno_semantic_input_snapshot.v1",
            "input_identity": {"benchmark/config": "1" * 64},
            "policy_identity": "2" * 64, "runtime_identity": "3" * 64,
            "authority_identity": "4" * 64, "review_prompt_identity": "5" * 64,
            "closure_unknown": False, "write_collision": False,
            "executed": {"commands": baseline["executed_commands"], "wall_ms": baseline["wall_ms"]},
        }
        value.update(changes)
        return value

    def test_typed_reuse_recovery_matrix_has_lineage_zero_false_reuse_and_stable_metrics(self) -> None:
        snapshot = self.semantic_snapshot()
        lineage = {"receipt_path": "/immutable/task-standing.json", "receipt_sha256": "6" * 64,
                   "candidate_sha": "7" * 40, "snapshot_sha256": risk_policy.digest(snapshot)}
        previous = {"snapshot": snapshot, "source_lineage": lineage}
        decisions = []
        for case in self.matrix["semantic_cases"]:
            current, prior = self.semantic_snapshot(), previous
            mutation = case["mutation"]
            if mutation == "absent": prior = None
            elif mutation == "input": current["input_identity"] = {"benchmark/config": "8" * 64}
            elif mutation == "prompt": current["review_prompt_identity"] = "8" * 64
            elif mutation == "policy": current["policy_identity"] = "8" * 64
            elif mutation == "runtime": current["runtime_identity"] = "8" * 64
            elif mutation == "authority": current["authority_identity"] = "8" * 64
            elif mutation == "collision": current["write_collision"] = True
            elif mutation == "unknown": current["closure_unknown"] = True
            elif mutation == "malformed": prior = {"snapshot": {"bad": True}, "source_lineage": lineage}
            elif mutation == "tampered":
                prior = copy.deepcopy(previous); prior["source_lineage"]["snapshot_sha256"] = "9" * 64
            first = risk_policy.semantic_reuse_decision(prior, current)
            self.assertEqual(first, risk_policy.semantic_reuse_decision(prior, current), case["id"])
            self.assertEqual(case["expected"], first["code"], case["id"])
            if first["code"] == "hit":
                self.assertEqual(lineage, first["source_lineage"])
            else:
                self.assertNotEqual("reused", first["disposition"])
            if first["stop"]:
                self.assertEqual(0, first["metrics"]["executed_commands"])
            decisions.append(first)

        self.assertEqual(self.matrix["expected_decision_distribution"],
                         dict(sorted(Counter(row["code"] for row in decisions).items())))
        aggregate = {
            "avoided_commands": sum(row["metrics"]["avoided_commands"] for row in decisions),
            "baseline_normalized_wall_ms_avoided": sum(row["metrics"]["wall_ms_avoided"] for row in decisions),
            "false_reuse": sum(row["disposition"] == "reused" and row["code"] != "hit" for row in decisions),
            "phase_reruns": sum(row["metrics"]["phase_reruns"] for row in decisions),
            "reuse_eligible_cases": sum(case["expected"] == "hit" for case in self.matrix["semantic_cases"]),
            "reused_cases": sum(row["code"] == "hit" for row in decisions),
            "whole_tree_fallbacks": sum(row["metrics"]["whole_tree_fallbacks"] for row in decisions),
        }
        self.assertEqual(self.matrix["expected_aggregate"], aggregate)

    def test_runtime_template_fixtures_and_managed_inventory_are_identical(self) -> None:
        repository = next(parent for parent in Path(__file__).resolve().parents
                          if (parent / "juno-code").is_dir() and (parent / ".juno_task").is_dir())
        pairs = [
            ("scripts/tests/test_lifecycle_evidence_reuse_matrix.py",
             ".juno_task/scripts/tests/test_lifecycle_evidence_reuse_matrix.py"),
            ("scripts/tests/fixtures/lifecycle-evidence-reuse-matrix.v1.json",
             ".juno_task/scripts/tests/fixtures/lifecycle-evidence-reuse-matrix.v1.json"),
        ]
        declaration = json.loads((repository / "juno-code/src/templates/managed-assets.json").read_text())
        declared = {(row["source"], row["destination"])
                    for row in declaration["assets"]}
        inventory = json.loads((repository / ".juno_task/managed-assets.json").read_text())
        for source, destination in pairs:
            with self.subTest(destination=destination):
                template = repository / "juno-code/src/templates" / source
                runtime = repository / destination
                self.assertEqual(template.read_bytes(), runtime.read_bytes())
                self.assertIn((source, destination), declared)
                digest = hashlib.sha256(template.read_bytes()).hexdigest()
                self.assertEqual(digest, inventory["assets"][destination]["sourceSha256"])
                self.assertEqual(digest, inventory["assets"][destination]["installedSha256"])

    def test_snapshot_receipt_is_immutable_tamper_evident_and_resume_safe(self) -> None:
        snapshot = self.compile()
        receipt = self.root / "receipts" / f'{snapshot["snapshot_sha256"]}.json'
        snapshots.write_operation_snapshot(receipt, snapshot)
        before = receipt.read_bytes(); before_sha = hashlib.sha256(before).hexdigest()
        with self.assertRaisesRegex(snapshots.OperationSnapshotError, "already exists"):
            snapshots.write_operation_snapshot(receipt, snapshot)
        self.assertEqual((before, before_sha),
                         (receipt.read_bytes(), hashlib.sha256(receipt.read_bytes()).hexdigest()))
        tampered = copy.deepcopy(snapshot)
        tampered["read_sets"][0]["inputs"]["juno-benchmark/src/evaluate.ts"] = "0" * 64
        self.assertFalse(snapshots.verify_operation_snapshot(tampered)["valid"])
        self.assertEqual("snapshot_invalid",
                         snapshots.phase_invalidation(tampered, snapshot)[0]["reason"])


if __name__ == "__main__":
    unittest.main()
