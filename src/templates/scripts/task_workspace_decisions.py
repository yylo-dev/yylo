#!/usr/bin/env python3
"""Pure task-workspace lifecycle decisions (Wave 3 pilot of 7djT8N).

Functional core for the task-workspace lifecycle. Every planner here is a
total function over immutable snapshot inputs: it must not read Git, the
filesystem, the environment, a clock, the network, locks, or subprocesses,
and it must not mutate its inputs. Physical identity resolution, Git
inspection and mutation, filesystem writes, locking, validator dispatch,
receipt persistence, and CLI rendering stay in ``task_workspace.py`` (the
imperative shell) and its sibling adapter modules.

The planners encode the exact transition, hydration, path-admission,
validation-routing, evidence-reuse/invalidation, queueing, and failure
contracts previously inlined in the imperative shell. Refusal messages are
characterization-pinned: the real-Git scenario suite in
``tests/test_task_workspace.py`` asserts the same strings end to end, so a
planner regression fails both the pure tables here and the scenario suite.

Purity is enforced, not merely documented:
``tests/test_task_workspace_decisions.py`` audits this module's import graph
against a strict allowlist and executes every pure table with ``open`` and
process creation poisoned, in addition to bounding total wall time.

Wave 3 pilot scope: task-workspace only. Whether the merge queue justifies
the same extraction is a measured follow-up decision recorded in
``docs/test-performance.md``; it is not silently absorbed here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

DECISIONS_SCHEMA_VERSION = "juno.task_workspace.decisions.v1"

# Canonical task-workspace lifecycle states (mirrors task_workspace.py).
WORKING = "WORKING"
QUEUED = "QUEUED"
HYDRATING = "HYDRATING"
HYDRATION_FAILED = "HYDRATION_FAILED"
REVIEW_FINDINGS = "REVIEW_FINDINGS"
KANBAN_SYNC_STATE = "KANBAN_SYNC_REQUIRED"
NOT_STARTED = "NOT_STARTED"
TRACKING_ONLY = "TRACKING_ONLY"

# `yy task hydrate` may explicitly rerun frozen hydration from exactly these
# states; anything else (missing record included) refuses.
HYDRATABLE_STATES = frozenset({WORKING, HYDRATION_FAILED, HYDRATING, REVIEW_FINDINGS})

# Handoff phase projection: durable lifecycle state -> agent-facing phase.
HANDOFF_PHASES = {
    "NOT_STARTED": "planned", "WORKING": "working", "QUEUED": "queued",
    "AWAITING_RISK": "validating",
    "REVIEWING": "reviewing", "REVIEW_FINDINGS": "findings",
    "REVIEW_FINDINGS_EXHAUSTED": "exhausted", "CONFLICT": "conflict",
    "CONFLICT_RESOLVED": "resolved", "REOPENING": "reopening",
    "REQUEUING_STALE": "restale", "RISK_EVIDENCE_READY": "approved",
    "MERGING": "merging", "MERGED": "merged",
    KANBAN_SYNC_STATE: "kanban-sync-required", "WITHDRAWN": "withdrawn",
}

# Evidence-reuse actions (command decision vocabulary shared with the shell).
ACTION_EXECUTE = "execute"
ACTION_REUSE = "reuse"
ACTION_FAILURE_STANDS = "failure_stands"
ACTION_INVALIDATE = "invalidate_and_execute"

# Queue-owned sentinel for the shared-field delta walk: a missing key differs
# from any present value and never equals user data.
QUEUE_MISSING = object()


@dataclass(frozen=True)
class CommandRequest:
    """One lifecycle command addressed to one task id."""

    command: str
    task_id: str


@dataclass(frozen=True)
class TaskSnapshot:
    """Immutable lifecycle facts the shell observed before requesting a plan.

    ``state`` is ``None`` when no task record exists. ``tracking_owner`` is
    the umbrella owner recorded in child reservations (``None`` when the task
    is not a tracking-only umbrella child).
    """

    task_id: str
    state: Optional[str] = None
    tracking_owner: Optional[str] = None


@dataclass(frozen=True)
class Finding:
    """One fail-closed refusal or advisory produced by a planner."""

    code: str
    message: str


@dataclass(frozen=True)
class TransitionDecision:
    """Planned transition for one command against one task snapshot."""

    command: str
    task_id: str
    admitted: bool
    state: Optional[str]
    phase: str
    idempotent: bool = False
    finding: Optional[Finding] = None


@dataclass(frozen=True)
class StatusProjection:
    """Read-only status projection for an absent task record."""

    state: str
    umbrella_owner_task_id: Optional[str] = None
    next_action: Optional[str] = None


@dataclass(frozen=True)
class ReceiptFact:
    """Immutable facts about one persisted standing-evidence receipt.

    ``present`` reflects the receipt file; ``valid`` is the combined schema,
    closure, command, and result integrity the shell checked while reading
    it; ``failed_prior`` is true when the persisted result timed out or
    exited nonzero; ``supersession_exists`` is true when the one allowed
    readiness supersession file for the current readiness identity already
    exists.
    """

    present: bool
    valid: bool = False
    failed_prior: bool = False
    readiness_sha256: Optional[str] = None
    supersession_exists: bool = False


@dataclass(frozen=True)
class EvidenceCommandPlan:
    """Planned handling of one standing-evidence command."""

    action: str
    finding: Optional[Finding] = None
    invalidation: Optional[list[dict[str, Any]]] = None
    supersession_suffix: Optional[str] = None
    stop_after: bool = False


@dataclass(frozen=True)
class EvidenceReusePlan:
    """Complete pre-execution reuse plan for one standing checkpoint.

    ``terminal`` carries the zero-command decision (skipped for an exact
    inert-documentation proof, not_applicable when a selected package
    profile has no matching focused command). Execution results can still
    stop the walk early; ``stop_after`` marks the deterministic stops.
    """

    entries: tuple[EvidenceCommandPlan, ...]
    terminal: Optional[dict[str, Any]] = None

    @property
    def counters(self) -> dict[str, int]:
        executed = reused = invalidated = 0
        for entry in self.entries:
            if entry.action == ACTION_EXECUTE:
                executed += 1
            elif entry.action == ACTION_REUSE:
                reused += 1
            elif entry.action == ACTION_INVALIDATE:
                invalidated += 1
        return {"executed": executed, "reused": reused, "invalidated": invalidated}


def handoff_phase(state: str) -> str:
    """Project one durable lifecycle state onto the agent-facing phase."""
    return HANDOFF_PHASES.get(state, state)


def path_within(path: str, roots: list[str]) -> bool:
    """Admit one repository path under an exact root set (no prefix games)."""
    return any(path == root or path.startswith(root + "/") for root in roots)


PATH_ORIGIN_PROJECTION_SCHEMA = "juno_path_origin_projection.v1"


def project_path_origins(*, base_tree: dict[str, Optional[str]],
                         source_tree: dict[str, Optional[str]],
                         target_tree: dict[str, Optional[str]],
                         candidate_tree: dict[str, Optional[str]],
                         admitted_paths: list[str],
                         generated_bindings: list[dict[str, Any]],
                         conflict_paths: list[str]) -> dict[str, Any]:
    """Classify frozen Git blob maps without ancestry-order shortcuts.

    Admitted legacy ``changed_paths`` are evidence only when the source blob
    actually differs from the immutable base.  An inherited target blob is
    target-derived only while it remains byte-identical to the target; altered
    inherited bytes are authored.  Every consumer receives this same versioned
    projection and must fail closed on ``ambiguous_paths``.
    """
    admitted = set(admitted_paths)
    conflicts = set(conflict_paths)
    generated = {
        row.get("destination") for row in generated_bindings
        if isinstance(row, dict) and isinstance(row.get("destination"), str)
    }
    paths = sorted(set(base_tree) | set(source_tree) | set(target_tree)
                   | set(candidate_tree) | admitted | conflicts | generated)
    rows: list[dict[str, Any]] = []
    authored: list[str] = []
    target_derived: list[str] = []
    generated_paths: list[str] = []
    candidate_delta: list[str] = []
    ambiguous: list[str] = []
    for path in paths:
        base = base_tree.get(path); source = source_tree.get(path)
        target = target_tree.get(path); candidate = candidate_tree.get(path)
        labels: list[str] = []
        source_changed = source != base
        target_changed = target != base
        if path in admitted and not source_changed:
            labels.append("ambiguous-legacy-admission")
            ambiguous.append(path)
        if source_changed:
            if target_changed and source == target and path not in admitted:
                labels.append("target-derived")
                target_derived.append(path)
            else:
                labels.append("authored")
                authored.append(path)
        if path in generated:
            labels.append("generated")
            generated_paths.append(path)
        if path in conflicts:
            labels.append("conflict")
        if candidate != target:
            labels.append("candidate-delta")
            candidate_delta.append(path)
        if labels:
            rows.append({"path": path, "origins": labels, "base_blob": base,
                         "source_blob": source, "target_blob": target,
                         "candidate_blob": candidate})
    return {"schema_version": PATH_ORIGIN_PROJECTION_SCHEMA,
            "authored_paths": authored,
            "target_derived_paths": target_derived,
            "generated_paths": generated_paths,
            "conflict_paths": sorted(conflicts),
            "candidate_delta_paths": candidate_delta,
            "ambiguous_paths": ambiguous, "paths": rows}


def validation_profile_selection(config: dict[str, Any],
                                 changed_paths: Any) -> dict[str, Any]:
    """Deterministically route validation from the authored Git path set.

    Exactly one matched profile covering every authored path selects that
    package-local suite alone. Any uncovered path, or a spread across profiles,
    conservatively adds the repository default suite (union semantics).
    """
    paths = [path for path in (changed_paths or []) if isinstance(path, str)]
    matched = sorted(
        (profile for profile in (config.get("validation_profiles") or [])
         if isinstance(profile, dict)
         and any(path_within(path, profile.get("path_roots", [])) for path in paths)),
        key=lambda profile: str(profile.get("id")))
    covered_roots = [root for profile in matched for root in profile["path_roots"]]
    covered = bool(paths) and all(path_within(path, covered_roots) for path in paths)
    if not matched or not paths:
        mode = "default"
    elif covered and len(matched) == 1:
        mode = "profile"
    else:
        mode = "union"
    return {"mode": mode, "profile_ids": [profile["id"] for profile in matched],
            "authored_path_count": len(paths)}


def selected_full_suite_commands(config: dict[str, Any],
                                 changed_paths: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return the ordered full-suite command rows plus their routing selection."""
    selection = validation_profile_selection(config, changed_paths)
    commands: list[dict[str, Any]] = []
    for profile_id in selection["profile_ids"]:
        profile = next(row for row in config.get("validation_profiles") or []
                       if row["id"] == profile_id)
        commands.extend(profile["commands"])
    if selection["mode"] != "profile":
        commands.append(config["full_suite_validation"])
    return commands, selection


