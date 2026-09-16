#!/usr/bin/env python3
"""
Run yylo tasks in parallel with queue management, output extraction, and aggregation.

Modes:
  Headless (default): ThreadPoolExecutor, output to log files only.
  Tmux windows:       Each worker = tmux window, coordinator = window 0.
  Tmux panes:         Workers as split panes, coordinator = top pane.
  Tmux tabs:          One dedicated tmux window/tab per task, named by task ID.
  Tmux handoff:       Add --tmux-handoff so each task gets a dedicated pane/window
                      that is not reused after completion. Add --max-panes-per-session N
                      to split large handoff lists across multiple tmux sessions.

Usage:
  # Kanban mode (task IDs as input)
  ./parallel_runner.sh --kanban TASK1,TASK2,TASK3 [--parallel 3]
  ./parallel_runner.sh --kanban TASK1 TASK2 TASK3 --parallel 2
  ./parallel_runner.sh --kanban "TASK1 TASK2 TASK3"
  ./parallel_runner.sh --kanban T1 T2 --prompt-file instructions.md
  ./parallel_runner.sh --kanban T1 T2 --prompt "Handle ## {{task_id}} with {{item}}"
  echo "Handle ## {{task_id}}" | ./parallel_runner.sh --kanban T1 --prompt -

  # Generic items mode (any list, requires --prompt-file/--prompt with {{item}})
  ./parallel_runner.sh --items "url1,url2,url3" --prompt-file crawl.md
  ./parallel_runner.sh --items shop1 shop2 shop3 --prompt "Analyze {{item}}"

  # File mode (structured data)
  ./parallel_runner.sh --items-file data.jsonl --prompt-file analyze.md
  ./parallel_runner.sh --items-file data.csv --strict --file-format csv

  # Raw command file mode (schema/lint/generator foundation)
  ./parallel_runner.sh --init-commands-example .juno_task/commands/workflows.yaml
  ./parallel_runner.sh --lint-commands-file .juno_task/commands/workflows.yaml
  ./parallel_runner.sh --commands-file .juno_task/commands/workflows.yaml --parallel 3

  # Common options
  ./parallel_runner.sh --tmux --kanban T1 T2 T3 --parallel 2
  ./parallel_runner.sh --tmux panes --kanban T1 T2 --parallel 2
  ./parallel_runner.sh --tmux tabs --no-attach --kanban T1 T2 T3
  ./parallel_runner.sh --tmux panes --tmux-handoff --items a b --parallel 2 --prompt "Analyze {{item}}"
  ./parallel_runner.sh --tmux panes --tmux-handoff --max-panes-per-session 4 --items a b c d e --name triage --prompt "Analyze {{item}}"
  ./parallel_runner.sh --tmux --kanban T1 T2 --name my-batch
  ./parallel_runner.sh -s codex --kanban T1 T2
  ./parallel_runner.sh -s pi -m gpt-5 --kanban T1 T2
  ./parallel_runner.sh -s pi --subagent-args "--live" --kanban T1 T2
  ./parallel_runner.sh --stop                    # stop only running session
  ./parallel_runner.sh --stop --name my-batch    # stop specific session
  ./parallel_runner.sh --stop-all                # stop all sessions

Input modes (exactly one required, unless --stop/--stop-all):
  --kanban       Kanban task IDs. {{task_id}} = {{item}} = the ID.
  --kanban-filter Filter string passed to kanban.sh list. Internally runs
                 kanban.sh list {filters} -f json --raw and extracts IDs.
  --items        Generic item list (comma/space separated). Auto-generates item-001 IDs.
  --items-file   Path to file (JSONL, CSV, TSV, XLSX). Format auto-detected by extension.
  --commands-file Path to YAML/JSON command batch. Schema v1 fans out command IDs only;
                 raw command execution is handled by the command scheduler increment.

Command file schema v1:
  schema_version: 1
  name: workflow-batch
  parallel: 2        # default only; explicit CLI --parallel wins
  cwd: .             # resolved relative to invocation cwd
  env:
    EXAMPLE: value
  commands:          # required non-empty list
    - id: workflow-a # unique [A-Za-z0-9_.-]+
      command:       # string => shell later; list => direct argv later
        - ./.juno_task/scripts/workflow_runner.sh
        - --workflow
        - .juno_task/workflows/a.yaml
    - id: smoke
      command: |
        cd yylo && npm test -- src/utils/__tests__/script-installer.test.ts
      cwd: .
      env:
        NODE_ENV: test
      timeout_seconds: 300

Command file utilities:
  --lint-commands-file PATH|-  Parse and validate command YAML/JSON without executing.
  --init-commands-example PATH Write boilerplate command YAML. Use --force to overwrite.

File options (for --items-file):
  --format       Force file format: jsonl, csv, tsv, xlsx (default: auto-detect).
  --no-header    CSV/TSV/XLSX: treat first row as data, not column headers.
  --chunk-size   Records per item (default: 1). >1 groups records into a JSON array.
  --start        First record to process, 1-indexed after header (default: 1).
  --end          Last record to process, inclusive (default: end of file).

Output extraction:
  --file-format  Expected output format (e.g., json, csv, md). Sets {{file_format}} placeholder.
  --strict       Extract response from fenced code block in output.
                 Writes extracted content to {output_dir}/{task_id}.{file_format}.
                 Marks task as ERROR if code block not found. Requires --file-format.

Arguments:
  --parallel     Max concurrent subprocesses (default: 3)
  -s, --service  Backend service: claude, codex, pi (default: claude). Env: JUNO_SERVICE.
  -m, --model    Model override. Env: JUNO_MODEL.
  --env          Environment overrides. KEY=VALUE pairs or path to .env file.
  --prompt-file  Path to a file whose content is appended to the prompt.
                 Loaded once at startup; per-task prompt files are materialized under logs/tmp.
                 Placeholders: {{task_id}}, {{item}}, {{file_format}}.
  --prompt       Inline prompt template content (same placeholders as --prompt-file).
                 Use --prompt - to read template content from stdin/heredoc.
  --subagent-args Extra raw args appended to each yylo invocation.
                 Example: --subagent-args "--live --thinking high"
  --tmux         Run in tmux mode. 'windows' (default), 'panes' (side-by-side),
                 or 'tabs' (one dedicated window/tab per task named by task ID).
  --no-attach    Start the tmux session and orchestrator in the background without attaching.
                 Prints attach, follow, wait, and stop commands for nohup/CI/cron use.
  --tmux-handoff Tmux-only mode: dedicate one worker pane/window per task and do not reuse it after completion.
  --max-panes-per-session N  With --tmux-handoff, split large task lists into capped tmux sessions.
  --name         Session name (default: auto-generated batch-N). Tmux session = pc-{name}.
  --output-dir   Structured output directory. Default: /tmp/yylo-sessions/{date}/{run_id}.
  --stop         Stop a session. Uses --name if provided, otherwise auto-detects.
  --stop-all     Stop ALL running sessions.
"""

from __future__ import annotations

# Runtime guard: fail with actionable guidance before any later import or
# runtime type evaluation can surface an opaque interpreter error. The guard
# itself uses only syntax every supported-and-unsupported Python parses.
import os as _os
import sys as _sys

_PYTHON_MINIMUM = (3, 10)

if _sys.version_info < _PYTHON_MINIMUM:
    _detected = ".".join(str(_part) for _part in _sys.version_info[:3])
    _required = ".".join(str(_part) for _part in _PYTHON_MINIMUM)
    _script = _os.path.abspath(__file__)
    # Installed layout: <project>/.juno_task/scripts/parallel_runner.sh
    _project_root = _os.path.dirname(_os.path.dirname(_os.path.dirname(_script)))
    _venv_python = _os.path.join(_project_root, ".venv_juno", "bin", "python")
    if _os.path.isfile(_venv_python):
        _recommendation = (
            "Recommended command:\n"
            "  %s %s --help\n"
            "If this managed runtime is stale, run `yy scripts update --force`." % (_venv_python, _script)
        )
    else:
        _recommendation = (
            "No managed Python runtime was found at:\n  %s\n"
            "Run `yy scripts update --force` (or .juno_task/scripts/install_requirements.sh)\n"
            "to provision .venv_juno, then re-run through .venv_juno/bin/python."
            % _venv_python
        )
    _sys.stderr.write(
        "parallel_runner: unsupported Python %s; %s or newer is required.\n"
        "Detected interpreter: %s\n%s\n"
        % (_detected, _required, _sys.executable or "unknown", _recommendation)
    )
    _sys.exit(2)

import argparse
import copy
import csv
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import textwrap
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

# SourceFileLoader-based tooling does not automatically add this script's
# directory to sys.path. Resolve the managed sibling helper explicitly so the
# executable and import-based lint/probe surfaces use the same contract.
_SCRIPT_DIRECTORY = str(Path(__file__).resolve().parent)
if _SCRIPT_DIRECTORY not in sys.path:
    sys.path.insert(0, _SCRIPT_DIRECTORY)
from invocation_correlation import child_invocation_environment


_SCOPED_CONTINUITY_KEY_PREFIXES = (
    "YYLO_LAST_SESSION_ID_SCOPE_",
    "YYLO_LAST_EXECUTION_SETTINGS_SCOPE_",
)
_LEGACY_CONTINUITY_KEYS = {
    "YYLO_LAST_SESSION_ID",
    "YYLO_LAST_EXECUTION_SETTINGS",
}


def _child_process_environment(base):
    """Preserve child config/routing while dropping historical continuity values."""
    return {
        name: value
        for name, value in base.items()
        if name not in _LEGACY_CONTINUITY_KEYS and not name.startswith(_SCOPED_CONTINUITY_KEY_PREFIXES)
    }


SCRIPT_DIR = Path(__file__).resolve().parent

STALE_CHECK_ENV = "YYLO_SKIP_SCRIPT_STALE_CHECK"
TEMPLATE_DIR_ENV = "YYLO_SCRIPT_TEMPLATE_DIR"


def _display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def _is_package_template_path(path: Path) -> bool:
    parts = path.parts
    template_suffixes = (("dist", "templates", "scripts"), ("src", "templates", "scripts"))
    for suffix in template_suffixes:
        suffix_len = len(suffix)
        if len(parts) >= suffix_len + 1 and tuple(parts[-suffix_len - 1 : -1]) == suffix:
            return True
    return False


def _installed_template_candidates(script_name: str) -> list[Path]:
    candidates: list[Path] = []
    env_template_dir = os.environ.get(TEMPLATE_DIR_ENV)
    if env_template_dir:
        candidates.append(Path(env_template_dir).expanduser() / script_name)

    for command_name in ("yy", "yylo", "ypl"):
        command_path = shutil.which(command_name)
        if not command_path:
            continue
        try:
            resolved = Path(command_path).resolve()
        except OSError:
            resolved = Path(command_path)
        for parent in (resolved.parent, *resolved.parents):
            candidates.append(parent / "dist" / "templates" / "scripts" / script_name)
            candidates.append(parent / "templates" / "scripts" / script_name)

    seen: set[str] = set()
    unique: list[Path] = []
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def warn_if_runtime_script_is_stale(script_name: str) -> None:
    if os.environ.get(STALE_CHECK_ENV) == "1":
        return
    try:
        runtime_path = Path(__file__).resolve()
        if not os.environ.get(TEMPLATE_DIR_ENV) and _is_package_template_path(runtime_path):
            return
        runtime_hash = hashlib.sha256(runtime_path.read_bytes()).hexdigest()
        for template_path in _installed_template_candidates(script_name):
            try:
                installed_path = template_path.resolve()
            except OSError:
                installed_path = template_path
            if installed_path == runtime_path or not template_path.is_file():
                continue
            installed_hash = hashlib.sha256(template_path.read_bytes()).hexdigest()
            if runtime_hash != installed_hash:
                print(
                    f"{script_name}: warning: this runtime script differs from the installed yylo template.\n"
                    f"  runtime: {_display_path(runtime_path)}\n"
                    f"  installed template: {installed_path}\n"
                    "  update with: yy scripts update --force",
                    file=sys.stderr,
                )
            return
    except Exception:
        return
# _log_base is the flat root for discoverable session files (PID, pause, dashboard).
# LOG_DIR (computed at startup in main()) nests under _log_base/{date}/{run_id}/
# for per-task logs and the combined log — isolating concurrent/repeated runs.
_log_base = SCRIPT_DIR / "logs"
LOG_DIR = _log_base  # overwritten in main() with run-ID path
COMBINED_LOG = LOG_DIR / "parallel_runner.log"  # overwritten in main()
STATUS_FILE = LOG_DIR / "parallel_runner_status.json"  # overwritten in main()

# Run timestamp token (HHMMSS-microseconds), generated once at startup in main()
_run_id = ""
_run_started_at = ""

# Temporary runtime artifacts are stored under logs/tmp and purged periodically.
_TMP_DIR_NAME = "tmp"
_TMP_STALE_MAX_AGE_SECONDS = 48 * 60 * 60

# Thread-safe lock for writing to the shared combined log
_log_lock = threading.Lock()

# Shared counters for remaining-task tracking
_completed_count = 0
_completed_lock = threading.Lock()
_total_tasks = 0

# Per-task elapsed times
_task_times = {}
_task_times_lock = threading.Lock()

# Shutdown flag for graceful exit (set by signal handlers)
_shutdown_event = threading.Event()

# --- Color system for task identification ---
# ANSI 256-color codes picked for high contrast between consecutive colors,
# visibility on both dark and light terminal backgrounds.
_TASK_COLORS = [
    196,  # red
    39,   # dodger blue
    208,  # orange
    35,   # cyan-green
    201,  # magenta/pink
    220,  # gold
    27,   # blue
    118,  # bright green
    163,  # rose
    45,   # turquoise
    214,  # dark orange
    99,   # purple
    82,   # lime
    197,  # hot pink
    33,   # royal blue
    215,  # sandy orange
    48,   # sea green
    135,  # medium purple
    226,  # yellow
    69,   # cornflower blue
]
_RESET = "\033[0m"

# --- Service / model defaults ---
_VALID_SERVICES = ("claude", "codex", "pi")
_DEFAULT_SERVICE = "claude"
_SERVICE_DEFAULT_MODEL = {
    "claude": ":sonnet",
    "codex": ":codex",
    "pi": "openai-codex/gpt-5.3-codex",
}


_SAFE_COMMAND_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_COMMAND_SCHEMA_VERSIONS = {"1", "1.0", "v1"}


class CommandFileError(Exception):
    '''Raised when a command YAML/JSON file is invalid.'''


def _count_indent(line):
    return len(line) - len(line.lstrip(" "))


def _parse_flow_sequence(value):
    content = value[1:-1].strip()
    if not content:
        return []
    items = []
    token = []
    quote = None
    escaped = False
    for char in content:
        if escaped:
            token.append(char)
            escaped = False
            continue
        if quote == '"' and char == "\\":
            token.append(char)
            escaped = True
            continue
        if quote:
            token.append(char)
            if char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
            token.append(char)
            continue
        if char == ",":
            item = "".join(token).strip()
            if not item:
                raise CommandFileError("flow sequence contains an empty item")
            items.append(_parse_scalar(item))
            token = []
            continue
        if char in "[]{}":
            raise CommandFileError("nested flow collections require PyYAML")
        token.append(char)
    if quote or escaped:
        raise CommandFileError("unterminated quoted scalar in flow sequence")
    item = "".join(token).strip()
    if not item:
        raise CommandFileError("flow sequence contains an empty item")
    items.append(_parse_scalar(item))
    return items


def _parse_scalar(value):
    value = value.strip()
    if value == "":
        return ""
    if value.startswith("[") and value.endswith("]"):
        return _parse_flow_sequence(value)
    if value in ("null", "Null", "NULL", "~"):
        return None
    if value in ("true", "True", "TRUE"):
        return True
    if value in ("false", "False", "FALSE"):
        return False
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            return json.loads(value)
        except (TypeError, ValueError) as exc:
            raise CommandFileError(f"invalid double-quoted scalar: {exc}") from exc
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    try:
        if re.match(r"^-?\d+$", value):
            return int(value)
        if re.match(r"^-?(\d+\.\d*|\d*\.\d+)$", value):
            return float(value)
    except ValueError:
        pass
    return value


def _read_literal_block(lines, start, parent_indent):
    i = start
    block_lines = []
    block_indent = None
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            block_lines.append("")
            i += 1
            continue
        indent = _count_indent(line)
        if indent <= parent_indent:
            break
        if block_indent is None:
            block_indent = indent
        block_lines.append(line[min(block_indent, len(line)):])
        i += 1
    return "\n".join(block_lines).rstrip("\n"), i


def _parse_indented_mapping(lines, start, parent_indent):
    result = {}
    i = start
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        indent = _count_indent(line)
        if indent <= parent_indent:
            break
        if ":" not in stripped:
            raise CommandFileError(f"expected mapping field near line {i + 1}: {line}")
        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value == "|":
            result[key], i = _read_literal_block(lines, i + 1, indent)
            continue
        if value == "":
            nested, i = _parse_indented_mapping(lines, i + 1, indent)
            result[key] = nested
            continue
        result[key] = _parse_scalar(value)
        i += 1
    return result, i


def _parse_scalar_list(lines, start, parent_indent):
    values = []
    i = start
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        indent = _count_indent(line)
        if indent <= parent_indent:
            break
        if not stripped.startswith("-"):
            break
        values.append(_parse_scalar(stripped[1:].strip()))
        i += 1
    return values, i


