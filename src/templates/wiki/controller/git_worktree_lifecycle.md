---
wiki_contract:
  line_limit: 340
  purpose: "Run exact-base task worktrees and one fenced per-target delivery owner."
  failure_mode_prevented: "Controller edits, stale-worker takeover, model polling, and unsafe target movement."
  runtime_contract_enforced: "yy task owns feature worktrees; one yy merge arbiter owns composition and expected-old-SHA CAS."
  validation_gate: "python3 .juno_task/scripts/tests/test_task_workspace.py && python3 .juno_task/scripts/tests/test_merge_queue.py"
  related_sots:
    - "controller/fenced_task_leases.md"
    - "controller/target_arbiter.md"
---

# Bolt task worktrees and merge queue

The controller is a metadata store. It contains Kanban/task truth and compact
receipts, not product code. Runtime scripts are installed from one versioned
YYLO package and are not synchronized through product history.

```text
metadata controller
  +-- yy task start X --> product worktree X -- implement/test/commit --+
  +-- yy task start Y --> product worktree Y -- implement/test/commit --+--> queue
                                                                          |
                                                   one target lock + expected-SHA CAS
                                                                          |
                                             moved target -> compose; conflict -> preserve
                                                                          |
                                                           verify identity -> MERGED
```

## Public commands

```text
yy task start TASK_ID                    # omit --path only for legacy baseline admission
yy task start TASK_ID --path exact/file   # repeat exact files; policy trees also supported
yy task status TASK_ID                   # state, producer fence, one eligible action/reason
yy task admission TASK_ID                # read-only dirty/committed scope gate
yy task preflight TASK_ID
yy task checkpoint TASK_ID
yy task checkpoint TASK_ID --accept CHECKPOINT_ID # exact evidence + ordered progress
yy task finish TASK_ID

yy task status RELATED_ID # reporting-only IDs show their ordinary delivery owner

yy task runtime-bootstrap --dry-run
# review the printed immutable receipt
yy task runtime-bootstrap --apply RECEIPT

yy merge status                 # bounded read-only summary (32 KiB / 19 rows maximum)
yy merge status --detail TASK_ID # bounded selected-task diagnostic projection
yy merge status --detail        # bounded active FIFO-attempt projection
yy merge status --full          # explicit legacy exhaustive representation
yy merge arbiter status         # read-only owner/next-action observation
yy merge arbiter run            # explicit fenced on-demand mutation
yy merge drive --through TASK_ID # explicit typed mutation
yy merge next                   # explicit single-step recovery
yy merge resolve TASK_ID        # explicit preserved-conflict recovery
yy merge recover-authority-drift TASK_ID --attempt N \
  --terminal-receipt /canonical/attempt-N-failed.json \
  --terminal-receipt-sha256 SHA256 --expected-revision RECORD_SHA256
```

`recover-authority-drift` is the only editable recovery for a preserved `MERGING`
incident that failed deterministically at `before_target_cas`. Read `record_revision`
from `yy merge status --detail TASK_ID` and the exact attempt/terminal receipt identity from
`yy merge arbiter status`, then run the command once. It requires a dead producer,
exact clean source and candidate worktrees, unchanged source/candidate/target
identities, and positive no-CAS proof. It atomically retains the failed queue
attempt and candidate as evidence while issuing a fresh fenced `WORKING` lease.
The safe next step is one descendant repair commit followed by
`yy task preflight TASK_ID`, using the returned lease token for later fenced
mutations. It distinctly refuses reviews, conflicts, post-CAS state, live
ownership, dirty or ambiguous worktrees, identity/revision drift, malformed
evidence, and repeated recovery. It never validates, reviews, composes, launches
a worker, cleans a candidate, or changes the protected target. Do not use
`yy merge next` to repeat the unchanged deterministic attempt.

Task start freezes the exact target SHA. Omitting `--path` retains the legacy
baseline envelope. Repeated tracked files freeze only those exact files plus
declared generated destinations; explicitly selected policy trees remain
supported, but exact-file requests never silently inherit baseline roots.
Before editing or testing there, the worker follows [task dependency hydration](task_dependency_hydration.md)
for each configured validation cwd and stops on provisioning or clean-tree
failure. Runtime identity is validated before any Juno-specific generated-output
admission. Task preflight is read-only and checks the clean committed tip,
admitted changed paths, generated-output closure, risk policy, and runtime
identity before expensive final gates. Task finish repeats that closure,
persists it, and runs focused validation before queueing. Focused rows without a
`resource` declaration run in independent concurrent lanes. Rows declaring the
same bounded `resource.id`, absolute `lock_path`, and `wait_timeout_seconds` run
once in policy order on one exclusive lane; resource wait precedes and never
consumes each row's operation `timeout_seconds`. Receipts retain bounded owner
and wait diagnostics, lane position, and critical-path contribution. A terminal
failure persists the complete schedule and is not automatically retried against
an unchanged candidate. Independent features can remain active concurrently.
Project classification is explicit: a source repository whose
`juno-code/package.json` names `yylo` must provide both authoritative,
strict declarations; an ordinary consumer without that package identity has no
Juno-source declaration requirement.

