#!/usr/bin/env bash

# yylo.sh
#
# Purpose: Shell wrapper for yylo CLI
#
# This script integrates bootstrap.sh with the main CLI entry point.
# When users run 'yylo', this wrapper:
# 1. Checks if project is initialized (.juno_task exists)
# 2. If initialized: Runs bootstrap.sh to ensure Python environment is ready
# 3. Executes the actual TypeScript CLI with all arguments
#
# Architecture: yylo = shell-shim + yylo logic
#
# Created by: yylo build system
# Auto-generated during npm build

set -euo pipefail

# Bounded 0.1 migration: accept legacy environment names without keeping them
# active. Conflicting mixed installs/configurations fail instead of silently
# selecting one runtime identity. Remove after the documented RC migration window.
while IFS='=' read -r legacy_name _; do
    [[ "$legacy_name" == JUNO_CODE_* ]] || continue
    canonical_name="YYLO_${legacy_name#JUNO_CODE_}"
    legacy_value="${!legacy_name}"
    # Explicit canonical configuration takes precedence over inherited legacy
    # telemetry/configuration during the bounded migration window.
    if [ "${!canonical_name+x}" = x ]; then continue; fi
    printf -v "$canonical_name" '%s' "$legacy_value"
    export "$canonical_name"
done < <(env)

# `yy` is generic, so refuse a mixed installation when its canonical peer is
# present but resolves to another package. Missing `yylo` remains valid for
# source-checkout and package-manager linking workflows.
if [ "$(basename "$0")" = yy ]; then
    yylo_peer="$(command -v yylo 2>/dev/null || true)"
    if [ -n "$yylo_peer" ] && [ ! "$0" -ef "$yylo_peer" ]; then
            echo "yylo: executable collision: yy and yylo resolve to different installations." >&2
            echo "yy=$0" >&2
            echo "yylo=$yylo_peer" >&2
            echo "Remove the unrelated or legacy yy executable, then reinstall @yylo/cli." >&2
            exit 78
    fi
fi

# Preserve the process environment that executable delegates must observe.
# Node selection below may normalize PATH for YYLO's own runtime, but a
# transparent delegate must discover and execute from the caller's PATH.
YYLO_CALLER_PATH="$PATH"
if [ "${YYLO_NODE_EXECUTABLE+x}" = x ]; then
    YYLO_CALLER_NODE_EXECUTABLE_SET=1
    YYLO_CALLER_NODE_EXECUTABLE="$YYLO_NODE_EXECUTABLE"
else
    YYLO_CALLER_NODE_EXECUTABLE_SET=0
    YYLO_CALLER_NODE_EXECUTABLE=""
fi

# Derive identity only from the executable boundary. ypl sources this wrapper,
# preserving its own $0; yy is an npm symlink to this file. exec -a carries the
# derived identity in kernel-owned argv[0] without adding a marker argument or
# requiring independently pinned runtimes to understand a new option.
case "$(basename "$0")" in
    yy) YYLO_LAUNCH_SURFACE_VALUE=yy ;;
    ypl|ypl.sh) YYLO_LAUNCH_SURFACE_VALUE=ypl ;;
    yylo|yylo.sh) YYLO_LAUNCH_SURFACE_VALUE=yylo ;;
    *) YYLO_LAUNCH_SURFACE_VALUE=yylo ;;
esac

