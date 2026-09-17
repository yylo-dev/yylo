"""Small, append-only terminal presentation. Never used for raw/capture payloads."""
import json
import re
import sys
from functools import lru_cache
from pathlib import Path

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
ITALIC = "\x1b[3m"
CYAN = "\x1b[36m"
RED = "\x1b[31m"
MAX_HIGHLIGHT_CHARS = 65536
OMISSION = re.compile(r"^\[\d+ lines, \d+ characters truncated\]$")
# Consume control strings as units, including unterminated strings (fail closed).
CONTROL = re.compile(
    r"(?:\x1b\]|\x9d).*?(?:\x07|\x1b\\|\x9c|$)"
    r"|(?:\x1b[P^_X]|[\x90\x98\x9e\x9f]).*?(?:\x1b\\|\x9c|$)"
    r"|(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]"
    r"|\x1b[ -/]*[@-Z\\-_]"
    r"|[\x00-\x08\x0b-\x1f\x7f-\x9f]", re.DOTALL)
SGR = re.compile(r"\x1b\[([0-9;]*)m")


def safe_sgr(sequence):
    match = SGR.fullmatch(sequence)
    if not match or len(sequence) > 128:
        return False
    values = [int(n or 0) for n in match[1].split(";")]
    i = 0
    while i < len(values):
        n = values[i]
        if n in (38, 48):
            count = 1 if values[i + 1:i + 2] == [5] else 3 if values[i + 1:i + 2] == [2] else 0
            if not count or len(values) < i + count + 2:
                return False
            if any(v > 255 for v in values[i + 2:i + 2 + count]):
                return False
            i += count + 2
        elif n in (0, 1, 2, 3, 4, 22, 23, 24, 39, 49) or 30 <= n <= 37 or 40 <= n <= 47 or 90 <= n <= 107:
            i += 1
        else:
            return False
    return True


def sanitize(text, keep_sgr=False):
    return CONTROL.sub(lambda m: m[0] if keep_sgr and safe_sgr(m[0]) else "", text)


def isolated_lines(text):
    """Carry shell styles across lines, but reset each line before framing it.

    Bound replay state even for hostile streams. A normal shell reset drops all
    prior state. Each retained line is independent when middle lines are omitted.
    """
    active = []
    output = []
    for line in sanitize(text, True).split("\n"):
        prefix = "".join(active)
        for match in SGR.finditer(line):
            # Empty / leading zero resets; do not confuse RGB zero components.
            if match[1].split(";")[0] in ("", "0"):
                active = []
            active.append(match[0])
            active = active[-16:]
        output.append(prefix + line + (RESET if prefix or "\x1b" in line else ""))
    return output


@lru_cache(maxsize=16)
def lexer_for(language):
    # Pure-Python wheel is shipped alongside the service. No pip, plugins,
    # subprocess, network, or globally installed Python dependency is needed.
    wheel = Path(__file__).with_name("vendor") / "pygments-2.19.2-py3-none-any.whl"
    if not wheel.is_file():
        return None
    if str(wheel) not in sys.path:
        sys.path.insert(0, str(wheel))
    try:
        from pygments.lexers import PythonLexer, TypeScriptLexer, JavascriptLexer, JsonLexer, BashLexer, CssLexer, HtmlLexer
        cls = {"python": PythonLexer, "typescript": TypeScriptLexer,
               "javascript": JavascriptLexer, "json": JsonLexer, "bash": BashLexer,
               "css": CssLexer, "html": HtmlLexer}.get(language)
        return cls(stripnl=False, ensurenl=False) if cls else None
    except (ImportError, OSError, ValueError):
        return None


ALIASES = {"py": "python", "python": "python", "ts": "typescript", "tsx": "typescript",
           "typescript": "typescript", "js": "javascript", "jsx": "javascript",
           "javascript": "javascript", "json": "json", "sh": "bash", "bash": "bash",
           "css": "css", "html": "html"}
EXTENSIONS = {".py": "python", ".pyi": "python", ".ts": "typescript", ".tsx": "typescript",
              ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
              ".json": "json", ".sh": "bash", ".css": "css", ".html": "html"}


