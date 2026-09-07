# Native Records aggregate acceptance (6Uf5mO)

Date: 2026-08-28  
Verdict: **PASS — authorized source/fixture migration gate open**

This verdict applies to the frozen protected parent target
`9ca943d95cb24cc06b79d72ec7945eaa48638e13` and its exact Ledger gitlink
`b07ae587291f26249cfab419b102f34ca850de25`. The task branch preserves the
original refusal review at `8ede12e96dc15834e1e2b7ed6671961ba1f69a31` and
history-preservingly merges the protected target. The reviewed source versions
are `yylo-ledger 0.2.0` and `@yylo/cli 0.2.0`; the pre-existing public CLI used
for benchmark smoke/probe identity reports `yylo-ledger 0.1.0rc2`.

**PASS is not release authorization.** Release/RC artifact verification, wheel
or RC build, package installation, publish, push, deploy, production archive,
live-controller corpus mutation, and lifecycle cleanup were excluded and were
not attempted.

## Protected composition and repaired findings

| Finding from the refusal | Outer commit | Exact nested commit | Disposition |
|---|---|---|---|
| Archived replacement checked reserved creation metadata before immutable cold authority | `cebc44c45` | `0cf9fe0199e8a7eb595e4849127f3ff2046ec856` | Repaired; exact test passes |
| CLI mutation receipt omitted `operation`; positional legacy archive returned no archived Task | `3c022ac8c` | `81c84f177dc4b0ad775be145bacbd226f56c8302` | Repaired; both exact tests pass |
| Benchmark generated noncanonical decimal IDs/paths instead of canonical shards | `aa2d53ff8` | `6ada6dcea09e9f3e380c7d60a4968b1736099ee5` | Repaired; 14k benchmark passes all gates |
| Composition of all repairs | `9ca943d95cb24cc06b79d72ec7945eaa48638e13` | `b07ae587291f26249cfab419b102f34ca850de25` | Frozen candidate validated |

The previous report counted six Ledger failures. The corrected interpretation is
**three product failures and three false positives**. The project-registry
failures were caused by the review command's global
`YYLO_LEDGER_INVOCATION_ROOT=/tmp` override, which changed the registry source
contract. With no override, all three project-registry tests pass. The three
product failures above were repaired before this continuation.

The exact protected commits and content-addressed lifecycle closure now provide
the task/repair disposition evidence. Empty response text is not treated as a
semantic blocker when that closure binds the admitted target, nested gitlink,
repair commits, and validation candidate.

## Delivery and PDR requirement matrix

| Requirement / wave | Evidence on exact candidate | Result |
|---|---|---|
| Wave 1: immutable ID, slug/aliases, typed relations, revision provenance and ID-first exact replacement | Foundation/storage/CLI unit and integration suites in full Ledger run | PASS |
| Wave 2A: Markdown wiki and YAML workflow profiles, front-matter import/export, schema validation and safe projection | Document, profile, front-matter, workflow and HTTP tests | PASS |
| Wave 2B: inline, local CAS, external-digest and link-only Artifacts; immutable revisions and explicit capture | Artifact/content-object tests and Wave 5 public acceptance | PASS |
| Wave 3: verified hot/cold authority, exact cold reads/history, disposable SQLite rebuild and scoped canonical search | Archive/cache/recovery tests, repaired archive-order test, Wave 5 storage acceptance | PASS |
| Wave 4: generic/typed CLI, canonical ID output, no remove API, safe read-only host | CLI/completion/hosting tests and focused Juno delegation tests | PASS |
| Wave 5P: automatic controller/project full-HEAD capture, shared/distinct roles, clean/dirty/detached/non-Git behavior | Git creation-context unit tests and both Wave 5 acceptance files | PASS |
| Creation context is immutable, sanitized, separate from completion `commit_hash`, and persists hot/cold/search | Creation, revision, archive, search and projection tests | PASS |
| Strict creation fails atomically when required Git context is absent/unstable | Git race/refusal fixtures in full suite | PASS |
| A recorded clean HEAD is checkout-reproducible | Wave 5 storage checkout fixture | PASS |
| Dirty Git capture does not claim or store dirty bytes | Wave 5 storage fixture | PASS — when `worktree_dirty=true`, only committed HEAD is reproducible; uncommitted bytes are not |
| Exact updates are compare-and-replace, never blind; absent/ambiguous/stale failures preserve canonical bytes and history | Foundation, concurrency and acceptance tests | PASS |
| Slug/alias input resolves to exactly one immutable ID before mutation; relations never store slugs | Foundation and CLI suites | PASS |
| Archives have one verified tier, cannot reopen/mutate, and remain readable after cache deletion/corruption | Archive suites plus repaired archived-replacement ordering test | PASS |
| SQLite is rebuildable and never sole truth | Cache rebuild, corrupt-cache and canonical cold-source fixtures | PASS |
| Legacy Task/archive-status/sealed-pack, conversion, rollback and doctor compatibility | Full Ledger suite, including migration/rollback/doctor fixtures; retained focused migration selection from the initial review | PASS |
| Unified typed search has hot/archive/all scopes, stable cursors, canonical verification, redaction and bounded output | Search suites and Wave 5 scale fixture | PASS |
| HTTP negotiation/render/range/redirect/error behavior is safe and read-only | Wave 5 public acceptance | PASS |
| No Record remove API, automatic run capture, workflow execution, arbitrary executable profiles, secret/reasoning capture, unsafe host mutation, or SQLite-only authority | CLI inventory, source path inventory and negative/security fixtures | PASS |

