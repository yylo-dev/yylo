#!/usr/bin/env bash

# slack_respond.sh
#
# Purpose: Send kanban agent responses back to Slack
#
# This script activates the Python virtual environment and runs the
# slack_respond.py script to send completed task responses back to
# Slack as threaded replies.
#
# Usage:
#   ./.juno_task/scripts/slack_respond.sh
#   ./.juno_task/scripts/slack_respond.sh --tag slack-input
#   ./.juno_task/scripts/slack_respond.sh --dry-run --verbose
#   ./.juno_task/scripts/slack_respond.sh --reset-tracker
#
# Environment Variables:
#   SLACK_BOT_TOKEN         Slack bot token (required, starts with xoxb-)
#   JUNO_DEBUG=true         Show debug messages
#   JUNO_VERBOSE=true       Show informational messages
#
# Created by: yylo init command
# Date: Auto-generated during project initialization

set -euo pipefail

# Debug output
if [ "${JUNO_DEBUG:-false}" = "true" ]; then
    echo "[DEBUG] slack_respond.sh is being executed from: $(pwd)" >&2
fi

# Color output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Configuration
VENV_DIR=".venv_juno"
SCRIPTS_DIR=".juno_task/scripts"
INSTALL_SCRIPT="${SCRIPTS_DIR}/install_requirements.sh"
SLACK_RESPOND_SCRIPT="${SCRIPTS_DIR}/slack_respond.py"

# Logging functions
log_info() {
    if [ "${JUNO_VERBOSE:-false}" = "true" ]; then
        echo -e "${BLUE}[SLACK_RESPOND]${NC} $1"
    fi
}

log_success() {
    if [ "${JUNO_VERBOSE:-false}" = "true" ]; then
        echo -e "${GREEN}[SLACK_RESPOND]${NC} $1"
    fi
}

log_warning() {
    if [ "${JUNO_VERBOSE:-false}" = "true" ]; then
        echo -e "${YELLOW}[SLACK_RESPOND]${NC} $1"
    fi
}

log_error() {
    echo -e "${RED}[SLACK_RESPOND]${NC} $1" >&2
}

# Check if we're in .venv_juno
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

# Activate virtual environment
activate_venv() {
    local venv_path="$1"

    if [ ! -d "$venv_path" ]; then
        log_error "Virtual environment not found: $venv_path"
        return 1
    fi

    if [ -f "$venv_path/bin/activate" ]; then
        # shellcheck disable=SC1091
        source "$venv_path/bin/activate"
        log_success "Activated virtual environment: $venv_path"
        return 0
    else
        log_error "Activation script not found: $venv_path/bin/activate"
        return 1
    fi
}

# Ensure Python environment is ready
ensure_python_environment() {
    log_info "Checking Python environment..."

    if is_in_venv_juno; then
        log_success "Already inside .venv_juno virtual environment"
        return 0
    fi

    if [ -d "$VENV_DIR" ]; then
        log_info "Found existing virtual environment: $VENV_DIR"
        if activate_venv "$VENV_DIR"; then
            return 0
        else
            log_error "Failed to activate virtual environment"
            return 1
        fi
    fi

    log_warning "Virtual environment not found: $VENV_DIR"
    log_info "Running install_requirements.sh to create virtual environment..."

    if [ ! -f "$INSTALL_SCRIPT" ]; then
        log_error "Install script not found: $INSTALL_SCRIPT"
        log_error "Please run 'yylo init' to initialize the project"
        return 1
    fi

    chmod +x "$INSTALL_SCRIPT"

    if bash "$INSTALL_SCRIPT"; then
        log_success "Python environment setup completed successfully"
        if [ -d "$VENV_DIR" ]; then
            activate_venv "$VENV_DIR"
        fi
        return 0
    else
        log_error "Failed to run install_requirements.sh"
        return 1
    fi
}

