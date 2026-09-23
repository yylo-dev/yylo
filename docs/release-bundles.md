# Exact release bundle preparation

A `yylo_release_bundle.v1` manifest is a portable description of exactly four
artifacts: `@yylo/cli`, `yylo-ledger`, `@yylo/benchmark`, and `yylo-skills`.
Each component has one canonical exact SemVer, HTTPS artifact URL, SHA-256,
byte length, and source repository/commit. Bundle version equals CLI version;
unchanged component releases may be reused. Installation paths, active state,
receipts and secrets are not portable fields.

The CLI's existing skills compatibility range may admit the selected skills
release during preparation, but the manifest records only its exact version.
No latest-tag selection, dependency solver, cache scan, download, or component
execution occurs in the bundle preparer.

## Maintainer preparation

First use the existing reviewed two-package release preparation. This still
owns CLI/benchmark builds and the current packed controller gate. That gate also
requires task-local Ledger source at the committed `juno_kanban` gitlink. Select
it at task creation where possible. For an existing worktree missing this
read-only test dependency, explicit `git submodule update --init --checkout
--no-fetch -- juno_kanban` materializes the pinned checkout; verify its HEAD
against `git ls-tree HEAD juno_kanban` and verify both checkouts remain clean.
This grants no Ledger edit authority, does not change the gitlink, and must not
reuse another worktree's dependency tree. If required objects are unavailable,
obtain the required checkout through authorized dependency preparation rather
than weakening the packed gate. With the
CLI built, prepare a new bundle from reviewed exact component release evidence:

```sh
scripts/release-cli.sh bundle CLI_VERSION BENCHMARK_VERSION \
  /external/reviewed-bundle-input.json /external/new-bundle.json
```

The private input has exactly two keys:

- `bundle`: the portable manifest below;
- `artifacts`: an object with exactly `cli`, `ledger`, `benchmark`, `skills`,
  each an absolute canonical local path to the corresponding retained artifact.

The preparer verifies all artifact hashes and lengths, authored CLI compatibility
expectations, CLI/benchmark package locks, and the existing prepared release's
CLI/benchmark identities. It also inspects the archives without extracting or
executing their contents: npm `package/package.json`, wheel `.dist-info/METADATA`,
and the skills archive's root `VERSION` plus `.claude-plugin/plugin.json`.
Packed CLI dependency declarations must agree with the selected bundle. Duplicate
members, unsafe paths, special members, oversized metadata and stale component
versions refuse. Inspection has artifact, member-count, expanded-size and process
time bounds. It refuses missing, changed or symlinked artifacts, Git-contained
output destinations and overwriting an existing output. Local artifact paths
are never emitted in the portable manifest.

The typed contract is `src/utils/release-bundle.ts`. The shape is:

```text
schema_version: yylo_release_bundle.v1
version: exact CLI version
controller_generation: {ordinary_dispatch: explicit-only-v1}
acceptance: {url, sha256, bytes}  # optional until qualification
components:
  cli:       {name, version, url, sha256, bytes, source: {repository, commit}}
  ledger:    {name, version, url, sha256, bytes, source: {repository, commit}}
  benchmark: {name, version, url, sha256, bytes, source: {repository, commit}}
  skills:    {name, version, url, sha256, bytes, source: {repository, commit}}
```

URLs must be HTTPS without credentials, queries or fragments. A digest binds the
bytes, not the origin: maintainers must review artifacts against trusted release
or registry evidence. The preparer does not independently certify URLs are
published or prove source provenance. Archive identity checks are not a malware
scan or proof that a package is safe to execute. Metadata claims alone are never
permission to execute a package.

The preparation result is explicitly `prepared_not_qualified`, with `published:
false` and `activated: false`. The declared ordinary-dispatch capability must
match both the source and packed CLI; this describes the existing explicit-only
contract, not a claim that software-only activation has already been implemented.
Existing `yylo_two_package_release.v2` manifests retain their current meaning
and are not silently promoted to bundle manifests.

## Explicit publication and acceptance verification

```sh
scripts/release-cli.sh bundle-verify CLI_VERSION BENCHMARK_VERSION \
  /external/bundle.json
```

This read-only maintainer operation uses bounded HTTPS requests, not npm/pip
installation or cache discovery. It verifies every selected artifact is available:

- CLI/benchmark: canonical npm registry name/version/tarball and SHA-512 integrity;
  published `gitHead`, when present, must agree with the declared source commit.
- Ledger: canonical PyPI release, non-yanked wheel URL/size/SHA-256.
- Skills: upstream `yylo-dev/yylo-skills` version tag resolves to the exact source
  commit, including an annotated-tag hop; download the canonical codeload archive
  at that commit. A mutable tag archive is not an artifact identity.
- Every downloaded artifact must also match the bundle's exact SHA-256 and size.

Missing releases, unavailable dependencies, conflicting hashes, unsafe origins
or redirects refuse. There is no implicit source fallback or automatic publication.
Registry-origin authentication is not a reproducible-build attestation: source
commits remain reviewed release provenance, not proof reconstructed from a wheel.

Without an acceptance reference the result is
`registry_available_not_qualified`, `ready_to_upgrade: false`, exit 2. To qualify,
the manifest must bind an exact published acceptance report. Its inert JSON shape
is:

```text
schema_version: yylo_bundle_upgrade_acceptance.v1
outcome: passed
coverage: four-component-upgrade.v1
bundle_inputs_sha256: SHA256 of canonical bundle inputs
source: {sha: exact prepared source commit, dirty: false}
gate_sha256: SHA256 of the ordered controller-upgrade gate implementation files
```

`bundleInputsDigest` in the shared module computes the canonical input digest:
version, controller-generation capability, and all four complete component
identities. The acceptance reference is excluded to avoid a self-hash cycle.
`gate_sha256` uses the existing release gate's ordered JavaScript/Python source
pair. The verifier binds source and CLI/benchmark identities to the existing
prepared release, independently rechecks all public artifact bytes, then verifies
the report reference, full input digest, clean source and gate implementation.
Changed evidence, partial publication, CLI-only coverage or stale input/gate hashes
cannot produce `acceptance_qualified` / `ready_to_upgrade: true`.

The existing CLI-only packed gate does **not** produce this four-component report;
that real-component qualification producer belongs to the downstream packed-release
gate work. Do not fabricate or relabel its report. The report reference must come
from reviewed maintainer execution evidence, not from arbitrary untrusted JSON.
This task supplies its verification contract, not the missing end-to-end producer.
The verifier never installs, activates, writes project pins or publishes anything.

## Portable desired pin

`yylo_release_pin.v1` contains only `version` and
`manifest: {url, sha256}` plus `schema_version`. The hash binds exact manifest
bytes, not a reserialized equivalent. The pin must be obtained through reviewed
project intent or trusted release evidence. A hash supplied alongside untrusted
bytes does not establish trust. Parsers bound pin/manifest input to 64 KiB,
reject unknown fields and verify version agreement.

This module defines the contract; it does not yet install a shared project pin
or change runtime selection. A future consumer must keep desired state separate
from machine-local activation and must not infer incompatibility merely from a
different pin. Ordinary startup, Ledger storage and installed controllers are
unchanged by this preparation foundation.
