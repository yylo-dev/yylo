---
wiki_contract:
  line_limit: 140
  purpose: "Seal, compose, validate, and deliver one history-preserving release epoch with one protected-target CAS."
  failure_mode_prevented: "Per-candidate target churn, omitted finished work, duplicate evidence/review, and release cuts while eligible candidates wait."
  runtime_contract_enforced: "Explicit immutable seal closes admission; one fenced drive drains the snapshot and emits read-only release readiness."
  validation_gate: "python3 .juno_task/scripts/tests/test_release_train.py && npm test -- src/cli/__tests__/release-command.test.ts"
  related_sots:
    - "watching_progress.md"
    - "controller/target_arbiter.md"
    - "controller/fenced_task_leases.md"
---

# Sealed release epochs

## Canonical lifecycle vocabulary

| Term/state | Owner and meaning | Allowed action |
| --- | --- | --- |
| fencing attempt / successor | controller-issued mutation credential; elapsed time never transfers it | observe lease; hand off or issue a successor only after producer-death proof |
| target arbiter | one on-demand deterministic owner for a protected target | observe status; explicitly run once and await its terminal receipt |
| complete input closure | tree, locks, generated/config inputs, command/selection, runtime, runner, policy, and receipt identity | reuse only on exact equality; otherwise restart the smallest invalid stage |
| `DRAFT` / explicit seal | projected candidates / immutable pre-cutoff membership | inspect freely; `seal` is an explicit admission-closing mutation |
| private train | dependency/FIFO composition with one merge commit per admitted task | never squash, rebase, force-push, or omit eligible finished work |
| `PAUSED_REQUIRED` / `EJECTED_OPTIONAL` | required failure pauses / optional failure carries its dependent subtree | resume with evidence or explicit fenced disposition |
| `NEEDS_OPERATOR` | ambiguity, scope/authority expansion, sensitive action, or exhausted bounded repair | consume one actionable packet; do not guess |
| `READY_CAS` / `RELEASE_READY` | aggregate gate complete / target CAS and readback proven | one expected-old-SHA CAS / read-only readiness only |

Command classes are strict: `plan|status|inspect|epoch-status|shadow` observe;
`seal|drive|eject|repair` mutate epoch state with explicit authority; target CAS
is a distinct protected-ref mutation; RC/tag/push/publish/deploy/cleanup are
external authorities. No observation command may acquire mutation authority.

## Release barrier

```text
explicit seal (close admission)
  -> drain every eligible pre-cutoff candidate
  -> compose private history-preserving train
  -> reuse exact task evidence and run one aggregate suite
  -> one expected-old-SHA target CAS + owner readback
  -> read-only release readiness
  -> reopen admission on the new base
```

`plan`, `status`, `inspect`, `epoch-status`, and `shadow` are observations. They
never seal, compose, mutate a target, release, push, publish, deploy, or clean.
`select` explicitly records routing for an exact umbrella/shared-path wave but
still grants no seal, validation, review, or target authority. Once selected,
the ordinary arbiter refuses those members with `epoch_delivery_selected`.
`seal` is the sole v1 admission-closing authority and returns a one-time fencing
token. Every later mutation requires that exact token.

## Operator flow

```bash
yy release train inspect /absolute/train.json --json
# inspect includes overlap, dependency/FIFO order, selected validation roots,
# per-candidate versus aggregate command counts, and a routing recommendation
yy release train select /absolute/train.json --json
yy release train seal /absolute/train.json --json
# retain lease_token from the seal result
yy release train drive EPOCH_ID --epoch-token TOKEN --json
yy release train epoch-status EPOCH_ID --json
```

The seal snapshots target/base, queue cutoff and FIFO order, dependency order,
required/optional/ambient-barrier disposition, task revision/tip/tree, queue
record, attempt, complete-input evidence, review, runtime, and policy identities.
All eligible pre-cutoff candidates are members, including unrelated finished
candidates. New candidates enter the next epoch. Retrying an identical seal is
idempotent; changing an existing epoch ID is refused.

Seal refuses before creating epoch state when any required member lacks an exact
review-ready closure bound to its frozen tip/tree and non-empty evidence. The
stable refusal begins `candidate.complete_input_missing` and requires closure
regeneration; operators must not knowingly seal a mixed/null snapshot.

Required failure transitions to `PAUSED_REQUIRED`. An optional ejection carries
its dependent subtree and leaves independent members admitted:

```bash
yy release train eject EPOCH TASK --reason REASON --epoch-token TOKEN
```

## Bootstrap-repair deadlock recovery

When the runtime repair needed to restore exact closures is itself queued behind
the affected blocked candidates, use the separate bootstrap-only transaction.
Its exact declaration names one self-hosting authority task, one repair task, the
affected blocked tasks, the expected target SHA, and all external exclusions:

```bash
yy release train bootstrap-inspect /absolute/bootstrap-repair.json --json
yy release train bootstrap-seal /absolute/bootstrap-repair.json --json
# retain bootstrap_token from the seal result
yy release train bootstrap-drive OPERATION_ID --bootstrap-token TOKEN --json
yy release train bootstrap-status OPERATION_ID --json
```

Seal proves both candidates are exactly queued with valid closures, the repair is
blocked by the authority task, each affected task is blocked by the repair, and
all unrelated active queue rows are frozen as preserved members. Drive composes
only authority then repair with both-parent history, permits no conflict worker,
performs one expected-old-SHA CAS/readback, refreshes the managed runtime, and
idempotently finalizes only its exact ancestry-bound members through canonical
Ledger/lifecycle readback. Its next action is to regenerate affected closures,
then seal a fresh all-eligible epoch. A retry completes missing reconciliation
without another CAS. It cannot seal that
epoch, complete affected tasks, release, tag, push, publish, deploy, or clean.

