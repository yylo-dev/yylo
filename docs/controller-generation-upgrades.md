# Controller generation upgrades

Scripts remain bundled with the CLI and copied into the registered metadata
controller. A package generation owns scripts, instruction identity, managed
inventory, controller policy and executable selection together. Updating just one
of these is not recovery.

## First use and diagnosis

Normal task/merge execution and agent startup assess the generation before local
runtime selection. Exact recognized preimages migrate automatically, without a
prompt or network installation. Read-only commands do not migrate:

```sh
yy scripts generation doctor
yy scripts doctor
yy integration runtime-doctor
```

After activation these doctors use the same package-generation assessment as
startup. Workspace topology checks remain separate. A prospective migration is
not a claim that an old controller has already been upgraded.

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
unique candidate artifacts; a larger cache requires an explicit artifact-bound
installation rather than unbounded startup work.
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

Interrupted owned transactions are resumed on first execution. Explicit recovery
is independent of old task admission:

```sh
yy scripts generation resume EXACT_TRANSACTION_ID
yy scripts generation rollback EXACT_TRANSACTION_ID
```

Recovery checks journal identity, current endpoints and independent changes.
Never remove a fence, overwrite scripts, rewrite a lease, retarget a product ref,
or reuse an unrelated recovery receipt to force admission.

## Explicit mixed-predecessor recovery

Older source adoption/rebind transactions may have moved the runtime while leaving
a previous instruction inventory. Automatic upgrade deliberately refuses this
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
inventory structure. It owns only package-declared CLI destinations. Explicit
apply backs up every replaced preimage in the transaction journal, refuses stale
inputs, preserves independent skills/unrelated files and task state, and uses the
same fenced rollback and operational readback as automatic migration. Customized
CLI-owned instructions may be replaced **only after this explicit review**; their
exact prior bytes remain in `previous/`. Automatic startup never chooses repair.
Malformed inventory, foreign occupied new destinations, bad package evidence,
unsafe paths and incompatible shared state still refuse.

Source adoption now reports `controller_generation`, `controller_ready` and a
safe next action separately from source-script admission. A completed source
transaction is not a claim that a mixed instruction generation is agent-ready.

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
automatic migration, compatible future instruction revision, all runtime doctors,
clean frozen hydration, task finish, native merge, independent byte preservation,
and deterministic successful/failing agent processes with secondary checkpoint
failure. Separate engine/public-dispatch suites cover malformed/unsupported
state, occupied destinations, active pins, concurrent readers/writers,
interruption recovery, rollback and preserved retained-runtime exits.

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
