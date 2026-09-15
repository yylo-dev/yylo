# LEARN_LLM consumer upgrade triage (P40Jij)

Date: 2026-09-15. Scope: report and disposable probes, **not an upgrade repair or authorization**.
Source examined: `0893b72b0898a3131065048d228e3aef20948ff1`.
Although its `juno-code/package.json` says `0.2.3-rc.3`, this evolving source checkout is not proof of the published RC3 package contents.

## Decision and evidence boundary

**No complete supported 0.2.2 → RC3 consumer migration route has been established.**
The report does not demonstrate that the consumer missed an existing end-to-end command.
Runtime registration repair and provenance repair are narrower operations, not a version-upgrade transaction.
The current coherence gate still requires a source twin in a consumer with no source tree.
The reported local helper patch is not a supported upstream route; collecting and validating it is necessary before reuse.
Do not use old FIFO commands as the current recovery interface.

Evidence classes used below:

- **R**: canonical incident report supplied with P40Jij, September 14. macOS/zsh, Node 22.22.3, scripts 0.2.2, global CLI RC3, isolated official packages selected via PATH. Not independently collected or replayed here.
- **S**: source inspection at the commit above; paths and functions below permit exact reinspection.
- **P**: three disposable probes retained in `../scripts/tests/consumer-upgrade-triage.test.py`. These reproduce current mechanisms, not the official-package incident or its proposed repair.
- **H**: historical documentation, not present-release acceptance: `ledger-native-records-aggregate-acceptance.md` and `lifecycle-simplification-migration.md`.

