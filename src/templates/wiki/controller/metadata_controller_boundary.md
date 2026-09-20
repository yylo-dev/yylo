---
wiki_contract:
  line_limit: 240
  purpose: 'Plan, prepare, verify, and roll back the metadata-only controller boundary without product-ref mutation.'
  failure_mode_prevented: 'Controller/product history reconciliation, tracked runtime copies, in-place conversion, and ambiguous rollback.'
  runtime_contract_enforced: 'metadata_controller.py'
  validation_gate: 'python3 .juno_task/scripts/tests/test_metadata_controller.py'
  related_sots:
    - 'git_worktree_lifecycle.md'
---

# Metadata-only controller boundary

## Agent profile

Metadata controllers may persist safe agent preferences in `.juno_task/config.json`.
`agentProfile: { "version": 1, "promptAssetRoot": ".juno_task/prompts" }` gives
file-backed macros an explicit controller-relative root; path and symlink escape
are refused. Controller, task, and integration-owner invocations share the
registered profile while working/session directories remain invocation-local.
Migration hash-binds config, plan, prompt, environment-source, and Git identity;
every field receives a disposition. `agentProfile.roleHooks` selects controller
hooks only for controllers and product hooks only for task/integration roles.
`environmentBinding` requires explicit authority plus an absolute regular 0600
file. Without a binding, the controller-root `.env.yylo` loads automatically as
an ambient source under the same regular non-symlink 0600 checks; an unsafe
file warns once and is skipped, a missing file stays silent, and an explicit
binding stays authoritative without an ambient double load.
Product paths stay product-only and lifecycle retires; the large plan
survives behind a compact landing page, and exact prepare retry is idempotent.

## Ownership

The controller is a state store, never a product source or integration participant. Controller commits never merge or synchronize into a product target. Its root commit contains only the paths admitted by `.juno_task/config/metadata-controller.json`:

```text
.juno_task/tasks/       canonical current Kanban tasks
.juno_task/ledger/      canonical task history
.juno_task/specs/       top-level task plans and decisions
.juno_task/state/       lifecycle.json plus atomic tasks.json task/queue state
.juno_task/receipts/    top-level final boundary/transition receipts
.juno_task/config/      minimal controller policy
```

Product source, tests, package metadata, release tooling, generated product assets, and bulky workflow attempts are absent. Only top-level spec and final-receipt files cross the migration boundary; nested workflow/lifecycle/task-set evidence, nested receipt trees, and arbitrary state files are rejected. They stay in the preserved rollback controller instead of bloating every future controller commit. Task and ledger directories remain recursive because they are canonical segmented stores. A product branch may retain minimal project lifecycle config, prompts, and install inputs, but it must not contain the controller-private roots declared by `product_forbidden`.

