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

Use `--format json|ndjson`; add `--raw` for compact JSON. Prefer the long spelling through the managed Ledger facade: the root CLI also uses `-f` for prompt files. The canonical Ledger shell wrapper preserves native `-f` and `--format` options. NDJSON emits exactly one bounded envelope per line; a multi-document delegated NDJSON payload is represented as an array in `data`, while a single document remains that document. Diagnostics, banners, identity details, warnings, and bootstrap progress are written to stderr. A refusal or failure still exits nonzero and emits a typed `error` in the selected format.

Use the `capabilities` command with `--format json --raw` to discover formats and projections instead of copying flags between commands.

## Native Ledger search

Place the format option after the native action, for example:

```sh
yy ledger record search --scope all --text simpl --projection summary --limit 60 --format json
```

Ledger owns native Record serialization. The shell adapter preserves the option
at native command scope rather than moving it to the legacy root parser, whose
format setting does not select the native Record format. This applies to the
`record`, `task`, `wiki`, `workflow`, and `artifact` namespaces. Legacy flat task
commands retain their global-option normalization.

The managed facade retains its public v1 response envelope and strict parser:
JSON `data` contains Ledger's page object (`records`, `next_cursor`, and native
page metadata). NDJSON `data` contains records followed by the native `type: page`
trailer; an empty page is a single trailer object. Pass `next_cursor` unchanged
with `--cursor` to request the next page. Do not count the trailer as a Record.
Malformed bytes still fail as `INVALID_CHILD_PAYLOAD`, not as an empty result.
This fixes argument scope; it does not remove the separate public envelope or
claim complete elimination of both format decisions.

## Compatibility

Human/default output is unchanged. The v1 envelope is additive and opt-in. The pre-existing merge `--json` spelling now emits this envelope; use `.data` for its former payload. Other existing scripts that consume legacy default output may omit `--format`; migrated callers read it from `.data`. Fields may be added compatibly within v1, but existing fields do not change meaning. Removing or changing a field requires a new schema or command version.

`--execution-envelope` remains the separate `juno_execution_envelope.v1` contract for managed provider runs. It now includes `command:{"name":"managed.run","version":1}`; consumers that validate exact keys must admit this additive identity field. Internal release receipts retain their existing receipt schemas; public release plan/status commands use the lifecycle envelope and advertise `plan`/`status` projections through capability discovery.