def highlight(text, language):
    if len(text) > MAX_HIGHLIGHT_CHARS:
        return text
    try:
        lexer = lexer_for(ALIASES.get(language.lower(), ""))
        if lexer is None:
            return text
        tokens = list(lexer.get_tokens(text))
        if "".join(value for _, value in tokens) != text:
            return text
        from pygments.token import Token
        def style(token):
            if token in Token.Comment: return ITALIC
            if token in Token.Keyword: return "\x1b[34m"
            if token in Token.Name.Tag: return CYAN  # JSON keys
            if token in Token.Literal.String: return "\x1b[32m"
            if token in Token.Literal.Number: return "\x1b[36m"
            if token in Token.Name.Function or token in Token.Name.Class: return CYAN
            return ""
        # Token resets never spill onto labels or the next physical line.
        return "".join("\n".join(style(t) + line + RESET if line and style(t) else line
                                  for line in value.split("\n")) for t, value in tokens)
    except Exception:
        # Presentation must not change execution success if a lexer fails.
        return text


def response(text, *, enabled, tool="", args=None, is_error=False):
    plain = sanitize(text)
    if not enabled:
        return plain
    if is_error:
        return "\n".join(BOLD + RED + line + RESET for line in plain.split("\n"))
    language = None
    if str(tool).lower() == "read":
        if isinstance(args, str):
            try: args = json.loads(args)
            except ValueError: args = None
        if isinstance(args, dict):
            filename = args.get("path", args.get("file_path"))
            if isinstance(filename, str):
                language = EXTENSIONS.get(Path(filename).suffix.lower())
    if not language and len(plain) <= MAX_HIGHLIGHT_CHARS:
        try:
            json.loads(plain)
            language = "json"
        except (ValueError, RecursionError):
            pass
    if not language:
        return "\n".join("\x1b[33m" + sanitize(line) + RESET if OMISSION.fullmatch(sanitize(line))
                         else line for line in isolated_lines(text))
    # Highlight each contiguous retained region, never the synthetic marker.
    chunks, current = [], []
    for line in plain.split("\n"):
        if OMISSION.fullmatch(line) or re.fullmatch(r"\[\d+ more lines in file\..*\]", line):
            if current:
                chunks.append(highlight("\n".join(current), language)); current = []
            chunks.append("\x1b[33m" + line + RESET)
        else:
            current.append(line)
    if current:
        chunks.append(highlight("\n".join(current), language))
    return "\n".join(chunks)


INLINE = re.compile(r"(`+)(.+?)\1|\*\*(.+?)\*\*|(?<!\w)\*([^*\n]+)\*(?!\w)|(?<!\w)_([^_\n]+)_(?!\w)")


def markdown(text, *, enabled, thinking=False):
    text = sanitize(text)
    if not enabled or len(text) > MAX_HIGHLIGHT_CHARS:
        return text
    base = ITALIC if thinking else ""
    def inline(line):
        def replace(m):
            value = next(v for v in m.groups()[1:] if v is not None)
            code = CYAN if m[1] else BOLD if m[3] else ITALIC
            return code + value + RESET + base
        return INLINE.sub(replace, line)
    lines, output, index = text.split("\n"), [], 0
    while index < len(lines):
        line = lines[index]
        fence = re.fullmatch(r"\s*(`{3,}|~{3,})([\w+-]*)\s*", line)
        if fence:
            end = index + 1
            while end < len(lines) and not re.fullmatch(r"\s*" + re.escape(fence[1][0]) + "{" + str(len(fence[1])) + r",}\s*", lines[end]):
                end += 1
            # Keep fence labels and code text; only prose markup is transformed.
            output.append(CYAN + line + RESET)
            if end > index + 1:
                output.append(highlight("\n".join(lines[index + 1:end]), fence[2]))
            if end < len(lines): output.append(CYAN + lines[end] + RESET)
            index = end + 1
            continue
        heading = re.match(r"^(\s{0,3})#{1,6}\s+(.+)$", line)
        if heading:
            output.append(base + BOLD + heading[1] + inline(heading[2]) + RESET)
        else:
            output.append(base + inline(line) + (RESET if base else ""))
        index += 1
    return "\n".join(output)
