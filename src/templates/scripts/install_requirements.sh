#!/usr/bin/env bash

# install_requirements.sh
#
# Purpose: Install Python dependencies required for yylo
#
# This script:
# 1. Checks if 'pipx' (recommended for app installations) is installed
# 2. Falls back to 'uv' (ultrafast Python package manager) if 'pipx' not available
# 3. Falls back to 'pip' if neither 'pipx' nor 'uv' is available
# 4. Detects externally managed Python (PEP 668) on Ubuntu/Debian systems
# 5. Handles installation based on environment:
#    - If inside venv: installs into venv
#    - If externally managed Python detected: uses pipx or creates temporary venv
#    - If outside venv (non-managed): uses --system flag for system-wide installation
# 6. Installs the YYLO CLI and script runtime dependencies, including PyYAML
# 7. Reports if requirements are already satisfied
#
# Usage: ./install_requirements.sh
#
# Created by: yylo init command
# Date: Auto-generated during project initialization

set -euo pipefail  # Exit on error, undefined variable, or pipe failure

# Handle --help early (before any debug output)
for arg in "$@"; do
    if [[ "$arg" == "-h" ]] || [[ "$arg" == "--help" ]]; then
        echo "Usage: install_requirements.sh [OPTIONS]"
        echo ""
        echo "Options:"
        echo "  --check-updates   Only check for updates without installing"
        echo "  --force-update    Force update check and upgrade packages"
        echo "  -h, --help        Show this help message"
        echo ""
        echo "Environment Variables:"
        echo "  VERSION_CHECK_INTERVAL_HOURS   Hours between automatic update checks (default: 24)"
        exit 0
    fi
done

# DEBUG OUTPUT: Show that install_requirements.sh is being executed
# User feedback: "Add a one line printing from .sh file as well so we could debug it"
echo "[DEBUG] install_requirements.sh is being executed from: ${PWD}" >&2
echo "[DEBUG] .venv_juno will be created in: ${PWD}/.venv_juno" >&2

# Color output for better readability
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Required packages
# Note: requests and python-dotenv are required by github.py
# slack_sdk is required by Slack integration scripts (slack_fetch.py, slack_respond.py)
# PyYAML is required for advanced workflow_runner contracts.
REQUIRED_PACKAGES=("juno-kanban" "requests" "python-dotenv" "slack_sdk" "PyYAML")
JUNO_KANBAN_MIN_VERSION="2.0.5"
JUNO_KANBAN_COMPAT_RANGE=">=${JUNO_KANBAN_MIN_VERSION},<3.0.0"
JUNO_KANBAN_REQUIREMENT="juno-kanban${JUNO_KANBAN_COMPAT_RANGE}"
VERSION_CHECK_CACHE_FORMAT="2"

# pipx is suitable only for app-style packages that expose console entry points.
# Installing pure libraries (e.g. requests/python-dotenv/slack_sdk/PyYAML) via pipx fails
# with "No apps associated" and breaks requirements bootstrapping.
PIPX_COMPATIBLE_PACKAGES=("juno-kanban")

# An isolated Juno 2 alias exports the one selected source checkout. Keep the
# package-name list above as the metadata/cache SOT while installing that reviewed
# source into the initialized project's own runtime.
package_install_target() {
    local package=$1
    if [ "$package" = "juno-kanban" ]; then
        if [ -n "${JUNO_002_KANBAN_SOURCE:-}" ]; then
            if [ ! -f "$JUNO_002_KANBAN_SOURCE/setup.py" ]; then
                log_error "Selected juno-kanban source is invalid: $JUNO_002_KANBAN_SOURCE"
                return 1
            fi
            printf '%s\n' "$JUNO_002_KANBAN_SOURCE"
        else
            printf '%s\n' "$JUNO_KANBAN_REQUIREMENT"
        fi
        return 0
    fi
    printf '%s\n' "$package"
}

is_source_managed_package() {
    [ "$1" = "juno-kanban" ] && [ -n "${JUNO_002_KANBAN_SOURCE:-}" ]
}

juno_kanban_runtime_is_compatible() {
    local executable output
    executable=$(command -v juno-kanban 2>/dev/null || true)
    [ -n "$executable" ] || return 1
    output=$("$executable" --version </dev/null 2>/dev/null) || return 1
    python3 - "$output" "$JUNO_KANBAN_MIN_VERSION" <<'PY'
import re
import sys

output, minimum = sys.argv[1:]
matches = re.findall(r"(?<![0-9A-Za-z])([0-9]+)\.([0-9]+)\.([0-9]+)(?![0-9A-Za-z])", output)
if len(matches) != 1:
    raise SystemExit(1)
version = tuple(int(part) for part in matches[0])
minimum_version = tuple(int(part) for part in minimum.split("."))
raise SystemExit(0 if minimum_version <= version < (3, 0, 0) else 1)
PY
}

ensure_selected_juno_kanban_runtime() {
    [ -n "${JUNO_002_KANBAN_SOURCE:-}" ] || return 0
    [ -f "$JUNO_002_KANBAN_SOURCE/setup.py" ] || {
        log_error "Selected juno-kanban source is invalid: $JUNO_002_KANBAN_SOURCE"
        return 1
    }
    juno_kanban_runtime_is_compatible && return 0

    log_warning "Repairing incompatible juno-kanban runtime from selected source: $JUNO_002_KANBAN_SOURCE"
    if command -v uv &>/dev/null; then
        uv pip install --reinstall "$JUNO_002_KANBAN_SOURCE" --quiet || return 1
    else
        python3 -m pip install --force-reinstall "$JUNO_002_KANBAN_SOURCE" --quiet || return 1
    fi
    if ! juno_kanban_runtime_is_compatible; then
        log_error "Selected juno-kanban source did not produce a compatible runtime ($JUNO_KANBAN_COMPAT_RANGE)"
        return 1
    fi
}

