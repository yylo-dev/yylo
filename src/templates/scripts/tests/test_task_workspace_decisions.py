#!/usr/bin/env python3
"""Pure table/property tests for task-workspace lifecycle decisions.

Wave 3 pilot of 7djT8N: the decision core in ``task_workspace_decisions.py``
is proven here without Git, filesystem mutation, subprocesses, locks, clocks,
environment, or network. Purity is enforced three ways:

1. an AST import audit against a strict stdlib allowlist;
2. every planner call in the pure classes runs with ``open`` and process/
   socket creation poisoned;
3. the whole module is bounded to the Wave 3 budget (2 seconds).

Characterization parity: refusal messages are pinned to the exact strings the
real-Git scenario suite (``test_task_workspace.py``) asserts end to end, so
semantic drift fails both suites. The wiring class additionally proves the
imperative shell routes its gates through the pure planners.
"""
from __future__ import annotations

import ast
import builtins
import contextlib
import io
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import task_workspace_decisions as decisions  # noqa: E402

MODULE_STARTED = time.monotonic()
PURE_BUDGET_SECONDS = 2.0

# Commands that address task lifecycle state.
COMMANDS = (
    "start", "hydrate", "status", "checkpoint", "evidence-run",
    "evidence-status", "evidence-await", "preflight", "finish",
    "child-checkpoint",
)

# Every durable lifecycle state the shell records, plus the missing record.
STATES = (
    None, "NOT_STARTED", "WORKING", "QUEUED", "HYDRATING", "HYDRATION_FAILED",
    "REVIEW_FINDINGS", "REVIEW_FINDINGS_EXHAUSTED", "AWAITING_RISK",
    "REVIEWING", "CONFLICT", "CONFLICT_RESOLVED",
    "REOPENING", "REQUEUING_STALE", "RISK_EVIDENCE_READY", "MERGING",
    "MERGED", "KANBAN_SYNC_REQUIRED", "WITHDRAWN",
)

ALLOWED_IMPORTS = {"__future__", "dataclasses", "typing"}
FORBIDDEN_IDENTIFIERS = {"open", "eval", "exec", "compile", "__import__",
                         "globals", "locals", "breakpoint"}


@contextlib.contextmanager
def poisoned_surface():
    """Fail any filesystem, process, or socket touch inside the block."""
    def refused(*args, **kwargs):
        raise AssertionError(
            "pure decision planner touched a poisoned surface "
            f"({args[0] if args else kwargs})")
    with mock.patch.object(builtins, "open", refused), \
            mock.patch("subprocess.Popen", refused), \
            mock.patch("subprocess.run", refused), \
            mock.patch("socket.socket", refused), \
            mock.patch.object(time, "time", refused), \
            mock.patch.object(time, "monotonic", refused):
        yield


class PurityContract(unittest.TestCase):
    """The decision core must stay import-clean and side-effect free."""

    def test_import_graph_is_strictly_pure(self) -> None:
        source = Path(decisions.__file__).read_text()
        tree = ast.parse(source)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertTrue(imported, "decision core must declare its imports")
        self.assertEqual(
            imported - ALLOWED_IMPORTS, set(),
            f"impure imports leaked into the decision core: "
            f"{sorted(imported - ALLOWED_IMPORTS)}")

    def test_no_dynamic_or_io_identifiers(self) -> None:
        tree = ast.parse(Path(decisions.__file__).read_text())
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        names |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        leaked = names & FORBIDDEN_IDENTIFIERS
        self.assertEqual(leaked, set(),
                         f"forbidden identifiers in decision core: {sorted(leaked)}")

    def test_snapshot_and_decision_types_are_immutable(self) -> None:
        frozen = (decisions.CommandRequest, decisions.TaskSnapshot,
                  decisions.Finding, decisions.TransitionDecision,
                  decisions.StatusProjection, decisions.ReceiptFact,
                  decisions.EvidenceCommandPlan, decisions.EvidenceReusePlan)
        for cls in frozen:
            self.assertTrue(hasattr(cls, "__dataclass_fields__"), cls.__name__)
            self.assertTrue(cls.__dataclass_params__.frozen,
                            f"{cls.__name__} must be a frozen dataclass")

    def test_module_completes_inside_the_pure_budget(self) -> None:
        self.assertLess(time.monotonic() - MODULE_STARTED, PURE_BUDGET_SECONDS,
                        "pure decision tables exceeded the Wave 3 budget")


