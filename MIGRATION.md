# Managed instruction bundle migration

## Typed-lifecycle instruction bundle

YYLO now treats controller guidance, lifecycle prompts, skills, wiki, workflows,
and managed runtime scripts as one instruction bundle. The installed
`.juno_task/managed-assets.json` receipt uses schema 2 and records
`juno_instruction_bundle.v1`, semantic version `1.0.0`, the exact package
version, asset count, a SHA-256 over every destination and source/installed
hash, and a final SHA-256 over the complete bundle identity. A partial or mixed receipt fails before managed mutation or shadow canary.

Fresh installs and upgrades use the same transactional `yy scripts update`
path. Migration from schema 1 to schema 2 accepts a coherent schema-1 receipt
for one upgrade and replaces it only after all owned bytes succeed. User-owned product/domain instructions and
customized managed files are preserved as conflicts; use `--force` only after
reviewing the generated candidates and backups. Interrupted updates restore the
exact prior roots, and retry is idempotent.

Before rollout:

```bash
yy scripts update
yy doctor workspace
npm run test:managed-assets
npm run test:skill-contracts
```

Rollback by reinstalling the prior exact YYLO package; retain immutable receipts
and review any preserved
customizations before rerunning `yy scripts update`. Schema-1 instruction
receipts remain upgrade-compatible for this release only. Once a runtime declares
instruction semantic version `1.0.0` mandatory, missing, mixed, or older bundles
are unsupported and managed agent dispatch must remain blocked.

This migration grants no target CAS, RC/tag, push, publication, deployment,
production mutation, or cleanup authority.
