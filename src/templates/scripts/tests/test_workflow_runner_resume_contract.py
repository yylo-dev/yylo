#!/usr/bin/env python3
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "workflow_runner.sh"
ASSERT_SCRIPT = Path(__file__).resolve().parents[1] / "workflow_assert.py"
loader = importlib.machinery.SourceFileLoader("workflow_runner_contract", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
runner = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(runner)


class WorkflowRunnerResumeContractTests(unittest.TestCase):
    def run_workflow(self, workflow_path: Path, run_dir: Path, *extra: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["JUNO_CODE_SKIP_SCRIPT_STALE_CHECK"] = "1"
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--workflow",
                str(workflow_path),
                "--project-root",
                str(workflow_path.parent),
                "--out-dir",
                str(run_dir),
                "--print-output",
                "none",
                *extra,
            ],
            text=True,
            capture_output=True,
            env=env,
        )

    def make_workflow(self, root: Path) -> tuple[Path, Path, Path]:
        receipt = root / "receipt.json"
        marker = root / "mutation-count.txt"
        producer_code = (
            "import json,os,pathlib; "
            f"m=pathlib.Path({str(marker)!r}); m.write_text(m.read_text()+'x' if m.exists() else 'x'); "
            f"pathlib.Path({str(receipt)!r}).write_text(json.dumps({{'schema_version':'demo.v1',"
            "'producer_step_digest':os.environ['JUNO_WORKFLOW_STEP_DIGEST'],'outcome':'completed'}))"
        )
        workflow = {
            "schema_version": 1,
            "workflow_id": "resume_contract_fixture",
            "receipts": [
                {
                    "id": "producer_receipt",
                    "producer": "producer",
                    "path": str(receipt),
                    "schema_version": "demo.v1",
                    "required_fields": ["producer_step_digest", "outcome"],
                }
            ],
            "terminal_gate": "gate",
            "steps": [
                {"id": "producer", "command": [sys.executable, "-c", producer_code]},
                {
                    "id": "gate",
                    "requires_receipts": ["producer_receipt"],
                    "command": [sys.executable, "-c", "print('gate')"],
                },
            ],
        }
        workflow_path = root / "workflow.json"
        workflow_path.write_text(json.dumps(workflow, indent=2) + "\n", encoding="utf-8")
        return workflow_path, receipt, marker

    def test_resume_reuses_verified_predecessor_without_repeating_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow, _, marker = self.make_workflow(root)
            run_dir = root / "run"
            first = self.run_workflow(workflow, run_dir)
            self.assertEqual(first.returncode, 0, first.stderr)
            second = self.run_workflow(workflow, run_dir, "--from-step", "gate")
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(marker.read_text(), "x")
            manifest = json.loads((run_dir / "manifest.json").read_text())
            self.assertEqual(manifest["steps"][0]["status"], "reused_verified")
            self.assertEqual(manifest["terminal_gate"], "gate")
            self.assertEqual(manifest["semantic_status"], "completed")
            contract = json.loads((run_dir / "run_contract.json").read_text())
            self.assertEqual(len(contract["attempts"]), 2)
            self.assertTrue(all(Path(item["manifest"]).is_file() for item in contract["attempts"]))

    def test_hot_edit_rejects_resume_before_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow, _, marker = self.make_workflow(root)
            run_dir = root / "run"
            self.assertEqual(self.run_workflow(workflow, run_dir).returncode, 0)
            payload = json.loads(workflow.read_text())
            payload["name"] = "edited after run"
            workflow.write_text(json.dumps(payload, indent=2) + "\n")
            resumed = self.run_workflow(workflow, run_dir, "--from-step", "gate")
            self.assertNotEqual(resumed.returncode, 0)
            self.assertIn("resume_contract[workflow_source_sha256]", resumed.stderr)
            self.assertEqual(marker.read_text(), "x")

    def test_changed_receipt_rejects_resume_before_consumer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow, receipt, marker = self.make_workflow(root)
            run_dir = root / "run"
            self.assertEqual(self.run_workflow(workflow, run_dir).returncode, 0)
            payload = json.loads(receipt.read_text())
            payload["outcome"] = "tampered"
            receipt.write_text(json.dumps(payload))
            resumed = self.run_workflow(workflow, run_dir, "--from-step", "gate")
            self.assertNotEqual(resumed.returncode, 0)
            self.assertIn("artifact_sha256", resumed.stderr)
            self.assertEqual(marker.read_text(), "x")

    def test_typed_receipt_reports_named_schema_mismatch(self):
        contract = {
            "id": "integration",
            "schema_version": "expected.v1",
            "required_fields": ["root.after"],
        }
        with self.assertRaisesRegex(runner.WorkflowError, r"receipt\[integration\]\.schema_version"):
            runner.validate_receipt_payload(
                contract,
                {"schema_version": "wrong.v1", "producer_step_digest": "abc"},
                "abc",
                location="fixture",
            )

    def test_harness_only_amendment_is_a_fresh_linked_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow, receipt, marker = self.make_workflow(root)
            original_run = root / "original-run"
            self.assertEqual(self.run_workflow(workflow, original_run).returncode, 0)
            amendment = {
                "schema_version": 1,
                "workflow_id": "resume_contract_fixture",
                "amendment_mode": "harness_only_validation",
                "frozen_inputs": [{"id": "prior_receipt", "path": str(receipt)}],
                "terminal_gate": "validate_only",
                "steps": [{"id": "validate_only", "command": [sys.executable, "-c", "print('validated')"]}],
            }
            amendment_path = root / "amendment.json"
            amendment_path.write_text(json.dumps(amendment, indent=2) + "\n")
            amended_run = root / "amended-run"
            result = self.run_workflow(
                amendment_path, amended_run, "--amends-run", str(original_run)
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            contract = json.loads((amended_run / "run_contract.json").read_text())
            self.assertEqual(contract["amendment_of"]["mode"], "harness_only_validation")
            self.assertEqual(contract["amendment_of"]["workflow_id"], "resume_contract_fixture")
            self.assertEqual(marker.read_text(), "x")

    def test_local_integration_is_read_only_regardless_of_validation_owners(self):
        # The Bolt hard cut rejects executable local_integration execution before
        # contract validation; validation_ownership no longer admits it.
        workflow = {
            "schema_version": 2,
            "workflow_class": "local_integration",
            "risk_tier": "high",
            "terminal_gate": "gate",
            "validation_ownership": {
                "pre_merge_review": "pre_merge_review",
                "candidate_review": "candidate_review",
                "actual_target_review": "gate",
            },
            "steps": [{"id": "gate", "command": "true"}],
        }
        with self.assertRaisesRegex(runner.WorkflowError, "legacy local_integration execution is read-only"):
            runner.validate_workflow(workflow)

    def test_local_integration_hard_cut_precedes_schema_migration(self):
        for schema in (None, 1, "v1", 2):
            workflow = {
                "workflow_class": "local_integration",
                "terminal_gate": "gate",
                "steps": [{"id": "gate", "command": "true"}],
            }
            if schema is not None:
                workflow["schema_version"] = schema
            with self.subTest(schema=schema), self.assertRaisesRegex(
                runner.WorkflowError, "legacy local_integration execution is read-only"
            ):
                runner.validate_workflow(workflow)

    def test_receipt_contract_requires_explicit_producer_step_digest(self):
        workflow = {
            "schema_version": 1,
            "receipts": [{
                "id": "evidence",
                "producer": "producer",
                "path": "evidence.json",
                "schema_version": "evidence.v1",
                "required_fields": ["step_digest", "outcome"],
            }],
            "steps": [{"id": "producer", "command": "true"}],
        }
        with self.assertRaisesRegex(runner.WorkflowError, "must include producer_step_digest"):
            runner.validate_workflow(workflow)

    def test_local_integration_contract_rejection_precedes_retired_helpers(self):
        # A fully declared v2/v3 integration contract is still rejected by the
        # hard cut before any step, receipt, or retired helper command is
        # validated or executed.
        workflow = {
            "schema_version": 2,
            "workflow_class": "local_integration",
            "risk_tier": "high",
            "integration_step": "integrate",
            "terminal_gate": "integrate",
            "integration_policy": {
                "queue": "automatic_after_review_pass",
                "channel_scope": "git_common_dir_and_target_ref",
                "target_movement": "rebuild_and_rereview",
                "checked_out_target": "detach_same_sha",
            },
            "validation_ownership": {
                "pre_merge_review": "pre_merge_review",
                "candidate_review": "candidate_review",
                "actual_target_review": "integrate",
            },
            "receipts": [
                {
                    "id": "integration",
                    "producer": "integrate",
                    "path": "integration.json",
                    "schema_version": "juno_local_integration.v3",
                    "required_fields": ["producer_step_digest", "outcome", "feature_tag_policy"],
                    "expected_fields": {"outcome": "integrated"},
                }
            ],
            "steps": [
                {"id": "pre_merge_review", "command": "true"},
                {"id": "candidate_review", "command": "true"},
                {
                    "id": "integrate",
                    "command": "python3 .juno_task/scripts/integration_owner_preflight.py integrate",
                },
            ],
        }
        with self.assertRaisesRegex(runner.WorkflowError, "legacy local_integration execution is read-only"):
            runner.validate_workflow(workflow)

    def test_receipt_expected_fields_bind_semantic_values(self):
        contract = {
            "id": "integration",
            "schema_version": "integration.v1",
            "required_fields": ["controller_disposition"],
            "expected_fields": {"controller_disposition": "target_integrated_controller_attached_clean"},
        }
        with self.assertRaisesRegex(runner.WorkflowError, r"expected_field\[controller_disposition\]"):
            runner.validate_receipt_payload(
                contract,
                {
                    "schema_version": "integration.v1",
                    "producer_step_digest": "abc",
                    "controller_disposition": "integration_failed_preserved",
                },
                "abc",
                location="fixture",
            )

    def test_named_assertion_emits_expected_and_actual(self):
        result = subprocess.run(
            [
                sys.executable,
                str(ASSERT_SCRIPT),
                "equal",
                "--name",
                "root.gitlink",
                "--expected",
                "abc",
                "--actual",
                "def",
            ],
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["name"], "root.gitlink")
        self.assertEqual(payload["expected"], "abc")
        self.assertEqual(payload["actual"], "def")

    def test_hydration_yaml_without_pyyaml_preserves_argv_and_outputs(self):
        workflow_yaml = """schema_version: v1
workflow_id: hydration_fallback
workflow_class: task_hydration
hydration_selection: admitted_scope_v1
steps:
  - id: node_dependencies
    dependency_root: juno-code
    name: Hydrate exact Node lock
    command: ["python3", "helper.py", "space in argument", "$(never execute)"]
    probe:
      - python3
      - helper.py
    timeout_seconds: 900
    fail_workflow: true
    non_interactive: true
    network: true
    sensitive: false
    outputs: ["juno-code/node_modules"]
"""
        with mock.patch.object(runner, "_managed_yaml_module", return_value=(None, "fixture")):
            workflow = runner.parse_yaml_like(workflow_yaml)
            runner.validate_workflow(workflow)
            step = workflow["steps"][0]
            self.assertEqual(step["command"], ["python3", "helper.py", "space in argument", "$(never execute)"])
            self.assertEqual(step["probe"], ["python3", "helper.py"])
            self.assertEqual(step["outputs"], ["juno-code/node_modules"])
            self.assertEqual(runner.parse_scalar("[]"), [])
            with self.assertRaisesRegex(runner.WorkflowError, "JSON syntax"):
                runner.parse_scalar("[unsupported, YAML]")

    def test_advanced_yaml_without_pyyaml_fails_with_dependency_guidance(self):
        real_import = __import__

        def import_without_yaml(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("fixture blocks PyYAML")
            return real_import(name, *args, **kwargs)

        workflow_yaml = """\
schema_version: 1
workflow_id: advanced_yaml
receipts:
  - id: evidence
    producer: producer
    path: receipt.json
    schema_version: receipt.v1
steps:
  - id: producer
    command: true
"""
        with mock.patch("builtins.__import__", side_effect=import_without_yaml):
            with self.assertRaisesRegex(runner.WorkflowError, "require Python PyYAML or JSON input"):
                runner.parse_yaml_like(workflow_yaml)


if __name__ == "__main__":
    unittest.main()