# Get the directory where this script is located
# IMPORTANT: Resolve symlinks first (npm creates symlinks in /usr/local/bin or /opt/homebrew/bin)
# We need the real path to find cli.mjs in the same directory
if [ -L "${BASH_SOURCE[0]}" ]; then
    # Follow the symlink to get the real script location
    REAL_SCRIPT="$(readlink "${BASH_SOURCE[0]}")"
    # If it's a relative symlink, make it absolute relative to the symlink location
    if [[ "$REAL_SCRIPT" != /* ]]; then
        REAL_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && cd "$(dirname "$REAL_SCRIPT")" && pwd)/$(basename "$REAL_SCRIPT")"
    fi
    SCRIPT_DIR="$(cd "$(dirname "$REAL_SCRIPT")" && pwd)"
else
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

# Path to the actual CLI entrypoint (Node.js)
CLI_ENTRYPOINT="${SCRIPT_DIR}/cli.mjs"
INVOCATION_BOUNDARY="${SCRIPT_DIR}/invocation-boundary.mjs"

# Routing must never execute resolver bytes from a mutable product/task
# checkout. Source and packaged layouts both place templates beside bin/utils.
PACKAGED_CONTROLLER_RESOLVER="${SCRIPT_DIR}/../templates/scripts/controller_resolver.py"

# Path to bootstrap.sh (should be in .juno_task/scripts after init)
BOOTSTRAP_SCRIPT=".juno_task/scripts/bootstrap.sh"

requests_help() {
    local token
    for token in "$@"; do
        [ "$token" = "--" ] && return 1
        case "$token" in -h|--help) return 0 ;; esac
    done
    return 1
}

classify_prebootstrap_command() {
    PREBOOTSTRAP_COMMAND=""
    PREBOOTSTRAP_SUBCOMMAND=""
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --) shift; PREBOOTSTRAP_COMMAND="${1:-}"; PREBOOTSTRAP_SUBCOMMAND="${2:-}"; break ;;
            -q|--quiet|--silent|--no-color|--enable-feedback|--no-hooks|--no-hook) shift ;;
            -c|--config|-l|--log-file|--log-level|-s|--subagent|-b|--backend|-m|--model|--agents|--mcp-timeout|-r|--resume|--stale-threshold|--on-hourly-limit|--thinking)
                [ "$#" -ge 2 ] || return 1
                shift 2 ;;
            --config=*|--log-file=*|--log-level=*|--subagent=*|--backend=*|--model=*|--agents=*|--mcp-timeout=*|--resume=*|--stale-threshold=*|--on-hourly-limit=*|--thinking=*|-c=*|-l=*|-s=*|-b=*|-m=*|-r=*) shift ;;
            -v|--verbose)
                shift
                case "${1:-}" in 0|1|2|true|false|yes|no) shift ;; esac ;;
            --verbose=*|-v=*) shift ;;
            *) PREBOOTSTRAP_COMMAND="$1"; PREBOOTSTRAP_SUBCOMMAND="${2:-}"; break ;;
        esac
    done
    case "$PREBOOTSTRAP_COMMAND" in
        -V|--version|info|where|benchmark|ledger|kanban|task|merge|integration|evidence) return 0 ;;
        doctor) [ "${2:-}" = "workspace" ] && return 0 ;;
    esac
    return 1
}

establish_node_contract() {
    local node_dir entry normalized=""
    local -a path_entries
    node_dir="$(cd "$(dirname "$YYLO_NODE_EXECUTABLE")" && pwd -P)"
    YYLO_NODE_EXECUTABLE="$node_dir/$(basename "$YYLO_NODE_EXECUTABLE")"
    export YYLO_NODE_EXECUTABLE
    IFS=: read -r -a path_entries <<< "$PATH"
    for entry in "${path_entries[@]}"; do
        [ -n "$entry" ] || continue
        [ "$entry" = "$node_dir" ] && continue
        normalized="${normalized:+$normalized:}$entry"
    done
    PATH="$node_dir${normalized:+:$normalized}"
    export PATH
}

require_compatible_node() {
    local ambient="" ambient_version="" candidate version major minor patch
    local nvm_root="" best_path="" best_major=-1 best_minor=-1 best_patch=-1
    YYLO_NODE_EXECUTABLE=""

    ambient="$(command -v node 2>/dev/null || true)"
    if [ -n "$ambient" ]; then
        ambient_version="$("$ambient" -p 'process.versions.node' 2>/dev/null || true)"
        if [[ "$ambient_version" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+) ]]; then
            major=$((10#${BASH_REMATCH[1]}))
            minor=$((10#${BASH_REMATCH[2]}))
            if (( major > 20 || (major == 20 && minor >= 10) )); then
                YYLO_NODE_EXECUTABLE="$ambient"
                establish_node_contract
                return 0
            fi
        fi
    fi

    if [ -n "${NVM_DIR:-}" ]; then
        nvm_root="$NVM_DIR"
    elif [ -n "${HOME:-}" ]; then
        nvm_root="$HOME/.nvm"
    fi
    if [ -n "$nvm_root" ]; then
        for candidate in "$nvm_root"/versions/node/v*/bin/node; do
            [ -x "$candidate" ] || continue
            version="$("$candidate" -p 'process.versions.node' 2>/dev/null || true)"
            [[ "$version" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+) ]] || continue
            major=$((10#${BASH_REMATCH[1]}))
            minor=$((10#${BASH_REMATCH[2]}))
            patch=$((10#${BASH_REMATCH[3]}))
            (( major > 20 || (major == 20 && minor >= 10) )) || continue
            if (( major > best_major ||
                  (major == best_major && minor > best_minor) ||
                  (major == best_major && minor == best_minor && patch > best_patch) )); then
                best_path="$candidate"
                best_major=$major
                best_minor=$minor
                best_patch=$patch
            fi
        done
    fi
    if [ -n "$best_path" ]; then
        YYLO_NODE_EXECUTABLE="$best_path"
        establish_node_contract
        return 0
    fi

    echo "yylo: no supported Node runtime is available; unsupported Node ${ambient_version:-not found}" >&2
    echo "effective executable: ${ambient:-not found}" >&2
    echo "effective version: ${ambient_version:-unknown}" >&2
    echo "required version: Node.js >=20.10" >&2
    echo "searched fallback: ${nvm_root:-an NVM directory}" >&2
    echo "Install a supported Node runtime or select it before invoking yy." >&2
    return 69
}

start_invocation_boundary() {
    YYLO_INVOCATION_STATE=""
    [ -f "$INVOCATION_BOUNDARY" ] || return 0
    require_compatible_node || return 0
    YYLO_INVOCATION_STATE="$(mktemp "${TMPDIR:-/tmp}/yylo-invocation.XXXXXX")" || return 0
    if ! (exec -a "$YYLO_LAUNCH_SURFACE_VALUE" "$YYLO_NODE_EXECUTABLE" \
        "$INVOCATION_BOUNDARY" start "$PWD" "$YYLO_INVOCATION_STATE"); then
        rm -f "$YYLO_INVOCATION_STATE"
        YYLO_INVOCATION_STATE=""
        return 0
    fi
    # The current runtime validates and continues this bounded private state.
    # Separately pinned runtimes ignore it and are finalized by this wrapper.
    exec 9<"$YYLO_INVOCATION_STATE"
    export YYLO_WRAPPER_LIFECYCLE=9
}

preflight_command_shaped_invocation() {
    [ "$#" -gt 0 ] || return 0
    # Older/mock entrypoints do not implement the side-effect-free protocol.
    # Never probe them with user input: that is the fallback defect itself.
    grep -q 'YYLO_PREFLIGHT_ONLY' "$CLI_ENTRYPOINT" 2>/dev/null || return 0
    case "${1:-}" in -V|--version|-h|--help) return 0 ;; esac
    # One positional token is always the backwards-compatible prompt form.
    # Ask the configured Commander surface to classify multi-token/option-led
    # input before bootstrap can touch project state.
    if [ "$#" -lt 2 ] && [[ "${1:-}" != -* ]]; then return 0; fi
    require_compatible_node || return $?
    # Preflight is not the user-visible runtime and must not consume/unlink the
    # open wrapper lifecycle state intended for either wrapper finalization or
    # the eventual runtime continuation.
    YYLO_PREFLIGHT_ONLY=1 YYLO_WRAPPER_LIFECYCLE= \
        "$YYLO_NODE_EXECUTABLE" "$CLI_ENTRYPOINT" "$@"
}

read_runtime_version() {
    local runtime="$1" output
    output="$(YYLO_PREFLIGHT_ONLY= YYLO_RUNTIME_PROBE=1 "$YYLO_NODE_EXECUTABLE" "$runtime" --version 2>/dev/null)" || return 1
    printf '%s\n' "$output" | tail -n 1 | sed -E 's/^yylo[[:space:]]+//; s/^v//'
}

has_managed_workspace_marker() {
    local current="$PWD" parent
    while true; do
        [ -d "$current/.juno_task" ] && return 0
        parent="$(dirname "$current")"
        [ "$parent" != "$current" ] || return 1
        current="$parent"
    done
}

route_registered_product_control() {
    local operation="${1:-}"
    shift || true
    case "$operation" in ledger|kanban|task|merge|integration|evidence) ;; *) return 1 ;; esac
    case "$operation" in ledger|kanban) has_managed_workspace_marker || return 1 ;; esac
    local effective_operation resolution fields controller invocation role branch source runtime
    case "$operation:$PREBOOTSTRAP_SUBCOMMAND" in
        ledger:*|kanban:*) effective_operation=kanban ;;
        task:status|task:preflight|task:recovery-plan|task:doctor|task:lease-status|task:|task:-h|task:--help) effective_operation=kanban ;;
        task:start|task:run|task:recover-predispatch|task:recover-wall-budget|task:hydrate|task:finish|task:checkpoint|task:child-checkpoint|task:sync|task:recovery-authorize|task:recovery-apply|task:runtime-bootstrap|task:lease-heartbeat|task:lease-handoff|task:lease-successor|task:lease-revoke|task:lease-release) effective_operation=orchestration ;;
        merge:status|merge:plan|merge:|merge:-h|merge:--help) effective_operation=kanban ;;
        merge:next|merge:resolve|merge:review|merge:reopen|merge:reconcile|merge:refresh|merge:drive|merge:withdraw|merge:arbiter|merge:recover-full-suite-failure|merge:recover-authority-drift|merge:supersede-lifecycle-journal) effective_operation=orchestration ;;
        evidence:status|evidence:|evidence:-h|evidence:--help) effective_operation=kanban ;;
        evidence:run|evidence:await) effective_operation=orchestration ;;
        integration:status|integration:|integration:-h|integration:--help) effective_operation=kanban ;;
        integration:sync|integration:runtime-doctor|integration:runtime-refresh|integration:register|integration:repair|integration:push) effective_operation=orchestration ;;
        *)
            echo "yylo: control-plane routing refused unknown $operation subcommand '$PREBOOTSTRAP_SUBCOMMAND'" >&2
            return 2 ;;
    esac
    [ -f "$PACKAGED_CONTROLLER_RESOLVER" ] || return 1
    if ! resolution="$(JUNO_TASK_ROOT= JUNO_CONTROLLER_BRANCH= JUNO_WORKSPACE_ROLE= JUNO_WORKSPACE_ENFORCEMENT=off \
        python3 "$PACKAGED_CONTROLLER_RESOLVER" --cwd "$PWD" --operation "$effective_operation")"; then
        return 2
    fi
    fields="$(printf '%s' "$resolution" | python3 -c 'import json,sys; x=json.load(sys.stdin); print(x["path"]); print(x["current_root"]); print(x["role"]); print(x.get("expected_branch") or ""); print(x["source"])')" || return 2
    controller="$(printf '%s\n' "$fields" | sed -n '1p')"
    invocation="$(printf '%s\n' "$fields" | sed -n '2p')"
    role="$(printf '%s\n' "$fields" | sed -n '3p')"
    branch="$(printf '%s\n' "$fields" | sed -n '4p')"
    source="$(printf '%s\n' "$fields" | sed -n '5p')"
    [ "$controller" != "$invocation" ] || return 1
    case "$role" in task|integration-owner) ;; *)
        echo "yylo: control-plane routing refused persisted workspace role '$role'; run yy doctor workspace" >&2
        return 2 ;;
    esac
    runtime="$(git -C "$controller" config --worktree --get juno.controller.runtimeExecutable 2>/dev/null || true)"
    if [ -z "$runtime" ] || [ ! -f "$runtime" ]; then
        echo "yylo: registered controller runtime is missing or stale; run yy doctor workspace from '$invocation'" >&2
        return 2
    fi
    require_compatible_node || return $?
    local launcher_version runtime_version
    if grep -q 'YYLO_PREFLIGHT_ONLY' "$CLI_ENTRYPOINT" 2>/dev/null; then
        launcher_version="$(read_runtime_version "$CLI_ENTRYPOINT")" || launcher_version=unknown
        runtime_version="$(read_runtime_version "$runtime")" || runtime_version=unknown
        if [ "$launcher_version" = unknown ] || [ "$runtime_version" = unknown ] || [ "$launcher_version" != "$runtime_version" ]; then
            echo "yylo: selected controller runtime cannot be proven to support this explicit command" >&2
            echo "launcher executable: $CLI_ENTRYPOINT" >&2
            echo "launcher version: $launcher_version" >&2
            echo "effective executable: $runtime" >&2
            echo "effective version: $runtime_version" >&2
            echo "Use the direct candidate CLI or align the registered controller runtime before retrying." >&2
            return 2
        fi
    fi
    export JUNO_TASK_ROOT="$controller"
    export JUNO_CONTROLLER_BRANCH="$branch"
    export JUNO_CONTROLLER_SOURCE="$source"
    export JUNO_WORKSPACE_ROLE=controller
    export JUNO_WORKSPACE_ENFORCEMENT=strict
    export JUNO_CONTROL_INVOCATION_ROOT="$invocation"
    export JUNO_CONTROL_INVOCATION_ROLE="$role"
    export JUNO_CONTROL_EFFECTIVE_ROOT="$controller"
    export JUNO_CONTROL_OPERATION="$effective_operation"
    cd "$controller"
    run_owned_command "$YYLO_NODE_EXECUTABLE" "$runtime" "$@"
    ROUTED_COMMAND_STATUS=$?
    return 0
}

