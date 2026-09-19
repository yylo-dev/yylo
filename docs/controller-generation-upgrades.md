# Controller generation upgrades

Scripts remain bundled with the CLI and copied into the registered metadata
controller. A package generation owns scripts, instruction identity, managed
inventory, controller policy and executable selection together. Updating just one
of these is not recovery.

## Harness-handoff diagnostics

The standalone Python service regression suites can use a task-local Python 3.12
venv with the hash-pinned `test_pi_service/requirements.lock` (`uv pip sync --python
.venv/bin/python --require-hashes test_pi_service/requirements.lock`). Do not borrow
another checkout's interpreter packages.

`YYLO_STARTUP_TIMING=1` opts into a bounded JSON diagnostic on service stderr
when a shipped Pi, Claude, Codex or Gemini service successfully launches its
harness executable. Pi's terminal and piped paths both observe the final harness,
not the intermediate Python service. Normal invocations emit no timing diagnostic.
The observation contains only the harness name, launch index, elapsed milliseconds,
clock and origin precision; it contains no prompt, session, path or model data.
It never imposes a runtime deadline or changes a launch failure.

The public shell wrapper resets the origin at entry, and retained Node runtime
redispatch preserves it. Service entry consumes it before harness descendants
launch. Bash without `EPOCHREALTIME`, missing/malformed origins and backwards clock
movement produce no sample. `unix_wall` and `origin_precision_us` describe the
clock representation, not guaranteed accuracy. Caller-provided environment data
is advisory, never authentication or accepted performance evidence.

This is **T_handoff**, not internal session readiness, provider acknowledgment or
first output. The diagnostic marks successful exec, not completed prompt-pipe
consumption. Validate exact prompt/resume routing separately with isolated harness
fixtures and cross-check wrapper-to-entry timing externally using a monotonic
clock. Neither a component sample nor this optional diagnostic alone establishes
the five-second end-to-end acceptance budget.

## Ordinary execution and explicit upgrade

Normal task/merge execution and agent startup authenticate the selected active
runtime under a reader lease. They never discover candidate artifacts, plan an
upgrade, migrate, repair or recover an interrupted transaction. Installing a new
global package does not activate it: ordinary execution uses the authenticated
retained active executable unless the invoking package is an exact-byte authenticated
copy of that active artifact. This comparison uses the already authenticated active
archive bytes, verifies every file and execution-directory entry at the distinct
invoking root, and includes those inputs in the reader guard's checked engine reuse.
It does not discover a candidate or infer trust from version equality. Differing,
Git-owned, symlinked or poisoned copies still dispatch only to the safe retained
runtime. Legacy controllers/runtimes unable to guarantee this
explicit-only contract refuse with maintenance guidance instead of being reentered.

Activate exact recognized preimages explicitly, without a prompt or network
installation:

```sh
yy scripts generation upgrade
```

Read-only diagnostics may assess a prospective candidate but do not migrate:

```sh
yy scripts generation doctor
yy scripts doctor
yy integration runtime-doctor
```

After activation these doctors use the same package-generation assessment as
startup. When the invoked package root is already the selected active generation,
assessment validates the active package, managed inventory, runtime selectors and
applicable attempt/ACTIVE-lease pins directly, without discovering a candidate or
building a hypothetical migration plan. Authentication and schema facts are reused
only within one assessment and only for identical evidence tuples; different roots
and later invocations still require authentication. This read-only observation is
not a transferable admission proof. Same-artifact distinct-root comparison avoids
redundant retained redispatch, not distinct-root authentication. Dispatch acquires the reader lease before its
single active assessment, retaining post-lock race protection. Different/global
package roots dispatch only to the authenticated active runtime with an authenticated
`explicit-only-v1` package capability; version equality is not authority.

The invocation-owned reader guard can retain the exact authenticated file digests,
directory entries, absent repository/fence markers and Git query results in memory.
Engine entry reuses that assessment only for the same controller/package roots,
unchanged Git environment, a live reader guard and checked unchanged inputs. It
rereads bytes and directory/Git observations; it does not repeat archive extraction,
schema parsing or pin authentication. Input changes invalidate reuse. Direct engine
callers without that checked guard validate independently. Neither callers nor
inherited environment variables can supply a proof, and no persistent trust cache
or serialized proof is created. Ordinary task-pin selection likewise uses active
readiness rather than a hypothetical upgrade plan. This does not certify provider/session
startup timing. Workspace topology checks remain separate. A prospective migration is
not a claim that an old controller has already been upgraded.

Generation assessment/explicit upgrade and engine dependency/backend/startup-hook
waits emit phase-specific stderr progress immediately and every four seconds while
pending. Quiet CLI/engine modes suppress these notices. Each timer is invocation-local,
clears on success or failure, owns no cancellation authority and never imposes a
performance deadline. Shipped Python services likewise report their pre-harness wait
and stop at successful exec; their progress flag is removed before harness children.
Custom hooks still run in their original order with their original failure policy.

