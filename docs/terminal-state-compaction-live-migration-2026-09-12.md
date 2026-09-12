# Terminal-state compaction live migration — 2026-09-12

The registered 2.1 metadata controller completed the owner-authorized Phase A
terminal-state compaction after target commit
`f5ee725cba27b33e27fc6474ff3480247f1e3c8c` was integrated and its managed
runtime generation was adopted.

## Frozen plan

- Controller source HEAD: `da70f80c4ef2aed0d771aef04b28a1f1a2386f7d`
- Original state: 62,158,805 bytes
- Original state SHA-256:
  `ed341b506164aafd0309a82c5750d971afc0d9b10c0b60c7f34e18f2143f0a93`
- Selected records: 540 (`MERGED`: 410, `WITHDRAWN`: 130)
- Preserved nonterminal records: 70
- Projected state: 2,101,010 bytes (96.6199% reduction)
- Plan SHA-256:
  `96f2ed86d1324d87612160b4c527ace8411c89afc6ed2552a9bcc903e07d4606`

The reviewed selection exactly matched all and only terminal records. The cold
ref did not exist before apply, and the plan exceeded the required 90% reduction
while remaining below the 5 MiB target.

## Applied archive

- Cold ref: `refs/juno/cold/task-state`
- Cold commit: `9f404ee77a1e7afb42beffe6276e02d7e17696c6`
- Archive ID: `ed341b506164aafd0309a82c`
- Manifest SHA-256:
  `9058ebebd13752090e5366cce11158b4b629ef153b8ab8a165ce7f145bcc4568`
- Applied hot state SHA-256:
  `2f4260cf456017f2b4d7ca2e92821008f8c87b7486904c3c310969fa7733fcbc`
- Applied hot state size: 2,101,010 bytes
- Compressed packs: 4
- Largest stored pack: 2,167,609 bytes
- Largest expanded pack: 16,743,945 bytes

Apply published and read back the archive before atomically replacing the hot
records. Independent post-apply inspection read all 540 records from the cold
Git objects, verified every pack and record digest, checked every manifest index
location and state, and matched every record to its hot tombstone. Explicit cold
lookup recovered the complete `jz6kE8` record with its expected digest.

After the next ordinary lifecycle checkpoint, hot state was 2,117,591 bytes at
controller commit `d93c13ce04bae7fa0235ba043de92054ad50ccc0`.
That checkpoint's largest new blob was the state file itself, so it introduced no
50 MiB GH001-sized blob. Active task status, terminal tombstone status, managed
runtime doctor, and workspace doctor completed successfully; workspace doctor
reported only pre-existing topology warnings.

No product history was rewritten. No push, package publication, deployment,
worktree cleanup, archive packing, or cold-ref retention mutation was performed.
Historical oversized blobs remain intact and may still produce warnings when
previously unpublished commits are first pushed.
