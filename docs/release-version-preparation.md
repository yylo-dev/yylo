# Reviewed version preparation

Run the repository maintainer tool from an authorized release-preparation
checkout, never an installed controller package or protected integration owner.
It has no publication, commit, tag, push or activation operation.

Create an external reviewed intent file, for example:

```json
{
  "versions": {"cli": "0.2.10", "skills": "2.0.5"},
  "compatibility": {"skills": "^2.0.5"}
}
```

These are illustrative proposed versions, not approval to release them. Version 1
supports stable `X.Y.Z` bumps and caret skills ranges; unsupported prerelease or
range syntax fails explicitly. Ledger and Benchmark compatibility is exact.

```sh
python3 scripts/release-plan.py plan /external/intent.json --output /external/new-plan.json
python3 scripts/release-plan.py check /external/new-plan.json
# Review versions, every file diff, source identities, checks and ordering.
python3 scripts/release-plan.py apply /external/new-plan.json --approve REVIEWED_PLAN_SHA256
python3 scripts/release-plan.py check /external/new-plan.json
```

The output is exclusive-create and external. The plan binds the three repository
HEADs, all known version-bearing fields and their exact preimages, and every
resulting file. Only explicitly selected package versions advance. Dependency
compatibility is an explicit intent, not an inferred bump of every package.

Owned fields include CLI/Benchmark manifests and both lockfile root versions,
current capability `sourceVersion` fields, Ledger's authored `__version__`, skills
VERSION/plugin/manifest identities, CLI dependencies, and the Ledger shell policy
and its declared runtime twin. Historical release evidence and old documentation
examples are intentionally not globally rewritten. New version-bearing fields
must be added to the inventory and independent tests when introduced.

`check` distinguishes prepared, partial and applied bytes. It does not claim that
a prepared plan is release-ready. Partial application fails and preserves bytes;
review it, then use `apply --continue --approve ...` with unchanged repository
HEADs. Conflicting contents, occupied temporary files and moved source revisions
refuse. Reapplying an already applied plan makes no changes. Review and remove an
interrupted `.yylo-bump-pending` temporary only with separate cleanup authority.

Commit each changed dependency repository first, then stage its exact gitlink in
the parent along with root version changes. Before the parent commit:

```sh
python3 scripts/release-plan.py check /external/new-plan.json --delivery
```

That check refuses uncommitted dependency changes, unstaged/stale gitlinks and
unplanned tracked changes. It does not commit anything. There is no atomic commit
across repositories. Unrelated dirty bytes appearing after planning are preserved;
conflicting planned files refuse. Use exclusive maintainer ownership during apply;
per-file atomic replacement is not a general concurrent-editor lock.

The plan records required packed fresh/upgrade, minimum-skills and public identity
checks. Passing version consistency is not evidence those checks ran. Publication
must wait for all dependency releases in the recorded order, immutable artifact
qualification and a separately approved maintainer operation.
