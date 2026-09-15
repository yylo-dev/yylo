---
wiki_contract:
  line_limit: 220
  purpose: "Run exact-base task worktrees and land one selected task with native Git."
  failure_mode_prevented: "Controller edits, stale-worker takeover, FIFO blocking, hidden model review, and unsafe target movement."
  runtime_contract_enforced: "yy task owns feature worktrees; yy merge selects one task, uses native Git composition and expected-old ref update, then projects Ledger separately."
  validation_gate: "python3 .juno_task/scripts/tests/test_task_workspace.py && python3 .juno_task/scripts/tests/test_merge_queue.py"
---

# Task worktrees and native Git delivery

The controller is a metadata store. Product edits happen only in exact-base task
worktrees. Delivery selects one immutable task source; there is no FIFO queue
drive, target arbiter, merge-owned test/review/model loop, or automatic repair.

```text
yy task start X -> edit/test/commit -> finish -> QUEUED
                                                        |
yy merge land X -> private native Git candidate -> expected-old update
                                                        |
                                  automatic lifecycle projection -> Ledger
                                  (`yy merge project X` retries projection)
```

## Public commands

```text
yy task start TASK_ID
yy task status TASK_ID
yy task admission TASK_ID
yy task preflight TASK_ID       # optional read-only admission diagnostic
yy task finish TASK_ID --lease-token <current-token>

yy merge status [TASK_ID]       # read-only independent task status
yy merge land TASK_ID           # compose and land one task; no tests/reviews/models
yy merge project TASK_ID        # retry or repair Ledger projection after Git integration
```

`task start` freezes the target and runs exact-lock hydration before reporting
`WORKING`. In the returned worktree, follow
[task dependency hydration](task_dependency_hydration.md) before editing or
testing and stop before implementation on failure. `preflight` is optional and
read-only, not a prerequisite. `finish` independently enforces admission and
configured task validation of the clean immutable source.

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

`merge land` projects Ledger immediately after Git success. If projection fails,
Git remains integrated and the command reports `merge project` as recovery. A
failed `merge project` leaves the Git result intact and retryable, never repeats
integration, and never
blocks another task's land. Historical queue, review, candidate, and CAS receipts
remain immutable data; no supported command executes or resumes their retired
engine.

## Read-only task doctor

`yy task doctor [TASK_ID]` compares lifecycle records with Ledger without repair.
Board scans use cursor-bound hot pages of 100 projected records, not one Ledger
process per task. Only selected IDs absent from hot pages receive batched exact
`get --compact` lookups, preserving cold-ID retrieval without enumerating cold
storage. Exact-ID doctor uses one bounded get. Bodies are never returned by doctor.

The default output covers at most 1000 lifecycle rows. Use `--limit 1..1000` and
`--offset N` to inspect sorted-ID windows; an offset is not a snapshot cursor.
`coverage` reports read calls, pages, cold batches, selected/total records,
`next_offset`, and completeness. Partial scans report `outcome: incomplete`,
never overall agreement. Missing tasks and failed reads remain distinct.
Each child is capped at 30 seconds and 1 MiB combined output; scans admit no new
child after 90 seconds or 100 hot pages. A child may finish within its own bound.
Oversized, malformed, failed, or stale-cursor responses stop the scan without
retrying by offset. Lifecycle changes during the scan also mark it incomplete.
Observations are explicitly non-atomic across lifecycle, hot pages, and cold
reads. A later change can invalidate an earlier observation; doctor grants no
projection or integration authority. Process-count reduction is not an elapsed-
time guarantee.

## Manual tokens and managed recovery

`start` and `lease-successor` return a `lease_token` once. Store it privately;
never paste it into task evidence, logs, or shell history. The placeholders below
mean the exact token from that command's response, not literal argument text.

```text
yy task start TASK_ID
# retain its returned token, then implement/test/commit in the returned worktree
yy task finish TASK_ID --lease-token <returned-token>
```

A helper's exit does not invalidate its token. An ACTIVE attempt's exact current
token admits manual gated commands even if `lease-status` reports
`lease_producer_dead`. Status observes without a token; it does not test bearer
authority. Reuse your current token rather than issuing another successor.

If the token is lost and the controller proves the predecessor ended:

```text
yy task lease-successor TASK_ID
# retain the NEW returned token; at the unchanged clean base:
yy task start TASK_ID --lease-token <returned-token>
# pass that same token to subsequent gated commands, such as finish
```

After edits or commits, retry the original gated command with the new token,
not `start`: start reentry independently requires the unchanged clean base.
Bare `lease-successor` followed by tokenless `start` is not a supported manual
sequence. `clean_resume` describes worktree bytes, not hydration/validation
clearance. Wrong or superseded tokens refuse; expiry alone grants no takeover.
Live or unknown producers require the current token, an exact holder handoff
(`lease-successor --handoff-receipt <path>`), or separately authorized operator
revoke. Never infer that authority from an error's suggested command.

For **authorized managed execution**, use `yy task run TASK_ID` or its alias
`yy task resume TASK_ID` instead. The continuing managed process acquires a
receipt-bound successor after proven predecessor death and carries the token
internally; no user-supplied token is needed. This may launch workers, not just
repair ownership. Existing terminal-run, hydration, dirty-worktree, identity,
and budget restrictions still apply; neither command resets budgets or repairs
unrelated blockers. Do not run standalone successor first for managed execution.

## Managed worker controller checks

Worker acceptance binds relevant inputs, not controller immobility:

```text
launch -> controller root/branch/registration + task state/requirements
       -> configuration/policy/runtime/instruction identity -> worker
       -> capture actual producer outcome
       -> compare relevant identities
            +-- unrelated task metadata / checkpoint -> continue
            +-- relevant change / unknown evidence -> refuse this worker
       -> unchanged candidate/path/clean-output checks -> consume result
```

Unlike the former HEAD/index/status and whole-state equality check, an unrelated
canonical task row, Ledger event, scope record or checkpoint does not invalidate
worker acceptance. Staging or committing already admitted metadata is harmless
when the task's inputs remain unchanged. Task-state acquisition is bounded;
other tasks' bodies and Ledger content are not read for this comparison.

The guard still binds the selected lifecycle record (including ownership), task
requirements and optional scope, controller root/branch/common repository and
registration, tracked non-task inputs, and installed configuration, scripts,
prompts, skills and instructions even when ignored by Git. Unknown non-metadata
dirt refuses; dirty or committed relevant changes do not become safe merely
because a checkpoint occurred. Missing/malformed task evidence refuses. Reviewer
controller checks remain strict in this iteration.

Receipts separate `producer_terminal_result` from `controller_guard` and overall
runner success/failure. A producer declaring `incomplete` is never promoted to
completed by a passing guard. A failed guard retains the captured producer result
when available and reports bounded changed identity keys and digests, not secret
file contents. This post-execution check is not a sandbox and does not identify
which process wrote a changed file.

There is no new command, retry loop, global lock or manual approval for harmless
checkpoint movement. Existing terminal-run restrictions, budgets, leases,
validation and native target-update checks are unchanged. Source delivery does
not activate an installed runtime or replay any historical failed attempt.

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
