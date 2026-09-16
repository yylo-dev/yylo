# Next-lifecycle acceptance replay

Date: 2026-09-09. Result: **NEEDS_DECISION**.

This report completes the analysis task: the frozen safety corpus was replayed on the fully integrated B–J candidate. The **next version is not accepted**. All seeded G1–G8 cases passed, but the frozen fixtures do not emit candidate `coordination_interventions`, so the required 70% reduction (80% stretch) cannot be calculated. Missing target evidence is not relabelled as success.

## Machine-readable result

```json
{
  "schema_version": "juno.lifecycle_simplification.acceptance.v1",
  "baseline_corpus_sha256": "ec8b3c499966fcd35d0dae6a9b564e5c60e7cd900a83d938915aebd3a5e7c827",
  "candidate": {
    "commit": "949f7271bb28d168cf73859c56073a6cb8140968",
    "tree": "dfb9e7fd1a82e92c5bba9715022530c86907b610",
    "package": "@yylo/cli@0.2.2",
    "repeats": 3
  },
  "safety": {"passed": true, "scenario_count": 16},
  "primary_target": {
    "metric": "coordination_interventions",
    "cohort": "routine",
    "baseline_weighted_median": 10.95,
    "candidate_weighted_median": null,
    "reduction": null,
    "reduction_required": 0.7,
    "stretch_reduction": 0.8,
    "complete": false,
    "passed": false
  },
  "readiness": "NEEDS_DECISION",
  "reason": "target_metric_unknown",
  "analysis_complete": true,
  "next_version_accepted": false,
  "percentile_policy": "three repeats support median/range only; no p99 claim"
}
```

The baseline manifest, routine membership, and weights were not changed. Its SHA-256 is `ec8b3c499966fcd35d0dae6a9b564e5c60e7cd900a83d938915aebd3a5e7c827`. The primary baseline remains 10.95 weighted median interventions. Candidate fixture receipts expose outcome, wall, and subprocess totals, but not the named target metric. Inferring interventions from subprocesses, state count, elapsed time, or source layout would change the denominator and is prohibited.

| Frozen routine scenario | Weight |
| --- | ---: |
| docs-inert | 0.05 |
| docs-active-cold | 0.10 |
| docs-inert-warm | 0.10 |
| docs-active-warm | 0.15 |
| normal | 0.25 |
| two | 0.10 |
| ten | 0.10 |
| multi | 0.15 |

## Frozen and candidate identities

Baseline reference remains `de9e3a8caa9d439654fe25dd06ecd5aae3af0bb4` (tree `f55b0f0cda1157e7795cb512f9effdc4fd60ff1b`). Candidate B–J integration is `949f7271bb28d168cf73859c56073a6cb8140968` (tree `dfb9e7fd1a82e92c5bba9715022530c86907b610`). Environment matched the baseline: Linux 6.17.0-1019-aws x86_64, Node v22.23.2, Python 3.12.3, Git 2.43.0, and UTC.

| Bound input | Candidate SHA-256 |
| --- | --- |
| `juno-code/package.json` | `d962a3e38fbca5048f8e324a719ecef7dd7fdbfc86ee3b45a5ad87f1166dd7dc` |
| `juno-code/package-lock.json` | `610fb6c5fec9488c9fe48245b97ee070f022566e9947001244e94324f40dec7b` |
| fixture constructor | `8c67924f011c3f113889546baff13475a905b35c00025f3b494fea96c8f7fe57` |
| profile runner | `556f3abec1ef4dda9f436b2b3736722922ed1bda6dc3da23146b579ee0cff30e` |
| owner runner | `77f6c794b42d483eec9c24f3d2cf5a7b5cbdf5d69012efab6536b959a344d365` |
| duration weights | `199718f55445546d7a8af63a08f7fd53af569ded42b21b3b3400c2e6febf04ca` |
| evidence matrix | `abe6098fd59f3fd9bd04a1b19ef840e6b5cc173fb40ae31e4b189a173ebe3b66` |
| task-workspace runtime/template | `0471f34d34fc06714154e190d95fa2a13e2a8dd1e64496d41a02aa6c0632320f` |
| merge-queue runtime/template | `42985d7fc5c8522fbd064dd5efed3226be0ab39c7d0c44f76b6d235f283a4374` |
| risk-policy runtime/template | `3830d0a509323274789df6b23014bbe8c9239322ccf1f6f66642515a769b6b44` |
| operation-snapshot runtime/template | `3e52eb9120539280ddeb9c035ec57e49d76b02defaddf813144801247eb58212` |
| controller managed-assets | `11ecbeafd5bad6af0307c3e077e19379f8277f5f7f437a6de125e59efe839927` |
| package managed-assets | `cd0b2a77d55c9a7b684c77a81a4aac4619a1f304bb0ed3397becd9cc8e0ba45d` |

Runtime/template pairs above are byte-identical. Policy remained low=0 reviewers, normal≤1, high=2 sequential predecessor-bound reviewers, one repair candidate, and one delta review group.

## Three-repeat replay

Times are fixture wall milliseconds, median `[min, max]`. They are secondary observations and are not substituted for coordination. The active-doc selector executes cold, corrected, and repeated audit phases but exports only one combined wall envelope; separate cold/warm candidate times are therefore missing rather than guessed.

