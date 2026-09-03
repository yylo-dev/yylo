# Test performance: benchmark profile and cold-setup elimination (Wave 1)

This document describes the Wave 1 delivery of the trusted-test performance
PDR (`7djT8N`): a reproducible benchmark profile, Node-by-default test
environments, an explicit retry quarantine policy, and content-addressed
immutable fixture bases that remove per-invocation venv and real-Git
controller construction.

Wave 2 (persistent advisory test daemon, warm affected edit loop, and the
warm/cold equivalence matrix) is documented in [test-daemon.md](./test-daemon.md).

## Task-workspace fixture modes

Use `npm run test:task-workspace:affected`, `:seeded`, `:hermetic`, or
`:complete`. Complete is authoritative and always runs the full inventory;
passing `--test-id` in complete mode is terminally ineligible with a truthful
`complete_requires_full_inventory` receipt. Use seeded or hermetic mode for an
explicit partial replay. Receipts include inventory, tiers, shards,
Git-process counts, timeout/process settlement, and p50/p95 timing. Seeded
failures include a hermetic replay command. The umbrella `npm test` suite checks
runner and representative real-Git contracts but does not launch `:complete`;
the explicit complete command is the single task-owned 239-case performance
gate, preventing duplicate expensive profiles and resource-lock races. The
complete gate is strict: a settled, otherwise eligible 239/239 profile must
finish in less than 150 seconds; 150.000 seconds is ineligible.

Arbitrary `--command` probes are cleanup diagnostics, not eligible evidence:
POSIX process groups cannot contain a descendant that creates a new session,
and a sampled process inventory cannot prove that no short-lived parent launched
such a descendant. The runner therefore records
`containment:unavailable_for_arbitrary_command` and exits nonzero after bounded
best-effort reconciliation instead of claiming settlement. Managed built-in
profiles use the bounded process-group and process-instance cleanup path and
remain eligible when that verification settles. On Windows, cleanup opens a
process handle, verifies creation identity on that handle, and terminates that
same handle; it never signals a previously verified bare PID.

The Python boundary extends the canonical `juno.test.fixture.base.v1` identity
and gives every consumer a disposable private instance. Drift or corruption
quarantines a published base and builds a new one; it is never repaired in
place. Force the permanent cold fallback with
`YYLO_TEST_DISABLE_FIXTURE_BASE_CACHE=1 npm run test:task-workspace:complete`.
Failed, skipped, timed-out, or inventory-drifted receipts are not performance
evidence. Host load and comparability metadata remain in the receipt only as
diagnostics: they never alter eligibility or exit status, and the runner never
waits or polls for an idle host.

## Benchmark profile

The harness `scripts/test-performance/benchmark-profile.mjs` measures one
command several times and emits a strict `juno.test.performance.profile.v1`
artifact that separates process startup, transform/collection, environment,
global setup, teardown, and receipt finalization, and records environment
identity (platform, Node/Python versions, CPU count), dependency-lock and
runtime identities (package-lock sha256, vitest version, vitest config and
global-setup digests), p50/p95 summaries per phase, and bounded references to
the retained raw logs (logs are truncated at 256 KiB and referenced by path,
size, and sha256 rather than committed).

Reproduce a focused-gate profile:

```bash
node scripts/test-performance/benchmark-profile.mjs \
  --label focused-task-workspace \
  -- npm test -- src/utils/__tests__/environment.test.ts
```

Reproduce the pre-cache cold fixture-construction baseline:

```bash
sh scripts/test-performance/probe-cold-fixture.sh
```

Artifacts are written to `test-results/performance/<id>.json` (gitignored;
reference them by digest when recording claims). The profile is deliberately
machine-bound: report the artifact id, environment identity, and phase
summaries together, never a bare wall-clock number.

## Node-by-default environments

`vitest.config.ts` sets `environment: 'node'`. The suite has no browser-DOM
dependence — every historical `document`/`window` reference in tests is a
local JSON-document variable — so Node-only runs no longer load `happy-dom`.
Files that genuinely need a DOM opt in with a docblock:

```ts
// @vitest-environment happy-dom
```