class TransitionMatrixTables(unittest.TestCase):
    """State x command admission tables with characterization-pinned messages."""

    def plan(self, command, state, owner=None, task_id="X"):
        with poisoned_surface():
            return decisions.plan_command_transition(
                decisions.CommandRequest(command, task_id),
                decisions.TaskSnapshot(task_id, state, owner))

    def test_working_admits_every_stateful_command(self) -> None:
        for command in COMMANDS:
            decision = self.plan(command, "WORKING")
            self.assertTrue(decision.admitted, command)
            self.assertIsNone(decision.finding)
            self.assertEqual(decision.phase, "working")

    def test_missing_record_refusals_are_exact(self) -> None:
        cases = {
            "checkpoint": "standing checkpoint requires a WORKING task",
            "evidence-run": "standing evidence run requires a WORKING task",
            "preflight": "task has not been started",
            "finish": "task has not been started",
            "child-checkpoint": "umbrella child checkpoint requires a WORKING umbrella",
        }
        for command, message in cases.items():
            decision = self.plan(command, None)
            self.assertFalse(decision.admitted, command)
            self.assertEqual(decision.finding.message, message, command)

    def test_wrong_state_refusals_name_the_state(self) -> None:
        for state in ("MERGED", "KANBAN_SYNC_REQUIRED", "HYDRATION_FAILED"):
            self.assertEqual(
                self.plan("preflight", state).finding.message,
                f"task cannot preflight from {state}")
            self.assertEqual(
                self.plan("finish", state).finding.message,
                f"task cannot finish from {state}")
            self.assertEqual(
                self.plan("checkpoint", state).finding.message,
                "standing checkpoint requires a WORKING task")
            self.assertEqual(
                self.plan("evidence-run", state).finding.message,
                "standing evidence run requires a WORKING task")

    def test_reporting_only_tasks_redirect_to_their_ordinary_delivery(self) -> None:
        owner = "UMB"
        self.assertEqual(
            self.plan("checkpoint", None, owner).finding.message,
            f"task X is reporting-only under delivery owner {owner}; "
            f"checkpoint the ordinary delivery instead: yy task checkpoint {owner}")
        self.assertEqual(
            self.plan("preflight", None, owner).finding.message,
            f"task X is reporting-only under delivery owner {owner}; "
            f"preflight the delivery instead: yy task preflight {owner}")
        self.assertEqual(
            self.plan("finish", None, owner).finding.message,
            f"task X is reporting-only under delivery owner {owner}; "
            f"finish the delivery instead: yy task finish {owner}")
        self.assertEqual(
            self.plan("start", None, owner).finding.message,
            f"task X is reporting-only under delivery owner {owner}")

    def test_hydrate_admits_exactly_the_frozen_hydration_states(self) -> None:
        for state in STATES:
            decision = self.plan("hydrate", state)
            hydrated = state in decisions.HYDRATABLE_STATES
            self.assertEqual(decision.admitted, hydrated, state)
            if not hydrated:
                self.assertEqual(
                    decision.finding.message,
                    f"task cannot hydrate from {state if state is not None else 'missing'}")

    def test_queued_finish_is_idempotent_reverification(self) -> None:
        decision = self.plan("finish", "QUEUED")
        self.assertTrue(decision.admitted)
        self.assertTrue(decision.idempotent)
        self.assertEqual(decision.phase, "queued")
        self.assertFalse(self.plan("finish", "WORKING").idempotent)

    def test_status_and_evidence_reads_never_refuse(self) -> None:
        for command in ("status", "evidence-status", "evidence-await"):
            for state in STATES:
                decision = self.plan(command, state)
                self.assertTrue(decision.admitted, (command, state))
                self.assertIsNone(decision.finding, (command, state))

    def test_unknown_command_is_a_programming_error(self) -> None:
        with poisoned_surface():
            self.assertRaises(
                ValueError, decisions.plan_command_transition,
                decisions.CommandRequest("rebase", "X"), decisions.TaskSnapshot("X"))

    def test_matrix_totality_properties(self) -> None:
        for command in COMMANDS:
            for state in STATES:
                for owner in (None, "UMB"):
                    decision = self.plan(command, state, owner)
                    self.assertEqual(decision.command, command)
                    self.assertEqual(decision.task_id, "X")
                    self.assertEqual(decision.admitted, decision.finding is None)
                    if decision.finding is not None:
                        self.assertTrue(decision.finding.message)
                        self.assertTrue(decision.finding.code)
                    self.assertIsInstance(decision.phase, str)
                    self.assertTrue(decision.phase)

    def test_start_idempotence_flag_marks_existing_records(self) -> None:
        self.assertFalse(self.plan("start", None).idempotent)
        for state in ("WORKING", "QUEUED", "MERGED"):
            self.assertTrue(self.plan("start", state).idempotent, state)