def selected_focused_rows(config: dict[str, Any], changed_paths: Any) -> list[dict[str, Any]]:
    """Route focused rows; mixed or default candidates run every focused row."""
    selection = validation_profile_selection(config, changed_paths)
    if selection["mode"] != "profile":
        return config["focused_validation"]
    roots = next(row for row in config.get("validation_profiles") or []
                 if row["id"] == selection["profile_ids"][0])["path_roots"]
    return [row for row in config["focused_validation"]
            if path_within(row["cwd"], roots)]


def selected_standing_rows(config: dict[str, Any], changed_paths: Any) -> list[dict[str, Any]]:
    """Return mandatory package gates plus the focused fallback when required."""
    selection = validation_profile_selection(config, changed_paths)
    commands: list[dict[str, Any]] = []
    for profile_id in selection["profile_ids"]:
        profile = next(row for row in config.get("validation_profiles") or []
                       if row["id"] == profile_id)
        commands.extend(profile["commands"])
    if selection["mode"] != "profile":
        commands.extend(config["focused_validation"])
    return commands


def _refuse(command: str, task_id: str, state: Optional[str], code: str,
            message: str) -> TransitionDecision:
    return TransitionDecision(command=command, task_id=task_id, admitted=False,
                              state=state, phase=handoff_phase(state or NOT_STARTED),
                              finding=Finding(code=code, message=message))


