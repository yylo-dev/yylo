# Frozen next-lifecycle baseline

This is the bounded current-version baseline for the next lifecycle acceptance replay. It is **not** a new benchmark product and it changes no lifecycle behavior. The executable source of the calculations is `scripts/test-performance/lifecycle-simplification-baseline.mjs`; the frozen inputs are `lifecycle-simplification-corpus.v1.json`.

## Frozen boundary

- Reference target: `de9e3a8caa9d439654fe25dd06ecd5aae3af0bb4` (tree `f55b0f0cda1157e7795cb512f9effdc4fd60ff1b`).
- Corpus SHA-256: `ec8b3c499966fcd35d0dae6a9b564e5c60e7cd900a83d938915aebd3a5e7c827`.
- Invoked package: `@yylo/cli` 0.2.2, `yy`; Ledger runtime 0.2.0.
- Environment: Linux 6.17.0-1019-aws x86_64, Node v22.23.2, Python 3.12.3, Git 2.43.0, UTC.
- Policy: low risk 0 reviewers, normal at most 1, high exactly 2 sequential predecessor-bound reviewers; one repair candidate and one delta review group.
- Fixture sources at the reference commit: task-workspace fixture `8c67924f011c3f113889546baff13475a905b35c00025f3b494fea96c8f7fe57`; profile runner `556f3abec1ef4dda9f436b2b3736722922ed1bda6dc3da23146b579ee0cff30e`; owner runner `77f6c794b42d483eec9c24f3d2cf5a7b5cbdf5d69012efab6536b959a344d365`; duration weights `199718f55445546d7a8af63a08f7fd53af569ded42b21b3b3400c2e6febf04ca`; evidence matrix `abe6098fd59f3fd9bd04a1b19ef840e6b5cc173fb40ae31e4b189a173ebe3b66`.
- Policy/runtime sources: task workspace `c8de31d5f8300ae8dd8957d2bcf2808de1d5362745ea7f43d7e9ff8e3a8eb960`; merge queue `24a25e905c40c80a306b91220842cf11c1759a0772458d319aa553dcf7d7d768`; risk policy `dd05186e8b8ab47885395aada558131577ee7aabe586363cf995c86140f85843`; operation snapshot `1ba7e016097d2772ceed43cddf5201341d77039d0ecc56a595a8a585a98ec06b`.

The manifest records every other source digest, exact fixture selector, environment class, denominator, repeat and complete-input identity. The driver verifies those bytes directly from the frozen Git commit by default.

## Measurement contract

The primary future target is at least **70% fewer lifecycle coordination interventions** (80% stretch) on the fixed routine cohort below. An intervention is a deterministic phase-advancing fixture command or handoff. The weighted baseline median is **10.95 interventions**. This is not a safety score and wall time is never substituted for it.

Routine membership and weights were frozen before any next-version comparison:

| Scenario         | Weight |
| ---------------- | -----: |
| docs-inert       |   0.05 |
| docs-active-cold |   0.10 |
| docs-inert-warm  |   0.10 |
| docs-active-warm |   0.15 |
| normal           |   0.25 |
| two              |   0.10 |
| ten              |   0.10 |
| multi            |   0.15 |

Only nonzero baseline coordination counts participate in the percentage. A zero baseline is compared as an absolute count with no regression; it is never divided. There are 78 unique complete input closures: each of the two- and ten-task cases is counted independently, never as one aggregate input. For concurrent scenarios, elapsed wall is the observed maximum envelope, never the sum of concurrent durations. `ten` is explicitly a deterministic ten-input projection of the integrated independent-task fixture, not ten live deliveries.

Each row has three repeats. Time is median `[min, max]` milliseconds. `hits/misses` are exact evidence decisions; `states/writers` inventories lifecycle states and independent authority writers. Known baseline refusal/failure outcomes are retained rather than masked.