class StatusProjectionTables(unittest.TestCase):
    def test_reporting_only_projection_redirects_to_delivery_status(self) -> None:
        with poisoned_surface():
            projection = decisions.status_projection(
                decisions.TaskSnapshot("X", None, "UMB"))
        self.assertEqual(projection.state, "TRACKING_ONLY")
        self.assertEqual(projection.umbrella_owner_task_id, "UMB")
        self.assertEqual(
            projection.next_action,
            "report through the ordinary delivery owner; "
            "inspect progress with: yy task status UMB")

    def test_absent_task_projects_not_started(self) -> None:
        with poisoned_surface():
            projection = decisions.status_projection(decisions.TaskSnapshot("X"))
        self.assertEqual(projection.state, "NOT_STARTED")
        self.assertIsNone(projection.umbrella_owner_task_id)
        self.assertIsNone(projection.next_action)

    def test_mutation_eligibility_exposes_one_action_or_operator_stop(self) -> None:
        with poisoned_surface():
            working = decisions.task_mutation_eligibility("X", "WORKING")
            queued = decisions.task_mutation_eligibility("X", "QUEUED")
            child = decisions.task_mutation_eligibility(
                "X", None, tracking_owner="UMB")
        self.assertEqual((working.operation, working.eligible, working.safe_next_action),
                         ("finish", True, "yy task preflight X"))
        self.assertEqual((queued.operation, queued.eligible, queued.reason_code),
                         (None, False, "task_already_queued"))
        self.assertEqual(child.safe_next_action, "yy task status UMB")
        self.assertTrue(all(item.invalidating_change for item in (working, queued, child)))


class HandoffPhaseTables(unittest.TestCase):
    def test_every_durable_state_has_a_phase(self) -> None:
        expected = {
            "NOT_STARTED": "planned", "WORKING": "working", "QUEUED": "queued",
            "AWAITING_RISK": "validating", "REVIEWING": "reviewing",
            "REVIEW_FINDINGS": "findings",
            "REVIEW_FINDINGS_EXHAUSTED": "exhausted", "CONFLICT": "conflict",
            "CONFLICT_RESOLVED": "resolved", "REOPENING": "reopening",
            "REQUEUING_STALE": "restale", "RISK_EVIDENCE_READY": "approved",
            "MERGING": "merging", "MERGED": "merged",
            "KANBAN_SYNC_REQUIRED": "kanban-sync-required",
            "WITHDRAWN": "withdrawn",
        }
        with poisoned_surface():
            for state, phase in expected.items():
                self.assertEqual(decisions.handoff_phase(state), phase, state)
            self.assertEqual(decisions.handoff_phase("FUTURE_STATE"), "FUTURE_STATE")


class PathAdmissionTables(unittest.TestCase):
    def test_exact_roots_admit_their_tree_and_nothing_else(self) -> None:
        with poisoned_surface():
            self.assertTrue(decisions.path_within("juno-code/src/a.ts", ["juno-code"]))
            self.assertTrue(decisions.path_within("juno-code", ["juno-code"]))
            self.assertFalse(decisions.path_within("juno-codex/a", ["juno-code"]))
            self.assertFalse(decisions.path_within("juno-code", ["juno-code/src"]))
            self.assertFalse(decisions.path_within("", ["juno-code"]))
            # Lexical prefix semantics by design: Git pathnames never contain
            # dot components, and callers pass canonical Git output only.
            self.assertTrue(
                decisions.path_within("juno-code/../secret", ["juno-code"]))

    def test_empty_root_set_admits_nothing(self) -> None:
        with poisoned_surface():
            self.assertFalse(decisions.path_within("juno-code", []))