def plan_command_transition(request: CommandRequest,
                            snapshot: TaskSnapshot) -> TransitionDecision:
    """Plan one lifecycle command against immutable task facts.

    This is the state x command admission matrix. It reproduces the exact
    fail-closed refusals the imperative shell raises, including the
    tracking-only umbrella-child redirects, so adapters can raise
    ``TaskWorkspaceError(decision.finding.message)`` verbatim. Commands whose
    full admission additionally needs config, Git, or umbrella evidence
    (``start``) are planned for their state dimensions only; the shell keeps
    authoritative admission checks.
    """
    command, task_id = request.command, request.task_id
    state, owner = snapshot.state, snapshot.tracking_owner

    if command == "start":
        if owner is not None and owner != task_id:
            return _refuse(command, task_id, state, "tracking_only_child",
                           f"task {task_id} is tracking-only under umbrella {owner}")
        return TransitionDecision(command, task_id, True, state or NOT_STARTED,
                                  handoff_phase(state or NOT_STARTED),
                                  idempotent=state is not None)

    if command == "hydrate":
        if state not in HYDRATABLE_STATES:
            return _refuse(command, task_id, state, "unhydratable_state",
                           f"task cannot hydrate from {state if state is not None else 'missing'}")
        return TransitionDecision(command, task_id, True, state, handoff_phase(state))

    if command == "status":
        # Read-only: never refuses; absent records project tracking/absence.
        return TransitionDecision(command, task_id, True, state, handoff_phase(state or NOT_STARTED))

    if command == "checkpoint":
        if state != WORKING:
            if owner is not None:
                return _refuse(command, task_id, state, "tracking_only_child",
                               f"task {task_id} is tracking-only under umbrella {owner}; "
                               f"checkpoint the umbrella child instead: "
                               f"yy task child-checkpoint {owner} {task_id}")
            return _refuse(command, task_id, state, "requires_working_task",
                           "standing checkpoint requires a WORKING task")
        return TransitionDecision(command, task_id, True, WORKING, handoff_phase(WORKING))

    if command == "evidence-run":
        if state != WORKING:
            return _refuse(command, task_id, state, "requires_working_task",
                           "standing evidence run requires a WORKING task")
        return TransitionDecision(command, task_id, True, WORKING, handoff_phase(WORKING))

    if command in ("evidence-status", "evidence-await"):
        # Standing plan identity is authoritative; no task-state gate.
        return TransitionDecision(command, task_id, True, state, handoff_phase(state or NOT_STARTED))

    if command == "preflight":
        if state is None:
            if owner is not None:
                return _refuse(command, task_id, state, "tracking_only_child",
                               f"task {task_id} is tracking-only under umbrella {owner}; "
                               f"preflight the umbrella instead: yy task preflight {owner}")
            return _refuse(command, task_id, state, "not_started",
                           "task has not been started")
        if state != WORKING:
            return _refuse(command, task_id, state, "wrong_state",
                           f"task cannot preflight from {state}")
        return TransitionDecision(command, task_id, True, WORKING, handoff_phase(WORKING))

    if command == "finish":
        if state is None:
            if owner is not None:
                return _refuse(command, task_id, state, "tracking_only_child",
                               f"task {task_id} is tracking-only under umbrella {owner}; "
                               f"finish the umbrella instead: yy task finish {owner}")
            return _refuse(command, task_id, state, "not_started",
                           "task has not been started")
        if state == QUEUED:
            return TransitionDecision(command, task_id, True, QUEUED,
                                      handoff_phase(QUEUED), idempotent=True)
        if state != WORKING:
            return _refuse(command, task_id, state, "wrong_state",
                           f"task cannot finish from {state}")
        return TransitionDecision(command, task_id, True, WORKING, handoff_phase(WORKING))

    if command == "child-checkpoint":
        if state != WORKING:
            return _refuse(command, task_id, state, "requires_working_umbrella",
                           "umbrella child checkpoint requires a WORKING umbrella")
        return TransitionDecision(command, task_id, True, WORKING, handoff_phase(WORKING))

    raise ValueError(f"unknown task-workspace command: {command!r}")


