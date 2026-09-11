---
wiki_contract:
  line_limit: 140
  purpose: "Stable post-run review contract for parallel-runner/subagent work, spec-invariant review, submodule commits, and write-capable CLI acceptance."
  failure_mode_prevented: "Root agents marking mixed, partial, dirty, or semantically mismatched subagent work as done."
  runtime_contract_enforced: "Kanban task status, git commits, task MUST/MUST NOT requirements, tests, and dangerous-path evidence agree before root work is closed."
  validation_gate: "wiki_root=$(yy wiki --path 2>/dev/null || true); wiki_file=$wiki_root/controller/parallel_runner_and_spec_review.md; test -f \"$wiki_file\" || wiki_file=.juno_task/wiki/parallel_runner_and_spec_review.md; ./.juno_task/scripts/wiki_lint.sh --file \"$wiki_file\""
  related_sots:
    - "git_worktree_lifecycle.md"
    - "runtime_migration_and_replacement_contract.md"
  owns:
    - "Post-parallel-run/spec-invariant review checklist."
    - "Requirement matrix review expectations."
    - "Write-capable CLI acceptance categories."
  does_not_own:
    - "How to author parallel-runner task bodies; use parallel_runner_task_creation_best_practices.md."
    - "Structured task contract schema; use task_contract_schema.md."
    - "Production/tmux launch mechanics and artifacts; use production_job_and_telemetry_workflows.md and tmux_best_practices.md."
    - "Task-specific evidence, run IDs, logs, or incident details; store them in kanban responses or specs."
---

# Parallel Runner + Spec-Invariant Review
Use this guide after `./.juno_task/scripts/parallel_runner.sh` finishes, before accepting subagent work, and before marking root work done for production jobs, write-capable CLIs, submodules, or multi-task implementation batches. Project-local task-authoring/schema guides may specialize those concerns without becoming package-managed files. For isolated checkout integration and cleanup, see [`git_worktree_lifecycle.md`](git_worktree_lifecycle.md); for replacement inventories, limit provenance, feasibility, cleanup ledgers, and validation ownership, see [`runtime_migration_and_replacement_contract.md`](runtime_migration_and_replacement_contract.md).

## Workflow and task-lifecycle guidance ownership

Workflow Runner owns generic ordered workflows only. Product implementation uses `yy task`; serialized delivery uses `yy merge`; `git_worktree_lifecycle.md` is their semantic source. Historical `workflow_class: local_integration` artifacts remain doctor-readable but cannot lint/start/resume/recover/amend. This page owns only post-run/spec review shared with Parallel Runner.

## Why this matters
Parallel/subagent runs can finish with mixed outcomes: failed tasks may leave useful diffs, successful tasks may miss a MUST/MUST NOT, and submodules may be committed without the parent pointer. A reviewer must independently prove that the implementation matches the selected kanban/spec contract.

Failure mode prevented: partial subagent work, dirty submodules, missing parent-pointer commits, semantic drift, fallback/shim/SOT expansion, or untested write paths being marked done.
Runtime contract enforced: task status, git commits, tests, acceptance evidence, and task requirements agree before closure.
Exact validation gate: runner logs inspected; root/submodule `git status` clean or explained; selected kanban tasks checked; independent requirement matrix completed; relevant tests and dangerous-path checks pass.
Why tests/backing implementation matter: runner summaries are evidence only; tests and code inspection prove behavior satisfies the contract.
## Review flow

```text
parallel_runner_wait.sh returns
  -> build independent acceptance matrix from user request, task bodies, specs, and wiki contracts
  -> inspect runner aggregation/logs and failed task logs
  -> inspect root and submodule diffs/status
  -> salvage, revert, or rerun partial edits explicitly
  -> compare matrix against code/tests/artifacts
  -> persist findings without changing product or Kanban
  -> wait for every required independent reviewer
  -> terminal orchestrator consolidates one repair packet
  -> separate repair owner runs targeted tests and dangerous-path checks
  -> commit submodules first, then parent pointer
  -> terminal owner runs finalize-review after workflow completion
  -> human reviewer accepts/rejects against the independent matrix
  -> integrate reviewed tip into the approved target and validate that target
  -> hand off to separately authorized deployment/E2E, then clean worktrees per lifecycle SOT
  -> mark tasks done only with validation + commit evidence
```

Do not use the implementation summary as the checklist. Read it after the matrix exists so the review does not inherit the same blind spots.