## Composition and conflict recovery

Composition uses an isolated controller-managed **full** worktree/ref rooted at
the sealed base. It disables and verifies absence of inherited sparse/skip-worktree
state before composition or validation. Dependency topology wins, then frozen
FIFO order. Every admitted task gets a both-parent merge commit; task commits
are never squashed, rebased, or rewritten. Ejected tips must be absent.

Before seal, read-only inspection precomposes the exact prefix and records every
remaining member after the first unresolved repair in one deterministic conservative
envelope. Historical both-parent proof is diagnostic only during inspection: it does
not advance the forecast unless drive has consumed it through the explicit narrow
`replay-repair` transition. Seal refuses unless that complete envelope fits one frozen,
declaration-bound authorization-neutral logical set. Member accounting and forecast
completion do not claim unknown post-repair trees are exact.

A conflict preserves the dirty checkout and writes one bounded repair packet with
base/ours/theirs, the authority hash, and every ordered logical-set member's task ID,
tip, tree, requirement digest, exact permitted paths, and selected validation root.
The canonical managed worker verifies and hydrates that root from its exact lock and
appends this complete frozen scope to the model prompt before launch, then atomically
finalizes an exact successful capture when provider capture is missing or stale. Run
one model session for the whole frozen set and submit its one immutable receipt:

```bash
yy release train repair EPOCH --receipt RECEIPT --epoch-token TOKEN
yy release train drive EPOCH --epoch-token TOKEN --json
```

A successor may instead replay an already consumed predecessor repair without a
model call, but only when package/runtime/policy, receipt artifacts, candidate,
parents, ancestry, and resulting tree are exact:

```bash
yy release train replay-repair EPOCH --predecessor-epoch PREDECESSOR \
  --receipt RECEIPT --epoch-token TOKEN --json
```

Before receipt consumption, the controller clones an isolated checkout at the
both-parent repair commit and deterministically composes the complete remaining frozen
suffix. It consumes the repair only when every member drains without another model
repair and records the exact per-member trees in the immutable repair reference; drive
revalidates those trees while composing. A remaining conflict refuses consumption as
`NEEDS_OPERATOR` with `repair.grouped_suffix_unresolved`. Any later conflict after a
consumed repair reaches the same truthful terminal reason and offers only fresh
successor inspection/seal—not an impossible second repair command.

Replay never edits predecessor evidence and records `model_rerun=false`. Missing or
ambiguous legacy evidence returns a typed incompatibility; deterministic adaptation
binds only immutable declaration bytes and grants no authority. Out-of-set edits,
unresolved conflicts, a second set, identity drift, sensitive/destructive choices,
new dependencies, or ambiguity stop as `NEEDS_OPERATOR`. Material repair receives
one scoped delta review.

## Evidence, CAS, and recovery

Candidate evidence is reusable only with its complete-input identity. Before the
aggregate gate, the train hydrates the selected validation root from its exact
lock using the canonical hydration helper and proves ignored outputs left no Git
drift. The train runs one aggregate suite per exact train tip. A failed aggregate
moves to `RECOVERING`; authorize an exact-tip retry without state-file edits:

```bash
yy release train retry EPOCH --epoch-token TOKEN --json
yy release train drive EPOCH --epoch-token TOKEN --json
```

The retry is fenced and binds the failure receipt; it never recomposes members or
performs a duplicate CAS. A passing exact aggregate receipt is reused. Use
`yy release train epoch-status EPOCH --json` for the bounded state/reason/next-action
projection; immutable receipt history remains out of the default projection. Before CAS,
drive rechecks queue records, ancestry/dispositions, runtime/policy, aggregate
evidence, and the target base. Target movement preserves the epoch as
`STALE`; create a successor seal on the new base and reuse only exact closures.

A successful CAS advances the protected ref once, advances/readbacks the
registered integration owner through the canonical merge owner, then revision-CAS
projects every exact admitted member to canonical Ledger `done` / lifecycle
`MERGED` before emitting `juno_release_epoch_readiness.v1`. Member finalization
has a separate immutable journal and resumes after interruption without another
CAS. For an older receipt-proven `RELEASE_READY` epoch whose Ledger projection was
interrupted or omitted, use the explicit typed recovery bound to the exact current
target readback:

```bash
yy release train reconcile-members EPOCH_ID --expected-target TARGET_SHA --json
```

Recovery verifies the seal, complete receipt chain, CAS/readiness, ordered member
tips and ancestry, queue/task identities, and each hot task's canonical own-record
revision plus immutable Ledger event hash through the supported history API. It
excludes relation/dependency-expanded `get` details that can change when another
member finalizes, while own fields and authority-bearing relation topology remain
revision-bound. It refuses target/replay drift and never substitutes direct Ledger
mutation. Readiness grants
no tag, package release, push, publication, deployment, production mutation, or
cleanup authority.

## Shadow canary and rollback

```bash
yy release train shadow /absolute/train.json --baseline /absolute/baseline.json \
  --output /absolute/decision.json --json
```

Shadow is read-only and reports seeded scenario coverage, target-move count,
duplicate unchanged-closure execution, model lifecycle calls, and cache-read
token deltas. The baseline is required and may be a lifecycle aggregate scorecard.
When the live queue has already drained, the positional source may instead be an
immutable sealed epoch `state.json`; shadow verifies the frozen plan identity and
every referenced receipt hash without consulting or mutating current queue state.
This historical replay is observation only, not authority to resume the old epoch.
Missing baseline fields, receipt drift, or missed thresholds fail closed with reason
codes and `BLOCK` (identity drift is an error). The disable/rollback switch stops
epoch drive without deleting immutable epoch receipts. Any legacy queue path is a
version-gated recovery mode, not the normal release workflow and not permission for
per-candidate RC cuts.
