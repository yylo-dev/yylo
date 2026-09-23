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
owns CLI/benchmark builds and the current packed controller gate. With the
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
CLI/benchmark identities. It refuses missing, changed or symlinked artifacts,
Git-contained output destinations and overwriting an existing output. Local
artifact paths are never emitted in the portable manifest.

The typed contract is `src/utils/release-bundle.ts`. The shape is:

```text
schema_version: yylo_release_bundle.v1
version: exact CLI version
components:
  cli:       {name, version, url, sha256, bytes, source: {repository, commit}}
  ledger:    {name, version, url, sha256, bytes, source: {repository, commit}}
  benchmark: {name, version, url, sha256, bytes, source: {repository, commit}}
  skills:    {name, version, url, sha256, bytes, source: {repository, commit}}
```

URLs must be HTTPS without credentials, queries or fragments. A digest binds the
bytes, not the origin: maintainers must review artifacts against trusted release
or registry evidence. The preparer does not independently certify URLs are
published, extract/check archive contents, or prove source provenance. Metadata
claims alone are never permission to execute a package.

The result is explicitly `prepared_not_qualified`, with `published: false` and
`activated: false`. Existing CLI-only acceptance is **not** a four-component
acceptance result. Bundle publication, qualification against actual components,
and consumer activation are separate work; this command provides none of them.
Existing `yylo_two_package_release.v2` manifests retain their current meaning
and are not silently promoted to bundle manifests.

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
