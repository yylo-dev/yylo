# Native Git merge characterization and deletion budget

Date: 2026-09-11. Frozen source: `2a46ae2e5ca4ce01c4a9cc4e78097bd2ff58cd57` (tree `e45fa26940f35326e616af3b8df893437486dbf0`). Git: 2.43.0.

## Decision

Retain a **small one-task adapter**, capped at **800 authored physical production lines**, rather than retaining the queue or exposing only a manual command sequence. The adapter exists for one concrete gap: an autonomous task handoff needs to resolve one immutable source commit, compose it privately, atomically update one protected ref against its observed old SHA, and report the Git result before separately projecting Ledger state. Git supplies composition, conflict preservation, ancestry, and compare-and-swap semantics; the adapter must not reproduce them.

The selected topology is a **private detached candidate plus `git update-ref <target> <candidate> <expected-old>` against an unchecked-out target ref**. A registered integration-owner checkout is synchronization/maintenance state, not the ref-mutation engine. The adapter must refuse if the configured target ref is checked out where an update would bypass a worktree index. There will not be a second checked-out-target implementation.

Tests and reviews remain explicit project checks outside merge. Merge launches zero models and owns no risk routing, repair, test scheduler, evidence cache, or FIFO. Declared task dependencies remain Ledger/project policy: Git intentionally does not infer semantic dependency from paths.

## Executable observations

`juno-code/scripts/test-performance/native-git-merge-characterization.mjs` creates disposable real repositories. Its matching Vitest test requires all 11 cases to pass:

| Case | Native observation |
| --- | --- |
| direct fast-forward | expected-old ref update lands the descendant exactly |
| divergent clean merge | native merge creates two parents and preserves both sides |
| same-file disjoint hunks | native merge preserves both edits |
| conflict X, unrelated Y | X's private conflict remains present while Y composes and lands; FIFO wait attributable to X is zero |
| competing updates | one expected-old update wins and the stale writer is rejected without lost commits |
| target moves during validation | stale validated candidate is rejected and requires recomposition/revalidation |
| dirty source checkout | committed source is composed elsewhere; uncommitted status and 20 payload bytes remain exact |
| already-contained source | ancestry check reports success without another integration commit |
| crash before update | target remains unchanged and private candidate remains inspectable |
| Git success, Ledger failure | target remains landed; projection can retry without duplicate Git integration |
| submodule gitlink | native merge preserves the source gitlink alongside an unrelated target change |

The harness records fixture wall time only to make execution transparent. It does not claim delivery latency, throughput, p99, production interventions, or test/review savings. Harness overhead and project checks are outside Git integration time.

## Frozen production closure

Counts are authored physical lines from the frozen source, counted once. Byte-identical installed/template twins are listed separately and are **not** added to the denominator.

### Direct merge-only denominator

| Authored source | Lines | Disposition |
| --- | ---: | --- |
| `.juno_task/scripts/merge_queue.py` | 8,687 | replace with the one-task adapter |
| `juno-code/src/cli/commands/merge.ts` | 353 | replace broad command plumbing with the small contract |
| `juno-code/src/templates/workflows/yy-merge-drive.yaml` | 1 | delete |
| **Direct merge-only production total** | **9,041** | baseline denominator |

The 800-line adapter cap yields at least `(9041-800)/9041 = 91.15%` deletion if fully used. Final measurement must include every retained/new merge-only helper and CLI/result line, wherever placed. Zero engine remains permissible if implementation proves the adapter unnecessary. The 99% stretch requires at most 90 retained lines and is not presumed.

Packaged/generated twins, counted separately: `juno-code/src/templates/scripts/merge_queue.py` (8,687 lines, byte-identical), installed `.juno_task/scripts/merge_queue.py`, runtime/template merge tests, managed manifests, prompts, wiki, and controller-agent instructions. Generated duplication is migration scope, not authored savings.

### Imported and calling closure

These files are in the complete runtime/caller closure. Their whole-file line counts are inventory, **not** merge-only denominator, because each has supported non-merge consumers. The replacement task must delete only the named merge call sites/functions and count any retained merge-specific residue in its final numerator.

| Authored source | Whole-file lines | Merge relationship |
| --- | ---: | --- |
| `.juno_task/scripts/task_workspace.py` | 8,305 | submission/handoff, task records, finalization |
| `.juno_task/scripts/integration_workspace.py` | 2,322 | candidate/owner synchronization callers |
| `.juno_task/scripts/risk_policy.py` | 1,692 | merge review/full-suite policy and shared policy |
| `.juno_task/scripts/task_workflow_helper.py` | 2,525 | merge-drive journals and shared workflow support |
| `.juno_task/scripts/operation_snapshot.py` | 424 | operation identity shared with task submission |
| `.juno_task/scripts/task_workspace_decisions.py` | 866 | arbiter/task decision vocabulary |
| `juno-code/src/utils/control-plane-router.ts` | 133 | shared routing imported by CLI adapter |
| `juno-code/src/utils/controller-checkpoint.ts` | 94 | post-finalization checkpoint imported by old adapter |