# Version check cache configuration
# This ensures we don't check PyPI on every run (performance optimization per Task RTafs5).
# Keep transient state repository-scoped but outside the tracked worktree. Linked worktrees
# share one Git-common-dir cache; non-Git initialization falls back to an XDG cache key.
default_version_check_cache_dir() {
    local common_dir cache_root cache_key role resolver controller
    common_dir=$(git -C "$PWD" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)
    role=$(git -C "$PWD" config --worktree --get juno.workspace.role 2>/dev/null || true)
    if [ "$role" = task ]; then
        resolver="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/controller_resolver.py"
        if [ -z "$common_dir" ] || [ ! -f "$resolver" ]; then
            echo "[ERROR] Registered task version state requires the controller resolver" >&2
            return 2
        fi
        if ! controller=$(env -u JUNO_WORKSPACE_ROLE -u JUNO_PROJECT_PATH \
            python3 "$resolver" --cwd "$PWD" --operation diagnostic --format root 2>/dev/null); then
            echo "[ERROR] Registered task version state could not resolve its controller" >&2
            return 2
        fi
        if [ ! -d "$controller/.juno_task" ]; then
            echo "[ERROR] Registered task version state resolved an unavailable controller" >&2
            return 2
        fi
        cache_key=$(printf '%s' "$common_dir" | cksum | awk '{print $1}')
        printf '%s\n' "$controller/.juno_task/runtime/managed-controller/version-checks/$cache_key"
        return
    fi
    if [ -n "${VERSION_CHECK_CACHE_DIR:-}" ]; then
        printf '%s\n' "$VERSION_CHECK_CACHE_DIR"
        return
    fi
    if [ -n "$common_dir" ]; then
        printf '%s\n' "$common_dir/juno/version-checks"
        return
    fi
    cache_root="${XDG_CACHE_HOME:-${HOME}/.cache}/yylo/version-checks"
    cache_key=$(printf '%s' "$PWD" | cksum | awk '{print $1}')
    printf '%s\n' "$cache_root/$cache_key"
}
# A registered task's persisted identity is authoritative: inherited overrides
# must never route controller diagnostics back into the product worktree.
VERSION_CHECK_CACHE_DIR="$(default_version_check_cache_dir)"
VERSION_CHECK_CACHE_FILE="${VERSION_CHECK_CACHE_DIR}/.version_check_cache"
VERSION_CHECK_FAILURE_FILE="${VERSION_CHECK_CACHE_DIR}/.version_check_failure"
VERSION_CHECK_LOCK_DIR="${VERSION_CHECK_CACHE_DIR}/.version_check_lock"
VERSION_CHECK_INTERVAL_HOURS="${VERSION_CHECK_INTERVAL_HOURS:-24}"  # Check for updates once per day (override via env var)
VERSION_CHECK_FAILURE_COOLDOWN_SECONDS=3600
INSTALLED_PACKAGE_METADATA=""
INSTALLED_PACKAGE_NAMES=()
INSTALLED_PACKAGE_VERSIONS=()
CACHED_EXPECTED_VERSIONS=()
INSTALLED_VERSION_RESULT=""
CACHED_EXPECTED_VERSION_RESULT=""
VERSION_CHECK_IS_STALE=true

# Logging functions
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Load every required installed version with one Python interpreter startup.
# REQUIRED_PACKAGES remains the only package-list source of truth: it is passed
# directly to importlib.metadata rather than repeated in Python code.
load_installed_package_metadata() {
    local python_cmd="python3"
    if ! command -v "$python_cmd" &>/dev/null; then
        python_cmd="python"
    fi

    if ! INSTALLED_PACKAGE_METADATA=$(
        "$python_cmd" - "$VERSION_CHECK_CACHE_FILE" "$VERSION_CHECK_INTERVAL_HOURS" "${REQUIRED_PACKAGES[@]}" <<'PY'
import importlib.metadata
from pathlib import Path
import sys
import time

cache_path = Path(sys.argv[1])
interval_seconds = int(sys.argv[2]) * 3600
packages = sys.argv[3:]
cache = {}
try:
    for line in cache_path.read_text(encoding="utf-8").splitlines():
        key, value = line.split("=", 1)
        cache[key] = value
    checked_at = int(cache.get("checked_at", ""))
except (OSError, ValueError):
    checked_at = 0
complete = (
    cache.get("format") == "2"
    and cache.get("policy.juno-kanban") == ">=2.0.5,<3.0.0"
    and cache.get("packages") == ",".join(packages)
    and all(cache.get(f"package.{package}") for package in packages)
)
fresh = complete and time.time() - checked_at < interval_seconds
print(f"__cache__|{'fresh' if fresh else 'stale'}|")
for package in packages:
    try:
        version = importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        version = ""
    expected = cache.get(f"package.{package}", "") if fresh else ""
    print(f"{package}|{version}|{expected}")
PY
    ); then
        INSTALLED_PACKAGE_METADATA=""
        return 1
    fi

    INSTALLED_PACKAGE_NAMES=()
    INSTALLED_PACKAGE_VERSIONS=()
    CACHED_EXPECTED_VERSIONS=()
    local package version expected_version
    while IFS='|' read -r package version expected_version; do
        if [ "$package" = "__cache__" ]; then
            [ "$version" = "fresh" ] && VERSION_CHECK_IS_STALE=false || VERSION_CHECK_IS_STALE=true
        else
            INSTALLED_PACKAGE_NAMES+=("$package")
            INSTALLED_PACKAGE_VERSIONS+=("${version:-}")
            CACHED_EXPECTED_VERSIONS+=("${expected_version:-}")
        fi
    done <<< "$INSTALLED_PACKAGE_METADATA"
}