class ValidationRoutingTables(unittest.TestCase):
    CONFIG = {
        "validation_profiles": [
            {"id": "benchmark-suite", "path_roots": ["juno-benchmark"],
             "commands": [{"id": "benchmark-test"}, {"id": "benchmark-build"}]},
        ],
        "full_suite_validation": {"id": "full-suite"},
        "focused_validation": [
            {"id": "task-workspace", "cwd": "juno-code"},
            {"id": "integration-workspace", "cwd": "juno-code"},
            {"id": "benchmark-lint", "cwd": "juno-benchmark"},
        ],
    }

    def selection(self, changed):
        with poisoned_surface():
            return decisions.validation_profile_selection(self.CONFIG, changed)

    def test_uncovered_paths_route_conservatively_to_the_default_suite(self) -> None:
        selection = self.selection(["juno-code/src/a.ts"])
        self.assertEqual(selection["mode"], "default")
        self.assertEqual(selection["profile_ids"], [])
        self.assertEqual(selection["authored_path_count"], 1)
        selection = self.selection([])
        self.assertEqual(selection["mode"], "default")

    def test_one_fully_covered_profile_selects_that_suite_alone(self) -> None:
        selection = self.selection(["juno-benchmark/a.ts", "juno-benchmark/b.js"])
        self.assertEqual(selection["mode"], "profile")
        self.assertEqual(selection["profile_ids"], ["benchmark-suite"])

    def test_spread_or_partial_coverage_uses_union_semantics(self) -> None:
        selection = self.selection(["juno-benchmark/a.ts", "juno-code/src/a.ts"])
        self.assertEqual(selection["mode"], "union")
        self.assertEqual(selection["profile_ids"], ["benchmark-suite"])
        selection = self.selection(["juno-benchmark/a.ts", "docs/x.md"])
        self.assertEqual(selection["mode"], "union")

    def test_full_suite_commands_add_default_only_outside_profile_mode(self) -> None:
        with poisoned_surface():
            commands, selection = decisions.selected_full_suite_commands(
                self.CONFIG, ["juno-benchmark/a.ts"])
        self.assertEqual([row["id"] for row in commands],
                         ["benchmark-test", "benchmark-build"])
        self.assertEqual(selection["mode"], "profile")
        with poisoned_surface():
            commands, _ = decisions.selected_full_suite_commands(
                self.CONFIG, ["juno-code/src/a.ts"])
        self.assertEqual([row["id"] for row in commands], ["full-suite"])
        with poisoned_surface():
            commands, _ = decisions.selected_full_suite_commands(
                self.CONFIG, ["juno-benchmark/a.ts", "docs/x.md"])
        self.assertEqual([row["id"] for row in commands],
                         ["benchmark-test", "benchmark-build", "full-suite"])

    def test_focused_rows_run_everything_unless_one_profile_covers_all(self) -> None:
        with poisoned_surface():
            rows = decisions.selected_focused_rows(self.CONFIG, ["juno-code/src/a.ts"])
        self.assertEqual([row["id"] for row in rows],
                         ["task-workspace", "integration-workspace", "benchmark-lint"])
        with poisoned_surface():
            rows = decisions.selected_focused_rows(
                self.CONFIG, ["juno-benchmark/a.ts"])
        self.assertEqual([row["id"] for row in rows], ["benchmark-lint"])

    def test_standing_rows_execute_package_gates_with_default_union(self) -> None:
        with poisoned_surface():
            rows = decisions.selected_standing_rows(
                self.CONFIG, ["juno-benchmark/a.ts"])
        self.assertEqual([row["id"] for row in rows],
                         ["benchmark-test", "benchmark-build"])
        with poisoned_surface():
            rows = decisions.selected_standing_rows(
                self.CONFIG, ["juno-benchmark/a.ts", "juno-code/src/a.ts"])
        self.assertEqual([row["id"] for row in rows], [
            "benchmark-test", "benchmark-build", "task-workspace",
            "integration-workspace", "benchmark-lint"])

    def test_non_string_paths_are_ignored_rather_than_routed(self) -> None:
        selection = self.selection([None, 3, "juno-benchmark/a.ts"])
        self.assertEqual(selection["authored_path_count"], 1)
        self.assertEqual(selection["mode"], "profile")