Both package generations require authenticated artifact evidence. Supported
sources are installation-owned `.yylo-generation-evidence.json` or npm's installed
package receipt with its exact SHA-512 artifact still in the offline cache.
`runtime-install-rebind` retains its authenticated artifact outside Git. Keep
prior versioned installations and artifacts available while tasks are pinned;
cache eviction or deleting the old installation is not authorized by migration.
Global npm installations that omit the hidden lock use bounded offline cache-index
discovery. Index framing and SHA-512 content hashes are checked, then the complete
installed package is compared with the tarball; a cache key or version match alone
never authenticates a candidate. Missing/evicted evidence still refuses without
network acquisition. Discovery reads at most 20,000 index files / 32 MiB and 2,048
unique candidate artifacts, with a 30-second discovery deadline; a larger cache
requires an explicit artifact-bound installation rather than unbounded startup
work during explicit maintenance/diagnostics, never ordinary admission. Nonregular
cache entries and ambiguous repository paths are rejected.

Before explicit activation, the candidate is retained in a content-addressed
prefix under `${XDG_STATE_HOME:-$HOME/.local/state}/yylo/installed-generations`.
This uses the authenticated tarball and offline npm with lifecycle scripts,
network access, audit and funding requests disabled. If dependencies are absent
from the offline cache, activation refuses without changing the controller.
The controller binds this retained executable, not the mutable global npm path,
so the next global install cannot erase the previous generation needed for
migration or rollback. An unchanged global artifact reuses its verified retained
copy; doctors remain read-only and do not install anything.
A semver string, matching one script, or an arbitrary package directory is not
sufficient evidence. Unknown or customized state is preserved, not overwritten.

Shared execution leases exclude generation writers. Active task pins bind the
original attempt to its authenticated retained runtime. If a Juno source target
advances beyond that old reader, the fully admitted current source reader may
continue the same attempt only with shared-state compatibility; the retained pin,
lease, hydration and creation receipt are not rewritten. Agent first-use routing
uses the registered CLI option grammar and actual `--cwd`, not a launcher's
scratch directory or an option-like prompt/file value. Neutral reviewers receive
no controller authority assertions; their quiet output preserves the complete
structured result even when streaming events are suppressed. Retained fallback invokes
the old **executable**, not merely old scripts under the new executable. If that
complete runtime cannot be proved safe, the command refuses precisely instead of
promising availability.

Interrupted transactions block ordinary execution without mutation. Explicit
recovery is independent of old task admission:

```sh
yy scripts generation resume EXACT_TRANSACTION_ID
yy scripts generation rollback EXACT_TRANSACTION_ID
```

Recovery checks journal identity, current endpoints and independent changes.
Never remove a fence, overwrite scripts, rewrite a lease, retarget a product ref,
or reuse an unrelated recovery receipt to force admission.

## Explicit mixed-predecessor recovery

Older source adoption/rebind transactions may have moved the runtime while leaving
a previous instruction inventory. Normal explicit upgrade deliberately refuses this
mixed state; repeated global installation or force-copying files is not recovery.
The installed maintenance engine offers an exact, separately reviewed repair:

```sh
yy scripts generation repair-plan /external/new-plan.json
# Review the reported reviewRequired paths and exact before/after bytes privately.
# The plan is mode 0600 and may contain local Git configuration; do not publish it.
yy scripts generation repair-apply /external/new-plan.json EXACT_REVIEWED_PLAN_ID
yy scripts generation doctor
yy scripts doctor
yy integration runtime-doctor
```

Planning authenticates both installed artifacts, the registered runtime and valid
inventory structure, then retains the candidate offline before writing the exact
reviewable plan. This explicit preparation may create an external immutable
installation, but never changes controller bytes. The plan already binds that
retained executable; apply cannot silently substitute another installation.
It owns only package-declared CLI destinations. Explicit
apply backs up every replaced preimage in the transaction journal, refuses stale
inputs, preserves independent skills/unrelated files and task state, and uses the
same fenced rollback and operational readback as normal explicit upgrade. Customized
CLI-owned instructions may be replaced **only after this explicit review**; their
exact prior bytes remain in `previous/`. Automatic startup never chooses repair.
Malformed inventory, foreign occupied new destinations, bad package evidence,
unsafe paths and incompatible shared state still refuse.

Source adoption now reports `controller_generation`, `controller_ready` and a
safe next action separately from source-script admission. A completed source
transaction is not a claim that a mixed instruction generation is agent-ready.

Source adoption selects every command declared by the authenticated package's
`bin` manifest, including `ypl` and `feedback-yylo`, using its own target rather
than assuming all commands point to `yylo.sh`. Both endpoint installations must
retain `.yylo-generation-evidence.json` and their exact artifacts; this explicit
selector check never searches the npm cache. Missing, foreign, mixed or shadowed
launchers, and added/removed command sets, require installation review before
adoption. Matching version labels do not establish ownership.