Canonical template twins of the Python files have the same counts and are not double-counted. `juno-code/src/bin/yylo.sh`, script installer, managed-assets declarations, workspace topology, task CLI callers, and package acceptance tests are indirect installation/routing consumers; they own references rather than a second merge engine.

## Old orchestration inventory

The old CLI exposes 17 top-level command nodes plus nested arbiter/reconcile/refresh operations. The engine has 11 visible lifecycle states, target-wide FIFO/prefix authority, candidate and risk records, drive/scope journals, arbiter fences, review/repair receipts, full-suite claims/caches, refresh/reopen/reconciliation/supersession recovery, owner synchronization, and post-CAS Ledger finalization. It imports five local runtimes and can launch model review/repair under risk policy.

The previous acceptance report remains `NEEDS_DECISION`: candidate coordination interventions were missing. Its 10.95 weighted baseline is not an elapsed-time result and is not evidence for a 70–80% saving. For this characterization, existing operator interventions, ready-to-integrated time, unrelated blocked time, and matched native-Git overhead remain **unknown** except for the deterministic fixture facts above. Missing data is not zero.

## Deletion contract for the serial follow-ups

Delete FIFO ordering/prefix snapshots, drive and arbiter journals, model review/repair, merge-owned validation and caches, authority sidecars coupled to unrelated queue members, target-refresh/reopen/supersession families, release specialization, and mandatory post-CAS owner hydration/Ledger completion. Preserve historical receipts as inert data and preserve every dirty/conflicted checkout.

Retained production behavior must be only:

1. resolve exactly one task and immutable source commit;
2. observe one unchecked-out destination ref;
3. compose in one private candidate with native Git;
4. optionally consume an explicit exact-candidate project check result;
5. update the ref once with expected-old protection;
6. report Git success immediately, then attempt an independent revision-checked Ledger projection;
7. return concise conflict, already-contained, projection-pending, or target-moved results with no unbounded retry.

A target move invalidates candidate checks. A projection retry does not repeat Git integration. A conflicting task cannot authorize or block an unrelated task. There is no compatibility execution path for the retired queue after the explicit migration inventory/cutover.

## Replacement result

The final one-task replacement retains 304 authored adapter lines, 94 authored TypeScript CLI lines, and 18 lines for the integration-maintenance lock moved out of merge: **416 retained/new production lines total**. Against the frozen 9,041-line denominator, this deletes **8,625 lines (95.40%)** without counting runtime/template twins twice. It is 384 lines below the 800-line cap. The separately measured 99% stretch would require at most 90 retained lines and was not reached. The adapter has three public commands (`status`, `land`, `project`), three active delivery states (`QUEUED`, `CONFLICT`, `GIT_INTEGRATED` before terminal `MERGED`), one task-state write after Git, and one separately retriable Ledger write. Merge-owned model calls are zero.

The replacement removed FIFO selection, arbiter/drive/resume journals, reviews, repair, risk routing, validation scheduling/cache, queue-tail authority, target refresh, reopen, reconciliation, supersession, and owner-checkout synchronization from the runtime. Focused real-Git tests cover clean divergent delivery, private conflict X plus unrelated Y, competing expected-old updates, target movement, dirty bytes, already-contained retry, attached-target refusal, and Git-success/Ledger-failure. Historical receipts are not read or deleted.

This is a source-line deletion result, not an elapsed-time claim. Operator interventions, production ready-to-integrated time, and matched project-check overhead remain unknown until observed after activation.

## Packed-package acceptance

The final source checkout ran `npm run test:managed-assets`, `npm run
test:installed-test-fixture-package`, `npm run test:fresh-init-package`, and `npm
run test:bolt-package-canary`. The canary installed the real packed tarball and
reported 14 selected tests, 7 expected refusals, 2 refused retired entrypoints,
15,063 ms elapsed, and zero model calls, agent tool calls, failed agent calls,
reviewer sessions, controller checkpoints, or tokens. The separately run 11-case
native-Git fixture passed in 3,142.10 ms summed scenario wall time. These are
local fixture timings only, not production latency claims.

The runtime surface has four delivery states (`QUEUED`, `CONFLICT`,
`GIT_INTEGRATED`, `MERGED`). `land` performs one expected-old Git ref write and
then one task-state write recording Git truth; `project` performs one separately
retryable revision-checked Ledger write. Conflict, attached target, stale target,
and crash-before-update cases perform no target-ref or Ledger write. Every
acceptance command launched zero models.

## Reproduction

```bash
cd juno-code
node scripts/test-performance/native-git-merge-characterization.mjs
npm test -- src/utils/__tests__/native-git-merge-characterization.test.ts
```

The commands use disposable `/tmp` repositories, launch no model, and do not touch the live queue, protected target, Ledger, network, release, or deployment state.