**Cheap admission and composition boundary:** Before expensive project checks, task admission evaluates state, fencing, declared dependencies, runtime/policy/package identity, blob origins, exact parity, locks, routing, and evidence eligibility through `juno_path_origin_projection.v1`. Native delivery later selects one immutable task; target movement requires recomposition and renewed exact-candidate checks.
**Independent reviewer boundary:** Review only. An independent reviewer never edits, commits, updates Kanban, launches another reviewer, repairs findings, or mutates refs/worktrees. Low risk has no semantic reviewer; normal risk has at most one; high-risk Reviewer A and Reviewer B run sequentially against one frozen base/tip. A replacement tip invalidates prior review evidence.

**Exhaustive finding and disposition boundary:** Every explicitly selected reviewer inspects the complete frozen change and returns all independently actionable supported findings up to 32, with explicit truncation truth. The project review owner—not merge—verifies structured responses, normalizes severity against the selected policy, deduplicates root causes, and decides repair. Malformed or truncated evidence is not acceptance. Reviewer B, when project policy requires it, remains blind to Reviewer A conclusions.

**Managed launcher boundary:** Bolt worker/reviewer dispatch and typed Workflow Runner `managed_agent` steps use `managed_agent_runner.py run --mode worker|reviewer`. It alone owns fresh `yy pi`, prompt/capture/response receipts, foreground process groups, and live separate/labelled channel logs. Tmux may observe those files but never owns or detaches the producer. Parallel Runner remains a generic batch tool, not a task-lifecycle owner.

**Reviewer launcher identity:** Launch every project subagent and independent reviewer through `yy pi`. Inherited project provider/model defaults are always permitted; an ordinary explicit selector is permitted only on exact project `workflowModels` membership (`--provider P --model M` is normalized to `P/M`, without alias expansion). Bare `pi`, direct agent/provider CLIs, provider-only selection, and inline-env, additional-args, or alternate-config overrides bypass Juno policy and are forbidden. Workflow lint applies this policy to steps, summary, pre-merge/candidate review, and nested actual-target review. Run/recovery evidence binds config identity/hash, allowlist hash, and normalized selection so policy drift fails closed. Failure mode prevented: a green review has irreproducible launch identity. Runtime contract: context separation does not change launcher ownership.
## Batch E2E runner protocol

Before launching an E2E kanban batch, treat tags as candidate signals, not proof. Build a preflight table with ID/status/title/tags/last_modified and include/exclude reason. Include tasks with an E2E-related tag when title/body clearly describes verification, post-deploy validation, acceptance, or production readout. Auto-exclude ambiguous implementation follow-ups, telemetry-building tasks, and tasks where E2E is only future support. Capture the batch start timestamp and selected ordered IDs before launching `nohup ./.juno_task/scripts/parallel_runner.sh ... &`. After completion, use the start/end timestamp for new-bug detection and report runner OK/failed separately from E2E done/open.

Failure mode prevented: wasted runner slots, ambiguous non-E2E execution, missed new bugs, and runner success being mistaken for E2E pass. Runtime contract enforced: launched IDs are true verification tasks, new bugs are timestamp-attributed, and final reports separate runner health from kanban/E2E outcome. Exact validation gate: preflight table plus start timestamp exists before launch; final summary lists selected IDs, auto-excluded IDs/reasons, runner failures, done E2Es, open E2Es, and timestamp-created/modified bug tasks.

## Runner outcome versus semantic outcome

Before launch, declare each selected task's expected terminal state (`done`, intentionally blocked/todo, or another explicit state) plus required verdict/artifact. After every task step, manually verify Kanban state, blockers, response verdict, and artifact until automation is approved. Agent exit 0 and runner success are transport outcomes, not semantic acceptance; final reporting must show both.

Assign one owner for each expensive production execution. Implementation owns targeted tests; the execution owner writes durable evidence; independent/root review consumes it and reruns the expensive job only when evidence is missing, stale, contradictory, or explicitly required. Focused tests are the edit loop; a full suite runs once at each review-ready candidate boundary. Long work belongs in a durable run root with launch/status/result/review phases and one bounded wait/result operation, not repeated model-driven sleep/tail polling.

Overfit guard: expected terminal states are task-configured, not globally `done`; reruns remain mandatory for missing or unsafe evidence. This section approves instruction/manual gates only, not a new helper.

