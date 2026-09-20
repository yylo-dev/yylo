#!/usr/bin/env bash
# Single compatibility policy for Juno 2's Kanban runtime.
# Keep consumers executable-agnostic: they source this file and validate output.

JUNO_KANBAN_COMPAT_RANGE='>=2.0.5,<3.0.0'
YYLO_LEDGER_COMPAT_RANGE='0.3.3'

juno_kanban_parse_compatible_version() {
    local output="${1-}"
    python3 - "$output" <<'PY'
import re
import sys

output = sys.argv[1]
matches = re.findall(r"(?<![0-9A-Za-z])([0-9]+)\.([0-9]+)\.([0-9]+)(?:[-+][0-9A-Za-z.-]+)?(?![0-9A-Za-z])", output)
if len(matches) != 1:
    print("expected exactly one semantic version in --version output", file=sys.stderr)
    raise SystemExit(2)
major, minor, patch = (int(part) for part in matches[0])
if (major, minor, patch) < (2, 0, 5) or major >= 3:
    print(f"unsupported juno-kanban version {major}.{minor}.{patch}; required >=2.0.5,<3.0.0", file=sys.stderr)
    raise SystemExit(3)
print(f"{major}.{minor}.{patch}")
PY
}

yylo_ledger_parse_compatible_version() {
    local output="${1-}"
    python3 - "$output" <<'PY'
import re
import sys

output = sys.argv[1]
match = re.fullmatch(r"yylo-ledger ([0-9]+)\.([0-9]+)\.([0-9]+)(?:rc[0-9]+)?", output.strip())
if not match:
    print("expected exactly one full-line yylo-ledger identity in --version output", file=sys.stderr)
    raise SystemExit(2)
major, minor, patch = (int(part) for part in match.groups()[:3])
if (major, minor, patch) != (0, 3, 3) or "rc" in output:
    print(f"unsupported yylo-ledger version {output.strip()}; required 0.3.3", file=sys.stderr)
    raise SystemExit(3)
print(f"{major}.{minor}.{patch}")
PY
}

juno_kanban_check_executable() {
    local executable="${1:?juno-kanban executable is required}"
    local label="${2:-runtime}"
    local output version policy parser
    if [[ ! -x "$executable" ]]; then
        echo "juno-kanban $label executable is missing or not executable: $executable" >&2
        return 1
    fi
    # Identity checks are metadata-only. Close stdin so a heredoc/pipe remains
    # untouched for the subsequent create command; the CLI treats readable stdin
    # as task-body input even when --version is present.
    if ! output=$("$executable" --version </dev/null 2>&1); then
        echo "juno-kanban $label --version failed: $executable: $output" >&2
        return 1
    fi
    case "${executable##*/}" in
        yylo-ledger)
            parser=yylo_ledger_parse_compatible_version
            policy="$YYLO_LEDGER_COMPAT_RANGE"
            ;;
        *)
            parser=juno_kanban_parse_compatible_version
            policy="$JUNO_KANBAN_COMPAT_RANGE"
            ;;
    esac
    if ! version=$("$parser" "$output"); then
        echo "juno-kanban $label identity rejected: executable=$executable output=$output policy=$policy" >&2
        return 1
    fi
}