def status_projection(snapshot: TaskSnapshot) -> StatusProjection:
    """Project an absent task record for read-only status."""
    if snapshot.tracking_owner is not None:
        owner = snapshot.tracking_owner
        return StatusProjection(
            state=TRACKING_ONLY,
            umbrella_owner_task_id=owner,
            next_action=("implement inside the umbrella worktree; "
                         f"record progress with: yy task child-checkpoint {owner} {snapshot.task_id}"))
    return StatusProjection(state=NOT_STARTED)


def plan_evidence_reuse(commands: list[dict[str, Any]],
                        facts: list[Optional[ReceiptFact]],
                        readiness_sha256: str,
                        documentation_route: Optional[dict[str, Any]] = None,
                        ) -> EvidenceReusePlan:
    """Plan standing-evidence command handling before any execution.

    Faithful encoding of the closure-bound reuse contract:

    - an absent receipt executes and persists a new immutable receipt;
    - a valid passing receipt is reused exactly (no re-execution);
    - a failed receipt is immutable: an unchanged readiness identity makes
      the failure stand (deterministic stop), while a changed readiness
      identity allows exactly one supersession before re-execution;
    - a malformed receipt is a fail-closed finding, never silent repair.

    Execution outcomes can still stop the walk (the shell breaks after the
    first failing result); ``stop_after`` marks the deterministic stops the
    planner itself proves.
    """
    entries: list[EvidenceCommandPlan] = []
    for fact in facts:
        if fact is None or not fact.present:
            entries.append(EvidenceCommandPlan(action=ACTION_EXECUTE))
            continue
        if not fact.valid:
            entries.append(EvidenceCommandPlan(
                action="fail_closed",
                finding=Finding(code="malformed_receipt",
                                message="standing command receipt is malformed"),
                stop_after=True))
            continue
        if fact.failed_prior:
            if fact.readiness_sha256 == readiness_sha256:
                entries.append(EvidenceCommandPlan(
                    action=ACTION_FAILURE_STANDS, stop_after=True))
            elif fact.supersession_exists:
                entries.append(EvidenceCommandPlan(
                    action="fail_closed",
                    finding=Finding(
                        code="supersession_exhausted",
                        message="failed evidence already consumed its one readiness supersession"),
                    stop_after=True))
            else:
                entries.append(EvidenceCommandPlan(
                    action=ACTION_INVALIDATE,
                    invalidation=[{"field": "readiness_sha256",
                                   "old": fact.readiness_sha256,
                                   "new": readiness_sha256}],
                    supersession_suffix=f".readiness-{readiness_sha256}.json"))
            continue
        entries.append(EvidenceCommandPlan(action=ACTION_REUSE))
    terminal: Optional[dict[str, Any]] = None
    if not commands:
        inert = isinstance(documentation_route, dict) and \
            documentation_route.get("mode") == "inert_zero_command"
        route_sha = (documentation_route or {}).get("route_sha256")
        terminal = {
            "command_id": "documentation-zero-command" if inert else "focused-validation",
            "decision": "skipped" if inert else "not_applicable",
            "closure": {"input_closure_sha256": route_sha},
            "reason": ("exact inert-documentation profile proof" if inert
                       else "selected package profile has no matching focused command"),
        }
    return EvidenceReusePlan(entries=tuple(entries), terminal=terminal)