# Main execution flow
finish_wrapper_invocation() {
    local code="$1" status="${2:-}"
    [ -n "${YYLO_INVOCATION_STATE:-}" ] || return 0
    "$YYLO_NODE_EXECUTABLE" "$INVOCATION_BOUNDARY" finish "$code" \
        "$YYLO_INVOCATION_STATE" "$status" >/dev/null || true
    rm -f "$YYLO_INVOCATION_STATE"
    YYLO_INVOCATION_STATE=""
    exec 9<&- 2>/dev/null || true
}

run_owned_command() {
    local status=0
    # Foreground execution preserves stdin and TTY semantics for separately
    # pinned runtimes that cannot continue the current lifecycle protocol.
    (exec -a "$YYLO_LAUNCH_SURFACE_VALUE" "$@") || status=$?
    finish_wrapper_invocation "$status"
    return "$status"
}

current_runtime_supports_lifecycle() {
    grep -q 'YYLO_WRAPPER_LIFECYCLE' "$CLI_ENTRYPOINT" 2>/dev/null
}

exec_current_runtime() {
    exec -a "$YYLO_LAUNCH_SURFACE_VALUE" "$YYLO_NODE_EXECUTABLE" "$CLI_ENTRYPOINT" "$@"
}

restore_caller_delegate_environment() {
    PATH="$YYLO_CALLER_PATH"
    export PATH
    if [ "$YYLO_CALLER_NODE_EXECUTABLE_SET" -eq 1 ]; then
        YYLO_NODE_EXECUTABLE="$YYLO_CALLER_NODE_EXECUTABLE"
        export YYLO_NODE_EXECUTABLE
    else
        unset YYLO_NODE_EXECUTABLE
    fi
}

