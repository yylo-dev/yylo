# Consumer upgrade boundaries and troubleshooting

An installed consumer has no YYLO source tree. Its product runtime, metadata
controller, installed CLI, Ledger executable, configuration, and active tasks
have distinct identities. Installing a newer CLI does not automatically upgrade
all of them. Do not copy a fake `juno-code` directory into a consumer or disable
provenance checks to make an upgrade pass.

## Inspect before changing anything

Use the intended CLI's absolute installed launcher when multiple installations
exist. Record its version and executable identity, then inspect:

```sh
yy where controller
yy info --json
yy integration status
```

Preserve controller metadata, registration, runtime receipts, task creation
receipts, managed inventory, configuration, product HEAD/history, dirty files,
and notebook/dataset bytes. Store checksummed backups outside the workspace
without exposing secrets. A clean controller is not permission to discard
untracked product files. Active task owners must retain their frozen runtime
identity or receive an explicit fenced disposition before maintenance.

## Choose the right boundary

| Symptom | Meaning and safe next step |
| --- | --- |
| Role exists but controller mode/registration is incomplete | Inventory and obtain reviewed registration recovery; product-file absence alone is not authority |
| Installed CLI differs from registered runtime | Use explicit receipt-bound installed-runtime rebind only after config and ownership admission |
| Target runtime is absent or stale | Inspect the consumer-specific runtime-bootstrap plan; do not assume it covers a complete cross-version transition |
| Provenance plan rejects different package/runtime bytes | Correct refusal: provenance repair authenticates matching bytes, not arbitrary version upgrades |
| Consumer finish demands a `juno-code/src/templates` twin | Stop: the source-only coherence path needs installed-consumer admission; never add a fake source tree |
| New merge verb reaches an old parser | CLI/script compatibility is not established; do not fall back to retired FIFO delivery |
| Product-only fields appear in metadata-controller config | Preserve the config and obtain schema-classified migration; do not silently delete or activate those fields |
| Selected Node directory changes nested `yy` lookup | Bind the intended CLI separately from Node and inspect the child environment; a matching version string is insufficient |
| Managed child lacks credentials | Select/authenticate the intended provider explicitly; never fabricate approval or substitute an arbitrary model |
| Controller became dirty during managed admission | Separate task-owned metadata from unrelated bytes and preserve both; checkpoints are durability aids, not evidence of product correctness |

The narrow consumer inspection surface is:

```sh
yy task runtime-bootstrap --dry-run
```

Review its exact registered-controller class, target SHA, installed package,
changed paths, inventory identity, rollback evidence, and any refusal. Apply
only a supported, separately authorized exact receipt. A source-development
runtime adoption is not a substitute for an installed-consumer upgrade.

For a fully admitted clean metadata controller, an explicit installed-runtime
rebind can select an already acquired external package:

```sh
yy migrate runtime-rebind --root <controller> --branch refs/heads/juno/controller-metadata --runtime <external-package>/dist/bin/cli.mjs --runtime-version <exact-version> --output <external-receipt.json>
```

This is not proof that tracked runtime bytes, Ledger storage, active producers,
or legacy controller configuration are compatible. Consult the selected
release's migration command inventory; never guess a missing cross-version
step from the command name. No complete universal 0.2.2-to-RC3 consumer upgrade
transaction has been verified. Unsupported transitions must stop with preserved
bytes rather than repeat forced updates, identical refreshes, or old merge verbs.

## Required acceptance for a supported transition

Installed-consumer coherence must prove matching controller/task executable
identity, receipt hash, external installed-release package name/version, exact
candidate/package-template bytes, and matching script inventory type/version/
hashes. Source projects must retain strict source/runtime twin validation.
Missing, conflicting, malformed, or escaping identities must fail closed.

Command compatibility must be checked before dispatch. Configuration migration
must classify every legacy field, preserve permitted provider/model settings,
keep credential references secret-safe, reject unknown fields, and validate with
the actual target loader. Child CLI identity must survive Node/PATH normalization
and nested invocations. Ledger compatibility is an independent exact runtime
check; stale skills and unrelated global installations are not release evidence.

Validate using disposable real-Git consumers without a source tree, exact old/new
installed artifacts, separate controller/task/integration worktrees, and a
configured local test provider. Cover interruption and stale-plan boundaries,
unrelated dirty bytes, missing credentials, mixed versions, and successful
native Artifact creation/readback with immutable IDs and exact Markdown bytes.
Task records are not substitutes for native artifacts. Diagnostic mechanism
probes are not end-to-end upgrade acceptance.

After verified Git integration, retry only failed Ledger projection; never
repeat integration to repair bookkeeping. Rollback after new Git or Ledger
writes must preserve intervening history and records. Replacing a backup over
new writes or rewriting bound receipts is not safe rollback. Publication,
consumer migration, credential changes, and cleanup require separate authority.