# Check for required Slack dependencies
check_slack_deps() {
    log_info "Checking Slack SDK dependencies..."

    if python3 -c "import slack_sdk; import dotenv" 2>/dev/null; then
        log_success "Slack SDK dependencies available"
        return 0
    fi

    log_warning "Slack SDK not installed. Installing..."
    pip install slack_sdk python-dotenv >/dev/null 2>&1 || {
        log_error "Failed to install Slack SDK dependencies"
        log_error "Please run: pip install slack_sdk python-dotenv"
        return 1
    }

    log_success "Slack SDK installed successfully"
    return 0
}

# Get script directory and project root
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$( cd "$SCRIPT_DIR/../.." && pwd )"

cd "$PROJECT_ROOT"

# Show help for Slack environment setup
show_env_help() {
    echo ""
    echo "========================================================================"
    echo "Slack Integration - Environment Setup"
    echo "========================================================================"
    echo ""
    echo "Required Environment Variables:"
    echo "  SLACK_BOT_TOKEN       Your Slack bot token (starts with xoxb-)"
    echo ""
    echo "Optional Environment Variables:"
    echo "  LOG_LEVEL             DEBUG, INFO, WARNING, ERROR (default: INFO)"
    echo ""
    echo "Configuration Methods:"
    echo "  1. Environment variables:"
    echo "     export SLACK_BOT_TOKEN=xoxb-your-token-here"
    echo ""
    echo "  2. .env file (in project root):"
    echo "     SLACK_BOT_TOKEN=xoxb-your-token-here"
    echo ""
    echo "  3. .juno_task/.env file (project-specific):"
    echo "     SLACK_BOT_TOKEN=xoxb-your-token-here"
    echo ""
    echo "To generate a Slack bot token:"
    echo "  1. Go to https://api.slack.com/apps and create a new app"
    echo "  2. Under 'OAuth & Permissions', add these scopes:"
    echo "     - channels:history, channels:read (public channels)"
    echo "     - groups:history, groups:read (private channels)"
    echo "     - users:read (user info)"
    echo "     - chat:write (send messages)"
    echo "  3. Install the app to your workspace"
    echo "  4. Copy the 'Bot User OAuth Token' (starts with xoxb-)"
    echo ""
    echo "  Full tutorial: https://api.slack.com/tutorials/tracks/getting-a-token"
    echo ""
    echo "========================================================================"
    echo ""
}

# Main function
main() {
    log_info "=== Slack Respond Wrapper ==="

    # Ensure Python environment
    if ! ensure_python_environment; then
        log_error "Failed to setup Python environment"
        exit 1
    fi

    # Check Slack dependencies
    if ! check_slack_deps; then
        exit 1
    fi

    # Load .env file if it exists
    if [ -f ".env" ]; then
        # shellcheck disable=SC1091
        set -a
        source .env
        set +a
        log_success "Loaded environment from .env"
    fi

    # Also check .juno_task/.env
    if [ -f ".juno_task/.env" ]; then
        # shellcheck disable=SC1091
        set -a
        source .juno_task/.env
        set +a
        log_success "Loaded environment from .juno_task/.env"
    fi

    # Check for SLACK_BOT_TOKEN
    if [ -z "${SLACK_BOT_TOKEN:-}" ]; then
        log_error "SLACK_BOT_TOKEN not set!"
        show_env_help
        exit 1
    fi

    # Validate token format
    if [[ ! "${SLACK_BOT_TOKEN:-}" =~ ^xoxb- ]]; then
        log_warning "SLACK_BOT_TOKEN does not start with 'xoxb-' - this may be an invalid bot token"
        log_info "Bot tokens from Slack should start with 'xoxb-'"
        log_info "To generate a valid token, visit: https://api.slack.com/tutorials/tracks/getting-a-token"
    fi

    log_success "Python environment ready!"
    log_success "Slack token configured"

    # Execute slack_respond.py
    log_info "Executing slack_respond.py: $*"
    exec python3 "$SLACK_RESPOND_SCRIPT" "$@"
}

main "$@"
