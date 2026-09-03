# YYLO Service Scripts

This directory contains service scripts that extend yylo functionality. These scripts are Python-based utilities that can be customized by users.

## Installation

Service scripts are automatically installed to `~/.yylo/services/` when you run:

```bash
yylo init
```

Or you can manually install/manage them:

```bash
# Install services
yylo services install

# List installed services
yylo services list

# Check installation status
yylo services status

# Show services directory path
yylo services path

# Uninstall services
yylo services uninstall --yes
```

## Available Services

### codex.py

A wrapper for OpenAI Codex CLI with configurable options.

#### Features

- Automatic codex installation check
- Support for inline prompts or prompt files
- Configurable model selection
- Auto-instruction prepending
- Full argument passthrough and override support
- JSON output support
- Verbose mode for debugging

#### Usage

```bash
# Basic usage with inline prompt
~/.yylo/services/codex.py -p "Write a hello world function"

# Using a prompt file
~/.yylo/services/codex.py -pp /path/to/prompt.txt

# Specify project directory
~/.yylo/services/codex.py -p "Add tests" --cd /path/to/project

# Override default model
~/.yylo/services/codex.py -p "Refactor code" -m gpt-4-turbo

# Custom auto-instruction
~/.yylo/services/codex.py -p "Fix bugs" --auto-instruction "You are a debugging expert"

# Add custom codex config
~/.yylo/services/codex.py -p "Write code" -c custom_option=value

# Enable verbose output
~/.yylo/services/codex.py -p "Analyze code" --verbose

# JSON output
~/.yylo/services/codex.py -p "Generate function" --json
```

#### Arguments

- `-p, --prompt <text>`: Prompt text to send to codex (required, mutually exclusive with --prompt-file)
- `-pp, --prompt-file <path>`: Path to file containing the prompt (required, mutually exclusive with --prompt)
- `--cd <path>`: Project path (absolute path). Default: current directory
- `-m, --model <name>`: Model name. Default: gpt-4
- `--auto-instruction <text>`: Auto instruction to prepend to prompt
- `-c, --config <arg>`: Additional codex config arguments (can be used multiple times)
- `--json`: Output in JSON format
- `--verbose`: Enable verbose output

#### Default Configuration

The script comes with these default codex configurations:

- `include_apply_patch_tool=true`
- `use_experimental_streamable_shell_tool=true`
- `sandbox_mode=danger-full-access`

You can override these by providing the same config key with `-c`:

```bash
# Override sandbox mode
~/.yylo/services/codex.py -p "Safe operation" -c sandbox_mode=safe
```

### claude.py

A wrapper for Anthropic Claude CLI with configurable options.

#### Features

- Automatic claude installation check
- Support for inline prompts or prompt files
- Configurable model selection (sonnet, opus, or full model names)
- Auto-instruction prepending
- Tool access control
- Permission mode configuration
- JSON output support
- Verbose mode for debugging
- Conversation continuation support

#### Usage

```bash
# Basic usage with inline prompt
~/.yylo/services/claude.py -p "Write a hello world function"

# Using a prompt file
~/.yylo/services/claude.py -pp /path/to/prompt.txt

# Specify project directory
~/.yylo/services/claude.py -p "Add tests" --cd /path/to/project

# Override default model
~/.yylo/services/claude.py -p "Refactor code" -m claude-opus-4-20250514

# Use model aliases
~/.yylo/services/claude.py -p "Fix bugs" -m sonnet

# Custom auto-instruction
~/.yylo/services/claude.py -p "Fix bugs" --auto-instruction "You are a debugging expert"

# Specify allowed tools
~/.yylo/services/claude.py -p "Write code" --tool Bash --tool Edit --tool Write

# Change permission mode
~/.yylo/services/claude.py -p "Review code" --permission-mode plan

# Continue previous conversation
~/.yylo/services/claude.py -p "Continue working" --continue

# Enable verbose output
~/.yylo/services/claude.py -p "Analyze code" --verbose

# JSON output
~/.yylo/services/claude.py -p "Generate function" --json
```

#### Arguments

- `-p, --prompt <text>`: Prompt text to send to claude (required, mutually exclusive with --prompt-file)
- `-pp, --prompt-file <path>`: Path to file containing the prompt (required, mutually exclusive with --prompt)
- `--cd <path>`: Project path (absolute path). Default: current directory
- `-m, --model <name>`: Model name (e.g. 'sonnet', 'opus', or full name). Default: claude-sonnet-4-20250514
- `--auto-instruction <text>`: Auto instruction to prepend to prompt
- `--tool <name>`: Allowed tools (can be used multiple times, e.g. 'Bash' 'Edit')
- `--permission-mode <mode>`: Permission mode: acceptEdits, bypassPermissions, default, or plan. Default: bypassPermissions
- `--json`: Output in JSON format
- `--verbose`: Enable verbose output
- `-c, --continue`: Continue the most recent conversation
- `--additional-args <args>`: Additional claude arguments as a space-separated string

