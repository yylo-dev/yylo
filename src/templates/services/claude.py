#!/usr/bin/env python3
"""
Claude Service Script for yylo
This script provides a wrapper around Anthropic Claude CLI with configurable options.
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

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


class ClaudeService:
    """Service wrapper for Anthropic Claude CLI"""

    # Default configuration
    DEFAULT_MODEL = "claude-sonnet-4-6"
    DEFAULT_PERMISSION_MODE = "default"
    DEFAULT_AUTO_INSTRUCTION = """"""

    # Model shorthand mappings (colon-prefixed names expand to full model IDs)
    MODEL_SHORTHANDS = {
        ":claude-haiku-4-5": "claude-haiku-4-5-20251001",
        ":claude-sonnet-4-5": "claude-sonnet-4-5-20250929",
        ":claude-sonnet-4-6": "claude-sonnet-4-6",
        ":claude-opus-4-5": "claude-opus-4-5-20251101",
        ":claude-opus-4-6": "claude-opus-4-6",
        ":claude-opus-4": "claude-opus-4-20250514",
        ":haiku": "claude-haiku-4-5-20251001",
        ":sonnet": "claude-sonnet-4-6",
        ":opus": "claude-opus-4-6",
    }

    # Default allowed tools (used with --append-allowed-tools)
    DEFAULT_ALLOWED_TOOLS = [
        "Task", "Bash", "Glob", "Grep", "ExitPlanMode", "Read", "Edit", "Write",
        "NotebookEdit", "WebFetch", "TodoWrite", "WebSearch", "BashOutput",
        "KillShell", "Skill", "SlashCommand", "EnterPlanMode"
    ]

    def __init__(self):
        self.model_name = self.DEFAULT_MODEL
        self.permission_mode = self.DEFAULT_PERMISSION_MODE
        self.auto_instruction = self.DEFAULT_AUTO_INSTRUCTION
        self.project_path = os.getcwd()
        self.prompt = ""
        self.additional_args: List[str] = []
        self.message_counter = 0
        self.verbose = False
        self._stdin_prompt: Optional[str] = None
        # User message truncation: -1 = no truncation, N = truncate to N lines
        self.user_message_truncate = int(os.environ.get("CLAUDE_USER_MESSAGE_PRETTY_TRUNCATE", "4"))
        self.last_result_event: Optional[Dict[str, Any]] = None

    def expand_model_shorthand(self, model: str) -> str:
        """Resolve shipped and project model shortcuts for Claude or Cursor."""
        subagent = os.environ.get("JUNO_SELECTED_SUBAGENT", "claude")
        if subagent not in {"claude", "cursor"}:
            subagent = "claude"
        return resolve_model_shortcut(model, self.MODEL_SHORTHANDS, subagent)

    def _prompt_arg_max_bytes(self) -> int:
        return _positive_int_env(PROMPT_ARG_MAX_BYTES_ENV, DEFAULT_PROMPT_ARG_MAX_BYTES)

    def _is_prompt_oversized(self, prompt: str) -> bool:
        return len(prompt.encode("utf-8")) > self._prompt_arg_max_bytes()

    def check_claude_installed(self) -> bool:
        """Check if claude CLI is installed and available"""
        try:
            result = subprocess.run(
                ["which", "claude"],
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
            description="Claude Service - Wrapper for Anthropic Claude CLI",
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog="""
Examples:
  %(prog)s -p "Write a hello world function"
  %(prog)s -pp prompt.txt --cd /path/to/project
  %(prog)s -p "Add tests" -m :opus --tool Bash --tool Edit
  %(prog)s -p "Quick task" -m :haiku --disallowed-tool Bash
  %(prog)s -p "Complex task" -m claude-opus-4-20250514 --tool Read --tool Write
  %(prog)s -p "Multi-tool task" --allowed-tools Bash Edit Read Write
  %(prog)s -p "Restricted task" --disallowed-tools Bash WebSearch
  %(prog)s --continue -p "Continue previous conversation"
  %(prog)s --resume abc123 -p "Resume session abc123"