If start reports a stale or absent ordinary consumer target task runtime,
`scripts update` refreshes only controller-local bytes and is not the recovery.
Run `yy task runtime-bootstrap --dry-run`, review the immutable
package/controller/target/path receipt, then apply that exact receipt. An absent
consumer runtime is recoverable; a present one additionally requires an exact
managed-inventory hash/version binding to an older package generation. Recovery
updates the runtime and its inventory entry's version/hashes together while
preserving the inventory-wide package version and validated unrelated entries. A Juno source
target is deliberately refused: use a controller package/runtime matching a
coherent newer target, or update an older source package, template, tracked
runtime, and managed inventory atomically. This command is restricted to
the exact registered, sparse metadata-controller class and refuses
synthetic/product/task worktrees. Apply uses a clean isolated target worktree,
creates a reviewed recovery commit and durably records its apply intent. Before mutation it discovers
all exact target-ref holders under the merge queue's repository/target-ref lock.
Every advancement uses expected-SHA CAS. With one exact clean unlocked holder,
the planned-path index/worktree state is prepared with Git's non-destructive merge
mode and revalidated before CAS; concurrent dirt refuses rather than being reset,
and no post-CAS operation can overwrite it. With no holder, a package-owned clean
guard checkout holds the branch until immediately before durable completion. Dirty, locked, moved,
or multiple holders refuse before mutation with a supported clean,
unlock, or reviewed extra-worktree removal action. An exact package-created
partial synchronization refuses with a bounded restore command for only the
planned paths; review and run it before rerunning the same receipt. Fully prepared
holder or completion interruptions recover directly; the durable intent prevents
another commit or unrelated ref mutation. Modified or completed
receipts, package mismatch, non-older inventory generations, and consumer target
customization without exact managed-inventory provenance also refuse.

One on-demand target arbiter serializes mutation with a fencing token, per-target
kernel lock, and expected-old-SHA update, then exits when idle or blocked.
All task/merge consumers use `juno_path_origin_projection.v1` from complete Git
blob maps: altered inherited bytes are authored and ambiguity fails closed. Lease
age alone never transfers ownership: successor attempts require controller proof
of producer death or explicit handoff. Agents observe rather than poll. Dirty
conflict bytes are preserved for one bounded managed repair. Exact complete-input
closures may be reused; drift restarts only the smallest invalid stage.

Review is queue-owned and risk-based: low zero, normal at most one, and high
Reviewer A followed by Reviewer B against the same frozen candidate under the
compatible v1 predecessor-bound receipt contract. The queue permits one repair
candidate and one delta review group; another material
finding stops as `REVIEW_FINDINGS_EXHAUSTED` instead of spawning an autonomous
loop. A changed product candidate invalidates prior semantic evidence, while a
byte-identical metadata/harness retry may reuse evidence only when all bound
policy/runtime/closure identities remain exact. A repeated deterministic
`FAILED_FULL_SUITE` is never rerun unchanged. `yy merge status --detail TASK_ID` returns
`deterministic_full_suite_repair_available` and one exact
`yy merge recover-full-suite-failure TASK ...` command binding the lifecycle
journal/revision, failed suite and finding, candidate/tree, target, producer, and
predecessor arbiter. Bare status is the `merge-status.summary.v1` projection and
never constructs exhaustive attempt payloads; detail is `merge-status.detail.v1`,
and only `--full` returns `merge-status.full.v1` legacy fields. Task and merge
status are observational: they expose lifecycle state, producer/fence observation,
one mutation eligibility decision, typed reason, exact invalidating change, prior
terminal evidence, and either one supported next action or an operator stop.
Malformed or unknown evidence is never success. Machine-owned transitions do not
require a redundant status call because the executor rechecks live authority and
state before expensive work and mutation. JSON projections
always declare their level, byte/row limits, truncation, and cursor. Interactive
output prints the same projection identifier and truncation/cursor truth; use
`--json` to force structured output in a terminal. That transition authorizes the existing queue-owned single
repair worker; only the failing router contract and its exact CLI/router
counterparts may change. The repaired delta runs focused validation before the
required policy suite and one delta review group. Any environmental failure,
live producer, moved identity, unrelated delta, malformed receipt, post-CAS
state, or spent repair/delta budget refuses without mutation. If existing `semantic-repair-0001` passes create/verify/edit preflight but is refused before provider launch, `yy merge status --detail TASK_ID` exposes the only supported `recover-repair-predispatch` command.
It binds failed arbiter, lifecycle revision/run/scope/journal, candidate/tree/target, repair authorization, exact worker path, all admission-receipt digests, no-provider receipt, and clean controller commit/tree; its immutable projection proves no provider launch/model cost, preserves worker/receipts, keeps `repair_count=1`, and makes only that worker eligible for typed safe next `yy merge arbiter run --through TASK_ID`.
Live producer, launch/terminal evidence, dirt, moved identity, altered worker, missing receipt, repeat, exhausted delta budget, conflict, or post-CAS state refuses without mutation; generic task pre-dispatch recovery does not own REVIEW_FINDINGS merge journals. After CAS, only deterministic identity/readback and bounded smoke checks run.