get_installed_version() {
    local package_name="$1"
    local index
    INSTALLED_VERSION_RESULT=""
    for (( index=0; index<${#INSTALLED_PACKAGE_NAMES[@]}; index++ )); do
        if [ "${INSTALLED_PACKAGE_NAMES[$index]}" = "$package_name" ]; then
            INSTALLED_VERSION_RESULT="${INSTALLED_PACKAGE_VERSIONS[$index]:-}"
            return 0
        fi
    done
}

get_cached_expected_version() {
    local package_name="$1"
    local index
    CACHED_EXPECTED_VERSION_RESULT=""
    for (( index=0; index<${#INSTALLED_PACKAGE_NAMES[@]}; index++ )); do
        if [ "${INSTALLED_PACKAGE_NAMES[$index]}" = "$package_name" ]; then
            CACHED_EXPECTED_VERSION_RESULT="${CACHED_EXPECTED_VERSIONS[$index]:-}"
            return 0
        fi
    done
}

# Function to get latest version from PyPI
get_pypi_latest_version() {
    local package_name="$1"
    local version=""

    # Use curl to fetch from PyPI JSON API (lightweight and fast)
    if command -v curl &>/dev/null; then
        version=$(curl -s --max-time 5 "https://pypi.org/pypi/${package_name}/json" 2>/dev/null | grep -o '"version":"[^"]*"' | head -1 | cut -d'"' -f4)
    fi

    echo "$version"
}

is_version_check_stale() {
    local cache_format="" checked_at="" packages="" now max_age field value expected
    local package_line_count=0
    [ -f "$VERSION_CHECK_CACHE_FILE" ] || return 0
    while IFS='=' read -r field value; do
        case "$field" in
            format) cache_format="$value" ;;
            checked_at) checked_at="$value" ;;
            packages) packages="$value" ;;
            package.*) [ -n "$value" ] && package_line_count=$(( package_line_count + 1 )) ;;
        esac
    done < "$VERSION_CHECK_CACHE_FILE"

    local IFS=,
    expected="${REQUIRED_PACKAGES[*]}"
    if [ "$cache_format" != "$VERSION_CHECK_CACHE_FORMAT" ] || [[ ! "$checked_at" =~ ^[0-9]+$ ]] || [ "$packages" != "$expected" ] || [ "$package_line_count" -ne "${#REQUIRED_PACKAGES[@]}" ] || ! grep -Fxq "policy.juno-kanban=$JUNO_KANBAN_COMPAT_RANGE" "$VERSION_CHECK_CACHE_FILE"; then
        return 0
    fi
    now=$(date +%s)
    max_age=$(( VERSION_CHECK_INTERVAL_HOURS * 3600 ))
    if [ $(( now - checked_at )) -ge "$max_age" ]; then
        return 0
    fi
    return 1
}

is_failure_cooldown_active() {
    local failed_at="" now field value
    [ -f "$VERSION_CHECK_FAILURE_FILE" ] || return 1
    while IFS='=' read -r field value; do
        [ "$field" = "failed_at" ] && failed_at="$value"
    done < "$VERSION_CHECK_FAILURE_FILE"
    now=$(date +%s)
    [[ "$failed_at" =~ ^[0-9]+$ ]] || return 1
    [ $(( now - failed_at )) -lt "$VERSION_CHECK_FAILURE_COOLDOWN_SECONDS" ]
}

atomic_write_failure_state() {
    mkdir -p "$VERSION_CHECK_CACHE_DIR"
    local temp_file
    temp_file=$(mktemp "${VERSION_CHECK_FAILURE_FILE}.tmp.XXXXXX")
    printf 'failed_at=%s\n' "$(date +%s)" > "$temp_file"
    mv "$temp_file" "$VERSION_CHECK_FAILURE_FILE"
}

atomic_publish_success_state() {
    local latest_metadata="$1"
    mkdir -p "$VERSION_CHECK_CACHE_DIR"
    local temp_file
    temp_file=$(mktemp "${VERSION_CHECK_CACHE_FILE}.tmp.XXXXXX")
    {
        echo "format=$VERSION_CHECK_CACHE_FORMAT"
        echo "checked_at=$(date +%s)"
        echo "policy.juno-kanban=$JUNO_KANBAN_COMPAT_RANGE"
        local IFS=,
        echo "packages=${REQUIRED_PACKAGES[*]}"
        printf '%s\n' "$latest_metadata"
    } > "$temp_file"
    mv "$temp_file" "$VERSION_CHECK_CACHE_FILE"
    rm -f "$VERSION_CHECK_FAILURE_FILE"
}

acquire_update_lock() {
    mkdir -p "$VERSION_CHECK_CACHE_DIR"
    local attempts=0 lock_pid=""
    while ! mkdir "$VERSION_CHECK_LOCK_DIR" 2>/dev/null; do
        if [ -f "$VERSION_CHECK_LOCK_DIR/pid" ]; then
            lock_pid=$(cat "$VERSION_CHECK_LOCK_DIR/pid" 2>/dev/null || true)
            if [[ "$lock_pid" =~ ^[0-9]+$ ]] && ! kill -0 "$lock_pid" 2>/dev/null; then
                rm -rf "$VERSION_CHECK_LOCK_DIR"
                continue
            fi
        fi
        attempts=$(( attempts + 1 ))
        if [ "$attempts" -ge 300 ]; then
            log_warning "Timed out waiting for dependency update lock; continuing without a network check"
            return 1
        fi
        sleep 0.1
    done
    echo "$$" > "$VERSION_CHECK_LOCK_DIR/pid"
}

release_update_lock() {
    rm -rf "$VERSION_CHECK_LOCK_DIR"
}

upgrade_packages() {
    local packages_to_upgrade=("$@")
    [ ${#packages_to_upgrade[@]} -gt 0 ] || return 0
    log_info "Upgrading packages: ${packages_to_upgrade[*]}"

    if command -v uv &>/dev/null && uv pip install --upgrade "${packages_to_upgrade[@]}" --quiet 2>/dev/null; then
        return 0
    fi
    log_warning "uv upgrade was unavailable or failed; trying pip"
    python3 -m pip install --upgrade "${packages_to_upgrade[@]}" --quiet 2>/dev/null
}

reconcile_fresh_cached_versions() {
    [ "$VERSION_CHECK_IS_STALE" = false ] || return 0

    local package installed_version expected_version
    local cached_requirements=()
    for package in "${REQUIRED_PACKAGES[@]}"; do
        is_source_managed_package "$package" && continue
        get_installed_version "$package"
        installed_version="$INSTALLED_VERSION_RESULT"
        get_cached_expected_version "$package"
        expected_version="$CACHED_EXPECTED_VERSION_RESULT"
        if [ -z "$expected_version" ]; then
            log_warning "Fresh dependency cache is incomplete; refusing cached repair"
            return 1
        fi
        if [ "$installed_version" != "$expected_version" ]; then
            cached_requirements+=("${package}==${expected_version}")
        fi
    done
    [ ${#cached_requirements[@]} -gt 0 ] || return 0

    if is_failure_cooldown_active; then
        log_warning "Previous cached dependency repair failed; retry suppressed for one hour"
        return 1
    fi
    if ! acquire_update_lock; then
        return 1
    fi
    trap release_update_lock EXIT

    # Another invocation may have repaired the environment while this process waited.
    if ! load_installed_package_metadata || [ "$VERSION_CHECK_IS_STALE" = true ]; then
        atomic_write_failure_state
        log_warning "Could not reload complete fresh dependency cache for repair"
        release_update_lock
        trap - EXIT
        return 1
    fi
    cached_requirements=()
    for package in "${REQUIRED_PACKAGES[@]}"; do
        is_source_managed_package "$package" && continue
        get_installed_version "$package"
        installed_version="$INSTALLED_VERSION_RESULT"
        get_cached_expected_version "$package"
        expected_version="$CACHED_EXPECTED_VERSION_RESULT"
        if [ -z "$expected_version" ]; then
            atomic_write_failure_state
            log_warning "Fresh dependency cache became incomplete during repair"
            release_update_lock
            trap - EXIT
            return 1
        fi
        if [ "$installed_version" != "$expected_version" ]; then
            cached_requirements+=("${package}==${expected_version}")
        fi
    done

    if [ ${#cached_requirements[@]} -gt 0 ]; then
        log_info "Repairing dependencies from fresh cached versions: ${cached_requirements[*]}"
        if ! upgrade_packages "${cached_requirements[@]}" || ! load_installed_package_metadata; then
            atomic_write_failure_state
            log_warning "Cached dependency repair failed; retrying after one hour"
            release_update_lock
            trap - EXIT
            return 1
        fi
    fi

    for package in "${REQUIRED_PACKAGES[@]}"; do
        is_source_managed_package "$package" && continue
        get_installed_version "$package"
        installed_version="$INSTALLED_VERSION_RESULT"
        get_cached_expected_version "$package"
        expected_version="$CACHED_EXPECTED_VERSION_RESULT"
        if [ -z "$expected_version" ] || [ "$installed_version" != "$expected_version" ]; then
            atomic_write_failure_state
            log_warning "Cached dependency repair verification failed; retrying after one hour"
            release_update_lock
            trap - EXIT
            return 1
        fi
    done

    rm -f "$VERSION_CHECK_FAILURE_FILE"
    log_success "All packages match fresh cached versions"
    release_update_lock
    trap - EXIT
    return 0
}

check_all_for_updates() {
    local force_check="${1:-false}"
    local package installed_version latest_version latest_metadata=""
    local packages_needing_upgrade=()

    if [ "$force_check" != "true" ] && [ "$VERSION_CHECK_IS_STALE" = false ]; then
        return 0
    fi
    if [ "$force_check" != "true" ] && is_failure_cooldown_active; then
        log_warning "Previous PyPI check failed; retry suppressed for one hour"
        return 0
    fi
    if ! acquire_update_lock; then
        return 0
    fi
    trap release_update_lock EXIT

    # Another invocation may have completed while this process waited.
    if [ "$force_check" != "true" ] && ! is_version_check_stale; then
        release_update_lock
        trap - EXIT
        return 0
    fi
    if [ "$force_check" != "true" ] && is_failure_cooldown_active; then
        release_update_lock
        trap - EXIT
        log_warning "Previous PyPI check failed; retry suppressed for one hour"
        return 0
    fi

    log_info "Performing periodic version check..."
    for package in "${REQUIRED_PACKAGES[@]}"; do
        get_installed_version "$package"
        installed_version="$INSTALLED_VERSION_RESULT"
        if is_source_managed_package "$package"; then
            latest_version="$installed_version"
        else
            latest_version=$(get_pypi_latest_version "$package")
        fi
        if [ -z "$latest_version" ]; then
            atomic_write_failure_state
            log_warning "Could not complete PyPI check; continuing and retrying after one hour"
            release_update_lock
            trap - EXIT
            return 0
        fi
        latest_metadata="${latest_metadata}package.${package}=${latest_version}"$'\n'
        if [ "$installed_version" != "$latest_version" ]; then
            packages_needing_upgrade+=("$package")
        fi
    done

    if [ ${#packages_needing_upgrade[@]} -gt 0 ]; then
        if ! upgrade_packages "${packages_needing_upgrade[@]}"; then
            atomic_write_failure_state
            log_warning "Dependency upgrade failed; continuing and retrying after one hour"
            release_update_lock
            trap - EXIT
            return 0
        fi
        if ! load_installed_package_metadata; then
            atomic_write_failure_state
            log_warning "Could not verify upgraded dependency metadata"
            release_update_lock
            trap - EXIT
            return 0
        fi
    fi

    for package in "${REQUIRED_PACKAGES[@]}"; do
        get_installed_version "$package"
        installed_version="$INSTALLED_VERSION_RESULT"
        latest_version=$(printf '%s' "$latest_metadata" | while IFS='=' read -r key value; do
            [ "$key" = "package.${package}" ] && { echo "$value"; break; }
        done)
        if [ -z "$installed_version" ] || [ "$installed_version" != "$latest_version" ]; then
            atomic_write_failure_state
            log_warning "Dependency verification did not match complete PyPI metadata; success cache not published"
            release_update_lock
            trap - EXIT
            return 0
        fi
    done

    atomic_publish_success_state "${latest_metadata%$'\n'}"
    log_success "All packages are verified at current PyPI versions"
    release_update_lock
    trap - EXIT
}

check_all_requirements_satisfied() {
    local package version
    local venv_path=".venv_juno"

    if ! is_in_venv_juno && [ ! -d "$venv_path" ]; then
        log_warning "Project virtual environment missing: $venv_path"
        log_info "Will create $venv_path and install requirements there"
        return 1
    fi

    load_installed_package_metadata || return 1
    if [ "$VERSION_CHECK_IS_STALE" = false ]; then
        # A complete fresh cache is authoritative and can repair drift without PyPI.
        reconcile_fresh_cached_versions || return 2
        return 0
    fi
    for package in "${REQUIRED_PACKAGES[@]}"; do
        get_installed_version "$package"
        version="$INSTALLED_VERSION_RESULT"
        [ -n "$version" ] || return 1
    done
    return 0
}

# Function to check if we're inside a virtual environment
is_in_virtualenv() {
    # Check for VIRTUAL_ENV environment variable (most common indicator)
    if [ -n "${VIRTUAL_ENV:-}" ]; then
        return 0  # Inside venv
    fi

    # Check for CONDA_DEFAULT_ENV (conda environments)
    if [ -n "${CONDA_DEFAULT_ENV:-}" ]; then
        return 0  # Inside conda env
    fi

    # Check if sys.prefix != sys.base_prefix (Python way to detect venv)
    if command -v python3 &> /dev/null; then
        if python3 -c "import sys; exit(0 if sys.prefix != sys.base_prefix else 1)" 2>/dev/null; then
            return 0  # Inside venv
        fi
    fi

    return 1  # Not inside venv
}

# Function to check if we're specifically inside .venv_juno
is_in_venv_juno() {
    if [ -n "${VIRTUAL_ENV:-}" ]; then
        if [[ "${VIRTUAL_ENV:-}" == *"/.venv_juno" ]] || [[ "${VIRTUAL_ENV:-}" == *".venv_juno"* ]]; then
            return 0
        fi

        if [ "$(basename "${VIRTUAL_ENV:-}")" = ".venv_juno" ]; then
            return 0
        fi
    fi

    return 1
}

# Function to activate project-local .venv_juno when available.
# Why: install/check commands often run from non-activated shells, while packages
# are installed into .venv_juno. Activating it early keeps package detection and
# periodic update checks aligned with the real install target.
activate_project_venv_if_available() {
    if is_in_venv_juno; then
        return 0
    fi

    local venv_path=".venv_juno"
    if [ -f "$venv_path/bin/activate" ]; then
        log_info "Detected project virtual environment at $venv_path; activating for dependency checks"
        # shellcheck disable=SC1091
        source "$venv_path/bin/activate"
        log_success "Activated $venv_path"
    fi
}

# Function to find the best Python version (3.10-3.13, preferably 3.13)
find_best_python() {
    # Try to find Python in order of preference: 3.13, 3.12, 3.11, 3.10
    local python_versions=("python3.13" "python3.12" "python3.11" "python3.10")

    for py_cmd in "${python_versions[@]}"; do
        if command -v "$py_cmd" &> /dev/null; then
            # Verify it's actually the right version
            local version
            version=$($py_cmd --version 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
            local major
            major=$(echo "$version" | cut -d'.' -f1)
            local minor
            minor=$(echo "$version" | cut -d'.' -f2)

            # Check if version is 3.10 or higher
            if [ "$major" -eq 3 ] && [ "$minor" -ge 10 ]; then
                echo "$py_cmd"
                return 0
            fi
        fi
    done

    # Fall back to python3 if available and check its version
    if command -v python3 &> /dev/null; then
        local version
        version=$(python3 --version 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
        local major
        major=$(echo "$version" | cut -d'.' -f1)
        local minor
        minor=$(echo "$version" | cut -d'.' -f2)

        # Check if version is 3.10 or higher
        if [ "$major" -eq 3 ] && [ "$minor" -ge 10 ]; then
            echo "python3"
            return 0
        else
            # Python3 exists but is too old
            log_error "Found Python $version, but Python 3.10+ is required"
            return 1
        fi
    fi

    # No suitable Python found
    log_error "No Python 3.10+ found. Please install Python 3.10, 3.11, 3.12, or 3.13 (preferably 3.13)"
    return 1
}

# Function to check if Python is externally managed (PEP 668)
# This is common on Ubuntu 23.04+, Debian, and other modern Linux distros
is_externally_managed_python() {
    # Check for EXTERNALLY-MANAGED marker file
    local python_cmd="python3"
    if ! command -v python3 &> /dev/null; then
        if command -v python &> /dev/null; then
            python_cmd="python"
        else
            return 1  # Python not found, can't determine
        fi
    fi

    # Get the stdlib directory and check for EXTERNALLY-MANAGED file
    local stdlib_dir
    stdlib_dir=$($python_cmd -c "import sysconfig; print(sysconfig.get_path('stdlib'))" 2>/dev/null || echo "")

    if [ -n "$stdlib_dir" ] && [ -f "$stdlib_dir/EXTERNALLY-MANAGED" ]; then
        return 0  # Externally managed
    fi

    return 1  # Not externally managed
}

# Function to upgrade pip to latest version inside the active venv
# Why: venv ships with the pip version bundled in the Python distribution,
# which can be months/years behind. Old pip may fail to resolve modern
# dependency metadata or miss security fixes. Upgrading pip is fast (<2s)
# and prevents hard-to-debug install failures downstream.
upgrade_pip_in_venv() {
    if ! is_in_virtualenv; then
        return 0  # Only upgrade pip inside a venv
    fi

    log_info "Upgrading pip to latest version in venv..."

    # Prefer uv for speed, fall back to pip itself
    if command -v uv &>/dev/null; then
        if uv pip install --upgrade pip --quiet 2>/dev/null; then
            local pip_ver
            pip_ver=$(python3 -m pip --version 2>/dev/null | awk '{print $2}' || echo "unknown")
            log_success "pip upgraded to v$pip_ver (via uv)"
            return 0
        fi
    fi

    # Fall back to pip self-upgrade
    if python3 -m pip install --upgrade pip --quiet 2>/dev/null; then
        local pip_ver
        pip_ver=$(python3 -m pip --version 2>/dev/null | awk '{print $2}' || echo "unknown")
        log_success "pip upgraded to v$pip_ver"
    else
        log_warning "Could not upgrade pip (non-fatal, continuing with current version)"
    fi
}

all_required_packages_pipx_compatible() {
    local compatible
    for package in "${REQUIRED_PACKAGES[@]}"; do
        compatible=false
        for pipx_pkg in "${PIPX_COMPATIBLE_PACKAGES[@]}"; do
            if [ "$package" = "$pipx_pkg" ]; then
                compatible=true
                break
            fi
        done

        if [ "$compatible" = false ]; then
            return 1
        fi
    done

    return 0
}

# Function to install packages using pipx
install_with_pipx() {
    log_info "Installing packages using 'pipx' (recommended for Python applications)..."

    local failed_packages=()

    for package in "${REQUIRED_PACKAGES[@]}"; do
        local install_target
        install_target=$(package_install_target "$package") || return 1
        log_info "Installing: $package"
        if pipx install "$install_target" --force &>/dev/null || pipx install "$install_target" &>/dev/null; then
            log_success "Successfully installed: $package"
        else
            log_error "Failed to install: $package"
            failed_packages+=("$package")
        fi
    done

    if [ ${#failed_packages[@]} -gt 0 ]; then
        log_error "Failed to install ${#failed_packages[@]} package(s): ${failed_packages[*]}"
        return 1
    fi

    return 0
}

# Function to install packages using uv with externally managed Python handling
install_with_uv() {
    log_info "Installing packages using 'uv' (ultrafast Python package manager)..."

    local uv_flags="--quiet"

    # CRITICAL FIX: Properly detect if uv will work in the current environment
    # User feedback: "Maybe the way you are verifying being inside venv by uv is not correct !!!"
    # Previous approach failed because uv pip list doesn't reliably indicate venv compatibility
    # NEW APPROACH: Always create .venv_juno unless we're already inside it

    local venv_path=".venv_juno"
    local need_venv=true

    # Check if we're already inside .venv_juno
    if [ -n "${VIRTUAL_ENV:-}" ] && ( [[ "${VIRTUAL_ENV:-}" == *"/.venv_juno" ]] || [[ "${VIRTUAL_ENV:-}" == *".venv_juno"* ]] ); then
        log_info "Already inside .venv_juno virtual environment"
        need_venv=false
    # Check if we're in .venv_juno by checking the activate script path
    elif [ -n "${VIRTUAL_ENV:-}" ] && [ "$(basename "${VIRTUAL_ENV:-}")" = ".venv_juno" ]; then
        log_info "Already inside .venv_juno virtual environment"
        need_venv=false
    fi

    # If we need a venv, create and activate .venv_juno
    if [ "$need_venv" = true ]; then
        log_info "Creating/using .venv_juno virtual environment for reliable uv installation..."

        # Find best Python version (3.10-3.13, preferably 3.13)
        local python_cmd
        if ! python_cmd=$(find_best_python); then
            log_error "Cannot create venv: No suitable Python version found"
            log_info "Please install Python 3.10+ (preferably Python 3.13)"
            log_info "  Mac: brew install python@3.13"
            log_info "  Ubuntu/Debian: sudo apt install python3.13 python3.13-venv"
            return 1
        fi

        local version
        version=$($python_cmd --version 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+')
        log_info "Using Python $version for virtual environment"

        # Create a project-local venv if it doesn't exist
        if [ ! -d "$venv_path" ]; then
            log_info "Creating virtual environment with $python_cmd..."
            if ! $python_cmd -m venv "$venv_path" 2>/dev/null; then
                log_error "Failed to create virtual environment"
                log_info "Please ensure python venv module is installed:"
                log_info "  Mac: brew install python@3.13"
                log_info "  Ubuntu/Debian: sudo apt install python3.13-venv python3-full"
                return 1
            fi
            log_success "Created virtual environment at $venv_path with Python $version"
        else
            log_info "Using existing virtual environment at $venv_path"
        fi

        # Activate the venv for this script
        # shellcheck disable=SC1091
        if [ -f "$venv_path/bin/activate" ]; then
            source "$venv_path/bin/activate"
            log_success "Activated virtual environment - uv will now install into .venv_juno"
        else
            log_error "Virtual environment activation script not found"
            return 1
        fi

        # Upgrade pip to latest version in venv
        upgrade_pip_in_venv
    fi

    local failed_packages=()

    for package in "${REQUIRED_PACKAGES[@]}"; do
        local install_target
        install_target=$(package_install_target "$package") || return 1
        log_info "Installing: $package"
        if uv pip install "$install_target" $uv_flags; then
            log_success "Successfully installed: $package"
        else
            log_error "Failed to install: $package"
            failed_packages+=("$package")
        fi
    done

    if [ ${#failed_packages[@]} -gt 0 ]; then
        log_error "Failed to install ${#failed_packages[@]} package(s): ${failed_packages[*]}"
        return 1
    fi

    return 0
}

# Function to install packages using pip with externally managed Python handling
install_with_pip() {
    log_info "Installing packages using 'pip'..."

    # Detect python command (python3 or python)
    local python_cmd="python3"
    if ! command -v python3 &> /dev/null; then
        if command -v python &> /dev/null; then
            python_cmd="python"
        else
            log_error "Python not found. Please install Python 3."
            return 1
        fi
    fi

    # Always install into project-local .venv_juno unless we are already inside it.
    # This keeps bootstrap behavior deterministic across global/conda/system python setups.
    if ! is_in_venv_juno; then
        if is_externally_managed_python; then
            log_warning "Detected externally managed Python (PEP 668) - Ubuntu/Debian system"
        fi
        log_info "Creating/using project virtual environment for installation..."

        # Find best Python version (3.10-3.13, preferably 3.13)
        if ! python_cmd=$(find_best_python); then
            log_error "Cannot create venv: No suitable Python version found"
            log_info "Please install Python 3.10+ (preferably Python 3.13)"
            log_info "  Ubuntu/Debian: sudo apt install python3.13 python3.13-venv"
            return 1
        fi

        local version
        version=$($python_cmd --version 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+')
        log_info "Using Python $version for virtual environment"

        local venv_path=".venv_juno"
        if [ ! -d "$venv_path" ]; then
            if ! $python_cmd -m venv "$venv_path" 2>/dev/null; then
                log_error "Failed to create virtual environment"
                log_info "Please install python3-venv (Linux: sudo apt install python3.13-venv python3-full)"
                return 1
            fi
            log_success "Created virtual environment at $venv_path with Python $version"
        fi

        # Activate the venv for this script
        # shellcheck disable=SC1091
        source "$venv_path/bin/activate"
        log_success "Activated virtual environment"
        python_cmd="python"  # Use the venv's python

        # Upgrade pip to latest version in venv
        upgrade_pip_in_venv
    fi

    local failed_packages=()

    for package in "${REQUIRED_PACKAGES[@]}"; do
        local install_target
        install_target=$(package_install_target "$package") || return 1
        log_info "Installing: $package"
        if $python_cmd -m pip install "$install_target" --quiet; then
            log_success "Successfully installed: $package"
        else
            log_error "Failed to install: $package"
            failed_packages+=("$package")
        fi
    done

    if [ ${#failed_packages[@]} -gt 0 ]; then
        log_error "Failed to install ${#failed_packages[@]} package(s): ${failed_packages[*]}"
        return 1
    fi

    return 0
}

# Main installation logic
main() {
    local force_update=false
    local check_updates_only=false

    # Parse command line arguments (--help is handled early, before debug output)
    while [[ $# -gt 0 ]]; do
        case $1 in
            --force-update)
                force_update=true
                shift
                ;;
            --check-updates)
                check_updates_only=true
                shift
                ;;
            *)
                shift
                ;;
        esac
    done

    echo ""
    log_info "=== Python Requirements Installation ==="
    echo ""

    # Align all checks with project-local installation target when available.
    activate_project_venv_if_available
    if ! ensure_selected_juno_kanban_runtime; then
        log_error "Failed to restore the selected YYLO Ledger runtime"
        exit 1
    fi

    # Handle --check-updates: just check and report, don't install
    if [ "$check_updates_only" = true ]; then
        log_info "Checking for updates..."
        load_installed_package_metadata || true
        for package in "${REQUIRED_PACKAGES[@]}"; do
            local installed_ver
            local latest_ver
            get_installed_version "$package"
            installed_ver="$INSTALLED_VERSION_RESULT"
            if is_source_managed_package "$package"; then
                latest_ver="$installed_ver"
            else
                latest_ver=$(get_pypi_latest_version "$package")
            fi

            if [ -z "$installed_ver" ]; then
                log_warning "$package is not installed"
            elif [ -z "$latest_ver" ]; then
                log_info "$package: v$installed_ver (could not check PyPI)"
            elif [ "$installed_ver" = "$latest_ver" ]; then
                log_success "$package: v$installed_ver (up-to-date)"
            else
                log_warning "$package: v$installed_ver -> v$latest_ver (update available)"
            fi
        done
        exit 0
    fi

    # Step 1: Check if all requirements are already satisfied
    log_info "Checking if requirements are already satisfied..."

    local requirements_status=0
    check_all_requirements_satisfied || requirements_status=$?
    if [ "$requirements_status" -eq 0 ]; then
        log_success "All requirements already satisfied!"
        echo ""
        log_info "Installed packages:"
        for package in "${REQUIRED_PACKAGES[@]}"; do
            local ver
            get_installed_version "$package"
            ver="$INSTALLED_VERSION_RESULT"
            echo "  ✓ $package (v$ver)"
        done
        echo ""

        # Step 1b: Periodic update check (only when cache is stale, or forced)
        # This ensures dependencies stay up-to-date without degrading performance
        check_all_for_updates "$force_update"
        exit 0
    fi
    if [ "$requirements_status" -eq 2 ]; then
        log_error "Requirements do not match fresh cached versions and repair failed"
        exit 2
    fi

    log_info "Some packages need to be installed."
    echo ""

    # Step 2: Determine which package manager to use
    local installer=""

    # Check if Python is externally managed (Ubuntu/Debian PEP 668)
    local is_ext_managed=false
    if is_externally_managed_python && ! is_in_virtualenv; then
        is_ext_managed=true
        log_warning "Detected externally managed Python environment (Ubuntu/Debian PEP 668)"
    fi

    # Prefer uv/pip because requirements include both app + library packages.
    # pipx is only valid when *all* required packages are app-style CLI tools.
    if [ "$is_ext_managed" = true ] && command -v pipx &> /dev/null; then
        if all_required_packages_pipx_compatible; then
            log_success "'pipx' found - using pipx (all required packages are pipx-compatible)"
            installer="pipx"
        else
            log_warning "'pipx' detected but skipped: requirements include library packages not supported by pipx"
        fi
    fi

    if [ -z "$installer" ] && command -v uv &> /dev/null; then
        log_success "'uv' found - using ultrafast Python package manager"
        installer="uv"
    elif [ -z "$installer" ] && ( command -v pip3 &> /dev/null || command -v pip &> /dev/null ); then
        log_success "'pip' found - using standard Python package installer"
        installer="pip"
    else
        # No package manager found
        log_error "No suitable package manager found!"
        echo ""
        log_info "Please install one of the following:"
        echo ""
        if [ "$is_ext_managed" = true ]; then
            echo "  Option 1: Install 'pipx' (RECOMMENDED for Ubuntu/Debian)"
            echo "    sudo apt install pipx"
            echo "    pipx ensurepath"
            echo ""
        fi
        echo "  Option 2: Install 'uv' (ultrafast Python package manager)"
        echo "    curl -LsSf https://astral.sh/uv/install.sh | sh"
        echo "    OR"
        echo "    brew install uv  (macOS)"
        echo ""
        echo "  Option 3: Install 'pip' (standard Python package manager)"
        echo "    python3 -m ensurepip --upgrade"
        echo "    OR"
        echo "    curl https://bootstrap.pypa.io/get-pip.py -o get-pip.py && python3 get-pip.py"
        echo ""
        if [ "$is_ext_managed" = true ]; then
            log_info "Note: On Ubuntu/Debian with externally managed Python, 'pipx' is recommended"
            log_info "Alternatively, install python3-venv: sudo apt install python3-venv python3-full"
        fi
        echo ""
        exit 1
    fi

    # Step 3: Install packages
    echo ""
    log_info "Installing required packages: ${REQUIRED_PACKAGES[*]}"
    echo ""

    if [ "$installer" = "pipx" ]; then
        if install_with_pipx; then
            echo ""
            log_success "All packages installed successfully using 'pipx'!"
            log_info "Packages installed in isolated environments and added to PATH"
            echo ""
            exit 0
        else
            log_error "Some packages failed to install with 'pipx'"
            exit 1
        fi
    elif [ "$installer" = "uv" ]; then
        if install_with_uv; then
            echo ""
            log_success "All packages installed successfully using 'uv'!"
            if [ -d ".venv_juno" ]; then
                log_info "Packages installed in virtual environment: .venv_juno"
                log_info "To use them, activate the venv: source .venv_juno/bin/activate"
            fi
            echo ""
            exit 0
        else
            log_error "Some packages failed to install with 'uv'"
            exit 1
        fi
    elif [ "$installer" = "pip" ]; then
        if install_with_pip; then
            echo ""
            log_success "All packages installed successfully using 'pip'!"
            if [ -d ".venv_juno" ]; then
                log_info "Packages installed in virtual environment: .venv_juno"
                log_info "To use them, activate the venv: source .venv_juno/bin/activate"
            fi
            echo ""
            exit 0
        else
            log_error "Some packages failed to install with 'pip'"
            exit 1
        fi
    fi

    # Should not reach here
    log_error "Unexpected error during installation"
    exit 1
}

# Run main function
main "$@"
