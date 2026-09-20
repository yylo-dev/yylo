#!/usr/bin/env python3
"""Watch an existing PID/log/footer producer without owning its lifecycle."""

from __future__ import annotations

import argparse
import codecs
import json
import math
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, TextIO

MAX_TAIL_BYTES = 64 * 1024
EVENT_SCHEMA = "juno.watch-event.v1"
FOOTER_SCHEMA = "juno.watch-footer.v1"
FOOTER_PATTERN = re.compile(
    rb"\Aschema_version=juno\.watch-footer\.v1\n"
    rb"exit_code=(0|[1-9][0-9]{0,2})\n"
    rb"completed_utc=([0-9]{4}-[0-9]{2}-[0-9]{2}T"
    rb"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z)\n\Z"
)


class WatchError(Exception):
    pass


class WatchInterrupted(Exception):
    def __init__(self, signum: int):
        self.signum = signum


@dataclass(frozen=True)
class ProcessIdentity:
    token: str
    start_epoch: Optional[float]
    state: str


@dataclass(frozen=True)
class FooterObservation:
    data: Optional[bytes]
    exit_code: Optional[int]
    completed_utc: Optional[str]
    error: Optional[str]

    @property
    def valid(self) -> bool:
        return self.data is not None and self.error is None


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def emit(event: str, **fields: object) -> None:
    record = {"schema_version": EVENT_SCHEMA, "event": event, "utc": utc_now(), **fields}
    print(json.dumps(record, ensure_ascii=True, separators=(",", ":")), flush=True)


def print_payload(name: str, data: bytes) -> None:
    """Frame exact bytes by length; no newline is inserted into the payload."""
    emit("payload_begin", payload_name=name, byte_length=len(data))
    sys.stdout.flush()
    if data:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    emit("payload_end", payload_name=name, byte_length=len(data))


def linux_identity(pid: int) -> Optional[ProcessIdentity]:
    stat_path = Path(f"/proc/{pid}/stat")
    try:
        raw = stat_path.read_text()
        close = raw.rfind(")")
        fields = raw[close + 2 :].split()
        state, start_ticks = fields[0], int(fields[19])
        clock_ticks = os.sysconf("SC_CLK_TCK")
        boot_epoch = None
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                boot_epoch = int(line.split()[1])
                break
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        start_epoch = None if boot_epoch is None else boot_epoch + start_ticks / clock_ticks
        return ProcessIdentity(f"linux:{boot_id}:{start_ticks}", start_epoch, state)
    except (FileNotFoundError, ProcessLookupError):
        return None
    except (OSError, ValueError, IndexError):
        return None


def ps_identity(pid: int) -> Optional[ProcessIdentity]:
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-o", "stat=", "-p", str(pid)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    line = result.stdout.strip()
    parts = line.split()
    if len(parts) < 6:
        return None
    start_text, state = " ".join(parts[:5]), parts[5]
    try:
        start_epoch = time.mktime(time.strptime(start_text, "%a %b %d %H:%M:%S %Y"))
    except ValueError:
        start_epoch = None
    return ProcessIdentity(f"ps:{start_text}", start_epoch, state)


def process_identity(pid: int) -> Optional[ProcessIdentity]:
    if sys.platform.startswith("linux"):
        identity = linux_identity(pid)
        if identity is not None:
            return identity
    return ps_identity(pid)


def pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False


def read_pid(pid_file: Path) -> int:
    try:
        text = pid_file.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise WatchError(f"cannot read --pid-file {pid_file}: {exc}") from exc
    if not text.isdigit() or int(text) <= 0:
        raise WatchError(f"--pid-file {pid_file} must contain one positive numeric PID")
    return int(text)


def observe_footer(footer_file: Path) -> FooterObservation:
    try:
        with footer_file.open("rb") as handle:
            data = handle.read(MAX_TAIL_BYTES + 1)
    except FileNotFoundError:
        return FooterObservation(None, None, None, None)
    except OSError as exc:
        raise WatchError(f"cannot read --footer-file {footer_file}: {exc}") from exc
    match = FOOTER_PATTERN.fullmatch(data)
    if match is None:
        return FooterObservation(data, None, None, "footer does not match the exact v1 schema")
    exit_code = int(match.group(1))
    if exit_code > 255:
        return FooterObservation(data, None, None, "exit_code is outside the supported 0..255 range")
    completed_utc = match.group(2).decode("ascii")
    try:
        datetime.fromisoformat(completed_utc[:-1] + "+00:00")
    except ValueError:
        return FooterObservation(data, None, None, "completed_utc is not a valid UTC timestamp")
    return FooterObservation(data, exit_code, completed_utc, None)


