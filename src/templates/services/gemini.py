#!/usr/bin/env python3
"""
Gemini Service Script for yylo
Headless wrapper around the Gemini CLI with JSON streaming and shorthand model support.
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from typing import List, Optional, Tuple

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


class GeminiService:
    """Service wrapper for Gemini CLI headless mode."""

    DEFAULT_MODEL = "gemini-2.5-pro"
    DEFAULT_OUTPUT_FORMAT = "stream-json"
    VALID_OUTPUT_FORMATS = ["stream-json", "json", "text"]

    # Common shorthand mappings (extendable as models evolve)
    MODEL_SHORTHANDS = {
        ":pro": "gemini-2.5-pro",
        ":flash": "gemini-2.5-flash",
        ":pro-2.5": "gemini-2.5-pro",
        ":flash-2.5": "gemini-2.5-flash",
        ":pro-3": "gemini-3.0-pro",
        ":flash-3": "gemini-3.0-flash",
    }

    def __init__(self):
        self.model_name = self.DEFAULT_MODEL
        self.output_format = self.DEFAULT_OUTPUT_FORMAT
        self.project_path = os.getcwd()
        self.prompt = ""
        self.include_dirs: List[str] = []
        self.approval_mode: Optional[str] = None
        self.yolo: bool = False
        self.debug = False
        self.verbose = False
        self._stdin_prompt: Optional[str] = None

    def expand_model_shorthand(self, model: str) -> str:
        """Resolve shipped and project model shortcuts for Gemini."""
        return resolve_model_shortcut(model, self.MODEL_SHORTHANDS, "gemini")

    def _prompt_arg_max_bytes(self) -> int:
        return _positive_int_env(PROMPT_ARG_MAX_BYTES_ENV, DEFAULT_PROMPT_ARG_MAX_BYTES)

    def _is_prompt_oversized(self, prompt: str) -> bool:
        return len(prompt.encode("utf-8")) > self._prompt_arg_max_bytes()

    def check_gemini_installed(self) -> bool:
        """Check if gemini CLI is installed and available."""
        try:
            result = subprocess.run(
                ["which", "gemini"],
                capture_output=True,
                text=True,
                check=False,
            )
            return result.returncode == 0
        except Exception:
            return False

    def ensure_api_key_present(self) -> bool:
        """Validate that GEMINI_API_KEY is set for headless execution."""
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if isinstance(api_key, str) and api_key.strip():
            return True

        print(
            "Error: GEMINI_API_KEY is not set. Export GEMINI_API_KEY before running gemini headless CLI.",
            file=sys.stderr,
        )
        print("Example: export GEMINI_API_KEY=\"your-api-key\"", file=sys.stderr)
        return False

    def parse_arguments(self) -> argparse.Namespace:
        """Parse command line arguments for the Gemini service."""
        parser = argparse.ArgumentParser(
            description="Gemini Service - Wrapper for Gemini CLI headless mode",
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog="""
Examples:
  %(prog)s -p "Quick summary of README" --output-format stream-json
  %(prog)s -pp prompt.txt --model :pro-3 --yolo
  %(prog)s -p "Refactor module" --include-directories src,docs
  %(prog)s -p "Audit code" --approval-mode auto_edit --debug
            """,
        )

        prompt_group = parser.add_mutually_exclusive_group(required=False)
        prompt_group.add_argument("-p", "--prompt", type=str, help="Prompt text to send to Gemini")
        prompt_group.add_argument("-pp", "--prompt-file", type=str, help="Path to file containing the prompt")

        parser.add_argument(
            "--cd",
            type=str,
            default=os.environ.get("GEMINI_PROJECT_PATH", os.getcwd()),
            help="Project path (absolute). Default: current directory (env: GEMINI_PROJECT_PATH)",
        )

        parser.add_argument(
            "-m",
            "--model",
            type=str,
            default=os.environ.get("GEMINI_MODEL", self.DEFAULT_MODEL),
            help=(
                "Gemini model. Supports shorthands (:pro, :flash, :pro-3, :flash-3, :pro-2.5, :flash-2.5) "
                f"or full IDs. Default: {self.DEFAULT_MODEL} (env: GEMINI_MODEL)"
            ),
        )

        parser.add_argument(
            "--output-format",
            type=str,
            choices=self.VALID_OUTPUT_FORMATS,
            default=os.environ.get("GEMINI_OUTPUT_FORMAT", self.DEFAULT_OUTPUT_FORMAT),
            help="Gemini output format (stream-json/json/text). Default: stream-json (env: GEMINI_OUTPUT_FORMAT)",
        )

        parser.add_argument(
            "--include-directories",
            type=str,
            help="Comma-separated list of directories to include for Gemini context (forwarded to CLI).",
        )

        parser.add_argument(
            "--approval-mode",
            type=str,
            help="Set approval mode (e.g., auto_edit). Defaults to --yolo for headless mode when not provided.",
        )

        parser.add_argument(
            "--yolo",
            action="store_true",
            help="Auto-approve all actions (non-interactive). Enabled by default when no approval mode is supplied.",
        )

        parser.add_argument(
            "--debug",
            action="store_true",
            help="Enable Gemini CLI debug output.",
        )

        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Print the constructed command before execution.",
        )

        return parser.parse_args()

    def _first_nonempty_str(self, *values: Optional[str]) -> str:
        """Return the first non-empty string from provided values."""
        for val in values:
            if isinstance(val, str) and val.strip() != "":
                return val
        return ""

    def _extract_content_text(self, payload: dict) -> str:
        """Extract human-readable content from various Gemini event payload shapes."""
        if not isinstance(payload, dict):
            return ""

        content_val = payload.get("content")
        if isinstance(content_val, list):
            parts: List[str] = []
            for entry in content_val:
                if isinstance(entry, dict):
                    text_val = entry.get("text") or entry.get("response") or entry.get("output")
                    if isinstance(text_val, str) and text_val.strip():
                        parts.append(text_val)
                elif isinstance(entry, str) and entry.strip():
                    parts.append(entry)
            if parts:
                return "\n".join(parts)
        elif isinstance(content_val, str):
            return content_val

        # Fall back to common fields
        return self._first_nonempty_str(
            payload.get("response"),
            payload.get("message"),
            payload.get("output"),
            payload.get("result") if isinstance(payload.get("result"), str) else "",
            payload.get("text"),
        )

    def _format_event_pretty(self, payload: dict) -> str:
        """
        Normalize Gemini CLI JSON output into a compact JSON header plus optional multi-line block.
        Ensures a `type` and `content` field exist so shell-backend can stream progress events.
        """
        try:
            raw_type = payload.get("type") or payload.get("event") or "message"
            msg_type = str(raw_type).strip() or "message"
            now = datetime.now().strftime("%I:%M:%S %p")

            content = self._extract_content_text(payload)
            # If still empty, serialize result/output objects as JSON to keep content non-undefined
            header = {
                "type": msg_type,
                "datetime": now,
            }

            def copy_if_present(key: str, dest: Optional[str] = None):
                val = payload.get(key)
                if val not in (None, ""):
                    header[dest or key] = val

            copy_if_present("role")
            copy_if_present("status")
            copy_if_present("tool_name", "tool")
            copy_if_present("tool_id", "tool_id")
            copy_if_present("timestamp")
            copy_if_present("session_id")
            copy_if_present("model")
            if payload.get("delta"):
                header["delta"] = True

            if msg_type == "tool_use" and not content:
                tool_params = payload.get("parameters") or payload.get("tool_use") or payload.get("input")
                if isinstance(tool_params, (dict, list)):
                    header["parameters"] = tool_params
                    content = json.dumps(tool_params, ensure_ascii=False)
                elif tool_params:
                    content = str(tool_params)

            if msg_type == "tool_result" and not content:
                tool_output = self._first_nonempty_str(payload.get("output"), payload.get("result"))
                if tool_output:
                    content = tool_output

            if msg_type == "init" and not content:
                init_summary = {k: payload.get(k) for k in ["session_id", "model"] if payload.get(k)}
                if init_summary:
                    content = json.dumps(init_summary, ensure_ascii=False)

            if msg_type == "result" and not content:
                if isinstance(payload.get("stats"), (dict, list)):
                    content = json.dumps(payload.get("stats"), ensure_ascii=False)

            if content and "\n" in content:
                return json.dumps(header, ensure_ascii=False) + "\ncontent:\n" + content

            if content != "":
                header["content"] = content if content is not None else ""

            return json.dumps(header, ensure_ascii=False)
        except Exception:
            return json.dumps(payload, ensure_ascii=False)

    def _split_json_stream(self, text: str) -> Tuple[List[str], str]:
        """
        Split a stream of concatenated JSON objects based on top-level brace balance.
        Returns (complete_objects, remainder).
        """
        objs: List[str] = []
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
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue

            if ch == '"':
                in_str = True
                buf.append(ch)
                continue

            if ch == "{":
                depth += 1
                started = True
                buf.append(ch)
                continue

            if ch == "}":
                depth -= 1
                buf.append(ch)
                if started and depth == 0:
                    candidate = "".join(buf).strip().strip("'\"")
                    if candidate:
                        objs.append(candidate)
                    buf = []
                    started = False
                continue

            if started:
                buf.append(ch)
            else:
                # Treat delimiter separators (e.g., ASCII 0x7f) as whitespace
                if ch == "\x7f":
                    continue
                buf.append(ch)

        remainder = "".join(buf) if buf else ""
        return objs, remainder

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

    def build_gemini_command(self, args: argparse.Namespace) -> List[str]:
        """Construct the Gemini CLI command for headless execution."""
        cmd = ["gemini"]

        # Gemini headless mode accepts prompt text from stdin. Keep small prompts
        # on --prompt for CLI compatibility, but pipe oversized prompts to avoid
        # subprocess argv E2BIG after shell-backend selected prompt-file transport.
        self._stdin_prompt = None
        if self.prompt:
            if self._is_prompt_oversized(self.prompt):
                self._stdin_prompt = self.prompt
            else:
                cmd.extend(["--prompt", self.prompt])

        cmd.extend(["--output-format", self.output_format])
        cmd.extend(["--model", self.model_name])

        include_dirs = []
        if args.include_directories:
            include_dirs = [part.strip() for part in args.include_directories.split(",") if part.strip()]
        self.include_dirs = include_dirs
        if include_dirs:
            cmd.extend(["--include-directories", ",".join(include_dirs)])

        if args.approval_mode:
            cmd.extend(["--approval-mode", args.approval_mode])
        else:
            # Default to yolo for headless automation when approval mode is not provided
            cmd.append("--yolo")

        if args.yolo:
            if "--yolo" not in cmd:
                cmd.append("--yolo")

        if args.debug:
            cmd.append("--debug")

        return cmd

    def run_gemini(self, cmd: List[str], verbose: bool = False) -> int:
        """Execute the Gemini CLI and normalize streaming output for shell-backend consumption."""
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

        try:
            process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE if self._stdin_prompt else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                universal_newlines=True,
                cwd=self.project_path,
            )

            record_harness_handoff("gemini")

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
            # (set when the stdout loop finishes). Once signaled, it gives the
            # process a grace period to exit, then terminates it and closes stdout.
            wait_timeout = int(os.environ.get("GEMINI_WAIT_TIMEOUT", "30"))
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
                            f"Warning: Gemini process did not exit within {wait_timeout}s after output. Terminating.",
                            file=sys.stderr
                        )
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            print("Warning: Gemini process did not respond to SIGTERM. Killing.", file=sys.stderr)
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

            pending = ""

            if process.stdout:
                try:
                    for raw_line in process.stdout:
                        combined = (pending + raw_line).replace("\x7f", "\n")
                        pending = ""

                        if not combined.strip():
                            continue

                        parts, pending = self._split_json_stream(combined)

                        if parts:
                            for part in parts:
                                try:
                                    parsed = json.loads(part)
                                    formatted = self._format_event_pretty(parsed)
                                    print(formatted, flush=True)
                                except Exception:
                                    print(part, flush=True)
                            continue

                        # Fallback for non-JSON lines or partial content
                        if pending:
                            continue

                        print(combined, end="" if combined.endswith("\n") else "\n", flush=True)
                except ValueError:
                    # Watchdog closed stdout because the process exited but the
                    # pipe stayed open (inherited FDs from child processes).
                    # This is expected — all output has been consumed.
                    pass

            # Signal watchdog that output is done (stdout loop has exited).
            output_done.set()

            if pending.strip():
                try:
                    parsed_tail = json.loads(pending)
                    print(self._format_event_pretty(parsed_tail), flush=True)
                except Exception:
                    print(pending, flush=True)

            # Ensure process has exited. The watchdog thread handles termination
            # if the process hangs, so by this point it should already be dead.
            # Use a short timeout as a safety net.
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass  # Watchdog thread will handle cleanup

            if process.stderr and process.returncode != 0:
                stderr_output = process.stderr.read()
                if stderr_output:
                    print(stderr_output, file=sys.stderr)

            return process.returncode

        except KeyboardInterrupt:
            print("\nInterrupted by user", file=sys.stderr)
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
            print(f"Error executing gemini: {e}", file=sys.stderr)
            return 1

    def run(self) -> int:
        """Main execution flow."""
        args = self.parse_arguments()

        # Prompt handling: allow env override for shell backend compatibility
        prompt_value = args.prompt or os.environ.get("JUNO_INSTRUCTION")
        if not prompt_value and not args.prompt_file:
            print("Error: Either -p/--prompt or -pp/--prompt-file is required.", file=sys.stderr)
            print("\nRun 'gemini.py --help' for usage information.", file=sys.stderr)
            return 1

        if not self.check_gemini_installed():
            print("Error: Gemini CLI is not available. Please install it: https://geminicli.com/docs/get-started/installation/", file=sys.stderr)
            return 1

        if not self.ensure_api_key_present():
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
        self.output_format = args.output_format or self.DEFAULT_OUTPUT_FORMAT
        if self.output_format not in self.VALID_OUTPUT_FORMATS:
            print(f"Warning: Unsupported output format '{self.output_format}'. Falling back to {self.DEFAULT_OUTPUT_FORMAT}.", file=sys.stderr)
            self.output_format = self.DEFAULT_OUTPUT_FORMAT
        self.debug = args.debug
        self.verbose = args.verbose
        self.approval_mode = args.approval_mode
        self.yolo = args.yolo

        if args.prompt_file:
            self.prompt = self.read_prompt_file(args.prompt_file)
        else:
            self.prompt = prompt_value

        cmd = self.build_gemini_command(args)
        return self.run_gemini(cmd, verbose=args.verbose)


def main():
    service = GeminiService()
    sys.exit(service.run())


if __name__ == "__main__":
    main()