class EvidenceReuseTables(unittest.TestCase):
    ROW = {"id": "task-workspace", "cwd": "juno-code", "argv": ["npm", "test"],
           "timeout_seconds": 900, "max_output_bytes": 32768}

    def plan(self, facts, commands=None, readiness="r1", route=None):
        with poisoned_surface():
            return decisions.plan_evidence_reuse(
                commands if commands is not None else [self.ROW],
                facts, readiness, route)

    def test_absent_receipt_executes(self) -> None:
        plan = self.plan([None])
        self.assertEqual(plan.entries[0].action, decisions.ACTION_EXECUTE)
        self.assertEqual(plan.counters, {"executed": 1, "reused": 0, "invalidated": 0})
        self.assertIsNone(plan.terminal)

    def test_passing_receipt_is_reused_exactly(self) -> None:
        plan = self.plan([decisions.ReceiptFact(present=True, valid=True,
                                                 failed_prior=False,
                                                 readiness_sha256="r1")])
        self.assertEqual(plan.entries[0].action, decisions.ACTION_REUSE)
        self.assertEqual(plan.counters, {"executed": 0, "reused": 1, "invalidated": 0})

    def test_failed_receipt_with_unchanged_readiness_stands(self) -> None:
        plan = self.plan([decisions.ReceiptFact(present=True, valid=True,
                                                 failed_prior=True,
                                                 readiness_sha256="r1")],
                         readiness="r1")
        entry = plan.entries[0]
        self.assertEqual(entry.action, decisions.ACTION_FAILURE_STANDS)
        self.assertTrue(entry.stop_after)
        self.assertIsNone(entry.finding)

    def test_failed_receipt_with_new_readiness_gets_one_supersession(self) -> None:
        plan = self.plan([decisions.ReceiptFact(present=True, valid=True,
                                                 failed_prior=True,
                                                 readiness_sha256="r0")],
                         readiness="r1")
        entry = plan.entries[0]
        self.assertEqual(entry.action, decisions.ACTION_INVALIDATE)
        self.assertEqual(entry.invalidation,
                         [{"field": "readiness_sha256", "old": "r0", "new": "r1"}])
        self.assertEqual(entry.supersession_suffix, ".readiness-r1.json")
        self.assertEqual(plan.counters["invalidated"], 1)

    def test_consumed_supersession_fails_closed(self) -> None:
        plan = self.plan([decisions.ReceiptFact(present=True, valid=True,
                                                 failed_prior=True,
                                                 readiness_sha256="r0",
                                                 supersession_exists=True)],
                         readiness="r1")
        entry = plan.entries[0]
        self.assertEqual(entry.action, "fail_closed")
        self.assertEqual(entry.finding.code, "supersession_exhausted")
        self.assertEqual(entry.finding.message,
                         "failed evidence already consumed its one readiness supersession")
        self.assertTrue(entry.stop_after)

    def test_malformed_receipt_is_never_silently_repaired(self) -> None:
        plan = self.plan([decisions.ReceiptFact(present=True, valid=False)])
        entry = plan.entries[0]
        self.assertEqual(entry.action, "fail_closed")
        self.assertEqual(entry.finding.code, "malformed_receipt")
        self.assertEqual(entry.finding.message, "standing command receipt is malformed")

    def test_zero_command_plan_carries_its_terminal_decision(self) -> None:
        plan = self.plan([], commands=[], route={"mode": "inert_zero_command",
                                                 "route_sha256": "d1"})
        terminal = plan.terminal
        self.assertEqual(terminal["command_id"], "documentation-zero-command")
        self.assertEqual(terminal["decision"], "skipped")
        self.assertEqual(terminal["closure"], {"input_closure_sha256": "d1"})
        self.assertEqual(terminal["reason"], "exact inert-documentation profile proof")
        plan = self.plan([], commands=[], route={"mode": "default",
                                                 "route_sha256": "d2"})
        self.assertEqual(plan.terminal["command_id"], "focused-validation")
        self.assertEqual(plan.terminal["decision"], "not_applicable")
        self.assertEqual(plan.terminal["closure"], {"input_closure_sha256": "d2"})

    def test_mixed_plan_counters_partition_the_commands(self) -> None:
        plan = self.plan([
            None,
            decisions.ReceiptFact(present=True, valid=True, failed_prior=False),
            decisions.ReceiptFact(present=True, valid=True, failed_prior=True,
                                  readiness_sha256="r0"),
        ], commands=[self.ROW, self.ROW, self.ROW], readiness="r1")
        self.assertEqual(plan.counters,
                         {"executed": 1, "reused": 1, "invalidated": 1})


class FailureContractTables(unittest.TestCase):
    def test_timeout_message_names_command_and_budget(self) -> None:
        with poisoned_surface():
            message = decisions.validation_failure_message(
                self.row(), {"timed_out": True, "exit_code": 124,
                             "stderr_tail": "", "stdout_tail": "x"})
        self.assertEqual(message, "focused validation timed out (suite) after 900s")

    def test_exit_message_prefers_stderr_then_stdout(self) -> None:
        row, result = self.row(), {"timed_out": False, "exit_code": 1,
                                   "stderr_tail": "boom", "stdout_tail": "out"}
        with poisoned_surface():
            self.assertEqual(
                decisions.validation_failure_message(row, result),
                "focused validation failed (suite, exit 1): boom")
        result = {"timed_out": False, "exit_code": 2,
                  "stderr_tail": "", "stdout_tail": "out"}
        with poisoned_surface():
            self.assertEqual(
                decisions.validation_failure_message(row, result),
                "focused validation failed (suite, exit 2): out")

    @staticmethod
    def row():
        return {"id": "suite", "timeout_seconds": 900}


class QueueingDecisionTables(unittest.TestCase):
    def test_sequence_admission_contract(self) -> None:
        with poisoned_surface():
            self.assertEqual(decisions.next_enqueue_sequence(
                {"schema_version": "juno_task_workspace_fifo.v1", "next": 7}), 7)
            for bad in (None, 3, {}, {"schema_version": "other", "next": 1},
                        {"schema_version": "juno_task_workspace_fifo.v1"},
                        {"schema_version": "juno_task_workspace_fifo.v1",
                         "next": True},
                        {"schema_version": "juno_task_workspace_fifo.v1", "next": 0},
                        {"schema_version": "juno_task_workspace_fifo.v1",
                         "next": 2**63},
                        {"schema_version": "juno_task_workspace_fifo.v1", "next": 1,
                         "extra": 2}):
                self.assertRaises(ValueError, decisions.next_enqueue_sequence, bad)

    def test_shared_queue_delta_reports_only_queue_owned_changes(self) -> None:
        before = {"schema_version": "s", "tasks": {"A": {"state": "WORKING"}},
                  "queues": {"fifo": {"next": 1}}}
        after = {"schema_version": "s", "tasks": {"A": {"state": "QUEUED"}},
                 "queues": {"fifo": {"next": 2}}}
        with poisoned_surface():
            self.assertEqual(decisions.shared_queue_delta(before, after),
                             ["queues.fifo.next"])
            self.assertEqual(decisions.shared_queue_delta(before, before), [])
            self.assertEqual(decisions.shared_queue_delta({}, {"queues": {}}),
                             ["queues"])
            self.assertEqual(decisions.shared_queue_delta(5, {}), ["<state>"])

    def test_missing_keys_and_unequal_leaves_differ(self) -> None:
        with poisoned_surface():
            self.assertEqual(
                decisions.shared_queue_delta(
                    {"queues": {"a": 1}}, {"queues": {"a": 2}}), ["queues.a"])
            self.assertEqual(
                decisions.shared_queue_delta(
                    {"queues": {"a": 1}}, {"queues": {}}), ["queues.a"])


