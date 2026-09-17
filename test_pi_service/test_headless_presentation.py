"""Presentation tests use deterministic text, never provider calls."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

SERVICES = Path(__file__).resolve().parents[1] / "src/templates/services"
sys.path.insert(0, str(SERVICES))
import headless_presentation as ui
from pi import PiService


@pytest.fixture(autouse=True)
def terminal(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("JUNO_PI_OUTPUT_TTY", "1")
    monkeypatch.setenv("TERM", "screen")  # Basic tmux-compatible palette, no truecolor.


@pytest.mark.parametrize("control", ["\x1b[2J", "\x1b[1;1H", "\x1b[?25l", "\x1b]0;title\x07",
    "\x1b]52;c;secret\x1b\\", "\x1bPstuff\x1b\\", "\x9dtitle\x9c", "\x00", "\r", "\x08",
    "\x1b[5m", "\x1b[8m", "\x1b[38;5;999m", "\x1b]title\x1b[31mhidden\x07"])
def test_only_safe_sgr_survives(control):
    assert ui.sanitize("a" + control + "b", True) == "ab"


@pytest.mark.parametrize("control", ["\x1b[31m", "\x1b[1;36m", "\x1b[38;5;120m", "\x1b[38;2;12;0;255m", "\x1b[m"])
def test_shell_sgr_preserved_and_plain_mode_strips(control):
    result = ui.response(control + "shell", enabled=True)
    assert control in result
    assert result.endswith(ui.RESET)
    assert ui.sanitize(result) == "shell"
    assert ui.response(control + "shell", enabled=False) == "shell"


def test_unterminated_control_strings_fail_closed():
    for sequence in ("\x1b]52;secret", "\x1bPsecret", "\x9fsecret"):
        assert ui.sanitize("ok" + sequence, True) == "ok"


def test_truncation_counts_plain_characters_and_retained_lines_reset():
    service = PiService()
    raw = "\x1b[32m" + "\n".join(f"line {i}" for i in range(22))
    truncated = service._truncate_tool_result_text(raw, preserve_sgr=True)
    plain = service._truncate_tool_result_text(raw)
    assert ui.sanitize(truncated) == plain
    omitted = "\n".join(f"line {i}" for i in range(15, 20))
    assert f"[5 lines, {len(omitted)} characters truncated]" in truncated
    lines = truncated.splitlines()
    assert lines[14].endswith(ui.RESET)
    assert lines[15].startswith("[5 lines")
    assert lines[16].startswith("\x1b[32m")
    assert lines[16].endswith(ui.RESET)


@pytest.mark.parametrize("path,text", [
    ("demo.py", "def greet(name: str):\n    return 'hello ' + name\n"),
    ("demo.ts", "const count: number = 12;\nfunction greet(): string { return 'hi'; }\n"),
])
def test_read_highlight_uses_explicit_path_and_preserves_bytes(path, text):
    colored = ui.response(text, enabled=True, tool="read", args={"path": path})
    assert "\x1b[" in colored
    assert ui.sanitize(colored) == text
    assert "38;" not in colored  # Works without tmux truecolor/256-color configuration.
    assert ui.response(text, enabled=True, tool="bash") == text
    assert ui.response(text, enabled=True, tool="read", args={"path": "unknown.xyz"}) == text


@pytest.mark.parametrize("text", ['{"x":"a\\nb"}', 'pattern = r"\\n\\t"'])
def test_semantic_results_do_not_unescape_source_or_json(text):
    service = PiService()
    result = service._semantic_result_text({"result": text})
    assert ui.sanitize(result) == text


def test_json_original_whitespace_and_token_colors():
    text = '{\n  "name": "value", "n": 123, "ok": true\n}'
    colored = ui.response(text, enabled=True)
    assert ui.sanitize(colored) == text
    assert ui.CYAN in colored and "\x1b[32m" in colored


def test_source_omission_and_provider_notice_are_not_lexer_tokens():
    text = 'def f():\n    return 1\n[3 lines, 12 characters truncated]\n# tail\n[90 more lines in file. Use offset=20 to continue.]'
    colored = ui.response(text, enabled=True, tool="read", args={"path": "x.py"})
    assert ui.sanitize(colored) == text
    assert "\x1b[33m[3 lines, 12 characters truncated]" in colored
    assert "\x1b[33m[90 more lines" in colored


def test_markdown_prose_and_fences_no_reflow():
    source = '# Heading\nBody **strong** and *soft* and `code`.\n- item\n| a | b |\n```python\ndef f():\n    return 2\n```'
    result = ui.markdown(source, enabled=True)
    assert ui.sanitize(result) == 'Heading\nBody strong and soft and code.\n- item\n| a | b |\n```python\ndef f():\n    return 2\n```'
    assert ui.BOLD + 'Heading' in result
    assert ui.ITALIC + 'soft' in result
    assert '\x1b[34mdef' in result
    assert ui.markdown(source, enabled=False) == source
    assert ui.response(source, enabled=True, tool="bash") == source
    assert ui.markdown("body", enabled=True) == "body"
    assert ui.markdown("body", enabled=True, thinking=True) == ui.ITALIC + "body" + ui.RESET


def test_unknown_and_empty_fences_preserve_lines():
    for text in ('```unknown\na **b**\n```', '```python\n```', '```python\nx = 1'):
        assert ui.sanitize(ui.markdown(text, enabled=True)) == text


def test_failures_override_shell_style_without_reinterpreting_words():
    raw = "\x1b[32merror failed blocked"
    failed = ui.response(raw, enabled=True, is_error=True)
    assert "\x1b[32m" not in failed
    assert ui.BOLD + ui.RED in failed
    assert ui.response("error failed blocked", enabled=True) == "error failed blocked"


@pytest.mark.parametrize("setting", ["NO_COLOR", "pipe", "dumb"])
def test_plain_modes_disable_every_presentation_type(monkeypatch, setting):
    if setting == "NO_COLOR": monkeypatch.setenv("NO_COLOR", "")
    elif setting == "pipe": monkeypatch.setenv("JUNO_PI_OUTPUT_TTY", "0")
    else: monkeypatch.setenv("TERM", "dumb")
    service = PiService()
    assert service._style_assistant("**body**") == "**body**"
    assert service._style_thinking("plan") == "plan"
    block = service._semantic_tool_block({"id": 1, "tool": "read", "args": {"path": "x.py"}},
        status="done", result="\x1b[31mreturn 42")
    assert "\x1b" not in block
    assert "[tool:read]" in block and "return 42" in block


def test_parallel_read_paths_stay_correlated(monkeypatch):
    service = PiService()
    monkeypatch.setattr(service, "_schedule_semantic_running", lambda key: None)
    for name, path in (("py", "x.py"), ("text", "x.txt")):
        service._format_semantic_event({"type": "tool_execution_start", "toolName": "read",
            "toolCallId": name, "args": {"path": path}})
    outputs = {}
    for name in ("text", "py"):
        outputs[name] = service._format_semantic_event({"type": "tool_execution_end", "toolName": "read",
            "toolCallId": name, "result": "return 42"})
    assert "\x1b[34mreturn" in outputs["py"]
    assert "  return 42\n" in outputs["text"]


def test_highlight_bound_and_missing_lexer_fall_back(monkeypatch):
    text = "x = 1\n" * 12000
    assert ui.highlight(text, "python") == text
    monkeypatch.setattr(ui, "lexer_for", lambda language: None)
    assert ui.highlight("return 42", "python") == "return 42"


def test_bundled_lexer_digest_and_isolated_python():
    manifest = json.loads((SERVICES / "vendor/pygments.json").read_text())
    wheel = SERVICES / "vendor" / manifest["file"]
    assert hashlib.sha256(wheel.read_bytes()).hexdigest() == manifest["sha256"]
    assert (SERVICES / "vendor/PYGMENTS-LICENSE").is_file()
    script = "import headless_presentation as p; s='return 42'; r=p.highlight(s,'python'); assert '\\x1b[' in r; assert p.sanitize(r)==s"
    result = subprocess.run([sys.executable, "-S", "-c", script], cwd=SERVICES,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