Controller execution comes from one released `yylo` installation outside every linked or unrelated mutable Git worktree and every Git ancestor. NVM global installs beneath the `~/.nvm` checkout must use `yy migrate runtime-install-rebind` with a fresh durable non-Git prefix (for example `~/.local/share/juno/runtimes/X.Y.Z`). The command either resolves the exact registry artifact and verifies its SHA-512/SHA-1 evidence, or accepts one external regular non-symlink npm pack tarball through `--artifact` and authenticates its bounded bytes, SHA-256, `yylo` name, and exact requested version. It installs only the authenticated tarball snapshot with lifecycle scripts disabled (offline for a local artifact) and records package evidence in an immutable success or rolled-back failure receipt before transactional rebind. A missing prerelease may use this exact local-artifact channel, but is never permission to copy a mutable source package by hand. Activated controllers admit their authenticated installed generation independently of compatible product-source movement; differing source bytes alone do not require an upgrade. If an unactivated legacy source controller refuses source drift, or a separately approved source-runtime adoption is needed, use only the complete `yy integration runtime-adopt-source --previous-sha FULL_SHA --target-sha FULL_SHA --install-prefix FRESH_NON_GIT_PATH --output NEW_EXTERNAL_RECEIPT` transaction printed by task-start refusal. It checks a clean fixed target and registered integration owner, safely advances a clean stale detached owner to that exact local target under the target lock, packs and authenticates the exact source artifact without publication, installs outside Git, rebinds the clean controller and atomically selects the receipt-bound public `yy`/`yylo` launcher, refreshes the exact target transition, runs runtime-doctor, executes a fresh `yy merge --help` selection check, and verifies task-start runtime admission. Its receipt distinguishes generations by target SHA and artifact SHA-256, preserves prior executable/config/generation/launcher identity, rolls back only transaction-owned resources on interruption or refresh failure, and returns verified idempotent results for exact completed replay. Conflicting replay, dirty or divergent owners/controllers, changed artifacts, reused outputs or prefixes, customized overlap, and stale receipts fail closed before mutation. `runtime-install-rebind`, `runtime-refresh`, and `scripts update` are deliberately insufficient partial substitutes. The controller is the default agent entry point: that immutable package provisions ignored local `AGENTS.md`, `CLAUDE.md`, and the core Juno skills under `.agents/skills/`, `.claude/skills/`, and `.pi/skills/`. These files guide orchestration without becoming controller history. Product- or domain-specific skills stay with product code; after `yy task start`, the agent enters the returned worktree and loads its product instructions and skills there. `.juno_task/runtime/identity.json` and installed `.juno_task/scripts/` are also ignored local state. Rebinding that installed runtime preflights controller cleanliness and receipt immutability, rolls identity/config back on any failure, and must leave controller `HEAD`, tree, index, and product refs unchanged.

## Preservation-first migration

Existing YYLO 2.0 projects first freeze project facts and owner decisions;
they do not begin with `metadata_controller.py prepare`. The packaged inventory
is read-only and its receipt must live outside the inspected repository:

```bash
yy migrate inventory --project /absolute/project --output /durable/inventory.json
yy migrate owner-template --inventory /durable/inventory.json \
  --output /durable/owner-answers.json
yy migrate generate-policy --inventory /durable/inventory.json \
  --answers /durable/owner-answers.json --output /durable/policy-bundle.json
```

Policy generation refuses unresolved or `block` dispositions and runs the
canonical metadata-controller, task-workspace and risk-policy validators. The
result is still a candidate: it grants no prepare, registration, ref movement,
cleanup, or release authority. See `@@migrate_juno_code_v2_to_v2_1`.

After review, generate the product metadata-removal diff in a disposable linked
worktree. Planning is read-only; apply refuses the inventoried source, the protected
target branch, dirty candidates, unrelated repositories, and stale base commits:

```bash
yy migrate evacuation-plan --inventory /durable/inventory.json \
  --policy /durable/policy-bundle.json --project /absolute/source \
  --output /durable/evacuation-plan.json
yy migrate evacuation-apply --plan /durable/evacuation-plan.json \
  --candidate /absolute/disposable-linked-worktree \
  --output /durable/evacuation-apply.json --allow-disposable-mutation
yy migrate evacuation-verify --plan /durable/evacuation-plan.json \
  --candidate /absolute/disposable-linked-worktree \
  --output /durable/evacuation-verify.json
```

Every controller-private root needs an owner disposition. Product-owned prompts,
config, docs, tests and code outside those roots remain untouched. Only the retired
top-level `lifecycle` and `controllerWorkspace` config keys are removed. Nested
repository or gitlink boundary crossings fail closed. The commands never stage,
commit, move a ref, register a controller, or delete rollback evidence.

The boundary helper deliberately does not register the new controller:

```bash
python3 .juno_task/scripts/metadata_controller.py migration-plan \
  --old-controller /path/to/old-controller \
  --old-branch refs/heads/old-controller \
  --expected-old-head OLD_SHA \
  --new-controller /path/to/new-controller \
  --new-branch refs/heads/juno/controller-metadata-v1 \
  --product-ref refs/heads/main \
  --expected-product-head PRODUCT_SHA \
  --runtime /installed/bin/yy \
  --runtime-version X.Y.Z \
  --policy-bundle /durable/path/reviewed-policy-bundle.json \
  --output /durable/path/migration-plan.json

python3 .juno_task/scripts/metadata_controller.py prepare \
  --plan /durable/path/migration-plan.json \
  --output /durable/path/prepare.json
```

`migration-plan` freezes the exact old controller, product target, installed runtime, selected metadata, excluded product/history inventory, rollback identity, and canonical reviewed metadata/task/risk policies. A single reviewed policy bundle is preferred; alternatively pass both `--task-workspace-policy` and `--risk-policy` with the global `--policy` metadata policy. `prepare` re-reads those exact sources and refuses source or content drift before mutation. It requires a fresh output receipt path, then creates a fresh unrelated root and linked worktree; it neither moves the product target nor changes live controller registration. The root boundary receipt binds every preserved source path, mode, blob identity, and generated policy digest. The old sparse/full controller remains intact.

Task/merge execution and agent startup assess controller generation before selecting local scripts. Recognized exact managed preimages migrate automatically as one package-owned transaction; scripts remain controller-local. Help, version, discovery, and doctor do not migrate. `yy scripts generation doctor` reports the same decision without exposing private preimage bytes.

Evidence must bind both installed packages to retained authenticated tarballs: installation-owned `.yylo-generation-evidence.json`, or npm's exact installed-package SHA-512 receipt plus its existing offline content-addressed cache. No network fetch, semver-only provenance, or guessed predecessor is permitted. Missing evidence, customized overlap, unsupported shared state, or occupied destinations preserve all bytes; only a separately verified old executable **and** script generation may serve as retained fallback.

Use `yy scripts generation resume TRANSACTION_ID` or `rollback TRANSACTION_ID` for an interrupted owned journal; normal first use resumes it automatically. Shared execution leases exclude generation writers, and active task runtime selection remains bound to the frozen attempt and authenticated retained package. No lifecycle lease, hydration record, service, independent skill, unrelated file, or product ref is rewritten to make an upgrade pass.
Activated source and consumer generations admit their complete controller-local runtime through the shared engine, using a tarball-authenticated captured import closure; they never rewrite historical product scripts/inventory or move its ref. Supported state/configuration, registration, ownership and hydration remain mandatory; product-copy equality is not an activated-controller gate. Legacy unactivated controllers retain their explicit recovery requirements. Genuine runtime upgrades may require closing reader sessions. Bounded non-destructive task-local recovery within authorized work needs no repeated approval; shared-runtime activation, destructive actions and ambiguous ownership still require an owner decision. After activation, `scripts doctor` and `integration runtime-doctor` share generation admission rather than requiring obsolete target-bound receipts. Explicit target-bound `yy scripts update --force` remains separately admitted only with exact release/target/receipt/policy proof; it is not generic recovery and never edits tracked policy.

A registered legacy controller whose metadata policy omits only the `integration-workspace.json` classifications must use the dedicated transaction; `scripts update`, including `--force`, intentionally refuses to mutate tracked policy:

```bash
yy migrate metadata-policy plan --root "$PWD" \
  --output /durable/metadata-policy-plan.json
# Review the exact preimage/result bytes, identities, additions, sources, and commit intent.
yy migrate metadata-policy apply --plan /durable/metadata-policy-plan.json \
  --output /durable/metadata-policy-apply.json \
  --authorize-metadata-policy-migration
```

Plan is read-only and binds the physical registered controller/role, branch and HEAD, product ref/head, policy preimage/result, task/risk bytes, package engine and integration-policy source, ignored runtime generation, exact endpoints, semantic additions, and bounded tree/commit intent. Apply rejects alternate indexes, dirt, unsafe topology, stale or tampered evidence, and identity/source races; serializes on repository, target-channel, dedicated policy, and real Git index locks; publishes one exact two-path commit by HEAD CAS; atomically installs the prepared index and endpoint bytes; and emits an immutable external receipt. Already-migrated exact package bytes are a clean no-op. Ordinary checkpoint authority remains unchanged and continues to reject arbitrary `.juno_task/config` edits.