class ShellWiringCharacterization(unittest.TestCase):
    """The imperative shell must route its gates through the pure planners."""

    def test_shell_aliases_the_pure_core(self) -> None:
        import task_workspace as shell
        self.assertIs(shell.path_within, decisions.path_within)
        self.assertIs(shell.validation_profile_selection,
                      decisions.validation_profile_selection)
        self.assertIs(shell.selected_full_suite_commands,
                      decisions.selected_full_suite_commands)
        self.assertIs(shell.selected_focused_rows, decisions.selected_focused_rows)
        self.assertIs(shell.selected_standing_rows, decisions.selected_standing_rows)
        self.assertIs(shell._handoff_phase, decisions.handoff_phase)

    def test_shell_gates_call_the_planner(self) -> None:
        shell = (Path(__file__).resolve().parents[1] / "task_workspace.py").read_text()
        self.assertIn("import task_workspace_decisions as decisions", shell)
        for marker in (
            'decisions.CommandRequest("start", task_id)',
            'decisions.CommandRequest("hydrate", task_id)',
            'decisions.CommandRequest("checkpoint", task_id)',
            'decisions.CommandRequest("evidence-run", task_id)',
            'decisions.CommandRequest("preflight", task_id)',
            'decisions.CommandRequest("finish", task_id)',
            'decisions.CommandRequest("child-checkpoint", task_id)',
        ):
            self.assertIn(marker, shell)
        self.assertIn("decisions.plan_evidence_reuse(", shell)
        self.assertIn("decisions.validation_failure_message(", shell)
        self.assertIn("decisions.status_projection(", shell)
        self.assertIn("decisions.next_enqueue_sequence(", shell)

    def test_shell_fence_gates_call_the_lease_planners(self) -> None:
        shell = (Path(__file__).resolve().parents[1] / "task_workspace.py").read_text()
        self.assertIn("decisions.plan_lease_authority(", shell)
        self.assertIn("decisions.plan_lease_successor(", shell)
        self.assertIn("_require_lease_fence(controller, \"start\"", shell)
        self.assertIn("_require_lease_fence(controller, \"hydrate\"", shell)
        self.assertIn("_require_lease_fence(controller, \"checkpoint\"", shell)
        self.assertIn("_require_lease_fence(controller, \"finish\"", shell)
        self.assertIn("_require_lease_fence(controller, \"sync\"", shell)