| Scenario           |     Active/fixture whole wall ms | Interventions | Commands | Hits/misses | Recovery | Bytes preserved | States/writers | Known failures |
| ------------------ | -------------------------------: | ------------: | -------: | ----------: | -------: | --------------: | -------------: | -------------: |
| docs-inert         |             1.135 [0.953, 3.257] |             1 |        0 |         0/0 |        0 |               0 |            2/1 |              0 |
| docs-active-cold   |       147.522 [138.501, 287.775] |             3 |       42 |         0/1 |        0 |               0 |            2/1 |              0 |
| docs-inert-warm    |             2.986 [1.145, 5.230] |             1 |        0 |         0/0 |        0 |               0 |            2/1 |              0 |
| docs-active-warm   |       293.638 [126.562, 331.191] |             2 |       42 |         0/0 |        0 |               0 |            2/1 |              0 |
| normal             |    9257.094 [8978.759, 9332.460] |             6 |       27 |         2/1 |        0 |               0 |            3/2 |              0 |
| high               |    5953.460 [5728.273, 6114.892] |            10 |      347 |         0/1 |        0 |               0 |            5/2 |              0 |
| two                | 16809.066 [16763.624, 16904.626] |            10 |       36 |         2/0 |        0 |               0 |            6/2 |              0 |
| ten                | 16809.066 [16763.624, 16904.626] |            50 |      180 |         2/0 |        0 |               0 |           30/2 |              0 |
| multi              |    4240.659 [4168.608, 4269.529] |            18 |      267 |         2/0 |        0 |               0 |            8/2 |              0 |
| conflict           | 11730.000 [10700.000, 11910.000] |             8 |        1 |         0/0 |        2 |              19 |            4/2 |              0 |
| failed             |    9700.000 [9400.000, 9960.000] |             5 |        1 |         0/1 |        1 |               0 |            3/1 |              3 |
| stale              |       819.596 [809.088, 913.932] |             2 |       25 |         0/0 |        1 |               0 |            2/1 |              3 |
| crash-validation   |    3949.435 [3842.193, 4056.173] |             3 |       24 |         0/1 |        1 |               0 |            3/1 |              0 |
| crash-cas          |    4510.000 [4490.000, 4560.000] |             4 |        1 |         2/0 |        1 |               0 |            4/2 |              0 |
| crash-finalization |    2632.511 [2555.308, 2667.325] |             3 |       67 |         0/0 |        1 |               0 |            4/2 |              0 |
| dirty              |    3748.909 [3626.237, 3977.938] |             3 |       22 |         0/0 |        1 |              19 |            2/1 |              0 |

Fixture queue wait is zero because no live queue is driven. Live whole-delivery elapsed and queue wait are **unavailable**, not inferred from that zero. Likewise paid-model tokens/cost, real reviewer recall, production escape rate, and reliable p90/p95/p99 are unavailable. Two high-risk model handoffs are deterministic reviewer fixtures only; they are not review-quality evidence. Three repeats support median/range, not tail claims. Duplicate dispatch is zero in every seeded repeat.

## Safety mapping

- G1 correct requirements: active docs, normal, multi.
- G2 preserve work: two, ten, conflict, dirty.
- G3 correct integration/CAS: normal, conflict, crash-CAS, crash-finalization.
- G4 exact evidence: docs, high risk, failed test, stale runtime, crash-validation.
- G5 truthful completion: normal, multi, failed test and all crash boundaries.
- G6 concurrency/fencing: high risk, two and ten independent inputs.
- G7 bounded recovery/low coordination: docs and multi-checkpoint delivery.
- G8 authority/external boundaries: high risk, stale runtime and dirty successor.

The seeded corpus covers validation-receipt, target-CAS and board-finalization crashes, a real textual Git conflict, failed validation, stale runtime, dirty-byte preservation, cold/warm docs, normal/high-risk policy, independent tasks and one multi-checkpoint delivery. Incomplete or malformed repeat output causes the driver to exit nonzero; it cannot become success.

## Consumers and migration evidence

Supported public consumers are `yy task` (`start`, `status`, `preflight`, `finish`, `run`, lease handoff/successor), `yy merge` status/arbiter/drive/recovery operations, the installed shell router, and managed package templates. The inventoried writers are the fenced task-attempt producer, fenced target arbiter and revision-checked Ledger projection; scenario counts include only writers exercised by that fixture. Legacy migration must account for the exact attempt states and umbrella requirements listed in the corpus: ordered scope union, tracking-only child refusal, checkpoint order/evidence, one parent owner and truthful projection. Unknown states fail safely and old receipts remain immutable.

At the frozen source, `preimplementation_contract`, `active_preimplementation_contract`, `run_handoff`, `HANDOFF_SCHEMA`, `HANDOFF_ROOT`, `_git_blob`, and the optional queue-review `acceptance_contract` field have no supported public producer. Their remaining producers are raw runtime dispatch and direct tests. This is the source-bound removal evidence for K6oZFW, not deletion authority in this task. The withdrawn Ixf60t dependency is replaced by the integrated immutable task-workspace fixture base/profile runner identified above; its historical record and source evidence are not rewritten.

## Reproduction

```bash
cd juno-code
node scripts/test-performance/lifecycle-simplification-baseline.mjs --out /tmp/lifecycle-baseline.json
npm test -- src/utils/__tests__/lifecycle-simplification-baseline.test.ts
```

The first command verifies the frozen source objects and reproduces all aggregation. Collection used the supported task-workspace profile runner and named disposable Python unittest fixtures; it did not invoke `yy task`, drive `yy merge`, launch paid models, mutate a controller, release, push or deploy.