A controller that already has the reviewed metadata policies but still contains the exact retired generated `controllerWorkspace.enabled` / `controller-workspace.json` config must not be registered or refreshed as if it were canonical. Repair only that known migration seam with a reviewed, external plan and a separate apply receipt:

```bash
python3 .juno_task/scripts/metadata_controller.py --policy .juno_task/config/metadata-controller.json \
  config-repair-plan --root /path/to/metadata-controller \
  --branch refs/heads/juno/controller-metadata-v1 --expected-head CONTROLLER_SHA \
  --product-ref refs/heads/main --expected-product-head PRODUCT_SHA \
  --output /durable/config-repair-plan.json
python3 .juno_task/scripts/metadata_controller.py --policy .juno_task/config/metadata-controller.json \
  config-repair-apply --plan /durable/config-repair-plan.json \
  --output /durable/config-repair-apply.json --authorize-config-repair
```

The plan hash-binds the exact controller head/tree, complete retired config content and bytes, the full derived after object and bytes, policy, product ref/head, Git common directory, and branch identity. The derived correction replaces only `controllerWorkspace`; `gitCheckpoint`, `promptMacros`, and every other config key/value remain semantically identical. Apply requires explicit authorization and a clean attached controller, writes a durable external intent before mutation, serializes on repository/controller writer locks, revalidates all frozen identities inside the lock, commits only `.juno_task/config.json`, and reads back unchanged non-config tree entries, branch identity, and product ref. Exact retry recovers a completed intent-bound commit without creating another commit. A lifecycle-bearing config, a different workspace pointer, or uncommitted manual work is preserved and refused for owner review.

Before a separately authorized cutover, run `verify --pending`, `verify-product`, the packaged real-Git acceptance test, Kanban mutation canaries while feature worktrees remain clean, and runtime-rebind verification. Verification and registration planning both require the exact canonical `controllerWorkspace` subobject and reject `lifecycle`, while admitting unrelated valid controller settings; a forged or stale pending receipt cannot activate the retired shape. Then create a `cutover-plan` receipt. The supported registrar remains a separate, explicit boundary:

```bash
yy migrate registration plan \
  --source-controller /path/to/preserved-controller --source-ref refs/heads/old-controller --expected-source-head OLD_SHA \
  --target-controller /path/to/metadata-controller --target-ref refs/heads/juno/controller-metadata-v1 --expected-target-head NEW_SHA \
  --product-root /path/to/integration-owner --product-ref refs/heads/main --expected-product-head PRODUCT_SHA \
  --runtime /installed/bin/yy --runtime-version 2.1.1 --inventory /durable/inventory.json \
  --policy-bundle /durable/policy-bundle.json --pending-verification /durable/pending-verify.json \
  --output /durable/registration-plan.json

# Separate owner authorization is required at this line.
yy migrate registration apply --plan /durable/registration-plan.json \
  --output /durable/registration-apply.json --authorize-apply
yy migrate registration verify --plan /durable/registration-plan.json \
  --output /durable/registration-verify.json
```

The apply receipt is preceded by an immutable intent receipt. Repeating apply
is idempotent. If interruption leaves only planned endpoint values, the same
authorized command completes them; foreign config values fail closed. The
registrar never moves product or controller refs and refuses dirty, detached,
stale, runtime-drifted, policy-drifted, or unrelated worktrees. The same atomic
transition registers the product worktree as `integration-owner`, binds
`protected-integration.v1` authority and its exact starting commit, and proves
that strict Kanban writes are refused there. Planning requires a fresh product
worktree with no pre-existing workspace-role authority. A legacy source
controller may have either the explicit `controller` role or no persisted role
when the registered resolver proves it is the active controller.