class LeaseAuthorityTables(unittest.TestCase):
    """Pure tables for fenced mutation authority and successor issuance."""

    @staticmethod
    def lease(state: str = "ACTIVE", kind: str = "process", pid: int = 4242,
              token: str = "digest-a") -> dict:
        return {"attempt": 1, "state": state, "producer_kind": kind,
                "producer": {"pid": pid}, "token_sha256": token}

    def run_authority(self, lease, token, observation, pid=999):
        with poisoned_surface():
            return decisions.plan_lease_authority(
                "checkpoint", "T1", lease, token, observation, pid)

    def test_no_active_lease_is_unfenced(self) -> None:
        for lease in (None, {}, self.lease(state="RELEASED"),
                      self.lease(state="REVOKED"), self.lease(state="HANDED_OFF")):
            decision = self.run_authority(
                lease, None, decisions.LeaseObservation("unknown", "x"))
            self.assertTrue(decision.admitted)
            self.assertEqual(decision.authority, "unfenced")

    def test_exact_token_admits_and_wrong_token_is_stale(self) -> None:
        decision = self.run_authority(
            self.lease(), "digest-a", decisions.LeaseObservation("alive", "pid live"))
        self.assertTrue(decision.admitted)
        self.assertEqual(decision.authority, "token")
        stale = self.run_authority(
            self.lease(), "digest-b", decisions.LeaseObservation("alive", "pid live"))
        self.assertFalse(stale.admitted)
        self.assertEqual(stale.code, decisions.LEASE_CODE_FENCE_STALE)

    def test_same_pid_live_producer_continuity_admits_only_process_leases(self) -> None:
        decision = self.run_authority(
            self.lease(), None, decisions.LeaseObservation("alive", "pid live"), pid=4242)
        self.assertTrue(decision.admitted)
        self.assertEqual(decision.authority, "producer")
        session = self.run_authority(
            self.lease(kind="session"), None,
            decisions.LeaseObservation("alive", "pid live"), pid=4242)
        self.assertFalse(session.admitted)
        self.assertEqual(session.code, decisions.LEASE_CODE_PRODUCER_MISMATCH)

    def test_dead_producer_and_unprovable_producer_fail_closed_with_codes(self) -> None:
        dead = self.run_authority(
            self.lease(), None, decisions.LeaseObservation("dead", "pid gone"))
        self.assertFalse(dead.admitted)
        self.assertEqual(dead.code, decisions.LEASE_CODE_PRODUCER_DEAD)
        self.assertIn("lease-successor", dead.message)
        unknown = self.run_authority(
            self.lease(), None, decisions.LeaseObservation("unknown", "ps failed"))
        self.assertFalse(unknown.admitted)
        self.assertEqual(unknown.code, decisions.LEASE_CODE_TOKEN_REQUIRED)
        self.assertIn("lease-revoke", unknown.message)

    def test_gated_command_vocabulary_is_bounded(self) -> None:
        self.assertEqual(decisions.LEASE_GATED_COMMANDS, frozenset({
            "start", "hydrate", "checkpoint", "child-checkpoint",
            "evidence-run", "finish", "sync"}))


class ResumeDecisionTables(unittest.TestCase):
    def decide(self, **values):
        with poisoned_surface():
            return decisions.plan_resume(decisions.ResumeFacts(owner="task", **values))

    def test_launch_terminal_and_deterministic_phase_choose_smallest_verified_stage(self) -> None:
        launch = self.decide(launch_observed=False, resumable_stage="IMPLEMENTING")
        self.assertTrue(launch.admitted)
        self.assertEqual(launch.classification, decisions.RESUME_LAUNCH_NOT_STARTED)
        self.assertEqual(launch.restart_stage, "IMPLEMENTING")
        terminal = self.decide(exact_terminal=True, launch_observed=True,
                               resumable_stage="CAPTURE")
        self.assertTrue(terminal.admitted)
        self.assertEqual(terminal.classification, decisions.RESUME_EXACT_TERMINAL_CAPTURE)
        deterministic = self.decide(producer_status="dead", launch_observed=True,
                                    resumable_stage="VALIDATING")
        self.assertTrue(deterministic.admitted)
        self.assertEqual(deterministic.classification,
                         decisions.RESUME_DETERMINISTIC_PHASE)

    def test_live_unknown_conflict_stale_and_budget_exhaustion_stop(self) -> None:
        cases = [
            ({"producer_status": "alive"}, decisions.RESUME_LIVE_AUTHORITY),
            ({"producer_status": "unknown"}, decisions.RESUME_UNKNOWN_OUTCOME),
            ({"conflict": True}, decisions.RESUME_REAL_CONFLICT),
            ({"stale_authority": True}, decisions.RESUME_STALE_AUTHORITY),
            ({"budget_remaining": False}, decisions.RESUME_BUDGET_EXHAUSTED),
        ]
        for values, classification in cases:
            with self.subTest(classification=classification):
                decision = self.decide(**values)
                self.assertFalse(decision.admitted)
                self.assertEqual(decision.classification, classification)

    def test_target_resume_routes_only_to_existing_arbiter(self) -> None:
        decision = decisions.plan_resume(decisions.ResumeFacts(
            owner="target", producer_status="dead", launch_observed=True,
            explicit_handoff=True, resumable_stage="FINALIZING"))
        self.assertEqual(decision.owner_command, "yy merge arbiter run")
        self.assertEqual(decision.restart_stage, "FINALIZING")