def recent_output(log_file: Path, line_limit: int) -> tuple[bytes, int]:
    try:
        size = log_file.stat().st_size
        with log_file.open("rb") as handle:
            handle.seek(max(0, size - MAX_TAIL_BYTES))
            data = handle.read(MAX_TAIL_BYTES)
    except FileNotFoundError:
        return b"", 0
    except OSError as exc:
        raise WatchError(f"cannot read --log-file {log_file}: {exc}") from exc
    lines = data.splitlines(keepends=True)
    return b"".join(lines[-line_limit:]), size


def print_valid_footer(footer_file: Path, observation: FooterObservation) -> int:
    assert observation.data is not None and observation.exit_code is not None
    emit(
        "footer_valid",
        footer_path=str(footer_file),
        byte_length=len(observation.data),
        footer_schema=FOOTER_SCHEMA,
        producer_exit_code=observation.exit_code,
        completed_utc=observation.completed_utc,
    )
    print_payload("footer", observation.data)
    return 0


def validate_args(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    paths = tuple(Path(value).expanduser() for value in (args.pid_file, args.log_file, args.footer_file))
    if len({str(path.absolute()) for path in paths}) != 3:
        raise WatchError("--pid-file, --log-file, and --footer-file must be distinct paths")
    for name in ("poll_interval", "snapshot_interval", "footer_grace"):
        value = getattr(args, name)
        if not (math.isfinite(value) and value > 0):
            raise WatchError(f"--{name.replace('_', '-')} must be finite and greater than zero")
    if args.tail_lines <= 0:
        raise WatchError("--tail-lines must be greater than zero")
    for label, path in (("--log-file", paths[1]), ("--footer-file", paths[2])):
        if path.exists() and not path.is_file():
            raise WatchError(f"{label} must name a regular file when present: {path}")
    return paths  # type: ignore[return-value]


def watch(
    args: argparse.Namespace,
    *,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    pid_file, log_file, footer_file = validate_args(args)
    pid = read_pid(pid_file)

    # Footer is checked before liveness. Only a valid footer is terminal truth.
    footer = observe_footer(footer_file)
    if footer.valid:
        emit("watch_started", pid=pid, state="valid_footer_already_present")
        return print_valid_footer(footer_file, footer)

    identity = process_identity(pid)
    live = identity is not None and pid_exists(pid) and "Z" not in identity.state.upper()
    if footer.data is None and not live:
        raise WatchError(f"PID {pid} is not a live process and footer is absent: {footer_file}")
    try:
        pid_mtime = pid_file.stat().st_mtime
    except OSError as exc:
        raise WatchError(f"cannot stat --pid-file {pid_file}: {exc}") from exc
    if live and identity is not None and identity.start_epoch is not None and identity.start_epoch > pid_mtime:
        raise WatchError(f"PID {pid} started after the PID file was written; refusing a stale/reused PID attachment")

    started = monotonic()
    next_poll = started + args.poll_interval
    next_snapshot = started + args.snapshot_interval
    exit_seen: Optional[float] = None if live else started
    malformed_signature: Optional[tuple[int, int]] = None
    emit(
        "watch_started",
        pid=pid,
        state="active" if live else "malformed_footer_after_process_exit",
        identity=None if identity is None else identity.token,
        poll_seconds=args.poll_interval,
    )
    if not live:
        emit("process_exited", pid=pid, footer_grace_seconds=args.footer_grace)

    while True:
        footer = observe_footer(footer_file)
        if footer.valid:
            return print_valid_footer(footer_file, footer)
        if footer.data is not None:
            signature = (len(footer.data), hash(footer.data))
            if signature != malformed_signature:
                emit(
                    "footer_malformed_waiting",
                    footer_path=str(footer_file),
                    byte_length=len(footer.data),
                    reason=footer.error,
                    policy="wait_while_live_then_fail_after_exit_grace",
                )
                malformed_signature = signature

        current = process_identity(pid) if live else None
        currently_live = (
            current is not None and pid_exists(pid) and "Z" not in current.state.upper()
        )
        if currently_live and identity is not None and (
            current.token != identity.token
            or (current.start_epoch is not None and current.start_epoch > pid_mtime)
        ):
            raise WatchError(
                f"PID {pid} identity changed from {identity.token} to {current.token}; refusing PID reuse"
            )
        now = monotonic()
        if live and not currently_live:
            live = False
            exit_seen = now
            emit("process_exited", pid=pid, footer_grace_seconds=args.footer_grace)
        if exit_seen is not None and now - exit_seen >= args.footer_grace:
            data, size = recent_output(log_file, args.tail_lines)
            if footer.data is not None:
                emit(
                    "malformed_footer",
                    pid=pid,
                    footer_path=str(footer_file),
                    byte_length=len(footer.data),
                    reason=footer.error,
                    log_bytes=size,
                    tail_lines=args.tail_lines,
                )
                print_payload("malformed_footer", footer.data)
            else:
                emit("missing_footer", pid=pid, log_bytes=size, tail_lines=args.tail_lines)
            print_payload("final_tail", data)
            return 2
        if exit_seen is None and now >= next_snapshot:
            data, size = recent_output(log_file, args.tail_lines)
            emit(
                "snapshot",
                pid=pid,
                elapsed_seconds=round(now - started, 1),
                state="active",
                log_bytes=size,
                tail_lines=args.tail_lines,
            )
            print_payload("snapshot_tail", data)
            while next_snapshot <= now:
                next_snapshot += args.snapshot_interval

        # Deadline scheduling subtracts identity/snapshot work instead of adding a full interval.
        now = monotonic()
        if now < next_poll:
            sleep(next_poll - now)
        after_sleep = monotonic()
        while next_poll <= after_sleep:
            next_poll += args.poll_interval


RUN_SCHEMA = "juno.watch-run.v1"
SEMANTIC_TAGS = {"THINKING", "TOOL", "INPUT", "TOOL_RESPONSE", "ANSWER", "STATUS"}
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
ANSI_RESET = "\x1b[0m"
ANSI_DIM_ITALIC = "\x1b[2m\x1b[3m"
ANSI_INPUT = "\x1b[1m\x1b[38;5;44m"
ANSI_RESPONSE = "\x1b[2m\x1b[38;5;108m"
ANSI_ERROR = "\x1b[1m\x1b[38;5;203m"
ANSI_ANSWER = "\x1b[1m"
ANSI_METADATA = "\x1b[2m\x1b[38;5;75m"
OPEN_TAG = re.compile(r"^\[(THINKING|TOOL|INPUT|TOOL_RESPONSE|ANSWER|STATUS)\](?: (\{.*\}))?(\n?)$")
CLOSE_TAG = re.compile(r"^\[/(TOOL|INPUT|TOOL_RESPONSE|ANSWER|STATUS)\](\n?)$")
THINKING_CLOSE = re.compile(r"^\[/THINKING : [0-9]+(?:\.[0-9]+)?s\](\n?)$")


class SemanticFollowFormatter:
    """Color only the frozen semantic grammar; pass everything else through."""

    def __init__(self, stream: TextIO, color: bool):
        self.stream = stream
        self.color = color
        self.pending = ""
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.stack: list[str] = []
        self.tool_errors: list[bool] = []

    def feed(self, data: bytes, *, final: bool = False) -> None:
        self.pending += self.decoder.decode(data, final=final)
        lines = self.pending.splitlines(keepends=True)
        if lines and not lines[-1].endswith(("\n", "\r")) and not final:
            self.pending = lines.pop()
        else:
            self.pending = ""
        for line in lines:
            self._line(line)
        if len(self.pending) > MAX_TAIL_BYTES:
            # Arbitrary unterminated log lines must not accumulate without bound.
            self.stream.write(self.pending)
            self.pending = ""
        if final and self.pending:
            value, self.pending = self.pending, ""
            self._line(value)
        self.stream.flush()

    def _styled(self, text: str, code: str) -> str:
        clean = ANSI_RE.sub("", text)
        return f"{code}{clean.rstrip(chr(10))}{ANSI_RESET}" + ("\n" if text.endswith("\n") else "")

    def _line(self, line: str) -> None:
        opening = OPEN_TAG.fullmatch(line)
        if opening:
            tag, raw_metadata, newline = opening.groups()
            metadata = None
            if tag in {"THINKING", "TOOL", "ANSWER", "STATUS"} and raw_metadata is None:
                self.stream.write(line); return
            if raw_metadata is not None:
                try: metadata = json.loads(raw_metadata)
                except json.JSONDecodeError: metadata = None
                if not isinstance(metadata, dict):
                    self.stream.write(line); return
            if tag == "TOOL":
                self.tool_errors.append(bool(metadata and metadata.get("isError") is True))
            self.stack.append(tag)
            if not self.color:
                self.stream.write(line); return
            label = f"[{tag}]"
            rendered = self._styled(label, ANSI_DIM_ITALIC)
            if raw_metadata is not None:
                rendered = rendered.rstrip("\n") + " " + self._styled(raw_metadata, ANSI_METADATA)
            if newline and not rendered.endswith("\n"): rendered += "\n"
            self.stream.write(rendered)
            return

        closing = CLOSE_TAG.fullmatch(line)
        thinking_close = THINKING_CLOSE.fullmatch(line)
        if closing or thinking_close:
            tag = "THINKING" if thinking_close else closing.group(1)
            if not self.stack or self.stack[-1] != tag:
                self.stream.write(line); return
            self.stack.pop()
            if tag == "TOOL" and self.tool_errors: self.tool_errors.pop()
            self.stream.write(self._styled(line, ANSI_DIM_ITALIC) if self.color else line)
            return

        if not self.color or not self.stack:
            self.stream.write(line); return
        current = self.stack[-1]
        if current == "INPUT": code = ANSI_INPUT
        elif current == "TOOL_RESPONSE": code = ANSI_ERROR if self.tool_errors and self.tool_errors[-1] else ANSI_RESPONSE
        elif current == "THINKING": code = ANSI_DIM_ITALIC
        elif current == "ANSWER": code = ANSI_ANSWER
        else:
            self.stream.write(line); return
        self.stream.write(self._styled(line, code))


def follow_run(root: Path, run_id: str, *, poll_interval: float = 0.05,
               sleep: Callable[[float], None] = time.sleep,
               stream: Optional[TextIO] = None, color: Optional[bool] = None) -> int:
    """Read bounded chunks; missing terminal evidence is never successful work."""
    _run_dir, _metadata, pid_file, log_file, footer_file = _run_paths(root, run_id)
    output = stream or sys.stdout
    use_color = (hasattr(output, "isatty") and output.isatty()
                 and os.environ.get("NO_COLOR") is None) if color is None else color
    formatter = SemanticFollowFormatter(output, bool(use_color))
    offset = 0
    identity: Optional[ProcessIdentity] = None
    exit_seen: Optional[float] = None
    while True:
        try:
            with log_file.open("rb") as handle:
                if os.fstat(handle.fileno()).st_size < offset:
                    raise WatchError("run log was truncated; preserve producer evidence")
                handle.seek(offset)
                chunk = handle.read(MAX_TAIL_BYTES)
        except FileNotFoundError:
            chunk = b""
        except OSError as exc:
            raise WatchError(f"cannot read run log {log_file}: {exc}") from exc
        if chunk:
            formatter.feed(chunk)
            offset += len(chunk)
        footer = observe_footer(footer_file)
        if footer.valid:
            # Drain the final snapshot in bounded chunks, not one unbounded read.
            try:
                with log_file.open("rb") as handle:
                    end = os.fstat(handle.fileno()).st_size
                    handle.seek(offset)
                    while offset < end:
                        final_chunk = handle.read(min(MAX_TAIL_BYTES, end - offset))
                        if not final_chunk:
                            break
                        formatter.feed(final_chunk)
                        offset += len(final_chunk)
            except FileNotFoundError:
                pass
            formatter.feed(b"", final=True)
            assert footer.exit_code is not None
            return footer.exit_code
        pid = read_pid(pid_file)
        current = process_identity(pid)
        if current is not None and pid_exists(pid) and "Z" not in current.state.upper():
            if ((identity is not None and current.token != identity.token)
                    or (current.start_epoch is not None
                        and current.start_epoch > pid_file.stat().st_mtime)):
                raise WatchError("producer identity changed; refusing PID reuse")
            identity = current
        elif exit_seen is None:
            exit_seen = time.monotonic()
        if exit_seen is not None and time.monotonic() - exit_seen >= 3.0:
            formatter.feed(b"", final=True)
            raise WatchError("producer exited without a valid footer; execution outcome unknown")
        sleep(poll_interval)


def _run_root(value: Optional[str]) -> Path:
    controller = os.environ.get("JUNO_TASK_ROOT")
    root = (Path(value).expanduser() if value else
            Path(controller).expanduser() / ".juno_task/runtime/watch-runs" if controller else
            Path.cwd() / ".juno_task/runtime/watch-runs")
    return root.absolute()


def _run_paths(root: Path, run_id: str) -> tuple[Path, Path, Path, Path, Path]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id):
        raise WatchError("run id is malformed")
    run_dir = root / run_id
    return run_dir, run_dir / "run.json", run_dir / "pid", run_dir / "combined.log", run_dir / "footer"


def _run_record(root: Path, run_id: str) -> dict[str, object]:
    _directory, metadata, _pid, _log, _footer = _run_paths(root, run_id)
    try:
        with metadata.open("rb") as handle:
            data = handle.read(MAX_TAIL_BYTES + 1)
        if len(data) > MAX_TAIL_BYTES:
            raise WatchError("run metadata exceeds observation limit")
        value = json.loads(data)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WatchError(f"run metadata is unavailable for {run_id}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != RUN_SCHEMA or value.get("run_id") != run_id:
        raise WatchError("run metadata identity is malformed")
    return value


def observed_run(root: Path, run_id: str, record: dict[str, object]) -> dict[str, object]:
    footer = observe_footer(_run_paths(root, run_id)[4])
    return {**record, "recorded_state": record.get("state"),
            "state": "COMPLETED" if footer.valid else "UNKNOWN",
            "exit_code": footer.exit_code if footer.valid else None,
            "execution_outcome": "exited" if footer.valid else "unknown",
            "footer_error": footer.error,
            "semantic_outcome": "not_evaluated", "cleanup_outcome": "not_evaluated",
            "task_completion": "not_evaluated", "read_only": True}


def run_command_cli(argv: list[str]) -> int:
    command = argv[0]
    parser = argparse.ArgumentParser(prog=f"watch_progress.py {command}")
    parser.add_argument("--root")
    if command in {"exec", "_produce"}:
        raise WatchError("watch execution is retired: run commands or agents explicitly "
                         "outside watch; observe existing runs with status, await or follow. "
                         "Only task finish verifies delivery.")
    parser.add_argument("run_id")
    args = parser.parse_args(argv[1:])
    root = _run_root(args.root)
    record = _run_record(root, args.run_id)
    if command == "status":
        print(json.dumps(observed_run(root, args.run_id, record), sort_keys=True)); return 0
    run_dir, _metadata, pid_file, log_file, footer_file = _run_paths(root, args.run_id)
    if command == "follow":
        return follow_run(root, args.run_id)
    if command == "await":
        watch_args = argparse.Namespace(pid_file=str(pid_file), log_file=str(log_file),
            footer_file=str(footer_file), poll_interval=0.2, snapshot_interval=60.0,
            tail_lines=40, footer_grace=3.0)
        result = watch(watch_args)
        if result: return result
        observed = observed_run(root, args.run_id, record)
        print(json.dumps(observed, sort_keys=True))
        return int(observed["exit_code"]) if observed["exit_code"] is not None else 2
    raise WatchError(f"unknown run command: {command}")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Watch an existing producer's PID, combined log, and strict terminal footer."
    )
    result.add_argument("--pid-file", required=True)
    result.add_argument("--log-file", required=True)
    result.add_argument("--footer-file", required=True)
    result.add_argument("--poll-interval", type=float, default=1.0)
    result.add_argument("--snapshot-interval", type=float, default=60.0)
    result.add_argument("--tail-lines", type=int, default=40)
    result.add_argument("--footer-grace", type=float, default=3.0)
    return result


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] in {"exec", "_produce", "status", "await", "follow"}:
        try:
            return run_command_cli(sys.argv[1:])
        except KeyboardInterrupt:
            return 130
        except (WatchError, OSError) as exc:
            print(f"watch command: error: {exc}", file=sys.stderr); return 2

    def interrupted(signum: int, _frame: object) -> None:
        raise WatchInterrupted(signum)

    signal.signal(signal.SIGTERM, interrupted)
    try:
        return watch(parser().parse_args())
    except WatchError as exc:
        emit("error", message=str(exc))
        return 2
    except WatchInterrupted as exc:
        emit(
            "interrupted",
            signal=signal.Signals(exc.signum).name,
            producer_action="none",
        )
        return 128 + exc.signum
    except KeyboardInterrupt:
        emit("interrupted", signal="SIGINT", producer_action="none")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
