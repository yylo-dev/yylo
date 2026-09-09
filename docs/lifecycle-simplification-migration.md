# Lifecycle simplification migration

This contract retires umbrella execution from the ordinary task interface while
preserving existing attempts and immutable evidence. New work uses one ordinary
Ledger task, one exact worktree, optional ordered delivery checkpoints, one
submission, and one queue-owned review/integration boundary.

## Supported surface

Normal delivery uses `yy task start|status|checkpoint|preflight|finish` and
`yy merge status|arbiter|drive`. `preflight` is optional and read-only; `finish`
enforces the same closure. Reporting task relationships do not own worktrees,
leases, candidates, reviews, or integration.

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
| ordinary `WORKING` or hydration/repair state | Keep its frozen runtime and active producer; drain normally or use receipt-bound fenced handoff |
| ordinary `QUEUED`, risk pending, or conflict | Preserve FIFO, candidate, review and conflict evidence; queue recovery remains the only owner |
| landed with pending finalization | Resume finalization from the recorded successful CAS; never integrate again |
| terminal, withdrawn, or historical | Read only; retain receipts and task identity |
| dirty attempt | Preserve every byte and refuse conversion until the owner supplies an authorized clean boundary or replacement |
| unconverted legacy umbrella already `WORKING` | Eligible only for the exact plan/authorization/apply flow below |
| converted legacy umbrella | Verify or continue as one ordinary delivery with reporting-only child IDs |
| unknown ownership, schema, runtime, target, or evidence | Refuse without mutation |

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

## Runtime and authority boundary

An active producer remains pinned to the runtime recorded at task start. Do not
replace its engine in place. Drain it or obtain the explicit fenced handoff or
successor receipt before activation. Runtime installation and controller refresh
remain maintenance operations, not post-CAS task delivery.

Conversion never rewrites the creation receipt, worktree, commits, review
receipts, queue evidence, or Ledger history. Push, tag, publication, deployment,
live migration, cleanup and release activation remain external maintainer
actions.

## Retirement condition

`legacy-lifecycle` is a finite reader/adapter for already-recorded umbrella
schemas. It can be removed only when an owner-proved inventory contains no
unconverted active umbrella attempt and retention policy no longer requires an
executable reader. Historical files remain immutable even after command
retirement. Ordinary task and merge commands are the sole supported execution
surface throughout the transition.