class LeaseSuccessorTables(unittest.TestCase):
    @staticmethod
    def lease(state: str = "ACTIVE", kind: str = "process") -> dict:
        return {"attempt": 2, "state": state, "producer_kind": kind,
                "producer": {"pid": 777}, "token_sha256": "digest-z"}

    def run_successor(self, lease, observation, handoff=None):
        with poisoned_surface():
            return decisions.plan_lease_successor(
                "T1", lease, observation, handoff)

    def test_dead_process_producer_admits_death_successor(self) -> None:
        decision = self.run_successor(
            self.lease(), decisions.LeaseObservation("dead", "pid gone"))
        self.assertTrue(decision.admitted)
        self.assertEqual(decision.authority_kind, "successor_death")

    def test_live_or_unknown_producer_never_yields_successor(self) -> None:
        live = self.run_successor(
            self.lease(), decisions.LeaseObservation("alive", "pid live"))
        self.assertFalse(live.admitted)
        self.assertEqual(live.code, decisions.LEASE_CODE_PRODUCER_LIVE)
        self.assertIn("expiry alone", live.message)
        unknown = self.run_successor(
            self.lease(), decisions.LeaseObservation("unknown", "no anchor"))
        self.assertFalse(unknown.admitted)
        self.assertEqual(unknown.code, decisions.LEASE_CODE_PRODUCER_UNKNOWN)

    def test_session_lease_requires_revoke_and_handoff_needs_receipt(self) -> None:
        session = self.run_successor(
            self.lease(kind="session"), decisions.LeaseObservation("dead", "pid gone"))
        self.assertFalse(session.admitted)
        self.assertEqual(session.code, decisions.LEASE_CODE_TOKEN_REQUIRED)
        self.assertIn("lease-revoke", session.message)
        active_with_receipt = self.run_successor(
            self.lease(), decisions.LeaseObservation("dead", "pid gone"),
            handoff={"kind": "handoff"})
        self.assertFalse(active_with_receipt.admitted)
        handed_off = self.run_successor(
            self.lease(state="HANDED_OFF"), decisions.LeaseObservation("alive", "x"))
        self.assertFalse(handed_off.admitted)
        self.assertEqual(handed_off.code, decisions.LEASE_CODE_TOKEN_REQUIRED)
        consumed = self.run_successor(
            self.lease(state="HANDED_OFF"), decisions.LeaseObservation("alive", "x"),
            handoff={"kind": "handoff", "attempt": 2})
        self.assertTrue(consumed.admitted)
        self.assertEqual(consumed.authority_kind, "successor_handoff")

    def test_revoke_authorizes_and_release_terminates(self) -> None:
        revoked = self.run_successor(
            self.lease(state="REVOKED"), decisions.LeaseObservation("alive", "x"))
        self.assertTrue(revoked.admitted)
        self.assertEqual(revoked.authority_kind, "successor_revoke")
        released = self.run_successor(
            self.lease(state="RELEASED"), decisions.LeaseObservation("dead", "x"))
        self.assertFalse(released.admitted)
        self.assertEqual(released.code, decisions.LEASE_CODE_RELEASED)
        empty = self.run_successor(None, decisions.LeaseObservation("dead", "x"))
        self.assertFalse(empty.admitted)
        self.assertEqual(empty.code, decisions.LEASE_CODE_NOT_ACTIVE)


class CanonicalPathOriginProjectionTests(unittest.TestCase):
    def test_projection_is_versioned_and_altered_inherited_bytes_are_authored(self) -> None:
        projection = decisions.project_path_origins(
            base_tree={"target.txt": "b0", "feature.txt": "f0"},
            source_tree={"target.txt": "tampered", "feature.txt": "f1"},
            target_tree={"target.txt": "t1", "feature.txt": "f0"},
            candidate_tree={"target.txt": "tampered", "feature.txt": "f1"},
            admitted_paths=["feature.txt"], generated_bindings=[], conflict_paths=[])
        self.assertEqual(projection["schema_version"], "juno_path_origin_projection.v1")
        self.assertEqual(projection["authored_paths"], ["feature.txt", "target.txt"])
        self.assertEqual(projection["target_derived_paths"], [])
        self.assertEqual(projection["candidate_delta_paths"], ["feature.txt", "target.txt"])

    def test_projection_accepts_unchanged_target_bytes_and_fails_ambiguous_legacy_admission(self) -> None:
        projection = decisions.project_path_origins(
            base_tree={"target.txt": "b0"}, source_tree={"target.txt": "t1"},
            target_tree={"target.txt": "t1"}, candidate_tree={"target.txt": "t1"},
            admitted_paths=[], generated_bindings=[], conflict_paths=[])
        self.assertEqual(projection["target_derived_paths"], ["target.txt"])
        ambiguous = decisions.project_path_origins(
            base_tree={"same.txt": "x"}, source_tree={"same.txt": "x"},
            target_tree={"same.txt": "x"}, candidate_tree={"same.txt": "x"},
            admitted_paths=["same.txt"], generated_bindings=[], conflict_paths=[])
        self.assertEqual(ambiguous["ambiguous_paths"], ["same.txt"])


def tearDownModule() -> None:
    elapsed = time.monotonic() - MODULE_STARTED
    if elapsed >= PURE_BUDGET_SECONDS:
        raise AssertionError(
            f"pure decision test module exceeded the Wave 3 budget: {elapsed:.2f}s")


if __name__ == "__main__":
    unittest.main(verbosity=1)