def validation_failure_message(row: dict[str, Any], result: dict[str, Any]) -> str:
    """Exact fail-closed diagnostic for one failed focused-validation result."""
    if result["timed_out"]:
        return f"focused validation timed out ({row['id']}) after {row['timeout_seconds']}s"
    detail = result["stderr_tail"] or result["stdout_tail"]
    return f"focused validation failed ({row['id']}, exit {result['exit_code']}): {detail}"


def next_enqueue_sequence(meta: Any) -> int:
    """Validate the FIFO sequence section and decide the next sequence value.

    Pure decision half of ``assign_enqueue_sequence``: the shell owns the
    state mutation, this planner owns the admission contract.
    """
    if (not isinstance(meta, dict) or set(meta) != {"schema_version", "next"}
            or meta.get("schema_version") != "juno_task_workspace_fifo.v1"
            or not isinstance(meta.get("next"), int) or isinstance(meta.get("next"), bool)
            or not 1 <= meta["next"] <= 2**63 - 1):
        raise ValueError("task FIFO sequence state is invalid")
    return meta["next"]


def shared_queue_delta(before: Any, after: Any) -> list[str]:
    """Deterministic dotted paths for changed non-task queue state.

    Structurally identical to the verifier in controller_checkpoint.py:
    dictionaries recurse per key, every other value is a leaf, and a missing
    key differs from any present value. Only the canonical "queues" section
    is queue-owned shared state.
    """
    paths: set[str] = set()

    def walk(old: Any, new: Any, prefix: str) -> None:
        if isinstance(old, dict) and isinstance(new, dict):
            for key in sorted(set(old) | set(new)):
                walk(old.get(key, QUEUE_MISSING), new.get(key, QUEUE_MISSING),
                     f"{prefix}.{key}" if prefix else str(key))
            return
        if old is QUEUE_MISSING or new is QUEUE_MISSING or old != new:
            paths.add(prefix)

    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after)):
            if key == "tasks":
                continue
            walk(before.get(key, QUEUE_MISSING), after.get(key, QUEUE_MISSING), str(key))
    else:
        paths.add("<state>")
    return sorted(paths)


