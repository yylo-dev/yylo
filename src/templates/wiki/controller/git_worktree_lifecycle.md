---
wiki_contract:
  line_limit: 220
  purpose: "Run exact-base task worktrees and land one selected task with native Git."
  failure_mode_prevented: "Controller edits, stale-worker takeover, FIFO blocking, hidden model review, and unsafe target movement."
  runtime_contract_enforced: "yy task owns feature worktrees; yy merge selects one task, uses native Git composition and expected-old ref update, then projects Ledger separately."
  validation_gate: "python3 .juno_task/scripts/tests/test_task_workspace.py && python3 .juno_task/scripts/tests/test_merge_queue.py"
  related_sots:
    - "controller/fenced_task_leases.md"
---

# Task worktrees and native Git delivery

The controller is a metadata store. Product edits happen only in exact-base task
worktrees. Delivery selects one immutable task source; there is no FIFO queue
drive, target arbiter, merge-owned test/review/model loop, or automatic repair.

```text
yy task start X -> edit/test/commit -> preflight -> finish -> QUEUED
                                                        |
yy merge land X -> private native Git candidate -> expected-old update
                                                        |
                                  Git result -> yy merge project X -> Ledger
```

## Public commands

```text
yy task start TASK_ID
yy task status TASK_ID
yy task admission TASK_ID
yy task preflight TASK_ID
yy task finish TASK_ID

yy merge status [TASK_ID]       # read-only independent task status
yy merge land TASK_ID           # compose and land one task; no tests/reviews/models
yy merge project TASK_ID        # retry the separate Ledger projection
```

`task start` freezes the target and runs exact-lock hydration before reporting
`WORKING`. In the returned worktree, follow
[task dependency hydration](task_dependency_hydration.md) before editing or
testing and stop before implementation on failure. `preflight` is read-only;
`finish` admits the clean immutable source after configured task validation.

`merge land` resolves only the selected task. It composes in a private detached
candidate and updates an unchecked-out target with Git expected-old protection.
If the target is checked out, delivery refuses rather than changing its ref
behind an index/worktree. If the target moves, recompose and rerun checks for the
new candidate; there is no unbounded retry. A conflict remains private to that
task and does not block an unrelated task.

Tests and semantic reviews are explicit project policy outside merge. The merge
adapter launches zero models, selects no reviewer, schedules no suite, and owns
no evidence cache. A native Git source already contained in target is ancestry
evidence only; it is not a claim that tests, review, or requirements passed.

Git success is reported before Ledger projection. A failed `merge project`
leaves the Git result intact and retryable, never repeats integration, and never
blocks another task's land. Historical queue, review, candidate, and CAS receipts
remain immutable data; no supported command executes or resumes their retired
engine.

## Runtime and controller recovery

Controller runtime bytes are package-bound. For an ordinary consumer with an
older managed runtime, use the receipt-bound `yy task runtime-bootstrap` flow.
A Juno source target requires an exact package artifact/runtime matching the
source generation, explicit controller rebind, and exact target-transition
runtime refresh. These are maintenance authorities, not task delivery, and must
not publish, push, deploy, or mutate the product ref.

## Preservation and exclusions

Never reset, stash, force, rebase, squash, or clean to manufacture compliance.
Preserve dirty/conflicted worktrees, source commits, and historical receipts.
Task completion and Git ancestry grant no package release, tag, push,
publication, deployment, production mutation, or cleanup authority.