A selector sidecar records exact before/after links before the first replacement.
Readback authenticates the candidate and verifies the complete set visible on
PATH. Failure rollback authenticates the predecessor and restores only links
still owned by the operation; an independent change is preserved and incomplete
rollback retains the candidate installation. Legacy adoption receipts without
complete command-set evidence refuse replay rather than claim all commands were
updated. Link replacements are individually atomic, not an atomic multi-link
switch; the existing source-adoption writer lock and recovery boundaries still
apply. This does not change ordinary global-to-retained dispatch or establish the
separate explicit-only startup policy.

## Source controllers versus consumer projects

Juno source targets still require exact source-runtime agreement and full
supported declaration admission. Their separate source-adoption transaction is
not replaced by a speculative package overwrite.

An activated consumer controller can instead admit its complete authenticated
controller-local generation. This does not rewrite the consumer's historical
tracked script/inventory or advance its product ref. The task reader bootstraps
only a tarball-authenticated captured maintenance/import closure; the shared
engine verifies registration, policy, full managed inventory, runtime identity,
state schemas and attempt-bound pins. Without an activated generation, legacy
target provenance/bootstrap admission remains strict.

Checkpointing selects eligible metadata, leaving unrelated untracked files and
independent skills untouched. Tracked product dirt and unsafe selected paths still
refuse. A checkpoint failure warns; it must not replace the primary agent's
stdout, failure or exit outcome.

## Explicit project validation and release gate

For changes to declarations, compatibility policy, migration, dispatch or
checkpoint boundaries, run these checks before task finish:

```sh
npm test -- src/utils/__tests__/instruction-bundle-compatibility.test.ts src/utils/__tests__/controller-generation-migration.test.ts src/utils/__tests__/controller-generation-startup.test.ts src/utils/__tests__/controller-generation-public.test.ts src/utils/__tests__/controller-checkpoint.test.ts src/utils/__tests__/controller-checkpoint-finalizer.test.ts src/utils/__tests__/controller-checkpoint-outcome.test.ts
npm run typecheck
npm run test:controller-upgrade
node scripts/verify-managed-assets.mjs --check-dist --check-pack
```

The packed gate installs offline in disposable registered fixtures and consumes an
unmodified npm artifact. It exercises source and consumer targets separately:
unchanged/refused ordinary launch before explicit activation, explicit upgrade and
same-artifact repeat, compatible future instruction revision, all runtime doctors,
clean frozen hydration, task finish, native merge, independent byte preservation,
and deterministic successful/failing agent processes with secondary checkpoint
failure. Separate engine/public-dispatch suites cover malformed/unsupported
state, occupied destinations, active pins, concurrent readers/writers,
interruption recovery, rollback and preserved retained-runtime exits.

The packed gate enumerates the artifact's complete `bin` manifest and requires a
safe probe/classification for every command. Both aliases exercise generation
admission; `ypl` reaches a deterministic external Pi substitute and refuses an
unsafe generation before that substitute starts (not merely `--help`). The
auxiliary feedback command is authenticated by its package mapping without
starting its service. The source fixture additionally runs the shipped
source-adoption **selector component**, injects each link-update failure, and
proves that a deliberately stale `ypl` cannot pass complete-set readback. This is
not a full source rebuild/adoption or process-kill recovery certification. Task
state, product target and authenticated installed bytes must remain intact.
Release readback rejects missing launcher coverage and binds both the JavaScript
gate and Python scenario implementation, as well as the exact artifact/source.

Predecessors in the reproducible matrix are **representative generated fixture
artifacts**, including historical `juno-code 2.1.3-rc.0.32` identity—not purported
published historical tarballs. Ledger and the model process are deterministic
external fixture adapters. These tests do not certify an unavailable historical
binary, actual model behavior, production topology, or a particular percentage
of operator/code reduction.

Maintainer release preparation runs the full CLI tests, then gates the exact
packed artifact using:

```sh
node juno-code/scripts/verify-controller-upgrade.mjs \
  --artifact /external/yylo-cli-VERSION.tgz \
  --report /external/new-controller-upgrade-acceptance.json
```

The immutable report binds the artifact SHA-256, source SHA/cleanliness and gate
implementation hash. Maintainer preparation defaults to
`${XDG_STATE_HOME:-$HOME/.local/state}/yylo/release-artifacts/TAG-SHA`;
`YYLO_RELEASE_ARTIFACT_DIR` overrides must also remain outside Git worktrees.
Release manifest v2 binds that report; publish/verify reject
missing, dirty-source, stale or hash-mismatched evidence. Native merge neither
launches reviewers nor runs this gate. Package publication, push and deployment
still require separate authority. Store run outcomes in Ledger, not this guide.