# ---------------------------------------------------------------------------
# Fenced task/worktree attempts (Wave 2 of the sealed merge-train PDR).
#
# A lease is controller-issued mutation authority over one task record.
# Expiry (heartbeat staleness) alone never grants takeover: a successor token
# is planned only after producer death is observable, an explicit handoff
# receipt exists, or an operator revoke receipt exists. Stale attempts fail
# closed with one actionable machine code.
# ---------------------------------------------------------------------------

LEASE_ACTIVE = "ACTIVE"
LEASE_RELEASED = "RELEASED"
LEASE_REVOKED = "REVOKED"
LEASE_HANDED_OFF = "HANDED_OFF"
LEASE_TERMINAL_STATES = frozenset({LEASE_RELEASED, LEASE_REVOKED, LEASE_HANDED_OFF})
PRODUCER_KINDS = frozenset({"process", "session"})

# Bounded mutation commands that carry worker authority over the task record.
LEASE_GATED_COMMANDS = frozenset({
    "start", "hydrate", "checkpoint", "child-checkpoint",
    "evidence-run", "finish", "sync",
})

LEASE_CODE_TOKEN_REQUIRED = "lease_token_required"
LEASE_CODE_FENCE_STALE = "lease_fence_stale"
LEASE_CODE_PRODUCER_MISMATCH = "lease_producer_mismatch"
LEASE_CODE_PRODUCER_DEAD = "lease_producer_dead"
LEASE_CODE_PRODUCER_LIVE = "lease_producer_live"
LEASE_CODE_PRODUCER_UNKNOWN = "lease_producer_unknown"
LEASE_CODE_NOT_ACTIVE = "lease_not_active"
LEASE_CODE_RELEASED = "lease_released"