| Scenario | Candidate fixture wall ms | Subprocesses | Result |
| --- | ---: | ---: | --- |
| docs-inert | 0.859 `[0.773, 0.925]` | 0 | 3/3 pass |
| docs-active cold/warm combined | 119.841 `[114.952, 148.327]` | 42 | 3/3 pass |
| normal | 2520.273 `[2484.420, 2685.984]` | 27 | 3/3 pass |
| high | 1807.554 `[1805.712, 2025.322]` | 250 | 3/3 pass |
| two | 6382.410 `[6275.244, 7020.559]` | 36 | 3/3 pass |
| ten, deterministic projection | 6382.410 `[6275.244, 7020.559]` | 180 | 3/3 pass |
| multi, ordinary delivery | 1435.717 `[1331.847, 1629.212]` | 252 | 3/3 pass |
| conflict | 13210 `[13110, 13460]` | 1 wrapper | 3/3 pass |
| failed test | 11720 `[11670, 11780]` | 1 wrapper | 3/3 expected refusal detected |
| stale runtime | 773.876 `[748.142, 774.203]` | 25 | 3/3 expected refusal detected |
| crash after validation receipt | 1382.905 `[1288.996, 1422.013]` | 24 | 3/3 pass |
| crash around target CAS | 5170 `[5120, 5200]` | 1 wrapper | 3/3 pass |
| crash during finalization | 967.168 `[864.848, 1005.651]` | 67 | 3/3 pass |
| dirty successor | 2001.788 `[1956.446, 2002.780]` | 26 | 3/3 pass; 19 bytes preserved each |

All 16 frozen scenarios completed three repeats: 42 expected passes and six expected known-failure/refusal detections, 48/48 total. For `multi`, the integrated ordinary-delivery selector replaced the retired special-umbrella selector while preserving the frozen scenario requirement. The legacy selector separately passed 3/3, proving the finite compatibility reader without running a second engine.

Secondary regressions are disclosed: conflict wall rose from 11730 to 13210 ms (+12.6%), failed-test wall from 9700 to 11720 ms (+20.8%), and dirty-successor subprocesses from 22 to 26 (+18.2%). Lower fixture wall elsewhere is not claimed as active-wall, compute, cost, or whole-delivery savings.

Three repeats support median/range only. They do not support reliable p90, p95, or p99, and this report makes no 90% or 99% claim.

## G1–G8 and seeded defect detection

| Gate | Retained evidence |
| --- | --- |
| G1 correct requirements | active-doc audit, normal delivery, ordinary cumulative checkpoints |
| G2 dirty/conflicted bytes | two/ten tasks, real conflict, 19-byte dirty recovery packet |
| G3 exact integration | normal composition, conflict, expected-old-SHA CAS, post-CAS resume |
| G4 exact evidence | docs, failed test, stale runtime, validation crash, changed/tampered closure negatives |
| G5 truthful completion | known failures remain failures; validation/CAS/Ledger crashes do not duplicate integration |
| G6 concurrency/fencing | high-risk and independent two/ten-task fixtures retain one fenced owner |
| G7 bounded recovery | unchanged deterministic failure launches once; checkpoint and advisory paths stay bounded |
| G8 authority boundaries | stale runtime, dirty successor, ineligible mutation, and new-umbrella start refuse non-mutating |

Supplementary current-layout fixtures passed 3/3 for exact command invalidation, stale evidence drift, stale-token authority refusal, unchanged deterministic failure reuse, mutation ineligibility before expensive work, bounded review disposition, advisory retention on the delivery, new-umbrella refusal, finite start-time conversion, and resumable post-CAS Ledger finalization. Seeded reviewers prove policy routing and bounds, not live semantic review quality.

## Separate success dimensions and unknowns

- **Managed model-run terminal rate:** `unknown / 0`; no paid/live model or semantic reviewer was launched.
- **Admitted-task delivery rate:** `unknown / 0`; no live task was admitted to or driven through a production queue. Seeded detection is separately 48/48 and is not delivery success.
- **Unchanged failure reuse:** 3/3 fixtures prove the first deterministic failed suite result stands and a second request launches no suite, reviewer, model, or repair.
- **Successor incidence:** one deliberate dirty successor per dirty repeat, 3/3. There is no live admitted-task denominator, so no production successor rate is inferred.
- **First-failure/phase cost:** focused fixtures retain distinct `resource_wait_ms`, `setup_ms`, `execution_ms`, `settlement_ms`, `overall_elapsed_ms`, and `first_failure_ms`; the timeout contract passed 3/3. Those fields are not exported into this aggregate, so no phase percentile is invented.
- **Whole delivery and queue wait:** unavailable; fixture zero wait is not a live zero.
- **Compute, tokens, and billed cost:** unavailable; subprocess totals are not compute or cost.
- **Review recall and production escape rate:** unavailable; deterministic seeded verdicts are not semantic quality evidence.

The report contains no raw prompt, transcript, secret, unrestricted command output, or live controller state.

## Decision required

The safety gate passes, but the named numeric gate is unknown. The current replay cannot establish either the 70% target or the 80% stretch target. An owner must decide whether to add source-bound intervention instrumentation in separately authorized work, rerun with an already authoritative compatible source, or decline next-version acceptance. This task does not lower the target, change weights, repair production behavior, launch follow-up tasks, infer release authority, or authorize live migration.

## Reproduction

From the exact candidate checkout, run the existing task-workspace profiler three times with the scenario selectors in `lifecycle-simplification-corpus.v1.json`, using the ordinary-delivery replacement for `multi`; run conflict, failed-test, and CAS selectors directly three times. Then run:

```bash
cd juno-code
node scripts/test-performance/lifecycle-simplification-baseline.mjs --out /tmp/lifecycle-baseline.json
npm test -- src/utils/__tests__/lifecycle-simplification-baseline.test.ts
npm run test:installed-test-fixture-package
```

These commands use disposable fixtures. They do not drive a live queue, publish, push, release, deploy, migrate, or mutate production.
