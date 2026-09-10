# YYLO release notes

## 0.2.3-rc.2 (local candidate)

Companion package: `@yylo/benchmark@0.1.1-rc.2`.

Comparison baseline: local `@yylo/cli@0.2.3-rc.1` and
`@yylo/benchmark@0.1.1-rc.1`. This is a local candidate only: no npm
publication, Git tag, GitHub release, push, or deployment is implied.

### Issues and capabilities to verify

- **CLI skills use the canonical suffixed set.** Command routing, installed
  skill names, argument contracts, and task-workspace admission expectations
  now target the independently versioned suffixed skill set instead of stale
  unsuffixed package copies (`cbd382d68`).
- **Fresh initialization creates a healthy workspace topology.** `yy init` now
  bootstraps the managed controller registration and local topology required by
  `yy info`, workspace doctor, and ordinary commands in a new Git repository
  (`80cdfd22d`).
- **Retired release-train assets stay retired.** Candidate packages no longer
  copy removed release-train templates or emit their tombstones as package
  payload (`036df69c7`, `90cdfa247`, `c6f3db6be`).
- **Template package tests retain their explicit mode.** Retirement cleanup no
  longer strips the package-template test-mode contract (`f25f2e12f`).
- **Template copying invokes the correct script entry.** The build now executes
  the intended template copier and verifies the resulting candidate payload
  (`2d6be606c`).
- **Lifecycle routing covers resumable operations.** The launcher routes the
  registered `task resume` and `merge resume` commands through the same fenced
  orchestration boundary as their CLI registrations.
- **Exact-base lock updates remain merge-admissible.** Merge composition admits
  the package-lock changes produced from the frozen exact base (`6d540e520`).
- **Candidate guidance and managed identity match the shipped generation.**
  Exact CLI/Benchmark install examples, the controller inventory checksum and
  package identity, and the target-bound recovery fixture are refreshed after
  the post-rc.1 changes so identity/parity checks accept the coherent pair.
- **Release-suite fixtures are deterministic under host load.** Benchmark's
  TERM-resistant descendant test allows enough startup time for PID evidence;
  CLI hard-kill and detached-descendant fixtures wait for complete/observable
  process evidence; color capability tests explicitly isolate `FORCE_COLOR`.
- **Benchmark remains the version-locked companion.** Aside from that test-only
  release-gate repair, Benchmark has no product code delta from rc.1 and is
  rebuilt and packed as `0.1.1-rc.2` so the local pair has one unambiguous
  candidate identity.

### Candidate acceptance checklist

- CLI and Benchmark tests, typechecks, and builds pass from exact lockfiles on
  Node 22; managed-asset, skill-contract, and fresh-init package checks pass.
- Packed artifact identities are exactly `@yylo/cli@0.2.3-rc.2` and
  `@yylo/benchmark@0.1.1-rc.2`, with SHA-256 checksums recorded outside Git.
- Both local tarballs install globally and `yy --version` plus
  `yylo-benchmark --version` resolve from the active npm prefix.
- A unique disposable `/tmp` Git repository passes non-interactive `yy init`,
  `yy info --json`, `yy doctor workspace`, CLI/Benchmark help delegation, and
  `yy watch exec pwd` without model or provider dispatch.

## 0.2.3-rc.1 (local candidate)

Companion package: `@yylo/benchmark@0.1.1-rc.1`.

Comparison baseline: published `@yylo/cli@0.2.2` and
`@yylo/benchmark@0.1.0`. This is a local candidate only: no npm publication,
Git tag, GitHub release, push, or deployment is implied.

### Issues and capabilities to verify

- **Fresh initialization keeps consumer configuration local.** New managed
  projects no longer inherit source/controller configuration that belongs to
  the package repository (`b06fb2005`). Verify with `yy init` in a fresh Git
  repository and inspect `yy info --json` and generated `.juno_task` files.
- **Fresh integration workspaces can bootstrap their registration.** The
  integration setup path can establish the expected workspace registration
  instead of requiring pre-existing local state (`9035e872f`).
- **Task admission is classified before runtime bootstrap.** Unsupported or
  mismatched admissions now fail on their actual admission reason rather than
  being obscured by bootstrap handling (`6e9ab1bd6`).
- **Task submissions have one immutable identity and coherent evidence.** Task
  submission/evidence handling was consolidated, unreachable handoff surfaces
  were removed, and focused evidence is cache-free (`68ac8f32a`, `9ac201432`,
  `9f5691a72`, `35861ca21`).
- **Lifecycle completion is resumable.** Landed finalization and task resume now
  route through the fenced owner and preserve synchronized runtime/template
  contracts (`a99853787`, `cf46cfb76`, `803d2666e`).
- **Umbrella work uses delivery checkpoints.** Ordinary task consumers now use
  the simplified lifecycle; eligibility is visible, repeated deterministic
  failures are suppressed, and queue review retains advisories without
  over-owning implementation (`95486d572`, `3b27f4583`, `3125df097`,
  `32382632a`).
- **Managed asset identity is canonical.** Runtime asset identities and
  inventories were refreshed so installed/controller parity checks compare the
  intended generation (`0b7a43c14`, `e6d80305b`).
- **YYLO skills are independently versioned.** Skills move to the canonical
  `yylo-skills` source/submodule and remote acquisition boundary instead of
  stale package-bundled copies (`afa142b5d`, `0490b5afd`, `2e9faa1b6`,
  `f90f8d2cb`).
- **Workspace relocation is receipt-bound and portable.** Relocation preserves
  explicit authority and authored-path admission across host layouts
  (`ff932becd`, `40abe12d8`).
- **Native Ledger controller state is checkpointed.** Controller policy and
  native Record storage inventory are bound consistently (`fcac78d7b`,
  `aec2a0778`).
- **Benchmark governed workflow CLI is restored.** The companion Benchmark
  candidate exposes the governed workflow command path again (`d43408558`).
- **Benchmark sandbox failures are actionable.** Unsupported or unavailable
  filesystem sandboxing now reports the actual environment limitation rather
  than an ambiguous execution failure (`55548d42c`).

### Candidate acceptance checklist

- CLI and Benchmark tests, typechecks, builds, package parity, and packed-pair
  checks pass from exact lockfiles on Node 22.
- Packed artifact identities are exactly `@yylo/cli@0.2.3-rc.1` and
  `@yylo/benchmark@0.1.1-rc.1` with recorded checksums.
- Both tarballs install globally and `yy --version` / `yylo-benchmark --version`
  report the candidate versions from the expected npm prefix.
- A unique disposable `/tmp` Git repository passes `yy init`, `yy info --json`,
  `yy doctor workspace`, help delegation, and `yy watch exec pwd` without model
  or provider dispatch.

Test-only fixture, timeout-budget, managed-inventory, and documentation commits
are intentionally not presented as separate user-facing fixes; they support the
checks above.
