#!/usr/bin/env python3
"""
Codex Service Script for yylo
This script provides a wrapper around OpenAI Codex CLI with configurable options.
"""

import argparse
import os
import subprocess
import sys
import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from environment_boundary import (
    ModelShortcutError,
    record_harness_handoff,
    resolve_model_shortcut,
    sanitize_current_process_environment,
    sanitize_model_shortcut_environment,
)

sanitize_current_process_environment()


DEFAULT_PROMPT_ARG_MAX_BYTES = 64 * 1024
PROMPT_ARG_MAX_BYTES_ENV = "JUNO_PROMPT_ARG_MAX_BYTES"


def _positive_int_env(name: str, fallback: int) -> int:
    value = os.environ.get(name, "")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


class CodexService:
    """Service wrapper for OpenAI Codex CLI"""

    # Default configuration
    DEFAULT_MODEL = "gpt-5.3-codex"
    DEFAULT_AUTO_INSTRUCTION = """You are an AI coding assistant. Follow the instructions provided and generate high-quality code."""

    # Model shorthand mappings (colon-prefixed names expand to full model IDs)
    MODEL_SHORTHANDS = {
        ":codex": "gpt-5.3-codex",
        ":codex-mini": "gpt-5.1-codex-mini",
        ":gpt-5": "gpt-5",
        ":mini": "gpt-5-codex-mini",
    }

    def __init__(self):
        self.model_name = self.DEFAULT_MODEL
        self.auto_instruction = self.DEFAULT_AUTO_INSTRUCTION
        self.project_path = os.getcwd()
        self.prompt = ""
        self.additional_args: List[str] = []
        self.verbose = False
        self._item_counter = 0
        self._stdin_prompt: Optional[str] = None

    def expand_model_shorthand(self, model: str) -> str:
        """Resolve shipped and project model shortcuts for Codex."""
        return resolve_model_shortcut(model, self.MODEL_SHORTHANDS, "codex")

    def _prompt_arg_max_bytes(self) -> int:
        return _positive_int_env(PROMPT_ARG_MAX_BYTES_ENV, DEFAULT_PROMPT_ARG_MAX_BYTES)

    def _is_prompt_oversized(self, prompt: str) -> bool:
        return len(prompt.encode("utf-8")) > self._prompt_arg_max_bytes()

    def check_codex_installed(self) -> bool:
        """Check if codex CLI is installed and available"""
        try:
            result = subprocess.run(
                ["which", "codex"],
                capture_output=True,
                text=True,
                check=False
            )
            return result.returncode == 0
        except Exception:
            return False

    def parse_arguments(self) -> argparse.Namespace:
        """Parse command line arguments"""
        parser = argparse.ArgumentParser(
            description="Codex Service - Wrapper for OpenAI Codex CLI",
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog="""
Examples:
  %(prog)s -p "Write a hello world function"
  %(prog)s -pp prompt.txt --cd /path/to/project
  %(prog)s -p "Add tests" -m gpt-4 -c custom_arg=value
  %(prog)s -p "Optimize code" -m :codex  # uses gpt-5.3-codex

Environment Variables:
  CODEX_MODEL                Model name (supports shorthand, default: gpt-5.3-codex)
  CODEX_HIDE_STREAM_TYPES    Comma-separated list of streaming msg types to hide
                             Default: turn_diff,token_count,exec_command_output_delta
  YYLO_HIDE_STREAM_TYPES Same as CODEX_HIDE_STREAM_TYPES (alias)
            """
        )

        # Core arguments
        prompt_group = parser.add_mutually_exclusive_group(required=False)
        prompt_group.add_argument(
            "-p", "--prompt",
            type=str,
            help="Prompt text to send to codex"
        )
        prompt_group.add_argument(
            "-pp", "--prompt-file",
            type=str,
            help="Path to file containing the prompt"
        )

        parser.add_argument(
            "--cd",
            type=str,
            default=os.getcwd(),
            help="Project path (absolute path). Default: current directory"
        )

        parser.add_argument(
            "-m", "--model",
            type=str,
            default=os.environ.get("CODEX_MODEL", self.DEFAULT_MODEL),
            help=f"Model name. Supports shorthand (e.g., ':codex', ':gpt-5', ':mini') or full model ID. Default: {self.DEFAULT_MODEL} (env: CODEX_MODEL)"
        )

        parser.add_argument(
            "--auto-instruction",
            type=str,
            default=self.DEFAULT_AUTO_INSTRUCTION,
            help="Auto instruction to prepend to prompt"
        )

        parser.add_argument(
            "-c", "--config",
            action="append",
            dest="configs",
            help="Additional codex config arguments (can be used multiple times)"
        )

        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Enable verbose output"
        )

        return parser.parse_args()

    def _first_nonempty_str(self, *values: Optional[str]) -> str:
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

    def _extract_message_text(self, payload: dict) -> str:
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

    def _normalize_item_id(self, payload: dict, outer_type: str) -> Optional[str]:
        """
        Prefer the existing id on item.* payloads; otherwise synthesize sequential item_{n}.
        Maintains a per-run counter so missing ids still expose turn counts.
        """
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

    def read_prompt_file(self, file_path: str) -> str:
        """Read prompt from a file"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return f.read().strip()
        except FileNotFoundError:
            print(f"Error: Prompt file not found: {file_path}", file=sys.stderr)
            sys.exit(1)
        except Exception as e:
            print(f"Error reading prompt file: {e}", file=sys.stderr)
            sys.exit(1)

    def build_codex_command(self, args: argparse.Namespace) -> List[str]:
        """Build the codex command with all arguments"""
        # Start with base command
        cmd = [
            "codex",
            "--cd", self.project_path,
            "-m", self.model_name,
        ]

        # Add default config arguments
        default_configs = [
            "include_apply_patch_tool=true",
            "use_experimental_streamable_shell_tool=true",
            "sandbox_mode=danger-full-access"
        ]

        # Track which configs are already set
        config_keys = set()
        user_configs = []

        # Process user-provided configs
        if args.configs:
            for config in args.configs:
                key = config.split('=')[0] if '=' in config else config
                config_keys.add(key)
                user_configs.append(config)

        # Add default configs that weren't overridden
        for config in default_configs:
            key = config.split('=')[0]
            if key not in config_keys:
                cmd.extend(["-c", config])

        # Add user configs (these will override defaults if keys match)
        for config in user_configs:
            cmd.extend(["-c", config])

        # Build the full prompt (auto_instruction + user prompt)
        full_prompt = f"{self.auto_instruction}\n\n{self.prompt}"

        # Add exec command with prompt. Small prompts stay positional for CLI
        # compatibility; oversized prompts use Codex's documented stdin marker
        # to avoid reintroducing OS E2BIG when this wrapper receives a prompt
        # file from shell-backend.
        self._stdin_prompt = None
        if self._is_prompt_oversized(full_prompt):
            self._stdin_prompt = full_prompt
            cmd.extend(["exec", "-"])
        else:
            cmd.extend(["exec", full_prompt])

        # Add --json flag for streaming support
        # This is CRITICAL for codex to output streaming responses
        # Without this flag, codex will not stream progress updates
        cmd.append("--json")

        return cmd

    def _format_msg_pretty(
        self,
        msg_type: str,
        payload: dict,
        outer_type: str = "",
        item_id: Optional[str] = None,
    ) -> Optional[str]:
        """
        Pretty format for specific msg types to be human readable while
        preserving a compact JSON header line that includes the msg.type.

        - agent_message/message/assistant: render message text as multi-line block
        - agent_reasoning: render 'text' field as multi-line text
        - exec_command_end: only output 'formatted_output' (suppress other fields)
        - token_count: fully suppressed (no final summary emission)

        Returns a string to print, or None to fall back to raw printing.
        """
        try:
            now = datetime.now().strftime("%I:%M:%S %p")
            msg_type = (msg_type or "").strip()
            header_type = (outer_type or msg_type).strip()
            base_type = header_type or msg_type or "message"

            def make_header(type_value: str):
                hdr = {"type": type_value, "datetime": now}
                if item_id:
                    hdr["id"] = item_id
                if outer_type and msg_type and outer_type != msg_type:
                    hdr["item_type"] = msg_type
                return hdr

            header = make_header(base_type)

            if isinstance(payload, dict):
                if item_id and "id" not in payload:
                    payload["id"] = item_id
                if payload.get("command"):
                    header["command"] = payload.get("command")
                if payload.get("status"):
                    header["status"] = payload.get("status")
                if payload.get("state") and not header.get("status"):
                    header["status"] = payload.get("state")

            # agent_reasoning → show 'text' human-readable
            if msg_type in {"agent_reasoning", "reasoning"}:
                content = self._extract_reasoning_text(payload)
                header = make_header(header_type or msg_type)
                if "\n" in content:
                    return json.dumps(header, ensure_ascii=False) + "\ntext:\n" + content
                header["text"] = content
                return json.dumps(header, ensure_ascii=False)

            if msg_type in {"agent_message", "message", "assistant_message", "assistant"}:
                content = self._extract_message_text(payload)
                header = make_header(header_type or msg_type)
                if "\n" in content:
                    return json.dumps(header, ensure_ascii=False) + "\nmessage:\n" + content
                if content != "":
                    header["message"] = content
                    return json.dumps(header, ensure_ascii=False)
                if header_type:
                    return json.dumps(header, ensure_ascii=False)

            # exec_command_end → only show 'formatted_output'
            if msg_type == "exec_command_end":
                formatted_output = payload.get("formatted_output", "") if isinstance(payload, dict) else ""
                header = {"type": msg_type, "datetime": now}
                if "\n" in formatted_output:
                    return json.dumps(header, ensure_ascii=False) + "\nformatted_output:\n" + formatted_output
                header["formatted_output"] = formatted_output
                return json.dumps(header, ensure_ascii=False)

            # item.* schema → command_execution blocks
            if msg_type == "command_execution":
                aggregated_output = self._extract_command_output_text(payload)
                if "\n" in aggregated_output:
                    return json.dumps(header, ensure_ascii=False) + "\naggregated_output:\n" + aggregated_output
                if aggregated_output:
                    header["aggregated_output"] = aggregated_output
                    return json.dumps(header, ensure_ascii=False)
                # No output (likely item.started) – still show header if it carries context
                if header_type:
                    return json.dumps(header, ensure_ascii=False)

            return None
        except Exception:
            return None

    def _normalize_event(self, obj_dict: dict):
        """
        Normalize legacy (msg-based) and new item.* schemas into a common tuple.
        Returns (msg_type, payload_dict, outer_type).
        """
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

    def run_codex(self, cmd: List[str], verbose: bool = False) -> int:
        """Execute the codex command and stream output with filtering and pretty-printing

        Robustness improvements:
        - Attempts to parse JSON even if the line has extra prefix/suffix noise
        - Falls back to string suppression for known noisy types if JSON parsing fails
        - Never emits token_count or exec_command_output_delta even on malformed lines
        """
        # Capture file support for structured output (parity with claude.py/pi.py)
        capture_path = os.environ.get("JUNO_SUBAGENT_CAPTURE_PATH")

        if verbose:
            # Truncate prompt in display to avoid confusing multi-line output
            display_cmd = []
            skip_next = False
            for i, part in enumerate(cmd):
                if skip_next:
                    skip_next = False
                    continue
                if part == "-p" and i + 1 < len(cmd):
                    prompt_val = cmd[i + 1]
                    if len(prompt_val) > 80 or "\n" in prompt_val:
                        first_line = prompt_val.split("\n")[0][:60]
                        display_cmd.append(f'-p "{first_line}..." ({len(prompt_val)} chars)')
                    else:
                        display_cmd.append(f"-p {prompt_val}")
                    skip_next = True
                else:
                    display_cmd.append(part)
            if not capture_path:
                print(f"Executing: {' '.join(display_cmd)}", file=sys.stderr)
                print("-" * 80, file=sys.stderr)
        self.last_result_event = None

        def write_capture_file():
            """Persist the final result event for programmatic capture without affecting screen output."""
            if not capture_path or not self.last_result_event:
                return
            try:
                Path(capture_path).write_text(
                    json.dumps(self.last_result_event, ensure_ascii=False),
                    encoding="utf-8"
                )
            except Exception as e:
                print(f"Warning: Failed to write capture file: {e}", file=sys.stderr)

        # Resolve hidden stream types (ENV configurable)
        default_hidden = {"turn_diff", "token_count", "exec_command_output_delta"}
        env_hide_1 = os.environ.get("CODEX_HIDE_STREAM_TYPES", "")
        env_hide_2 = os.environ.get("YYLO_HIDE_STREAM_TYPES", "")
        hide_types = set(default_hidden)
        for env_val in (env_hide_1, env_hide_2):
            if env_val:
                parts = [p.strip() for p in env_val.split(",") if p.strip()]
                hide_types.update(parts)

        # Reset per-run item counter for synthesized ids
        self._item_counter = 0

        # We fully suppress all token_count events (do not emit even at end)
        last_token_count = None

        try:
            # Run the command and stream output
            process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE if self._stdin_prompt else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                universal_newlines=True
            )

            record_harness_handoff("codex")

            if self._stdin_prompt and process.stdin:
                try:
                    process.stdin.write(self._stdin_prompt)
                    process.stdin.close()
                except BrokenPipeError:
                    pass

            # Watchdog thread: handles two scenarios where the stdout loop blocks:
            # 1. Process exits but its stdout pipe stays open (inherited FDs)
            # 2. Process itself never exits (hung event loop)
            # The watchdog waits for an "output done" signal from the main thread
            # (set when the stdout loop finishes or a completion event is received).
            # Once signaled, it gives the process a grace period to exit, then
            # terminates it and closes stdout.
            wait_timeout = int(os.environ.get("CODEX_WAIT_TIMEOUT", "30"))
            output_done = threading.Event()

            def _stdout_watchdog():
                """Terminate process and close stdout pipe if it hangs after output."""
                # Wait until the main thread signals output is done,
                # OR until the process exits on its own (poll every second).
                while not output_done.is_set():
                    if process.poll() is not None:
                        break
                    output_done.wait(timeout=1)

                if output_done.is_set() and process.poll() is None:
                    # Output is done but process hasn't exited — give it grace period.
                    try:
                        process.wait(timeout=wait_timeout)
                    except subprocess.TimeoutExpired:
                        print(
                            f"Warning: Codex process did not exit within {wait_timeout}s after output. Terminating.",
                            file=sys.stderr
                        )
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            print("Warning: Codex process did not respond to SIGTERM. Killing.", file=sys.stderr)
                            process.kill()
                            try:
                                process.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                pass

                # Process has exited (naturally or forcefully).
                # Grace period for remaining pipe data to flush, then close stdout.
                time.sleep(2)
                try:
                    if process.stdout and not process.stdout.closed:
                        process.stdout.close()
                except Exception:
                    pass

            watchdog = threading.Thread(target=_stdout_watchdog, daemon=True)
            watchdog.start()

            def split_json_stream(text: str):
                objs = []
                buf: List[str] = []
                depth = 0
                in_str = False
                esc = False
                started = False
                for ch in text:
                    if in_str:
                        buf.append(ch)
                        if esc:
                            esc = False
                        elif ch == '\\':
                            esc = True
                        elif ch == '"':
                            in_str = False
                        continue
                    if ch == '"':
                        in_str = True
                        buf.append(ch)
                        continue
                    if ch == '{':
                        depth += 1
                        started = True
                        buf.append(ch)
                        continue
                    if ch == '}':
                        depth -= 1
                        buf.append(ch)
                        if started and depth == 0:
                            candidate = ''.join(buf).strip().strip("'\"")
                            if candidate:
                                objs.append(candidate)
                            buf = []
                            started = False
                        continue
                    if started:
                        buf.append(ch)
                remainder = ''.join(buf) if buf else ""
                return objs, remainder

            def handle_obj(obj_dict: dict):
                nonlocal last_token_count
                msg_type_inner, payload_inner, outer_type_inner = self._normalize_event(obj_dict)
                item_id_inner = self._normalize_item_id(payload_inner, outer_type_inner)

                if (
                    item_id_inner
                    and isinstance(obj_dict.get("item"), dict)
                    and not obj_dict["item"].get("id")
                ):
                    obj_dict["item"]["id"] = item_id_inner

                # Track last agent_message for capture file (structured output)
                if msg_type_inner in ("agent_message", "message", "assistant_message", "assistant"):
                    self.last_result_event = obj_dict

                if msg_type_inner == "token_count":
                    last_token_count = obj_dict
                    return  # suppress

                if msg_type_inner and msg_type_inner in hide_types:
                    return  # suppress

                pretty_line_inner = self._format_msg_pretty(
                    msg_type_inner,
                    payload_inner,
                    outer_type_inner,
                    item_id=item_id_inner,
                )
                if pretty_line_inner is not None:
                    print(pretty_line_inner, flush=True)
                else:
                    # print normalized JSON
                    print(json.dumps(obj_dict, ensure_ascii=False), flush=True)

            pending = ""

            if process.stdout:
                try:
                    for raw_line in process.stdout:
                        combined = pending + raw_line
                        if not combined.strip():
                            pending = ""
                            continue

                        # If no braces present at all, treat as plain text (with suppression)
                        if "{" not in combined and "}" not in combined:
                            lower = combined.lower()
                            if (
                                '"token_count"' in lower
                                or '"exec_command_output_delta"' in lower
                                or '"turn_diff"' in lower
                            ):
                                pending = ""
                                continue
                            print(combined, end="" if combined.endswith("\n") else "\n", flush=True)
                            pending = ""
                            continue

                        # Preserve and emit any prefix before the first brace
                        first_brace = combined.find("{")
                        if first_brace > 0:
                            prefix = combined[:first_brace]
                            lower_prefix = prefix.lower()
                            if (
                                '"token_count"' not in lower_prefix
                                and '"exec_command_output_delta"' not in lower_prefix
                                and '"turn_diff"' not in lower_prefix
                                and prefix.strip()
                            ):
                                print(prefix, end="" if prefix.endswith("\n") else "\n", flush=True)
                            combined = combined[first_brace:]

                        parts, pending = split_json_stream(combined)

                        if parts:
                            for part in parts:
                                try:
                                    sub = json.loads(part)
                                    if isinstance(sub, dict):
                                        handle_obj(sub)
                                    else:
                                        low = part.lower()
                                        if (
                                            '"token_count"' in low
                                            or '"exec_command_output_delta"' in low
                                            or '"turn_diff"' in low
                                        ):
                                            continue
                                        print(part, flush=True)
                                except Exception:
                                    low = part.lower()
                                    if (
                                        '"token_count"' in low
                                        or '"exec_command_output_delta"' in low
                                        or '"turn_diff"' in low
                                    ):
                                        continue
                                    print(part, flush=True)
                            continue

                        # No complete object found yet; keep buffering if likely in the middle of one
                        if pending:
                            continue

                        # Fallback for malformed/non-JSON lines that still contain braces
                        lower = combined.lower()
                        if (
                            '"token_count"' in lower
                            or '"exec_command_output_delta"' in lower
                            or '"turn_diff"' in lower
                        ):
                            continue
                        print(combined, end="" if combined.endswith("\n") else "\n", flush=True)
                except ValueError:
                    # Watchdog closed stdout because the process exited but the
                    # pipe stayed open (inherited FDs from child processes).
                    # This is expected — all output has been consumed.
                    pass

            # Signal watchdog that output is done (stdout loop has exited).
            output_done.set()

            # Flush any pending buffered content after the stream ends
            if pending.strip():
                try:
                    tail_obj = json.loads(pending)
                    if isinstance(tail_obj, dict):
                        handle_obj(tail_obj)
                    else:
                        print(pending, flush=True)
                except Exception:
                    low_tail = pending.lower()
                    if (
                        '"token_count"' not in low_tail
                        and '"exec_command_output_delta"' not in low_tail
                        and '"turn_diff"' not in low_tail
                    ):
                        print(pending, flush=True)

            # Ensure process has exited. The watchdog thread handles termination
            # if the process hangs, so by this point it should already be dead.
            # Use a short timeout as a safety net.
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass  # Watchdog thread will handle cleanup

            # Do not emit token_count summary; fully suppressed per user feedback

            # Print stderr if there were errors
            if process.stderr and process.returncode != 0:
                stderr_output = process.stderr.read()
                if stderr_output:
                    print(stderr_output, file=sys.stderr)

            write_capture_file()
            return process.returncode

        except KeyboardInterrupt:
            print("\nInterrupted by user", file=sys.stderr)
            write_capture_file()
            try:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            except Exception:
                pass
            return 130
        except Exception as e:
            print(f"Error executing codex: {e}", file=sys.stderr)
            write_capture_file()
            return 1

    def run(self) -> int:
        """Main execution flow"""
        # Parse arguments first to handle --help
        args = self.parse_arguments()

        # Check if prompt is provided
        if not args.prompt and not args.prompt_file:
            print(
                "Error: Either -p/--prompt or -pp/--prompt-file is required.",
                file=sys.stderr
            )
            print("\nRun 'codex.py --help' for usage information.", file=sys.stderr)
            return 1

        # Check if codex is installed
        if not self.check_codex_installed():
            print(
                "Error: OpenAI Codex is not available. Please install it.",
                file=sys.stderr
            )
            print(
                "Visit: https://openai.com/blog/openai-codex for installation instructions",
                file=sys.stderr
            )
            return 1

        # Set configuration from arguments
        self.project_path = os.path.abspath(args.cd)
        # Expand model shorthand
        try:
            self.model_name = self.expand_model_shorthand(args.model)
        except ModelShortcutError as error:
            print(f"Error: {error}", file=sys.stderr)
            return 1
        finally:
            sanitize_model_shortcut_environment()
        self.auto_instruction = args.auto_instruction

        # Get prompt from file or argument
        if args.prompt_file:
            self.prompt = self.read_prompt_file(args.prompt_file)
        else:
            self.prompt = args.prompt

        # Validate project path
        if not os.path.isdir(self.project_path):
            print(
                f"Error: Project path does not exist: {self.project_path}",
                file=sys.stderr
            )
            return 1

        # Build and execute command
        cmd = self.build_codex_command(args)
        self.verbose = args.verbose
        return self.run_codex(cmd, verbose=args.verbose)


def main():
    """Entry point"""
    service = CodexService()
    sys.exit(service.run())


if __name__ == "__main__":
    main()