@dataclass(frozen=True)
class LeaseObservation:
    """Controller-owned liveness readback for one lease producer."""

    status: str  # "alive" | "dead" | "unknown"
    detail: str


@dataclass(frozen=True)
class LeaseAuthorityDecision:
    """Admission decision for one gated mutation against one lease."""

    command: str
    task_id: str
    admitted: bool
    code: Optional[str]
    message: str
    authority: str  # "token" | "producer" | "unfenced"


def _lease_refusal(command: str, task_id: str, code: str, message: str) -> LeaseAuthorityDecision:
    return LeaseAuthorityDecision(command, task_id, False, code, message, "none")


def plan_lease_authority(command: str, task_id: str, lease: Any,
                         presented_token_sha256: Optional[str],
                         observation: LeaseObservation,
                         current_pid: Optional[int]) -> LeaseAuthorityDecision:
    """Plan mutation authority for one gated command against one lease.

    Pure decision half of the fencing gate. ``lease`` is the plain task-record
    ``fencing`` mapping (or None). The imperative shell owns token hashing,
    liveness readback, receipts, and state mutation.

    Admission paths, in order:

    1. no ACTIVE lease -> unfenced (legacy or terminal);
    2. exact current token -> holder authority;
    3. process lease whose live producer is this very process -> producer
       continuity (in-process workers and scenario suites).

    Everything else fails closed with one actionable code: a provably dead
    producer names the successor command; a wrong token is a stale fence; an
    unrelated live or unprovable producer demands token/handoff/revoke.
    """
    if not isinstance(lease, dict) or lease.get("state") != LEASE_ACTIVE:
        return LeaseAuthorityDecision(command, task_id, True, None,
                                      "no active fencing lease", "unfenced")
    token_sha256 = lease.get("token_sha256")
    producer_pid = (lease.get("producer") or {}).get("pid") \
        if isinstance(lease.get("producer"), dict) else None
    if (isinstance(token_sha256, str) and isinstance(presented_token_sha256, str)
            and presented_token_sha256 == token_sha256):
        return LeaseAuthorityDecision(command, task_id, True, None,
                                      "exact fencing token", "token")
    if (lease.get("producer_kind") == "process"
            and isinstance(producer_pid, int) and producer_pid == current_pid
            and observation.status == "alive"):
        return LeaseAuthorityDecision(command, task_id, True, None,
                                      "live process producer continuity", "producer")
    if presented_token_sha256 is not None:
        # A presented-but-wrong token is a stale fence regardless of current
        # producer liveness: the caller claims an authority attempt it no
        # longer holds.
        return _lease_refusal(
            command, task_id, LEASE_CODE_FENCE_STALE,
            f"task {task_id} fencing token is stale for attempt {lease.get('attempt')}; "
            "obtain the current token from its holder, an explicit handoff, or "
            f"yy task lease-successor {task_id}")
    if observation.status == "dead":
        return _lease_refusal(
            command, task_id, LEASE_CODE_PRODUCER_DEAD,
            f"task {task_id} holds fencing attempt {lease.get('attempt')} whose producer is "
            f"provably ended ({observation.detail}); obtain a receipt-bound successor with: "
            f"yy task lease-successor {task_id}")
    if observation.status == "alive":
        return _lease_refusal(
            command, task_id, LEASE_CODE_PRODUCER_MISMATCH,
            f"task {task_id} is fenced by another live producer (attempt "
            f"{lease.get('attempt')}, {observation.detail}); present the current "
            "--lease-token, or obtain an explicit handoff or operator revoke")
    return _lease_refusal(
        command, task_id, LEASE_CODE_TOKEN_REQUIRED,
        f"task {task_id} holds fencing attempt {lease.get('attempt')} whose producer cannot be "
        f"proven ended ({observation.detail}); present the current --lease-token, obtain an "
        f"explicit handoff, or recover with: yy task lease-revoke {task_id} --reason <why> && "
        f"yy task lease-successor {task_id}")


