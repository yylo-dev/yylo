# Packed CLI / skills qualification

Release preparation requires `YYLO_RELEASE_SKILLS_INPUTS` naming a reviewed JSON
file. Missing inputs fail before building. Existing artifact directories are
never erased or reused: select a new external output directory. Successful
prepare binds the copied inputs and packed-skills report into the existing
release manifest, alongside controller-upgrade evidence. Verification checks
both reports, their input/artifact hashes and required scenarios.

Input schema (`commit` must be the full exact Git commit, not a branch/tag):

```json
{
  "schema_version": "yylo_packed_skills_inputs.v1",
  "toolchain": {"node": "22.23.2", "npm": "10.9.8", "git": "git version 2.43.0"},
  "minimum": {"repository": "/external/skills", "version": "2.0.5", "commit": "FULL_40_HEX_COMMIT"},
  "selected": {"repository": "/external/skills", "version": "2.0.5", "commit": "FULL_40_HEX_COMMIT"}
}
```

Tool versions above are examples: approve the exact installed toolchain. The
minimum must equal the packed CLI's declared caret lower bound. This version of
the gate refuses other range syntax instead of guessing a minimum. Both commits
must include coherent VERSION and skills-manifest.json. Existing v2.0.4 lacks a
manifest: it cannot pass this new release gate by substituting a newer submodule.
Use reviewed version preparation to select a new compatible minimum release.

For an already packed CLI (no rebuild):

```sh
node scripts/verify-packed-skills.mjs /external/cli.tgz /external/inputs.json /external/new-report.json
```

The harness installs that tarball with lifecycle scripts disabled in an isolated
prefix/cache/HOME. It pins node/npm/git versions and controls launcher paths.
It qualifies the supported **Git fallback acquisition lane**: npx is deliberately
unavailable, and an isolated Git URL mapping exposes only each exact input commit
through a private test transport tag. Original repository tags are untouched.
This is not qualification of the external `npx skills` tool or proof of public
availability. npm dependency acquisition occurs during isolated preparation;
skills acquisition and behavioral comparisons are offline.

Checks exercise fresh minimum, upgrade minimum-to-selected and fresh selected
through the actual packed CLI, compare every installed skill byte for Codex,
Claude and Pi, and preserve unrelated user skill fixtures. If minimum equals
selected, the upgrade scenario tests repeat-install behavior; select distinct
releases to qualify a version-changing upgrade. Workspaces are retained for
review, including failed attempts; no implicit cleanup or upload occurs.

`YYLO_PACKED_CLI_ARTIFACT=/external/cli.tgz node --test scripts/tests/packed-skills.test.mjs`
runs independent integration fixtures. The positive next-release fixture is
explicitly synthetic; the real historical v2.0.4 negative must fail. Passing it is
not a claim that current public dependencies are release-ready. Public dependency
identity verification and maintainer publication approval remain separate.