R preserves consumer task references `z4dOvO` and `8bZXAn` (not this board's task relations), branch `juno/task-8bZXAn`, candidate `e1faa061bd239141b2726be3509ebc8cab3e15bf`, and claimed official runtime SHA-256 `9fa7351fec80eda01e1c2bce75e58f1e254afcc2e11e0c1a18ebfc788a2b3604`.
Only `.juno_task/scripts/task_workspace.py` and `.juno_task/managed-assets.json` reportedly changed in that candidate.
The six local repair regressions, four focused merge tests and 32 project tests are **reported**, not tests executed here.
Reported latest state: queued, review blocked on ZAI authentication, not landed, refresh/migration/artifacts incomplete, controller checkpointed clean; learning drafts and product bytes remain in the learning worktree. No native artifact Record IDs exist in the supplied evidence.

Collect from the reporter, through a separately authorized evidence transfer: original/repaired helpers, repair script and receipt, all six regression sources/results, package manifests and tarball integrity, runtime/registration receipts, exact invocation/environment-key inventory (no values containing secrets), queue/checkpoint/binding logs, and config pre/post hashes with secret-redacted field classifications. Preserve originals read-only and digest every attachment. No claim here relies on possession of those attachments.

## Findings and classification

| # | Classification and affected version | Evidence, interpretation and disposition |
|---|---|---|
| 1 | **Supported behavior with actionable recovery**, conditional on complete metadata-only admission; origin of partial registration **unresolved**. R: old controller/RC3 rebind. | Role alone does not prove metadata-only mode or installed runtime identity. S: `juno-code/src/cli/commands/migrate.ts` exposes `runtime-rebind`; `.juno_task/scripts/metadata_controller.py` owns it. Clean controller, exact branch, external installed executable and external receipt are required. Preserve registration first; absent mode is not permission to guess or set Git config blindly. Use migration inventory/owner classification, then conditional rebind below. R's manual mode edit is not established as a supported automatic recovery. |
| 2 | **Supported fail-closed behavior with an unsupported upgrade gap**. R: 0.2.2 scripts/RC3 package; S: current. | `.juno_task/scripts/target_runtime_provenance.py:package_snapshot,target_provenance` validates installed identity/inventory and requires target runtime bytes to equal package bytes. Refusal on different versions is correct. `scripts update --force` cannot authorize bypassing bound generations. No-source-change runtime refresh does not turn provenance repair into upgrade. The missing cross-version transaction needs its own implementation, including finding 3. |
| 3 | **Reproduced defect in current source**; official RC3 occurrence remains R. | `.juno_task/scripts/task_workflow_helper.py:grouped_coherence` unconditionally maps runtime paths to `juno-code/src/templates/scripts/`; absent/different twin emits `coherence.runtime_template_mismatch`. P creates a real temporary Git consumer with no source tree and observes precisely that finding. It does not prove official bytes pass every other admission check. Specify separate installed-consumer proof below, never a fake source tree. |
| 4 | **Superseded legacy behavior needing transition guidance**, plus current compatibility-admission gap confirmed by inspection. R: RC3 front end/0.2.2 merge script. | `juno-code/src/cli/commands/merge.ts:invokeMergeAtController` checks script existence then dispatches the requested operation without a protocol handshake. Missing-script guidance says `scripts update`, which is insufficient for a bound cross-version controller. Current interface is `status/land/project`; R's `plan/drive/arbiter` belong to retired FIFO machinery. Capability refusal must occur before dispatch; do not resurrect FIFO. |
| 5 | **Unresolved historical observation**, legacy review-in-merge sequencing is **superseded**. R: mixed versions, task metadata dirtied between validation/reviewer admission. | `.juno_task/scripts/managed_agent_runner.py:controller_identity` rejects missing config or dirty status; a checkpoint may also invalidate a previously captured binding. R cannot establish which writer or ordering caused this. Current `merge.ts` explicitly launches no tests/reviews/models. Investigate remaining managed-worker boundaries, not retired queue scheduling. Never blanket-ignore `.juno_task` dirt or let checkpoint success become a lifecycle gate. |
| 6 | **Reproduced defect in current source**; old/RC3 occurrence R. | `.juno_task/scripts/managed_agent_runner.py:managed_node_contract,clean_environment` records the pre-normalization `yy` then prepends selected Node's directory. P supplies isolated and global fake executables and proves `which yy` in returned PATH changes to global. Version probe is mocked; no provider launched. R's Node symlink workaround is evidence, not a permanent remedy. |
| 7 | **Reproduced config derivation defect in current source**; actual CLI rejection supported by S, incident execution R. | P proves `derive_compatible_config` preserves all four product fields. `juno-code/src/core/config.ts` rejects product-only controller fields; `migration_inventory.py:CONFIG_FIELD_CLASSIFICATION` classifies `workingDirectory`, `sessionDirectory`, `autoDependencyUpdate`, `hooks` as product-only. Existing sparse→metadata-only translation does not sanitize these. Diagnose before binding/launch, preserve controller provider/model and safe settings; unknown fields require disposition, not deletion. |
| 8 | **Supported authentication refusal with unresolved preflight coverage**. R: `zai/glm-5.3`, missing ZAI key; no credentials/provider calls exercised here. | Missing credentials are not a runtime defect or grounds to fabricate approval. An unrecognized/custom model warning is not proof the model is invalid. Require intentional custom-ID selection and child-visible authentication before costly/mutating managed execution. This belongs to the managed runner/model-policy boundary, not native merge. |

Telemetry `.git` EPERM, temporary invocation ENOENT and `Kanban task hydration timed out for Review. Automatic substitution was skipped.` remain **unresolved observations**, not confirmed defects. Reviewer reportedly launched afterward. Reproduce independently with exact invocation paths, permissions, sandbox and timing evidence before filing product fixes.

## Ownership before and after transition

| Concern | Historical owner / incident mismatch | Intended/current owner and invariant |
|---|---|---|
| Controller registration | Worktree role existed without mode/runtime registration | Registered metadata controller; migration inventory plus explicit authorized binding, not product-file absence alone |
| Installed runtime identity | PATH-selected isolated CLI versus global installation | External immutable `@yylo/cli` package identity, executable/version/hash receipt; active task creation identity stays pinned |
| Tracked runtime provenance | Consumer task owns tracked runtime and inventory; source-only gate interfered | Admitted product task owns candidate commit; source or installed-consumer proof selected by trusted markers |
| Managed generation | Old controller scripts bound to prior target | Maintenance-owned receipt-bound refresh after target delivery; no in-place replacement under active producers |
| Config | Old product settings leaked into metadata controller and reviewer | Schema-classified migration; controller-only settings and resolved file identities bound before managed launch |
| CLI dispatch | New command sent directly to old parser | Package/control-plane router admits script protocol before any mutation |
| Subprocess identity | Node PATH normalization changed CLI selection | Canonical runner binds exact CLI separately from Node; nested children inherit that identity |
| Ledger | Legacy venv Kanban plus unrelated global Ledger | Independently installed exact release-compatible Ledger via `yy ledger`; native records, not task substitutes |
| Delivery | FIFO queue owned validation/review execution | Target owner uses native `status/land/project`; tests and semantic reviews are explicit external checks |

Related work: QMoWi8 is source-development cutover, not consumer installed-release acceptance. 0N4yaK/2lw5JS retire FIFO/native-delivery machinery. Coordinate transition dispositions with those owners without asserting these tasks fix the consumer gate.

## Ordered migration assessment and stop/resume boundaries

The following is a **conditional maintenance runbook specification**, not permission to execute it on LEARN_LLM or this controller. Commands shown are source-defined surfaces; exact historical package help must be captured before applying to 0.2.2/RC3. There is deliberately no invented command bridging the unsupported step.

1. Freeze ownership and preserve evidence. Record controller/product/task/integration paths, refs, HEADs, dirty paths, Git registration, task creation/runtime receipts, managed inventory/generation, dependency policy, config and selected executables. Back up metadata including Ledger/CAS, registration and all product/draft bytes externally with checksums and restrictive permissions. Never record credential values in triage logs. Do not stop producers by killing or replacing their runtime: drain or use ordinary fenced handoff with owner authority.
2. With the explicitly selected installed CLI (call its absolute launcher, not an ambiguous global `yy`), inspect `yy where controller`, `yy info --json`, and `yy migrate --help`. Source-defined inventory command:
   ```bash
   yy migrate inventory --project <product-worktree> --controller <controller> --product-ref refs/heads/main --runtime <external-package>/dist/bin/cli.mjs --output <external-inventory.json>
   ```
   Resolve partial registration and field dispositions through owner-reviewed migration admission. Zero tracked product files is necessary but insufficient. Missing/stale/unknown inventory is a stop boundary. Product relocation in R was reportedly already completed and checksum-verified; do not repeat it automatically.
3. Admit controller config and dependency/CLI/provider compatibility **before** rebinding or expensive task execution. The current absence of one unified preflight is a gap, not an instruction to hand-edit fields. The supported migration implementation must produce a redacted plan, preserve provider/model settings, reject unclassified fields and make secret/file mappings explicit. A missing authorized migration plan is a stop boundary.
4. For a clean, fully admitted metadata-only controller and a separately acquired immutable external RC3 package, the narrow registration command is:
   ```bash
   yy migrate runtime-rebind --root <controller> --branch refs/heads/juno/controller-metadata --runtime <external-rc3-package>/dist/bin/cli.mjs --runtime-version 0.2.3-rc.3 --output <external-rebind.json>
   ```
   This installs no package and changes no tracked runtime. Preserve previous binding and the receipt; verify exact executable/package/version/hash readback. The reported incident reached this boundary. It does not establish downstream compatibility or authorize changing existing task receipts.
5. **Stop: cross-version tracked-runtime transaction is not established.** An ordinary authorized upgrade task would need exact package runtime/manifest bytes, consumer coherence admission, one candidate, explicit checks, ordinary queueing and target-owner delivery. R's locally patched finish is not sufficient upstream proof. Do not substitute `scripts update --force`, repeated identical refreshes, old `merge drive`, source adoption, or relaxed validation.
6. Provenance-only recovery is conditional on already matching target/package runtime bytes and matching controller identity/inventory/generation. Source-defined commands are:
   ```bash
   yy migrate target-runtime-provenance plan --controller <controller> --output <external-provenance-plan.json>
   yy migrate target-runtime-provenance apply --plan <external-provenance-plan.json> --output <external-provenance-apply.json> --authorize-target-runtime-provenance
   ```
   Apply requires separately reviewed authorization; mismatch means stop, not edit the receipt. This is an alternative narrow repair, **not step 5's upgrade**.
7. Only after a supported candidate is delivered and all active producers have a valid disposition may the maintenance owner plan generation refresh:
   ```bash
   yy integration runtime-refresh --previous-sha <admitted-old-target> --target-sha <delivered-target> --dry-run
   yy integration runtime-refresh --previous-sha <admitted-old-target> --target-sha <delivered-target> --apply <reviewed-refresh-receipt>
   ```
   Stale target, customization, dirty bytes, identity mismatch or changed registration invalidate the plan. Replan against live identities, never rewrite a bound receipt. New native-delivery `yy merge project TASK_ID` is only for already-integrated projection recovery, never a second merge. Legacy pending/conflicted candidates require the explicit disposition table in `lifecycle-simplification-migration.md`.
8. After independent Ledger release admission and its supported schema migration, create/read back native artifacts through `yy ledger`. Exact installed Ledger migration subcommands and rollback semantics have not been collected for this consumer; **stop until verified**, rather than applying old Kanban migration prompts or inventing a migration command. Artifact creation is excluded from this task.

Rollback boundaries: before any apply, retain immutable plans and byte-identical backups; registration-only rollback needs an owner-approved restoration of the previous registration, not a target reset. After task delivery, retain source/target ancestry and use a separately authorized ordinary corrective commit, never force/reset history. After generation activation, never launch an older runtime against a newer schema blindly. After Ledger writes/migration, preserve new records and use that version's verified rollback procedure; backup restoration is not safe over intervening writes. Resume only from the last receipt whose inputs still match; ambiguity remains blocked. No universal atomic rollback across Git registration, target, generation and Ledger has been demonstrated.

## Consumer coherence contract for a follow-up

Choose source versus installed-consumer mode from trusted registered identity/markers, not simply absence of `juno-code`. Source/template markers retain the original strict twin check, including deletion and unequal bytes. Conflicting or unknown markers fail closed.

For installed consumers, admit only when **all** proofs agree:

1. Registered controller runtime identity and task worktree `runtimeExecutable` identity match; executable bytes hash to the immutable receipt.
2. Identity proves untracked external installed-release `@yylo/cli`; package name/version match the receipt and valid version syntax, not only the launcher version string.
3. Installed package manifest resolves exactly one script source/destination; candidate runtime blob equals its exact package-template bytes. Compare candidate Git blobs, not mutable working-copy approximations.
4. Candidate managed-assets has valid schema, package identity, script asset type, template version, source hash and installed hash, all consistent with those bytes and release.
5. Reject traversal, absolute/escaping asset paths, unsafe symlinks, missing executable/package/template/runtime, malformed/ambiguous JSON/manifests, duplicate assets, stale receipt, changed executable or package, wrong name/version/type/hash and partial registration. Recheck proof under ordinary admission; no package mutation or bound receipt rewriting.

Negative tests must independently vary every identity component above, including another package with identical runtime bytes, task/controller executable mismatch, source marker with missing twin, modified/missing consumer runtime, malformed/missing manifest and unsafe paths. Positive case: exact official runtime plus manifest in a consumer without source tree. Persist proof across managed refresh and repeat admission after refresh. R's six reported regressions cover only part of this matrix and must not be called upstream tests.

## Compatibility, cleanliness, config and authentication contracts

- Dispatch: read a package/script protocol capability before spawning any mutating operation. Unsupported/absent/malformed protocol returns a typed compatibility refusal with selected CLI/script identities and owner-reviewed transition guidance. No speculative mutation to probe support, no auto-fallback to retired verbs. Test new/new, new/old, old/new, missing script, forged capabilities, unsupported schema and active old writer.
- Child identity: bind absolute lexical launcher plus resolved executable/package/version/hash separately from Node. Use canonical launch machinery, not direct provider CLIs. Normalization must preserve the selected CLI for nested `yy` calls even if the Node directory contains another `yy`; fail on identity drift. Test newer global versus older isolated, Node symlink, spaces, changed PATH, relative/empty entries and nested grandchildren. Merely recording the original `yy_executable` is insufficient.
- Cleanliness: capture eligible checkpoint metadata before deriving controller binding; recapture/verify identity after checkpoint. Separate task-owned metadata from unrelated modified/untracked/product bytes. Checkpoint warnings remain best-effort durability information, never product inputs or gates. Dirty unrelated bytes still refuse admission. Test concurrent unrelated changes, checkpoint failure, stale bindings and exact eligible task metadata; reproduce surviving worker paths before touching queue code.
- Config: migration plan classifies all fields using schema ownership, exports product-only fields to an authorized product config or records disposition, preserves safe controller settings and secret references, rejects unknown fields, and validates with the actual target loader before activation. Derivation is not an unrestricted silent filter. Test each of four reported fields separately and together, unknown keys, provider/model preservation, secret redaction, path mappings and drift after binding.
- Provider: resolve required configured model/provider with the canonical policy; distinguish recognized from intentionally custom IDs. Check authentication visibility inside the same sanitized child environment/config without printing values, reasoning or credentials. Missing/unknown selection yields typed early refusal before worktree/queue mutation or costly suites. An owner selects/authenticates an allowed model through supported configuration/auth surfaces, then runs a bounded authorized test-provider probe. No hardcoded substitute, implicit network call, override, or fabricated approval. Test authenticated stub success, missing key early failure, custom-ID consent, redacted logs and child/parent visibility differences.

## Dependency and documentation reconciliation

| Inventory | Provenance and conclusion |
|---|---|
| Controller `.venv_juno`: juno-kanban 2.0.5, no Ledger | R only; old Kanban does not satisfy independent Ledger executable admission |
| Global yylo-ledger 0.2.0 versus policy `>=0.1.0,<0.2.0` | R only; the observed version is outside that reported range, independently of RC3 requirements |
| RC3 requires Ledger 0.3.1 | R and current S agree: `juno-code/package.json:yyloLedger.version`, `src/cli/commands/ledger.ts:LEDGER_VERSION_RANGE`, template `scripts/juno-toolchain-policy.sh` all require exact 0.3.1 |
| Consumer skill refers to 0.2.1rc2 | R only; origin and installation provenance unproven; not a permissible dependency pin |
| Current root `.pi/skills/kanban-workflow/SKILL.md` says YYLO 0.2.2 / Ledger 0.2.0 task CLI | S: stale relative to package's 0.3.1; reconcile canonical generation and all installed twins in a separate authorized change |
| Historical native-record acceptance used source Ledger/CLI 0.2.0 and public probe 0.1.0rc2 | H explicitly excludes release acceptance/live migration; not evidence those combinations support this upgrade |

The current Ledger delegate discovers the independent executable on PATH and performs an exact release handshake; it deliberately has no repository-local fallback. Installing into `.venv_juno` alone does not prove the executable selected by `yy ledger`. Record executable path, version and artifact integrity in both outer and child environments. Historical npm `latest=0.2.2`/`next=0.2.3-rc.3` are R, not current registry observations; no registry queried here. Package metadata and verified installed artifacts outrank stale skills, but current source metadata cannot retroactively authenticate a historical tarball. Freeze both old/new official packages and their actual Ledger distributions in the follow-up fixture.

## Independently scoped follow-up proposals (not filed externally)

| Proposal | Path ownership | Dependencies and acceptance / focused tests |
|---|---|---|
| A: Installed-consumer coherence | Canonical `juno-code/src/templates/scripts/task_workflow_helper.py`, required runtime twin, task-workspace/coherence tests | Independent of QMoWi8; full identity matrix above passes; source mismatch still refuses; installed no-source candidate admitted without bypass |
| B: Consumer upgrade transaction and config/dependency admission | `src/cli/commands/migrate.ts`, canonical `metadata_controller.py`, `target_runtime_provenance.py`, `integration_workspace.py`, `migration_inventory.py`, core config, migration docs/skills and required generated twins | Plan can be built independently; activation depends on A/C/D/E and frozen release inventory. Exact plan/apply/resume boundaries, config loader admission, dependency handshake, backup/rollback and fault injection per boundary; existing metadata-controller/provenance/integration focused suites |
| C: Pre-dispatch script compatibility | `src/cli/commands/merge.ts`, control-plane runtime admission, canonical script capability declaration and merge-command tests | Independent; coordinate 0N4yaK/2lw5JS transition guidance. Old script refused before spawn; no retired executor restoration; clear non-cyclic recovery diagnosis |
| D: Exact child CLI identity | Canonical `managed_agent_runner.py`, child-process environment/launcher, required runtime twin and runner tests | Independent; nested child selects identical intended package despite Node/global collision; preserve canonical runner/model policy |
| E: Early managed-provider preflight | Canonical runner plus configured model/provider/auth resolution and runner/config tests | Independent design, integrates D. Authenticated test-provider success and early redacted unauthenticated refusal; no model calls added to native merge |
| F: Surviving metadata checkpoint/binding ordering investigation | Canonical `controller_checkpoint.py`, runner evidence capture and their focused tests | Collect R logs first; only file a fix if still-supported worker path reproduces. Exact eligible metadata checkpoint works, unrelated dirty bytes refused, checkpoint failure is warning; no FIFO resurrection |
| G: Real installed-consumer upgrade acceptance | `juno-code` installed-binary integration fixtures and fixture documentation; Ledger native-record acceptance owned by Ledger maintainers | Depends on A–E and release-compatible Ledger fixture, F only if reproduced. Execute full matrix below; no publication or live migration authority implied |

Canonical authored templates and declared runtime/generated destinations must remain coherent in each implementation task. This report edits none of them. Follow-ups need their own frozen path admission; Ledger submodule/product changes are not authorized by this report.

## Real-consumer end-to-end fixture specification

Use disposable Git repositories with **no `juno-code` tree**: product main containing notebook, dataset and training-script checksum fixtures; metadata-only controller on its own branch; separate task and protected integration-owner worktrees. Seed exact 0.2.2 managed scripts, inventory/registration variants and archived old receipts. Install immutable official old and RC3 packages outside Git, plus release-matched independently packaged Ledger; a newer global CLI shares selected Node's bin directory. Include macOS/zsh/Node 22.22.3 as incident parity and Linux/Node 22 as CI coverage. Neither synthetic probe packages nor source version strings satisfy release identity acceptance.

Run the approved route from inventory through consumer candidate admission, explicit checks, ordinary queueing/owner delivery, maintenance refresh, Ledger schema migration and native records. Stop before mutation for old parser, partial registration, unknown config, wrong Ledger, wrong package bytes, unauthenticated provider or unrelated dirt. Fault-inject every receipt/target/config boundary; prove safe re-entry without duplicate candidate delivery, record creation or metadata loss. Confirm product/draft hashes and controller isolation after every phase, active producer pinning throughout, and repair persistence after refresh.

Use an intentionally authenticated local test provider through the canonical configured runner for success (no production credentials/network); missing credentials must fail before expensive suites and mutations. Required semantic approval remains explicit, never inferred from stub transport success.

Create two **native Artifact records**, not Tasks: optimization-learning placeholder and neural-network benchmark Markdown table. The independent Ledger artifact help must confirm the surface before use; the proposed fixture calls are:

```bash
yy ledger artifact create --title "Optimization learning notes" --mode inline --media-type text/markdown --file <placeholder.md>
yy ledger artifact create --title "Neural-network benchmarking" --mode inline --media-type text/markdown --file <benchmark-table.md>
```

Capture returned immutable Record IDs, verify kind=artifact, then use that exact Ledger release's documented record/content readback commands by ID. Those readback/migration commands remain an inventory requirement, not guessed flags here. Compare original UTF-8 Markdown bytes and SHA-256, including table pipes, backticks, dollar signs, fenced code, Unicode and final newline; check literal shell payload never executed. Resolve each ID again after refresh/restart and verify creation context points to the controller/project roles. Retain machine receipts and bytes; assert IDs are neither consumer task IDs nor task records. No such creation or readback occurred in P40Jij.

## Validation executed during this triage

- Hydration receipt passed; task-local Node 22.23.2 and exact lock hashes matched frozen receipt, worktree initially clean.
- `python3 juno-code/scripts/tests/consumer-upgrade-triage.test.py`: **3 passed**. Initial run had one test-harness assertion failure because findings include an additional digest field; assertion was narrowed to the relevant code/path/twin before rerunning. The deterministic unchanged failure was not repeated.
- Probes intentionally assert the observed current defects (consumer twin requirement, PATH identity drift, product config retention). They are diagnostic evidence, **not desired behavior or upgrade acceptance**; replace/retire with positive regression tests when each follow-up lands. Consumer probe also encounters absent controller-policy admission, so it makes no full-finish claim.
- `git diff --check`: passed before commit.
- No full suite, release verifier, official package installation, provider launch, external consumer access, Ledger mutation, lifecycle transition or controller asset edit was performed. R's local repair and upgrade remain unverified.
