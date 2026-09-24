# Skills release compatibility

`yylo-skills/skills-manifest.json` is producer-owned. It names every released
skill, its invocation semantics, placeholder counts, and contract version.
`src/skills-requirements.json` is independently reviewed consumer policy, not
output generated from that manifest. The installer uses the consumer identities.
The offline `npm run test:skill-contracts` gate compares both with the pinned
source, invocation metadata and the CLI's declared skills version range. Missing
inputs fail; initialize the exact Git submodule before running it.

The release inventory policy is strict: additional released identities require
an explicit consumer policy change. This is not permission to delete unrelated
user-installed skills. Existing installer ownership/digest protections remain.
Behavioral tests in the skills repository and installer suite remain independent
of manifest generation; their requirements are not inferred from the manifest.

## Introduction and existing releases

The previously published skills v2.0.4 has no release manifest. Source addition
of a manifest does not retroactively change that release. Installed CLI runtime
behavior and its current skills range are unchanged by this source gate; existing
v2.0.4 installation remains supported by the installer. A new release qualification
must supply the actual selected and minimum-supported release inputs. It must
not substitute the current submodule for an older tag, synthesize a missing
manifest, or claim this source check proves the entire supported range.

Before publishing a CLI requiring this release contract, the maintainer must
review a new skills release version and the corresponding CLI minimum version
change through version preparation. The public readiness gate must then verify
that exact release. No version or compatibility bump is implicit in adding these
tools. A missing manifest from an older selected release is an actionable release
blocker, not a reason to skip validation or modify an immutable tag.
