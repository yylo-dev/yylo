---
wiki_contract:
  line_limit: 140
  purpose: "Hydrate required task and validation roots through a frozen project workflow."
  failure_mode_prevented: "Unrelated installers block narrow work, or required task-local dependencies are absent or stale."
  runtime_contract_enforced: "Task start records scoped exact-lock preparation; preflight and finish verify without provisioning."
  validation_gate: "npm test -- src/utils/__tests__/worktree-hydration.test.ts src/utils/__tests__/managed-project-assets.test.ts"
  related_sots:
    - "git_worktree_lifecycle.md"
  owns:
    - "Workflow-driven exact-lock dependency preparation during task start and explicit retry."
  does_not_own:
    - "Project-specific commands, network authorization, and sensitive-file source approval."
---

# Task-worktree dependency hydration

## Frozen preparation before implementation

Task targets own `.juno_task/config/worktree-hydration.yaml`. `yy task start`
freezes its exact path and bytes, lints `workflow_class: task_hydration`, and runs
Workflow Runner with the task worktree as project/run root. Before editing, read
the controller workspace policy and inspect the returned hydration result. Stop
if preparation is missing, failed, stale, or leaves tracked/unignored changes.

Every step retains bounded argv execution, an idempotency probe, workflow-fatal
and non-interactive flags, explicit network/sensitive declarations, and declared
outputs. Use reviewed exact-lock, task-local installers. Env files require
owner-approved source/destination pairs through `worktree_hydration.py`, which
copies without echoing content and enforces mode `0600`.

## Explicit scope-selection policy

Existing workflows without `hydration_selection` retain **all steps** and their
historical dependency checks. They are not silently narrowed. To opt in:

```yaml
workflow_class: task_hydration
hydration_selection: admitted_scope_v1
steps:
  - id: node_dependencies
    dependency_root: juno-code
    # Keep the complete command/probe/timeout/network/output declarations.
```

Mark only independently omittable preparation steps with a literal, normalized
worktree-relative `dependency_root`. Untagged steps always run; retain a final
untagged clean-tree check. This is selection, not a dependency scheduler: steps
remain in workflow order, with no inferred dependency graph or shared cache.

Selection uses **admitted paths**, never task prose or the currently small diff.
It includes roots touched by scope and roots needed by applicable focused,
profile, and possible full-suite validation (`cwd` and declared `input_paths`).
A scope wholly inside one validation profile uses that profile's requirements;
mixed, parent, or uncovered scope also keeps the default requirements. Broad
admission remains broad even when the agent intends to edit just one package.
Validation commands using another dependency root must declare that input.

The shipped monorepo workflow labels CLI and benchmark installers separately.
CLI-only admission avoids benchmark installation; benchmark-only admission uses
its package-local checks. Multi-root/default broad admission keeps both. A failed
unselected installer is not executed; a selected failure still blocks readiness.
Consumer workflows retain their existing project-owned preparation policy.

The attempt stores selected/omitted step IDs, admitted paths, required validation
rows, selected dependency roots, the projected workflow, and a hashed selection
receipt alongside runner evidence. Exact Node locks, install stamps, dependency
probes, and installed-content manifests bind required roots to task-local bytes.
The controller does not borrow another worktree's `node_modules`, silently use
global tools, grant network authority, or claim an omitted check passed.

## Retry and relevant-input invalidation

Failures preserve the worktree, ownership, and bounded artifacts in
`HYDRATION_FAILED`. Repair the stated prerequisite and explicitly run
`yy task hydrate TASK_ID` with the current task authority. Existing successful
probes skip satisfied steps; no autonomous retry or takeover occurs.

Scoped readiness refuses missing/stale selected locks, missing or altered
installed dependencies, missing selection evidence, and changed relevant
validation requirements. Unselected lock changes and unrelated task metadata
are not readiness inputs. Preflight and finish verify evidence; neither installs.

After an authorized scope expansion, prior selection is stale until explicit
hydration prepares the expanded scope on the clean task branch. Hydration does
not grant broader edit authority. A changed relevant lock likewise requires
explicit exact-lock preparation. A changed frozen workflow invalidates scoped
readiness and cannot be silently re-frozen by retry: use a separately admitted
workflow identity (normally a new task based on the revised target), preserving
existing work and receipts. Never bypass a frozen workflow's authority to run
new network or sensitive commands.

For Node roots, use Node 22 and the checked-in `package-lock.json`. The canonical
helper's `hydrate-node --cwd ROOT` runs the task-local installer and records
`node_modules/.yylo-package-lock.sha256`; `verify-node-lock --cwd ROOT` checks it.
For other ecosystems, declare their reviewed exact-lock preparation explicitly.
Keep generated outputs ignored, record attempt logs and honest timings, and
verify `git status --short` after preparation. Do not report readiness from a
partial dependency tree or equate source merge with installed-runtime activation.