#### Default Configuration

The script comes with these default allowed tools:

- Read, Write, Edit, MultiEdit
- Bash, Glob, Grep
- WebFetch, WebSearch
- TodoWrite

You can override these by providing the `--tool` argument:

```bash
# Use only specific tools
~/.yylo/services/claude.py -p "Safe operation" --tool Read --tool Write
```

### gemini.py

Headless wrapper for Gemini CLI with shorthand model support and JSON/text output normalization.

#### Features

- Headless execution via `--prompt/-p` or `--prompt-file`
- Shorthand model aliases (`:pro`, `:flash`, `:pro-3`, `:flash-3`, `:pro-2.5`, `:flash-2.5`)
- Streaming JSON output normalization (default `--output-format stream-json`)
- Auto-approval for headless mode (defaults to `--yolo` when no approval mode is provided)
- Fails fast when `GEMINI_API_KEY` is missing to prevent confusing CLI errors
- Optional directory inclusion (`--include-directories`) and debug passthrough

#### Usage

```bash
# Basic headless run with shorthand model (stream-json output is default)
~/.yylo/services/gemini.py -p "Summarize the README" -m :pro-3

# Include project context and enable debug logging
~/.yylo/services/gemini.py -p "Audit the project" --include-directories src,docs --debug

# Auto-approve actions explicitly (default when no approval mode provided)
~/.yylo/services/gemini.py -p "Refactor the code" --yolo

# Emit non-streaming JSON if needed
~/.yylo/services/gemini.py -p "Quick JSON response" --output-format json
```

#### Arguments

- `-p, --prompt <text>`: Prompt text (required, mutually exclusive with --prompt-file)
- `-pp, --prompt-file <path>`: Path to prompt file (required if no --prompt)
- `--cd <path>`: Project path (default: current directory)
- `-m, --model <name>`: Gemini model (supports shorthand aliases)
- `--output-format <stream-json|json|text>`: Output format (default: stream-json)
- `--include-directories <list>`: Comma-separated directories to include
- `--approval-mode <mode>`: Approval mode (e.g., auto_edit). If omitted, `--yolo` is applied for headless automation.
- `--yolo`: Auto-approve actions (non-interactive)
- `--debug`: Enable Gemini CLI debug output
- `--verbose`: Print the constructed command before execution

### pi.py

A wrapper for the Pi coding agent CLI -- a multi-provider coding agent that supports Anthropic, OpenAI, Google, Groq, xAI, and more.

#### Prerequisites

Pi requires separate installation of the pi-coding-agent CLI:

```bash
npm install -g @mariozechner/pi-coding-agent
```

#### Features

- Multi-provider support (Anthropic, OpenAI, Google, Groq, xAI, etc.)
- Model shorthand aliases (`:pi`, `:sonnet`, `:opus`, `:luna`, `:sol`, `:gpt`, `:gpt5.5`, `:mini`, `:gpt-5`, `:api-codex`, `:gemini-pro`, etc.)
- Support for inline prompts or prompt files
- Headless JSON mode with one line-oriented human formatter: jq-colored compact headers on TTYs, dim italic thinking, bold assistant text, cyan tool calls, and green/red tool results
- Tool progress after 500 ms and bounded results (first 15 lines, omitted-middle count, final 2 lines); pipes keep the same layout without ANSI and `PI_PRETTY=false` preserves raw NDJSON
- Live interactive mode via `--live` (Pi TUI + auto-exit on non-aborted `agent_end`)
- Temporary live extension capture (`JUNO_SUBAGENT_CAPTURE_PATH`) for iteration summaries/cost
- Verbose mode for debugging

#### Usage

```bash
# Basic headless JSON-mode usage with Anthropic model
~/.yylo/services/pi.py -p "Write a hello world function" -m :sonnet

# Use with Codex Sol shortcut (:gpt aliases to :sol)
~/.yylo/services/pi.py -p "Refactor code" -m :gpt

# Use with Codex Terra or older Codex GPT 5.5 shortcuts
~/.yylo/services/pi.py -p "Implement focused fix" -m :mini
~/.yylo/services/pi.py -p "Refactor code" -m :gpt5.5

# Use with OpenAI model
~/.yylo/services/pi.py -p "Refactor code" -m :gpt-5

# Use with Gemini model
~/.yylo/services/pi.py -p "Add tests" -m :gemini-pro

# Live interactive mode (Pi TUI + auto-exit extension on non-aborted completion)
~/.yylo/services/pi.py --live -p "Summarize this repo" -m :api-codex

# Specify project directory
~/.yylo/services/pi.py -p "Fix bugs" --cd /path/to/project

# Enable verbose output
~/.yylo/services/pi.py -p "Analyze code" --verbose
```