Default Tools (enabled by default when no --allowed-tools specified):
  Task, Bash, Glob, Grep, ExitPlanMode, Read, Edit, Write, NotebookEdit,
  WebFetch, TodoWrite, WebSearch, BashOutput, KillShell, Skill, SlashCommand, EnterPlanMode

Environment Variables:
  CLAUDE_PROJECT_PATH                  Project path (default: current directory)
  CLAUDE_MODEL                         Model name (default: claude-sonnet-4-6)
  CLAUDE_AUTO_INSTRUCTION              Auto instruction to prepend to prompt
  CLAUDE_PERMISSION_MODE               Permission mode (default: default)
  CLAUDE_PRETTY                        Pretty print JSON output (default: true)
  CLAUDE_VERBOSE                       Enable verbose output (default: false)
  CLAUDE_USER_MESSAGE_PRETTY_TRUNCATE  Max lines for user messages in pretty mode (default: 4, -1: no truncation)
            """
        )

        # Core arguments
        prompt_group = parser.add_mutually_exclusive_group(required=False)
        prompt_group.add_argument(
            "-p", "--prompt",
            type=str,
            help="Prompt text to send to claude"
        )
        prompt_group.add_argument(
            "-pp", "--prompt-file",
            type=str,
            help="Path to file containing the prompt"
        )

        parser.add_argument(
            "--cd",
            type=str,
            default=os.environ.get("CLAUDE_PROJECT_PATH", os.getcwd()),
            help="Project path (absolute path). Default: current directory (env: CLAUDE_PROJECT_PATH)"
        )

        parser.add_argument(
            "-m", "--model",
            type=str,
            default=os.environ.get("CLAUDE_MODEL", self.DEFAULT_MODEL),
            help=f"Model name. Supports shorthand (e.g., ':haiku', ':sonnet', ':opus', ':claude-sonnet-4-6') or full model ID (e.g., 'claude-sonnet-4-6'). Default: {self.DEFAULT_MODEL} (env: CLAUDE_MODEL)"
        )

        parser.add_argument(
            "--auto-instruction",
            type=str,
            default=os.environ.get("CLAUDE_AUTO_INSTRUCTION", self.DEFAULT_AUTO_INSTRUCTION),
            help="Auto instruction to prepend to prompt (env: CLAUDE_AUTO_INSTRUCTION)"
        )

        parser.add_argument(
            "--tools",
            action="append",
            dest="tools",
            help="Specify the list of available tools from the built-in set (only works with --print mode). Use \"\" to disable all tools, \"default\" to use all tools, or specify tool names (e.g. \"Bash\" \"Edit\" \"Read\"). Forwarded to claude CLI."
        )

        parser.add_argument(
            "--allowedTools", "--allowed-tools",
            action="append",
            dest="allowed_tools",
            help="Permission-based filtering of specific tool instances (e.g. 'Bash(git:*)' 'Edit'). Accepts both --allowedTools and --allowed-tools. Default when not specified: Task, Bash, Glob, Grep, ExitPlanMode, Read, Edit, Write, NotebookEdit, WebFetch, TodoWrite, WebSearch, BashOutput, KillShell, Skill, SlashCommand, EnterPlanMode"
        )

        parser.add_argument(
            "--disallowedTools", "--disallowed-tools",
            action="append",
            dest="disallowed_tools",
            help="Disallowed tools (can be used multiple times, e.g. 'Bash' 'Edit'). Accepts both --disallowedTools and --disallowed-tools. By default, no tools are disallowed"
        )

        parser.add_argument(
            "--appendAllowedTools", "--append-allowed-tools",
            action="append",
            dest="append_allowed_tools",
            help="Append tools to the default allowed-tools list (mutually exclusive with --allowed-tools). Accepts both --appendAllowedTools and --append-allowed-tools."
        )

        parser.add_argument(
            "--permission-mode",
            type=str,
            choices=["acceptEdits", "bypassPermissions", "default", "plan", "skip"],
            default=os.environ.get("CLAUDE_PERMISSION_MODE", self.DEFAULT_PERMISSION_MODE),
            help=f"Permission mode for the session. Default: {self.DEFAULT_PERMISSION_MODE} (env: CLAUDE_PERMISSION_MODE)"
        )

        parser.add_argument(
            "--json",
            action="store_true",
            default=True,
            help="Output in JSON format (default: True)"
        )

        parser.add_argument(
            "--pretty",
            type=str,
            default=os.environ.get("CLAUDE_PRETTY", "true").lower(),
            choices=["true", "false"],
            help="Pretty print JSON output (default: true, env: CLAUDE_PRETTY)"
        )

        parser.add_argument(
            "--verbose",
            action="store_true",
            default=os.environ.get("CLAUDE_VERBOSE", "false").lower() == "true",
            help="Enable verbose output (env: CLAUDE_VERBOSE)"
        )

        parser.add_argument(
            "-c", "--continue",
            action="store_true",
            dest="continue_conversation",
            help="Continue the most recent conversation"
        )

        parser.add_argument(
            "-r", "--resume",
            type=str,
            dest="resume_session",
            metavar="SESSION_ID",
            help="Resume a conversation by session ID (e.g., claude --resume abc123)"
        )

        parser.add_argument(
            "--agents",
            type=str,
            help="Agents configuration (forwarded to Claude CLI --agents flag)"
        )

        parser.add_argument(
            "--additional-args",
            type=str,
            help="Additional claude arguments as a space-separated string"
        )

        return parser.parse_args()

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

    def build_claude_command(self, args: argparse.Namespace) -> List[str]:
        """Build the claude command with all arguments"""
        # Start with base command
        cmd = [
            "claude",
            "--print",  # Non-interactive mode 
            "--model", self.model_name,
            "--permission-mode", args.permission_mode,
        ]

        # Build the full prompt (auto_instruction + user prompt). Small prompts stay
        # positional for CLI compatibility; oversized prompts use stdin so yylo
        # does not reintroduce OS E2BIG failures after shell-backend selected file
        # transport into this wrapper.
        full_prompt = f"{self.auto_instruction}\n\n{self.prompt}"
        self._stdin_prompt = None
        if self._is_prompt_oversized(full_prompt):
            self._stdin_prompt = full_prompt
        else:
            # IMPORTANT: Prompt must come BEFORE tool-related flags because some flags
            # consume all following arguments.
            cmd.append(full_prompt)

        # Add available tools from built-in set if specified (AFTER the prompt)
        # Note: --tools controls which built-in Claude tools are available (only works with --print mode)
        if args.tools:
            cmd.append("--tools")
            cmd.extend(args.tools)
        # No else block: By default Claude enables all tools

        # Handle allowed tools (either --allowed-tools or --append-allowed-tools, but not both)
        # These are mutually exclusive - validation already done above
        # When neither is specified, use the default allowed-tools list
        if args.allowed_tools:
            # Use the explicitly specified allowed tools (replaces default)
            cmd.append("--allowedTools")
            cmd.extend(args.allowed_tools)
        elif args.append_allowed_tools:
            # Append specified tools to the default list
            combined_tools = self.DEFAULT_ALLOWED_TOOLS + args.append_allowed_tools
            cmd.append("--allowedTools")
            cmd.extend(combined_tools)
        else:
            # Use default allowed-tools list when no explicit list is provided
            cmd.append("--allowedTools")
            cmd.extend(self.DEFAULT_ALLOWED_TOOLS)

        # Add disallowed tools if specified (AFTER the prompt)
        # Note: claude CLI expects camelCase --disallowedTools (not kebab-case --disallowed-tools)
        if args.disallowed_tools:
            cmd.append("--disallowedTools")
            cmd.extend(args.disallowed_tools)

        # Add continue flag if specified
        if args.continue_conversation:
            cmd.append("--continue")

        # Add resume flag if specified
        if args.resume_session:
            cmd.extend(["--resume", args.resume_session])

        # Add agents configuration if specified
        if args.agents:
            cmd.extend(["--agents", args.agents])

        # Add output format if JSON requested
        # Note: stream-json requires --verbose when using --print mode
        if args.json:
            cmd.extend(["--output-format", "stream-json", "--verbose"])

        # Add any additional arguments
        if args.additional_args:
            additional = args.additional_args.split()
            cmd.extend(additional)

        return cmd

    def pretty_format_json(self, json_line: str) -> Optional[str]:
        """
        Format JSON line for pretty output.
        For type=assistant: show datetime, message content, and counter
        For type=user: show datetime, message content (truncated based on env var), and counter
        For other types: show full message with datetime and counter
        Returns None if line should be skipped

        IMPORTANT: Always preserve the 'type' field so shell backend can parse events

        MULTI-LINE HANDLING: When content/result fields contain \\n escape sequences,
        the output shows the JSON metadata on one line, then the actual content/result
        value is printed below with newlines properly rendered (similar to jq -r or @text).
        This keeps JSON structure compact while making multi-line strings readable.

        USER MESSAGE TRUNCATION: User messages are truncated based on CLAUDE_USER_MESSAGE_PRETTY_TRUNCATE
        environment variable (default: 4 lines, -1: no truncation). When truncated, a [Truncated...]
        indicator is added. This only applies to user messages in pretty mode.
        """
        try:
            data = json.loads(json_line)
            self.message_counter += 1

            # Get current datetime in readable format
            now = datetime.now().strftime("%I:%M:%S %p")

            # For user messages, show simplified output with truncation
            if data.get("type") == "user":
                message = data.get("message", {})
                content_list = message.get("content", [])

                # Extract text content
                text_content = ""
                for item in content_list:
                    if isinstance(item, dict) and item.get("type") == "text":
                        text_content = item.get("text", "")
                        break

                # Apply truncation for user messages based on CLAUDE_USER_MESSAGE_PRETTY_TRUNCATE
                # -1 means no truncation, otherwise truncate to N lines
                if self.user_message_truncate != -1:
                    lines = text_content.split('\n')
                    if len(lines) > self.user_message_truncate:
                        # Truncate to N lines and add indicator
                        text_content = '\n'.join(lines[:self.user_message_truncate]) + '\n[Truncated...]'

                # Create simplified output with datetime, content, and counter
                simplified = {
                    "type": "user",
                    "datetime": now,
                    "counter": f"#{self.message_counter}"
                }

                # Check if content has newlines after potential truncation
                if '\n' in text_content:
                    # Multi-line content: print JSON metadata, then raw content
                    metadata = {
                        "type": "user",
                        "datetime": now,
                        "counter": f"#{self.message_counter}"
                    }
                    # Print metadata as compact JSON on first line
                    output = json.dumps(metadata, ensure_ascii=False)
                    # Then print content label and raw multi-line text
                    output += "\ncontent:\n" + text_content
                    return output
                else:
                    # Single-line content: normal JSON
                    simplified["content"] = text_content
                    return json.dumps(simplified, ensure_ascii=False)

            # For progress events, handle bash_progress and skip hook_progress
            elif data.get("type") == "progress":
                progress_data = data.get("data", {})
                progress_type = progress_data.get("type", "")

                # Skip hook_progress events (not interested)
                if progress_type == "hook_progress":
                    return None

                # Display bash_progress events with [Progress] tag
                if progress_type == "bash_progress":
                    # Extract relevant fields from bash_progress
                    output_text = progress_data.get("output", "")
                    elapsed_time = progress_data.get("elapsedTimeSeconds", 0)
                    total_lines = progress_data.get("totalLines", 0)

                    # Create simplified output with datetime and counter
                    simplified = {
                        "type": "progress",
                        "progress_type": "bash_progress",
                        "datetime": now,
                        "counter": f"#{self.message_counter}",
                        "elapsed": f"{elapsed_time}s",
                        "lines": total_lines
                    }

                    # Check if output has newlines
                    if '\n' in output_text:
                        # Multi-line output: print metadata, then raw output
                        metadata_json = json.dumps(simplified, ensure_ascii=False)
                        return metadata_json + "\n[Progress] output:\n" + output_text
                    else:
                        # Single-line output: normal JSON with [Progress] tag
                        simplified["output"] = output_text
                        # Add [Progress] tag to the output
                        output_json = json.dumps(simplified, ensure_ascii=False)
                        return f"[Progress] {output_json}"

                # For other progress types, display with datetime and counter
                simplified = {
                    "type": "progress",
                    "progress_type": progress_type,
                    "datetime": now,
                    "counter": f"#{self.message_counter}",
                    "data": progress_data
                }
                return json.dumps(simplified, ensure_ascii=False)

            # For assistant messages, show simplified output
            elif data.get("type") == "assistant":
                message = data.get("message", {})
                content_list = message.get("content", [])

                # Extract text content or tool_use from content array
                text_content = ""
                tool_use_data = None

                for item in content_list:
                    if isinstance(item, dict):
                        if item.get("type") == "text":
                            text_content = item.get("text", "")
                            break
                        elif item.get("type") == "tool_use":
                            # Extract tool name and input for tool_use
                            tool_use_data = {
                                "name": item.get("name", ""),
                                "input": item.get("input", {})
                            }
                            break

                # Create simplified output with datetime, content/tool_use, and counter
                # KEEP the 'type' field for shell backend compatibility
                simplified = {
                    "type": "assistant",
                    "datetime": now,
                    "counter": f"#{self.message_counter}"
                }

                # Add either content or tool_use data
                if tool_use_data:
                    # Check if prompt field in tool_use.input has multi-line content
                    tool_input = tool_use_data.get("input", {})
                    prompt_field = tool_input.get("prompt", "")

                    if isinstance(prompt_field, str) and '\n' in prompt_field:
                        # Multi-line prompt: extract it and render separately
                        # Create a copy of tool_use_data with prompt removed
                        tool_use_copy = {
                            "name": tool_use_data.get("name", ""),
                            "input": {k: v for k, v in tool_input.items() if k != "prompt"}
                        }

                        simplified["tool_use"] = tool_use_copy

                        # Print metadata as compact JSON on first line
                        output = json.dumps(simplified, ensure_ascii=False)
                        # Then print prompt label and raw multi-line text
                        output += "\nprompt:\n" + prompt_field
                        return output
                    else:
                        # No multi-line prompt: normal JSON output for tool_use
                        simplified["tool_use"] = tool_use_data
                        return json.dumps(simplified, ensure_ascii=False)
                else:
                    # For content, check if it has newlines
                    if '\n' in text_content:
                        # Multi-line content: print JSON metadata, then raw content
                        metadata = {
                            "type": "assistant",
                            "datetime": now,
                            "counter": f"#{self.message_counter}"
                        }
                        # Print metadata as compact JSON on first line
                        output = json.dumps(metadata, ensure_ascii=False)
                        # Then print content label and raw multi-line text
                        output += "\ncontent:\n" + text_content
                        return output
                    else:
                        # Single-line content: normal JSON
                        simplified["content"] = text_content
                        return json.dumps(simplified, ensure_ascii=False)
            else:
                # For other message types, check if there's nested content to flatten
                message = data.get("message", {})
                content_list = message.get("content", [])

                # Check if this is a message with nested tool_result or similar content
                if content_list and isinstance(content_list, list) and len(content_list) > 0:
                    nested_item = content_list[0]
                    if isinstance(nested_item, dict) and nested_item.get("type") in ["tool_result"]:
                        # Flatten the nested structure by pulling nested fields to top level
                        flattened = {
                            "datetime": now,
                            "counter": f"#{self.message_counter}",
                        }

                        # Add tool_use_id if present
                        if "tool_use_id" in nested_item:
                            flattened["tool_use_id"] = nested_item["tool_use_id"]

                        # Add type from nested item
                        flattened["type"] = nested_item["type"]

                        # Handle content field with multiline support
                        nested_content = nested_item.get("content", "")
                        if isinstance(nested_content, str) and '\n' in nested_content:
                            # Multi-line content: separate metadata from content
                            # Print metadata as compact JSON
                            metadata_json = json.dumps(flattened, ensure_ascii=False)
                            # Then print content label and raw multi-line text
                            return metadata_json + "\ncontent:\n" + nested_content
                        else:
                            # Single-line content: normal JSON
                            flattened["content"] = nested_content
                            return json.dumps(flattened, ensure_ascii=False)

                # For other message types, show full message with datetime and counter
                # Type field is already present in data, so it's preserved
                output = {
                    "datetime": now,
                    "counter": f"#{self.message_counter}",
                    **data
                }

                # Check if 'result' field has multi-line content
                if "result" in output and isinstance(output["result"], str) and '\n' in output["result"]:
                    # Multi-line result: separate metadata from content
                    result_value = output.pop("result")
                    # Print metadata as compact JSON
                    metadata_json = json.dumps(output, ensure_ascii=False)
                    # Then print result label and raw multi-line text
                    return metadata_json + "\nresult:\n" + result_value
                else:
                    # Normal JSON output
                    return json.dumps(output, ensure_ascii=False)

        except json.JSONDecodeError:
            # If not valid JSON, return as-is
            return json_line
        except Exception as e:
            # On any error, return original line
            print(f"Warning: Error formatting JSON: {e}", file=sys.stderr)
            return json_line

    def run_claude(self, cmd: List[str], verbose: bool = False, pretty: bool = True) -> int:
        """Execute the claude command and stream output"""
        capture_path = os.environ.get("JUNO_SUBAGENT_CAPTURE_PATH")

        if verbose and not capture_path:
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
            print(f"Executing: {' '.join(display_cmd)}", file=sys.stderr)
            print("-" * 80, file=sys.stderr)

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

        try:
            # Change to project directory before running
            original_cwd = os.getcwd()
            os.chdir(self.project_path)

            # Run the command and stream output
            # Use line buffering (bufsize=1) to ensure each JSON line is output immediately
            process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE if self._stdin_prompt else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,  # Line buffering for immediate output
                universal_newlines=True
            )

            record_harness_handoff("claude")

            if self._stdin_prompt and process.stdin:
                try:
                    process.stdin.write(self._stdin_prompt)
                    process.stdin.close()
                except BrokenPipeError:
                    pass

            # Watchdog thread: handles two scenarios where the stdout loop blocks:
            # 1. Process exits but its stdout pipe stays open (inherited FDs)
            # 2. Process itself never exits (hung Node.js event loop)
            # The watchdog waits for the "result received" signal from the main
            # thread (set when a type=result event is parsed). Once signaled, it
            # gives the process a grace period to exit, then terminates it.
            wait_timeout = int(os.environ.get("CLAUDE_WAIT_TIMEOUT", "30"))
            result_received = threading.Event()

            def _stdout_watchdog():
                """Terminate process and close stdout pipe if it hangs after result."""
                # Wait until the main thread signals that the result event was received,
                # OR until the process exits on its own (poll every second).
                while not result_received.is_set():
                    if process.poll() is not None:
                        # Process exited on its own before result event
                        break
                    result_received.wait(timeout=1)

                if result_received.is_set():
                    # Result was received — give process grace period to exit cleanly.
                    try:
                        process.wait(timeout=wait_timeout)
                    except subprocess.TimeoutExpired:
                        print(
                            f"Warning: Claude process did not exit within {wait_timeout}s after result. Terminating.",
                            file=sys.stderr
                        )
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            print("Warning: Claude process did not respond to SIGTERM. Killing.", file=sys.stderr)
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

            # Stream stdout line by line (each line is a JSON object when using stream-json)
            # This allows users to pipe to jq and see output as it streams
            if process.stdout:
                try:
                    for line in process.stdout:
                        raw_line = line.strip()
                        # Capture the raw final result event for programmatic consumption
                        try:
                            parsed_raw = json.loads(raw_line)
                            if isinstance(parsed_raw, dict) and parsed_raw.get("type") == "result":
                                self.last_result_event = parsed_raw
                                # Signal the watchdog that the result has been received.
                                # The CLI is done; if it doesn't exit soon, watchdog
                                # will terminate it.
                                result_received.set()
                        except json.JSONDecodeError:
                            # Ignore non-JSON lines here; pretty formatter will handle them
                            pass

                        # Apply pretty formatting if enabled
                        if pretty:
                            formatted_line = self.pretty_format_json(raw_line)
                            if formatted_line:
                                print(formatted_line, flush=True)
                        else:
                            # Raw output without formatting
                            print(line, end='', flush=True)
                except ValueError:
                    # Watchdog closed stdout because the process exited but the
                    # pipe stayed open (inherited FDs from child processes).
                    # This is expected — all output has been consumed.
                    pass

            # Signal watchdog in case stdout closed without a result event
            # (e.g., process crashed or was killed externally).
            result_received.set()

            # Ensure process has exited. The watchdog thread handles termination
            # if the process hangs, so by this point it should already be dead.
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass  # Watchdog thread will handle cleanup

            # Print stderr if there were errors
            if process.stderr and process.returncode != 0:
                stderr_output = process.stderr.read()
                if stderr_output:
                    print(stderr_output, file=sys.stderr)

            # Persist the raw final result event for programmatic capture
            write_capture_file()

            # Restore original working directory
            os.chdir(original_cwd)

            return process.returncode

        except KeyboardInterrupt:
            print("\nInterrupted by user", file=sys.stderr)
            if process:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            write_capture_file()
            # Restore original working directory
            if 'original_cwd' in locals():
                os.chdir(original_cwd)
            return 130
        except Exception as e:
            print(f"Error executing claude: {e}", file=sys.stderr)
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
            write_capture_file()
            # Restore original working directory
            if 'original_cwd' in locals():
                os.chdir(original_cwd)
            return 1

    def run(self) -> int:
        """Main execution flow"""
        # Parse arguments first to handle --help
        args = self.parse_arguments()

        # Validate that --allowed-tools and --append-allowed-tools are mutually exclusive
        if args.allowed_tools and args.append_allowed_tools:
            print(
                "Error: --allowed-tools and --append-allowed-tools are mutually exclusive. Use one or the other.",
                file=sys.stderr
            )
            return 1

        # Check if prompt is provided
        if not args.prompt and not args.prompt_file:
            print(
                "Error: Either -p/--prompt or -pp/--prompt-file is required.",
                file=sys.stderr
            )
            print("\nRun 'claude.py --help' for usage information.", file=sys.stderr)
            print("\nAvailable Environment Variables:", file=sys.stderr)
            print("  CLAUDE_PROJECT_PATH                  Project path (default: current directory)", file=sys.stderr)
            print("  CLAUDE_MODEL                         Model name (default: claude-sonnet-4-6)", file=sys.stderr)
            print("  CLAUDE_AUTO_INSTRUCTION              Auto instruction to prepend to prompt", file=sys.stderr)
            print("  CLAUDE_PERMISSION_MODE               Permission mode (default: default)", file=sys.stderr)
            print("  CLAUDE_PRETTY                        Pretty print JSON output (default: true)", file=sys.stderr)
            print("  CLAUDE_VERBOSE                       Enable verbose output (default: false)", file=sys.stderr)
            print("  CLAUDE_USER_MESSAGE_PRETTY_TRUNCATE  Max lines for user messages in pretty mode (default: 4, -1: no truncation)", file=sys.stderr)
            return 1

        # Check if claude is installed
        if not self.check_claude_installed():
            print(
                "Error: Claude CLI is not available. Please install it.",
                file=sys.stderr
            )
            print(
                "Visit: https://docs.anthropic.com/en/docs/agents-and-tools/claude-code for installation instructions",
                file=sys.stderr
            )
            return 1

        # Set configuration from arguments
        self.project_path = os.path.abspath(args.cd)
        # Expand model shorthand (e.g., :haiku -> claude-haiku-4-5-20251001)
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
        cmd = self.build_claude_command(args)
        pretty = args.pretty == "true"
        self.verbose = args.verbose
        return self.run_claude(cmd, verbose=args.verbose, pretty=pretty)


def main():
    """Entry point"""
    service = ClaudeService()
    sys.exit(service.run())


if __name__ == "__main__":
    main()