exec_transparent_delegate_runtime() {
    local selected_node="$YYLO_NODE_EXECUTABLE"
    restore_caller_delegate_environment
    exec -a "$YYLO_LAUNCH_SURFACE_VALUE" "$selected_node" "$CLI_ENTRYPOINT" "$@"
}

run_transparent_delegate_runtime() {
    local selected_node="$YYLO_NODE_EXECUTABLE" status=0
    (
        restore_caller_delegate_environment
        exec -a "$YYLO_LAUNCH_SURFACE_VALUE" "$selected_node" "$CLI_ENTRYPOINT" "$@"
    ) || status=$?
    finish_wrapper_invocation "$status"
    return "$status"
}

finalize_bootstrap_failure() {
    local status=$?
    trap - EXIT
    finish_wrapper_invocation "$status"
    exit "$status"
}

main() {
    # Help is terminal discovery. Hand it directly to the packaged Commander
    # surface before lifecycle recording, routing, or project bootstrap.
    if requests_help "$@"; then
        require_compatible_node || return $?
        if grep -q 'YYLO_HELP_PREFLIGHT_ONLY' "$CLI_ENTRYPOINT" 2>/dev/null; then
            local help_status=0
            YYLO_HELP_PREFLIGHT_ONLY=1 "$YYLO_NODE_EXECUTABLE" "$CLI_ENTRYPOINT" "$@" || help_status=$?
            case "$help_status" in
                80) exec_current_runtime "$@" ;;
                0) ;;
                *) return "$help_status" ;;
            esac
        else
            # Capability-unknown legacy runtimes retain the safe broad bypass.
            exec_current_runtime "$@"
        fi
    fi

    # Record the user-visible attempt before preflight, runtime routing, or
    # bootstrap. Current runtimes continue it in the same process; this wrapper
    # finalizes preflight/bootstrap failures and capability-unknown runtimes.
    start_invocation_boundary || return $?

    # Unknown command-shaped input is checked by the compiled command surface
    # before bootstrap, hooks, providers, config, skills, or installers run.
    preflight_command_shaped_invocation "$@" || {
        local status=$?
        finish_wrapper_invocation "$status"
        return "$status"
    }

    # Classify discovery and control-plane commands before touching checkout
    # bootstrap. Registered product worktrees dispatch through the controller's
    # pinned runtime; controller calls retain the current packaged CLI.
    if classify_prebootstrap_command "$@"; then
        ROUTED_COMMAND_STATUS=""
        route_registered_product_control "$PREBOOTSTRAP_COMMAND" "$@" || {
            local status=$?
            if [ "$status" -ne 1 ]; then
                finish_wrapper_invocation "$status"
                return "$status"
            fi
        }
        if [ -n "$ROUTED_COMMAND_STATUS" ]; then
            return "$ROUTED_COMMAND_STATUS"
        fi
        require_compatible_node || {
            local status=$?
            finish_wrapper_invocation "$status"
            return "$status"
        }
        if [ "$PREBOOTSTRAP_COMMAND" = benchmark ]; then
            if current_runtime_supports_lifecycle; then
                exec_transparent_delegate_runtime "$@"
            fi
            run_transparent_delegate_runtime "$@"
            return $?
        fi
        if current_runtime_supports_lifecycle; then
            exec_current_runtime "$@"
        fi
        run_owned_command "$YYLO_NODE_EXECUTABLE" "$CLI_ENTRYPOINT" "$@"
        return $?
    fi

    require_compatible_node || {
        local status=$?
        finish_wrapper_invocation "$status"
        return "$status"
    }

    # Check if we're in an initialized yylo project
    if [ -d ".juno_task" ] && [ -f "$BOOTSTRAP_SCRIPT" ]; then
        # Project is initialized - use bootstrap.sh to setup environment and run CLI
        # Bootstrap.sh will:
        # 1. Check if we're in a venv
        # 2. Check if .venv_juno exists, create if needed
        # 3. Activate venv if needed
        # 4. Execute the command we pass to it

        if current_runtime_supports_lifecycle; then
            # Source bootstrap under an EXIT finalizer. A bootstrap refusal is
            # terminalized here; bootstrap's final exec preserves the invocation
            # PID and hands lifecycle ownership to the current CLI.
            trap finalize_bootstrap_failure EXIT
            # shellcheck source=/dev/null
            source "$BOOTSTRAP_SCRIPT" bash -c 'exec -a "$1" "$2" "${@:3}"' _ \
                "$YYLO_LAUNCH_SURFACE_VALUE" "$YYLO_NODE_EXECUTABLE" "$CLI_ENTRYPOINT" "$@"
        fi
        run_owned_command bash "$BOOTSTRAP_SCRIPT" bash -c 'exec -a "$1" "$2" "${@:3}"' _ \
            "$YYLO_LAUNCH_SURFACE_VALUE" "$YYLO_NODE_EXECUTABLE" "$CLI_ENTRYPOINT" "$@"
    else
        # Not initialized or bootstrap missing - run CLI directly. Current
        # runtimes preserve stdin/TTY and the historical producer PID contract.
        if current_runtime_supports_lifecycle; then
            exec_current_runtime "$@"
        fi
        run_owned_command "$YYLO_NODE_EXECUTABLE" "$CLI_ENTRYPOINT" "$@"
    fi
}

# Run main with all arguments
main "$@"
