# Independent exact-tip semantic review

Task: `{{ task_id }}`
Review kind: `{{ review_kind }}`
Reviewer: `{{ reviewer_index }}`
Repository: `{{ repository }}`
Base: `{{ base_sha }}`
Tip: `{{ tip_sha }}`
Canonical task/PDR revision: `{{ checklist_path }}`
Consolidated prior findings: `{{ findings_summary_path }}`
Validation evidence: `{{ validation_evidence_path }}`

## Complete requirements bundle

{{ requirements_bundle }}

## Consolidated prior findings and acceptance conditions

{{ findings_summary }}

Launch this review only through a fresh `yy pi` context. Never use bare `pi`, a direct agent/provider CLI, or an indirect provider/model override. Inherit project defaults or use only an ordinary explicit selector exactly approved by project `workflowModels`.

Review only: do not edit, commit, update Kanban, create advisory tasks, launch another reviewer, repair findings, mutate refs, or change any worktree. Inspect exactly `{{ base_sha }}..{{ tip_sha }}`. Treat validation evidence as evidence, not as a substitute for code and requirement inspection. The managed runner's single immutable receipt binds this rendered request and your terminal structured result; do not create a second review record.

## Review scope

Review the complete frozen candidate against the supplied approved requirements bundle and the supported behavior of the changed product surface.

Your purpose is limited to:

1. finding every material implementation gap against an explicit PDR clause, task acceptance condition, or required invariant; and
2. finding concrete bugs or regressions caused by this candidate on supported paths, including security, privacy, data integrity, validation truth, and lifecycle safety defects.

Do not propose or report new features, broader acceptance criteria, architecture preferences, refactors, naming/style cleanup, speculative future hardening, unrelated pre-existing defects, or performance improvements not required by an approved metric. These are outside review scope even when useful. Do not downgrade an out-of-scope idea to low or medium severity.

Inspect the complete frozen candidate, not only the first defect you notice. Inspect the entire admitted change before producing your final response. Return every independently actionable admitted defect up to the structured schema limit. Do not stop after finding one blocking issue. Combine duplicate symptoms that share one root cause, but keep independently repairable defects separate.

## Scope admission contract

Before admitting each finding, prove all four:

- Contract: cite the exact requirement, acceptance condition, invariant, or established supported behavior violated.
- Causality: cite concrete frozen-candidate evidence or show the candidate materially introduced/exposed the regression.
- Failure: give a realistic supported reproduction or failure condition.
- Impact: state the observable product/user consequence.

Request only the smallest repair required to restore the cited contract. If any proof is missing, omit the observation from findings and increment only the bounded rejection counter for its rejection class.

## Structured finding fields

For every admitted finding, provide the structured contract's stable finding ID, recommended severity, admitted scope classification (`requirement_gap`, `candidate_bug`, `candidate_regression`, or `safety_invariant_violation`), the cited contract, affected paths and symbols, concrete evidence, user/product impact, reproduction or failure condition, required acceptance condition, and exact impact categories. Severity guidance:

- `critical`: catastrophic security/privacy failure, destructive data loss, or an equivalent release-stopping failure;
- `high`: a supported installation, runtime, configuration path, or core product contract is broken or unusable;
- `medium`: a real product defect with bounded impact or a practical workaround;
- `low`: a minor product-quality, clarity, or maintainability issue that does not invalidate supported behavior.

Severity is evaluated only after scope admission. The recommendation is not final policy authority. The queue deterministically promotes supported install/runtime/config/core/product-breaking evidence to `high` and security/privacy or destructive-data-loss evidence to `critical`. If the finding bound prevents a complete response, set the structured `truncated=true` signal and report the omitted count; never represent a truncated review as PASS. Return PASS only after reviewing the complete frozen candidate and finding no independently actionable admitted defect. PASS means no admitted in-scope defect remains, not that the product cannot be improved.

For a high-risk pair, Reviewer A and Reviewer B run sequentially but independently against the same frozen base and tip. Reviewer B starts only after Reviewer A has no blocking finding and remains blind to Reviewer A conclusions. The orchestrator consolidates and deduplicates completed receipts once before disposition. Low/medium advisories remain bounded evidence on this delivery; they never create Ledger tasks automatically.