`src/utils/__tests__/fixtures/happy-dom-probe.test.ts` proves the opt-in
still yields a DOM, and `src/utils/__tests__/vitest-policy.test.ts` pins the
default plus the opt-in contract.

## Retry policy

Ordinary failures execute exactly once (`retry: 0`). Retries exist only as an
explicit, reported quarantine: `YYLO_TEST_QUARANTINE_RETRIES=<n>` (integer in
`[0, 5]`) emits a `[quarantine] ... advisory-not-first-pass` marker into the
structured output. Lifecycle admission argv never sets the variable, so a
retried pass can never become an eligible first-pass receipt.

## Content-addressed fixture bases

`src/test-utils/fixture-base-cache.ts` replaces the per-invocation
Python-venv + fixture-controller construction in
`src/test-utils/global-setup.ts` with:

- a base key — SHA-256 over the dependency lock, fixture schema,
  implementation/admission contract digests, Python interpreter identity, and
  Node runtime generation — so any drift materializes a new base and stale
  bases are never reused;
- immutable bases published by atomic rename and sealed read-only
  (`yylo-fixture-base.json` manifest with content digests; attempted mutation
  fails at the filesystem level; corruption is detected by digest and the
  damaged base is quarantined, then rebuilt);
- a disposable writable overlay per run, copied from the sealed base, with
  cleanup that is path-scoped to exactly this run's overlay — it can never
  delete the shared base, a foreign cache entry, or another process's overlay;
- serialized materialization through claim files with dead-owner recovery for
  concurrent cold starts;
- an explicit cold fallback (`YYLO_TEST_DISABLE_FIXTURE_BASE_CACHE=1`, or an
  unusable cache root) that preserves the pre-cache construction semantics.

Bases live under `$(realpath tmpdir)/yylo-fixture-bases/` and overlays under
`$(realpath tmpdir)/yylo-fixture-overlays/`. The git mutation sentinel and
all fixture environment semantics are unchanged; only construction cost is
removed. Phase instrumentation is available to the harness through
`YYLO_TEST_GLOBAL_SETUP_PHASE_REPORT=<path>`.

## Task-workspace functional-core pilot (Wave 3)

`task_workspace_decisions.py` now owns the pure state/command, path-admission,
validation-routing, standing-evidence reuse, failure, and FIFO decisions. The
existing `task_workspace.py` remains the imperative shell for physical identity,
Git and filesystem I/O, locks, subprocess dispatch, receipt persistence, and CLI
rendering. Runtime and package-template copies are managed parity twins.

The pure table suite forbids filesystem/process/socket/clock access and covers
the complete lifecycle state × command matrix. Configured focused validation
runs that suite plus one real-Git finish/validation/receipt/FIFO adapter canary;
the comprehensive real-Git scenarios and installed-package canaries remain in
the full suite. No admission schema or public CLI behavior changed.

Controlled macOS profile (Node 22.22.3, Python 3.13.9, five measured runs after
one warm-up):

- pure decision tables: p50 107.312 ms, p95 108.629 ms; artifact SHA-256
  `ce0b457236bbad67fe7dc4a779529717227d0a703863b531720f53606c256eed`;
- real-Git adapter canary: p50 2518.448 ms, p95 14566.675 ms; artifact SHA-256
  `a89496d120c2e6a61b92ff317b23444c8f25b9d00df8f5a942e7d99c2537ddfd`.

Reproduce the profiles from `juno-code/`:

```bash
node scripts/test-performance/benchmark-profile.mjs --label wave3-pure-decisions \
  --cwd .. --repetitions 5 --warmup 1 -- \
  python3 .juno_task/scripts/tests/test_task_workspace_decisions.py
node scripts/test-performance/benchmark-profile.mjs --label wave3-adapter-canary \
  --cwd .. --repetitions 5 --warmup 1 -- \
  python3 .juno_task/scripts/tests/test_task_workspace.py \
  TaskWorkspaceTests.test_finish_queues_clean_committed_tip_without_merging_or_cleanup
```

The pilot supports extracting similarly stable pure decisions from merge-queue
code later, but current evidence does not justify absorbing that high-risk,
broad refactor into Wave 3. Reassess only with a separate task and measured
merge-queue profile.
