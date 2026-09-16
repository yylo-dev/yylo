# Machine output contract

YYLO 0.2.3 introduces the opt-in `yylo.machine-response.v1` envelope for controller lifecycle commands.

```json
{
  "schema_version": "yylo.machine-response.v1",
  "command": { "name": "task.status", "version": 1 },
  "status": "success",
  "projection": "default",
  "data": {},
  "error": null
}
```

Use `--format json|ndjson`; add `--raw` for compact JSON. Ledger keeps its established short spelling, `-f json|ndjson --raw`. NDJSON emits exactly one bounded envelope per line; a delegated NDJSON payload is represented as an array in `data`. Diagnostics, banners, identity details, warnings, and bootstrap progress are written to stderr. A refusal or failure still exits nonzero and emits a typed `error` in the selected format.

Use the `capabilities` command with `--format json --raw` to discover formats and projections instead of copying flags between commands.

## Compatibility

Human/default output is unchanged. The v1 envelope is additive and opt-in. The pre-existing merge `--json` spelling now emits this envelope; use `.data` for its former payload. Other existing scripts that consume legacy default output may omit `--format`; migrated callers read it from `.data`. Fields may be added compatibly within v1, but existing fields do not change meaning. Removing or changing a field requires a new schema or command version.

`--execution-envelope` remains the separate `juno_execution_envelope.v1` contract for managed provider runs. It now includes `command:{"name":"managed.run","version":1}`; consumers that validate exact keys must admit this additive identity field. Internal release receipts retain their existing receipt schemas; public release plan/status commands use the lifecycle envelope and advertise `plan`/`status` projections through capability discovery.