Rollback is equally explicit: preserve the prior controller worktree/ref,
verify the active metadata controller, and run `yy migrate registration
rollback --plan /durable/registration-plan.json --output
/durable/registration-rollback.json --authorize-rollback`. It restores the
exact frozen registration values, all three product role values, and the
pending-controller role. Neither
transition merges controller commits into product history, rewrites history,
deletes a worktree, pushes, or releases. Keep every receipt outside the Git
common directory and all protected worktrees.

## Operating topology after cutover

The controller branch remains separate because it owns Kanban state. A separate
long-lived integration branch is not required merely to synchronize controller
and product state. `integration-owner` is a protected clean worktree role for
the real product target. Keep its checkout detached while native delivery updates
the target ref with expected-old protection; attach it to the exact target only
for separately authorized shared validation, servers, release, or deploy. A
project may still choose a staging branch as explicit product policy, but the
controller never merges into it.

```text
metadata controller branch/worktree (Kanban, ledger, decisions, receipts)
        | task start X                         | task start Y
        v                                      v
feature/X branch + worktree              feature/Y branch + worktree
  agent edits + project checks             agent edits + project checks
        | task finish                         | task finish
        v                                     v
  immutable source X                    immutable source Y
        | yy merge land X                     | independently selectable
        v                                     v
private native-Git candidate -- expected-old update --> product target ref
        |
        +-- automatic lifecycle projection --> Ledger
            (`yy merge project X` retries projection)
```

Run Kanban and task/merge orchestration from the metadata controller. Run agent
development and focused tests from the returned feature worktree. Run the full
multi-service stack, integration/E2E validation, release build, and deploy
manager from the clean integration-owner worktree after queued changes merge.
Deployment remains a separate authority. Never deploy from the controller or
from an unfinished feature worktree. If feature worktrees need local servers,
give each isolated ports and state; otherwise keep the shared stack solely in
the integration-owner worktree.

The transition is serialized: stop shared servers and require a clean checkout,
detach the integration owner before the explicit `yy merge land TASK_ID`, read
its Git-and-Ledger result, and run `yy merge project TASK_ID` only if projection
needs recovery. Attach the exact target only for shared validation/deployment and detach before another
mutation. Never leave the target ref attached while expecting native expected-old
delivery to advance it.

## Refusal rules

Refuse when any of these identities drift:

- old controller path, attached full branch ref, or expected head;
- reviewed metadata controller/product refs;
- Git common directory;
- installed released executable, version, or executable digest;
- metadata policy digest;
- new destination freshness;
- metadata-only root/tree boundary or local runtime binding;
- product target expected SHA or controller-private path absence.

Verification accepts ordinary descendants of the prepared root while checking the unique root tree against the receipt-bound source bytes and the current tree against the narrow boundary. Current tasks, ledger, and top-level specs must each remain nonempty; optional metadata classes are not invented as requirements. Canonical config/policy, state files, and the immutable root boundary receipt must remain present and structurally valid.

`tracked_top_level_files` means direct child files only. One history-preserving recovery rule exists for controllers created by older checkpoint generations: a regular nested `.juno_task/specs/**/artifacts/**` blob is admitted only when its current blob was committed atomically with a canonical task/ledger entry containing its exact path. A directly referenced report may bind a same-directory companion only by its exact filename. Runtime-bootstrap dry-run reports every recovered path, attribution commit, reference, and owning rule. This does not rewrite or relocate evidence. Any later artifact change must establish fresh canonical attribution in the same commit; dirty nested evidence cannot self-attest and checkpoint refuses it before staging. Unattributed nested files must be preserved for owner-reviewed externalization rather than deleted to make verification pass.

Creating or staging a product path, deleting the whole canonical board, deleting a generated control, adding nested spec/receipt evidence outside that narrow historical rule, adding symlinks/gitlinks, or adding an unrecognized state/config path makes verification fail. Diagnostics list every exact path, reason, and policy rule. Runtime updates are local generation changes, not controller/product synchronization work.
