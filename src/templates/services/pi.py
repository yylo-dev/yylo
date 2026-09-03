#!/usr/bin/env python3
"""
Pi Agent Service Script for yylo
Headless wrapper around the Pi coding agent CLI with JSON streaming and shorthand model support.
"""

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, TextIO, Tuple

from environment_boundary import (
    ModelShortcutError,
    resolve_model_shortcut,
    sanitize_current_process_environment,
    sanitize_model_shortcut_environment,
)

sanitize_current_process_environment()


DEFAULT_PROMPT_ARG_MAX_BYTES = 64 * 1024
# Live mode still needs a short argv prompt that tells the operator/model where
# to read the continuation file. If tests or users set the generic threshold
# below that instruction size, use this conservative effective floor for live
# argv splitting rather than producing an argv prompt that violates its own cap.
MIN_LIVE_PROMPT_ARG_MAX_BYTES = 4096
PROMPT_ARG_MAX_BYTES_ENV = "JUNO_PROMPT_ARG_MAX_BYTES"


def _positive_int_env(name: str, fallback: int) -> int:
    value = os.environ.get(name, "")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


class PiService:
    """Service wrapper for Pi coding agent headless mode."""

    DEFAULT_MODEL = ":gpt"

    # Model shorthands — Pi is multi-provider so shorthands include provider/model format.
    # All colon-prefixed shorthands are expanded before being passed to pi CLI.
    MODEL_SHORTHANDS: Dict[str, str] = {
        # Meta shorthand
        ":pi": ":gpt",
        ":default": ":gpt",
        # Anthropic
        ":sonnet": "anthropic/claude-sonnet-4-6",
        ":opus": "anthropic/claude-opus-4-6",
        ":haiku": "anthropic/claude-haiku-4-5-20251001",
        # OpenAI
        ":luna": "openai-codex/gpt-5.6-luna",
        ":sol": "openai-codex/gpt-5.6-sol",
        ":gpt": ":sol",
        ":gpt5.5": "openai-codex/gpt-5.5",
        ":mini": "openai-codex/gpt-5.6-terra",
        ":gpt-5": "openai/gpt-5",
        ":gpt-4o": "openai/gpt-4o",
        ":o3": "openai/o3",
        ":codex": "openai-codex/gpt-5.3-codex",
        ":api-codex": "openai/gpt-5.3-codex",
        ":codex-spark": "openai-codex/gpt-5.3-codex-spark",
        ":api-codex-spark": "openai/gpt-5.3-codex-spark",
        # Google
        ":gemini-pro": "google/gemini-2.5-pro",
        ":gemini-flash": "google/gemini-2.5-flash",
        # Groq
        ":groq": "groq/llama-4-scout-17b-16e-instruct",
        # xAI
        ":grok": "xai/grok-3",
    }

    # Default stream types to suppress (Pi outputs lifecycle events that are noisy)
    DEFAULT_HIDDEN_STREAM_TYPES = {
        "auto_compaction_start",
        "auto_compaction_end",
        "auto_retry_start",
        "auto_retry_end",
        "session",
        "message_start",
        "message_end",
        "tool_execution_update",
    }

    # message_update sub-events to suppress (streaming deltas are noisy;
    # completion events like text_end, thinking_end, toolcall_end are kept)
    _PI_HIDDEN_MESSAGE_UPDATE_EVENTS = {
        "text_delta",
        "text_start",
        "thinking_delta",
        "thinking_start",
        "toolcall_delta",
        "toolcall_start",
    }

    # Prettifier mode constants
    PRETTIFIER_PI = "pi"
    PRETTIFIER_CLAUDE = "claude"
    PRETTIFIER_CODEX = "codex"
    PRETTIFIER_LIVE = "live"

    # Provider-neutral headless UI palette. Labels remain explicit so the
    # transcript is equally understandable when ANSI styling is unavailable.
    ANSI_GREEN = "\x1b[38;5;40m"
    ANSI_CYAN = "\x1b[38;5;44m"
    ANSI_YELLOW = "\x1b[38;5;220m"
    ANSI_RED = "\x1b[38;5;203m"
    ANSI_BLUE = "\x1b[38;5;75m"
    ANSI_MAGENTA = "\x1b[38;5;141m"
    ANSI_BOLD = "\x1b[1m"
    ANSI_DIM = "\x1b[2m"
    ANSI_ITALIC = "\x1b[3m"
    ANSI_RESET = "\x1b[0m"

    # Keep tool args readable while preventing giant inline payloads.
    TOOL_ARG_STRING_MAX_CHARS = 400
    _ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

    def __init__(self):
        self.model_name = self.DEFAULT_MODEL
        self.project_path = os.getcwd()
        self.prompt = ""
        self.verbose = False
        self.last_result_event: Optional[dict] = None
        self.session_id: Optional[str] = None
        self.message_counter = 0
        self.prettifier_mode = self.PRETTIFIER_PI
        # Tool call grouping: buffer toolcall_end until tool_execution_end arrives
        self._pending_tool_calls: Dict[str, dict] = {}  # toolCallId -> {tool, args/command}
        # Buffer tool_execution_start data for fallback + timing (when toolcall_end arrives late)
        self._pending_exec_starts: Dict[str, dict] = {}  # toolCallId -> {tool, args/command, started_at}
        self._pending_tool_running_timers: Dict[str, threading.Timer] = {}
        self._tool_running_lock = threading.Lock()
        # Track whether we're inside a tool execution
        self._in_tool_execution: bool = False
        # Buffer raw non-JSON tool stdout so it doesn't interleave with structured events
        self._buffered_tool_stdout_lines: List[str] = []
        # Per-run usage/cost accumulation (used for result + agent_end total cost visibility)
        self._run_usage_totals: Optional[dict] = None
        self._run_total_cost_usd: Optional[float] = None
        self._run_seen_usage_keys: Set[str] = set()
        # Claude prettifier state
        self.user_message_truncate = int(os.environ.get("CLAUDE_USER_MESSAGE_PRETTY_TRUNCATE", "4"))
        # Codex prettifier state
        self._item_counter = 0
        self._codex_first_assistant_seen = False
        self._codex_tool_result_max_lines = int(os.environ.get("PI_TOOL_RESULT_MAX_LINES", "15"))
        self._tool_result_tail_lines = 2
        self._turn_cost_display_threshold_usd = 0.5
        # Keys to hide from intermediate assistant messages in Codex mode
        self._codex_metadata_keys = {"api", "provider", "model", "usage", "stopReason", "timestamp"}
        self._managed_prompt_files: List[Path] = []

    def _prompt_arg_max_bytes(self) -> int:
        return _positive_int_env(PROMPT_ARG_MAX_BYTES_ENV, DEFAULT_PROMPT_ARG_MAX_BYTES)

    def _is_prompt_oversized(self, prompt: str) -> bool:
        return len(prompt.encode("utf-8")) > self._prompt_arg_max_bytes()

    def _live_prompt_arg_max_bytes(self, header: str) -> int:
        configured_limit = self._prompt_arg_max_bytes()
        if configured_limit > len(header.encode("utf-8")):
            return configured_limit
        return max(configured_limit, MIN_LIVE_PROMPT_ARG_MAX_BYTES)

    def _create_managed_prompt_path(self) -> Path:
        prompt_dir = Path("/tmp/yylo")
        prompt_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        path = prompt_dir / f"{timestamp}-prompt.md"
        suffix = 1
        while path.exists():
            path = prompt_dir / f"{timestamp}-{suffix}-prompt.md"
            suffix += 1
        return path

    def _create_managed_prompt_file(self, prompt: str) -> Path:
        path = self._create_managed_prompt_path()
        path.write_text(prompt, encoding="utf-8")
        self._managed_prompt_files.append(path)
        return path

    def _utf8_prefix_within_bytes(self, text: str, max_bytes: int) -> str:
        if max_bytes <= 0:
            return ""
        encoded = text.encode("utf-8")
        if len(encoded) <= max_bytes:
            return text
        return encoded[:max_bytes].decode("utf-8", errors="ignore")

    def _split_live_prompt_to_file(self, prompt: str) -> str:
        prompt_path = self._create_managed_prompt_path()
        header = (
            "The original prompt was split because it is too large for live argv transport.\n"
            "Read the remaining prompt from this continuation file before acting: "
            f"{prompt_path}\n\n"
            "Beginning of original prompt:\n"
        )
        prefix_budget = self._live_prompt_arg_max_bytes(header) - len(header.encode("utf-8"))
        beginning = self._utf8_prefix_within_bytes(prompt, prefix_budget)
        remainder = prompt[len(beginning):]
        prompt_path.write_text(remainder, encoding="utf-8")
        self._managed_prompt_files.append(prompt_path)
        return header + beginning

    def _cleanup_managed_prompt_files(self) -> None:
        for prompt_path in self._managed_prompt_files:
            try:
                prompt_path.unlink(missing_ok=True)
            except Exception as exc:
                print(f"Warning: Failed to remove managed prompt file {prompt_path}: {exc}", file=sys.stderr)
        self._managed_prompt_files.clear()

    def _color_enabled(self) -> bool:
        """Check if ANSI color output is appropriate (TTY + NO_COLOR not set)."""
        if os.environ.get("NO_COLOR") is not None:
            return False
        return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

    def _colorize_lines(self, text: str, color_code: str) -> str:
        """Apply ANSI styling per line so line-oriented renderers cannot leak it."""
        if "\n" not in text:
            return f"{color_code}{text}{self.ANSI_RESET}"
        return "\n".join(f"{color_code}{line}{self.ANSI_RESET}" for line in text.split("\n"))

    def _style_text(self, text: str, *codes: str) -> str:
        if not self._color_enabled() or not text:
            return text
        return self._colorize_lines(text, "".join(codes))

    def _colorize_result(self, text: str, is_error: bool = False) -> str:
        """Make tool responses distinct and highlight omitted-middle markers."""
        if is_error:
            return self._style_text(text, self.ANSI_BOLD, self.ANSI_RED)
        if not self._color_enabled():
            return text
        styled: List[str] = []
        for line in text.split("\n"):
            color = self.ANSI_YELLOW if re.fullmatch(r"\[\d+ lines, \d+ characters truncated\]", line) else self.ANSI_GREEN
            styled.append(f"{color}{line}{self.ANSI_RESET}")
        return "\n".join(styled)

    def _colorize_command(self, text: str) -> str:
        """Render tool command/argument blocks in cyan."""
        return self._style_text(text, self.ANSI_CYAN)

    def _style_thinking(self, text: str) -> str:
        return self._style_text(text, self.ANSI_DIM, self.ANSI_ITALIC)

    def _style_assistant(self, text: str) -> str:
        return self._style_text(text, self.ANSI_BOLD)

    def _format_json_header(self, header: Dict) -> str:
        """Render compact valid JSON, adding jq-inspired token colors on TTYs."""
        raw = json.dumps(header, ensure_ascii=False, separators=(",", ":"))
        if not self._color_enabled():
            return raw

        out: List[str] = []
        current_key: Optional[str] = None
        index = 0
        while index < len(raw):
            char = raw[index]
            if char == '"':
                end = index + 1
                while end < len(raw):
                    if raw[end] == '"' and raw[end - 1] != "\\":
                        break
                    slash_count = 0
                    probe = end - 1
                    while probe >= index and raw[probe] == "\\":
                        slash_count += 1
                        probe -= 1
                    if raw[end] == '"' and slash_count % 2 == 0:
                        break
                    end += 1
                token = raw[index:end + 1]
                probe = end + 1
                while probe < len(raw) and raw[probe].isspace():
                    probe += 1
                is_key = probe < len(raw) and raw[probe] == ":"
                color = (
                    self.ANSI_BLUE
                    if is_key
                    else self.ANSI_BOLD + self.ANSI_YELLOW
                    if current_key == "status" and token == '"running"'
                    else self.ANSI_CYAN
                    if current_key in {"tool", "toolName", "command"}
                    else self.ANSI_GREEN
                )
                out.append(f"{color}{token}{self.ANSI_RESET}")
                if is_key:
                    try:
                        current_key = json.loads(token)
                    except json.JSONDecodeError:
                        current_key = None
                else:
                    current_key = None
                index = end + 1
                continue
            if char.isdigit() or char == "-":
                match = re.match(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?", raw[index:])
                if match:
                    token = match.group(0)
                    number_style = (
                        self.ANSI_BOLD + self.ANSI_YELLOW
                        if current_key == "cost_usd"
                        else self.ANSI_MAGENTA
                    )
                    out.append(f"{number_style}{token}{self.ANSI_RESET}")
                    current_key = None
                    index += len(token)
                    continue
            matched_literal = next((value for value in ("true", "false", "null") if raw.startswith(value, index)), None)
            if matched_literal:
                out.append(f"{self.ANSI_YELLOW}{matched_literal}{self.ANSI_RESET}")
                index += len(matched_literal)
                continue
            out.append(char)
            index += 1
        return "".join(out)

    def _normalize_multiline_tool_text(self, text: str) -> str:
        """Render escaped newline sequences as real newlines for tool command/args blocks."""
        if "\n" in text:
            return text
        if "\\n" in text:
            return text.replace("\\n", "\n")
        return text

    def _format_tool_invocation_header(self, header: Dict) -> str:
        """Serialize a tool header and render multiline command/args as separate readable blocks."""
        metadata = dict(header)
        block_label: Optional[str] = None
        block_text: Optional[str] = None

        command_val = metadata.get("command")
        if isinstance(command_val, str) and command_val.strip():
            command_text = self._normalize_multiline_tool_text(command_val)
            if "\n" in command_text:
                metadata.pop("command", None)
                block_label = "command:"
                block_text = self._colorize_command(command_text)

        if block_text is None:
            args_val = metadata.get("args")
            if isinstance(args_val, str) and args_val.strip():
                args_text = self._normalize_multiline_tool_text(args_val)
                if "\n" in args_text:
                    metadata.pop("args", None)
                    block_label = "args:"
                    block_text = self._colorize_command(args_text)

        output = self._format_json_header(metadata)
        if block_text is None:
            return output
        return output + "\n" + block_label + "\n" + block_text

    def _strip_ansi_sequences(self, text: str) -> str:
        """Remove ANSI escape sequences to prevent color bleed in prettified output."""
        if not isinstance(text, str) or "\x1b" not in text:
            return text
        return self._ANSI_ESCAPE_RE.sub("", text)

    def _sanitize_tool_argument_value(self, value):
        """Recursively sanitize tool args while preserving JSON structure."""
        if isinstance(value, str):
            clean = self._strip_ansi_sequences(value)
            if len(clean) > self.TOOL_ARG_STRING_MAX_CHARS:
                return clean[:self.TOOL_ARG_STRING_MAX_CHARS] + "..."
            return clean
        if isinstance(value, dict):
            return {k: self._sanitize_tool_argument_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._sanitize_tool_argument_value(v) for v in value]
        return value

    def _highlight_formatted_header(self, formatted: str) -> str:
        """Apply the shared jq header style to a formatter's leading JSON line."""
        if not formatted:
            return formatted
        first, separator, rest = formatted.partition("\n")
        try:
            header = json.loads(self._strip_ansi_sequences(first))
        except (json.JSONDecodeError, TypeError):
            return formatted
        if not isinstance(header, dict):
            return formatted
        result_text = header.pop("result", None) if header.get("type") == "tool" else None
        is_error = bool(header.get("isError"))
        highlighted = self._format_json_header(header)
        existing = separator + rest if separator else ""
        if isinstance(result_text, str) and result_text:
            result_block = self._colorize_result("result:", is_error=is_error) + "\n" + self._colorize_result(result_text, is_error=is_error)
            return highlighted + existing + ("\n" if not existing or not existing.endswith("\n") else "") + result_block
        return highlighted + existing

    def _format_tool_result_block(self, header: Dict, result_text: str, is_error: bool = False) -> str:
        label = self._colorize_result("result:", is_error=is_error)
        body = self._colorize_result(result_text, is_error=is_error)
        return self._format_tool_invocation_header(header) + "\n" + label + "\n" + body

    def _format_execution_time(self, payload: dict, pending: Optional[dict] = None) -> Optional[str]:
        """Return execution time string (e.g. 0.12s) from payload or measured start time."""
        seconds: Optional[float] = None

        # Prefer explicit durations if Pi adds them in future versions.
        for key in ("executionTimeSeconds", "durationSeconds", "elapsedSeconds"):
            value = payload.get(key)
            if isinstance(value, (int, float)):
                seconds = float(value)
                break

        if seconds is None:
            for key in ("executionTimeMs", "durationMs", "elapsedMs"):
                value = payload.get(key)
                if isinstance(value, (int, float)):
                    seconds = float(value) / 1000.0
                    break

        if seconds is None and isinstance(pending, dict):
            started_at = pending.get("started_at")
            if isinstance(started_at, (int, float)):
                seconds = max(0.0, time.perf_counter() - started_at)

        if seconds is None:
            return None
        return f"{seconds:.2f}s"

    def expand_model_shorthand(self, model: str) -> str:
        """Resolve shipped and project model shortcuts for Pi."""
        return resolve_model_shortcut(model, self.MODEL_SHORTHANDS, "pi")

    def _detect_prettifier_mode(self, model: str) -> str:
        """Detect which prettifier to use based on the resolved model name.

        Pi CLI always uses its own event protocol (message, turn_end,
        message_update, agent_end, etc.) regardless of the underlying LLM.
        Codex models also use Pi's event protocol but may additionally emit
        native Codex events (agent_reasoning, agent_message, exec_command_end).
        The LIVE prettifier handles both Pi-native and Codex-native events,
        giving real-time streaming output for all model types.
        Claude models still use Pi's event protocol, NOT Claude CLI events.
        """
        model_lower = model.lower()
        if "codex" in model_lower:
            return self.PRETTIFIER_LIVE
        # All non-Codex models (including Claude) use Pi's native event protocol
        return self.PRETTIFIER_PI

    def check_pi_installed(self) -> bool:
        """Check if pi CLI is installed and available."""
        try:
            result = subprocess.run(
                ["which", "pi"],
                capture_output=True,
                text=True,
                check=False,
            )
            return result.returncode == 0
        except Exception:
            return False

    def parse_arguments(self) -> argparse.Namespace:
        """Parse command line arguments for the Pi service."""
        parser = argparse.ArgumentParser(
            description="Pi Agent Service - Wrapper for Pi coding agent headless mode",
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog="""
Examples:
  %(prog)s -p "Review this code" -m :sonnet
  %(prog)s -pp prompt.txt --model openai/gpt-4o
  %(prog)s -p "Refactor module" --thinking high
  %(prog)s -p "Fix bug" --provider anthropic --model claude-sonnet-4-5-20250929
  %(prog)s -p "Audit code" -m :luna --tools read,bash,edit

Model shorthands:
  :pi, :default    -> :gpt -> openai-codex/gpt-5.6-sol
  :sonnet          -> anthropic/claude-sonnet-4-6
  :opus            -> anthropic/claude-opus-4-6
  :haiku           -> anthropic/claude-haiku-4-5-20251001
  :luna            -> openai-codex/gpt-5.6-luna
  :sol             -> openai-codex/gpt-5.6-sol
  :gpt             -> :sol -> openai-codex/gpt-5.6-sol
  :gpt5.5          -> openai-codex/gpt-5.5
  :mini            -> openai-codex/gpt-5.6-terra
  :gpt-5           -> openai/gpt-5
  :gpt-4o          -> openai/gpt-4o
  :o3              -> openai/o3
  :codex           -> openai-codex/gpt-5.3-codex
  :api-codex       -> openai/gpt-5.3-codex
  :codex-spark     -> openai-codex/gpt-5.3-codex-spark
  :api-codex-spark -> openai/gpt-5.3-codex-spark
  :gemini-pro      -> google/gemini-2.5-pro
  :gemini-flash    -> google/gemini-2.5-flash
  :groq            -> groq/llama-4-scout-17b-16e-instruct
  :grok            -> xai/grok-3
            """,
        )

        prompt_group = parser.add_mutually_exclusive_group(required=False)
        prompt_group.add_argument("-p", "--prompt", type=str, help="Prompt text to send to Pi")
        prompt_group.add_argument("-pp", "--prompt-file", type=str, help="Path to file containing the prompt")

        parser.add_argument(
            "--cd",
            type=str,
            default=os.environ.get("PI_PROJECT_PATH", os.getcwd()),
            help="Project path (absolute). Default: current directory (env: PI_PROJECT_PATH)",
        )

        parser.add_argument(
            "-m",
            "--model",
            type=str,
            default=os.environ.get("PI_MODEL", self.DEFAULT_MODEL),
            help=(
                "Model name. Supports shorthands (:pi, :sonnet, :opus, :luna, :sol, :gpt, :gpt5.5, :mini, :gpt-5, :gemini-pro, etc.) "
                f"or provider/model format. Default: {self.DEFAULT_MODEL} (env: PI_MODEL)"
            ),
        )

        parser.add_argument(
            "--provider",
            type=str,
            default=os.environ.get("PI_PROVIDER", ""),
            help="LLM provider (anthropic, openai, google, etc.). Overrides provider in model string. (env: PI_PROVIDER)",
        )

        parser.add_argument(
            "--thinking",
            type=str,
            choices=["off", "minimal", "low", "medium", "high", "xhigh", "max"],
            default=os.environ.get("PI_THINKING", None),
            help="Thinking level (off/minimal/low/medium/high/xhigh/max). (env: PI_THINKING)",
        )

        parser.add_argument(
            "--tools",
            type=str,
            default=os.environ.get("PI_TOOLS", None),
            help="Comma-separated tool list (read,bash,edit,write,grep,find,ls). (env: PI_TOOLS)",
        )

        parser.add_argument(
            "--no-tools",
            action="store_true",
            help="Disable all built-in Pi tools.",
        )

        parser.add_argument(
            "--system-prompt",
            type=str,
            default=os.environ.get("PI_SYSTEM_PROMPT", None),
            help="Replace Pi's system prompt with custom text. (env: PI_SYSTEM_PROMPT)",
        )

        parser.add_argument(
            "--append-system-prompt",
            type=str,
            default=os.environ.get("PI_APPEND_SYSTEM_PROMPT", None),
            help="Append to Pi's default system prompt. (env: PI_APPEND_SYSTEM_PROMPT)",
        )

        parser.add_argument(
            "--no-extensions",
            action="store_true",
            help="Disable Pi extensions.",
        )

        parser.add_argument(
            "--no-skills",
            action="store_true",
            help="Disable Pi skills.",
        )

        parser.add_argument(
            "--no-session",
            action="store_true",
            default=os.environ.get("PI_NO_SESSION", "false").lower() == "true",
            help="Disable session persistence (ephemeral mode). (env: PI_NO_SESSION)",
        )

        parser.add_argument(
            "--resume",
            type=str,
            default=None,
            help="Resume a previous session by session ID. Passed to Pi CLI as --session <id>.",
        )

        parser.add_argument(
            "--fork",
            type=str,
            default=None,
            help="Fork/clone from a previous Pi session ID or path. Passed to Pi CLI as --fork <source>.",
        )

        parser.add_argument(
            "--auto-instruction",
            type=str,
            default=os.environ.get("PI_AUTO_INSTRUCTION", ""),
            help="Instruction text prepended to the prompt. (env: PI_AUTO_INSTRUCTION)",
        )

        parser.add_argument(
            "--additional-args",
            type=str,
            default="",
            help="Space-separated additional pi CLI arguments to append.",
        )

        parser.add_argument(
            "--live",
            action="store_true",
            default=os.environ.get("PI_LIVE", "false").lower() == "true",
            help="Run Pi in interactive/live mode (no --mode json). Uses an auto-exit extension to capture agent_end and shutdown cleanly. (env: PI_LIVE)",
        )

        parser.add_argument(
            "--live-manual",
            action="store_true",
            default=False,
            help="Internal: live session start without an initial prompt (used by continue flows).",
        )

        parser.add_argument(
            "--pretty",
            type=str,
            default=os.environ.get("PI_PRETTY", "true"),
            help="Pretty-print JSON output (true/false). Default: true (env: PI_PRETTY)",
        )

        parser.add_argument(
            "--verbose",
            action="store_true",
            default=os.environ.get("PI_VERBOSE", "false").lower() == "true",
            help="Verbose mode: print command before execution and enable live stream output with real-time text streaming. (env: PI_VERBOSE)",
        )

        return parser.parse_args()

    def read_prompt_file(self, file_path: str) -> str:
        """Read prompt content from a file."""
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except FileNotFoundError:
            print(f"Error: Prompt file not found: {file_path}", file=sys.stderr)
            sys.exit(1)
        except Exception as e:
            print(f"Error reading prompt file: {e}", file=sys.stderr)
            sys.exit(1)

    def build_pi_command(
        self,
        args: argparse.Namespace,
        live_extension_path: Optional[str] = None,
    ) -> Tuple[List[str], Optional[str]]:
        """Construct the Pi CLI command.

        Non-live mode keeps the existing headless JSON contract.
        Live mode switches to Pi interactive defaults (no --mode json, no -p)
        and passes the initial prompt positionally.
        """
        is_live_mode = bool(getattr(args, "live", False))
        cmd = ["pi"]
        if not is_live_mode:
            cmd.extend(["--mode", "json"])

        # Model: an exact provider/model identity is always normalized to the
        # separate Pi arguments. An explicit provider may confirm that identity,
        # but must never rewrite it or leave the combined value as the model id.
        model = self.model_name
        provider = args.provider.strip() if args.provider else ""

        if "/" in model:
            model_provider, bare_model = model.split("/", 1)
            if provider and provider != model_provider:
                raise ValueError(
                    f"Explicit provider {provider!r} conflicts with model identity {model!r}"
                )
            provider = model_provider
            model = bare_model

        if provider:
            cmd.extend(["--provider", provider])

        cmd.extend(["--model", model])

        # Thinking level
        if args.thinking:
            cmd.extend(["--thinking", args.thinking])

        # Tool control
        if args.no_tools:
            cmd.append("--no-tools")
        elif args.tools:
            cmd.extend(["--tools", args.tools])

        # System prompt
        if args.system_prompt:
            cmd.extend(["--system-prompt", args.system_prompt])
        elif args.append_system_prompt:
            cmd.extend(["--append-system-prompt", args.append_system_prompt])

        # Extension/skill control
        if args.no_extensions:
            cmd.append("--no-extensions")
        if args.no_skills:
            cmd.append("--no-skills")

        # Session control. Clone mode must use Pi's native --fork as the
        # durable source of truth; never combine it with ephemeral no-session
        # mode or a direct resume of the source session.
        fork_source = getattr(args, "fork", None)
        if isinstance(fork_source, str):
            fork_source = fork_source.strip()
        if fork_source:
            if args.no_session:
                raise ValueError("Pi --fork clone mode is mutually exclusive with --no-session")
            if getattr(args, "resume", None):
                raise ValueError("Pi --fork clone mode is mutually exclusive with --resume")
            cmd.extend(["--fork", fork_source])
        elif getattr(args, "resume", None):
            cmd.extend(["--session", args.resume])
        elif args.no_session:
            cmd.append("--no-session")

        # Attach live auto-exit extension when requested.
        if is_live_mode and live_extension_path:
            cmd.extend(["-e", live_extension_path])

        # Build prompt with optional auto-instruction
        full_prompt = self.prompt
        live_manual = bool(getattr(args, "live_manual", False))

        if args.auto_instruction:
            if full_prompt:
                full_prompt = f"{args.auto_instruction}\n\n{full_prompt}"
            elif not (is_live_mode and live_manual):
                full_prompt = args.auto_instruction

        stdin_prompt: Optional[str] = None

        if is_live_mode:
            # Live mode normally uses positional prompt input (no -p and no stdin piping).
            # For oversized prompts, keep the beginning in argv for live context and
            # write only the continuation to /tmp/yylo/{timestamp}-prompt.md.
            if full_prompt:
                if self._is_prompt_oversized(full_prompt):
                    full_prompt = self._split_live_prompt_to_file(full_prompt)
                cmd.append(full_prompt)

            # Additional raw arguments should still be honored; place before the
            # positional prompt so flags remain flags.
            if args.additional_args:
                extra = args.additional_args.strip().split()
                if extra:
                    if full_prompt:
                        cmd = cmd[:-1] + extra + [cmd[-1]]
                    else:
                        cmd.extend(extra)
            return cmd, None

        cmd.append("-p")
        stdin_prompt = full_prompt

        # Additional raw arguments
        if args.additional_args:
            extra = args.additional_args.strip().split()
            if extra:
                cmd.extend(extra)

        return cmd, stdin_prompt

    # ── Codex prettifier helpers ──────────────────────────────────────────

    def _first_nonempty_str(self, *values) -> str:
        """Return the first non-empty string value."""
        for val in values:
            if isinstance(val, str) and val != "":
                return val
        return ""

    def _extract_content_text(self, payload: dict) -> str:
        """Join text-like fields from content arrays (item.* schema)."""
        content = payload.get("content") if isinstance(payload, dict) else None
        parts: List[str] = []
        if isinstance(content, list):
            for entry in content:
                if not isinstance(entry, dict):
                    continue
                text_val = (
                    entry.get("text")
                    or entry.get("message")
                    or entry.get("output_text")
                    or entry.get("input_text")
                )
                if isinstance(text_val, str) and text_val != "":
                    parts.append(text_val)
        return "\n".join(parts) if parts else ""

    def _extract_command_output_text(self, payload: dict) -> str:
        """Extract aggregated/command output from various item.* layouts."""
        if not isinstance(payload, dict):
            return ""
        result = payload.get("result") if isinstance(payload.get("result"), dict) else None
        content_text = self._extract_content_text(payload)
        return self._first_nonempty_str(
            payload.get("aggregated_output"),
            payload.get("output"),
            payload.get("formatted_output"),
            result.get("aggregated_output") if result else None,
            result.get("output") if result else None,
            result.get("formatted_output") if result else None,
            content_text,
        )

    def _extract_reasoning_text(self, payload: dict) -> str:
        """Extract reasoning text from legacy and item.* schemas."""
        if not isinstance(payload, dict):
            return ""
        reasoning_obj = payload.get("reasoning") if isinstance(payload.get("reasoning"), dict) else None
        result_obj = payload.get("result") if isinstance(payload.get("result"), dict) else None
        content_text = self._extract_content_text(payload)
        return self._first_nonempty_str(
            payload.get("text"),
            payload.get("reasoning_text"),
            reasoning_obj.get("text") if reasoning_obj else None,
            result_obj.get("text") if result_obj else None,
            content_text,
        )

    def _extract_message_text_codex(self, payload: dict) -> str:
        """Extract final/assistant message text from item.* schemas."""
        if not isinstance(payload, dict):
            return ""
        result_obj = payload.get("result") if isinstance(payload.get("result"), dict) else None
        content_text = self._extract_content_text(payload)
        return self._first_nonempty_str(
            payload.get("message"),
            payload.get("text"),
            payload.get("final"),
            result_obj.get("message") if result_obj else None,
            result_obj.get("text") if result_obj else None,
            content_text,
        )

    def _normalize_codex_event(self, obj_dict: dict):
        """Normalize legacy (msg-based) and new item.* schemas into a common tuple."""
        msg = obj_dict.get("msg") if isinstance(obj_dict.get("msg"), dict) else {}
        outer_type = (obj_dict.get("type") or "").strip()
        item = obj_dict.get("item") if isinstance(obj_dict.get("item"), dict) else None

        msg_type = (msg.get("type") or "").strip() if isinstance(msg, dict) else ""
        payload = msg if isinstance(msg, dict) else {}

        if not msg_type and item is not None:
            msg_type = (item.get("type") or "").strip() or outer_type
            payload = item
        elif not msg_type:
            msg_type = outer_type

        return msg_type, payload, outer_type

    def _normalize_item_id(self, payload: dict, outer_type: str) -> Optional[str]:
        """Prefer existing id on item.* payloads; otherwise synthesize sequential item_{n}."""
        item_id = payload.get("id") if isinstance(payload, dict) else None
        if isinstance(item_id, str) and item_id.strip():
            parsed = self._parse_item_number(item_id)
            if parsed is not None and parsed + 1 > self._item_counter:
                self._item_counter = parsed + 1
            return item_id.strip()

        if isinstance(outer_type, str) and outer_type.startswith("item."):
            generated = f"item_{self._item_counter}"
            self._item_counter += 1
            return generated

        return None

    def _parse_item_number(self, item_id: str) -> Optional[int]:
        """Return numeric component from item_{n} ids or None if unparseable."""
        if not isinstance(item_id, str):
            return None
        item_id = item_id.strip()
        if not item_id.startswith("item_"):
            return None
        try:
            return int(item_id.split("item_", 1)[1])
        except Exception:
            return None

    def _strip_thinking_signature(self, content_list: list) -> list:
        """Remove thinkingSignature, textSignature, and encrypted_content from content items."""
        if not isinstance(content_list, list):
            return content_list
        for item in content_list:
            if isinstance(item, dict):
                item.pop("thinkingSignature", None)
                item.pop("textSignature", None)
                item.pop("encrypted_content", None)
        return content_list

    def _sanitize_codex_event(self, obj: dict, strip_metadata: bool = True) -> dict:
        """Deep-sanitize a Codex event: strip thinkingSignature and encrypted_content
        from any nested content arrays, and optionally remove metadata keys from
        nested message dicts.

        Handles Pi-wrapped events like message_update which nest messages under
        'partial', 'message', 'assistantMessageEvent', etc.
        """
        if not isinstance(obj, dict):
            return obj

        # Strip thinkingSignature from top-level content
        if isinstance(obj.get("content"), list):
            self._strip_thinking_signature(obj["content"])

        # Remove encrypted signatures and encrypted_content anywhere
        obj.pop("encrypted_content", None)
        obj.pop("textSignature", None)

        # Remove metadata keys from this level
        if strip_metadata:
            for mk in self._codex_metadata_keys:
                obj.pop(mk, None)

        # Recurse into known nested message containers
        for nested_key in ("partial", "message", "assistantMessageEvent"):
            nested = obj.get(nested_key)
            if isinstance(nested, dict):
                self._sanitize_codex_event(nested, strip_metadata)

        # Recurse into content arrays to strip encrypted_content from items
        content = obj.get("content")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    item.pop("encrypted_content", None)
                    item.pop("thinkingSignature", None)
                    # If thinkingSignature was a string containing encrypted_content, it's already removed
                    # Also recurse deeper if needed
                    self._sanitize_codex_event(item, strip_metadata=False)

        return obj

    def _truncate_tool_result_text(self, text: str) -> str:
        """Show a bounded head and tail with exact omitted-middle counts."""
        if not isinstance(text, str):
            return text
        display_text = self._strip_ansi_sequences(text.replace("\\n", "\n").replace("\\t", "\t"))
        lines = display_text.splitlines()
        if display_text.endswith(("\n", "\r")):
            # splitlines intentionally avoids inventing an empty final display line.
            pass
        if not lines and display_text:
            lines = [display_text]
        head_lines = max(0, self._codex_tool_result_max_lines)
        tail_lines = max(0, self._tool_result_tail_lines)
        if len(lines) <= head_lines + tail_lines:
            return display_text

        omitted = lines[head_lines:len(lines) - tail_lines if tail_lines else len(lines)]
        omitted_text = "\n".join(omitted)
        marker = f"[{len(omitted)} lines, {len(omitted_text)} characters truncated]"
        visible = [*lines[:head_lines], marker]
        if tail_lines:
            visible.extend(lines[-tail_lines:])
        return "\n".join(visible)

    def _configure_headless_ui(self) -> Optional[str]:
        raw = os.environ.get("HEADLESS_UI_TURN_COST_DISPLAY_THRESHOLD_USD", "0.5").strip()
        try:
            value = float(raw)
        except ValueError:
            return "HEADLESS_UI_TURN_COST_DISPLAY_THRESHOLD_USD must be a finite number >= 0"
        if not math.isfinite(value) or value < 0:
            return "HEADLESS_UI_TURN_COST_DISPLAY_THRESHOLD_USD must be a finite number >= 0"
        self._turn_cost_display_threshold_usd = value
        return None

    def _add_visible_turn_cost(self, header: Dict, event: dict) -> None:
        cost = self._extract_total_cost_usd(event)
        if cost is not None and cost > self._turn_cost_display_threshold_usd:
            header["cost_usd"] = cost

    def _is_codex_final_message(self, parsed: dict) -> bool:
        """Detect if this is the final assistant message (contains type=text content or stopReason=stop)."""
        if not isinstance(parsed, dict):
            return False
        if parsed.get("stopReason") == "stop":
            return True
        content = parsed.get("content")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    return True
        return False

    def _format_pi_codex_message(self, parsed: dict) -> Optional[str]:
        """Format a Pi-wrapped Codex message (role-based with content arrays).

        Handles:
        - Stripping thinkingSignature from thinking content
        - Truncating toolResult text to configured max lines
        - Hiding metadata keys from intermediate assistant messages
        """
        if not isinstance(parsed, dict):
            return None

        role = parsed.get("role", "")
        now = datetime.now().strftime("%I:%M:%S %p")
        self.message_counter += 1

        # --- toolResult role: truncate text content ---
        if role == "toolResult":
            header: Dict = {
                "type": "toolResult",
                "datetime": now,
                "counter": f"#{self.message_counter}",
                "toolName": parsed.get("toolName", ""),
            }
            is_error = parsed.get("isError", False)
            if is_error:
                header["isError"] = True

            content = parsed.get("content")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        text_val = item.get("text", "")
                        truncated = self._truncate_tool_result_text(text_val)
                        if "\n" in truncated:
                            return json.dumps(header, ensure_ascii=False) + "\ncontent:\n" + truncated
                        header["content"] = truncated
                        return json.dumps(header, ensure_ascii=False)

            return json.dumps(header, ensure_ascii=False)

        # --- assistant role: strip thinkingSignature and manage metadata ---
        if role == "assistant":
            content = parsed.get("content")
            if isinstance(content, list):
                self._strip_thinking_signature(content)

            is_final = self._is_codex_final_message(parsed)
            is_first = not self._codex_first_assistant_seen
            self._codex_first_assistant_seen = True

            show_metadata = is_first or is_final

            # Build display object
            display: Dict = {}
            for key, value in parsed.items():
                if not show_metadata and key in self._codex_metadata_keys:
                    continue
                display[key] = value

            # Add datetime and counter
            display["datetime"] = now
            display["counter"] = f"#{self.message_counter}"

            # Extract main content for pretty display
            if isinstance(content, list):
                parts = []
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "thinking":
                            thinking_text = item.get("thinking", "")
                            if thinking_text:
                                parts.append(f"[thinking] {thinking_text}")
                        elif item.get("type") == "toolCall":
                            name = item.get("name", "")
                            args = item.get("arguments", {})
                            if isinstance(args, dict):
                                cmd = args.get("command", "")
                                if isinstance(cmd, str) and cmd:
                                    parts.append(f"[toolCall] {name}: {self._sanitize_tool_argument_value(cmd)}")
                                else:
                                    args_clean = self._sanitize_tool_argument_value(args)
                                    args_str = json.dumps(args_clean, ensure_ascii=False)
                                    parts.append(f"[toolCall] {name}: {args_str}")
                            else:
                                parts.append(f"[toolCall] {name}")
                        elif item.get("type") == "text":
                            text_val = item.get("text", "")
                            if text_val:
                                parts.append(text_val)

                if parts:
                    combined = "\n".join(parts)
                    header_obj: Dict = {"type": "assistant", "datetime": now, "counter": f"#{self.message_counter}"}
                    if show_metadata:
                        for mk in ("api", "provider", "model", "stopReason"):
                            if mk in parsed:
                                header_obj[mk] = parsed[mk]
                        if "usage" in parsed and is_final:
                            header_obj["usage"] = parsed["usage"]
                    if "\n" in combined:
                        return json.dumps(header_obj, ensure_ascii=False) + "\ncontent:\n" + combined
                    header_obj["content"] = combined
                    return json.dumps(header_obj, ensure_ascii=False)

            # Fallback: dump the filtered display object
            return json.dumps(display, ensure_ascii=False)

        return None

    # Event subtypes to suppress in message_update (streaming deltas are noisy)
    _CODEX_HIDDEN_MESSAGE_UPDATE_SUBTYPES = {
        "text_delta", "text_start",
        "thinking_delta", "thinking_start",
        "toolcall_delta", "toolcall_start",
    }

    def _format_pi_codex_event(self, parsed: dict) -> Optional[str]:
        """Format Pi-wrapped events when in Codex prettifier mode.

        Handles Pi event types (message_update, turn_end, message_start, etc.)
        that wrap Codex-style content. Returns formatted string, empty string
        to suppress, or None if this method doesn't handle the event type.
        """
        event_type = parsed.get("type", "")
        if not event_type:
            return None

        now = datetime.now().strftime("%I:%M:%S %p")

        # --- message_update: filter by assistantMessageEvent subtype ---
        if event_type == "message_update":
            ame = parsed.get("assistantMessageEvent", {})
            if isinstance(ame, dict):
                ame_type = ame.get("type", "")

                # Suppress noisy streaming delta/start events
                if ame_type in self._CODEX_HIDDEN_MESSAGE_UPDATE_SUBTYPES:
                    return ""  # suppress

                # text_end: show the complete text content
                if ame_type == "text_end":
                    self.message_counter += 1
                    content_text = ame.get("content", "")
                    header: Dict = {
                        "type": "text_end",
                        "datetime": now,
                        "counter": f"#{self.message_counter}",
                    }
                    if isinstance(content_text, str) and content_text.strip():
                        return self._format_json_header(header) + "\ncontent:\n" + self._style_assistant(content_text)
                    return self._format_json_header(header)

                # thinking_end: show the final thinking summary
                if ame_type == "thinking_end":
                    self.message_counter += 1
                    thinking_text = ame.get("content", "")
                    header = {
                        "type": "thinking_end",
                        "datetime": now,
                        "counter": f"#{self.message_counter}",
                    }
                    if isinstance(thinking_text, str) and thinking_text.strip():
                        return self._format_json_header(header) + "\nthinking:\n" + self._style_thinking(thinking_text)
                    return self._format_json_header(header)

                # toolcall_end: buffer for grouping with tool_execution_end
                if ame_type == "toolcall_end":
                    tool_call = ame.get("toolCall", {})
                    if self._buffer_tool_call_end(tool_call, now):
                        return ""  # suppress — will emit combined event on tool_execution_end
                    # No toolCallId — fallback to original format
                    self.message_counter += 1
                    header = {
                        "type": "toolcall_end",
                        "datetime": now,
                        "counter": f"#{self.message_counter}",
                    }
                    if isinstance(tool_call, dict):
                        header["tool"] = tool_call.get("name", "")
                        args = tool_call.get("arguments", {})
                        if isinstance(args, dict):
                            cmd = args.get("command", "")
                            if isinstance(cmd, str) and cmd:
                                header["command"] = self._sanitize_tool_argument_value(cmd)
                            else:
                                header["args"] = self._sanitize_tool_argument_value(args)
                        elif isinstance(args, str) and args.strip():
                            header["args"] = self._sanitize_tool_argument_value(args)
                    return self._format_tool_invocation_header(header)

            # Other message_update subtypes: suppress by default
            return ""

        # --- turn_end: metadata only (text already shown by text_end/thinking_end/toolcall_end) ---
        if event_type == "turn_end":
            self.message_counter += 1
            header = {
                "type": "turn_end",
                "datetime": now,
                "counter": f"#{self.message_counter}",
            }
            tool_results = parsed.get("toolResults")
            if isinstance(tool_results, list):
                header["tool_results_count"] = len(tool_results)
            self._add_visible_turn_cost(header, parsed)
            return self._format_json_header(header)

        # --- message_start: minimal header (no counter — only *_end events get counters) ---
        if event_type == "message_start":
            message = parsed.get("message", {})
            header = {
                "type": "message_start",
                "datetime": now,
            }
            if isinstance(message, dict):
                role = message.get("role")
                if role:
                    header["role"] = role
            return json.dumps(header, ensure_ascii=False)

        # --- message_end: metadata only (text already shown by text_end/thinking_end/toolcall_end) ---
        if event_type == "message_end":
            self.message_counter += 1
            header = {
                "type": "message_end",
                "datetime": now,
                "counter": f"#{self.message_counter}",
            }
            return json.dumps(header, ensure_ascii=False)

        # --- tool_execution_start: always suppress, buffer args ---
        if event_type == "tool_execution_start":
            self._buffer_exec_start(parsed)
            self._in_tool_execution = True
            return ""  # suppress

        # --- tool_execution_end: combine with buffered data ---
        if event_type == "tool_execution_end":
            self._in_tool_execution = False
            tool_call_id = parsed.get("toolCallId")
            self._cancel_tool_running(tool_call_id)

            pending_tool = self._pending_tool_calls.pop(tool_call_id, None) if tool_call_id else None
            pending_exec = self._pending_exec_starts.pop(tool_call_id, None) if tool_call_id else None
            if pending_tool and pending_exec and "started_at" in pending_exec:
                pending_tool["started_at"] = pending_exec["started_at"]
            pending = pending_tool or pending_exec

            if pending:
                return self._build_combined_tool_event(pending, parsed, now)

            # No buffered data — minimal fallback
            self.message_counter += 1
            header = {
                "type": "tool",
                "datetime": now,
                "counter": f"#{self.message_counter}",
                "tool": parsed.get("toolName", ""),
            }
            execution_time = self._format_execution_time(parsed)
            if execution_time:
                header["execution_time"] = execution_time

            is_error = parsed.get("isError", False)
            if is_error:
                header["isError"] = True

            result_val = parsed.get("result")
            colorize_error = self._color_enabled() and bool(is_error)

            if isinstance(result_val, str) and result_val.strip():
                truncated = self._truncate_tool_result_text(result_val)
                if "\n" in truncated or colorize_error:
                    label = "result:"
                    colored = self._colorize_result(truncated, is_error=bool(is_error))
                    if colorize_error:
                        label = self._colorize_result(label, is_error=True)
                    return self._format_tool_invocation_header(header) + "\n" + label + "\n" + colored
                header["result"] = truncated
                return self._format_tool_invocation_header(header)

            if isinstance(result_val, dict):
                result_content = result_val.get("content")
                if isinstance(result_content, list):
                    for rc_item in result_content:
                        if isinstance(rc_item, dict) and rc_item.get("type") == "text":
                            text = rc_item.get("text", "")
                            truncated = self._truncate_tool_result_text(text)
                            if "\n" in truncated or colorize_error:
                                label = "result:"
                                colored = self._colorize_result(truncated, is_error=bool(is_error))
                                if colorize_error:
                                    label = self._colorize_result(label, is_error=True)
                                return self._format_tool_invocation_header(header) + "\n" + label + "\n" + colored
                            header["result"] = truncated
                            return self._format_tool_invocation_header(header)

                result_json = self._strip_ansi_sequences(json.dumps(result_val, ensure_ascii=False))
                if "\n" in result_json or colorize_error:
                    label = "result:"
                    colored = self._colorize_result(result_json, is_error=bool(is_error))
                    if colorize_error:
                        label = self._colorize_result(label, is_error=True)
                    return self._format_tool_invocation_header(header) + "\n" + label + "\n" + colored
                header["result"] = result_json
                return self._format_tool_invocation_header(header)

            if isinstance(result_val, list):
                result_json = self._strip_ansi_sequences(json.dumps(result_val, ensure_ascii=False))
                if "\n" in result_json or colorize_error:
                    label = "result:"
                    colored = self._colorize_result(result_json, is_error=bool(is_error))
                    if colorize_error:
                        label = self._colorize_result(label, is_error=True)
                    return self._format_tool_invocation_header(header) + "\n" + label + "\n" + colored
                header["result"] = result_json
                return self._format_tool_invocation_header(header)

            return self._format_tool_invocation_header(header)

        # --- turn_start: suppress (no user-visible value) ---
        if event_type == "turn_start":
            return ""

        # --- agent_start: simple header (no counter — only *_end events get counters) ---
        if event_type == "agent_start":
            return json.dumps({
                "type": event_type,
                "datetime": now,
            }, ensure_ascii=False)

        # --- agent_end: capture and show summary ---
        if event_type == "agent_end":
            self.message_counter += 1
            header = {
                "type": "agent_end",
                "datetime": now,
                "counter": f"#{self.message_counter}",
            }
            messages = parsed.get("messages")
            if isinstance(messages, list):
                header["message_count"] = len(messages)
            total_cost_usd = self._extract_total_cost_usd(parsed)
            if total_cost_usd is not None:
                header["total_cost_usd"] = total_cost_usd
            return json.dumps(header, ensure_ascii=False)

        # Not a Pi-wrapped event type we handle
        return None

    def _format_event_pretty_codex(self, payload: dict) -> Optional[str]:
        """Format a Codex-schema JSON event for human-readable output."""
        try:
            msg_type, msg_payload, outer_type = self._normalize_codex_event(payload)
            item_id = self._normalize_item_id(msg_payload, outer_type)

            now = datetime.now().strftime("%I:%M:%S %p")
            self.message_counter += 1
            header_type = (outer_type or msg_type).strip()
            base_type = header_type or msg_type or "message"

            def make_header(type_value: str):
                hdr: Dict = {"type": type_value, "datetime": now, "counter": f"#{self.message_counter}"}
                if item_id:
                    hdr["id"] = item_id
                if outer_type and msg_type and outer_type != msg_type:
                    hdr["item_type"] = msg_type
                return hdr

            header = make_header(base_type)

            if isinstance(msg_payload, dict):
                if item_id and "id" not in msg_payload:
                    msg_payload["id"] = item_id
                if msg_payload.get("command"):
                    header["command"] = msg_payload.get("command")
                if msg_payload.get("status"):
                    header["status"] = msg_payload.get("status")
                if msg_payload.get("state") and not header.get("status"):
                    header["status"] = msg_payload.get("state")

            # agent_reasoning
            if msg_type in {"agent_reasoning", "reasoning"}:
                content = self._extract_reasoning_text(msg_payload)
                header = make_header(header_type or msg_type)
                if "\n" in content:
                    return json.dumps(header, ensure_ascii=False) + "\ntext:\n" + content
                header["text"] = content
                return json.dumps(header, ensure_ascii=False)

            # agent_message / assistant
            if msg_type in {"agent_message", "message", "assistant_message", "assistant"}:
                content = self._extract_message_text_codex(msg_payload)
                header = make_header(header_type or msg_type)
                if "\n" in content:
                    return json.dumps(header, ensure_ascii=False) + "\nmessage:\n" + content
                if content != "":
                    header["message"] = content
                    return json.dumps(header, ensure_ascii=False)
                if header_type:
                    return json.dumps(header, ensure_ascii=False)

            # exec_command_end
            if msg_type == "exec_command_end":
                formatted_output = msg_payload.get("formatted_output", "") if isinstance(msg_payload, dict) else ""
                header = {"type": msg_type, "datetime": now}
                if "\n" in formatted_output:
                    return json.dumps(header, ensure_ascii=False) + "\nformatted_output:\n" + formatted_output
                header["formatted_output"] = formatted_output
                return json.dumps(header, ensure_ascii=False)

            # command_execution
            if msg_type == "command_execution":
                aggregated_output = self._extract_command_output_text(msg_payload)
                if "\n" in aggregated_output:
                    return json.dumps(header, ensure_ascii=False) + "\naggregated_output:\n" + aggregated_output
                if aggregated_output:
                    header["aggregated_output"] = aggregated_output
                    return json.dumps(header, ensure_ascii=False)
                if header_type:
                    return json.dumps(header, ensure_ascii=False)

            return None
        except Exception:
            return None

    # ── Claude prettifier ─────────────────────────────────────────────────

    def _format_event_pretty_claude(self, json_line: str) -> Optional[str]:
        """Format a Claude-schema JSON event for human-readable output."""
        try:
            data = json.loads(json_line) if isinstance(json_line, str) else json_line
            self.message_counter += 1
            now = datetime.now().strftime("%I:%M:%S %p")

            if data.get("type") == "user":
                message = data.get("message", {})
                content_list = message.get("content", [])
                text_content = ""
                for item in content_list:
                    if isinstance(item, dict) and item.get("type") == "text":
                        text_content = item.get("text", "")
                        break

                if self.user_message_truncate != -1:
                    lines = text_content.split('\n')
                    if len(lines) > self.user_message_truncate:
                        text_content = '\n'.join(lines[:self.user_message_truncate]) + '\n[Truncated...]'

                metadata: Dict = {"type": "user", "datetime": now, "counter": f"#{self.message_counter}"}
                if '\n' in text_content:
                    return json.dumps(metadata, ensure_ascii=False) + "\ncontent:\n" + text_content
                metadata["content"] = text_content
                return json.dumps(metadata, ensure_ascii=False)

            elif data.get("type") == "progress":
                progress_data = data.get("data", {})
                progress_type = progress_data.get("type", "")

                if progress_type == "hook_progress":
                    return None

                if progress_type == "bash_progress":
                    output_text = progress_data.get("output", "")
                    elapsed_time = progress_data.get("elapsedTimeSeconds", 0)
                    total_lines = progress_data.get("totalLines", 0)
                    simplified: Dict = {
                        "type": "progress", "progress_type": "bash_progress",
                        "datetime": now, "counter": f"#{self.message_counter}",
                        "elapsed": f"{elapsed_time}s", "lines": total_lines,
                    }
                    if '\n' in output_text:
                        return json.dumps(simplified, ensure_ascii=False) + "\n[Progress] output:\n" + output_text
                    simplified["output"] = output_text
                    return f"[Progress] {json.dumps(simplified, ensure_ascii=False)}"

                return json.dumps({
                    "type": "progress", "progress_type": progress_type,
                    "datetime": now, "counter": f"#{self.message_counter}",
                    "data": progress_data,
                }, ensure_ascii=False)

            elif data.get("type") == "assistant":
                message = data.get("message", {})
                content_list = message.get("content", [])
                text_content = ""
                tool_use_data = None

                for item in content_list:
                    if isinstance(item, dict):
                        if item.get("type") == "text":
                            text_content = item.get("text", "")
                            break
                        elif item.get("type") == "tool_use":
                            tool_use_data = {"name": item.get("name", ""), "input": item.get("input", {})}
                            break

                metadata = {"type": "assistant", "datetime": now, "counter": f"#{self.message_counter}"}

                if tool_use_data:
                    tool_input = tool_use_data.get("input", {})
                    prompt_field = tool_input.get("prompt", "")
                    if isinstance(prompt_field, str) and '\n' in prompt_field:
                        tool_use_copy = {
                            "name": tool_use_data.get("name", ""),
                            "input": {k: v for k, v in tool_input.items() if k != "prompt"},
                        }
                        metadata["tool_use"] = tool_use_copy
                        return json.dumps(metadata, ensure_ascii=False) + "\nprompt:\n" + prompt_field
                    metadata["tool_use"] = tool_use_data
                    return json.dumps(metadata, ensure_ascii=False)
                else:
                    if '\n' in text_content:
                        return json.dumps(metadata, ensure_ascii=False) + "\ncontent:\n" + text_content
                    metadata["content"] = text_content
                    return json.dumps(metadata, ensure_ascii=False)

            else:
                message = data.get("message", {})
                content_list = message.get("content", [])
                if content_list and isinstance(content_list, list) and len(content_list) > 0:
                    nested_item = content_list[0]
                    if isinstance(nested_item, dict) and nested_item.get("type") in ["tool_result"]:
                        flattened: Dict = {"datetime": now, "counter": f"#{self.message_counter}"}
                        if "tool_use_id" in nested_item:
                            flattened["tool_use_id"] = nested_item["tool_use_id"]
                        flattened["type"] = nested_item["type"]
                        nested_content = nested_item.get("content", "")
                        if isinstance(nested_content, str) and '\n' in nested_content:
                            return json.dumps(flattened, ensure_ascii=False) + "\ncontent:\n" + nested_content
                        flattened["content"] = nested_content
                        return json.dumps(flattened, ensure_ascii=False)

                output: Dict = {"datetime": now, "counter": f"#{self.message_counter}", **data}
                if "result" in output and isinstance(output["result"], str) and '\n' in output["result"]:
                    result_value = output.pop("result")
                    return json.dumps(output, ensure_ascii=False) + "\nresult:\n" + result_value
                return json.dumps(output, ensure_ascii=False)

        except json.JSONDecodeError:
            return json_line if isinstance(json_line, str) else None
        except Exception:
            return json_line if isinstance(json_line, str) else None

    # ── Pi prettifier helpers ─────────────────────────────────────────────

    def _extract_text_from_message(self, message: dict) -> str:
        """Extract human-readable text from a Pi message object."""
        if not isinstance(message, dict):
            return ""

        # Direct text/content fields
        for field in ("text", "content", "message", "response", "output"):
            val = message.get(field)
            if isinstance(val, str) and val.strip():
                return val

        # content array (Claude-style)
        content = message.get("content")
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text") or item.get("content")
                    if isinstance(text, str) and text.strip():
                        parts.append(text)
                elif isinstance(item, str) and item.strip():
                    parts.append(item)
            if parts:
                return "\n".join(parts)

        return ""

    def _buffer_tool_call_end(self, tool_call: dict, now: str) -> bool:
        """Buffer toolcall_end info for grouping with tool_execution_end.

        Returns True if successfully buffered (caller should suppress output),
        False if no toolCallId present (caller should emit normally).
        """
        tc_id = tool_call.get("toolCallId", "") if isinstance(tool_call, dict) else ""
        if not tc_id:
            return False

        pending: Dict = {"tool": tool_call.get("name", ""), "datetime": now}
        args = tool_call.get("arguments", {})

        if isinstance(args, dict):
            cmd = args.get("command", "")
            if isinstance(cmd, str) and cmd:
                pending["command"] = self._sanitize_tool_argument_value(cmd)
            else:
                pending["args"] = self._sanitize_tool_argument_value(args)
        elif isinstance(args, str) and args.strip():
            pending["args"] = self._sanitize_tool_argument_value(args)

        self._pending_tool_calls[tc_id] = pending
        return True

    def _schedule_tool_running(self, tool_call_id: str, pending: dict) -> None:
        """Show a pending tool only when it remains active for 500 ms."""
        if not tool_call_id:
            return

        def emit_running() -> None:
            with self._tool_running_lock:
                if self._pending_tool_running_timers.pop(tool_call_id, None) is None:
                    return
            header: Dict = {
                "type": "tool",
                "status": "running",
                "tool": pending.get("tool", ""),
            }
            if "command" in pending:
                header["command"] = pending["command"]
            elif "args" in pending:
                header["args"] = pending["args"]
            print(self._format_tool_invocation_header(header), flush=True)

        timer = threading.Timer(0.5, emit_running)
        timer.daemon = True
        with self._tool_running_lock:
            previous = self._pending_tool_running_timers.pop(tool_call_id, None)
            if previous:
                previous.cancel()
            self._pending_tool_running_timers[tool_call_id] = timer
        timer.start()

    def _cancel_tool_running(self, tool_call_id: object) -> None:
        if not isinstance(tool_call_id, str) or not tool_call_id:
            return
        with self._tool_running_lock:
            timer = self._pending_tool_running_timers.pop(tool_call_id, None)
        if timer:
            timer.cancel()

    def _cancel_all_tool_running(self) -> None:
        with self._tool_running_lock:
            timers = list(self._pending_tool_running_timers.values())
            self._pending_tool_running_timers.clear()
        for timer in timers:
            timer.cancel()

    def _buffer_exec_start(self, payload: dict) -> None:
        """Buffer tool_execution_start data for tool_execution_end fallback + timing."""
        tc_id = payload.get("toolCallId", "")
        if not tc_id:
            return

        pending: Dict = {
            "tool": payload.get("toolName", ""),
            "started_at": time.perf_counter(),
        }
        args_val = payload.get("args")
        if isinstance(args_val, dict):
            cmd = args_val.get("command", "")
            if isinstance(cmd, str) and cmd:
                pending["command"] = self._sanitize_tool_argument_value(cmd)
            else:
                pending["args"] = self._sanitize_tool_argument_value(args_val)
        elif isinstance(args_val, str) and args_val.strip():
            pending["args"] = self._sanitize_tool_argument_value(args_val)

        self._pending_exec_starts[tc_id] = pending

    def _build_combined_tool_event(self, pending: dict, payload: dict, now: str) -> str:
        """Build a combined 'tool' event from buffered toolcall_end + tool_execution_end."""
        self._cancel_tool_running(payload.get("toolCallId"))
        self.message_counter += 1
        header: Dict = {
            "type": "tool",
            "status": "done",
            "datetime": now,
            "counter": f"#{self.message_counter}",
            "tool": pending.get("tool", payload.get("toolName", "")),
        }

        # Args from buffered toolcall/tool_execution_start
        if "command" in pending:
            header["command"] = pending["command"]
        elif "args" in pending:
            header["args"] = pending["args"]

        # Execution time (source of truth: tool_execution_start -> tool_execution_end)
        execution_time = self._format_execution_time(payload, pending)
        if execution_time:
            header["execution_time"] = execution_time

        is_error = payload.get("isError", False)
        if is_error:
            header["isError"] = True

        # Result extraction (handles string, dict with content array, and list)
        result_val = payload.get("result")
        result_text = None
        if isinstance(result_val, str) and result_val.strip():
            result_text = self._truncate_tool_result_text(result_val)
        elif isinstance(result_val, dict):
            result_content = result_val.get("content")
            if isinstance(result_content, list):
                for rc_item in result_content:
                    if isinstance(rc_item, dict) and rc_item.get("type") == "text":
                        result_text = self._truncate_tool_result_text(rc_item.get("text", ""))
                        break
            if result_text is None:
                result_text = self._strip_ansi_sequences(json.dumps(result_val, ensure_ascii=False))
        elif isinstance(result_val, list):
            result_text = self._strip_ansi_sequences(json.dumps(result_val, ensure_ascii=False))

        if result_text:
            return self._format_tool_result_block(header, result_text, is_error=bool(is_error))

        return self._format_tool_invocation_header(header)

    def _format_event_pretty(self, payload: dict) -> Optional[str]:
        """
        Format a Pi JSON streaming event for human-readable output.
        Returns formatted string or None to skip the event.
        """
        try:
            event_type = payload.get("type", "")
            now = datetime.now().strftime("%I:%M:%S %p")

            # Counter is only added to *_end events (below, per-branch)
            header: Dict = {
                "type": event_type,
                "datetime": now,
            }

            # --- Session header (no counter) ---
            if event_type == "session":
                header["version"] = payload.get("version")
                header["id"] = payload.get("id")
                return json.dumps(header, ensure_ascii=False)

            # --- turn_start: suppress (no user-visible value) ---
            if event_type == "turn_start":
                return None

            # --- agent_start: simple header (no counter) ---
            if event_type == "agent_start":
                return json.dumps(header, ensure_ascii=False)

            if event_type == "agent_end":
                self.message_counter += 1
                header["counter"] = f"#{self.message_counter}"
                messages = payload.get("messages")
                if isinstance(messages, list):
                    header["message_count"] = len(messages)
                total_cost_usd = self._extract_total_cost_usd(payload)
                if total_cost_usd is not None:
                    header["total_cost_usd"] = total_cost_usd
                return json.dumps(header, ensure_ascii=False)

            if event_type == "turn_end":
                self.message_counter += 1
                header["counter"] = f"#{self.message_counter}"
                tool_results = payload.get("toolResults")
                if isinstance(tool_results, list):
                    header["tool_results_count"] = len(tool_results)
                self._add_visible_turn_cost(header, payload)
                # Skip message text - already displayed by text_end/thinking_end/toolcall_end
                return self._format_json_header(header)

            # --- Message events (assistant streaming) ---
            if event_type == "message_start":
                message = payload.get("message", {})
                role = message.get("role") if isinstance(message, dict) else None
                if role:
                    header["role"] = role
                return json.dumps(header, ensure_ascii=False)

            if event_type == "message_update":
                # Check for noisy streaming sub-events and suppress them
                ame = payload.get("assistantMessageEvent", {})
                ame_type = ame.get("type", "") if isinstance(ame, dict) else ""
                event_subtype = payload.get("event", ame_type)
                if event_subtype in self._PI_HIDDEN_MESSAGE_UPDATE_EVENTS:
                    return None  # Suppress noisy streaming deltas

                # toolcall_end: buffer for grouping with tool_execution_end
                if isinstance(ame, dict) and ame_type == "toolcall_end":
                    tool_call = ame.get("toolCall", {})
                    if self._buffer_tool_call_end(tool_call, now):
                        return None  # suppress — will emit combined event on tool_execution_end
                    # No toolCallId — fallback to original format
                    self.message_counter += 1
                    header["counter"] = f"#{self.message_counter}"
                    header["event"] = ame_type
                    if isinstance(tool_call, dict):
                        header["tool"] = tool_call.get("name", "")
                        args = tool_call.get("arguments", {})
                        if isinstance(args, dict):
                            cmd = args.get("command", "")
                            if isinstance(cmd, str) and cmd:
                                header["command"] = self._sanitize_tool_argument_value(cmd)
                            else:
                                header["args"] = self._sanitize_tool_argument_value(args)
                        elif isinstance(args, str) and args.strip():
                            header["args"] = self._sanitize_tool_argument_value(args)
                    return self._format_tool_invocation_header(header)

                # thinking_end: show thinking content (*_end → gets counter)
                if isinstance(ame, dict) and ame_type == "thinking_end":
                    self.message_counter += 1
                    header["counter"] = f"#{self.message_counter}"
                    header["event"] = ame_type
                    thinking_text = ame.get("thinking", "") or ame.get("content", "") or ame.get("text", "")
                    if isinstance(thinking_text, str) and thinking_text.strip():
                        return self._format_json_header(header) + "\nthinking:\n" + self._style_thinking(thinking_text)
                    return self._format_json_header(header)

                # Any other *_end subtypes (e.g. text_end) get counter
                if isinstance(ame, dict) and ame_type and ame_type.endswith("_end"):
                    self.message_counter += 1
                    header["counter"] = f"#{self.message_counter}"

                message = payload.get("message", {})
                text = self._extract_text_from_message(message) if isinstance(message, dict) else ""

                # Also check assistantMessageEvent for completion text
                if isinstance(ame, dict):
                    if ame_type:
                        header["event"] = ame_type
                    delta_text = ame.get("text") or ame.get("delta") or ""
                    if isinstance(delta_text, str) and delta_text.strip():
                        if not text:
                            text = delta_text

                if text:
                    return self._format_json_header(header) + "\ncontent:\n" + self._style_assistant(text)
                return self._format_json_header(header)

            if event_type == "message_end":
                self.message_counter += 1
                header["counter"] = f"#{self.message_counter}"
                # Skip message text - already displayed by text_end/thinking_end/toolcall_end
                return json.dumps(header, ensure_ascii=False)

            # --- Tool execution events ---
            # Always suppress tool_execution_start: buffer its args for
            # tool_execution_end to use.  The user sees nothing until the
            # tool finishes, then gets a single combined "tool" event.
            if event_type == "tool_execution_start":
                self._buffer_exec_start(payload)
                self._in_tool_execution = True
                return None

            if event_type == "tool_execution_update":
                # Suppress updates — result will arrive in tool_execution_end
                return None

            if event_type == "tool_execution_end":
                self._in_tool_execution = False
                tool_call_id = payload.get("toolCallId")
                self._cancel_tool_running(tool_call_id)

                pending_tool = self._pending_tool_calls.pop(tool_call_id, None) if tool_call_id else None
                pending_exec = self._pending_exec_starts.pop(tool_call_id, None) if tool_call_id else None
                if pending_tool and pending_exec and "started_at" in pending_exec:
                    pending_tool["started_at"] = pending_exec["started_at"]
                pending = pending_tool or pending_exec

                if pending:
                    return self._build_combined_tool_event(pending, payload, now)

                # No buffered data at all — minimal fallback
                self.message_counter += 1
                header["type"] = "tool"
                header["counter"] = f"#{self.message_counter}"
                header["tool"] = payload.get("toolName", "")

                execution_time = self._format_execution_time(payload)
                if execution_time:
                    header["execution_time"] = execution_time

                is_error = payload.get("isError", False)
                if is_error:
                    header["isError"] = True

                result_val = payload.get("result")
                colorize_error = self._color_enabled() and bool(is_error)

                if isinstance(result_val, str) and result_val.strip():
                    truncated = self._truncate_tool_result_text(result_val)
                    if "\n" in truncated or colorize_error:
                        label = "result:"
                        colored = self._colorize_result(truncated, is_error=bool(is_error))
                        if colorize_error:
                            label = self._colorize_result(label, is_error=True)
                        return self._format_tool_invocation_header(header) + "\n" + label + "\n" + colored
                    header["result"] = truncated
                    return self._format_tool_invocation_header(header)

                if isinstance(result_val, dict):
                    result_content = result_val.get("content")
                    if isinstance(result_content, list):
                        for rc_item in result_content:
                            if isinstance(rc_item, dict) and rc_item.get("type") == "text":
                                text = rc_item.get("text", "")
                                truncated = self._truncate_tool_result_text(text)
                                if "\n" in truncated or colorize_error:
                                    label = "result:"
                                    colored = self._colorize_result(truncated, is_error=bool(is_error))
                                    if colorize_error:
                                        label = self._colorize_result(label, is_error=True)
                                    return self._format_tool_invocation_header(header) + "\n" + label + "\n" + colored
                                header["result"] = truncated
                                return self._format_tool_invocation_header(header)

                    result_str = self._strip_ansi_sequences(json.dumps(result_val, ensure_ascii=False))
                    if "\n" in result_str or len(result_str) > 200 or colorize_error:
                        label = "result:"
                        colored = self._colorize_result(result_str, is_error=bool(is_error))
                        if colorize_error:
                            label = self._colorize_result(label, is_error=True)
                        return self._format_tool_invocation_header(header) + "\n" + label + "\n" + colored
                    header["result"] = result_str
                    return self._format_tool_invocation_header(header)

                if isinstance(result_val, list):
                    result_str = self._strip_ansi_sequences(json.dumps(result_val, ensure_ascii=False))
                    if "\n" in result_str or len(result_str) > 200 or colorize_error:
                        label = "result:"
                        colored = self._colorize_result(result_str, is_error=bool(is_error))
                        if colorize_error:
                            label = self._colorize_result(label, is_error=True)
                        return self._format_tool_invocation_header(header) + "\n" + label + "\n" + colored
                    header["result"] = result_str
                    return self._format_tool_invocation_header(header)

                return self._format_tool_invocation_header(header)

            # --- Retry/compaction events ---
            if event_type == "auto_retry_start":
                header["attempt"] = payload.get("attempt")
                header["maxAttempts"] = payload.get("maxAttempts")
                header["delayMs"] = payload.get("delayMs")
                error_msg = payload.get("errorMessage", "")
                if error_msg:
                    header["error"] = error_msg
                return json.dumps(header, ensure_ascii=False)

            if event_type == "auto_retry_end":
                self.message_counter += 1
                header["counter"] = f"#{self.message_counter}"
                header["success"] = payload.get("success")
                header["attempt"] = payload.get("attempt")
                final_err = payload.get("finalError")
                if final_err:
                    header["finalError"] = final_err
                return json.dumps(header, ensure_ascii=False)

            # --- Fallback: emit raw with datetime ---
            header.update({k: v for k, v in payload.items() if k not in ("type",)})
            return json.dumps(header, ensure_ascii=False)

        except Exception:
            return json.dumps(payload, ensure_ascii=False)

    # ── Live stream prettifier ─────────────────────────────────────────────

    def _format_event_live(self, parsed: dict) -> Optional[str]:
        """Format Pi events for live streaming mode.

        Returns:
            str ending with \\n: a complete line to print
            str NOT ending with \\n: a delta to append (streaming text)
            "": suppress this event
            None: use raw JSON fallback
        """
        event_type = parsed.get("type", "")
        now = datetime.now().strftime("%I:%M:%S %p")

        if event_type == "message_update":
            ame = parsed.get("assistantMessageEvent", {})
            ame_type = ame.get("type", "") if isinstance(ame, dict) else ""

            # Stream text deltas directly (no JSON, no newline).
            if ame_type == "text_delta":
                delta = ame.get("delta", "")
                if isinstance(delta, str) and delta:
                    return self._style_assistant(delta)
                return ""

            if ame_type == "thinking_delta":
                delta = ame.get("delta", "")
                if isinstance(delta, str) and delta:
                    return self._style_thinking(delta)
                return ""

            # Section start markers (no counter — only *_end events get counters)
            if ame_type == "text_start":
                return json.dumps({"type": "text_start", "datetime": now}) + "\n"

            if ame_type == "thinking_start":
                return json.dumps({"type": "thinking_start", "datetime": now}) + "\n"

            # Section end markers (text was already streamed)
            if ame_type == "text_end":
                self.message_counter += 1
                return "\n" + json.dumps({"type": "text_end", "datetime": now, "counter": f"#{self.message_counter}"}) + "\n"

            if ame_type == "thinking_end":
                self.message_counter += 1
                return "\n" + json.dumps({"type": "thinking_end", "datetime": now, "counter": f"#{self.message_counter}"}) + "\n"

            # Tool call end: buffer for grouping with tool_execution_end
            if ame_type == "toolcall_end":
                tc = ame.get("toolCall", {})
                if self._buffer_tool_call_end(tc, now):
                    return ""  # suppress — will emit combined event on tool_execution_end
                # No toolCallId — fallback to original format
                self.message_counter += 1
                header = {"type": "toolcall_end", "datetime": now, "counter": f"#{self.message_counter}"}
                if isinstance(tc, dict):
                    header["tool"] = tc.get("name", "")
                    args = tc.get("arguments", {})
                    if isinstance(args, dict):
                        cmd = args.get("command", "")
                        if isinstance(cmd, str) and cmd:
                            header["command"] = self._sanitize_tool_argument_value(cmd)
                        else:
                            header["args"] = self._sanitize_tool_argument_value(args)
                    elif isinstance(args, str) and args.strip():
                        header["args"] = self._sanitize_tool_argument_value(args)
                return self._format_tool_invocation_header(header) + "\n"

            # Suppress all other message_update subtypes (toolcall_start, toolcall_delta, etc.)
            return ""

        # Suppress redundant events
        if event_type in ("message_start", "message_end"):
            return ""

        # tool_execution_start: always suppress, buffer args
        if event_type == "tool_execution_start":
            self._buffer_exec_start(parsed)
            self._in_tool_execution = True
            return ""  # suppress

        # tool_execution_end: combine with buffered data
        if event_type == "tool_execution_end":
            self._in_tool_execution = False
            tool_call_id = parsed.get("toolCallId")
            self._cancel_tool_running(tool_call_id)

            pending_tool = self._pending_tool_calls.pop(tool_call_id, None) if tool_call_id else None
            pending_exec = self._pending_exec_starts.pop(tool_call_id, None) if tool_call_id else None
            if pending_tool and pending_exec and "started_at" in pending_exec:
                pending_tool["started_at"] = pending_exec["started_at"]
            pending = pending_tool or pending_exec

            if pending:
                return self._build_combined_tool_event(pending, parsed, now) + "\n"

            # No buffered data — minimal fallback
            self.message_counter += 1
            header = {
                "type": "tool",
                "datetime": now,
                "counter": f"#{self.message_counter}",
                "tool": parsed.get("toolName", ""),
            }
            execution_time = self._format_execution_time(parsed)
            if execution_time:
                header["execution_time"] = execution_time

            is_error = parsed.get("isError", False)
            if is_error:
                header["isError"] = True

            result_val = parsed.get("result")
            colorize_error = self._color_enabled() and bool(is_error)

            if isinstance(result_val, str) and result_val.strip():
                truncated = self._truncate_tool_result_text(result_val)
                if "\n" in truncated or colorize_error:
                    label = "result:"
                    colored = self._colorize_result(truncated, is_error=bool(is_error))
                    if colorize_error:
                        label = self._colorize_result(label, is_error=True)
                    return self._format_tool_invocation_header(header) + "\n" + label + "\n" + colored + "\n"
                header["result"] = truncated
                return self._format_tool_invocation_header(header) + "\n"

            if isinstance(result_val, dict):
                result_content = result_val.get("content")
                if isinstance(result_content, list):
                    for rc_item in result_content:
                        if isinstance(rc_item, dict) and rc_item.get("type") == "text":
                            text = rc_item.get("text", "")
                            truncated = self._truncate_tool_result_text(text)
                            if "\n" in truncated or colorize_error:
                                label = "result:"
                                colored = self._colorize_result(truncated, is_error=bool(is_error))
                                if colorize_error:
                                    label = self._colorize_result(label, is_error=True)
                                return self._format_tool_invocation_header(header) + "\n" + label + "\n" + colored + "\n"
                            header["result"] = truncated
                            return self._format_tool_invocation_header(header) + "\n"

                result_json = self._strip_ansi_sequences(json.dumps(result_val, ensure_ascii=False))
                if "\n" in result_json or colorize_error:
                    label = "result:"
                    colored = self._colorize_result(result_json, is_error=bool(is_error))
                    if colorize_error:
                        label = self._colorize_result(label, is_error=True)
                    return self._format_tool_invocation_header(header) + "\n" + label + "\n" + colored + "\n"
                header["result"] = result_json
                return self._format_tool_invocation_header(header) + "\n"

            if isinstance(result_val, list):
                result_json = self._strip_ansi_sequences(json.dumps(result_val, ensure_ascii=False))
                if "\n" in result_json or colorize_error:
                    label = "result:"
                    colored = self._colorize_result(result_json, is_error=bool(is_error))
                    if colorize_error:
                        label = self._colorize_result(label, is_error=True)
                    return self._format_tool_invocation_header(header) + "\n" + label + "\n" + colored + "\n"
                header["result"] = result_json
                return self._format_tool_invocation_header(header) + "\n"

            return self._format_tool_invocation_header(header) + "\n"

        # turn_end: metadata only
        if event_type == "turn_end":
            self.message_counter += 1
            header = {"type": "turn_end", "datetime": now, "counter": f"#{self.message_counter}"}
            tool_results = parsed.get("toolResults")
            if isinstance(tool_results, list):
                header["tool_results_count"] = len(tool_results)
            self._add_visible_turn_cost(header, parsed)
            return self._format_json_header(header) + "\n"

        # turn_start: suppress (no user-visible value)
        if event_type == "turn_start":
            return ""

        # agent_start (no counter — only *_end events get counters)
        if event_type == "agent_start":
            return json.dumps({"type": event_type, "datetime": now}) + "\n"

        # agent_end
        if event_type == "agent_end":
            self.message_counter += 1
            header = {"type": "agent_end", "datetime": now, "counter": f"#{self.message_counter}"}
            messages = parsed.get("messages")
            if isinstance(messages, list):
                header["message_count"] = len(messages)
            total_cost_usd = self._extract_total_cost_usd(parsed)
            if total_cost_usd is not None:
                header["total_cost_usd"] = total_cost_usd
            return json.dumps(header, ensure_ascii=False) + "\n"

        # --- Role-based messages (Pi-wrapped Codex messages) ---
        role = parsed.get("role", "")
        if role == "toolResult":
            self.message_counter += 1
            header = {
                "type": "toolResult",
                "datetime": now,
                "counter": f"#{self.message_counter}",
                "toolName": parsed.get("toolName", ""),
            }
            is_error = parsed.get("isError", False)
            if is_error:
                header["isError"] = True
            content = parsed.get("content")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        text_val = item.get("text", "")
                        truncated = self._truncate_tool_result_text(text_val)
                        use_color = self._color_enabled()
                        if "\n" in truncated or use_color:
                            colored = self._colorize_result(truncated, is_error=bool(is_error))
                            label = self._colorize_result("content:", is_error=bool(is_error))
                            return json.dumps(header, ensure_ascii=False) + "\n" + label + "\n" + colored + "\n"
                        header["content"] = truncated
                        return json.dumps(header, ensure_ascii=False) + "\n"
            return json.dumps(header, ensure_ascii=False) + "\n"

        if role == "assistant":
            self.message_counter += 1
            content = parsed.get("content")
            if isinstance(content, list):
                self._strip_thinking_signature(content)
            header = {"type": "assistant", "datetime": now, "counter": f"#{self.message_counter}"}
            text_parts = []
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "text":
                            text_parts.append(item.get("text", ""))
                        elif item.get("type") == "thinking":
                            text_parts.append(f"[thinking] {item.get('thinking', '')}")
                        elif item.get("type") == "toolCall":
                            name = item.get("name", "")
                            args = item.get("arguments", {})
                            cmd = args.get("command", "") if isinstance(args, dict) else ""
                            text_parts.append(f"[toolCall] {name}: {cmd}" if cmd else f"[toolCall] {name}")
            if text_parts:
                combined = "\n".join(text_parts)
                if "\n" in combined:
                    return json.dumps(header, ensure_ascii=False) + "\n" + combined + "\n"
                header["content"] = combined
            return json.dumps(header, ensure_ascii=False) + "\n"

        if role:
            # Other roles — minimal JSON header
            self.message_counter += 1
            return json.dumps({"type": role, "datetime": now, "counter": f"#{self.message_counter}"}, ensure_ascii=False) + "\n"

        # --- Native Codex events (agent_reasoning, agent_message, exec_command_end, etc.) ---
        msg_type, payload, outer_type = self._normalize_codex_event(parsed)

        if msg_type in ("agent_reasoning", "reasoning"):
            self.message_counter += 1
            content = self._extract_reasoning_text(payload)
            header = {"type": msg_type, "datetime": now, "counter": f"#{self.message_counter}"}
            if "\n" in content:
                return json.dumps(header, ensure_ascii=False) + "\ntext:\n" + content + "\n"
            if content:
                header["text"] = content
            return json.dumps(header, ensure_ascii=False) + "\n"

        if msg_type in ("agent_message", "assistant_message"):
            self.message_counter += 1
            content = self._extract_message_text_codex(payload)
            header = {"type": msg_type, "datetime": now, "counter": f"#{self.message_counter}"}
            if "\n" in content:
                return json.dumps(header, ensure_ascii=False) + "\nmessage:\n" + content + "\n"
            if content:
                header["message"] = content
            return json.dumps(header, ensure_ascii=False) + "\n"

        if msg_type == "exec_command_end":
            self.message_counter += 1
            formatted_output = payload.get("formatted_output", "") if isinstance(payload, dict) else ""
            header = {"type": msg_type, "datetime": now, "counter": f"#{self.message_counter}"}
            if "\n" in formatted_output:
                return json.dumps(header, ensure_ascii=False) + "\nformatted_output:\n" + formatted_output + "\n"
            if formatted_output:
                header["formatted_output"] = formatted_output
            return json.dumps(header, ensure_ascii=False) + "\n"

        if msg_type == "command_execution":
            self.message_counter += 1
            aggregated_output = self._extract_command_output_text(payload)
            header = {"type": msg_type, "datetime": now, "counter": f"#{self.message_counter}"}
            if "\n" in aggregated_output:
                return json.dumps(header, ensure_ascii=False) + "\naggregated_output:\n" + aggregated_output + "\n"
            if aggregated_output:
                header["aggregated_output"] = aggregated_output
            return json.dumps(header, ensure_ascii=False) + "\n"

        # Fallback: not handled
        return None

    def _build_hide_types(self) -> set:
        """Build the set of event types to suppress from output."""
        hide_types = set(self.DEFAULT_HIDDEN_STREAM_TYPES)
        for env_name in ("PI_HIDE_STREAM_TYPES", "YYLO_HIDE_STREAM_TYPES"):
            env_val = os.environ.get(env_name, "")
            if env_val:
                parts = [p.strip() for p in env_val.split(",") if p.strip()]
                hide_types.update(parts)
        return hide_types

    @staticmethod
    def _toolcall_end_delay_seconds() -> float:
        """Return delay for fallback toolcall_end visibility (default 3s)."""
        raw = os.environ.get("PI_TOOLCALL_END_DELAY_SECONDS", "3")
        try:
            delay = float(raw)
        except (TypeError, ValueError):
            delay = 3.0
        return max(0.0, delay)

    @staticmethod
    def _sanitize_sub_agent_response(event: dict) -> dict:
        """Strip bulky fields (messages, type) from sub_agent_response to reduce token usage."""
        return {k: v for k, v in event.items() if k not in ("messages", "type")}

    def _reset_run_cost_tracking(self) -> None:
        """Reset per-run usage/cost accumulation state."""
        self._run_usage_totals = None
        self._run_total_cost_usd = None
        self._run_seen_usage_keys.clear()

    @staticmethod
    def _is_numeric_value(value: object) -> bool:
        """True for int/float values (excluding bool)."""
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    @staticmethod
    def _normalize_usage_payload(usage: dict) -> Optional[dict]:
        """Normalize usage payload into numeric totals for accumulation."""
        if not isinstance(usage, dict):
            return None

        usage_cost = usage.get("cost")
        cost_payload = usage_cost if isinstance(usage_cost, dict) else {}

        input_tokens = float(usage.get("input")) if PiService._is_numeric_value(usage.get("input")) else 0.0
        output_tokens = float(usage.get("output")) if PiService._is_numeric_value(usage.get("output")) else 0.0
        cache_read_tokens = float(usage.get("cacheRead")) if PiService._is_numeric_value(usage.get("cacheRead")) else 0.0
        cache_write_tokens = float(usage.get("cacheWrite")) if PiService._is_numeric_value(usage.get("cacheWrite")) else 0.0

        total_tokens_raw = usage.get("totalTokens")
        total_tokens = (
            float(total_tokens_raw)
            if PiService._is_numeric_value(total_tokens_raw)
            else input_tokens + output_tokens + cache_read_tokens + cache_write_tokens
        )

        cost_input = float(cost_payload.get("input")) if PiService._is_numeric_value(cost_payload.get("input")) else 0.0
        cost_output = float(cost_payload.get("output")) if PiService._is_numeric_value(cost_payload.get("output")) else 0.0
        cost_cache_read = (
            float(cost_payload.get("cacheRead")) if PiService._is_numeric_value(cost_payload.get("cacheRead")) else 0.0
        )
        cost_cache_write = (
            float(cost_payload.get("cacheWrite")) if PiService._is_numeric_value(cost_payload.get("cacheWrite")) else 0.0
        )

        cost_total_raw = cost_payload.get("total")
        cost_total = (
            float(cost_total_raw)
            if PiService._is_numeric_value(cost_total_raw)
            else cost_input + cost_output + cost_cache_read + cost_cache_write
        )

        has_any_value = any(
            PiService._is_numeric_value(v)
            for v in (
                usage.get("input"),
                usage.get("output"),
                usage.get("cacheRead"),
                usage.get("cacheWrite"),
                usage.get("totalTokens"),
                cost_payload.get("input"),
                cost_payload.get("output"),
                cost_payload.get("cacheRead"),
                cost_payload.get("cacheWrite"),
                cost_payload.get("total"),
            )
        )

        if not has_any_value:
            return None

        return {
            "input": input_tokens,
            "output": output_tokens,
            "cacheRead": cache_read_tokens,
            "cacheWrite": cache_write_tokens,
            "totalTokens": total_tokens,
            "cost": {
                "input": cost_input,
                "output": cost_output,
                "cacheRead": cost_cache_read,
                "cacheWrite": cost_cache_write,
                "total": cost_total,
            },
        }

    @staticmethod
    def _merge_usage_payloads(base: Optional[dict], delta: Optional[dict]) -> Optional[dict]:
        """Merge normalized usage payloads by summing token/cost fields."""
        if not isinstance(base, dict):
            return delta
        if not isinstance(delta, dict):
            return base

        base_cost = base.get("cost") if isinstance(base.get("cost"), dict) else {}
        delta_cost = delta.get("cost") if isinstance(delta.get("cost"), dict) else {}

        return {
            "input": float(base.get("input", 0.0)) + float(delta.get("input", 0.0)),
            "output": float(base.get("output", 0.0)) + float(delta.get("output", 0.0)),
            "cacheRead": float(base.get("cacheRead", 0.0)) + float(delta.get("cacheRead", 0.0)),
            "cacheWrite": float(base.get("cacheWrite", 0.0)) + float(delta.get("cacheWrite", 0.0)),
            "totalTokens": float(base.get("totalTokens", 0.0)) + float(delta.get("totalTokens", 0.0)),
            "cost": {
                "input": float(base_cost.get("input", 0.0)) + float(delta_cost.get("input", 0.0)),
                "output": float(base_cost.get("output", 0.0)) + float(delta_cost.get("output", 0.0)),
                "cacheRead": float(base_cost.get("cacheRead", 0.0)) + float(delta_cost.get("cacheRead", 0.0)),
                "cacheWrite": float(base_cost.get("cacheWrite", 0.0)) + float(delta_cost.get("cacheWrite", 0.0)),
                "total": float(base_cost.get("total", 0.0)) + float(delta_cost.get("total", 0.0)),
            },
        }

    @staticmethod
    def _aggregate_assistant_usages(messages: list) -> Optional[dict]:
        """Aggregate assistant usage payloads from an event messages array."""
        if not isinstance(messages, list):
            return None

        assistant_usages: List[dict] = []
        for msg in messages:
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                usage = msg.get("usage")
                if isinstance(usage, dict):
                    assistant_usages.append(usage)

        if not assistant_usages:
            return None
        if len(assistant_usages) == 1:
            return assistant_usages[0]

        totals: Optional[dict] = None
        for usage in assistant_usages:
            normalized = PiService._normalize_usage_payload(usage)
            totals = PiService._merge_usage_payloads(totals, normalized)

        return totals

    def _assistant_usage_dedupe_key(self, message: dict, usage: dict) -> Optional[str]:
        """Build a stable dedupe key for assistant usage seen across message/turn_end events."""
        if not isinstance(message, dict) or not isinstance(usage, dict):
            return None

        for id_key in ("id", "messageId", "message_id"):
            value = message.get(id_key)
            if isinstance(value, str) and value.strip():
                return f"id:{value.strip()}"

        timestamp = message.get("timestamp")
        if self._is_numeric_value(timestamp):
            return f"ts:{int(float(timestamp))}"
        if isinstance(timestamp, str) and timestamp.strip():
            return f"ts:{timestamp.strip()}"

        usage_cost = usage.get("cost") if isinstance(usage.get("cost"), dict) else {}
        signature: Dict[str, object] = {
            "stopReason": message.get("stopReason") if isinstance(message.get("stopReason"), str) else "",
            "input": usage.get("input", 0.0),
            "output": usage.get("output", 0.0),
            "cacheRead": usage.get("cacheRead", 0.0),
            "cacheWrite": usage.get("cacheWrite", 0.0),
            "totalTokens": usage.get("totalTokens", 0.0),
            "costTotal": usage_cost.get("total", 0.0),
        }

        text = self._extract_text_from_message(message)
        if text:
            signature["text"] = text[:120]

        return "sig:" + json.dumps(signature, sort_keys=True, ensure_ascii=False)

    def _track_assistant_usage_from_event(self, event: dict) -> None:
        """Accumulate per-run assistant usage from stream events."""
        if not isinstance(event, dict):
            return

        event_type = event.get("type")
        if event_type not in ("message", "message_end", "turn_end"):
            return

        message = event.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return

        normalized_usage = self._normalize_usage_payload(message.get("usage"))
        if not isinstance(normalized_usage, dict):
            return

        usage_key = self._assistant_usage_dedupe_key(message, normalized_usage)
        if usage_key and usage_key in self._run_seen_usage_keys:
            return
        if usage_key:
            self._run_seen_usage_keys.add(usage_key)

        self._run_usage_totals = self._merge_usage_payloads(self._run_usage_totals, normalized_usage)
        self._run_total_cost_usd = self._extract_total_cost_usd(
            {"usage": self._run_usage_totals},
            self._run_usage_totals,
        )

    def _get_accumulated_total_cost_usd(self) -> Optional[float]:
        """Return accumulated per-run total cost when available."""
        if self._is_numeric_value(self._run_total_cost_usd):
            return float(self._run_total_cost_usd)
        if isinstance(self._run_usage_totals, dict):
            return self._extract_total_cost_usd({"usage": self._run_usage_totals}, self._run_usage_totals)
        return None

    @staticmethod
    def _extract_usage_from_event(event: dict) -> Optional[dict]:
        """Extract usage payload from Pi event shapes (event/message/messages)."""
        if not isinstance(event, dict):
            return None

        messages = event.get("messages")
        if event.get("type") == "agent_end" and isinstance(messages, list):
            aggregated = PiService._aggregate_assistant_usages(messages)
            if isinstance(aggregated, dict):
                return aggregated

        direct_usage = event.get("usage")
        if isinstance(direct_usage, dict):
            return direct_usage

        message = event.get("message")
        if isinstance(message, dict):
            message_usage = message.get("usage")
            if isinstance(message_usage, dict):
                return message_usage

        if isinstance(messages, list):
            aggregated = PiService._aggregate_assistant_usages(messages)
            if isinstance(aggregated, dict):
                return aggregated

        return None

    @staticmethod
    def _extract_total_cost_usd(event: dict, usage: Optional[dict] = None) -> Optional[float]:
        """Extract total USD cost from explicit fields or usage.cost.total."""
        if not isinstance(event, dict):
            return None

        for key in ("total_cost_usd", "totalCostUsd", "totalCostUSD"):
            value = event.get(key)
            if PiService._is_numeric_value(value):
                return float(value)

        direct_cost = event.get("cost")
        if PiService._is_numeric_value(direct_cost):
            return float(direct_cost)
        if isinstance(direct_cost, dict):
            total = direct_cost.get("total")
            if PiService._is_numeric_value(total):
                return float(total)

        usage_payload = usage if isinstance(usage, dict) else None
        if usage_payload is None:
            usage_payload = PiService._extract_usage_from_event(event)

        if isinstance(usage_payload, dict):
            usage_cost = usage_payload.get("cost")
            if isinstance(usage_cost, dict):
                total = usage_cost.get("total")
                if PiService._is_numeric_value(total):
                    return float(total)

        return None

    @staticmethod
    def _extract_session_id_from_event(event: Optional[dict]) -> Optional[str]:
        """Extract session id from common Pi/Codex payload shapes."""
        if not isinstance(event, dict):
            return None

        candidates: List[object] = [
            event.get("session_id"),
            event.get("sessionId"),
        ]

        if event.get("type") == "session":
            candidates.append(event.get("id"))

        message = event.get("message")
        if isinstance(message, dict):
            candidates.extend(
                [
                    message.get("session_id"),
                    message.get("sessionId"),
                ]
            )

        nested = event.get("sub_agent_response")
        if isinstance(nested, dict):
            candidates.extend(
                [
                    nested.get("session_id"),
                    nested.get("sessionId"),
                ]
            )
            if nested.get("type") == "session":
                candidates.append(nested.get("id"))

        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()

        return None

    @staticmethod
    def _is_error_result_event(event: Optional[dict]) -> bool:
        """Return True when event represents a terminal error payload."""
        if not isinstance(event, dict):
            return False

        if event.get("is_error") is True:
            return True

        subtype = event.get("subtype")
        if isinstance(subtype, str) and subtype.lower() == "error":
            return True

        event_type = event.get("type")
        if isinstance(event_type, str) and event_type.lower() in {"error", "turn.failed", "turn_failed"}:
            return True

        return False

    @staticmethod
    def _is_success_result_event(event: Optional[dict]) -> bool:
        """Return True when event is an explicit successful result envelope."""
        if not isinstance(event, dict):
            return False

        if PiService._is_error_result_event(event):
            return False

        subtype = event.get("subtype")
        if isinstance(subtype, str) and subtype.lower() == "success":
            return True

        event_type = event.get("type")
        if isinstance(event_type, str) and event_type.lower() == "result" and event.get("is_error") is False:
            result_value = event.get("result")
            if isinstance(result_value, str):
                return bool(result_value.strip())
            if result_value not in (None, "", [], {}):
                return True

        return False

    @staticmethod
    def _is_assistant_text_success_result_event(event: Optional[dict]) -> bool:
        """Return True when success came from assistant text-only stream events."""
        if not PiService._is_success_result_event(event):
            return False

        if not isinstance(event, dict):
            return False

        response_type = event.get("sub_agent_response_type")
        if isinstance(response_type, str):
            return response_type.lower() in {"message", "turn_end", "agent_end"}

        sub_agent_response = event.get("sub_agent_response")
        if not isinstance(sub_agent_response, dict):
            return False

        response_type = sub_agent_response.get("type")
        if not isinstance(response_type, str):
            return False

        return response_type.lower() in {"message", "turn_end", "agent_end"}

    @staticmethod
    def _should_promote_stderr_error(last_result_event: Optional[dict]) -> bool:
        """Return True when stderr errors should override the current result event."""
        if not PiService._is_success_result_event(last_result_event):
            return True

        # Assistant-text-derived success payloads are not authoritative when
        # provider stderr already surfaced a terminal Codex/Pi error.
        return PiService._is_assistant_text_success_result_event(last_result_event)

    @staticmethod
    def _normalize_error_text(raw_text: str) -> str:
        """Normalize stderr/plaintext by stripping ANSI and carriage controls."""
        if not isinstance(raw_text, str):
            return ""

        text = PiService._ANSI_ESCAPE_RE.sub("", raw_text)
        text = text.replace("\x08", "")
        text = text.replace("\r", "\n")
        return text

    @staticmethod
    def _extract_error_message_from_stderr_output(stderr_output: str) -> Optional[str]:
        """Extract terminal provider errors from full stderr output blocks."""
        if not isinstance(stderr_output, str):
            return None

        text = PiService._normalize_error_text(stderr_output).strip()
        if not text:
            return None

        extracted = PiService._extract_error_message_from_text(text)
        if extracted:
            return extracted

        for line in text.splitlines():
            extracted_line = PiService._extract_error_message_from_text(line)
            if extracted_line:
                return extracted_line

        normalized = " ".join(text.split())
        lowered = normalized.lower()
        if "error: codex error" in lowered or "codex error" in lowered or "server_error" in lowered:
            return normalized

        return None

    @staticmethod
    def _missing_session_recovery_message(message: str) -> str:
        """Redact missing-session provider details and give scope-safe recovery guidance."""
        lowered = message.lower()
        missing = (
            "session not found" in lowered
            or "no session found" in lowered
            or "unknown session" in lowered
            or ("session" in lowered and "does not exist" in lowered)
        )
        if not missing:
            return message
        return (
            "Pi session is unavailable. Continuity metadata was preserved. "
            "Resume an existing session with --resume <session-id> or start a new run; "
            "automatic continuation never falls back to another scope."
        )

    @staticmethod
    def _extract_error_message_from_event(event: dict) -> Optional[str]:
        """Extract a human-readable message from Pi/Codex error event shapes."""
        if not isinstance(event, dict):
            return None

        event_type = event.get("type")
        if event_type == "auto_retry_end" and event.get("success") is False:
            final_error = event.get("finalError")
            if isinstance(final_error, str) and final_error.strip():
                return PiService._missing_session_recovery_message(final_error.strip())
            return "Auto-retry failed after maximum attempts"

        if not PiService._is_error_result_event(event):
            return None

        def _stringify_error(value: object) -> Optional[str]:
            if isinstance(value, str):
                text = value.strip()
                return text if text else None
            if isinstance(value, dict):
                nested_message = value.get("message")
                if isinstance(nested_message, str) and nested_message.strip():
                    return nested_message.strip()
                nested_error = value.get("error")
                if isinstance(nested_error, str) and nested_error.strip():
                    return nested_error.strip()
                try:
                    return json.dumps(value, ensure_ascii=False)
                except Exception:
                    return str(value)
            if value is not None:
                return str(value)
            return None

        for key in ("error", "message", "errorMessage", "result"):
            extracted = _stringify_error(event.get(key))
            if extracted:
                return PiService._missing_session_recovery_message(extracted)

        return "Unknown Pi error"

    @staticmethod
    def _extract_error_message_from_text(raw_text: str) -> Optional[str]:
        """Extract an error message from stderr/plaintext lines."""
        if not isinstance(raw_text, str):
            return None

        text = PiService._normalize_error_text(raw_text).strip()
        if not text:
            return None

        # Direct JSON line
        try:
            parsed = json.loads(text)
            extracted = PiService._extract_error_message_from_event(parsed)
            if extracted:
                return extracted
        except Exception:
            pass

        # Prefix + JSON payload pattern (e.g. "Error: Codex error: {...}")
        json_start = text.find("{")
        if json_start > 0:
            json_candidate = text[json_start:]
            try:
                parsed = json.loads(json_candidate)
                extracted = PiService._extract_error_message_from_event(parsed)
                if extracted:
                    return extracted
            except Exception:
                pass

        lowered = text.lower()
        recovered = PiService._missing_session_recovery_message(text)
        if recovered != text:
            return recovered

        # Sometimes progress spinners and carriage updates prefix the same line.
        # Detect embedded `Error: Codex error:` signatures even when not line-start.
        embedded_error_index = lowered.find("error:")
        if embedded_error_index >= 0:
            embedded_text = text[embedded_error_index:].strip()
            embedded_lowered = embedded_text.lower()
            if "codex error" in embedded_lowered or "server_error" in embedded_lowered:
                message = embedded_text.split(":", 1)[1].strip() if embedded_lowered.startswith("error:") else embedded_text
                return message or embedded_text

        if lowered.startswith("error:"):
            message = text.split(":", 1)[1].strip()
            return message or text

        if "server_error" in lowered or "codex error" in lowered:
            return text

        return None

    @staticmethod
    def _extract_provider_error_from_result_text(result_text: str) -> Optional[str]:
        """Detect provider-level failures that leaked into assistant result text."""
        if not isinstance(result_text, str):
            return None

        text = result_text.strip()
        if not text:
            return None

        normalized = " ".join(text.split())
        lowered = normalized.lower()

        provider_signatures = (
            "chatgpt usage limit",
            "usage limit",
            "rate limit",
            "insufficient_quota",
            "too many requests",
            "codex error",
            "server_error",
            "please retry this request later",
            "occurred while processing your request",
        )

        if lowered.startswith("error:"):
            payload = normalized.split(":", 1)[1].strip() if ":" in normalized else ""
            if any(signature in lowered for signature in provider_signatures) or "try again in" in lowered:
                return payload or normalized

        if any(signature in lowered for signature in provider_signatures):
            return normalized

        return None

    def _build_success_result_event(self, text: str, event: dict) -> dict:
        """Build standardized success envelope for shell-backend capture."""
        usage = self._extract_usage_from_event(event)
        if isinstance(self._run_usage_totals, dict):
            usage = self._run_usage_totals

        total_cost_usd = self._extract_total_cost_usd(event, usage)
        accumulated_total_cost = self._get_accumulated_total_cost_usd()
        if accumulated_total_cost is not None:
            total_cost_usd = accumulated_total_cost

        result_event: Dict = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": text,
            "session_id": self.session_id,
            "sub_agent_response": self._sanitize_sub_agent_response(event),
        }
        if "/" in self.model_name:
            provider, model = self.model_name.split("/", 1)
            if provider and model:
                result_event["provider"] = provider
                result_event["model"] = model

        event_type = event.get("type") if isinstance(event, dict) else None
        if isinstance(event_type, str) and event_type.strip():
            result_event["sub_agent_response_type"] = event_type

        if isinstance(usage, dict):
            result_event["usage"] = usage
        if total_cost_usd is not None:
            result_event["total_cost_usd"] = total_cost_usd

        return result_event

    def _build_error_result_event(self, error_message: str, event: Optional[dict] = None) -> dict:
        """Build standardized error envelope for shell-backend capture."""
        message = error_message.strip() if isinstance(error_message, str) else str(error_message)

        result_event: Dict = {
            "type": "result",
            "subtype": "error",
            "is_error": True,
            "result": message,
            "error": message,
            "session_id": self.session_id,
        }
        if "/" in self.model_name:
            provider, model = self.model_name.split("/", 1)
            if provider and model:
                result_event["provider"] = provider
                result_event["model"] = model

        if isinstance(event, dict):
            result_event["sub_agent_response"] = self._sanitize_sub_agent_response(event)

        return result_event

    def _write_capture_file(self, capture_path: Optional[str]) -> None:
        """Write final result event to capture file for shell backend."""
        if not capture_path or not self.last_result_event:
            return

        payload = dict(self.last_result_event)

        if not payload.get("session_id"):
            existing_capture: Optional[dict] = None
            try:
                capture_file = Path(capture_path)
                if capture_file.exists():
                    raw_existing = capture_file.read_text(encoding="utf-8").strip()
                    if raw_existing:
                        parsed_existing = json.loads(raw_existing)
                        if isinstance(parsed_existing, dict):
                            existing_capture = parsed_existing
            except Exception:
                existing_capture = None

            existing_session_id: Optional[str] = None
            if isinstance(existing_capture, dict):
                candidate = existing_capture.get("session_id")
                if isinstance(candidate, str) and candidate.strip():
                    existing_session_id = candidate.strip()
                elif isinstance(existing_capture.get("sub_agent_response"), dict):
                    nested = existing_capture["sub_agent_response"].get("session_id")
                    if isinstance(nested, str) and nested.strip():
                        existing_session_id = nested.strip()

            if existing_session_id:
                payload["session_id"] = existing_session_id
                if not self.session_id:
                    self.session_id = existing_session_id

        self.last_result_event = payload

        try:
            Path(capture_path).write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"Warning: Could not write capture file: {e}", file=sys.stderr)

    @staticmethod
    def _read_capture_result_event(capture_path: Optional[str]) -> Optional[dict]:
        """Read current capture file payload if present."""
        if not capture_path:
            return None

        try:
            capture_file = Path(capture_path)
            if not capture_file.exists():
                return None
            raw_payload = capture_file.read_text(encoding="utf-8").strip()
            if not raw_payload:
                return None
            parsed = json.loads(raw_payload)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return None

        return None

    def _apply_capture_result_event(self, capture_path: Optional[str]) -> Tuple[Optional[dict], bool]:
        """Hydrate final state from capture payload and report stderr-promotion suppression."""
        capture_event = self._read_capture_result_event(capture_path)
        if not isinstance(capture_event, dict):
            return None, False

        capture_session_id = capture_event.get("session_id")
        if isinstance(capture_session_id, str) and capture_session_id.strip() and not self.session_id:
            self.session_id = capture_session_id.strip()

        if self.last_result_event is None:
            self.last_result_event = capture_event
        elif (
            isinstance(self.last_result_event, dict)
            and not self.last_result_event.get("session_id")
            and isinstance(capture_session_id, str)
            and capture_session_id.strip()
        ):
            self.last_result_event["session_id"] = capture_session_id.strip()

        capture_provider_error: Optional[str] = None
        if self._is_success_result_event(capture_event):
            capture_result = capture_event.get("result")
            if isinstance(capture_result, str):
                capture_provider_error = self._extract_provider_error_from_result_text(capture_result)
                if capture_provider_error:
                    self.last_result_event = self._build_error_result_event(capture_provider_error, capture_event)

        suppress_stderr_promotion = self._is_success_result_event(capture_event) and not capture_provider_error
        return capture_event, suppress_stderr_promotion

    def _build_live_auto_exit_extension_source(self, capture_path: Optional[str]) -> str:
        """Build a temporary Pi extension source used by --live mode.

        The extension listens for agent_end, writes a compact result envelope to
        JUNO_SUBAGENT_CAPTURE_PATH-compatible location, then requests
        graceful shutdown via ctx.shutdown().
        """
        capture_literal = json.dumps(capture_path or "")
        source = """import type { ExtensionAPI } from \"@mariozechner/pi-coding-agent\";
import * as fs from \"node:fs\";

const capturePath = __CAPTURE_PATH__;
const defaultShutdownDelayMs = 3000;
const parsedShutdownDelayMs = Number(process.env.PI_LIVE_AGENT_END_DELAY_MS ?? defaultShutdownDelayMs);
const shutdownDelayMs =
  Number.isFinite(parsedShutdownDelayMs) && parsedShutdownDelayMs >= 0
    ? parsedShutdownDelayMs
    : defaultShutdownDelayMs;

function extractTextFromMessages(messages: any[]): string {
  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i];
    if (!msg || msg.role !== \"assistant\") continue;

    const content = msg.content;
    if (typeof content === \"string\") {
      if (content.trim()) return content;
      continue;
    }

    if (Array.isArray(content)) {
      const parts: string[] = [];
      for (const item of content) {
        if (typeof item === \"string\" && item.trim()) {
          parts.push(item);
          continue;
        }
        if (item && item.type === \"text\" && typeof item.text === \"string\" && item.text.trim()) {
          parts.push(item.text);
        }
      }
      if (parts.length > 0) return parts.join(\"\\n\");
    }
  }
  return \"\";
}

function isFiniteNumber(value: any): value is number {
  return typeof value === \"number\" && Number.isFinite(value);
}

function normalizeUsage(usage: any): any | undefined {
  if (!usage || typeof usage !== \"object\") return undefined;

  const cost = usage.cost && typeof usage.cost === \"object\" ? usage.cost : {};

  const input = isFiniteNumber(usage.input) ? usage.input : 0;
  const output = isFiniteNumber(usage.output) ? usage.output : 0;
  const cacheRead = isFiniteNumber(usage.cacheRead) ? usage.cacheRead : 0;
  const cacheWrite = isFiniteNumber(usage.cacheWrite) ? usage.cacheWrite : 0;
  const totalTokens = isFiniteNumber(usage.totalTokens)
    ? usage.totalTokens
    : input + output + cacheRead + cacheWrite;

  const costInput = isFiniteNumber(cost.input) ? cost.input : 0;
  const costOutput = isFiniteNumber(cost.output) ? cost.output : 0;
  const costCacheRead = isFiniteNumber(cost.cacheRead) ? cost.cacheRead : 0;
  const costCacheWrite = isFiniteNumber(cost.cacheWrite) ? cost.cacheWrite : 0;
  const costTotal = isFiniteNumber(cost.total)
    ? cost.total
    : costInput + costOutput + costCacheRead + costCacheWrite;

  const hasAnyValue =
    isFiniteNumber(usage.input) ||
    isFiniteNumber(usage.output) ||
    isFiniteNumber(usage.cacheRead) ||
    isFiniteNumber(usage.cacheWrite) ||
    isFiniteNumber(usage.totalTokens) ||
    isFiniteNumber(cost.input) ||
    isFiniteNumber(cost.output) ||
    isFiniteNumber(cost.cacheRead) ||
    isFiniteNumber(cost.cacheWrite) ||
    isFiniteNumber(cost.total);

  if (!hasAnyValue) return undefined;

  return {
    input,
    output,
    cacheRead,
    cacheWrite,
    totalTokens,
    cost: {
      input: costInput,
      output: costOutput,
      cacheRead: costCacheRead,
      cacheWrite: costCacheWrite,
      total: costTotal,
    },
  };
}

function mergeUsage(base: any | undefined, delta: any | undefined): any | undefined {
  if (!base) return delta;
  if (!delta) return base;

  const baseCost = base.cost && typeof base.cost === \"object\" ? base.cost : {};
  const deltaCost = delta.cost && typeof delta.cost === \"object\" ? delta.cost : {};

  return {
    input: (base.input ?? 0) + (delta.input ?? 0),
    output: (base.output ?? 0) + (delta.output ?? 0),
    cacheRead: (base.cacheRead ?? 0) + (delta.cacheRead ?? 0),
    cacheWrite: (base.cacheWrite ?? 0) + (delta.cacheWrite ?? 0),
    totalTokens: (base.totalTokens ?? 0) + (delta.totalTokens ?? 0),
    cost: {
      input: (baseCost.input ?? 0) + (deltaCost.input ?? 0),
      output: (baseCost.output ?? 0) + (deltaCost.output ?? 0),
      cacheRead: (baseCost.cacheRead ?? 0) + (deltaCost.cacheRead ?? 0),
      cacheWrite: (baseCost.cacheWrite ?? 0) + (deltaCost.cacheWrite ?? 0),
      total: (baseCost.total ?? 0) + (deltaCost.total ?? 0),
    },
  };
}

function extractAssistantUsage(messages: any[]): any | undefined {
  let totals: any | undefined;

  for (const msg of messages) {
    if (!msg || msg.role !== \"assistant\") {
      continue;
    }

    const normalized = normalizeUsage(msg.usage);
    if (!normalized) {
      continue;
    }

    totals = mergeUsage(totals, normalized);
  }

  return totals;
}

function extractLatestAssistantStopReason(messages: any[]): string | undefined {
  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i];
    if (!msg || msg.role !== \"assistant\") {
      continue;
    }

    const reason = msg.stopReason;
    return typeof reason === \"string\" && reason ? reason : undefined;
  }

  return undefined;
}

function writeCapturePayload(payload: any): void {
  if (!capturePath) {
    return;
  }

  fs.writeFileSync(capturePath, JSON.stringify(payload), \"utf-8\");
}

function persistSessionSnapshot(sessionId: unknown): void {
  if (typeof sessionId !== \"string\" || !sessionId) {
    return;
  }

  try {
    writeCapturePayload({
      type: \"result\",
      subtype: \"session\",
      is_error: false,
      session_id: sessionId,
    });
  } catch {
    // Non-fatal: runtime capture should continue even if snapshot write fails.
  }
}

export default function (pi: ExtensionAPI) {
  let completed = false;
  let latestSessionId: string | undefined;
  let pendingShutdownTimer: ReturnType<typeof setTimeout> | undefined;

  // When agent_end fires with stopReason=error, Pi may be about to auto-retry
  // internally. The Pi extension API does NOT expose auto_retry_start/end events
  // (those go through session.subscribe(), not the extension runner). We therefore
  // use a generous delay before treating an error agent_end as final, which gives
  // Pi time to complete its retry and emit a subsequent non-error agent_end. If a
  // successful agent_end arrives it cancels this timer via clearPendingShutdown().
  const defaultErrorAgentEndDelayMs = 30000;
  const parsedErrorDelayMs = Number(process.env.PI_LIVE_ERROR_AGENT_END_DELAY_MS ?? defaultErrorAgentEndDelayMs);
  const errorAgentEndDelayMs =
    Number.isFinite(parsedErrorDelayMs) && parsedErrorDelayMs >= 0
      ? parsedErrorDelayMs
      : defaultErrorAgentEndDelayMs;

  function clearPendingShutdown(): void {
    if (pendingShutdownTimer) {
      clearTimeout(pendingShutdownTimer);
      pendingShutdownTimer = undefined;
    }
  }

  async function finalizeAndShutdown(event: any, ctx: any): Promise<void> {
    try {
      const messages = Array.isArray(event?.messages) ? event.messages : [];
      const usage = extractAssistantUsage(messages);
      const totalCost = typeof usage?.cost?.total === \"number\" ? usage.cost.total : undefined;
      const stopReason = extractLatestAssistantStopReason(messages);
      const managerSessionId =
        typeof ctx?.sessionManager?.getSessionId === \"function\"
          ? ctx.sessionManager.getSessionId()
          : undefined;
      const sessionId =
        (typeof managerSessionId === \"string\" && managerSessionId ? managerSessionId : undefined) ||
        latestSessionId;
      const resultText = extractTextFromMessages(messages);
      const isError = stopReason === \"error\";
      const resolvedResult = isError
        ? resultText || \"Request error\"
        : resultText;
      const payload: any = {
        type: \"result\",
        subtype: isError ? \"error\" : \"success\",
        is_error: isError,
        result: resolvedResult,
        usage,
        total_cost_usd: totalCost,
        sub_agent_response: event,
      };

      if (isError) {
        payload.error = resolvedResult;
      }

      if (typeof sessionId === \"string\" && sessionId) {
        payload.session_id = sessionId;
      }

      writeCapturePayload(payload);
    } catch {
      // Keep shutdown behavior even when capture writing fails.
    } finally {
      await ctx.shutdown();
    }
  }

  pi.on(\"session\", (event, ctx) => {
    const eventSessionId = typeof event?.id === \"string\" ? event.id : undefined;
    const managerSessionId =
      typeof ctx?.sessionManager?.getSessionId === \"function\"
        ? ctx.sessionManager.getSessionId()
        : undefined;
    const sessionId = managerSessionId || eventSessionId;

    if (typeof sessionId === \"string\" && sessionId) {
      latestSessionId = sessionId;
    }

    persistSessionSnapshot(sessionId);
  });

  // Capture session_id as early as possible from session_start so it is available
  // as a fallback even when the agent_end handler fires during error/shutdown paths.
  pi.on(\"session_start\", (event, ctx) => {
    const managerSessionId =
      typeof ctx?.sessionManager?.getSessionId === \"function\"
        ? ctx.sessionManager.getSessionId()
        : undefined;
    if (typeof managerSessionId === \"string\" && managerSessionId) {
      latestSessionId = managerSessionId;
    }
    if (latestSessionId) {
      persistSessionSnapshot(latestSessionId);
    }
  });

  // When Pi auto-retries a failed request, it calls agent.continue() internally which
  // emits a new agent_start event before the retry runs. By cancelling any pending
  // error-based shutdown timer here we ensure the retry has time to complete and emit
  // its own agent_end (success or error) before we finalize. This is the primary
  // mechanism for surviving multiple retries — the 30s error delay is only the last-
  // resort fallback when all retries are exhausted and no new agent_start arrives.
  pi.on(\"agent_start\", () => {
    clearPendingShutdown();
  });

  pi.on(\"agent_end\", async (event, ctx) => {
    const messages = Array.isArray(event?.messages) ? event.messages : [];
    const stopReason = extractLatestAssistantStopReason(messages);

    // Esc-aborted runs should keep Pi open for user interaction.
    if (stopReason === \"aborted\") {
      return;
    }

    if (completed) return;

    // Cancel any previously scheduled shutdown (e.g. from a prior error agent_end).
    clearPendingShutdown();

    // When stopReason is \"error\", Pi may internally auto-retry the request.
    // Pi fires agent_start before each retry attempt (via agent.continue() →
    // runAgentLoopContinue), so the agent_start handler above will cancel this
    // timer before it fires during an active retry. The long delay is therefore
    // only the fallback for when all retries are exhausted and no new agent_start
    // arrives within errorAgentEndDelayMs.
    const delay = stopReason === \"error\" ? errorAgentEndDelayMs : shutdownDelayMs;

    pendingShutdownTimer = setTimeout(() => {
      pendingShutdownTimer = undefined;
      if (completed) {
        return;
      }
      completed = true;
      void finalizeAndShutdown(event, ctx);
    }, delay);
  });
}
"""
        return source.replace("__CAPTURE_PATH__", capture_literal)

    def _create_live_auto_exit_extension_file(self, capture_path: Optional[str]) -> Optional[Path]:
        """Create a temporary live-mode extension file and return its path."""
        try:
            fd, temp_path = tempfile.mkstemp(prefix="juno-pi-live-auto-exit-", suffix=".ts")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(self._build_live_auto_exit_extension_source(capture_path))
            return Path(temp_path)
        except Exception as exc:
            print(f"Warning: Failed to create live auto-exit extension: {exc}", file=sys.stderr)
            return None

    def _open_live_tty_stdin(self) -> Optional[TextIO]:
        """Open /dev/tty for live-mode stdin fallback when stdin is redirected."""
        try:
            return open("/dev/tty", "r", encoding="utf-8", errors="ignore")
        except OSError:
            return None

    def run_pi(self, cmd: List[str], args: argparse.Namespace,
               stdin_prompt: Optional[str] = None) -> int:
        """Execute the Pi CLI and stream/format its JSON output.

        Args:
            cmd: Command argument list from build_pi_command.
            args: Parsed argparse namespace.
            stdin_prompt: If set, pipe this text via stdin to the Pi CLI
                          (used for multiline/large prompts).
        """
        verbose = args.verbose
        pretty = args.pretty.lower() != "false"
        capture_path = os.environ.get("JUNO_SUBAGENT_CAPTURE_PATH")
        if not os.environ.get("JUNO_TOOL_ID"):
            # Ignore inherited capture paths outside yylo shell-backend execution.
            capture_path = None
        if capture_path:
            # Each invocation should start with a clean capture file. This avoids
            # stale inherited env values from previous runs poisoning status.
            try:
                Path(capture_path).unlink(missing_ok=True)
            except Exception:
                pass
        hide_types = self._build_hide_types()
        self._buffered_tool_stdout_lines.clear()
        self._cancel_all_tool_running()
        self._reset_run_cost_tracking()
        cancel_delayed_toolcalls = lambda: None
        stderr_error_messages: List[str] = []
        stderr_output_lines: List[str] = []

        resume_session = getattr(args, "resume", None)
        if isinstance(resume_session, str) and resume_session.strip():
            self.session_id = resume_session.strip()

        if verbose:
            # Truncate prompt in display to avoid confusing multi-line output
            display_cmd = list(cmd)
            if stdin_prompt:
                first_line = stdin_prompt.split("\n")[0][:60]
                display_cmd.append(f'[stdin: "{first_line}..." ({len(stdin_prompt)} chars)]')
            else:
                filtered = []
                skip_next = False
                for i, part in enumerate(cmd):
                    if skip_next:
                        skip_next = False
                        continue
                    if part == "-p" and i + 1 < len(cmd):
                        prompt_val = cmd[i + 1]
                        if len(prompt_val) > 80 or "\n" in prompt_val:
                            first_line = prompt_val.split("\n")[0][:60]
                            filtered.append(f'-p "{first_line}..." ({len(prompt_val)} chars)')
                        else:
                            filtered.append(f"-p {prompt_val}")
                        skip_next = True
                    else:
                        filtered.append(part)
                display_cmd = filtered
            # Only show Executing once: skip when running under yylo shell backend
            # (shell backend already logs the command in debug mode)
            if not capture_path:
                print(f"Executing: {' '.join(display_cmd)}", file=sys.stderr)
                print("-" * 80, file=sys.stderr)

        process: Optional[subprocess.Popen] = None
        live_mode_requested = bool(getattr(args, "live", False))
        stdin_has_tty = (
            hasattr(sys.stdin, "isatty")
            and sys.stdin.isatty()
        )
        stdout_has_tty = (
            hasattr(sys.stdout, "isatty")
            and sys.stdout.isatty()
        )
        live_tty_stdin: Optional[TextIO] = None
        if live_mode_requested and stdout_has_tty and not stdin_has_tty:
            live_tty_stdin = self._open_live_tty_stdin()

        is_live_tty_passthrough = (
            live_mode_requested
            and stdout_has_tty
            and (stdin_has_tty or live_tty_stdin is not None)
        )

        try:
            if is_live_tty_passthrough:
                # Interactive live mode: attach Pi directly to the current terminal.
                # Keep stdout inherited for full-screen TUI rendering/input, but
                # capture stderr so terminal provider errors can still propagate.
                popen_kwargs = {
                    "cwd": self.project_path,
                    "stderr": subprocess.PIPE,
                    "text": True,
                    "universal_newlines": True,
                }
                if live_tty_stdin is not None:
                    popen_kwargs["stdin"] = live_tty_stdin

                try:
                    process = subprocess.Popen(cmd, **popen_kwargs)

                    def _live_tty_stderr_reader():
                        """Read stderr during live TTY mode and capture terminal failures."""
                        try:
                            if process.stderr:
                                for stderr_line in process.stderr:
                                    stderr_output_lines.append(stderr_line)
                                    print(stderr_line, end="", file=sys.stderr, flush=True)
                                    extracted_error = self._extract_error_message_from_text(stderr_line)
                                    if extracted_error:
                                        stderr_error_messages.append(extracted_error)
                        except (ValueError, OSError):
                            pass

                    stderr_thread = threading.Thread(target=_live_tty_stderr_reader, daemon=True)
                    stderr_thread.start()

                    process.wait()
                    stderr_thread.join(timeout=3)

                    _, suppress_stderr_promotion = self._apply_capture_result_event(capture_path)

                    final_stderr_error_message = self._extract_error_message_from_stderr_output(
                        "".join(stderr_output_lines)
                    )
                    if not final_stderr_error_message and stderr_error_messages:
                        final_stderr_error_message = stderr_error_messages[-1]
                    if (
                        final_stderr_error_message
                        and not suppress_stderr_promotion
                        and self._should_promote_stderr_error(self.last_result_event)
                    ):
                        self.last_result_event = self._build_error_result_event(final_stderr_error_message)

                    self._write_capture_file(capture_path)

                    final_return_code = process.returncode or 0
                    if final_return_code == 0 and self._is_error_result_event(self.last_result_event):
                        final_return_code = 1

                    return final_return_code
                finally:
                    if live_tty_stdin is not None:
                        try:
                            live_tty_stdin.close()
                        except OSError:
                            pass

            process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE if stdin_prompt else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                universal_newlines=True,
                cwd=self.project_path,
            )

            # Pipe the prompt via stdin if using stdin mode (multiline/large prompts).
            # Pi CLI reads stdin when isTTY is false and prepends it to messages.
            if stdin_prompt and process.stdin:
                try:
                    process.stdin.write(stdin_prompt)
                    process.stdin.close()
                except BrokenPipeError:
                    pass  # Process may have exited early

            # Watchdog thread: handles stdout pipe blocking after process exit.
            wait_timeout = int(os.environ.get("PI_WAIT_TIMEOUT", "30"))
            output_done = threading.Event()

            def _stdout_watchdog():
                """Terminate process and close stdout pipe if it hangs after output."""
                while not output_done.is_set():
                    if process.poll() is not None:
                        break
                    output_done.wait(timeout=1)

                if output_done.is_set() and process.poll() is None:
                    try:
                        process.wait(timeout=wait_timeout)
                    except subprocess.TimeoutExpired:
                        print(
                            f"Warning: Pi process did not exit within {wait_timeout}s after output. Terminating.",
                            file=sys.stderr,
                        )
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            print("Warning: Pi process did not respond to SIGTERM. Killing.", file=sys.stderr)
                            process.kill()
                            try:
                                process.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                pass

                time.sleep(2)
                try:
                    if process.stdout and not process.stdout.closed:
                        process.stdout.close()
                except Exception:
                    pass

            watchdog = threading.Thread(target=_stdout_watchdog, daemon=True)
            watchdog.start()

            # Stream stderr in a separate thread so Pi diagnostic output is visible
            def _stderr_reader():
                """Read stderr, forward to stderr, and capture terminal error signals."""
                try:
                    if process.stderr:
                        for stderr_line in process.stderr:
                            stderr_output_lines.append(stderr_line)
                            print(stderr_line, end="", file=sys.stderr, flush=True)
                            extracted_error = self._extract_error_message_from_text(stderr_line)
                            if extracted_error:
                                stderr_error_messages.append(extracted_error)
                except (ValueError, OSError):
                    pass

            stderr_thread = threading.Thread(target=_stderr_reader, daemon=True)
            stderr_thread.start()

            cancel_delayed_toolcalls = lambda: None

            if process.stdout:
                pending_tool_execution_end: Optional[dict] = None
                pending_turn_end_after_tool: Optional[dict] = None
                toolcall_end_delay_seconds = self._toolcall_end_delay_seconds()
                pending_delayed_toolcalls: Dict[int, dict] = {}
                delayed_toolcalls_lock = threading.Lock()
                delayed_toolcall_seq = 0

                def _extract_fallback_toolcall_name(parsed_event: dict) -> Optional[str]:
                    if parsed_event.get("type") != "message_update":
                        return None
                    assistant_event = parsed_event.get("assistantMessageEvent")
                    if not isinstance(assistant_event, dict) or assistant_event.get("type") != "toolcall_end":
                        return None
                    tool_call = assistant_event.get("toolCall")
                    if not isinstance(tool_call, dict):
                        return None
                    tool_call_id = tool_call.get("toolCallId")
                    if isinstance(tool_call_id, str) and tool_call_id.strip():
                        return None
                    name = tool_call.get("name", "")
                    return name if isinstance(name, str) else ""

                def _format_deferred_toolcall(parsed_event: dict, mode: str) -> Optional[str]:
                    if mode == self.PRETTIFIER_LIVE:
                        return self._format_event_live(parsed_event)
                    if mode == self.PRETTIFIER_CODEX:
                        return self._format_pi_codex_event(parsed_event)
                    if mode == self.PRETTIFIER_CLAUDE:
                        return self._format_event_pretty_claude(parsed_event)
                    return self._format_event_pretty(parsed_event)

                def _emit_stdout(formatted: str, raw: bool = False) -> None:
                    styled = self._highlight_formatted_header(formatted) if pretty else formatted
                    if raw:
                        sys.stdout.write(styled)
                        sys.stdout.flush()
                        return
                    print(styled, flush=True)

                def _schedule_delayed_toolcall(parsed_event: dict, tool_name: str, mode: str) -> None:
                    nonlocal delayed_toolcall_seq

                    def _emit_delayed_toolcall(event_payload: dict, event_mode: str) -> None:
                        formatted = _format_deferred_toolcall(event_payload, event_mode)
                        if not formatted:
                            return
                        _emit_stdout(formatted, raw=event_mode == self.PRETTIFIER_LIVE)

                    if toolcall_end_delay_seconds <= 0:
                        _emit_delayed_toolcall(parsed_event, mode)
                        return

                    delayed_toolcall_seq += 1
                    entry_id = delayed_toolcall_seq
                    entry: Dict = {
                        "id": entry_id,
                        "tool": tool_name,
                        "event": parsed_event,
                        "mode": mode,
                    }

                    def _timer_emit() -> None:
                        with delayed_toolcalls_lock:
                            pending = pending_delayed_toolcalls.pop(entry_id, None)
                        if not pending:
                            return
                        _emit_delayed_toolcall(pending["event"], pending["mode"])

                    timer = threading.Timer(toolcall_end_delay_seconds, _timer_emit)
                    timer.daemon = True
                    entry["timer"] = timer
                    with delayed_toolcalls_lock:
                        pending_delayed_toolcalls[entry_id] = entry
                    timer.start()

                def _cancel_delayed_toolcall(tool_name: str) -> None:
                    with delayed_toolcalls_lock:
                        if not pending_delayed_toolcalls:
                            return

                        selected_id: Optional[int] = None
                        if tool_name:
                            for entry_id, entry in pending_delayed_toolcalls.items():
                                if entry.get("tool") == tool_name:
                                    selected_id = entry_id
                                    break

                        if selected_id is None:
                            selected_id = min(pending_delayed_toolcalls.keys())

                        pending = pending_delayed_toolcalls.pop(selected_id, None)

                    if pending:
                        timer = pending.get("timer")
                        if timer:
                            timer.cancel()

                def _cancel_all_delayed_toolcalls() -> None:
                    with delayed_toolcalls_lock:
                        pending = list(pending_delayed_toolcalls.values())
                        pending_delayed_toolcalls.clear()
                    for entry in pending:
                        timer = entry.get("timer")
                        if timer:
                            timer.cancel()

                cancel_delayed_toolcalls = _cancel_all_delayed_toolcalls

                def _emit_parsed_event(parsed_event: dict, raw_json_line: Optional[str] = None) -> None:
                    event_type = parsed_event.get("type", "")

                    # Capture session ID as early as possible from any event shape.
                    event_session_id = self._extract_session_id_from_event(parsed_event)
                    if event_session_id:
                        self.session_id = event_session_id
                        if (
                            isinstance(self.last_result_event, dict)
                            and not self.last_result_event.get("session_id")
                            and isinstance(self.session_id, str)
                            and self.session_id.strip()
                        ):
                            self.last_result_event["session_id"] = self.session_id

                    # Capture terminal error events even when upstream exits with code 0.
                    error_message = self._extract_error_message_from_event(parsed_event)
                    if error_message:
                        self.last_result_event = self._build_error_result_event(error_message, parsed_event)

                    # Track per-run assistant usage from stream events.
                    self._track_assistant_usage_from_event(parsed_event)

                    # Ensure agent_end reflects cumulative per-run totals when available.
                    if event_type == "agent_end":
                        accumulated_total_cost = self._get_accumulated_total_cost_usd()
                        if accumulated_total_cost is not None:
                            parsed_event["total_cost_usd"] = accumulated_total_cost
                        if isinstance(self._run_usage_totals, dict):
                            parsed_event["usage"] = self._run_usage_totals

                    # Capture result event for shell backend
                    if event_type == "agent_end":
                        # agent_end has a 'messages' array; extract final assistant text
                        messages = parsed_event.get("messages", [])
                        text = ""
                        assistant_stop_reason = ""
                        assistant_error_message = ""
                        if isinstance(messages, list):
                            # Use the latest assistant message as source of truth.
                            for m in reversed(messages):
                                if isinstance(m, dict) and m.get("role") == "assistant":
                                    stop_reason = m.get("stopReason")
                                    if isinstance(stop_reason, str):
                                        assistant_stop_reason = stop_reason.strip()
                                    error_message = m.get("errorMessage")
                                    if isinstance(error_message, str):
                                        assistant_error_message = error_message.strip()
                                    text = self._extract_text_from_message(m)
                                    break

                        if assistant_stop_reason in {"error", "aborted"}:
                            terminal_error = (
                                assistant_error_message
                                or text
                                or f"Request {assistant_stop_reason}"
                            )
                            self.last_result_event = self._build_error_result_event(
                                terminal_error,
                                parsed_event,
                            )
                        elif text:
                            provider_error = self._extract_provider_error_from_result_text(text)
                            if provider_error:
                                self.last_result_event = self._build_error_result_event(provider_error, parsed_event)
                            else:
                                self.last_result_event = self._build_success_result_event(text, parsed_event)
                        elif not self._is_error_result_event(self.last_result_event):
                            self.last_result_event = parsed_event
                    elif event_type == "message":
                        # OpenAI-compatible format: capture last assistant message
                        msg = parsed_event.get("message", {})
                        if isinstance(msg, dict) and msg.get("role") == "assistant":
                            text = self._extract_text_from_message(msg)
                            if text:
                                provider_error = self._extract_provider_error_from_result_text(text)
                                if provider_error:
                                    self.last_result_event = self._build_error_result_event(provider_error, parsed_event)
                                else:
                                    self.last_result_event = self._build_success_result_event(text, parsed_event)
                    elif event_type == "turn_end":
                        # turn_end may contain the final assistant message
                        msg = parsed_event.get("message", {})
                        if isinstance(msg, dict):
                            text = self._extract_text_from_message(msg)
                            if text:
                                provider_error = self._extract_provider_error_from_result_text(text)
                                if provider_error:
                                    self.last_result_event = self._build_error_result_event(provider_error, parsed_event)
                                else:
                                    self.last_result_event = self._build_success_result_event(text, parsed_event)

                    # Filter hidden stream types (live mode handles its own filtering)
                    if event_type in hide_types and self.prettifier_mode != self.PRETTIFIER_LIVE:
                        return

                    # Schedule visible progress only in the active stream loop; the
                    # pure formatter helpers remain deterministic for tests/reuse.
                    if pretty and event_type == "tool_execution_start":
                        self._buffer_exec_start(parsed_event)
                        tool_call_id = parsed_event.get("toolCallId")
                        pending_start = self._pending_exec_starts.get(tool_call_id) if isinstance(tool_call_id, str) else None
                        if isinstance(tool_call_id, str) and isinstance(pending_start, dict):
                            self._schedule_tool_running(tool_call_id, pending_start)

                    # Fallback toolcall_end events (without toolCallId) are delayed so
                    # short tool executions only show the final combined tool event.
                    if pretty:
                        fallback_tool_name = _extract_fallback_toolcall_name(parsed_event)
                        if fallback_tool_name is not None:
                            _schedule_delayed_toolcall(parsed_event, fallback_tool_name, self.prettifier_mode)
                            return

                    # Live stream mode: stream deltas in real-time
                    if self.prettifier_mode == self.PRETTIFIER_LIVE:
                        if event_type in hide_types:
                            # In live mode, still suppress session/compaction/retry events
                            # but NOT message_start/message_end (handled by _format_event_live)
                            if event_type not in ("message_start", "message_end"):
                                return
                        formatted_live = self._format_event_live(parsed_event)
                        if formatted_live is not None:
                            if formatted_live == "":
                                return
                            _emit_stdout(formatted_live, raw=True)
                        else:
                            # Fallback: print raw JSON for unhandled event types
                            print(json.dumps(parsed_event, ensure_ascii=False), flush=True)
                        return

                    # Format and print using model-appropriate prettifier
                    if pretty:
                        if self.prettifier_mode == self.PRETTIFIER_CODEX:
                            # Try Pi-wrapped Codex format first (role-based messages)
                            if "role" in parsed_event:
                                formatted = self._format_pi_codex_message(parsed_event)
                            else:
                                # Try Pi event handler (message_update, turn_end, etc.)
                                formatted = self._format_pi_codex_event(parsed_event)
                                if formatted is None:
                                    # Try native Codex event handler
                                    formatted = self._format_event_pretty_codex(parsed_event)
                            if formatted is None:
                                # Sanitize before raw JSON fallback: strip thinkingSignature,
                                # encrypted_content, and metadata from nested Codex events.
                                self._sanitize_codex_event(parsed_event, strip_metadata=True)
                                formatted = json.dumps(parsed_event, ensure_ascii=False)
                            elif formatted == "":
                                return
                        elif self.prettifier_mode == self.PRETTIFIER_CLAUDE:
                            formatted = self._format_event_pretty_claude(parsed_event)
                        else:
                            formatted = self._format_event_pretty(parsed_event)
                        if formatted is not None:
                            _emit_stdout(formatted)
                    else:
                        if raw_json_line is not None:
                            print(raw_json_line, flush=True)
                        else:
                            print(json.dumps(parsed_event, ensure_ascii=False), flush=True)

                def _merge_buffered_tool_stdout_into(event_payload: dict) -> None:
                    buffered_text = "\n".join(self._buffered_tool_stdout_lines).strip()
                    if not buffered_text:
                        self._buffered_tool_stdout_lines.clear()
                        return

                    result_val = event_payload.get("result")
                    if result_val in (None, "", [], {}):
                        event_payload["result"] = buffered_text
                    elif isinstance(result_val, str):
                        existing = self._strip_ansi_sequences(result_val)
                        if existing:
                            if not existing.endswith("\n"):
                                existing += "\n"
                            event_payload["result"] = existing + buffered_text
                        else:
                            event_payload["result"] = buffered_text
                    else:
                        # Keep complex result structures untouched; print trailing raw lines
                        # before the next structured event for stable transcript ordering.
                        print(buffered_text, flush=True)

                    self._buffered_tool_stdout_lines.clear()

                def _flush_pending_tool_events() -> None:
                    nonlocal pending_tool_execution_end, pending_turn_end_after_tool
                    if pending_tool_execution_end is not None:
                        _merge_buffered_tool_stdout_into(pending_tool_execution_end)
                        _emit_parsed_event(pending_tool_execution_end)
                        pending_tool_execution_end = None

                    if pending_turn_end_after_tool is not None:
                        if self._buffered_tool_stdout_lines:
                            print("\n".join(self._buffered_tool_stdout_lines), flush=True)
                            self._buffered_tool_stdout_lines.clear()
                        _emit_parsed_event(pending_turn_end_after_tool)
                        pending_turn_end_after_tool = None

                try:
                    for raw_line in process.stdout:
                        line = raw_line.rstrip("\n\r")
                        if not line.strip():
                            continue

                        # Try to parse as JSON
                        try:
                            parsed = json.loads(line)
                        except json.JSONDecodeError:
                            # Non-JSON output (raw tool stdout). In pretty mode, buffer raw
                            # lines while tool execution events are pending to avoid
                            # interleaving with structured events (e.g. turn_end).
                            if pretty and (
                                self._in_tool_execution
                                or pending_tool_execution_end is not None
                                or pending_turn_end_after_tool is not None
                            ):
                                self._buffered_tool_stdout_lines.append(self._strip_ansi_sequences(line))
                                continue
                            print(line, flush=True)
                            continue

                        event_type = parsed.get("type", "")

                        if pretty and event_type == "tool_execution_start":
                            # Reset raw tool stdout buffer per tool execution.
                            self._buffered_tool_stdout_lines.clear()

                        if pretty and event_type == "tool_execution_end":
                            # Tool finished before the delayed fallback timer fired — suppress
                            # the pending fallback toolcall_end preview.
                            tool_name = parsed.get("toolName", "")
                            _cancel_delayed_toolcall(tool_name if isinstance(tool_name, str) else "")

                            # Defer emission so any trailing raw stdout can be grouped before
                            # downstream structured metadata like turn_end.
                            pending_tool_execution_end = parsed
                            continue

                        if pretty and event_type == "turn_end" and pending_tool_execution_end is not None:
                            # Hold turn_end until buffered trailing raw stdout is flushed with
                            # the pending tool event.
                            pending_turn_end_after_tool = parsed
                            continue

                        if pretty and (
                            pending_tool_execution_end is not None or pending_turn_end_after_tool is not None
                        ):
                            _flush_pending_tool_events()

                        _emit_parsed_event(parsed, raw_json_line=line)

                    # Flush any deferred tool/turn events at end-of-stream.
                    if pretty and (
                        pending_tool_execution_end is not None or pending_turn_end_after_tool is not None
                    ):
                        _flush_pending_tool_events()
                    elif self._buffered_tool_stdout_lines:
                        print("\n".join(self._buffered_tool_stdout_lines), flush=True)
                        self._buffered_tool_stdout_lines.clear()

                except ValueError:
                    # Watchdog closed stdout — expected when process exits but pipe stays open.
                    pass

            # Signal watchdog that output loop is done
            output_done.set()
            cancel_delayed_toolcalls()
            self._cancel_all_tool_running()

            # Wait for process cleanup
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

            # Wait for stderr thread to finish before deriving fallback errors.
            stderr_thread.join(timeout=3)

            _, suppress_stderr_promotion = self._apply_capture_result_event(capture_path)

            # If stderr surfaced a terminal error, persist the failure unless a
            # non-assistant-text success envelope is already authoritative.
            final_stderr_error_message = self._extract_error_message_from_stderr_output(
                "".join(stderr_output_lines)
            )
            if not final_stderr_error_message and stderr_error_messages:
                final_stderr_error_message = stderr_error_messages[-1]
            if (
                final_stderr_error_message
                and not suppress_stderr_promotion
                and self._should_promote_stderr_error(self.last_result_event)
            ):
                self.last_result_event = self._build_error_result_event(final_stderr_error_message)

            # Write capture file for shell backend
            self._write_capture_file(capture_path)

            final_return_code = process.returncode or 0
            if final_return_code == 0 and self._is_error_result_event(self.last_result_event):
                final_return_code = 1

            return final_return_code

        except KeyboardInterrupt:
            print("\nInterrupted by user", file=sys.stderr)
            cancel_delayed_toolcalls()
            self._cancel_all_tool_running()
            try:
                if process is not None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
            except Exception:
                pass
            self._write_capture_file(capture_path)
            return 130

        except Exception as e:
            print(f"Error executing pi: {e}", file=sys.stderr)
            cancel_delayed_toolcalls()
            self._cancel_all_tool_running()
            try:
                if process is not None and process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
            except Exception:
                pass
            self._write_capture_file(capture_path)
            return 1

    def run(self) -> int:
        """Main execution flow."""
        args = self.parse_arguments()
        headless_ui_error = self._configure_headless_ui()
        if headless_ui_error:
            print(f"Error: {headless_ui_error}", file=sys.stderr)
            return 1

        # Prompt handling
        prompt_value = args.prompt or os.environ.get("JUNO_INSTRUCTION")
        resume_session = args.resume if isinstance(args.resume, str) and args.resume.strip() else None
        live_manual_session = bool(args.live and args.live_manual)

        if (
            args.live
            and resume_session
            and not prompt_value
            and not args.prompt_file
        ):
            # Defensive fallback for direct wrapper usage: allow promptless live resume
            # even if --live-manual was not passed explicitly.
            live_manual_session = True

        if live_manual_session:
            args.live_manual = True

        if not prompt_value and not args.prompt_file and not live_manual_session:
            print("Error: Either -p/--prompt or -pp/--prompt-file is required.", file=sys.stderr)
            print("\nRun 'pi.py --help' for usage information.", file=sys.stderr)
            return 1

        if not self.check_pi_installed():
            print(
                "Error: Pi CLI is not available. Please install it:\n"
                "  npm install -g @mariozechner/pi-coding-agent\n"
                "See: https://pi.dev/",
                file=sys.stderr,
            )
            return 1

        self.project_path = os.path.abspath(args.cd)
        if not os.path.isdir(self.project_path):
            print(f"Error: Project path does not exist: {self.project_path}", file=sys.stderr)
            return 1

        try:
            self.model_name = self.expand_model_shorthand(args.model)
        except ModelShortcutError as error:
            print(f"Error: {error}", file=sys.stderr)
            return 1
        finally:
            sanitize_model_shortcut_environment()
        self.prettifier_mode = self._detect_prettifier_mode(self.model_name)
        self.verbose = args.verbose

        # Verbose mode enables live stream prettifier for real-time output.
        # Codex models already default to LIVE; this ensures all models get
        # real-time streaming when -v is used.
        if args.verbose:
            self.prettifier_mode = self.PRETTIFIER_LIVE

        if self.verbose:
            print(f"Prettifier mode: {self.prettifier_mode} (model: {self.model_name})", file=sys.stderr)

        if args.prompt_file:
            self.prompt = self.read_prompt_file(args.prompt_file)
        elif prompt_value:
            self.prompt = prompt_value
        else:
            self.prompt = ""

        if args.live and args.no_extensions and not live_manual_session:
            print("Error: --live requires extensions enabled (remove --no-extensions).", file=sys.stderr)
            return 1

        live_extension_file: Optional[Path] = None
        if args.live and not live_manual_session:
            capture_path = os.environ.get("JUNO_SUBAGENT_CAPTURE_PATH")
            if not os.environ.get("JUNO_TOOL_ID"):
                capture_path = None
            live_extension_file = self._create_live_auto_exit_extension_file(capture_path)
            if not live_extension_file:
                print("Error: Could not create live auto-exit extension.", file=sys.stderr)
                return 1

        try:
            cmd, stdin_prompt = self.build_pi_command(
                args,
                live_extension_path=str(live_extension_file) if live_extension_file else None,
            )
            return self.run_pi(cmd, args, stdin_prompt=stdin_prompt)
        finally:
            if live_extension_file is not None:
                try:
                    live_extension_file.unlink(missing_ok=True)
                except Exception as e:
                    print(f"Warning: Failed to remove temp live extension: {e}", file=sys.stderr)
            self._cleanup_managed_prompt_files()


def main():
    service = PiService()
    sys.exit(service.run())


if __name__ == "__main__":
    main()
