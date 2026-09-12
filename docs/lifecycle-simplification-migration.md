# Lifecycle simplification migration

This contract retires umbrella execution from the ordinary task interface while
preserving existing attempts and immutable evidence. New work uses one ordinary
Ledger task, one exact worktree, optional ordered delivery checkpoints, one
submission, and one selected-task native-Git delivery boundary.

## Supported surface

Normal delivery uses `yy task start|status|checkpoint|preflight|finish` and
`yy merge status|land|project`. `preflight` is optional and read-only; `finish`
enforces the same closure. `land` selects one immutable task and uses native Git;
`project` records Git success separately. Reporting relationships do not own
worktrees, candidates, tests, reviews, or integration.

No `yy task` command creates or advances an umbrella. Finite legacy handling is
isolated under `yy migrate legacy-lifecycle`; it calls the same managed task
runtime and does not introduce another state store or executor.

## Read-only inventory and disposition

Before any conversion, run `yy migrate legacy-lifecycle inventory [TASK_ID]`,
preserve the controller's exact bytes, and inspect task/merge status for every
reported active record. Classify every record rather than treating absence from
the active queue as permission to mutate it:

| Observed state | Disposition |
| --- | --- |
| ordinary `WORKING` or hydration/repair state | Preserve its worktree, source commit, dirty bytes, runtime and owner; finish or use a receipt-bound fenced handoff before native delivery |
| legacy `QUEUED` | Preserve the immutable source and queue receipt; map only that task to `yy merge land TASK_ID` after the retired writer is stopped |
| legacy reviewed/risk-pending | Preserve review/risk evidence as historical data; it grants no native-delivery test/review claim; land only after explicit current checks |
| legacy `CONFLICT` | Preserve the dirty candidate and every byte; do not import it automatically; owner either resolves to an explicit commit or keeps it blocked while unrelated tasks land |
| post-CAS/finalization pending | Verify source ancestry and exact target readback, map to `GIT_INTEGRATED`, and run only `yy merge project TASK_ID`; never integrate again |
| terminal, withdrawn, or historical | Read only; retain receipts and task identity; expose no executable old command |
| dirty attempt | Preserve every byte and refuse conversion until the owner supplies an authorized clean boundary or replacement |
| unconverted legacy umbrella already `WORKING` | Eligible only for the exact plan/authorization/apply flow below |
| converted legacy umbrella | Verify or continue as one ordinary delivery with reporting-only child IDs |
| unknown ownership, schema, runtime, target, or evidence | Refuse without mutation and request an owner disposition |

New umbrella starts are unsupported. Existing unconverted attempts may drain an
already-admitted child checkpoint with `legacy-lifecycle checkpoint`; this never
starts a child worktree or grants child lifecycle authority.

## Exact conversion

The conversion is intended for disposable fixtures and explicitly authorized
legacy drains. Live apply requires separate owner and release coordination.

```bash
yy migrate legacy-lifecycle plan TASK_ID \
  --umbrella-admission /absolute/frozen-input.json \
  --output /external/reviewed-plan.json

yy migrate legacy-lifecycle authorize TASK_ID \
  --umbrella-admission /absolute/frozen-input.json \
  --plan /external/reviewed-plan.json

yy migrate legacy-lifecycle apply TASK_ID \
  --umbrella-admission /absolute/frozen-input.json \
  --plan /external/reviewed-plan.json \
  --authorization-receipt /controller/.juno_task/receipts/task-admission-authorizations/RECEIPT.json

yy migrate legacy-lifecycle verify TASK_ID \
  --umbrella-admission /absolute/frozen-input.json \
  --plan /external/reviewed-plan.json \
  --authorization-receipt /controller/.juno_task/receipts/task-admission-authorizations/RECEIPT.json
```

Plan and verify are read-only. Apply is idempotent for the same exact plan and
authorization. The runtime rechecks the task body, target/base/ref/worktree,
clean committed history including every merge parent edge, generated bindings,
child revisions/scopes, authorization ledger, predecessor receipt and current
runtime. Stale or ambiguous inputs fail closed.

Verification binds the immutable plan and authorization to the supersession,
ordinary checkpoint contract, reporting ownership, preserved changed paths and
full prior commit history. It writes no controller state.

## One-time cutover and authority boundary

Inventory `WORKING`, queued, reviewed, conflicted, and post-CAS-pending records
before activating native delivery. Stop or hand off every old producer, record
the disposition from the table above, then verify no retired workflow/arbiter
entry point is installed. Never run old and new writers concurrently.

An active producer remains pinned to the runtime recorded at task start. Do not
replace its engine in place. Drain it or obtain the explicit fenced handoff or
successor receipt before activation. Runtime installation and controller refresh
remain maintenance operations, not post-CAS task delivery.

Conversion never rewrites the creation receipt, worktree, commits, review
receipts, queue evidence, or Ledger history. Push, tag, publication, deployment,
live migration, cleanup and release activation remain external maintainer
actions.

## Bounded terminal lifecycle state

Full active records and shared ownership remain hot. Complete `MERGED` and
`WITHDRAWN` records may move to bounded compressed packs on the opt-in
`refs/juno/cold/task-state` ref only after exact publication and readback; hot
state retains digest-bound tombstones. The target is 5 MB, warning threshold is
8 MB, and ordinary writes refuse above 25 MB.

Migration is explicit and one-cut:

```bash
yy task state-archive-plan --output /external/state-plan.json
yy task state-archive-apply --plan /external/state-plan.json \
  --output /external/state-apply.json --authorize-state-compaction
yy task state-archive-verify --plan /external/state-plan.json
yy task state-archive-get TASK_ID
yy task state-archive-rollback --plan /external/state-plan.json \
  --output /external/state-rollback.json --authorize-state-rollback
```

Plan and receipts must remain outside the repository. Apply requires a clean,
unchanged controller and publishes cold evidence before replacing hot records.
Rollback restores the exact committed preimage and preserves the cold ref.
Older runtimes refuse the bounded schema; there is no permanent dual writer.
Existing Git history is not rewritten, so an initial push containing old
unpublished large blobs may still emit historical GH001 warnings. Subsequent
checkpoint blobs must remain below the documented bounds.

## Retirement condition

`legacy-lifecycle` is a finite reader/adapter for already-recorded umbrella
schemas. It can be removed only when an owner-proved inventory contains no
unconverted active umbrella attempt and retention policy no longer requires an
executable reader. Historical files remain immutable even after command
retirement. Ordinary task commands and `yy merge status|land|project` are the sole supported
execution surface throughout the transition.