## Acceptance reuse and smallest-stage resume
Build the requirement matrix independently, but do not automatically rerun a completed expensive gate. Terminal evidence is reusable when it identifies implementation/source/config scope and proves driver exit, semantic acceptance, reconciliation, safety/privacy, cleanup, and artifact disposition. Root review then inspects that evidence and runs only a targeted high-risk smoke unless evidence is missing, stale, failed, or contradicted.
Before retrying, classify the failure: product/data defect, harness portability, environment/configuration, or runner/process interruption. Resume the smallest failed stage. A harness or environment fix does not invalidate earlier implementation tests/source artifacts unless scope changed; a process interruption does not justify a new end-to-end workflow. Always report runner health separately from semantic outcome.
For an unchanged same-run contract, use `--from-step`. For a corrected harness contract, create a fresh output directory with `amendment_mode: harness_only_validation` and `--amends-run PRIOR_RUN`; combine it with `--from-step STEP` only when the runner should revalidate/import the successful prefix and execute the suffix. The pre-dispatch check binds the newest prior manifest to its hash, exact completed attempt, command/template/input/variable identity, original producer-attempt lineage, and receipt bytes/contracts. Only a receipt path relocation may differ. The console and `manifest.json.amendment_plan` name reused versus executed steps, and reused prefix entries are `amendment_revalidated`. Any stale, failed, missing, tampered, ambiguous, added, removed, reassigned, or weakened evidence blocks dispatch; never repair history in place.
Failure mode prevented: a successful semantic gate is repeated until timeout, or a small harness/config failure launches a new full workflow. Runtime contract enforced: one owner performs full semantic acceptance and downstream reviewers reuse complete terminal evidence with targeted smoke. Exact validation gate: review packet names failure class, resumed stage, terminal evidence fields, reuse/execute plan, and why any repeated full gate was necessary. Why tests/backing implementation matter: evidence reuse is safe only when implementation/source identity and semantic reconciliation are machine-checkable.

## Required checklist

1. Inspect latest runner output: failed IDs, task log paths, and aggregation JSON if present.
2. For every failed task, read its log and decide whether any partial edits are salvaged, reverted, or rerun.
3. Check working trees: root `git status --short`; for changed submodules also check `git status --short`, `git log --oneline -n 3`, and parent pointer consistency.
   If any task used an isolated checkout, run the repository-wide inventory from [`git_worktree_lifecycle.md`](git_worktree_lifecycle.md), record every auxiliary disposition, and block closure on dirty, unintegrated, divergent, missing-target, or unexplained worktrees.
4. Build a compact matrix:
   - source rows from selected kanban task bodies, parent specs, MUST/MUST NOT, path inventory, AGENTS.md, and stable wiki contracts;
   - require code/test evidence for each affected path/function and for every triggered stable contract (auth/proxy identity, billing, paywall, write paths, public APIs, etc.);
   - for write-capable wrappers, include downstream invariants before approval: child CLI arg constraints, DB column types, resume/idempotency semantics, terminal artifacts, and operator telemetry semantics;
   - verify task responses do not claim untested behavior;
   - treat new env flags, statuses, queue/write paths, replay/deferred state, migrations/adapters/fallbacks, split identity params, or SOT-like behavior as mismatches unless explicitly allowed.
5. Map evidence to gates before accepting validation: list material dimensions such as browser/device class, host/domain, auth mode, source/cache dimension, or write/read mode; prove each is covered or record why a gap is accepted; prefer task-scoped harnesses over broad global config changes when unrelated suites would inherit behavior.
6. Check single-SOT cleanup when a validation harness is added/replaced: remove or consolidate stale duplicate route, endpoint, query, job-name, or invariant lists; if duplicates remain, name the owner and drift-prevention rule.
7. For touched public API routes, inspect route surface before approval: method/path, generated body/query params, auth dependencies, response model/envelope, and OpenAPI-impacting handler signature. Internal reuse must call private helpers, not add client-visible handler params.
8. If a reviewer finds a bug or mismatch, record a read-only finding with evidence and acceptance conditions. Wait for all required reviewers. The terminal orchestrator then creates/reopens Kanban bugs where required and emits one consolidated repair packet; the reviewer never performs those mutations. Use `--body-file` for multiline markdown, commands, `$`, pipes, or code fences.
9. A separate repair owner runs task-specific validation and records exact output in the Kanban response or artifact.
10. For submodules, commit/push inside the submodule first, then commit the parent submodule SHA. Prove `git ls-tree HEAD <submodule_path>` matches the submodule `git rev-parse HEAD`.
    For ordinary worktree branches, the integration owner must fetch the approved target, integrate only after independent review, prove task-tip ancestry (or record and verify an approved squash replacement), run integrated-target validation, and permit deployment/E2E only afterward. Remove worktrees and task branches only through the lifecycle cleanup gate.