The search acceptance fixture covers 241 records, disjoint 30-record cursor pages,
a 32,000-byte output cap, restricted title/payload redaction, stale-index refusal,
and a five-second fixture budget. The HTTP fixture covers a 4,096-byte host cap,
a four-byte range cap, CSP/script escaping, unsafe redirect and method refusal,
and byte-identical canonical files before/after reads.

## Authorized aggregate commands and results

| Command | Result |
|---|---|
| `cd juno_kanban && python3 -m pytest -q` | PASS: **712 passed, 1 skipped**, 1 deprecation warning, 246.28 s. No `YYLO_LEDGER_INVOCATION_ROOT` override. |
| Exact archive replacement, complete receipt, positional archive, and three project-registry test nodes | PASS: **6 passed**, 1 warning, 2.67 s |
| `pytest -q tests/acceptance/test_record_storage_invariants.py tests/acceptance/test_public_record_surfaces.py` | PASS: **13 passed**, 2.65 s |
| Focused `juno-code` Ledger command/binary/facade/runtime tests | PASS: **3 files, 14 tests**, 5.37 s |
| `cd juno-code && npm run typecheck` | PASS |
| `cd juno-code && npm run build` | PASS, including skill and implementation-contract checks |
| Declared 14k source benchmark using `PYTHONPATH=$PWD/src`, the source benchmark script, and the existing public CLI for smoke/probe | PASS: all nine gates |

## Benchmark report identity

- Report: `/tmp/6Uf5mO-benchmark-14k.json`
- Sidecar: `/tmp/6Uf5mO-benchmark-14k.json.sha256`
- Size: 4,275 bytes
- SHA-256: `c7929ebc43ce90b45a2ce69fe2629648465c64ba59f4b6ada358174ae3e1f1cf`
- Schema/operation: receipt v2, `installed-cli-benchmark`
- Fixture: 14,000 canonical sharded Tasks; fixture commit
  `880bd65e05b13ead37d48379a11c78eff3d05893`
- Source benchmark SHA-256:
  `499d965705acfbd3c10f65b301a8a777689076be4654f715487ecbc713d7421a`
- Cold rebuild: 12.448 s; peak RSS 96,698,368 bytes
- p95: get 4.566 ms; list 84.388 ms; search 87.336 ms; mutation 127.119 ms
- Maximum blob: 207,194 bytes
- Gates: cold time/RSS, get/list/search/mutation latency, max blob, write
  amplification, and ledger-output independence all `true`

The report and sidecar are bounded ephemeral `/tmp` evidence and were not added
to product or controller state. Their identity is recorded here; no benchmark
fixture was retained after the script's own temporary-context exit.

## Verdict and exclusions

All authorized mandatory source and fixture gates pass on the exact composed
candidate, so the compatibility/migration acceptance verdict is **PASS** and the
six delivery waves are complete for this scope. Doctor, conversion, rollback,
archive recovery, canonical-byte preservation, exact-update behavior, search
redaction, and hosting safety are covered by the green full/focused fixture suite.

The release verifier is **excluded, not failed**: its project-local `.venv` and
RC-wheel assumptions belong to separately authorized release acceptance. No
wheel/RC build, package installation, live corpus operation, release, tag, push,
publish, deploy, production mutation, archive maintenance, controller metadata
mutation, `yy task finish`, or cleanup was performed.

Pi session: `01a0467f-e64c-7182-b6b9-cff63f1a2664`.
