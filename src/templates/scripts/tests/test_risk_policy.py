#!/usr/bin/env python3
"""Adversarial real-Git tests for deterministic Bolt risk and reuse evidence."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "risk_policy.py"
POLICY = Path(__file__).resolve().parents[2] / "config/risk-policy.json"
SPEC = importlib.util.spec_from_file_location("risk_policy", SCRIPT)
assert SPEC and SPEC.loader
rp = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(rp)
GATE_SPEC = importlib.util.spec_from_file_location("release_gate", SCRIPT.with_name("release_gate.py"))
assert GATE_SPEC and GATE_SPEC.loader
release = importlib.util.module_from_spec(GATE_SPEC); GATE_SPEC.loader.exec_module(release)


class RiskPolicyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.policy = rp.load_policy(POLICY)

    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="juno-bolt-risk-test-"))
        self.repo = self.temp / "repo"
        subprocess.run(["git", "init", "-b", "main", str(self.repo)], check=True,
                       stdout=subprocess.DEVNULL)
        self.git("config", "user.name", "Test"); self.git("config", "user.email", "t@invalid")
        (self.repo / ".baseline").write_text("base\n")
        self.git("add", ".baseline"); self.git("commit", "-m", "base")
        self.base_sha = self.git("rev-parse", "HEAD")
        self.counter = 0
        self.make_candidate({"src/runtime.ts": "runtime\n"})

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def git(self, *args: str) -> str:
        return subprocess.check_output(["git", "-C", str(self.repo), *args], text=True).strip()

    def make_candidate(self, files: dict[str, str]) -> str:
        self.counter += 1
        self.git("switch", "-C", f"candidate-{self.counter}", self.base_sha)
        for name, content in files.items():
            path = self.repo / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(content)
        self.git("add", "-A"); self.git("commit", "-m", f"candidate {self.counter}")
        self.candidate_sha = self.git("rev-parse", "HEAD")
        return self.candidate_sha

    def request(self, *, candidate_sha: str | None = None,
                target_ref: str = "refs/heads/main",
                expected_target_sha: str | None = None) -> dict:
        return {"repository": str(self.repo), "candidate_sha": candidate_sha or self.candidate_sha,
                "target_ref": target_ref,
                "expected_target_sha": expected_target_sha or self.base_sha}

    def plan(self, files: dict[str, str], flags: list[str] | None = None) -> dict:
        self.make_candidate(files)
        return rp.classify(self.policy, self.request(), flags)

    def object_file(self, name: str, value: dict) -> tuple[str, str]:
        path = self.temp / name; data = rp.canonical(value); path.write_bytes(data)
        return str(path), hashlib.sha256(data).hexdigest()

    def review(self, plan: dict, role: str, sequence: int, session: str, *, findings: int = 0,
               predecessor: dict | None = None, tool_id: str | None = None,
               severity: str = "high", impact_category: str = "supported_runtime",
               scope: str = "candidate_bug",
               rejection_counters: dict | None = None) -> dict:
        predecessor_mark = None
        if predecessor:
            prior = json.loads(Path(predecessor["runner_receipt_path"]).read_text())
            predecessor_mark = {"receipt_sha256": predecessor["runner_receipt_sha256"],
                                "tool_id": prior["tool_id"], "session_id": prior["session_id"],
                                "completed_at": prior["completed_at"],
                                "binding_sha256": prior["review_binding"]["binding_sha256"]}
        binding = {"schema_version": rp.REVIEW_BINDING_SCHEMA,
                   "candidate_sha": plan["candidate"]["candidate_sha"],
                   "policy_identity": plan["policy_identity"], "reviewer_role": role,
                   "sequence": sequence, "predecessor": predecessor_mark}
        binding["binding_sha256"] = rp.digest(binding)
        result = {"schema_version": rp.REVIEW_RESULT_SCHEMA,
                  "candidate_sha": binding["candidate_sha"],
                  "policy_identity": binding["policy_identity"], "reviewer_role": role,
                  "sequence": sequence, "verdict": "findings" if findings else "pass",
                  "truncated": False, "omitted_finding_count": 0,
                  "rejection_counters": rejection_counters or {},
                  "findings": [{"code": f"F{i}", "severity": severity, "summary": "finding",
                                "paths": ["src/runtime.ts"], "symbols": ["run"],
                                "evidence": "frozen candidate evidence", "impact": "runtime broken",
                                "failure_condition": "supported invocation", "acceptance_condition": "works",
                                "impact_categories": [impact_category],
                                "scope_classification": scope,
                                "cited_contract": "PDR 2.2 reviewer-scope and anti-scope-creep gate"}
                               for i in range(findings)]}
        response_path, response_sha = self.object_file(f"response-{session}.json", result)
        prompt_path, prompt_sha = self.object_file(
            f"prompt-{session}.json", {"candidate": binding["candidate_sha"],
                                        "policy": binding["policy_identity"],
                                        "reviewer": role})
        receipt = {"schema_version": rp.MANAGED_RUNNER_SCHEMA, "mode": "reviewer",
                   "state": "succeeded", "semantic_outcome": "completed", "session_id": session,
                   "tool_id": tool_id or f"bolt_{role}",
                   "completed_at": f"2026-08-09T00:00:0{sequence}Z",
                   "identity": {"candidate_sha": binding["candidate_sha"]},
                   "review_binding": binding,
                   "artifacts": {
                       "prompt": {"path": prompt_path,
                                  "bytes": Path(prompt_path).stat().st_size,
                                  "sha256": prompt_sha},
                       "response": {"path": response_path,
                                    "bytes": Path(response_path).stat().st_size,
                                    "sha256": response_sha}}}
        path, mark = self.object_file(f"runner-{session}.json", receipt)
        return {"runner_receipt_path": path, "runner_receipt_sha256": mark}

    def high_reviews(self, plan: dict) -> list[dict]:
        first = self.review(plan, "reviewer_a", 1, "a")
        return [first, self.review(plan, "reviewer_b", 2, "b", predecessor=first)]

    def full_suite(self, plan: dict, name: str = "full-suite.json") -> dict | None:
        if not plan["full_suite_required"]:
            return None
        command = {"id": "full-suite", "cwd": ".", "argv": ["test", "all"],
                   "timeout_seconds": 60, "max_output_bytes": 4096}
        identity = {"task_workspace_config_sha256": "1" * 64,
                    "full_suite_config_sha256": "2" * 64,
                    "task_validation_commands_sha256": "3" * 64}
        receipt_path = (self.temp / name).resolve()
        claim_path = (self.temp / (name + ".claim")).resolve()
        token = "a" * 48
        claim = {"schema_version": rp.FULL_SUITE_CLAIM_SCHEMA,
                 "producer": {"schema_version": rp.FULL_SUITE_PRODUCER_SCHEMA,
                              "tool_id": rp.FULL_SUITE_TOOL_ID},
                 "task_id": "T1",
                 "candidate": {"candidate_sha": plan["candidate"]["candidate_sha"],
                               "candidate_tree": plan["candidate"]["candidate_tree"]},
                 "policy_identity": plan["policy_identity"],
                 "validation_identity": identity, "command": command,
                 "token": token, "attempt_number": 1,
                 "expected_receipt_path": str(receipt_path)}
        claim_path.write_bytes(rp.canonical(claim))
        claim_ref = {"claim_path": str(claim_path),
                     "claim_sha256": hashlib.sha256(claim_path.read_bytes()).hexdigest()}
        claim_binding = {**claim_ref, "token": token, "attempt_number": 1}
        receipt = {"schema_version": rp.FULL_SUITE_SCHEMA,
                   "producer": {"schema_version": rp.FULL_SUITE_PRODUCER_SCHEMA,
                                "tool_id": rp.FULL_SUITE_TOOL_ID},
                   "candidate": {"candidate_sha": plan["candidate"]["candidate_sha"],
                                 "candidate_tree": plan["candidate"]["candidate_tree"]},
                   "policy_identity": plan["policy_identity"],
                   "claim": claim_binding,
                   "validation_identity": identity, "command": command,
                   "started_at": "2026-08-09T00:00:00Z",
                   "completed_at": "2026-08-09T00:00:01Z",
                   "timing": {"schema_version": rp.VALIDATION_TIMING_SCHEMA,
                              "states": [
                                  {"state": "WAITING_FOR_RESOURCE", "duration_ms": 0},
                                  {"state": "SETUP", "duration_ms": 10},
                                  {"state": "RUNNING", "duration_ms": 980},
                                  {"state": "TEARDOWN", "duration_ms": 10},
                                  {"state": "PASSED", "duration_ms": 0}],
                              "wall_duration_ms": 1000,
                              "critical_path_contribution_ms": 1000},
                   "resource": {"id": None, "lock_identity_sha256": None,
                                "wait_timeout_seconds": None, "owner_diagnostics": None},
                   "identity": {"command_sha256": "4" * 64, "cwd_sha256": "5" * 64,
                                "policy_sha256": "6" * 64,
                                "candidate_sha": plan["candidate"]["candidate_sha"],
                                "candidate_tree": plan["candidate"]["candidate_tree"]},
                   "result": {"exit_code": 0, "timed_out": False,
                              "stdout": {"sha256": hashlib.sha256(b"ok").hexdigest(),
                                         "tail": "ok", "truncated_bytes": 0},
                              "stderr": {"sha256": hashlib.sha256(b"").hexdigest(),
                                         "tail": "", "truncated_bytes": 0}}}
        receipt_path.write_bytes(rp.canonical(receipt))
        receipt_ref = {"receipt_path": str(receipt_path),
                       "receipt_sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest()}
        return {"schema_version": rp.FULL_SUITE_ADMISSION_SCHEMA, "state": "COMPLETE",
                "attempt_number": 1, "token": token, "claim": claim_ref,
                "receipt": receipt_ref}

    def gate(self, plan: dict) -> dict:
        sha = plan["candidate"]["candidate_sha"]
        auth = {"schema_version": release.AUTH_SCHEMA, "candidate_sha": sha,
                "policy_identity": plan["policy_identity"], "owner_id": "owner",
                "authorized_scopes": ["local_release"]}
        auth_path, auth_sha = self.object_file("authorization.json", auth)
        receipt_path = self.temp / "release.json"
        release.produce(Path(auth_path), auth_sha, sha, plan["policy_identity"], receipt_path)
        return {"receipt_path": str(receipt_path),
                "receipt_sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
                "authorization_path": auth_path}

    def finish(self, plan: dict, reviews: list[dict] | None = None, **changes: object) -> dict:
        values = {"affected_tests_passed": True,
                  "full_suite_admission": self.full_suite(plan),
                  "reviews": reviews or [], "metrics": {}, "policy": self.policy}
        request = changes.pop("candidate_request", self.request(
            candidate_sha=plan["candidate"]["candidate_sha"],
            expected_target_sha=plan["candidate"]["target_sha"]))
        values.update(changes)
        return rp.finalize(plan, request, **values)

    def evidence_ref(self, evidence: dict, name: str = "evidence.json") -> dict:
        path, mark = self.object_file(name, evidence)
        return {"receipt_path": path, "receipt_sha256": mark}

    def test_git_derived_scope_cannot_claim_security_bytes_as_docs(self) -> None:
        plan = self.plan({"docs/readme.md": "docs\n", "src/security/auth.ts": "unsafe\n"})
        self.assertEqual("high", plan["tier"])
        self.assertEqual(["docs/readme.md", "src/security/auth.ts"], plan["changed_paths"])
        self.assertEqual(plan["changed_paths"], plan["candidate"]["changed_paths"])
        forged = copy.deepcopy(plan); forged["changed_paths"] = ["docs/readme.md"]
        forged["candidate"]["changed_paths"] = ["docs/readme.md"]
        forged["tier"] = "low"; forged["reasons"] = ["docs_only"]
        forged["reviewer_sequence"] = []; forged["min_reviews"] = forged["max_reviews"] = 0
        forged["full_suite_required"] = False
        with self.assertRaisesRegex(rp.RiskPolicyError, "drifted"):
            self.finish(forged)

    def test_security_rename_to_docs_is_still_high_risk(self) -> None:
        self.git("switch", "main")
        security = self.repo / "src/security/auth.ts"; security.parent.mkdir(parents=True)
        security.write_text("guard\n"); self.git("add", "src/security/auth.ts")
        self.git("commit", "-m", "security base"); self.base_sha = self.git("rev-parse", "HEAD")
        self.counter += 1; self.git("switch", "-c", f"rename-{self.counter}")
        (self.repo / "docs").mkdir(); self.git("mv", "src/security/auth.ts", "docs/auth.md")
        self.git("commit", "-m", "hide security as docs"); self.candidate_sha = self.git("rev-parse", "HEAD")
        plan = rp.classify(self.policy, self.request())
        self.assertEqual("high", plan["tier"])
        self.assertEqual(["docs/auth.md", "src/security/auth.ts"], plan["changed_paths"])

    def test_lifecycle_infrastructure_requires_two_sequential_reviewers(self) -> None:
        for path in (".juno_task/scripts/merge_queue.py",
                     "juno-code/src/templates/scripts/merge_queue.py"):
            plan = self.plan({path: "runtime\n"})
            self.assertEqual("high", plan["tier"])
            self.assertEqual(["reviewer_a", "reviewer_b"], plan["reviewer_sequence"])
            self.assertEqual((2, 2), (plan["min_reviews"], plan["max_reviews"]))

    def test_docs_normal_high_and_release_are_derived(self) -> None:
        docs = self.plan({"docs/flow.md": "docs\n"})
        self.assertEqual(("low", 0), (docs["tier"], docs["max_reviews"]))
        self.assertEqual("passed", self.finish(docs)["status"])
        normal = self.plan({"src/runtime.ts": "runtime\n"})
        self.assertEqual("passed", self.finish(normal)["status"])
        high = self.plan({"src/security/auth.ts": "auth\n"})
        self.assertEqual("passed", self.finish(high, self.high_reviews(high))["status"])
        released = self.plan({"src/security/auth.ts": "auth\n"}, ["release"])
        self.assertEqual(("high", 2, True, True),
                         (released["tier"], released["min_reviews"],
                          released["full_suite_required"], released["release_gate_required"]))
        released_evidence = self.finish(
            released, self.high_reviews(released), release_gate=self.gate(released))
        self.assertEqual("passed", released_evidence["status"])
        verified = rp.verify_candidate_evidence(
            self.policy,
            self.request(candidate_sha=released["candidate"]["candidate_sha"]),
            ["release"], self.evidence_ref(released_evidence, "released-evidence.json"))
        self.assertTrue(verified["eligible"])

    def test_target_ref_grammar_and_multi_commit_direct_tip(self) -> None:
        for ref in ("main", "refs/remotes/origin/main", "refs/heads/main~1",
                    "refs/heads/main@{0}", "refs/heads/../main", "refs/tags/main"):
            with self.assertRaises(rp.RiskPolicyError):
                rp.classify(self.policy, self.request(target_ref=ref))
        with self.assertRaisesRegex(rp.RiskPolicyError, "exact current"):
            rp.classify(self.policy, self.request(expected_target_sha=self.candidate_sha))
        self.git("switch", "-C", "stacked", self.candidate_sha)
        (self.repo / "second.ts").write_text("second\n"); self.git("add", "second.ts")
        self.git("commit", "-m", "stacked")
        stacked = self.git("rev-parse", "HEAD")
        plan = rp.classify(self.policy, self.request(candidate_sha=stacked))
        self.assertEqual("direct_descendant", plan["candidate"]["candidate_kind"])
        self.assertEqual(stacked, plan["candidate"]["source_feature_tip"])
        self.assertEqual(["second.ts", "src/runtime.ts"], plan["changed_paths"])
        self.assertEqual("passed", self.finish(
            plan, candidate_request=self.request(candidate_sha=stacked))["status"])

        tree = self.git("rev-parse", stacked + "^{tree}")
        unrelated_root = subprocess.check_output(
            ["git", "-C", str(self.repo), "-c", "user.name=T", "-c", "user.email=t@i",
             "commit-tree", tree, "-m", "unrelated root"], text=True).strip()
        unrelated = subprocess.check_output(
            ["git", "-C", str(self.repo), "-c", "user.name=T", "-c", "user.email=t@i",
             "commit-tree", tree, "-p", unrelated_root, "-m", "unrelated"], text=True).strip()
        with self.assertRaisesRegex(rp.RiskPolicyError, "not descended"):
            rp.classify(self.policy, self.request(candidate_sha=unrelated))

    def test_ordinary_two_parent_target_first_merge_is_bound(self) -> None:
        feature = self.candidate_sha
        tree = self.git("rev-parse", feature + "^{tree}")
        merge = subprocess.check_output(
            ["git", "-C", str(self.repo), "-c", "user.name=T", "-c", "user.email=t@i",
             "commit-tree", tree, "-p", self.base_sha, "-p", feature, "-m", "merge"],
            text=True).strip()
        plan = rp.classify(self.policy, self.request(candidate_sha=merge))
        self.assertEqual([self.base_sha, feature], plan["candidate"]["parents"])
        self.assertEqual(feature, plan["candidate"]["source_feature_tip"])
        self.assertEqual("target_first_merge", plan["candidate"]["candidate_kind"])

    def test_moved_target_two_parent_merge_remains_composition_candidate(self) -> None:
        feature = self.candidate_sha
        self.git("switch", "main"); (self.repo / "target-moved.ts").write_text("target\n")
        self.git("add", "target-moved.ts"); self.git("commit", "-m", "move target")
        moved_target = self.git("rev-parse", "HEAD")
        tree = self.git("rev-parse", feature + "^{tree}")
        merge = subprocess.check_output(
            ["git", "-C", str(self.repo), "-c", "user.name=T", "-c", "user.email=t@i",
             "commit-tree", tree, "-p", moved_target, "-p", feature, "-m", "moved merge"],
            text=True).strip()
        request = self.request(candidate_sha=merge, expected_target_sha=moved_target)
        plan = rp.classify(self.policy, request)
        self.assertEqual("target_first_merge", plan["candidate"]["candidate_kind"])
        self.assertEqual([moved_target, feature], plan["candidate"]["parents"])

    def test_feature_internal_merge_is_a_direct_descendant_candidate(self) -> None:
        first = self.candidate_sha
        self.git("switch", "-c", "feature-side", first)
        (self.repo / "side.ts").write_text("side\n"); self.git("add", "side.ts")
        self.git("commit", "-m", "side"); side = self.git("rev-parse", "HEAD")
        self.git("switch", "-c", "feature-main", first)
        (self.repo / "main.ts").write_text("main\n"); self.git("add", "main.ts")
        self.git("commit", "-m", "main"); feature_main = self.git("rev-parse", "HEAD")
        tree = self.git("rev-parse", feature_main + "^{tree}")
        internal_merge = subprocess.check_output(
            ["git", "-C", str(self.repo), "-c", "user.name=T", "-c", "user.email=t@i",
             "commit-tree", tree, "-p", feature_main, "-p", side, "-m", "internal merge"],
            text=True).strip()
        plan = rp.classify(self.policy, self.request(candidate_sha=internal_merge))
        self.assertEqual("direct_descendant", plan["candidate"]["candidate_kind"])
        self.assertEqual(internal_merge, plan["candidate"]["source_feature_tip"])

    def test_target_movement_invalidates_plan_before_validation(self) -> None:
        plan = self.plan({"docs/flow.md": "docs\n"})
        self.git("switch", "main"); (self.repo / "target").write_text("moved\n")
        self.git("add", "target"); self.git("commit", "-m", "move target")
        with self.assertRaisesRegex(rp.RiskPolicyError, "exact current"):
            self.finish(plan)

    def test_review_is_response_derived_and_a_findings_short_circuit(self) -> None:
        normal = self.plan({"src/runtime.ts": "runtime\n"})
        finding = self.review(normal, "reviewer", 1, "finding", findings=1)
        result = self.finish(normal, [finding])
        self.assertEqual(("failed", 1), (result["status"], result["reviews"][0]["finding_count"]))
        claimed = dict(finding, verdict="pass")
        with self.assertRaisesRegex(rp.RiskPolicyError, "strict schema"):
            self.finish(normal, [claimed])
        high = self.plan({"src/security/auth.ts": "auth\n"})
        first = self.review(high, "reviewer_a", 1, "a-find", findings=1)
        result = self.finish(high, [first, {"transcript": "never read"}])
        self.assertEqual(1, result["validation"]["review_dispatches"])

    def test_exhaustive_bound_and_severity_disposition_are_deterministic(self) -> None:
        normal = self.plan({"src/runtime.ts": "runtime\n"})
        advisory = self.review(normal, "reviewer", 1, "advisory", findings=1,
                               severity="medium", impact_category="bounded_product_defect")
        accepted = self.finish(normal, [advisory])
        compact = accepted["reviews"][0]
        self.assertEqual({"reviewer", "sequence", "verdict", "finding_count",
                          "advisory_count", "blocking_count", "findings",
                          "rejected_observation_count", "rejection_counters",
                          "review_reference"}, set(compact))
        self.assertEqual(rp.REVIEW_REFERENCE_SCHEMA,
                         compact["review_reference"]["schema_version"])
        self.assertEqual(("passed", 1, 0),
                         (accepted["status"], compact["advisory_count"], compact["blocking_count"]))
        promoted = self.review(normal, "reviewer", 1, "promoted", findings=1,
                               severity="low", impact_category="supported_install")
        blocked = self.finish(normal, [promoted])
        finding = blocked["reviews"][0]["findings"][0]
        self.assertEqual(("failed", "high", True),
                         (blocked["status"], finding["normalized_severity"], finding["blocking"]))
        exhaustive = self.review(normal, "reviewer", 1, "thirty-two", findings=32)
        bounded = self.finish(normal, [exhaustive])
        self.assertEqual(("failed", 32),
                         (bounded["status"], bounded["reviews"][0]["finding_count"]))

    def test_scope_admission_precedes_severity_dedup_and_advisories(self) -> None:
        normal = self.plan({"src/runtime.ts": "runtime\n"})
        for scope in ("requirement_gap", "candidate_bug",
                      "candidate_regression", "safety_invariant_violation"):
            advisory = self.review(normal, "reviewer", 1, f"scope-{scope}", findings=1,
                                   severity="medium", impact_category="bounded_product_defect",
                                   scope=scope,
                                   rejection_counters={"enhancement": 2, "design_preference": 1})
            accepted = self.finish(normal, [advisory])
            compact = accepted["reviews"][0]
            finding = compact["findings"][0]
            self.assertEqual(scope, finding["scope_classification"])
            self.assertEqual("PDR 2.2 reviewer-scope and anti-scope-creep gate",
                             finding["cited_contract"])
            self.assertEqual(("passed", 1, 0, 3),
                             (accepted["status"], compact["advisory_count"],
                             compact["blocking_count"],
                             compact["rejected_observation_count"]))
            self.assertEqual({"design_preference": 1, "enhancement": 2},
                             compact["rejection_counters"])

    def test_unscoped_or_malformed_findings_fail_closed(self) -> None:
        normal = self.plan({"src/runtime.ts": "runtime\n"})
        reference = self.review(normal, "reviewer", 1, "unscoped", findings=1)
        receipt_path = Path(reference["runner_receipt_path"])
        receipt = json.loads(receipt_path.read_text())
        response_path = Path(receipt["artifacts"]["response"]["path"])
        base = json.loads(response_path.read_text())
        variants = []
        missing_scope = json.loads(json.dumps(base))
        del missing_scope["findings"][0]["scope_classification"]
        variants.append(missing_scope)
        rejected_scope = json.loads(json.dumps(base))
        rejected_scope["findings"][0]["scope_classification"] = "enhancement"
        variants.append(rejected_scope)
        empty_contract = json.loads(json.dumps(base))
        empty_contract["findings"][0]["cited_contract"] = ""
        variants.append(empty_contract)
        legacy_v1 = json.loads(json.dumps(base))
        del legacy_v1["rejection_counters"]
        variants.append(legacy_v1)
        unknown_counter = json.loads(json.dumps(base))
        unknown_counter["rejection_counters"] = {"nice_to_have": 1}
        variants.append(unknown_counter)
        bool_counter = json.loads(json.dumps(base))
        bool_counter["rejection_counters"] = {"enhancement": True}
        variants.append(bool_counter)
        overflow_counter = json.loads(json.dumps(base))
        overflow_counter["rejection_counters"] = {"enhancement": 65}
        variants.append(overflow_counter)
        for variant in variants:
            response_path.write_bytes(rp.canonical(variant))
            receipt["artifacts"]["response"].update({
                "bytes": response_path.stat().st_size,
                "sha256": hashlib.sha256(response_path.read_bytes()).hexdigest()})
            receipt_path.write_bytes(rp.canonical(receipt))
            drifted = dict(reference,
                           runner_receipt_sha256=hashlib.sha256(receipt_path.read_bytes()).hexdigest())
            with self.assertRaisesRegex(rp.RiskPolicyError,
                                        "malformed or unbounded|admitted scope|schema/binding"):
                self.finish(normal, [drifted])

    def test_truncated_review_fails_closed_as_policy_evidence(self) -> None:
        plan = self.plan({"src/runtime.ts": "runtime\n"})
        reference = self.review(plan, "reviewer", 1, "truncated")
        receipt_path = Path(reference["runner_receipt_path"])
        receipt = json.loads(receipt_path.read_text())
        response_path = Path(receipt["artifacts"]["response"]["path"])
        response = json.loads(response_path.read_text())
        response.update({"verdict": "findings", "truncated": True,
                         "omitted_finding_count": 1,
                         "findings": [{"code": "F0", "severity": "medium", "summary": "bounded",
                                       "paths": ["src/runtime.ts"], "symbols": [],
                                       "evidence": "partial review", "impact": "bounded defect",
                                       "failure_condition": "edge", "acceptance_condition": "fix edge",
                                       "impact_categories": ["bounded_product_defect"],
                                       "scope_classification": "candidate_bug",
                                       "cited_contract": "PDR 2.1 bootstrap gate"}]})
        response_path.write_bytes(rp.canonical(response))
        receipt["artifacts"]["response"].update({
            "bytes": response_path.stat().st_size,
            "sha256": hashlib.sha256(response_path.read_bytes()).hexdigest()})
        receipt_path.write_bytes(rp.canonical(receipt))
        reference["runner_receipt_sha256"] = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(rp.RiskPolicyError, "truncated"):
            self.finish(plan, [reference])

    def test_reuse_requires_canonical_receipt_reference_and_reopens_artifacts(self) -> None:
        plan = self.plan({"src/security/auth.ts": "auth\n"})
        reviews = self.high_reviews(plan)
        prior = self.finish(plan, reviews)
        prior_ref = self.evidence_ref(prior)
        reused = self.finish(plan, previous=prior_ref, full_suite_admission=None,
                             reviews={"transcript": "ignored"})
        self.assertEqual("passed", reused["status"])
        with self.assertRaisesRegex(rp.RiskPolicyError, "exact canonical receipt reference"):
            self.finish(plan, previous=prior, full_suite_admission=None)
        fabricated = copy.deepcopy(prior); fabricated["reviews"][0]["finding_count"] = 99
        fabricated_ref = self.evidence_ref(fabricated, "fabricated.json")
        with self.assertRaises(rp.RiskPolicyError):
            self.finish(plan, previous=fabricated_ref, full_suite_admission=None)
        Path(reviews[0]["runner_receipt_path"]).unlink()
        with self.assertRaisesRegex(rp.RiskPolicyError, "missing|unreadable"):
            self.finish(plan, previous=prior_ref, full_suite_admission=None)

    def test_reuse_rejects_changed_response_and_supports_verified_chain(self) -> None:
        plan = self.plan({"src/security/auth.ts": "auth\n"})
        reviews = self.high_reviews(plan); prior = self.finish(plan, reviews)
        prior_ref = self.evidence_ref(prior)
        reused = self.finish(plan, previous=prior_ref, full_suite_admission=None)
        reused_ref = self.evidence_ref(reused, "reused.json")
        repeated = self.finish(plan, previous=reused_ref, full_suite_admission=None)
        self.assertEqual(reused["semantic_evidence_reused"]["origin_reviews"],
                         repeated["semantic_evidence_reused"]["origin_reviews"])
        receipt = json.loads(Path(reviews[0]["runner_receipt_path"]).read_text())
        prompt_path = Path(receipt["artifacts"]["prompt"]["path"])
        prompt_bytes = prompt_path.read_bytes()
        prompt_path.write_text("{}\n")
        with self.assertRaisesRegex(rp.RiskPolicyError, "request identity"):
            self.finish(plan, previous=prior_ref, full_suite_admission=None)
        prompt_path.write_bytes(prompt_bytes)
        Path(receipt["artifacts"]["response"]["path"]).write_text("{}\n")
        with self.assertRaisesRegex(rp.RiskPolicyError, "digest does not match"):
            self.finish(plan, previous=prior_ref, full_suite_admission=None)

    def test_previous_schema_producer_and_policy_are_strict(self) -> None:
        plan = self.plan({"src/security/auth.ts": "auth\n"})
        prior = self.finish(plan, self.high_reviews(plan))
        for mutation in ("unknown", "producer", "policy", "transcript"):
            bad = copy.deepcopy(prior)
            if mutation == "unknown": bad["unknown"] = True
            elif mutation == "producer": bad["producer"]["tool_id"] = "caller"
            elif mutation == "policy": bad["policy"]["policy_identity"] = "f" * 64
            else: bad["transcript"] = "forbidden"
            with self.assertRaisesRegex(rp.RiskPolicyError, "previous|policy"):
                self.finish(plan, previous=self.evidence_ref(bad, mutation + ".json"),
                            full_suite_admission=None)

    def test_merge_queue_callable_freshly_verifies_eligibility(self) -> None:
        plan = self.plan({"src/security/auth.ts": "auth\n"})
        request = self.request(candidate_sha=plan["candidate"]["candidate_sha"])
        evidence = self.finish(plan, self.high_reviews(plan))
        reference = self.evidence_ref(evidence, "queue-evidence.json")
        verified = rp.verify_candidate_evidence(self.policy, request, [], reference)
        self.assertTrue(verified["eligible"])
        self.assertEqual(plan, verified["plan"])

        forged = copy.deepcopy(evidence); forged["validation"]["review_dispatches"] = 0
        with self.assertRaisesRegex(rp.RiskPolicyError, "dispatch count"):
            rp.verify_candidate_evidence(
                self.policy, request, [], self.evidence_ref(forged, "queue-forged.json"))
        failed = self.finish(plan, affected_tests_passed=False, full_suite_admission=None)
        self.assertFalse(rp.verify_candidate_evidence(
            self.policy, request, [], self.evidence_ref(failed, "queue-failed.json"))["eligible"])

        boolean_only = copy.deepcopy(evidence)
        boolean_only["validation"]["full_suite"] = "passed"
        boolean_only["validation"]["full_suite_admission"] = None
        with self.assertRaisesRegex(rp.RiskPolicyError, "full-suite admission"):
            rp.verify_candidate_evidence(
                self.policy, request, [], self.evidence_ref(boolean_only, "boolean-only.json"))

    def test_semantic_reuse_decision_codes_phase_local_restart_and_metrics(self) -> None:
        def snapshot(**changes: object) -> dict:
            value = {
                "schema_version": "juno_semantic_input_snapshot.v1",
                "input_identity": {"src/runtime.ts": "1" * 64},
                "policy_identity": "2" * 64,
                "runtime_identity": "3" * 64,
                "authority_identity": "4" * 64,
                "review_prompt_identity": "5" * 64,
                "closure_unknown": False,
                "write_collision": False,
                "executed": {"commands": 3, "wall_ms": 1200},
            }
            value.update(changes)
            return value

        lineage = {"receipt_path": "/immutable/evidence.json",
                   "receipt_sha256": "6" * 64, "candidate_sha": "7" * 40,
                   "snapshot_sha256": rp.digest(snapshot())}
        previous = {"snapshot": snapshot(), "source_lineage": lineage}
        cases = [
            (None, snapshot(), "cold_miss", "tests", True),
            (previous, snapshot(), "hit", "none", False),
            (previous, snapshot(input_identity={"src/runtime.ts": "8" * 64}),
             "input_changed", "tests", True),
            (previous, snapshot(policy_identity="8" * 64), "policy_changed", "tests", True),
            (previous, snapshot(runtime_identity="8" * 64), "runtime_changed", "tests", True),
            (previous, snapshot(authority_identity="8" * 64), "authority_changed", "stop", False),
            (previous, snapshot(review_prompt_identity="8" * 64),
             "input_changed", "review", False),
            (previous, snapshot(write_collision=True), "write_collision", "stop", False),
            (previous, snapshot(closure_unknown=True), "closure_unknown", "tests", True),
            ({"snapshot": {"bad": True}, "source_lineage": lineage}, snapshot(),
             "malformed_evidence", "tests", True),
        ]
        for prior, current, code, phase, rerun_tests in cases:
            with self.subTest(code=code, phase=phase):
                decision = rp.semantic_reuse_decision(prior, current)
                self.assertEqual((code, phase, rerun_tests),
                                 (decision["code"], decision["restart_phase"],
                                  decision["rerun_tests"]))
        hit = rp.semantic_reuse_decision(previous, snapshot())
        self.assertEqual({"executed_commands": 0, "reused_commands": 3,
                          "avoided_commands": 3, "wall_ms_avoided": 1200,
                          "phase_reruns": 0, "whole_tree_fallbacks": 0},
                         hit["metrics"])

    def test_semantic_reuse_changed_input_detail_is_bounded_and_idempotent(self) -> None:
        def snap(inputs: dict[str, str]) -> dict:
            return {"schema_version": "juno_semantic_input_snapshot.v1",
                    "input_identity": inputs, "policy_identity": "2" * 64,
                    "runtime_identity": "3" * 64, "authority_identity": "4" * 64,
                    "review_prompt_identity": "5" * 64, "closure_unknown": False,
                    "write_collision": False,
                    "executed": {"commands": 2, "wall_ms": 50}}
        old = snap({f"src/{index}.ts": "1" * 64 for index in range(40)})
        current = snap({f"src/{index}.ts": "9" * 64 for index in range(40)})
        previous = {"snapshot": old,
                    "source_lineage": {"receipt_path": "/immutable/evidence.json",
                                       "receipt_sha256": "6" * 64,
                                       "candidate_sha": "7" * 40,
                                       "snapshot_sha256": rp.digest(old)}}
        first = rp.semantic_reuse_decision(previous, current)
        second = rp.semantic_reuse_decision(previous, current)
        self.assertEqual(first, second)
        self.assertEqual(16, len(first["changed_inputs"]))
        self.assertEqual(24, first["omitted_changed_input_count"])
        self.assertEqual(1, first["metrics"]["phase_reruns"])

    def test_validation_short_circuit_metrics_and_receipt_bound(self) -> None:
        plan = self.plan({"src/security/auth.ts": "auth\n"})
        result = self.finish(plan, affected_tests_passed=False, full_suite_admission=None,
                             reviews={"transcript": "never read"},
                             previous={"transcript": "never read"})
        self.assertEqual(0, result["validation"]["review_dispatches"])
        with self.assertRaisesRegex(rp.RiskPolicyError, "metrics"):
            self.finish(plan, metrics={"messages": 1})
        receipt = self.finish(plan, self.high_reviews(plan))
        output = self.temp / "bounded.json"; rp.atomic_receipt(output, receipt, self.policy)
        self.assertLess(output.stat().st_size, self.policy["limits"]["max_receipt_bytes"])
        self.assertNotIn("transcript", output.read_text())

    def test_full_suite_command_provenance_accepts_bounded_input_paths(self) -> None:
        plan = self.plan({"juno-benchmark/package.json": "{}\n"})
        command = {"id": "benchmark-test", "cwd": "juno-benchmark",
                   "argv": ["npm", "test"], "timeout_seconds": 900,
                   "max_output_bytes": 32768,
                   "input_paths": ["juno-benchmark/package.json",
                                   "juno-benchmark/src"]}
        rp._validate_command_row(command, plan)

    def test_full_suite_command_provenance_rejects_invalid_input_paths(self) -> None:
        plan = self.plan({"juno-benchmark/package.json": "{}\n"})
        base = {"id": "benchmark-test", "cwd": "juno-benchmark",
                "argv": ["npm", "test"], "timeout_seconds": 900,
                "max_output_bytes": 32768}
        invalid = ([], ["../package.json"], ["/tmp/package.json"],
                   ["juno-benchmark/./src"], ["juno-benchmark/src"] * 2,
                   [f"path-{index}" for index in range(65)])
        for input_paths in invalid:
            with self.subTest(input_paths=input_paths):
                with self.assertRaisesRegex(
                        rp.RiskPolicyError, "command row provenance is invalid"):
                    rp._validate_command_row({**base, "input_paths": input_paths}, plan)

    def test_lifecycle_infrastructure_requires_two_sequential_reviewers(self) -> None:
        for path in (".juno_task/scripts/merge_queue.py",
                     "juno-code/src/templates/scripts/merge_queue.py"):
            plan = self.plan({path: "runtime\n"})
            self.assertEqual("high", plan["tier"])
            self.assertEqual(["reviewer_a", "reviewer_b"], plan["reviewer_sequence"])
            self.assertEqual((2, 2), (plan["min_reviews"], plan["max_reviews"]))



if __name__ == "__main__":
    unittest.main()
