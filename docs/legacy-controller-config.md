# Legacy metadata-controller configuration

Agent startup does not repair ownership conflicts. A metadata-only source with
`workingDirectory`, `sessionDirectory`, `gitFlow`, `autoDependencyUpdate`, `hooks`,
`skipHooks`, or retired `lifecycle` is rejected, including explicit config files.
The diagnostic names the source and every rejected field, never their values.
Even a legacy cwd equal to the controller is product-owned persisted state; the
agent cwd instead comes from invocation context. Registered task invocations keep
their product cwd.

## Why old fields can remain

Historical project defaults persist product cwd/session/hook settings. The old
workspace-pointer-only `config_repair_plan` changed `controllerWorkspace` and
preserved every other key. It could therefore produce metadata-only JSON that
its loader rejects. This writer now refuses that combination and points to the
ownership migration below. This proves a retention path, not which operation
modified any particular consumer's file. Do not modify a reported consumer to
investigate it.

## Explicit reviewed repair

Run from the registered controller with its exact bound package:

```sh
yy scripts doctor
yy migrate controller-config plan --root "$PWD" --output /external/config-plan.json
# Review the entire immutable plan and all field dispositions first.
yy migrate controller-config apply --plan /external/config-plan.json \
  --output /external/config-apply.json --authorize-config-repair
```

The controller must be clean, attached to its registered branch, and bound to
the package running the migration. Plans bind the config hash, controller
HEAD/tree, product ref, policy, runtime package/engine, registration, and any
managed-generation receipt. Apply rechecks those identities under repository
writer locks, requires explicit authorization, changes only config in one
commit, and preserves the original bytes in its parent. Reapplying the same plan
is idempotent. Dirty notebooks/config, symlinks, a moved ref, or runtime drift
refuse without replacing the input. External plan/receipt paths must be outside
all linked worktrees and Git administration.

Supported agent preferences, prompt mappings, and an existing agent profile are
retained. Legacy product fields and retired lifecycle state are classified and
excluded from the new controller config; their original values remain in the
frozen parent, not activated as controller hooks. This narrow repair does not
install product settings in a task checkout. Review a separate product-overlay
migration if those settings are still required. Unknown or secret-owned fields
refuse the narrow repair: inspect the CLI's migration inventory help and use the
reviewed full migration rather than deleting fields or copying raw config.

Runtime mismatch is a separate failure. Inspect `yy scripts doctor` and the
CLI's runtime-rebind migration help; runtime installation/rebinding requires its
own owner authority. Config repair never performs an upgrade or weakens receipt
binding. An older binding that cannot execute this migration must be resolved by
that supported maintenance path first.

## Focused validation

- Config tests: source diagnostics, no rewrites, profiles, explicit files, cwd.
- Metadata-controller real-Git tests: reviewed repair, preference/parent/product
  preservation, dirty/stale inputs, runtime mismatch, tampered dispositions,
  and workspace-only writer refusal.
- After `npm run build`, run `node scripts/test-controller-config-startup.mjs`.
  This exercises the packaged migration CLI, rejected Pi startup, and migrated
  packaged configuration/engine preflight with only provider dispatch stubbed.
  No model, network calls, or package installs are used.