@dataclass(frozen=True)
class LeaseSuccessorDecision:
    """Admission decision for issuing one successor fencing attempt."""

    task_id: str
    admitted: bool
    code: Optional[str]
    authority_kind: str  # "successor_death" | "successor_handoff" | "successor_revoke"
    message: str


def plan_lease_successor(task_id: str, lease: Any,
                         observation: LeaseObservation,
                         handoff_receipt: Any) -> LeaseSuccessorDecision:
    """Plan successor issuance for one lease.

    Precondition matrix (expiry alone is never authority):

    - ACTIVE process lease + provably dead producer -> successor_death;
    - HANDED_OFF lease + unconsumed exact handoff receipt -> successor_handoff;
    - REVOKED lease -> successor_revoke (operator already decided);
    - ACTIVE session lease, live/unknown producer, missing or unconsumable
      handoff, or RELEASED lease -> refuse with one actionable code.
    """
    if not isinstance(lease, dict):
        return LeaseSuccessorDecision(task_id, False, LEASE_CODE_NOT_ACTIVE,
                                      "none", "task holds no fencing lease to succeed")
    state = lease.get("state")
    attempt = lease.get("attempt")
    if state == LEASE_ACTIVE:
        if handoff_receipt is not None:
            return LeaseSuccessorDecision(
                task_id, False, LEASE_CODE_PRODUCER_LIVE, "none",
                f"handoff receipt is not consumable while attempt {attempt} is still ACTIVE; "
                "the holder must run lease-handoff first")
        if lease.get("producer_kind") != "process":
            return LeaseSuccessorDecision(
                task_id, False, LEASE_CODE_TOKEN_REQUIRED, "none",
                f"attempt {attempt} is a session lease; producer death is not observable; "
                f"recover with: yy task lease-revoke {task_id} --reason <why>")
        if observation.status == "dead":
            return LeaseSuccessorDecision(
                task_id, True, None, "successor_death",
                f"producer of attempt {attempt} provably ended ({observation.detail})")
        if observation.status == "alive":
            return LeaseSuccessorDecision(
                task_id, False, LEASE_CODE_PRODUCER_LIVE, "none",
                f"producer of attempt {attempt} is alive ({observation.detail}); "
                "expiry alone never grants takeover")
        return LeaseSuccessorDecision(
            task_id, False, LEASE_CODE_PRODUCER_UNKNOWN, "none",
            f"producer of attempt {attempt} cannot be observed ({observation.detail}); "
            f"recover with: yy task lease-revoke {task_id} --reason <why>")
    if state == LEASE_HANDED_OFF:
        if not isinstance(handoff_receipt, dict):
            return LeaseSuccessorDecision(
                task_id, False, LEASE_CODE_TOKEN_REQUIRED, "none",
                f"attempt {attempt} is handed off; consume its exact handoff receipt with "
                "--handoff-receipt <path>")
        return LeaseSuccessorDecision(
            task_id, True, None, "successor_handoff",
            f"explicit handoff receipt for attempt {attempt} accepted")
    if state == LEASE_REVOKED:
        return LeaseSuccessorDecision(
            task_id, True, None, "successor_revoke",
            f"operator revoke of attempt {attempt} authorizes one successor")
    if state == LEASE_RELEASED:
        return LeaseSuccessorDecision(
            task_id, False, LEASE_CODE_RELEASED, "none",
            f"attempt {attempt} was released terminally; mutation proceeds unfenced")
    return LeaseSuccessorDecision(
        task_id, False, LEASE_CODE_NOT_ACTIVE, "none",
        f"unknown fencing lease state {state!r}")