def _parse_commands_list(lines, start, parent_indent):
    commands = []
    i = start
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        indent = _count_indent(line)
        if indent <= parent_indent:
            break
        if not stripped.startswith("-"):
            raise CommandFileError(f"expected command list item near line {i + 1}: {line}")
        item = {}
        rest = stripped[1:].strip()
        if rest:
            if ":" not in rest:
                raise CommandFileError(f"expected key/value after '-' near line {i + 1}")
            key, value = rest.split(":", 1)
            item[key.strip()] = _parse_scalar(value.strip())
        i += 1
        while i < len(lines):
            child = lines[i]
            child_stripped = child.strip()
            child_indent = _count_indent(child)
            if not child_stripped or child_stripped.startswith("#"):
                i += 1
                continue
            if child_indent <= indent:
                break
            if ":" not in child_stripped:
                raise CommandFileError(f"expected command field near line {i + 1}: {child}")
            key, value = child_stripped.split(":", 1)
            key = key.strip()
            value = value.strip()
            if value == "|":
                item[key], i = _read_literal_block(lines, i + 1, child_indent)
                continue
            if value == "":
                if key == "command":
                    item[key], i = _parse_scalar_list(lines, i + 1, child_indent)
                elif key == "env":
                    item[key], i = _parse_indented_mapping(lines, i + 1, child_indent)
                else:
                    item[key], i = _parse_indented_mapping(lines, i + 1, child_indent)
                continue
            item[key] = _parse_scalar(value)
            i += 1
        commands.append(item)
    return commands, i


def _parse_command_yaml_like(text):
    '''Parse command YAML/JSON without requiring PyYAML.'''
    try:
        loaded = json.loads(text)
        if isinstance(loaded, dict):
            return loaded
        raise CommandFileError("command file must be a YAML/JSON mapping")
    except json.JSONDecodeError:
        pass
    try:
        import yaml  # type: ignore
        loaded = yaml.safe_load(text)
        if not isinstance(loaded, dict):
            raise CommandFileError("command file must be a YAML mapping")
        return loaded
    except ImportError:
        pass
    except CommandFileError:
        raise
    except Exception as exc:
        raise CommandFileError(f"failed to parse command YAML: {exc}") from exc
    lines = text.splitlines()
    root = {}
    i = 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        if _count_indent(raw) != 0 or ":" not in stripped:
            raise CommandFileError(f"unsupported YAML near line {i + 1}: {raw}")
        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value == "|":
            root[key], i = _read_literal_block(lines, i + 1, _count_indent(raw))
            continue
        if value:
            root[key] = _parse_scalar(value)
            i += 1
            continue
        if key == "commands":
            root[key], i = _parse_commands_list(lines, i + 1, _count_indent(raw))
        else:
            root[key], i = _parse_indented_mapping(lines, i + 1, _count_indent(raw))
    return root


def _read_command_file(path):
    if path == "-":
        if sys.stdin.isatty():
            raise CommandFileError("--commands-file - requires redirected stdin (pipe/heredoc)")
        text = sys.stdin.read()
    else:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise CommandFileError(f"could not read command file {path}: {exc}") from exc
    if not text.strip():
        raise CommandFileError("command file is empty")
    return text


def _validate_command_env(env, label):
    if env is None:
        return {}
    if not isinstance(env, dict):
        raise CommandFileError(f"{label} env must be a mapping")
    normalized = {}
    for key, value in env.items():
        key_text = str(key)
        if not key_text:
            raise CommandFileError(f"{label} env contains an empty key")
        if isinstance(value, (dict, list)):
            raise CommandFileError(f"{label} env value for {key_text} must be a scalar")
        normalized[key_text] = "" if value is None else str(value)
    return normalized


def _resolve_command_cwd(value, base_cwd, label):
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise CommandFileError(f"{label} cwd must be a non-empty string")
    cwd_path = Path(value)
    if not cwd_path.is_absolute():
        cwd_path = Path(base_cwd) / cwd_path
    return str(cwd_path.resolve())


def _validate_command_file(data, base_cwd):
    if not isinstance(data, dict):
        raise CommandFileError("command file must be a mapping")
    if "schema_version" in data and str(data["schema_version"]).strip() not in _COMMAND_SCHEMA_VERSIONS:
        raise CommandFileError(f"unsupported schema_version: {data['schema_version']}")
    commands = data.get("commands")
    if not isinstance(commands, list) or not commands:
        raise CommandFileError("command file must define a non-empty commands list")
    if "name" in data and data["name"] is not None and not isinstance(data["name"], str):
        raise CommandFileError("name must be a string when provided")
    if "parallel" in data:
        parallel = data["parallel"]
        if isinstance(parallel, bool) or not isinstance(parallel, int) or parallel < 1:
            raise CommandFileError("parallel must be a positive integer when provided")
    root_cwd = _resolve_command_cwd(data.get("cwd"), base_cwd, "top-level")
    root_env = _validate_command_env(data.get("env"), "top-level")
    seen = set()
    normalized = []
    for idx, command_entry in enumerate(commands, start=1):
        if not isinstance(command_entry, dict):
            raise CommandFileError(f"command {idx} must be a mapping")
        command_id = str(command_entry.get("id") or "").strip()
        if not command_id:
            raise CommandFileError(f"command {idx} is missing required id")
        if not _SAFE_COMMAND_ID_RE.match(command_id):
            raise CommandFileError(f"command id contains unsupported characters: {command_id}")
        if command_id in seen:
            raise CommandFileError(f"duplicate command id: {command_id}")
        seen.add(command_id)
        if "command" not in command_entry:
            raise CommandFileError(f"command {command_id} is missing required command")
        command = command_entry["command"]
        if isinstance(command, str):
            if not command.strip():
                raise CommandFileError(f"command {command_id} command string must be non-empty")
        elif isinstance(command, list):
            if not command:
                raise CommandFileError(f"command {command_id} argv list must be non-empty")
            for part in command:
                if isinstance(part, (dict, list)):
                    raise CommandFileError(f"command {command_id} argv entries must be scalars")
                if str(part) == "":
                    raise CommandFileError(f"command {command_id} argv entries must be non-empty")
            command = [str(part) for part in command]
        else:
            raise CommandFileError(f"command {command_id} command must be a string or argv list")
        timeout_seconds = command_entry.get("timeout_seconds")
        if timeout_seconds is not None:
            if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
                raise CommandFileError(f"command {command_id} timeout_seconds must be positive when provided")
        normalized.append({
            "id": command_id,
            "command": command,
            "cwd": _resolve_command_cwd(command_entry.get("cwd"), base_cwd, f"command {command_id}") or root_cwd,
            "env": {**root_env, **_validate_command_env(command_entry.get("env"), f"command {command_id}")},
            "timeout_seconds": timeout_seconds,
        })
    return {"schema_version": data.get("schema_version", 1), "name": data.get("name"), "parallel": data.get("parallel"), "cwd": root_cwd, "env": root_env, "commands": normalized}


def _load_commands_file(path, base_cwd):
    return _validate_command_file(_parse_command_yaml_like(_read_command_file(path)), base_cwd)


_COMMANDS_EXAMPLE = '''schema_version: 1
name: workflow-batch
parallel: 2
cwd: .
env:
  EXAMPLE: value
commands:
  - id: workflow-a
    command:
      - ./.juno_task/scripts/workflow_runner.sh
      - --workflow
      - .juno_task/workflows/a.yaml
  - id: workflow-b
    command:
      - ./.juno_task/scripts/workflow_runner.sh
      - --workflow
      - .juno_task/workflows/b.yaml
  - id: smoke
    command: |
      cd yylo && npm test -- src/utils/__tests__/script-installer.test.ts
    timeout_seconds: 300
'''


def _init_commands_example(path, force=False):
    target = Path(path)
    if target.exists() and not force:
        raise CommandFileError(f"refusing to overwrite existing command file: {target} (pass --force to replace)")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_COMMANDS_EXAMPLE, encoding="utf-8")
    return target

# Resolved environment overrides from --env args (populated at parse time)
_env_overrides = {}

# Map task_id -> color (assigned at startup)
_task_color_map = {}

# Map task_id -> item value (the full input data for each task)
# For --kanban: item == task_id. For --items/--items-file: item is the real data.
_item_map = {}

# Map command id -> normalized command spec for --commands-file execution.
_command_spec_by_id = {}

# Python helper script piped via tmux pipe-pane to format output into log files.
# argv: task_id, task_log_path, combined_log_path, ansi_color_code
# Combined log gets colored dot; task log stays plain.
_LOG_PIPE_SCRIPT = r"""#!/usr/bin/env python3
import re, sys
from datetime import datetime

task_id = sys.argv[1]
task_log_path = sys.argv[2]
combined_log_path = sys.argv[3]
color_code = sys.argv[4] if len(sys.argv) > 4 else '7'
ansi_re = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')
dot = '\033[38;5;%sm\u25cf\033[0m' % color_code  # colored dot

with open(task_log_path, 'w') as tl, open(combined_log_path, 'a') as cl:
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        line = line.rstrip('\n\r')
        if not line:
            continue
        ts = datetime.now().strftime('%H:%M:%S')
        clean = ansi_re.sub('', line)
        tl.write('[%s] [%s] %s\n' % (ts, task_id, clean))
        tl.flush()
        cl.write('[%s] %s [%s] %s\n' % (ts, dot, task_id, line))
        cl.flush()
"""


# ---------------------------------------------------------------------------
# Per-session file helpers
# ---------------------------------------------------------------------------

def _session_name_to_tmux(name):
    """Convert session name to tmux session name."""
    return f"pc-{name}"


def _tmp_root():
    return _log_base / _TMP_DIR_NAME


def _session_state_root():
    """Legacy shared state root kept for backwards-compatible cleanup/discovery."""
    return _tmp_root() / ".session_state"


def _dashboard_file(name, run_dir=None):
    """Dashboard path for a specific run directory."""
    base_dir = run_dir or LOG_DIR
    return base_dir / ".dashboard"


def _pause_file(name, run_dir=None):
    """Pause file path for a specific run directory."""
    base_dir = run_dir or LOG_DIR
    return base_dir / ".pause"


def _pid_file(name, run_dir=None):
    """PID marker for a specific run directory/session name."""
    base_dir = run_dir or LOG_DIR
    return base_dir / f"orchestrator_pid_{name}"


def _pid_files_for_name(name):
    """Return new-layout + legacy PID files for a given session name."""
    files = list(_log_base.glob(f"*/*/orchestrator_pid_{name}"))
    legacy_root = _session_state_root()
    files.extend(legacy_root.glob(f"orchestrator_pid_{name}"))
    files.append(_log_base / f".orchestrator_pid_{name}")
    return [f for f in files if f.exists()]


def _legacy_session_state_files(name):
    """Legacy state/lock files under logs/ root from older runner versions."""
    return [
        _log_base / f".dashboard_{name}",
        _log_base / f".pause_{name}",
        _log_base / f".orchestrator_pid_{name}",
    ]


def _orchestrator_log(name):
    return LOG_DIR / f"orchestrator_{name}.log"


def _write_run_status(state, mode, exit_code=None, pid=None, error=None, session_name=None):
    """Atomically write machine-readable run status for wait helpers.

    The status JSON is the single source of truth for run completion. Helpers
    should read this file instead of parsing human-readable log summaries.
    """
    payload = {
        "state": state,
        "run_id": _run_id,
        "log_path": str(COMBINED_LOG),
        "status_path": str(STATUS_FILE),
        "started_at": _run_started_at or None,
        "finished_at": datetime.now().isoformat() if state == "completed" else None,
        "exit_code": exit_code,
        "mode": mode,
        "pid": pid if pid is not None else os.getpid(),
    }
    if error:
        payload["error"] = str(error)
    if session_name:
        payload["session_name"] = session_name

    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = STATUS_FILE.with_name(f"{STATUS_FILE.name}.tmp.{os.getpid()}")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp_path, STATUS_FILE)


def _tmp_dir(name):
    # Keep runtime artifacts under logs/tmp so long-running sessions are
    # resilient to OS cleanup jobs and temp lifecycle is centralized.
    return _tmp_root() / name


def _legacy_tmp_paths():
    """Legacy temporary paths from older runner versions (.tmp_*)."""
    return list(_log_base.glob(".tmp_*"))