#### Arguments

- `-p, --prompt <text>`: Prompt text (required, mutually exclusive with --prompt-file)
- `-pp, --prompt-file <path>`: Path to prompt file (required if no --prompt)
- `--cd <path>`: Project path (default: current directory)
- `-m, --model <name>`: Model name (supports shorthand aliases, including `:luna` → `openai-codex/gpt-5.6-luna`, `:sol` → `openai-codex/gpt-5.6-sol`, `:gpt` → `:sol`, `:gpt5.5` → `openai-codex/gpt-5.5`, `:mini` → `openai-codex/gpt-5.6-terra`, `:codex` → `openai-codex/gpt-5.3-codex`, and `:api-codex` → `openai/gpt-5.3-codex`)
- `--thinking <level>`: Thinking level (`off`, `minimal`, `low`, `medium`, `high`, `xhigh`, or `max`); GPT-5.6 models support `max`
- `--live`: Run Pi in interactive mode (no `--mode json`, prompt passed positionally)
- `--no-extensions`: Disable Pi extensions (incompatible with `--live`)
- `--verbose`: Enable verbose output

Headless turn cost display is provider-neutral. Set `headlessUi.turnCostDisplayThresholdUsd` in `.juno_task/config.json` (default `0.5`), or override it with `HEADLESS_UI_TURN_COST_DISPLAY_THRESHOLD_USD`. Authoritative per-turn cost is shown only when it is strictly above the threshold; unavailable cost is omitted.

#### Via yylo

```bash
# Run Pi through yylo (headless default)
yylo -b shell -s pi -m :sonnet -i 1 -v -p "your task"

# Run Pi in live interactive mode (auto-exits on non-aborted completion)
yylo pi --live -p '/skill:ralph-loop' -i 1

# Override the :gpt default when a different provider or model is required
yylo pi --live -m :sonnet -p "your task" -i 1

# Quick shortcut
yylo pi "your task"
```

Notes:
- `--live` is Pi-only and expects an interactive terminal for clean TUI rendering.
- Esc interruptions do not auto-exit Pi: interrupted (`stopReason=aborted`) turns keep the live session open.
- To manually exit Pi and return control to yylo, use Pi's normal exit keys (for example `Ctrl+C` twice quickly or `Ctrl+D` on an empty editor).
- Pi TUI should run on a modern Node runtime (Node 20+ recommended).

## Customization

All service scripts installed in `~/.yylo/services/` can be modified to suit your needs. This directory is designed for user customization.

### Adding Custom Services

You can add your own service scripts to `~/.yylo/services/`:

1. Create a new Python script (e.g., `my-service.py`)
2. Make it executable: `chmod +x ~/.yylo/services/my-service.py`
3. Use it from anywhere: `~/.yylo/services/my-service.py`

### Service Script Template

Here's a basic template for creating your own service:

```python
#!/usr/bin/env python3
"""
My Custom Service
Description of what this service does
"""

import argparse
import subprocess
import sys

def main():
    parser = argparse.ArgumentParser(description="My Custom Service")
    parser.add_argument('-p', '--prompt', required=True, help='Prompt text')
    parser.add_argument('--cd', default='.', help='Working directory')

    args = parser.parse_args()

    # Your service logic here
    print(f"Processing: {args.prompt}")

    return 0

if __name__ == "__main__":
    sys.exit(main())
```

## Future Extensions

These service scripts are part of yylo's extensibility model. In future versions, you'll be able to:

- Use these scripts as alternative backends for yylo subagents
- Create custom subagent implementations without MCP server dependency
- Share and install community-created service scripts
- Integrate with other AI coding tools and CLIs

## Requirements

Service scripts require Python 3.6+ to be installed on your system. Individual services may have additional requirements:

- **codex.py**: Requires OpenAI Codex CLI to be installed
- **claude.py**: Requires Anthropic Claude CLI to be installed (see https://docs.anthropic.com/en/docs/agents-and-tools/claude-code)
- **gemini.py**: Requires Gemini CLI to be installed (see https://geminicli.com/docs/cli/headless/)
- **pi.py**: Requires Pi coding agent CLI to be installed (`npm install -g @mariozechner/pi-coding-agent`)

## Troubleshooting

### Services not found

If services are not installed, run:

```bash
yylo services install
```

### Permission denied

Make sure scripts are executable:

```bash
chmod +x ~/.yylo/services/*.py
```

### Python not found

Ensure Python 3 is installed and available in your PATH:

```bash
python3 --version
```

## Support

For issues or feature requests related to service scripts, please visit:
https://github.com/yylo-dev/yylo/issues