Cleanup refuses unless the delivered commit is reachable and the task worktree
is safe to remove. Push, release, publication, deployment, production mutation,
restart, and post-deploy E2E are never implied by merge completion.

Historical local-integration receipts remain readable by Workflow Runner doctor.
Their executors are retired and must not be adapted into the Bolt path.
## Ordinary delivery checkpoints

One cohesive delivery is one ordinary task, admitted scope, worktree,
submission, review policy and merge boundary. Checkpoint deliveries must start
with repeated exact `--path` arguments; implicit baseline scope is refused.
Its authored task body may contain
one bounded `[delivery_checkpoints]` JSON contract using
`juno_task_delivery_checkpoints.v1`. The contract orders 1–32 requirement
objects (`id`, `requirement`, `final`), requires exactly one final item ordered
last, and may name reporting-only `tracking_task_ids`. Those IDs have no start,
checkpoint, finish, review or integration authority while owned; status points
to the ordinary delivery and never claims a separate integration.

After a coherent commit, run `yy task checkpoint TASK_ID --accept ID`. The
executor runs or reuses the normal exact standing-evidence producer and records
an immutable requirement/base/tip/tree/submission/validation binding. Evidence
must chain in requirement order. The final checkpoint validates the cumulative
candidate, and preflight/finish refuse unless it binds the exact submitted tip.
Checkpoint completion means `IMPLEMENTED`; only the owner's landed target result
means `INTEGRATED`.

The ordinary `yy task` surface has no umbrella start, child checkpoint, or
conversion command. Existing umbrella schemas are finite legacy inputs under
`yy migrate legacy-lifecycle plan|authorize|apply|verify|checkpoint` only.
Plan and verify are read-only; apply requires the controller-issued receipt for
one exact reviewed plan, and checkpoint only drains an already-WORKING legacy
attempt. These aliases call the same managed task runtime and never create a
child worktree or a second executor. Historical receipts, dirty bytes and
predecessor evidence remain immutable, unsupported state refuses, and no child
is represented as separately integrated. See
`juno-code/docs/lifecycle-simplification-migration.md` for state dispositions,
live-authority restrictions and the owner-proved retirement condition. Live
conversion and release activation remain separately authorized maintenance.

## Checkout-aware entry points

The same installed `yy` command can start in the controller, integration owner,
task worktree, or a nested directory. Shared Git registration binds those
checkouts to one exact controller path/ref and product target; routing happens
before checkout-local bootstrap. The caller's checkout is never switched,
cleaned, stashed, or made authoritative by inference.

```text
invocation directory
  +-- controller ---------+
  +-- integration owner --+--> controller router --> Kanban/task/merge runtime
  +-- task worktree ------+
  +-- nested directory ---+

product bytes
  +-- task worktree ------> edit, focused test, commit
  +-- integration owner --> synced read/debug/server checkout
```

Use `yy info --json` for stable machine-readable topology, `yy where
controller|integration|target|task` for one script-safe path, and `yy doctor
workspace` for offline health/refusal guidance. Missing, stale, dirty, attached,
or ambiguous integration ownership fails closed.

Existing admitted workspaces may retain a historical
`.juno_task/scripts/install_requirements.sh` that writes
`.juno_task/.version_check_cache` inside the checkout. `yy integration status`
and `yy integration runtime-doctor` report both exact paths separately. A
receipt-bound runtime transition may replace the writer only when its bytes
match immutable target history. A tracked cache is never restored or deleted
automatically: remove it in a normal product task, validate the protected
worktrees byte-stable, and deliver that commit through `yy task`/`yy merge`.
If that delivered target leaves a clean, detached, full registered owner stale
with only these two findings, `yy integration repair --dry-run` may emit the
narrow `stale_owner_legacy_cache_migration.v1` disposition. It binds the old
HEAD/tree/role base, exact target SHA/tree, finding-removal evidence, protected
authority, and a topology-preserving recursive gitlink closure. Gitlink SHAs may
advance, but every target object must already exist locally. Apply revalidates
the receipt under the target lock, advances only the bound owner and role base,
hydrates with `--no-fetch`, and requires an exact clean final readback. Any other
finding, dirt, topology change, unavailable object, authority mismatch, or ref
drift refuses; this is not a generic blocker bypass.

