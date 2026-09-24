# Read-only public dependency readiness

After preparing the exact CLI artifacts, packed-skills evidence and reviewed
four-component portable bundle, run:

```sh
scripts/release-cli.sh readiness CLI_VERSION BENCHMARK_VERSION /external/bundle.json
# Or the standalone read-only verifier after building the repository tools:
node scripts/verify-public-dependencies.mjs /external/bundle.json /external/prepared-manifest.json
```

The verifier consumes the existing `yylo_release_bundle.v1` and prepared release
manifest; it does not introduce another release plan or publication engine. It
checks package declarations, prepared CLI/Benchmark artifact identities, and the
selected/minimum skills identities from packed acceptance before network reads.

It queries public npm and PyPI version metadata, checks the exact artifact URLs,
downloads bytes with bounds and verifies SHA-256 and length. Skills additionally
require the public release tag to resolve to the approved commit; annotated tags
are followed with a finite bound. Both selected and minimum-supported skills
manifests must be publicly retrievable at their exact qualified commit/digest.
Local gitlinks alone never prove public readiness.

Results distinguish `exact`, `absent`, `conflict` and `unavailable`, with bounded
reason codes. Only authoritative HTTP 404 is absence. Authentication errors,
rate limiting, malformed responses, timeout, excessive redirects and oversized
responses fail closed. No registry credentials are read, no uploads occur, and
no cache is deleted. Read-only redirects are restricted to approved public HTTPS
hosts; custom registries/private dependency hosting are intentionally unsupported.

`ready` is a point-in-time observation, not a permanent lease or permission to
publish. Run it immediately before an independently approved publication. This
iteration does not change publication orchestration or add retries: the npm E404
publication-helper defect is outside this verifier's scope. An accepted-but-
processing upload must be resolved by its maintainer continuation, not by treating
this verifier's temporary absence result as permission to upload again.

Current source adds a skills manifest after published v2.0.4. The new local skills
commit is not public merely because its gitlink has merged. Qualification still
requires reviewed new versions, actual dependency publication under separate
authority, and successful public readback. Synthetic test fixtures are not release
readiness evidence.