def _cleanup_tmp_path(path):
    """Remove a tmp file/dir path safely."""
    try:
        if path.is_dir():
            shutil.rmtree(str(path), ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def cleanup_stale_tmp_artifacts(max_age_seconds=_TMP_STALE_MAX_AGE_SECONDS):
    """Remove stale tmp artifacts older than max_age_seconds.

    Returns number of removed paths.
    """
    now = time.time()
    removed = 0

    candidates = []
    tmp_root = _tmp_root()
    if tmp_root.exists():
        for path in tmp_root.iterdir():
            if path.name.startswith('.'):
                continue
            candidates.append(path)

    candidates.extend(_legacy_tmp_paths())

    for path in candidates:
        try:
            age = now - path.stat().st_mtime
        except OSError:
            continue
        if age < max_age_seconds:
            continue
        if _cleanup_tmp_path(path):
            removed += 1

    return removed


def _write_log_pipe_helper(name):
    """Write the Python log-pipe helper under logs/tmp (once per session)."""
    tmp = _tmp_dir(name)
    tmp.mkdir(parents=True, exist_ok=True)
    helper_path = tmp / "log_pipe.py"
    helper_path.write_text(_LOG_PIPE_SCRIPT)
    return str(helper_path)


def _resolve_service_model(args):
    """Resolve service and model from CLI args > env vars > defaults."""
    service = getattr(args, "service", None) or os.environ.get("JUNO_SERVICE") or _DEFAULT_SERVICE
    service = service.lower()
    if service not in _VALID_SERVICES:
        print(f"ERROR: Invalid service '{service}'. Must be one of: {', '.join(_VALID_SERVICES)}",
              file=sys.stderr)
        sys.exit(2)
    model = getattr(args, "model", None) or os.environ.get("JUNO_MODEL") or _SERVICE_DEFAULT_MODEL[service]
    return service, model


def _parse_env_file(path):
    """Parse a .env file into a dict. Supports KEY=VALUE, comments (#), empty lines."""
    env = {}
    for line_num, raw_line in enumerate(
        Path(path).read_text(encoding="utf-8").splitlines(), 1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            print(f"WARNING: Skipping invalid line {line_num} in {path}: {line}",
                  file=sys.stderr)
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        env[key] = value
    return env


def _resolve_env_overrides(env_args):
    """Resolve --env arguments into a merged dict."""
    overrides = {}
    if not env_args:
        return overrides
    for arg in env_args:
        if "=" in arg:
            key_part = arg.split("=", 1)[0]
            if Path(key_part).exists() and not key_part.replace("_", "").replace("-", "").isalnum():
                overrides.update(_parse_env_file(arg))
            else:
                key, _, value = arg.partition("=")
                overrides[key.strip()] = value.strip()
        else:
            path_obj = Path(arg)
            if not path_obj.exists():
                print(f"ERROR: Env file not found: {arg}", file=sys.stderr)
                sys.exit(2)
            overrides.update(_parse_env_file(arg))
    return overrides


def _sanitize_current_process_environment():
    environment = _child_process_environment(dict(os.environ))
    os.environ.clear()
    os.environ.update(environment)


def _build_process_env(extra_capture_env=None, task_id=None):
    """Build the environment dict for one canonical child invocation."""
    env = _child_process_environment(dict(os.environ))
    env.update(_env_overrides)
    # Reduce buffering for python-backed subcommands so task/combined logs stream promptly.
    env.setdefault("PYTHONUNBUFFERED", "1")
    if extra_capture_env:
        env.update(extra_capture_env)
    return child_invocation_environment(
        env, launch_surface="parallel_runner", task_id=task_id, source=os.environ,
    )


def _is_command_mode(args):
    return bool(getattr(args, "commands_file", None))


def _command_spec_for(task_id):
    spec = _command_spec_by_id.get(task_id)
    if spec is None:
        raise KeyError(f"unknown command task id: {task_id}")
    return spec


def _format_command_for_log(command):
    if isinstance(command, list):
        return " ".join(shlex.quote(str(part)) for part in command)
    return command


def _command_popen_kwargs(spec, default_cwd, task_id=None):
    command = spec["command"]
    cwd = spec.get("cwd") or default_cwd
    env = _build_process_env(spec.get("env") or {}, task_id=task_id)
    if isinstance(command, list):
        return command, False, cwd, env
    return command, True, cwd, env


def _generate_env_exports(task_id=None):
    """Generate shell export lines for one canonical child invocation."""
    merged = _build_process_env(task_id=task_id)
    lines = []
    for key, value in sorted(merged.items()):
        if key in ("TERM_SESSION_ID", "TMUX", "TMUX_PANE", "STY", "WINDOW",
                    "SHLVL", "OLDPWD", "_"):
            continue
        lines.append(f"export {key}={shlex.quote(value)}")
    return "\n".join(lines)


def _resolve_subagent_args(raw_args):
    """Resolve repeatable --subagent-args strings into argv tokens."""
    resolved = []
    if not raw_args:
        return resolved
    for raw in raw_args:
        try:
            resolved.extend(shlex.split(raw))
        except ValueError as exc:
            print(f"ERROR: Invalid --subagent-args value '{raw}': {exc}", file=sys.stderr)
            sys.exit(2)
    return resolved


def _normalize_subagent_args_argv(argv):
    """Normalize common `subagent-args` invocation shapes before argparse.

    Supports:
    - `--subagent-args --live` (dash-prefixed value)
    - `subagent-args --live` (missing leading `--` typo)
    """
    normalized = []
    i = 0
    while i < len(argv):
        item = argv[i]
        if item == "subagent-args":
            item = "--subagent-args"
        if item == "--subagent-args" and i + 1 < len(argv):
            next_item = argv[i + 1]
            normalized.append(f"--subagent-args={next_item}")
            i += 2
            continue
        normalized.append(item)
        i += 1
    return normalized


def _contains_live_subagent_flag(subagent_args):
    """Detect Pi live-mode flags in resolved --subagent-args tokens."""
    for token in subagent_args or []:
        if token == "--live" or token.startswith("--live="):
            return True
    return False


# ---------------------------------------------------------------------------
# File parsing — multi-format pipeline
# ---------------------------------------------------------------------------

_FORMAT_EXTENSIONS = {
    ".jsonl": "jsonl",
    ".ndjson": "jsonl",
    ".csv": "csv",
    ".tsv": "tsv",
    ".xlsx": "xlsx",
}
_VALID_FORMATS = ("jsonl", "csv", "tsv", "xlsx")


def _detect_format(path, format_override=None):
    """Detect file format from extension or --format override."""
    if format_override:
        fmt = format_override.lower()
        if fmt not in _VALID_FORMATS:
            print(f"ERROR: Unknown format '{fmt}'. Must be one of: {', '.join(_VALID_FORMATS)}",
                  file=sys.stderr)
            sys.exit(2)
        return fmt
    ext = Path(path).suffix.lower()
    fmt = _FORMAT_EXTENSIONS.get(ext)
    if not fmt:
        print(f"ERROR: Cannot detect format from extension '{ext}'. "
              f"Use --format to specify one of: {', '.join(_VALID_FORMATS)}",
              file=sys.stderr)
        sys.exit(2)
    return fmt


def _record_to_item(record):
    """Convert a parsed record to its {{item}} string representation."""
    if isinstance(record, str):
        return record
    return json.dumps(record, ensure_ascii=False)


def _parse_jsonl(path):
    """Parse JSONL/NDJSON into a list of parsed values."""
    records = []
    for line_num, raw_line in enumerate(
        Path(path).read_text(encoding="utf-8").splitlines(), 1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as e:
            print(f"ERROR: Invalid JSON at {path}:{line_num}: {e}", file=sys.stderr)
            sys.exit(2)
    return records


def _parse_csv(path, has_header=True, delimiter=","):
    """Parse CSV/TSV into a list of records (dicts if header, lists otherwise)."""
    text = Path(path).read_text(encoding="utf-8")
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = list(reader)
    if not rows:
        return []
    if has_header:
        headers = rows[0]
        return [dict(zip(headers, row)) for row in rows[1:]]
    return [row for row in rows]


def _parse_xlsx(path, has_header=True):
    """Parse XLSX into a list of records (dicts if header, lists otherwise)."""
    try:
        import openpyxl
    except ImportError:
        print("ERROR: openpyxl is required for XLSX files. Install with:\n"
              "  pip install openpyxl", file=sys.stderr)
        sys.exit(2)
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows = []
    for row in ws.iter_rows(values_only=True):
        rows.append([cell if cell is not None else "" for cell in row])
    wb.close()
    if not rows:
        return []
    if has_header:
        headers = [str(h) for h in rows[0]]
        return [dict(zip(headers, row)) for row in rows[1:]]
    return rows


def _parse_items_file(path, format_override=None, has_header=True,
                      start=None, end=None, chunk_size=1):
    """Full pipeline: parse -> slice -> chunk -> return list of item strings."""
    file_path = Path(path)
    if not file_path.exists():
        print(f"ERROR: Items file not found: {path}", file=sys.stderr)
        sys.exit(2)

    fmt = _detect_format(path, format_override)

    if fmt == "jsonl":
        records = _parse_jsonl(path)
    elif fmt == "csv":
        records = _parse_csv(path, has_header=has_header, delimiter=",")
    elif fmt == "tsv":
        records = _parse_csv(path, has_header=has_header, delimiter="\t")
    elif fmt == "xlsx":
        records = _parse_xlsx(path, has_header=has_header)
    else:
        print(f"ERROR: Unsupported format '{fmt}'", file=sys.stderr)
        sys.exit(2)

    if not records:
        print(f"ERROR: No records found in {path}", file=sys.stderr)
        sys.exit(2)

    # Slice: --start and --end (1-indexed, inclusive, after header)
    start_idx = (start - 1) if start and start >= 1 else 0
    end_idx = end if end else len(records)
    records = records[start_idx:end_idx]

    if not records:
        print(f"ERROR: No records in range --start {start} --end {end} "
              f"(file has {end_idx} data rows)", file=sys.stderr)
        sys.exit(2)

    # Chunk: group records into chunks of --chunk-size
    items = []
    for i in range(0, len(records), chunk_size):
        chunk = records[i:i + chunk_size]
        if chunk_size == 1:
            items.append(_record_to_item(chunk[0]))
        else:
            items.append(json.dumps(chunk, ensure_ascii=False))

    return items


# ---------------------------------------------------------------------------
# Input resolution
# ---------------------------------------------------------------------------

def _resolve_input(args):
    """Resolve input mode and return task_ids list. Populates _item_map."""
    global _item_map, _command_spec_by_id

    modes = sum([
        bool(args.kanban),
        bool(args.items),
        bool(args.items_file),
        bool(getattr(args, "commands_file", None)),
    ])
    if modes == 0:
        print("ERROR: One of --kanban, --kanban-filter, --items, --items-file, or --commands-file is required "
              "(unless using --stop, --stop-all, --lint-commands-file, or --init-commands-example)", file=sys.stderr)
        sys.exit(2)
    if modes > 1:
        print("ERROR: Only one of --kanban, --kanban-filter, --items, --items-file, or --commands-file can be used",
              file=sys.stderr)
        sys.exit(2)

    if getattr(args, "commands_file", None):
        command_specs = getattr(args, "command_specs", None)
        if command_specs is None:
            try:
                loaded = _load_commands_file(args.commands_file, os.getcwd())
            except CommandFileError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                sys.exit(2)
            command_specs = loaded["commands"]
            args.command_specs = command_specs
        task_ids = [entry["id"] for entry in command_specs]
        _item_map = {entry["id"]: json.dumps(entry, ensure_ascii=False) for entry in command_specs}
        _command_spec_by_id = {entry["id"]: entry for entry in command_specs}
        return task_ids

    if args.kanban:
        task_ids = args.kanban
        _item_map = {tid: tid for tid in task_ids}
        return task_ids

    # Generic items — auto-generate IDs
    if args.items:
        raw_items = []
        for val in args.items:
            for part in val.replace(",", " ").split():
                part = part.strip()
                if part:
                    raw_items.append(part)
    else:
        raw_items = _parse_items_file(
            args.items_file,
            format_override=getattr(args, "format", None),
            has_header=not getattr(args, "no_header", False),
            start=getattr(args, "start", None),
            end=getattr(args, "end", None),
            chunk_size=getattr(args, "chunk_size", 1) or 1,
        )

    if not raw_items:
        print("ERROR: No items found in input", file=sys.stderr)
        sys.exit(2)

    width = max(len(str(len(raw_items))), 3)
    task_ids = []
    for i, item_value in enumerate(raw_items, 1):
        task_id = f"item-{i:0{width}d}"
        task_ids.append(task_id)
        _item_map[task_id] = item_value

    return task_ids


def _color_for(task_id):
    """Get the ANSI colored dot prefix for a task_id."""
    code = _task_color_map.get(task_id, 7)
    return f"\033[38;5;{code}m\u25cf{_RESET}"


def _colored_tag(task_id):
    """Return a colored dot + [task_id] string for log lines."""
    return f"{_color_for(task_id)} [{task_id}]"


# ---------------------------------------------------------------------------
# Structured output capture
# ---------------------------------------------------------------------------

def _resolve_output_dir(args):
    """Resolve the output directory for structured task output."""
    if args.output_dir:
        output_dir = Path(args.output_dir)
    elif os.environ.get("JUNO_OUTPUT_DIR"):
        output_dir = Path(os.environ["JUNO_OUTPUT_DIR"])
    else:
        today = datetime.now().strftime("%Y-%m-%d")
        output_dir = Path(f"/tmp/yylo-sessions/{today}")
    if _run_id:
        output_dir = output_dir / _run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def _task_output_path(output_dir, task_id):
    """Path for the per-task structured output JSON."""
    return output_dir / f"{task_id}.json"


def _parse_result_from_lines(lines):
    """Parse the most recent yylo result event from text lines."""
    for line in reversed(lines):
        idx = line.find('{"type":')
        if idx == -1:
            continue
        candidate = line[idx:]
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict) and parsed.get("type") == "result":
                return parsed
        except json.JSONDecodeError:
            continue
    return None


def _strip_parallel_log_prefix(line):
    if not line:
        return line

    # Task logs may prefix each line with relay metadata:
    #   [HH:MM:SS] [TASKID] ...
    # Strip these tokens before parsing yylo summary fields.
    line = re.sub(r"^\[[0-9]{2}:[0-9]{2}:[0-9]{2}\]\s+", "", line)
    line = re.sub(r"^\[[A-Za-z0-9_-]{3,}\]\s+", "", line)
    return line


def _looks_like_clock_token(token):
    if not token:
        return False
    return re.fullmatch(r"\[?[0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?\]?", token) is not None


def _parse_result_from_cli_summary_text(clean_text):
    """Build a synthetic result payload from yylo CLI summary text.

    This is a fallback when no structured {"type":"result"} object is present in logs.
    """
    if not clean_text:
        return None

    raw_lines = clean_text.splitlines()
    lines = [_strip_parallel_log_prefix(line) for line in raw_lines]
    normalized_text = "\n".join(lines)

    total_cost_usd = None
    total_cost_match = re.search(r"Total Cost:\s*\$([0-9]+(?:\.[0-9]+)?)", normalized_text)
    if total_cost_match:
        total_cost_usd = _to_number(total_cost_match.group(1))

    session_id = None
    # Handles both:
    #   "Iteration 1: <session>    cost: $..."
    #   "<session>    cost: $..."
    session_cost_patterns = [
        r"Iteration\s+\d+:\s*([A-Za-z0-9._:-]+)\s+cost:\s*\$([0-9]+(?:\.[0-9]+)?)",
        r"^\s*([A-Za-z0-9._:-]+)\s+cost:\s*\$([0-9]+(?:\.[0-9]+)?)\s*$",
    ]

    per_session_cost = None
    for line in lines:
        for pattern in session_cost_patterns:
            m = re.search(pattern, line)
            if not m:
                continue
            candidate = m.group(1).strip()
            if _looks_like_clock_token(candidate):
                continue
            session_id = candidate
            per_session_cost = _to_number(m.group(2))

    # Fallback for "Session ID:" section without inline cost.
    if not session_id:
        for idx, line in enumerate(lines):
            if "Session ID" not in line:
                continue
            for nxt in lines[idx + 1: idx + 4]:
                token = nxt.strip()
                if not token or token.startswith("🔑") or token.startswith("-"):
                    continue
                candidate = token.split()[0]
                if _looks_like_clock_token(candidate):
                    continue
                session_id = candidate
                break
            if session_id:
                break

    result_text = None
    for idx, line in enumerate(lines):
        if "📄 Result:" in line or line.strip() == "Result:":
            collected = []
            for nxt in lines[idx + 1:]:
                if (
                    "📊 Statistics:" in nxt
                    or nxt.strip() == "Statistics:"
                    or "🔑 Session ID" in nxt
                ):
                    break
                if nxt.strip():
                    collected.append(nxt)
            if collected:
                result_text = "\n".join(collected).strip()
            break

    if total_cost_usd is None and per_session_cost is not None:
        total_cost_usd = per_session_cost

    if session_id is None and total_cost_usd is None and not result_text:
        return None

    is_error = ("Execution failed" in normalized_text) or ("❌" in normalized_text)

    payload = {
        "type": "result",
        "subtype": "error" if is_error else "success",
        "is_error": is_error,
    }
    if session_id:
        payload["session_id"] = session_id
    if total_cost_usd is not None:
        payload["total_cost_usd"] = total_cost_usd
    if result_text:
        payload["result"] = result_text

    return payload


def _parse_result_from_text(text):
    """Parse latest yylo result event from text output.

    Supports:
    - single-line JSON event logs
    - pretty/multi-line JSON blocks
    - fallback CLI summary lines (session id / cost / result text)
    """
    if not text:
        return None

    # Fast path for compact line-based JSON logs.
    parsed = _parse_result_from_lines(text.splitlines())
    if parsed is not None:
        return parsed

    ansi_re = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')
    clean = ansi_re.sub('', text)

    decoder = json.JSONDecoder()
    idx = 0
    best = None

    while True:
        brace = clean.find('{', idx)
        if brace == -1:
            break

        try:
            obj, end = decoder.raw_decode(clean, brace)
        except json.JSONDecodeError:
            idx = brace + 1
            continue

        if isinstance(obj, dict) and obj.get("type") == "result":
            best = obj
        idx = max(end, brace + 1)

    if best is not None:
        return best

    return _parse_result_from_cli_summary_text(clean)


def _parse_result_from_log(task_log_path):
    """Parse the yylo result event from a task log file."""
    try:
        text = Path(task_log_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    return _parse_result_from_text(text)


def _extract_response(backend_result, file_format):
    """Extract the clean response from a backend result.

    Tries fenced code blocks first, falls back to raw result.
    """
    if not isinstance(backend_result, dict):
        return None, None

    raw_result = backend_result.get("result")
    if not raw_result:
        return None, None

    if file_format:
        content, _ = _extract_from_fenced_block(raw_result, file_format)
        if content is not None:
            return content, None

    any_block = re.compile(r"```\w*\s*\n(.*?)```", re.DOTALL)
    matches = list(any_block.finditer(raw_result))
    if matches:
        return matches[-1].group(1).rstrip(), None

    error_msg = (f"No ```{file_format} code block found (used raw result)"
                 if file_format else None)
    return raw_result, error_msg


def _to_number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _extract_total_cost_usd(payload):
    """Extract total USD cost from structured backend payload variants."""
    if not isinstance(payload, dict):
        return None

    for key in ("total_cost_usd", "totalCostUsd", "totalCostUSD"):
        direct = _to_number(payload.get(key))
        if direct is not None:
            return direct

    usage = payload.get("usage")
    if isinstance(usage, dict):
        usage_cost = usage.get("cost")
        if isinstance(usage_cost, dict):
            nested = _to_number(usage_cost.get("total"))
            if nested is not None:
                return nested

    return None


def _extract_session_id(payload):
    """Extract session id from structured backend payload variants."""
    if not isinstance(payload, dict):
        return None

    direct = payload.get("session_id")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    camel = payload.get("sessionId")
    if isinstance(camel, str) and camel.strip():
        return camel.strip()

    nested = payload.get("sub_agent_response")
    if isinstance(nested, dict):
        sub_id = nested.get("session_id")
        if isinstance(sub_id, str) and sub_id.strip():
            return sub_id.strip()

    return None


def _write_task_output(output_dir, task_id, exit_code, wall_time, start_time,
                       end_time, worker_id=-1,
                       extracted_response=None, extraction_error=None,
                       backend_result=None, file_format=""):
    """Write per-task structured JSON output."""
    output_path = _task_output_path(output_dir, task_id)

    if extracted_response is None and backend_result is not None:
        extracted_response, extraction_error = _extract_response(
            backend_result, file_format,
        )

    session_id = _extract_session_id(backend_result)
    total_cost_usd = _extract_total_cost_usd(backend_result)

    task_output = {
        "task_id": task_id,
        "session_id": session_id,
        "total_cost_usd": total_cost_usd,
        "exit_code": exit_code,
        "wall_time_seconds": round(wall_time, 2),
        "start_time": start_time,
        "end_time": end_time,
        "worker_id": worker_id,
        "backend_result": backend_result,
        "extracted_response": extracted_response,
        "extraction_error": extraction_error,
    }

    output_path.write_text(
        json.dumps(task_output, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return task_output


def _extract_from_fenced_block(log_text, file_format):
    """Extract content from the last fenced code block matching the format."""
    pattern = re.compile(
        r"```" + re.escape(file_format) + r"\s*\n(.*?)```",
        re.DOTALL,
    )
    matches = list(pattern.finditer(log_text))
    if not matches:
        return None, f"No ```{file_format} code block found in task output"
    content = matches[-1].group(1)
    content = content.rstrip()
    return content, None


def _extract_strict_output(task_id, task_log_path, output_dir, file_format, exit_code):
    """Run strict extraction on a completed task's log."""
    try:
        log_text = Path(task_log_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        _update_task_json(output_dir, task_id, None, f"Could not read task log: {e}")
        return 1

    content, error = _extract_from_fenced_block(log_text, file_format)

    if error:
        _update_task_json(output_dir, task_id, None, error)
        return 1

    extracted_path = output_dir / f"{task_id}.{file_format}"
    extracted_path.write_text(content, encoding="utf-8")
    _update_task_json(output_dir, task_id, content, None)

    return exit_code


def _update_task_json(output_dir, task_id, extracted_response, extraction_error):
    """Update an existing per-task JSON file with extraction results."""
    output_path = _task_output_path(output_dir, task_id)
    if not output_path.exists():
        return
    try:
        data = json.loads(output_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    data["extracted_response"] = extracted_response
    data["extraction_error"] = extraction_error
    if extraction_error:
        data["exit_code"] = 1
    output_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _write_aggregation(output_dir, task_outputs, wall_time, parallelism,
                       mode="headless", session_name=None, file_format="",
                       require_extraction=True):
    """Build and write the aggregation file."""
    succeeded = sum(1 for t in task_outputs.values() if t["exit_code"] == 0)
    failed = sum(1 for t in task_outputs.values() if t["exit_code"] != 0)

    merged_parts = []
    failed_ids = []
    failed_sessions = {}
    session_rows = []
    total_cost_usd = 0.0
    for tid in sorted(task_outputs.keys()):
        t = task_outputs[tid]
        er = t.get("extracted_response")
        br = t.get("backend_result") or {}
        backend_ok = isinstance(br, dict) and br.get("exit_code", -1) == 0
        sid = t.get("session_id")
        task_cost = _to_number(t.get("total_cost_usd"))
        if task_cost is None:
            task_cost = _extract_total_cost_usd(br)
        if task_cost is not None:
            total_cost_usd += task_cost

        session_rows.append({
            "task_id": tid,
            "session_id": sid,
            "total_cost_usd": task_cost,
            "exit_code": t.get("exit_code"),
        })

        if er and (t.get("exit_code") == 0 or backend_ok):
            if file_format == "csv" and merged_parts:
                lines = er.split("\n")
                if len(lines) > 1:
                    merged_parts.append("\n".join(lines[1:]))
            else:
                merged_parts.append(er)
        elif require_extraction or t.get("exit_code") != 0:
            failed_ids.append(tid)
            if sid:
                failed_sessions[tid] = sid

    separator = "\n" if file_format in ("csv", "tsv") else "\n\n"
    merged_extracted = separator.join(merged_parts) if merged_parts else None

    aggregation = {
        "meta": {
            "created_at": datetime.now().isoformat(),
            "run_id": _run_id,
            "total_tasks": len(task_outputs),
            "succeeded": succeeded,
            "failed": failed,
            "wall_time_seconds": round(wall_time, 2),
            "parallelism": parallelism,
            "mode": mode,
            "session_name": session_name,
            "total_cost_usd": round(total_cost_usd, 10),
        },
        "session_summary": {
            "total_cost_usd": round(total_cost_usd, 10),
            "tasks": session_rows,
        },
        "merged_extracted": merged_extracted,
        "tasks": task_outputs,
    }

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    agg_path = output_dir / f"aggregation_{timestamp}.json"
    agg_path.write_text(
        json.dumps(aggregation, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    merged_path = None
    if merged_extracted and file_format:
        merged_path = output_dir / f"merged_{timestamp}.{file_format}"
        merged_path.write_text(merged_extracted, encoding="utf-8")

    return {
        "agg_path": str(agg_path),
        "merged_path": str(merged_path) if merged_path else None,
        "failed_ids": failed_ids,
        "failed_sessions": failed_sessions,
        "error_count": len(failed_ids),
        "total_cost_usd": round(total_cost_usd, 10),
        "error_label": "chunks failed extraction" if require_extraction else "tasks failed",
    }


def _format_output_summary(agg_result):
    """Format output paths and errors into a multi-line summary string."""
    lines = []
    lines.append("=" * 60)
    lines.append("OUTPUT FILES")
    lines.append("=" * 60)
    lines.append(f"  Run ID:       {_run_id}")
    lines.append(f"  Aggregation:  {agg_result['agg_path']}")
    if agg_result["merged_path"]:
        lines.append(f"  Merged file:  {agg_result['merged_path']}")
    total_cost_usd = agg_result.get("total_cost_usd")
    if total_cost_usd is not None:
        lines.append(f"  Total cost:   ${total_cost_usd:.6f} USD")
    if agg_result["error_count"] > 0:
        lines.append(f"  Errors:       {agg_result['error_count']} {agg_result.get('error_label', 'chunks failed extraction')}")
        lines.append(f"  Failed IDs:   {', '.join(agg_result['failed_ids'])}")
        failed_sessions = agg_result.get("failed_sessions", {})
        if failed_sessions:
            lines.append("  Sessions to investigate:")
            for tid, sid in failed_sessions.items():
                lines.append(f"    {tid}: {sid}")
        no_session = [tid for tid in agg_result["failed_ids"] if tid not in failed_sessions]
        if no_session:
            lines.append(f"  No session ID: {', '.join(no_session)}")
    else:
        lines.append("  Errors:       0")
    lines.append("=" * 60)
    return "\n".join(lines)


def _print_output_summary(agg_result):
    """Print output summary to combined log."""
    for line in _format_output_summary(agg_result).splitlines():
        log_combined(line)


# ---------------------------------------------------------------------------
# Auto-naming
# ---------------------------------------------------------------------------

def _iter_pid_files():
    """Yield current + legacy PID marker files for running-session discovery."""
    yield from _log_base.glob("*/*/orchestrator_pid_*")
    state_root = _session_state_root()
    if state_root.exists():
        yield from state_root.glob("orchestrator_pid_*")
    yield from _log_base.glob(".orchestrator_pid_*")


def _next_batch_name():
    """Find the next available batch-N name."""
    existing = set()
    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True,
        )
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if line.startswith("pc-batch-"):
                try:
                    n = int(line[len("pc-batch-"):])
                    existing.add(n)
                except ValueError:
                    pass
    except Exception:
        pass
    try:
        for f in _iter_pid_files():
            if f.name.startswith("orchestrator_pid_batch-"):
                suffix = f.name[len("orchestrator_pid_batch-"):]
            elif f.name.startswith(".orchestrator_pid_batch-"):
                suffix = f.name[len(".orchestrator_pid_batch-"):]
            else:
                continue
            try:
                existing.add(int(suffix))
            except ValueError:
                pass
    except Exception:
        pass
    counter = 1
    while counter in existing:
        counter += 1
    return f"batch-{counter}"


# ---------------------------------------------------------------------------
# Stop commands
# ---------------------------------------------------------------------------

def _list_running_sessions():
    """Return list of (name, pid) tuples for running sessions."""
    sessions = []
    seen_names = set()
    try:
        for f in _iter_pid_files():
            if f.name.startswith("orchestrator_pid_"):
                name = f.name[len("orchestrator_pid_"):]
            elif f.name.startswith(".orchestrator_pid_"):
                name = f.name[len(".orchestrator_pid_"):]
            else:
                continue
            if name in seen_names:
                continue
            try:
                pid = int(f.read_text().strip())
                os.kill(pid, 0)
                sessions.append((name, pid))
                seen_names.add(name)
            except (ValueError, ProcessLookupError, PermissionError):
                pass
    except Exception:
        pass
    return sessions


def _stop_session(name):
    """Stop a single session by name."""
    stopped = False
    tmux_session = _session_name_to_tmux(name)

    pid_paths = _pid_files_for_name(name)
    for pid_path in pid_paths:
        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            print(f"  Sent SIGTERM to orchestrator (PID {pid})")
            stopped = True
        except (ValueError, ProcessLookupError, PermissionError):
            pass

    result = subprocess.run(
        ["tmux", "kill-session", "-t", tmux_session],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(f"  Killed tmux session '{tmux_session}'")
        stopped = True

    for pid_path in pid_paths:
        try:
            pid_path.unlink(missing_ok=True)
        except OSError:
            pass
        run_dir = pid_path.parent
        if run_dir not in (_session_state_root(), _log_base):
            for state_path in [_dashboard_file(name, run_dir=run_dir), _pause_file(name, run_dir=run_dir)]:
                try:
                    state_path.unlink(missing_ok=True)
                except OSError:
                    pass

    for f in _legacy_session_state_files(name):
        try:
            f.unlink(missing_ok=True)
        except OSError:
            pass

    tmp = _tmp_dir(name)
    if tmp.exists():
        shutil.rmtree(str(tmp), ignore_errors=True)

    # Backward-compat cleanup for legacy path style.
    legacy_tmp = _log_base / f".tmp_{name}"
    if legacy_tmp.exists():
        shutil.rmtree(str(legacy_tmp), ignore_errors=True)

    return stopped


def run_stop(args):
    """Handle --stop command."""
    if args.name:
        name = args.name
        print(f"Stopping session '{name}'...")
        if _stop_session(name):
            print(f"Session '{name}' stopped.")
        else:
            print(f"No running session found with name '{name}'.")
        return

    sessions = _list_running_sessions()
    if len(sessions) == 0:
        print("No running sessions found.")
        return
    if len(sessions) == 1:
        name = sessions[0][0]
        print(f"Stopping session '{name}'...")
        _stop_session(name)
        print(f"Session '{name}' stopped.")
        return

    print(f"Multiple sessions running ({len(sessions)}). Specify --name:")
    for name, pid in sessions:
        print(f"  --name {name}  (PID {pid}, tmux: pc-{name})")
    sys.exit(1)


def run_stop_all():
    """Handle --stop-all command."""
    sessions = _list_running_sessions()
    session_names = set(s[0] for s in sessions)
    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True,
        )
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if line.startswith("pc-"):
                name = line[3:]
                session_names.add(name)
    except Exception:
        pass

    if not session_names:
        print("No running sessions found.")
        return

    print(f"Stopping {len(session_names)} session(s)...")
    for name in sorted(session_names):
        print(f"\n  [{name}]")
        _stop_session(name)
    print(f"\nAll sessions stopped.")


# ---------------------------------------------------------------------------
# CLI parsing
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Run yylo tasks in parallel with queue management and output extraction",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
        Command file schema v1:
          schema_version: 1
          name: workflow-batch
          parallel: 2  # default only; explicit CLI --parallel wins
          cwd: .
          env:
            EXAMPLE: value
          commands:
            - id: workflow-a
              command:
                - ./.juno_task/scripts/workflow_runner.sh
                - --workflow
                - .juno_task/workflows/a.yaml
            - id: smoke
              command: |
                cd yylo && npm test -- src/utils/__tests__/script-installer.test.ts
              timeout_seconds: 300

        Command entries require unique ids matching [A-Za-z0-9_.-]+ and a command.
        command strings are shell commands for the raw-command scheduler; command lists are direct argv.

        Examples:
          parallel_runner.sh --init-commands-example .juno_task/commands/workflows.yaml
          parallel_runner.sh --lint-commands-file .juno_task/commands/workflows.yaml
          parallel_runner.sh --commands-file .juno_task/commands/workflows.yaml --parallel 3
        """))

    # --- Input modes ---
    input_group = parser.add_argument_group("input modes (exactly one required unless --stop/--stop-all)")
    input_group.add_argument(
        "--kanban", nargs="+",
        help="Kanban task IDs (comma/space/quoted). {{task_id}} = {{item}} = the ID.",
    )
    input_group.add_argument(
        "--kanban-filter", type=str, default=None,
        help="Filter string passed to kanban.sh list. Internally runs "
             "kanban.sh list {filters} -f json --raw and extracts IDs.",
    )
    input_group.add_argument(
        "--items", nargs="+",
        help="Generic item list (comma/space/quoted). Auto-generates item-NNN IDs.",
    )
    input_group.add_argument(
        "--items-file", type=str, default=None,
        help="Path to file (JSONL, CSV, TSV, XLSX). Format auto-detected by extension.",
    )
    input_group.add_argument(
        "--commands-file", type=str, default=None,
        help="Path to command YAML/JSON file, or '-' for stdin. Validates schema v1 and fans out commands by id.",
    )

    command_group = parser.add_argument_group("command file utilities")
    command_group.add_argument(
        "--lint-commands-file", type=str, default=None,
        help="Validate a command YAML/JSON file without executing it. Use '-' for stdin.",
    )
    command_group.add_argument(
        "--init-commands-example", type=str, default=None,
        help="Write a boilerplate command YAML file that runs multiple workflows/commands in parallel.",
    )
    command_group.add_argument(
        "--force", action="store_true", default=False,
        help="Allow --init-commands-example to overwrite an existing file.",
    )

    # --- File options ---
    file_group = parser.add_argument_group("file options (for --items-file)")
    file_group.add_argument(
        "--format", type=str, default=None, choices=["jsonl", "csv", "tsv", "xlsx"],
        help="Force file format (default: auto-detect by extension).",
    )
    file_group.add_argument(
        "--no-header", action="store_true", default=False,
        help="CSV/TSV/XLSX: treat first row as data, not column headers.",
    )
    file_group.add_argument(
        "--chunk-size", type=int, default=1,
        help="Records per item (default: 1). >1 groups records into a JSON array.",
    )
    file_group.add_argument(
        "--start", type=int, default=None,
        help="First record to process, 1-indexed after header (default: 1).",
    )
    file_group.add_argument(
        "--end", type=int, default=None,
        help="Last record to process, inclusive (default: end of file).",
    )

    # --- Output extraction ---
    extract_group = parser.add_argument_group("output extraction")
    extract_group.add_argument(
        "--file-format", type=str, default=None,
        help="Expected output format (e.g., json, csv, md). Sets {{file_format}} placeholder.",
    )
    extract_group.add_argument(
        "--strict", action="store_true", default=False,
        help="Extract response from fenced code block. Requires --file-format.",
    )

    # --- Execution options ---
    parser.add_argument(
        "--parallel", type=int, default=None,
        help="Max concurrent subprocesses (default: 3)",
    )
    parser.add_argument(
        "-s", "--service", type=str, default=None, choices=["claude", "codex", "pi"],
        help="Backend service (default: claude). Env: JUNO_SERVICE.",
    )
    parser.add_argument(
        "-m", "--model", type=str, default=None,
        help="Model override. Env: JUNO_MODEL.",
    )
    parser.add_argument(
        "--env", nargs="+", default=None,
        help="Environment overrides. KEY=VALUE or .env file path.",
    )
    parser.add_argument(
        "--prompt-file", type=str, default=None,
        help="Prompt template file. Loaded once at startup. Placeholders: {{task_id}}, {{item}}, {{file_format}}.",
    )
    parser.add_argument(
        "--prompt", type=str, default=None,
        help="Inline prompt template content. Use '-' to read from stdin. Placeholders: {{task_id}}, {{item}}, {{file_format}}.",
    )
    parser.add_argument(
        "--subagent-args", action="append", default=None,
        help="Extra raw args appended to each yylo invocation. Repeatable; values are shell-split.",
    )
    parser.add_argument(
        "--tmux", nargs="?", const="windows", default=None, choices=["windows", "panes", "tabs"],
        help="Run in tmux mode. 'windows' (default), 'panes', or 'tabs' (one dedicated window/tab per task).",
    )
    parser.add_argument(
        "--no-attach", action="store_true", default=False,
        help="Start tmux workers in the background without attaching; print attach/follow/wait/stop commands.",
    )
    parser.add_argument(
        "--tmux-handoff", action="store_true", default=False,
        help="Tmux-only mode that dedicates one worker pane/window per task and never reuses completed workers.",
    )
    parser.add_argument(
        "--max-panes-per-session", type=int, default=None,
        help="With --tmux-handoff, split large task lists into tmux sessions capped at N task panes/windows each.",
    )
    parser.add_argument(
        "--name", type=str, default=None,
        help="Session name (default: auto-generated batch-N). Tmux session = pc-{name}.",
    )
    parser.add_argument(
        "--output-dir", type=str, default=None,
        help="Structured output directory. Default: /tmp/yylo-sessions/{date}/{run_id}.",
    )
    parser.add_argument(
        "--stop", action="store_true", default=False,
        help="Stop a session. Uses --name if provided, otherwise auto-detects.",
    )
    parser.add_argument(
        "--stop-all", action="store_true", default=False,
        help="Stop ALL running sessions.",
    )
    normalized_argv = _normalize_subagent_args_argv(sys.argv[1:])
    args = parser.parse_args(normalized_argv)

    if args.init_commands_example:
        try:
            target = _init_commands_example(args.init_commands_example, args.force)
        except CommandFileError as exc:
            parser.error(str(exc))
        print(f"Wrote command example: {target}")
        sys.exit(0)

    if args.lint_commands_file:
        try:
            loaded = _load_commands_file(args.lint_commands_file, os.getcwd())
        except CommandFileError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(2)
        print(f"OK: {len(loaded['commands'])} command(s) valid in {args.lint_commands_file}")
        sys.exit(0)

    # Handle stop commands first
    if args.stop_all:
        return args
    if args.stop:
        return args

    if args.prompt_file and args.prompt:
        parser.error("Use either --prompt-file or --prompt, not both")

    if args.commands_file and args.kanban_filter:
        parser.error("Cannot use --commands-file together with --kanban-filter")

    if args.commands_file:
        incompatible = []
        if args.prompt_file:
            incompatible.append("--prompt-file")
        if args.prompt is not None:
            incompatible.append("--prompt")
        if args.service:
            incompatible.append("--service")
        if args.model:
            incompatible.append("--model")
        if args.subagent_args:
            incompatible.append("--subagent-args")
        if args.strict:
            incompatible.append("--strict")
        if args.file_format:
            incompatible.append("--file-format")
        if incompatible:
            parser.error("--commands-file runs raw commands and cannot be combined with " + ", ".join(incompatible))
        try:
            loaded_commands = _load_commands_file(args.commands_file, os.getcwd())
        except CommandFileError as exc:
            parser.error(str(exc))
        args.command_file_metadata = loaded_commands
        args.command_specs = loaded_commands["commands"]
        if args.parallel is None and loaded_commands.get("parallel"):
            args.parallel = loaded_commands["parallel"]

    if args.parallel is None:
        args.parallel = 3

    # Resolve --kanban-filter -> --kanban
    if args.kanban_filter:
        if args.kanban:
            parser.error("Cannot use --kanban-filter together with --kanban")
        kanban_script = SCRIPT_DIR / "kanban.sh"
        if not kanban_script.exists():
            parser.error(f"Kanban script not found: {kanban_script}")
        filter_args = shlex.split(args.kanban_filter)
        cmd = [str(kanban_script), "list"] + filter_args + ["-f", "json", "--raw"]
        print(f"Running kanban filter: {' '.join(cmd)}", file=sys.stderr)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            parser.error(f"kanban.sh list failed (exit {result.returncode}):\n{result.stderr.strip()}")
        first_line = result.stdout.strip().split("\n")[0] if result.stdout.strip() else "[]"
        try:
            tasks = json.loads(first_line)
        except json.JSONDecodeError as e:
            parser.error(f"Failed to parse kanban output as JSON: {e}\nOutput: {first_line[:200]}")
        if not isinstance(tasks, list):
            parser.error(f"Expected JSON array from kanban output, got {type(tasks).__name__}")
        ids = [t["id"] for t in tasks if isinstance(t, dict) and "id" in t]
        if not ids:
            parser.error("kanban-filter returned 0 tasks. Check your filters.")
        print(f"kanban-filter resolved {len(ids)} task(s): {', '.join(ids[:10])}"
              f"{'...' if len(ids) > 10 else ''}", file=sys.stderr)
        args.kanban = ids

    # Auto-infer --file-format from --items-file extension
    if not args.file_format and args.items_file:
        ext = Path(args.items_file).suffix.lstrip(".").lower()
        if ext in ("csv", "tsv", "json", "jsonl", "ndjson", "md", "txt", "xlsx"):
            infer_map = {"xlsx": "csv", "ndjson": "jsonl"}
            args.file_format = infer_map.get(ext, ext)
            print(f"Auto-inferred --file-format={args.file_format} from input file extension",
                  file=sys.stderr)

    if args.strict and not args.file_format:
        parser.error("--strict requires --file-format to be set")

    global _env_overrides
    _env_overrides = _resolve_env_overrides(args.env)

    args.subagent_args_list = _resolve_subagent_args(args.subagent_args)

    if args.tmux == "tabs":
        args.tmux_handoff = True

    if args.max_panes_per_session is not None:
        if args.max_panes_per_session < 1:
            parser.error("--max-panes-per-session must be a positive integer")
        if not args.tmux_handoff:
            parser.error("--max-panes-per-session requires --tmux-handoff")

    if args.tmux_handoff and not args.tmux:
        parser.error("--tmux-handoff requires --tmux because handoff preserves tmux panes/windows for later attachment")

    if args.no_attach and not args.tmux:
        parser.error("--no-attach requires --tmux")
    if args.tmux and not args.no_attach and not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(
            "WARNING: tmux attach was requested from a non-TTY context and may fail with "
            "'open terminal failed: not a terminal'. Use --no-attach for an explicit background launch.",
            file=sys.stderr,
        )

    live_in_tmux = args.tmux and _contains_live_subagent_flag(args.subagent_args_list)
    if live_in_tmux and args.parallel > 1 and args.tmux != "tabs":
        parser.error(
            "--tmux with --subagent-args '--live' is interactive and only supports --parallel 1. "
            "Set --parallel 1, remove --live, or run headless mode."
        )
    if live_in_tmux:
        print(
            "WARNING: --tmux with --subagent-args '--live' enables interactive Pi TUI in the worker pane; "
            "batch progress resumes after you exit that live session.",
            file=sys.stderr,
        )

    # Flatten --kanban
    if args.kanban:
        flat = []
        for val in args.kanban:
            for part in val.replace(",", " ").split():
                part = part.strip()
                if part:
                    flat.append(part)
        args.kanban = flat

    task_ids = _resolve_input(args)
    args.kanban = task_ids

    if args.tmux == "tabs":
        args.parallel = len(task_ids)

    if live_in_tmux and args.parallel > 1:
        parser.error(
            "--tmux with --subagent-args '--live' is interactive and only supports --parallel 1. "
            "Set --parallel 1, remove --live, or run headless mode."
        )

    if args.tmux_handoff and args.max_panes_per_session is None and len(task_ids) > args.parallel:
        parser.error(
            f"--tmux-handoff requires one dedicated worker per task for this increment: "
            f"got {len(task_ids)} task(s) with --parallel {args.parallel}. "
            "Increase --parallel to at least the number of tasks, add --max-panes-per-session, or omit --tmux-handoff."
        )

    if args.tmux_handoff and args.max_panes_per_session is not None and len(task_ids) <= args.max_panes_per_session:
        args.parallel = max(args.parallel, len(task_ids))

    return args


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def log_combined(msg, task_id=None):
    """Write a timestamped line to both stdout and the combined log (thread-safe)."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    plain_line = f"[{timestamp}] {msg}"
    if task_id:
        colored_line = f"[{timestamp}] {_colored_tag(task_id)} {msg}"
    else:
        colored_line = plain_line
    with _log_lock:
        print(colored_line, flush=True)
        with open(COMBINED_LOG, "a") as f:
            f.write(plain_line + "\n")


def stream_to_log(pipe, task_id, task_log_path):
    """Read lines from a subprocess pipe and write to both per-task and combined logs."""
    tag_colored = _colored_tag(task_id)
    with open(task_log_path, "a") as task_log:
        for raw_line in iter(pipe.readline, b""):
            decoded = raw_line.decode("utf-8", errors="replace").rstrip("\n")
            timestamp = datetime.now().strftime("%H:%M:%S")
            plain_entry = f"[{timestamp}] [{task_id}] {decoded}"
            colored_entry = f"[{timestamp}] {tag_colored} {decoded}"

            task_log.write(plain_entry + "\n")
            task_log.flush()

            with _log_lock:
                with open(COMBINED_LOG, "a") as f:
                    f.write(colored_entry + "\n")


def format_duration(seconds):
    """Format seconds into human-readable duration."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    secs = seconds % 60
    if minutes < 60:
        return f"{minutes}m {secs:.0f}s"
    hours = int(minutes // 60)
    mins = minutes % 60
    return f"{hours}h {mins}m {secs:.0f}s"


def resolve_prompt_source(args, pwd):
    """Resolve prompt source metadata and return (source_label, template_text)."""
    if args.prompt_file:
        prompt_path = Path(args.prompt_file)
        if not prompt_path.is_absolute():
            prompt_path = Path(pwd) / prompt_path
        if not prompt_path.exists():
            print(f"ERROR: Prompt file not found: {prompt_path}", file=sys.stderr)
            sys.exit(2)
        try:
            template = prompt_path.read_text(encoding="utf-8")
        except Exception as exc:
            print(f"ERROR: Could not read prompt file at startup: {exc}", file=sys.stderr)
            sys.exit(2)
        return str(prompt_path), template

    if args.prompt is not None:
        if args.prompt == "-":
            if sys.stdin.isatty():
                print("ERROR: --prompt - requires redirected stdin (pipe/heredoc)", file=sys.stderr)
                sys.exit(2)
            template = sys.stdin.read()
            if not template.strip():
                print("ERROR: --prompt - received empty stdin content", file=sys.stderr)
                sys.exit(2)
            return "stdin", template
        return "inline", args.prompt

    return None, None


def render_prompt(task_id, prompt_template, file_format=""):
    """Render prompt template placeholders for a task."""
    if not prompt_template:
        return ""
    rendered = prompt_template.replace("{{task_id}}", task_id)
    rendered = rendered.replace("{{item}}", _item_map.get(task_id, task_id))
    rendered = rendered.replace("{{file_format}}", file_format)
    return "\n\n---\n\n" + rendered


def prepare_prompt_files(task_ids, prompt_template, prompt_dir, file_format=""):
    """Materialize all per-task prompt files at startup for long-running resilience."""
    prompt_paths = {}
    prompt_dir.mkdir(parents=True, exist_ok=True)
    for task_id in task_ids:
        prompt_path = prompt_dir / f"prompt_{task_id}.txt"
        prompt_path.write_text(render_prompt(task_id, prompt_template, file_format), encoding="utf-8")
        prompt_paths[task_id] = str(prompt_path)
    return prompt_paths


def print_summary(task_ids, results, task_times, wall_elapsed, total_tasks):
    """Print final results and stats summary."""
    log_combined("")
    log_combined("=" * 60)
    log_combined("RESULTS")
    log_combined("=" * 60)

    ok = 0
    failed = 0
    for task_id in task_ids:
        rc = results.get(task_id, -1)
        elapsed = task_times.get(task_id, 0)
        status_str = "OK" if rc == 0 else f"FAILED (exit {rc})"
        log_combined(f"  {status_str}  [{format_duration(elapsed)}]", task_id)
        if rc == 0:
            ok += 1
        else:
            failed += 1

    log_combined("-" * 60)
    log_combined("STATS")
    log_combined(f"  Total tasks:    {total_tasks}")
    log_combined(f"  Succeeded:      {ok}")
    log_combined(f"  Failed:         {failed}")
    log_combined(f"  Wall time:      {format_duration(wall_elapsed)}")
    if task_times:
        avg = sum(task_times.values()) / len(task_times)
        fastest = min(task_times.values())
        slowest = max(task_times.values())
        log_combined(f"  Avg per task:   {format_duration(avg)}")
        log_combined(f"  Fastest task:   {format_duration(fastest)}")
        log_combined(f"  Slowest task:   {format_duration(slowest)}")
    log_combined(f"  Run ID:         {_run_id}")
    log_combined(f"  Per-task logs:  {LOG_DIR}/task_<TASK_ID>.log")
    log_combined("=" * 60)

    return failed


# ---------------------------------------------------------------------------
# Headless mode
# ---------------------------------------------------------------------------

def run_task(task_id, semaphore, pwd, prompt_path=None, output_dir=None,
             service="claude", model=":sonnet", file_format="", strict=False,
             subagent_args=None):
    """Run a single yylo subprocess (called from its own thread)."""
    global _completed_count

    semaphore.acquire()
    try:
        log_combined(f"Starting yylo (thread {threading.current_thread().name})", task_id)
        start = time.monotonic()
        start_iso = datetime.now().isoformat()

        task_log_path = LOG_DIR / f"task_{task_id}.log"

        if prompt_path and not Path(prompt_path).exists():
            log_combined("Prompt file missing at runtime; task cannot start", task_id)
            return task_id, 1

        env = _build_process_env({"ASSIGNED_TASK_ID": task_id}, task_id=task_id)

        cmd = [
            "yylo",
            "-b", "shell",
            "-s", service,
            "-m", model,
            "-i", "1",
            "-v",
            "--no-hooks",
        ]
        if subagent_args:
            cmd.extend(subagent_args)
        if prompt_path:
            cmd.extend(["-f", str(prompt_path)])

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=pwd,
            bufsize=0,
            env=env,
        )

        reader = threading.Thread(
            target=stream_to_log,
            args=(proc.stdout, task_id, task_log_path),
            daemon=True,
        )
        reader.start()

        proc.wait()
        reader.join()

        elapsed = time.monotonic() - start

        with _completed_lock:
            _completed_count += 1
            done = _completed_count
            remaining = _total_tasks - done

        with _task_times_lock:
            _task_times[task_id] = elapsed

        actual_exit_code = proc.returncode
        backend_result = _parse_result_from_log(task_log_path) if output_dir else None

        if output_dir:
            _write_task_output(
                output_dir, task_id, actual_exit_code, elapsed,
                start_iso, datetime.now().isoformat(),
                backend_result=backend_result, file_format=file_format,
            )

        if strict and file_format and output_dir:
            actual_exit_code = _extract_strict_output(
                task_id, task_log_path, output_dir, file_format, actual_exit_code,
            )

        status = "OK" if actual_exit_code == 0 else f"FAILED (exit {actual_exit_code})"
        log_combined(
            f"Finished - {status} ({format_duration(elapsed)}) "
            f"| Progress: {done}/{_total_tasks} done, {remaining} remaining",
            task_id,
        )
        return task_id, actual_exit_code

    finally:
        semaphore.release()


def run_raw_command_task(task_id, semaphore, pwd, output_dir=None):
    """Run a single raw command from --commands-file (called from its own thread)."""
    global _completed_count

    semaphore.acquire()
    try:
        spec = _command_spec_for(task_id)
        command, shell, cwd, env = _command_popen_kwargs(spec, pwd, task_id)
        timeout_seconds = spec.get("timeout_seconds")
        task_log_path = LOG_DIR / f"task_{task_id}.log"

        log_combined(
            f"Starting raw command (thread {threading.current_thread().name}, "
            f"cwd={cwd}, shell={shell}): {_format_command_for_log(spec['command'])}",
            task_id,
        )
        start = time.monotonic()
        start_iso = datetime.now().isoformat()

        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=cwd,
            shell=shell,
            bufsize=0,
            env=env,
            preexec_fn=os.setsid if hasattr(os, "setsid") else None,
        )

        reader = threading.Thread(
            target=stream_to_log,
            args=(proc.stdout, task_id, task_log_path),
            daemon=True,
        )
        reader.start()

        timed_out = False
        try:
            proc.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            log_combined(f"TIMEOUT after {timeout_seconds}s; terminating raw command", task_id)
            try:
                if hasattr(os, "killpg"):
                    os.killpg(proc.pid, signal.SIGTERM)
                else:
                    proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                log_combined("Timeout termination grace expired; killing raw command", task_id)
                if hasattr(os, "killpg"):
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
                proc.wait()

        reader.join(timeout=5)
        elapsed = time.monotonic() - start

        with _completed_lock:
            _completed_count += 1
            done = _completed_count
            remaining = _total_tasks - done

        with _task_times_lock:
            _task_times[task_id] = elapsed

        actual_exit_code = 124 if timed_out else proc.returncode

        if output_dir:
            _write_task_output(
                output_dir, task_id, actual_exit_code, elapsed,
                start_iso, datetime.now().isoformat(), backend_result=None,
            )

        status = "OK" if actual_exit_code == 0 else f"FAILED (exit {actual_exit_code})"
        if timed_out:
            status = f"FAILED (timeout {timeout_seconds}s)"
        log_combined(
            f"Finished - {status} ({format_duration(elapsed)}) "
            f"| Progress: {done}/{_total_tasks} done, {remaining} remaining",
            task_id,
        )
        return task_id, actual_exit_code

    finally:
        semaphore.release()


def run_headless_mode(args, pwd, prompt_source_label, prompt_template,
                      output_dir, service, model, subagent_args=None):
    """Run tasks in headless mode using ThreadPoolExecutor."""
    global _total_tasks
    _total_tasks = len(args.kanban)
    wall_start = time.monotonic()

    log_combined("=" * 60)
    log_combined(f"Starting parallel task execution")
    log_combined(f"Run ID: {_run_id}")
    log_combined(f"PWD: {pwd}")
    input_label = "Commands" if _is_command_mode(args) else "Tasks"
    log_combined(f"{input_label} ({_total_tasks}): {', '.join(args.kanban)}")
    if _is_command_mode(args):
        for tid in args.kanban[:5]:
            spec = _command_spec_for(tid)
            log_combined(
                f"  {tid} -> cwd={spec.get('cwd') or pwd} shell={isinstance(spec['command'], str)} "
                f"timeout={spec.get('timeout_seconds') or '-'} command={_format_command_for_log(spec['command'])}"
            )
        if len(args.kanban) > 5:
            log_combined(f"  ... and {len(args.kanban) - 5} more command(s)")
    if any(tid.startswith("item-") for tid in args.kanban):
        preview = [f"  {tid} -> {_item_map[tid][:80]}" for tid in args.kanban[:3]]
        log_combined(f"Items preview:\n" + "\n".join(preview)
                     + (f"\n  ... and {len(args.kanban) - 3} more" if len(args.kanban) > 3 else ""))
    log_combined(f"Parallelism: {args.parallel}")
    if not _is_command_mode(args):
        log_combined(f"Service: {service} | Model: {model}")
    if subagent_args:
        log_combined(f"Subagent args: {' '.join(subagent_args)}")
    if prompt_source_label:
        source_type = "Prompt file" if prompt_source_label not in ("inline", "stdin") else "Prompt source"
        log_combined(f"{source_type}: {prompt_source_label} (materialized at startup)")
    if output_dir:
        log_combined(f"Output dir: {output_dir}")
    legend = "  ".join(f"{_color_for(tid)} {tid}" for tid in args.kanban)
    with _log_lock:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Legend: {legend}", flush=True)
        with open(COMBINED_LOG, "a") as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Legend: {', '.join(args.kanban)}\n")
    log_combined("=" * 60)

    semaphore = threading.Semaphore(args.parallel)

    file_format = getattr(args, "file_format", "") or ""
    strict = getattr(args, "strict", False)

    prompt_paths = {}
    if prompt_template is not None:
        prompt_dir = _tmp_dir(_run_id) / "prompts"
        prompt_paths = prepare_prompt_files(args.kanban, prompt_template, prompt_dir, file_format)
        log_combined(f"Prebuilt {len(prompt_paths)} prompt files in {prompt_dir}")

    with ThreadPoolExecutor(max_workers=len(args.kanban)) as pool:
        if _is_command_mode(args):
            futures = {
                pool.submit(run_raw_command_task, task_id, semaphore, pwd, output_dir): task_id
                for task_id in args.kanban
            }
        else:
            futures = {
                pool.submit(run_task, task_id, semaphore, pwd, prompt_paths.get(task_id), output_dir,
                            service, model, file_format, strict, subagent_args): task_id
                for task_id in args.kanban
            }

        results = {}
        for future in as_completed(futures):
            task_id = futures[future]
            try:
                tid, returncode = future.result()
                results[tid] = returncode
            except Exception as exc:
                log_combined(f"EXCEPTION: {exc}", task_id)
                results[task_id] = -1

    wall_elapsed = time.monotonic() - wall_start
    failed = print_summary(args.kanban, results, _task_times, wall_elapsed, _total_tasks)

    if output_dir:
        task_outputs = {}
        for tid in args.kanban:
            cap = _task_output_path(output_dir, tid)
            if cap.exists():
                try:
                    task_outputs[tid] = json.loads(cap.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    task_outputs[tid] = {"task_id": tid, "exit_code": results.get(tid, -1),
                                         "backend_result": None}
            else:
                task_outputs[tid] = {"task_id": tid, "exit_code": results.get(tid, -1),
                                     "backend_result": None}
        agg_result = _write_aggregation(
            output_dir, task_outputs, wall_elapsed, args.parallel,
            mode="headless", file_format=args.file_format,
            require_extraction=not _is_command_mode(args),
        )
        _print_output_summary(agg_result)

    shutil.rmtree(str(_tmp_dir(_run_id)), ignore_errors=True)
    exit_code = 1 if failed > 0 else 0
    _write_run_status("completed", "headless", exit_code=exit_code)
    sys.exit(exit_code)


# ---------------------------------------------------------------------------
# Tmux mode — data structures
# ---------------------------------------------------------------------------

class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class TaskState:
    task_id: str
    status: TaskStatus = TaskStatus.PENDING
    worker_id: int = -1
    start_time: float = 0.0
    end_time: float = 0.0
    exit_code: int = -1
    sentinel_id: str = ""
    start_time_iso: str = ""
    end_time_iso: str = ""
    session_id: str = ""
    total_cost_usd: float = 0.0


@dataclass
class WorkerState:
    worker_id: int
    tmux_target: str
    current_task: str = ""
    busy: bool = False
    sentinel_id: str = ""
    handoff_completed: bool = False


# ---------------------------------------------------------------------------
# Tmux mode — session creation
# ---------------------------------------------------------------------------

def tmux_run(cmd, check=True):
    """Run a tmux command, return stdout."""
    result = subprocess.run(
        ["tmux"] + cmd,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"tmux {' '.join(cmd)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _ansi256_to_tmux_color(code):
    """Convert ANSI 256-color code to tmux colour string."""
    return f"colour{code}"


def _tmux_safe_window_name(task_id, index, used_names):
    """Return a tmux-safe, unique window/tab name derived from the task ID."""
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", str(task_id)).strip("-")
    if not safe:
        safe = f"task-{index + 1}"
    safe = safe[:60]
    candidate = safe
    suffix = 2
    while candidate in used_names:
        suffix_text = f"-{suffix}"
        candidate = f"{safe[:60 - len(suffix_text)]}{suffix_text}"
        suffix += 1
    used_names.add(candidate)
    return candidate


def update_pane_border_color(worker_target, task_id):
    """Set the pane/window border color to match the task's assigned color."""
    color_code = _task_color_map.get(task_id, 7)
    tmux_color = _ansi256_to_tmux_color(color_code)
    tmux_run([
        "select-pane", "-t", worker_target,
        "-P", f"border-style=fg={tmux_color}",
    ], check=False)
    tmux_run([
        "select-pane", "-t", worker_target,
        "-T", f"{task_id}",
    ], check=False)


def create_tmux_session(session_name, mode, num_workers, pwd, task_ids=None):
    """Create tmux session with coordinator + worker windows/panes/tabs."""
    subprocess.run(
        ["tmux", "kill-session", "-t", session_name],
        capture_output=True,
    )

    workers = []

    def _setup_status_bar():
        tmux_run(["set-option", "-t", session_name, "status", "on"], check=False)
        tmux_run(["set-option", "-t", session_name, "status-style", "bg=black,fg=white"], check=False)
        tmux_run(["set-option", "-t", session_name, "status-left", f"#[fg=cyan,bold] {session_name} #[default]"], check=False)
        tmux_run(["set-option", "-t", session_name, "status-left-length", "25"], check=False)
        tmux_run(["set-option", "-t", session_name, "status-right", "Starting..."], check=False)
        tmux_run(["set-option", "-t", session_name, "status-right-length", "120"], check=False)
        tmux_run(["set-option", "-t", session_name, "pane-border-status", "top"], check=False)
        tmux_run(["set-option", "-t", session_name, "pane-border-format",
                   " #{pane_index}: #{pane_title} "], check=False)
        tmux_run(["set-option", "-t", session_name, "pane-border-indicators", "colour"], check=False)

    if mode in ("windows", "tabs"):
        tmux_run([
            "new-session", "-d", "-s", session_name,
            "-n", "coordinator", "-x", "200", "-y", "50",
        ])
        tmux_run(["set-option", "-t", session_name, "remain-on-exit", "off"])
        _setup_status_bar()

        used_window_names = {"coordinator"}
        for i in range(num_workers):
            if mode == "tabs" and task_ids and i < len(task_ids):
                name = _tmux_safe_window_name(task_ids[i], i, used_window_names)
            else:
                name = f"worker-{i}"
            tmux_run(["new-window", "-t", session_name, "-n", name])
            target = f"{session_name}:{name}"
            tmux_run(["send-keys", "-t", target, f"cd {shlex.quote(pwd)}", "Enter"])
            workers.append(WorkerState(worker_id=i, tmux_target=target))

        tmux_run(["select-window", "-t", f"{session_name}:coordinator"])
        coordinator_target = f"{session_name}:coordinator"

    else:  # panes
        tmux_run([
            "new-session", "-d", "-s", session_name,
            "-n", "main", "-x", "200", "-y", "50",
        ])
        tmux_run(["set-option", "-t", session_name, "remain-on-exit", "off"])
        # FIX-005: Increase scrollback for panes mode to prevent sentinel eviction
        tmux_run(["set-option", "-t", session_name, "history-limit", "50000"], check=False)
        _setup_status_bar()

        base = f"{session_name}:main"

        for i in range(num_workers):
            tmux_run(["split-window", "-t", base, "-v"])
            tmux_run(["select-layout", "-t", base, "tiled"])

        for i in range(num_workers):
            pane_idx = i + 1
            target = f"{base}.{pane_idx}"
            tmux_run(["send-keys", "-t", target, f"cd {shlex.quote(pwd)}", "Enter"])
            workers.append(WorkerState(worker_id=i, tmux_target=target))

        coordinator_target = f"{base}.0"

    return coordinator_target, workers


# ---------------------------------------------------------------------------
# Tmux mode — command building & dispatch
# ---------------------------------------------------------------------------

def write_runner_script(task_id, pwd, prompt_path, session_name_short,
                        output_dir=None, service="claude", model=":sonnet",
                        file_format="", subagent_args=None):
    """Write bash runner script for a task."""
    sentinel_id = uuid.uuid4().hex[:12]

    tmp = _tmp_dir(session_name_short)
    tmp.mkdir(parents=True, exist_ok=True)

    env_exports = _generate_env_exports(task_id)
    env_exports += "\nexport ASSIGNED_TASK_ID=%s" % shlex.quote(task_id)

    env_path = tmp / f"env_{task_id}.sh"
    env_path.write_text(env_exports + "\n")

    subagent_args_shell = " ".join(shlex.quote(arg) for arg in (subagent_args or []))
    prompt_arg = f" -f {shlex.quote(str(prompt_path))}" if prompt_path else ""

    runner_path = tmp / f"run_{task_id}.sh"
    runner_path.write_text(textwrap.dedent("""\
        #!/bin/bash
        source %(env_path)s
        cd %(pwd)s
        yylo -b shell -s %(service)s -m %(model)s -i 1 -v --no-hooks%(subagent_args)s%(prompt_arg)s
        echo "___DONE_%(sentinel_id)s_${?}___"
    """) % {
        "env_path": shlex.quote(str(env_path)),
        "pwd": shlex.quote(pwd),
        "prompt_arg": prompt_arg,
        "sentinel_id": sentinel_id,
        "service": shlex.quote(service),
        "model": shlex.quote(model),
        "subagent_args": f" {subagent_args_shell}" if subagent_args_shell else "",
    })

    return str(runner_path), sentinel_id


def write_raw_command_runner_script(task_id, pwd, session_name_short):
    """Write a Python runner script for a raw command task.

    The runner uses subprocess directly so YAML list commands preserve shell=False
    semantics while YAML string commands use shell=True, matching workflow_runner.sh.
    """
    sentinel_id = uuid.uuid4().hex[:12]
    spec = _command_spec_for(task_id)
    command, shell, cwd, env = _command_popen_kwargs(spec, pwd, task_id)
    timeout_seconds = spec.get("timeout_seconds")

    tmp = _tmp_dir(session_name_short)
    tmp.mkdir(parents=True, exist_ok=True)
    spec_path = tmp / f"command_{task_id}.json"
    runner_path = tmp / f"run_{task_id}.py"

    spec_path.write_text(json.dumps({
        "command": command,
        "shell": shell,
        "cwd": cwd,
        "env": env,
        "timeout_seconds": timeout_seconds,
        "sentinel_id": sentinel_id,
    }, ensure_ascii=False), encoding="utf-8")

    runner_path.write_text(textwrap.dedent("""\
        #!/usr/bin/env python3
        import json, os, signal, subprocess, sys
        from pathlib import Path

        spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        proc = subprocess.Popen(
            spec["command"],
            stdout=sys.stdout,
            stderr=subprocess.STDOUT,
            cwd=spec["cwd"],
            shell=spec["shell"],
            env=spec["env"],
            preexec_fn=os.setsid if hasattr(os, "setsid") else None,
        )
        timed_out = False
        try:
            proc.wait(timeout=spec.get("timeout_seconds"))
        except subprocess.TimeoutExpired:
            timed_out = True
            print(f"TIMEOUT after {spec.get('timeout_seconds')}s; terminating raw command", flush=True)
            try:
                if hasattr(os, "killpg"):
                    os.killpg(proc.pid, signal.SIGTERM)
                else:
                    proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                print("Timeout termination grace expired; killing raw command", flush=True)
                if hasattr(os, "killpg"):
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
                proc.wait()
        exit_code = 124 if timed_out else proc.returncode
        print(f"___DONE_{spec['sentinel_id']}_{exit_code}___", flush=True)
    """), encoding="utf-8")
    runner_path.chmod(0o755)
    return str(runner_path), str(spec_path), sentinel_id


def dispatch_task(worker, task_id, task_state, pwd, prompt_paths,
                  session_name_short, output_dir=None, service="claude",
                  model=":sonnet", file_format="", subagent_args=None,
                  prompt_template=None, command_mode=False):
    """Send a task command to a worker's tmux pane/window."""
    if command_mode:
        runner_path, command_spec_path, sentinel_id = write_raw_command_runner_script(
            task_id, pwd, session_name_short)
    else:
        prompt_path = prompt_paths.get(task_id)
        if prompt_path and not Path(prompt_path).exists():
            prompt_path_obj = Path(prompt_path)
            prompt_path_obj.parent.mkdir(parents=True, exist_ok=True)
            prompt_path_obj.write_text(render_prompt(task_id, prompt_template, file_format), encoding="utf-8")

        runner_path, sentinel_id = write_runner_script(
            task_id, pwd, prompt_path, session_name_short, output_dir,
            service, model, file_format, subagent_args)

    # FIX-003: Stop old pipe-pane explicitly before starting new one
    tmux_run(["pipe-pane", "-t", worker.tmux_target], check=False)
    time.sleep(0.1)

    task_log = str(LOG_DIR / f"task_{task_id}.log")
    helper = str(_tmp_dir(session_name_short) / "log_pipe.py")
    color_code = str(_task_color_map.get(task_id, 7))
    tmux_run([
        "pipe-pane", "-t", worker.tmux_target,
        "-o", "python3 -u %s %s %s %s %s" % (
            shlex.quote(helper),
            shlex.quote(task_id),
            shlex.quote(task_log),
            shlex.quote(str(COMBINED_LOG)),
            shlex.quote(color_code),
        ),
    ])

    update_pane_border_color(worker.tmux_target, task_id)

    if command_mode:
        tmux_run(["send-keys", "-t", worker.tmux_target, f"python3 -u {shlex.quote(runner_path)} {shlex.quote(command_spec_path)}", "Enter"])
    else:
        tmux_run(["send-keys", "-t", worker.tmux_target, f"bash {shlex.quote(runner_path)}", "Enter"])

    worker.busy = True
    worker.current_task = task_id
    worker.sentinel_id = sentinel_id
    task_state.status = TaskStatus.RUNNING
    task_state.worker_id = worker.worker_id
    task_state.start_time = time.monotonic()
    task_state.start_time_iso = datetime.now().isoformat()
    task_state.sentinel_id = sentinel_id

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tag = _colored_tag(task_id)
    with open(COMBINED_LOG, "a") as f:
        if command_mode:
            spec = _command_spec_for(task_id)
            f.write(
                f"[{timestamp}] {tag} Dispatched raw command to worker-{worker.worker_id} "
                f"(cwd={spec.get('cwd') or pwd}, shell={isinstance(spec['command'], str)}, "
                f"timeout={spec.get('timeout_seconds') or '-'})\n"
            )
        else:
            f.write(f"[{timestamp}] {tag} Dispatched to worker-{worker.worker_id}\n")


# ---------------------------------------------------------------------------
# Tmux mode — completion detection
# ---------------------------------------------------------------------------

_SENTINEL_RE = re.compile(r"___DONE_([a-f0-9]+)_(\d+)___")


def check_worker_done(worker):
    """Check if a worker's current task has finished by looking for the sentinel."""
    if not worker.busy:
        return False, -1

    try:
        # FIX-004: -J joins wrapped lines (prevents sentinel wrap in narrow panes)
        output = tmux_run([
            "capture-pane", "-t", worker.tmux_target,
            "-p", "-J", "-S", "-20",
        ], check=False)
    except Exception:
        return False, -1

    for match in _SENTINEL_RE.finditer(output):
        if match.group(1) == worker.sentinel_id:
            exit_code = int(match.group(2))
            return True, exit_code

    return False, -1


# ---------------------------------------------------------------------------
# Tmux mode — dashboard
# ---------------------------------------------------------------------------

def update_tmux_status_bar(task_states, paused, wall_start, session_name):
    """Update tmux bottom status bar with progress summary."""
    now = time.monotonic()
    done = sum(1 for t in task_states.values() if t.status == TaskStatus.DONE)
    failed = sum(1 for t in task_states.values() if t.status == TaskStatus.FAILED)
    running = sum(1 for t in task_states.values() if t.status == TaskStatus.RUNNING)
    total = len(task_states)
    completed = done + failed
    pct = int(completed / total * 100) if total > 0 else 0
    elapsed = format_duration(now - wall_start)

    pause_tag = " #[fg=yellow,bold]PAUSED#[default]" if paused else ""
    fail_tag = f" #[fg=red]{failed}F#[default]" if failed > 0 else ""

    status = (
        f"#[fg=cyan,bold]Progress:#[default] {completed}/{total} ({pct}%)"
        f" | #[fg=green]{done}OK#[default]{fail_tag}"
        f" | #[fg=blue]{running} running#[default]"
        f" | {elapsed}{pause_tag}"
    )

    try:
        tmux_run(["set-option", "-t", session_name, "status-right-length", "120"], check=False)
        tmux_run(["set-option", "-t", session_name, "status-right", status], check=False)
        tmux_run(["set-option", "-t", session_name, "status-style", "bg=black,fg=white"], check=False)
    except Exception:
        pass


def update_dashboard_file(task_states, workers, paused, wall_start, session_name_short, session_name):
    """Write dashboard content to the run-local dashboard file."""
    now = time.monotonic()
    lines = []
    dashboard_path = _dashboard_file(session_name_short)
    pause_path = _pause_file(session_name_short)

    pending = sum(1 for t in task_states.values() if t.status == TaskStatus.PENDING)
    running = sum(1 for t in task_states.values() if t.status == TaskStatus.RUNNING)
    done = sum(1 for t in task_states.values() if t.status == TaskStatus.DONE)
    failed = sum(1 for t in task_states.values() if t.status == TaskStatus.FAILED)
    total = len(task_states)

    other_sessions = _list_running_sessions()
    total_sessions = len(other_sessions)

    lines.append("=" * 58)
    session_header = f"  PARALLEL RUNNER — {session_name_short.upper()}"
    if total_sessions > 1:
        session_header += f"  ({total_sessions} sessions active)"
    lines.append(session_header)
    lines.append("=" * 58)

    # Recent completions
    finished = sorted(
        [t for t in task_states.values() if t.status in (TaskStatus.DONE, TaskStatus.FAILED)],
        key=lambda t: t.end_time,
        reverse=True,
    )[:4]
    lines.append("")
    lines.append("  RECENT COMPLETIONS:")
    if finished:
        for t in finished:
            elapsed_t = format_duration(t.end_time - t.start_time) if t.start_time > 0 else "?"
            dot = _color_for(t.task_id)
            if t.status == TaskStatus.DONE:
                status = f"\033[32mOK\033[0m"
            else:
                status = f"\033[31mFAIL(exit {t.exit_code})\033[0m"
            log_path = f"{LOG_DIR}/task_{t.task_id}.log"
            lines.append(f"    {dot} {t.task_id}: {status} [{elapsed_t}] -> {log_path}")
    else:
        lines.append("    (none yet)")

    # Help
    lines.append("")
    if not paused:
        lines.append(f"  Pause:    touch {pause_path}")
    else:
        lines.append(f"  Resume:   rm {pause_path}")
    lines.append(f"  Logs:     {LOG_DIR}/task_<TASK_ID>.log")
    lines.append("  Detach:   Ctrl-b d  (orchestrator keeps running)")
    lines.append(f"  Reattach: tmux attach -t {session_name}")
    lines.append(f"  Stop:     Ctrl-c  (or --stop --name {session_name_short})")

    if paused:
        lines.append("")
        lines.append("  \033[33;1m*** PAUSED ***\033[0m")

    # Running workers
    lines.append("")
    lines.append("  RUNNING:")
    running_tasks = [t for t in task_states.values() if t.status == TaskStatus.RUNNING]
    if running_tasks:
        for t in running_tasks:
            elapsed_t = format_duration(now - t.start_time)
            dot = _color_for(t.task_id)
            lines.append(f"    {dot} worker-{t.worker_id}: {t.task_id} ({elapsed_t})")
    else:
        lines.append("    (none)")

    # Progress bar
    lines.append("")
    lines.append("-" * 58)
    elapsed = format_duration(now - wall_start)
    total_cost_usd = sum(t.total_cost_usd for t in task_states.values() if t.total_cost_usd > 0)
    lines.append(f"  Pending: {pending}  |  Running: {running}  |  Done: {done}  |  Failed: {failed}  |  Total: {total}")
    lines.append(f"  Total Cost (USD): ${total_cost_usd:.6f}")
    if total > 0:
        completed = done + failed
        remaining = pending + running
        pct = int(completed / total * 100)
        bar_width = 40
        filled = int(bar_width * completed / total)
        bar = "\u2588" * filled + "\u2591" * (bar_width - filled)
        eta_str = ""
        if completed > 0 and remaining > 0:
            finished_tasks = [t for t in task_states.values()
                              if t.status in (TaskStatus.DONE, TaskStatus.FAILED)
                              and t.start_time > 0 and t.end_time > 0]
            if finished_tasks:
                avg_task = sum(t.end_time - t.start_time for t in finished_tasks) / len(finished_tasks)
                active_workers = max(len(workers), 1)
                eta_secs = avg_task * remaining / active_workers
                eta_str = f"  ETA: ~{format_duration(eta_secs)}"
        lines.append(f"  [{bar}] {pct}% ({completed}/{total})  Wall: {elapsed}{eta_str}")
    lines.append("-" * 58)

    # Write atomically so tmux dashboard readers never see a partially-written frame.
    dashboard_content = "\n".join(lines) + "\n"
    dashboard_tmp_path = dashboard_path.parent / f"{dashboard_path.name}.tmp"
    dashboard_tmp_path.write_text(dashboard_content, encoding="utf-8")
    os.replace(dashboard_tmp_path, dashboard_path)
    update_tmux_status_bar(task_states, paused, wall_start, session_name)


# ---------------------------------------------------------------------------
# Tmux mode — orchestration loop
# ---------------------------------------------------------------------------

def orchestration_loop(task_states, workers, task_queue, pwd, prompt_paths,
                       prompt_template, wall_start, session_name_short, session_name,
                       output_dir=None, service="claude", model=":sonnet",
                       file_format="", strict=False, subagent_args=None,
                       command_mode=False, tmux_handoff=False):
    """Main orchestration loop — polls workers, dispatches tasks, updates dashboard."""
    all_task_ids = list(task_states.keys())
    pause_path = _pause_file(session_name_short)

    while True:
        if _shutdown_event.is_set():
            print(f"[{datetime.now()}] Shutdown signal received, stopping dispatch...",
                  file=sys.stderr)
            break

        result = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            capture_output=True,
        )
        if result.returncode != 0:
            print(f"[{datetime.now()}] Tmux session '{session_name}' gone, shutting down...",
                  file=sys.stderr)
            break

        paused = pause_path.exists()

        # Check each worker for completion
        for worker in workers:
            if not worker.busy:
                continue

            done, exit_code = check_worker_done(worker)
            if not done:
                continue

            task_id = worker.current_task
            ts = task_states[task_id]
            ts.end_time = time.monotonic()
            ts.end_time_iso = datetime.now().isoformat()
            ts.exit_code = exit_code

            if exit_code == 0:
                ts.status = TaskStatus.DONE
            else:
                ts.status = TaskStatus.FAILED

            worker.busy = False
            worker.current_task = ""
            worker.sentinel_id = ""
            if tmux_handoff:
                worker.handoff_completed = True

            elapsed = ts.end_time - ts.start_time
            status_str = "OK" if exit_code == 0 else f"FAILED (exit {exit_code})"

            # FIX-002: Parse result from log, with capture-pane fallback
            task_log_path = LOG_DIR / f"task_{task_id}.log"
            backend_result = None if command_mode else _parse_result_from_log(task_log_path)

            if backend_result is None and not command_mode:
                try:
                    scrollback = tmux_run([
                        "capture-pane", "-t", worker.tmux_target,
                        "-p", "-S", "-",
                    ], check=False)
                    if scrollback:
                        ansi_re = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')
                        clean = ansi_re.sub('', scrollback)
                        backend_result = _parse_result_from_text(clean)
                except Exception:
                    pass

            ts.session_id = _extract_session_id(backend_result) or ""
            ts.total_cost_usd = _extract_total_cost_usd(backend_result) or 0.0

            if strict and file_format and output_dir:
                exit_code = _extract_strict_output(
                    task_id, task_log_path, output_dir, file_format, exit_code,
                )
                ts.exit_code = exit_code
                if exit_code != 0 and ts.status != TaskStatus.FAILED:
                    ts.status = TaskStatus.FAILED

            if output_dir:
                _write_task_output(
                    output_dir, task_id, exit_code, elapsed,
                    ts.start_time_iso, ts.end_time_iso, ts.worker_id,
                    backend_result=backend_result, file_format=file_format,
                )

            # Clean up temp files
            tmp = _tmp_dir(session_name_short)
            for tmp_f in [tmp / f"prompt_{task_id}.txt", tmp / f"run_{task_id}.sh",
                         tmp / f"run_{task_id}.py", tmp / f"env_{task_id}.sh",
                         tmp / f"command_{task_id}.json"]:
                try:
                    tmp_f.unlink(missing_ok=True)
                except OSError:
                    pass

            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            tag = _colored_tag(task_id)
            with open(COMBINED_LOG, "a") as f:
                f.write(
                    f"[{timestamp}] {tag} Finished - {status_str} "
                    f"({format_duration(elapsed)})\n"
                )

        # Dispatch new tasks to free workers
        if not paused and not _shutdown_event.is_set():
            for worker in workers:
                if worker.busy or worker.handoff_completed:
                    continue
                if not task_queue:
                    break
                next_task_id = task_queue.popleft()
                dispatch_task(
                    worker, next_task_id, task_states[next_task_id],
                    pwd, prompt_paths, session_name_short, output_dir,
                    service, model, file_format, subagent_args,
                    prompt_template, command_mode,
                )

        # Update dashboard
        update_dashboard_file(task_states, workers, paused, wall_start,
                              session_name_short, session_name)

        # Check if all done
        all_done = all(
            ts.status in (TaskStatus.DONE, TaskStatus.FAILED)
            for ts in task_states.values()
        )
        if all_done:
            break

        if _shutdown_event.is_set():
            any_busy = any(w.busy for w in workers)
            if not any_busy:
                break

        time.sleep(2)

    # Close all pipe-panes
    for worker in workers:
        tmux_run(["pipe-pane", "-t", worker.tmux_target], check=False)

    shutil.rmtree(str(_tmp_dir(session_name_short)), ignore_errors=True)

    # Final dashboard update
    update_dashboard_file(task_states, workers, False, wall_start,
                          session_name_short, session_name)

    # Build results
    results = {}
    task_times = {}
    for tid, ts in task_states.items():
        results[tid] = ts.exit_code
        if ts.start_time > 0 and ts.end_time > 0:
            task_times[tid] = ts.end_time - ts.start_time

    wall_elapsed = time.monotonic() - wall_start

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ok = sum(1 for ts in task_states.values() if ts.status == TaskStatus.DONE)
    failed = sum(1 for ts in task_states.values() if ts.status == TaskStatus.FAILED)

    summary_lines = [
        "",
        "=" * 60,
        "RESULTS",
        "=" * 60,
    ]
    for tid in all_task_ids:
        rc = results.get(tid, -1)
        el = task_times.get(tid, 0)
        st = "OK" if rc == 0 else f"FAILED (exit {rc})"
        tag = _colored_tag(tid)
        summary_lines.append(f"  {tag} {st}  [{format_duration(el)}]")

    summary_lines.extend([
        "-" * 60,
        "STATS",
        f"  Total tasks:    {len(task_states)}",
        f"  Succeeded:      {ok}",
        f"  Failed:         {failed}",
        f"  Wall time:      {format_duration(wall_elapsed)}",
    ])
    if task_times:
        avg = sum(task_times.values()) / len(task_times)
        fastest = min(task_times.values())
        slowest = max(task_times.values())
        summary_lines.extend([
            f"  Avg per task:   {format_duration(avg)}",
            f"  Fastest task:   {format_duration(fastest)}",
            f"  Slowest task:   {format_duration(slowest)}",
        ])
    total_cost_usd = sum(ts.total_cost_usd for ts in task_states.values() if ts.total_cost_usd > 0)
    summary_lines.extend([
        f"  Total cost:     ${total_cost_usd:.6f} USD",
        f"  Run ID:         {_run_id}",
        f"  Per-task logs:  {LOG_DIR}/task_<TASK_ID>.log",
    ])

    session_rows = []
    for tid in all_task_ids:
        ts = task_states[tid]
        if not ts.session_id and ts.total_cost_usd <= 0:
            continue
        session_rows.append((tid, ts.session_id or "-", ts.total_cost_usd))

    if session_rows:
        summary_lines.append("  Session summary:")
        for tid, sid, cost in session_rows:
            summary_lines.append(f"    {tid}: session_id={sid}, cost=${cost:.6f}")

    summary_lines.append("=" * 60)

    with open(COMBINED_LOG, "a") as f:
        for line in summary_lines:
            f.write(f"[{timestamp}] {line}\n")

    dashboard_path = _dashboard_file(session_name_short)
    with open(dashboard_path, "a") as f:
        f.write("\n")
        for line in summary_lines:
            f.write(f"  {line}\n")

    # Write aggregation
    if output_dir:
        task_outputs = {}
        for tid in all_task_ids:
            cap = _task_output_path(output_dir, tid)
            if cap.exists():
                try:
                    task_outputs[tid] = json.loads(cap.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    task_outputs[tid] = {"task_id": tid, "exit_code": results.get(tid, -1),
                                         "backend_result": None}
            else:
                task_outputs[tid] = {"task_id": tid, "exit_code": results.get(tid, -1),
                                     "backend_result": None}
        agg_result = _write_aggregation(
            output_dir, task_outputs, wall_elapsed, len(workers),
            mode=f"tmux/{session_name}", session_name=session_name_short,
            file_format=file_format,
            require_extraction=not command_mode,
        )
        summary_text = _format_output_summary(agg_result)
        with open(COMBINED_LOG, "a") as f:
            f.write(f"[{timestamp}] {summary_text}\n")
        with open(dashboard_path, "a") as f:
            f.write(f"\n{summary_text}\n")

    # Tmux notification
    try:
        tmux_run([
            "display-message", "-t", session_name,
            f"All {len(task_states)} tasks complete! ({ok} OK, {failed} failed)",
        ], check=False)
    except Exception:
        pass

    return 1 if failed > 0 else 0


# ---------------------------------------------------------------------------
# Tmux mode — entry point
# ---------------------------------------------------------------------------

def _print_detached_tmux_commands(session_name, session_name_short):
    """Print concrete operator commands for an explicitly detached tmux launch."""
    runner_command = shlex.quote(str(Path(sys.argv[0]).resolve()))
    wait_script = shlex.quote(str(SCRIPT_DIR / "parallel_runner_wait.sh"))
    print(f"Stop: {runner_command} --stop --name {shlex.quote(session_name_short)}")
    print(f"Attach: tmux attach -t {shlex.quote(session_name)}")
    print(f"Follow: tail -f {shlex.quote(str(COMBINED_LOG))}")
    print(f"Wait: {wait_script} --run-dir {shlex.quote(str(LOG_DIR))} --verbose")


def run_tmux_mode(args, pwd, prompt_source_label, prompt_template, output_dir,
                  service, model, subagent_args=None, attach=True):
    """Set up tmux session and run orchestrator."""
    num_workers = args.parallel
    mode = args.tmux
    command_mode = _is_command_mode(args)

    session_name_short = args.name if args.name else _next_batch_name()
    session_name = _session_name_to_tmux(session_name_short)

    if mode == "panes" and num_workers > 5:
        print(
            f"WARNING: --parallel {num_workers} with panes mode may make panes too small. "
            f"Consider using 'windows' mode or reducing --parallel to 5.",
            file=sys.stderr,
        )

    _log_base.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # Kill stale orchestrator daemon for THIS session name only
    stale_pid_files = _pid_files_for_name(session_name_short)
    for stale_pid_path in stale_pid_files:
        try:
            old_pid = int(stale_pid_path.read_text().strip())
            os.kill(old_pid, signal.SIGTERM)
            print(f"Killed stale orchestrator for '{session_name_short}' (PID {old_pid})")
            time.sleep(0.3)
        except (ValueError, ProcessLookupError, PermissionError):
            pass

    pid_path = _pid_file(session_name_short, run_dir=LOG_DIR)
    for f in [_dashboard_file(session_name_short, run_dir=LOG_DIR), _pause_file(session_name_short, run_dir=LOG_DIR), pid_path, *_legacy_session_state_files(session_name_short), *stale_pid_files]:
        if f.exists():
            f.unlink()

    # Log startup
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(COMBINED_LOG, "a") as f:
        f.write(f"[{timestamp}] {'=' * 60}\n")
        f.write(f"[{timestamp}] Starting parallel task execution (tmux {mode} mode)\n")
        f.write(f"[{timestamp}] Run ID: {_run_id}\n")
        f.write(f"[{timestamp}] Session: {session_name} (name: {session_name_short})\n")
        f.write(f"[{timestamp}] PWD: {pwd}\n")
        input_label = "Commands" if command_mode else "Tasks"
        f.write(f"[{timestamp}] {input_label} ({len(args.kanban)}): {', '.join(args.kanban)}\n")
        if command_mode:
            for tid in args.kanban[:5]:
                spec = _command_spec_for(tid)
                f.write(
                    f"[{timestamp}]   {tid} -> cwd={spec.get('cwd') or pwd} "
                    f"shell={isinstance(spec['command'], str)} timeout={spec.get('timeout_seconds') or '-'} "
                    f"command={_format_command_for_log(spec['command'])}\n"
                )
            if len(args.kanban) > 5:
                f.write(f"[{timestamp}]   ... and {len(args.kanban) - 5} more command(s)\n")
        if any(tid.startswith("item-") for tid in args.kanban):
            for tid in args.kanban[:3]:
                f.write(f"[{timestamp}]   {tid} -> {_item_map[tid][:80]}\n")
            if len(args.kanban) > 3:
                f.write(f"[{timestamp}]   ... and {len(args.kanban) - 3} more\n")
        f.write(f"[{timestamp}] Parallelism: {num_workers}\n")
        if getattr(args, "tmux_handoff", False):
            f.write(f"[{timestamp}] Tmux handoff: enabled (dedicated worker per task; completed workers are not reused)\n")
        if not command_mode:
            f.write(f"[{timestamp}] Service: {service} | Model: {model}\n")
        if subagent_args:
            f.write(f"[{timestamp}] Subagent args: {' '.join(subagent_args)}\n")
        if prompt_source_label:
            source_type = "Prompt file" if prompt_source_label not in ("inline", "stdin") else "Prompt source"
            f.write(f"[{timestamp}] {source_type}: {prompt_source_label} (materialized at startup)\n")
        if output_dir:
            f.write(f"[{timestamp}] Output dir: {output_dir}\n")
        f.write(f"[{timestamp}] {'=' * 60}\n")

    _write_log_pipe_helper(session_name_short)

    print(f"Creating tmux session '{session_name}' ({mode} mode, {num_workers} workers)...")

    coordinator_target, workers = create_tmux_session(session_name, mode, num_workers, pwd, args.kanban if mode == "tabs" else None)

    task_states = {}
    for tid in args.kanban:
        task_states[tid] = TaskState(task_id=tid)
    task_queue = deque(args.kanban)

    file_format = getattr(args, "file_format", "") or ""
    prompt_paths = {}
    if prompt_template is not None:
        prompt_dir = _tmp_dir(session_name_short) / "prompts"
        prompt_paths = prepare_prompt_files(args.kanban, prompt_template, prompt_dir, file_format)

    wall_start = time.monotonic()

    # Start coordinator dashboard
    # NOTE (PfU2s8): Clear BEFORE printing each frame.
    # If clear happens after `cat`, tmux can leave stale prompt/command tails
    # (e.g. parts of this dashboard_cmd string) stitched into dashboard rows.
    # Keep: printf '\033[H\033[J'; cat ...
    pid_path_str = shlex.quote(str(pid_path))
    dashboard_file_str = shlex.quote(str(_dashboard_file(session_name_short)))
    dashboard_cmd = (
        f"trap 'kill $(cat {pid_path_str}) 2>/dev/null; exit' INT; "
        f"while true; do printf '\\033[H\\033[J'; cat {dashboard_file_str} 2>/dev/null "
        f"|| echo 'Waiting for dashboard...'; sleep 2; done"
    )
    tmux_run(["send-keys", "-t", coordinator_target, dashboard_cmd, "Enter"])

    update_dashboard_file(task_states, workers, False, wall_start,
                          session_name_short, session_name)

    # Fork: child = orchestrator daemon, parent = tmux attach
    pid = os.fork()

    if pid == 0:
        # Child process — orchestrator daemon
        try:
            os.setsid()
        except OSError:
            pass

        pid_path.write_text(str(os.getpid()))

        log_fd = os.open(
            str(_orchestrator_log(session_name_short)),
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        )
        os.dup2(log_fd, 1)
        os.dup2(log_fd, 2)
        os.close(log_fd)

        signal.signal(signal.SIGHUP, signal.SIG_IGN)

        def _shutdown_handler(signum, frame):
            sig_name = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
            print(f"[{datetime.now()}] Received {sig_name}, initiating graceful shutdown...",
                  file=sys.stderr)
            _shutdown_event.set()

        signal.signal(signal.SIGTERM, _shutdown_handler)
        signal.signal(signal.SIGINT, _shutdown_handler)

        try:
            _write_run_status("running", f"tmux/{mode}", pid=os.getpid(), session_name=session_name_short)
            for worker in workers:
                if not task_queue:
                    break
                next_task_id = task_queue.popleft()
                dispatch_task(
                    worker, next_task_id, task_states[next_task_id],
                    pwd, prompt_paths, session_name_short, output_dir,
                    service, model, file_format, subagent_args,
                    prompt_template, command_mode,
                )

            update_dashboard_file(task_states, workers, False, wall_start,
                                  session_name_short, session_name)

            strict = getattr(args, "strict", False)
            exit_code = orchestration_loop(
                task_states, workers, task_queue,
                pwd, prompt_paths, prompt_template, wall_start,
                session_name_short, session_name, output_dir,
                service, model, file_format, strict, subagent_args,
                command_mode, getattr(args, "tmux_handoff", False),
            )
        except Exception as exc:
            import traceback
            traceback.print_exc()
            exit_code = 1
            _write_run_status("completed", f"tmux/{mode}", exit_code=exit_code, error=exc, session_name=session_name_short)
        else:
            _write_run_status("completed", f"tmux/{mode}", exit_code=exit_code, session_name=session_name_short)

        if pid_path.exists():
            try:
                pid_path.unlink()
            except OSError:
                pass

        os._exit(exit_code)

    else:
        # Parent process — attach to tmux session
        print(f"Orchestrator daemon started (PID {pid})")
        print(f"Run ID: {_run_id}")
        print(f"Session: {session_name} (name: {session_name_short})")
        print(f"{'Commands' if command_mode else 'Tasks'}: {', '.join(args.kanban)}")
        print(f"Workers: {num_workers}")
        if getattr(args, "tmux_handoff", False):
            print("Tmux handoff: enabled (completed panes/windows are preserved and not reused)")
        print(f"Logs: {LOG_DIR}/")
        print(f"Pause: touch {_pause_file(session_name_short)}")
        if not attach:
            _print_detached_tmux_commands(session_name, session_name_short)
            print()
            return {
                "session_name": session_name,
                "session_name_short": session_name_short,
                "pid": pid,
                "run_id": _run_id,
                "log_dir": str(LOG_DIR),
                "status_path": str(STATUS_FILE),
                "combined_log": str(COMBINED_LOG),
                "dashboard_path": str(_dashboard_file(session_name_short)),
                "pause_path": str(_pause_file(session_name_short)),
                "items": list(args.kanban),
            }
        runner_command = shlex.quote(str(Path(sys.argv[0]).resolve()))
        print(f"Stop: {runner_command} --stop --name {shlex.quote(session_name_short)}")
        print(f"Attaching to tmux session...")
        print()

        time.sleep(0.5)

        os.execvp("tmux", ["tmux", "attach-session", "-t", session_name])


def _chunked(items, size):
    """Split items into fixed-size chunks while preserving input order."""
    return [items[i:i + size] for i in range(0, len(items), size)]


def _handoff_child_name(base_name, index, total_chunks):
    """Return the stable short session name for a handoff child session."""
    if total_chunks <= 1:
        return base_name
    return f"{base_name}-{index}"


def _write_parent_handoff_manifest(parent_log_dir, manifest):
    """Write the parent manifest atomically in the parent run artifact directory."""
    manifest_path = parent_log_dir / "tmux_handoff_manifest.json"
    tmp_path = manifest_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp_path, manifest_path)
    return manifest_path


def _read_child_status(status_path):
    """Read a child session status file, returning a normalized status payload."""
    path = Path(status_path)
    if not path.exists():
        return {"state": "pending", "exit_code": None, "status_path": str(path)}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {"state": "unknown", "exit_code": None, "status_path": str(path), "error": str(exc)}
    if not isinstance(payload, dict):
        return {"state": "unknown", "exit_code": None, "status_path": str(path), "error": "status JSON is not an object"}
    return payload


def _refresh_handoff_manifest_status(parent_log_dir, manifest):
    """Refresh child status fields in the parent manifest and return aggregate completion."""
    all_completed = True
    aggregate_exit = 0
    for child in manifest.get("child_sessions", []):
        payload = _read_child_status(child.get("status_path", ""))
        state = payload.get("state") or "unknown"
        exit_code = payload.get("exit_code")
        child["state"] = state
        child["exit_code"] = exit_code
        child["started_at"] = payload.get("started_at")
        child["finished_at"] = payload.get("finished_at")
        child["last_status_checked_at"] = datetime.now().isoformat()
        if payload.get("error"):
            child["status_error"] = payload.get("error")
        elif "status_error" in child:
            child.pop("status_error", None)
        if state != "completed":
            all_completed = False
        elif exit_code:
            aggregate_exit = 1
    manifest["last_status_checked_at"] = datetime.now().isoformat()
    manifest["completed_child_sessions"] = sum(
        1 for child in manifest.get("child_sessions", []) if child.get("state") == "completed"
    )
    manifest["failed_child_sessions"] = sum(
        1 for child in manifest.get("child_sessions", []) if child.get("state") == "completed" and child.get("exit_code")
    )
    _write_parent_handoff_manifest(parent_log_dir, manifest)
    return all_completed, aggregate_exit


def _wait_for_handoff_children(parent_log_dir, manifest, poll_seconds=2):
    """Keep the parent runner alive until all child tmux handoff sessions complete."""
    print("Waiting for child tmux handoff sessions to complete...")
    while True:
        all_completed, aggregate_exit = _refresh_handoff_manifest_status(parent_log_dir, manifest)
        if all_completed:
            return aggregate_exit
        time.sleep(poll_seconds)


def run_tmux_handoff_batched(args, pwd, prompt_source_label, prompt_template, output_dir,
                             service, model, subagent_args=None):
    """Create multiple capped --tmux-handoff child sessions and wait for real completion."""
    global LOG_DIR, COMBINED_LOG, STATUS_FILE

    cap = args.max_panes_per_session
    parent_log_dir = LOG_DIR
    parent_status_file = STATUS_FILE
    parent_run_id = _run_id
    parent_name = args.name if args.name else _next_batch_name()
    chunks = _chunked(list(args.kanban), cap)
    total_chunks = len(chunks)

    parent_log_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1,
        "created_at": datetime.now().isoformat(),
        "parent_run_id": parent_run_id,
        "parent_name": parent_name,
        "parent_log_dir": str(parent_log_dir),
        "parent_status_path": str(parent_status_file),
        "mode": f"tmux/{args.tmux}/handoff-batched",
        "max_panes_per_session": cap,
        "total_items": len(args.kanban),
        "output_dir": str(output_dir) if output_dir else None,
        "aggregation_glob": str(output_dir / "*" / "aggregation_*.json") if output_dir else None,
        "child_sessions": [],
    }

    print("Creating capped tmux handoff sessions...")
    print(f"Parent run ID: {parent_run_id}")
    print(f"Parent artifacts: {parent_log_dir}")
    print(f"Items: {len(args.kanban)} | cap: {cap} | sessions: {total_chunks}")

    for index, chunk in enumerate(chunks, start=1):
        child_short = _handoff_child_name(parent_name, index, total_chunks)
        child_args = copy.copy(args)
        child_args.name = child_short
        child_args.kanban = list(chunk)
        child_args.parallel = len(chunk)
        child_args.max_panes_per_session = None

        child_log_dir = parent_log_dir / child_short
        child_output_dir = output_dir / child_short if output_dir else None
        if child_output_dir:
            child_output_dir.mkdir(parents=True, exist_ok=True)
        LOG_DIR = child_log_dir
        COMBINED_LOG = LOG_DIR / "parallel_runner.log"
        STATUS_FILE = LOG_DIR / "parallel_runner_status.json"
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        _write_run_status("running", f"tmux/{args.tmux}", session_name=child_short)

        child_info = run_tmux_mode(
            child_args, pwd, prompt_source_label, prompt_template, child_output_dir,
            service, model, subagent_args, attach=False,
        )
        if not child_info:
            child_info = {
                "session_name": _session_name_to_tmux(child_short),
                "session_name_short": child_short,
                "log_dir": str(LOG_DIR),
                "status_path": str(STATUS_FILE),
                "items": list(chunk),
                "output_dir": str(child_output_dir) if child_output_dir else None,
                "aggregation_glob": str(child_output_dir / "aggregation_*.json") if child_output_dir else None,
            }
        child_info.update({
            "index": index,
            "task_ids": list(chunk),
            "output_dir": str(child_output_dir) if child_output_dir else None,
            "aggregation_glob": str(child_output_dir / "aggregation_*.json") if child_output_dir else None,
            "attach_command": f"tmux attach -t {child_info['session_name']}",
            "stop_command": f"{Path(sys.argv[0]).name} --stop --name {child_short}",
            "state": "running",
            "exit_code": None,
        })
        manifest["child_sessions"].append(child_info)
        _write_parent_handoff_manifest(parent_log_dir, manifest)

    LOG_DIR = parent_log_dir
    COMBINED_LOG = parent_log_dir / "parallel_runner.log"
    STATUS_FILE = parent_status_file
    manifest_path = _write_parent_handoff_manifest(parent_log_dir, manifest)

    print()
    print("Tmux handoff sessions are running. Attach to any session while the parent waits:")
    for child in manifest["child_sessions"]:
        print(f"  {child['session_name']}: {child['attach_command']}  # {', '.join(child['task_ids'])}")
    print(f"Parent manifest: {manifest_path}")
    print("Each child uses one dedicated non-reused pane/window per task.")

    try:
        exit_code = _wait_for_handoff_children(parent_log_dir, manifest)
    except KeyboardInterrupt:
        exit_code = 130
        manifest["interrupted_at"] = datetime.now().isoformat()
        _write_parent_handoff_manifest(parent_log_dir, manifest)
        print("Interrupted while waiting for child sessions; child tmux sessions may still be running.", file=sys.stderr)

    _write_run_status("completed", f"tmux/{args.tmux}/handoff-batched", exit_code=exit_code, session_name=parent_name)
    return exit_code


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def resolve_session_metadata_directory(controller_root: str) -> str:
    override = os.environ.get("YYLO_SESSION_METADATA_DIRECTORY", "").strip()
    if override:
        candidate = Path(override)
        return str(candidate if candidate.is_absolute() else (Path(controller_root) / candidate).resolve())
    completed = subprocess.run(
        ["git", "-C", controller_root, "rev-parse", "--path-format=absolute", "--git-common-dir"],
        text=True, capture_output=True,
    )
    if completed.returncode == 0 and completed.stdout.strip():
        return str(Path(completed.stdout.strip()).resolve() / "juno" / "session_metadata")
    identity = hashlib.sha256(str(Path(controller_root).resolve()).encode()).hexdigest()[:16]
    state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return str(state_home / "@yylo/cli" / "session_metadata" / identity)


def resolve_controller_environment() -> dict[str, str]:
    resolver = Path(__file__).resolve().with_name("controller_resolver.py")
    if not resolver.is_file():
        resolver = next(
            (parent / ".juno_task/scripts/controller_resolver.py" for parent in (Path.cwd(), *Path.cwd().parents)
             if (parent / ".juno_task/scripts/controller_resolver.py").is_file()),
            resolver,
        )
    completed = subprocess.run(
        [sys.executable, str(resolver), "--cwd", os.getcwd(), "--operation", "orchestration"],
        text=True, capture_output=True, check=True,
    )
    resolution = json.loads(completed.stdout)
    controller_root = str(resolution["path"])
    return {
        "JUNO_TASK_ROOT": controller_root,
        "JUNO_CONTROLLER_SOURCE": resolution["source"],
        "JUNO_WORKSPACE_ROLE": resolution["role"],
        "YYLO_SESSION_METADATA_DIRECTORY": resolve_session_metadata_directory(controller_root),
    }


def checkpoint_after_finalization(exit_code: int) -> None:
    """Best effort only; terminal parallel artifacts must already exist."""
    if os.environ.get("JUNO_CONTROLLER_CHECKPOINT_ACTIVE") == "1":
        return
    root = Path(os.environ["JUNO_TASK_ROOT"]).resolve()
    script = root / ".juno_task/scripts/controller_checkpoint.py"
    if not script.is_file():
        return
    message = "chore(controller): checkpoint finalized parallel run state"
    if exit_code:
        message = f"chore(controller): checkpoint failed parallel run state (exit {exit_code})"
    env = _child_process_environment(dict(os.environ))
    env["JUNO_CONTROLLER_CHECKPOINT_ACTIVE"] = "1"
    try:
        completed = subprocess.run(
            [sys.executable, str(script), "--root", str(root), "commit", "--message", message],
            cwd=root, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=30, check=False,
        )
        if completed.returncode:
            detail = (completed.stderr or completed.stdout or f"exit {completed.returncode}").strip()
            detail = re.sub(r"(?im)^(\s*(?:authorization|token|password|secret)\s*[:=]\s*).*$", r"\1[REDACTED]", detail)
            detail = detail[-2000:]
            raise RuntimeError(f"checkpoint exit {completed.returncode}: {detail}")
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        print(
            f"[parallel_runner] WARNING: controller checkpoint failed after finalization; "
            f"run {script} --root {root} commit manually: {exc}", file=sys.stderr,
        )


def main():
    global LOG_DIR, COMBINED_LOG, STATUS_FILE, _run_id, _run_started_at

    _sanitize_current_process_environment()
    os.environ.update(resolve_controller_environment())
    warn_if_runtime_script_is_stale("parallel_runner.sh")
    args = parse_args()

    if args.stop_all:
        run_stop_all()
        return
    if args.stop:
        run_stop(args)
        return

    pwd = os.getcwd()

    # Generate run ID and compute per-run LOG_DIR
    started_at = datetime.now()
    _run_id = started_at.strftime("%H%M%S-%f")
    _run_started_at = started_at.isoformat()
    date_str = started_at.strftime("%Y-%m-%d")
    LOG_DIR = _log_base / date_str / _run_id
    COMBINED_LOG = LOG_DIR / "parallel_runner.log"
    STATUS_FILE = LOG_DIR / "parallel_runner_status.json"

    # Assign colors
    for i, tid in enumerate(args.kanban):
        _task_color_map[tid] = _TASK_COLORS[i % len(_TASK_COLORS)]

    service, model = _resolve_service_model(args)
    prompt_source_label, prompt_template = resolve_prompt_source(args, pwd)
    output_dir = _resolve_output_dir(args)
    subagent_args = getattr(args, "subagent_args_list", [])

    _log_base.mkdir(parents=True, exist_ok=True)
    removed_tmp = cleanup_stale_tmp_artifacts()
    if removed_tmp > 0:
        print(f"[parallel_runner] Removed {removed_tmp} stale tmp artifact(s) older than 48h")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[parallel_runner] Run artifacts: {LOG_DIR}")
    initial_mode = f"tmux/{args.tmux}" if args.tmux else "headless"
    _write_run_status("running", initial_mode)

    if args.tmux:
        if args.tmux_handoff and args.max_panes_per_session is not None and len(args.kanban) > args.max_panes_per_session:
            sys.exit(run_tmux_handoff_batched(args, pwd, prompt_source_label, prompt_template, output_dir, service, model, subagent_args))
        else:
            run_tmux_mode(
                args, pwd, prompt_source_label, prompt_template, output_dir,
                service, model, subagent_args, attach=not args.no_attach,
            )
    else:
        run_headless_mode(args, pwd, prompt_source_label, prompt_template, output_dir, service, model, subagent_args)


if __name__ == "__main__":
    _final_exit_code = 0
    try:
        main()
    except SystemExit as exc:
        _final_exit_code = int(exc.code) if isinstance(exc.code, int) else 1
        raise
    except BaseException:
        _final_exit_code = 1
        raise
    finally:
        # main returns only after task/session summaries and parent handoff
        # manifests have reached their terminal durable state.
        checkpoint_after_finalization(_final_exit_code)