## Receipt-bound stale lifecycle supersession

A managed merge-drive journal that remains nonterminal after its in-flight task
was receipt-recovered and requeued must not be deleted or have its prior events
rewritten. Use `yy merge supersede-lifecycle-journal` only with the exact run,
journal revision and byte digest, frozen scope, terminal failed-arbiter receipt,
recovered task receipt, unchanged target SHA, and current actionable FIFO digest.
The operation requires a dead producer, a proven pre-CAS lineage, and a current
FIFO that differs from the frozen one. It appends one immutable `SUPERSEDED`
projection and deterministic summary, preserves queue rows byte-for-byte, and is
idempotent for the same complete binding. Live producers, valid current scopes,
missing recovery lineage, target drift, post-CAS evidence, malformed artifacts,
and changed revisions refuse with distinct reason codes.

The command output names `yy merge arbiter run` as the only safe next action. A
fresh compiler then selects current FIFO normally; supersession grants no queue
reorder, candidate deletion, target CAS, release, push, deploy, or cleanup
authority. Obtain `current_fifo.sha256` from the candidate runtime's read-only
`arbiter status` projection and never improvise identities or edit evidence:

```bash
python3 "$CANDIDATE/.juno_task/scripts/merge_queue.py" --controller "$CONTROLLER" arbiter status
python3 "$CANDIDATE/.juno_task/scripts/merge_queue.py" --controller "$CONTROLLER" supersede-lifecycle-journal \
  --run-id "$RUN_ID" --expected-journal-revision "$REVISION" \
  --expected-journal-sha256 "$JOURNAL_SHA256" --scope-sha256 "$SCOPE_SHA256" \
  --arbiter-attempt "$ARBITER_ATTEMPT" --terminal-receipt "$FAILED_RECEIPT" \
  --terminal-receipt-sha256 "$FAILED_RECEIPT_SHA256" --recovered-task "$TASK_ID" \
  --recovery-receipt "$RECOVERY_RECEIPT" --recovery-receipt-sha256 "$RECOVERY_SHA256" \
  --expected-target-sha "$TARGET_SHA" --expected-current-fifo-sha256 "$FIFO_SHA256"
```

## Integration owner lifecycle

```text
status [--fetch]
       |
       v
sync: guard -> fetch -> verify target -> fast-forward -> exact submodules
       |
       +--> healthy: inspect/debug/start local server here
       +--> refusal: repair --dry-run -> review receipt -> repair --apply RECEIPT

publication: push --------------------------------------------> plan + apply under one lock
             push --dry-run -> optional review -> push --apply RECEIPT
             child repositories first -----------------------> root last
```

Repair never discards local work, and push never follows from sync or repair
authority. Bare `yy integration push` is explicit publication authority and
internally persists then applies one exact plan; dry-run/apply remain available
for delayed or audited publication. Every apply binds its plan receipt to exact topology and SHAs,
rechecks readiness under a lock, and records partial-failure truth for safe
retry. Package publication, deployment, production mutation, and post-deploy
E2E remain outside these commands.

## Observable nonblocking execution

Use `@@life_cycle TASK_IDS_OR_GOAL` to load the versioned orchestration contract.
For example, `yy pi -p '@@life_cycle T1 then T2; stop before release'` preserves
the caller payload once while directing work through canonical `yy` lifecycle
commands.

For every long-running agent, finish, merge, or authorized release command,
create a private task-ID `mktemp -d` run directory and place distinct log, PID,
and footer files inside it. Capture combined stdout and stderr, keep the producer
timeout-bounded, atomically publish its PID immediately, and atomically rename a
strict `juno.watch-footer.v1` footer immediately after exit. Resolve
`controller_root=$(yy where controller)` and invoke the absolute
`$controller_root/.juno_task/scripts/watch_progress.py` path rather than a
checkout-relative script or rewritten polling loop. Its producer example,
JSONL/raw-payload framing, and footer/PID identity contract are in the watching
progress guidance (`yy_pi_progress.md`). A quiet process doing real-Git or test work is
active until PID/process evidence or a valid terminal footer proves completion;
log silence alone is never a hang signal. Report exact exit, elapsed duration,
and run-directory paths. This pattern adds observation only; Workflow Runner and
the managed-agent runner remain the execution owners.

Independent review is fresh and read-only against one frozen committed diff.
Task finish, merge/CAS, integration repair/push receipts, release build/tag/global
verification, and push/publish/deploy authorities remain separate boundaries.

Agent orchestration instructions and core skills are ignored installed assets in
the controller. Product/domain instructions and skills are tracked in product
history and materialized in task worktrees. A controller symlink inside the
integration owner is intentionally unnecessary and unsafe for search/staging;
use `yy where controller` when an agent or script needs the exact path.