11. For workflow runs, the currently running review step MUST NOT run terminal doctor on its own incomplete workflow. After completion, the named terminal owner runs `task_workflow_helper.py finalize-review <run_dir> --manifest <task-set.json>`, then inspects persisted doctor/packet/receipt evidence and records a human verdict separate from runner success. If timeout/signal leaves no terminal manifest, classify it as interruption, preserve step artifacts, and resume only the smallest invalid stage; do not call missing terminal evidence a completed run. Final review reconciles every machine-declared expected output and every review-created bug/amendment, not only original task IDs. Retain artifacts under the declared spec/run root until the disposition owner applies the recorded retention/archive rule.
12. Use `kanban.sh search --tag <E2E_TAG> --format table` for human E2E tag isolation. Do not assume default Kanban output is JSON unless an explicit JSON format flag is supported and used; for JSON, write `kanban.sh get TASK -f json` to a temp file before parsing to avoid broken-pipe false failures.
13. Classify dirty tree before accepting: `feature-owned`, `pre-existing/unrelated`, `runner artifact`, `generated build artifact`, or `submodule pointer`; unresolved feature-owned or unexplained submodule drift blocks closure. Do not hand-claim root/submodule clean without fresh `git status --short` evidence; if a response/packet contradicts actual status, create or reopen a Kanban drift task before parent closure.
14. If a subagent response or artifact says a post-run diagnostic could not run, root review must rerun the exact command when possible and correct stale evidence before final reporting.

Forbidden-term grep for SOT/semantic expansion reviews:

```bash
git diff <base>..<head> -- <paths> | rg -n "deferred|replay|fallback|adapter|migration|compat|pending|synthetic|inferred|shim|SOT|source of truth|validation_.*id|proxy_.*id"
```

Failure mode prevented: reviewers accepting tests that encode a shortcut while the implementation changes identity, billing, paywall, write-path, or public-API contracts. Runtime contract enforced: green tests are accepted only when code preserves triggered stable contracts or the owner explicitly approved the contract change. Exact validation gate: review matrix includes contract rows plus grep/diff evidence for high-risk identity/SOT changes. Why tests/backing implementation matter: behavior tests without backing-contract checks can bless the wrong implementation.

## Review packets and helpers

Use `.juno_task/scripts/kanban_review_packet.py TASK_ID [--base BASE --head HEAD] [--format json]` when helpful. For workflow batches, use `task_workflow_helper.py workflow-review-packet <run_dir> --tasks A,B --e2e-tag TAG --manifest TASK_SET --output PACKET`: stdout stays concise while full evidence persists. Optional manifest `owned_paths` and `baseline_dirty_paths` classify expected dirt without hiding `unexpected`; any unexpected path blocks or needs an explicit classification. Verify cross-task status/commit/tag/response claims with `verify-mutation-claims --output RECEIPT`; read-after-write receipts are structural evidence only.

Minimum evidence: true dependency edges, task id/commit/response, changed line ranges, tests, mutation receipts, scoped dirty tree, artifact disposition, and submodule pointers. Approval still requires independent semantic judgment. Workflow/task creation never grants push/deploy authority; the matrix must name repository/runtime scope, semantic owner, and separately authorized deploy owner.

Failure mode prevented: reviewers missing a changed path, unverified claim, dirty submodule, workflow step failure, E2E tag leakage, or parent-pointer mismatch.
Runtime contract enforced: every reviewed task traces to exact code lines, task response, artifacts, workflow evidence, and validation evidence.
Exact validation gate: packet or equivalent evidence plus reviewer-authored matrix is attached to the review artifact or summarized in kanban.

## Public API route-surface review
For auth, payment, admin, and other public backend routes changed by a batch, verify the framework-generated route contract, not only function behavior: body/query params, dependencies, response model, and OpenAPI-facing signature. Unexpected auth/user/session body/query fields are mismatches unless explicitly approved.
Failure mode prevented: internal helper params becoming client inputs. Runtime contract enforced: public route truth comes from declared inputs and server dependencies; internal reuse stays private. Exact validation gate: route-surface assertion plus matrix row for method/path/body/query/auth/response.

## Write-capable CLI review
For CLIs or jobs that can write production data, verify dangerous operator paths before closure. A project-local `task_contract_schema.md`, when present, may define structured contract fields. Create deploy/E2E tasks only after this review fixes final commit/tag, or mark expected commit/tag as `TBD pending spec-invariant review` and update it before running deploy/E2E.

Minimum evidence to record: dry-run writes zero rows; authorized write path requires the named approval/control and writes only the approved target; resume existing skips completed work; repeated run-id without resume cannot duplicate completed work; partial existing output hard-stops before more writes; failed child/retry behavior is observable and retry-safe; wrapper-generated IDs/paths satisfy downstream CLI and DB schemas; no unintended DDL, latest/live mutation, fallback, adapter, replay, or alternate SOT when forbidden.

## What belongs here

Keep this page process-wide. Put domain metrics, SQL predicates, production run IDs, customer incidents, one-off logs, and project-local task-authoring conventions in Kanban responses, specs, or unowned local wikis.
