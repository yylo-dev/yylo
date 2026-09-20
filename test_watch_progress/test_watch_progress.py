import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "src/templates/scripts/watch_progress.py"
VALID_FOOTER = (
    b"schema_version=juno.watch-footer.v1\n"
    b"exit_code=7\n"
    b"completed_utc=2026-08-12T21:09:28.123Z\n"
)


def load_watcher():
    spec = importlib.util.spec_from_file_location("watch_progress_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class WatchProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.children = []

    def tearDown(self):
        for child in self.children:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    child.kill()
        self.temp.cleanup()

    def atomic_write(self, path, data):
        temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
        temporary.write_bytes(data)
        temporary.replace(path)

    def producer(self, seconds=5):
        child = subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"])
        self.children.append(child)
        temporary = self.root / "pid.tmp"
        temporary.write_text(f"{child.pid}\n")
        temporary.replace(self.root / "pid")
        return child

    def command(self, *extra, root=None):
        root = root or self.root
        return [
            sys.executable,
            str(SCRIPT),
            "--pid-file", str(root / "pid"),
            "--log-file", str(root / "log"),
            "--footer-file", str(root / "footer"),
            "--poll-interval", "0.05",
            "--snapshot-interval", "10",
            "--footer-grace", "0.4",
            *extra,
        ]

    def invoke(self, *extra, timeout=4, root=None):
        return subprocess.run(self.command(*extra, root=root), capture_output=True, timeout=timeout)

    def events(self, output):
        result = []
        offset = 0
        while offset < len(output):
            end = output.find(b"\n", offset)
            self.assertNotEqual(end, -1, output[offset:])
            event = json.loads(output[offset:end])
            result.append(event)
            offset = end + 1
            if event["event"] == "payload_begin":
                length = event["byte_length"]
                payload = output[offset:offset + length]
                offset += length
                end = output.find(b"\n", offset)
                self.assertNotEqual(end, -1)
                closing = json.loads(output[offset:end])
                result.append({**closing, "payload": payload})
                offset = end + 1
        return result

    def payload(self, output, name):
        matches = [event for event in self.events(output) if event.get("event") == "payload_end" and event.get("payload_name") == name]
        self.assertEqual(len(matches), 1, self.events(output))
        return matches[0]["payload"]

    def test_template_and_installed_runtime_are_byte_identical(self):
        runtime = Path(__file__).parents[2] / ".juno_task/scripts/watch_progress.py"
        self.assertEqual(SCRIPT.read_bytes(), runtime.read_bytes())

    def test_every_timing_argument_requires_a_finite_positive_value(self):
        flags = ("--poll-interval", "--snapshot-interval", "--footer-grace")
        invalid_values = ("nan", "inf", "-inf", "0")
        (self.root / "pid").write_text("999999\n")
        (self.root / "footer").write_bytes(VALID_FOOTER)

        for flag in flags:
            for value in invalid_values:
                with self.subTest(flag=flag, value=value):
                    result = self.invoke(f"{flag}={value}")
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    errors = [event for event in self.events(result.stdout) if event["event"] == "error"]
                    self.assertEqual(len(errors), 1, self.events(result.stdout))
                    self.assertEqual(
                        errors[0]["message"],
                        f"{flag} must be finite and greater than zero",
                    )

            with self.subTest(flag=flag, value="ordinary positive"):
                result = self.invoke(f"{flag}=0.125")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("footer_valid", [event["event"] for event in self.events(result.stdout)])

    def test_valid_existing_footer_is_terminal_truth_after_process_exit(self):
        (self.root / "pid").write_text("999999\n")
        (self.root / "footer").write_bytes(VALID_FOOTER)
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        events = self.events(result.stdout)
        self.assertIn("footer_valid", [event["event"] for event in events])
        self.assertEqual(self.payload(result.stdout, "footer"), VALID_FOOTER)

    def test_strict_footer_contract_rejects_empty_partial_duplicate_unknown_invalid_and_arbitrary(self):
        invalid = {
            "empty": b"",
            "partial": b"schema_version=juno.watch-footer.v1\nexit_code=0\n",
            "duplicate": VALID_FOOTER.replace(b"exit_code=7\n", b"exit_code=7\nexit_code=0\n"),
            "unknown": VALID_FOOTER + b"message=done\n",
            "version": VALID_FOOTER.replace(b"v1", b"v2"),
            "negative_exit": VALID_FOOTER.replace(b"exit_code=7", b"exit_code=-1"),
            "large_exit": VALID_FOOTER.replace(b"exit_code=7", b"exit_code=256"),
            "float_exit": VALID_FOOTER.replace(b"exit_code=7", b"exit_code=1.0"),
            "invalid_time": VALID_FOOTER.replace(b"2026-08-12T21:09:28.123Z", b"2026-02-30T21:09:28Z"),
            "offset_time": VALID_FOOTER.replace(b"2026-08-12T21:09:28.123Z", b"2026-08-12T21:09:28+01:00"),
            "arbitrary": b"done\n\x00bytes",
        }
        for name, footer in invalid.items():
            with self.subTest(name=name):
                case = self.root / name
                case.mkdir()
                (case / "pid").write_text("999999\n")
                (case / "footer").write_bytes(footer)
                result = self.invoke("--footer-grace", "0.05", root=case)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("malformed_footer", [event["event"] for event in self.events(result.stdout)])
                self.assertEqual(self.payload(result.stdout, "malformed_footer"), footer)

    def test_malformed_live_footer_can_be_atomically_replaced_before_exit(self):
        self.producer()
        (self.root / "footer").write_bytes(b"partial")
        threading.Timer(0.12, lambda: self.atomic_write(self.root / "footer", VALID_FOOTER)).start()
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        names = [event["event"] for event in self.events(result.stdout)]
        self.assertIn("footer_malformed_waiting", names)
        self.assertIn("footer_valid", names)

    def test_footer_appears_live_and_is_detected_without_long_poll_lag(self):
        self.producer()
        threading.Timer(0.15, lambda: self.atomic_write(self.root / "footer", VALID_FOOTER)).start()
        started = time.monotonic()
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertLess(time.monotonic() - started, 0.7)
        self.assertEqual(self.payload(result.stdout, "footer"), VALID_FOOTER)

    def test_quiet_live_process_emits_bounded_snapshots_with_missing_log(self):
        self.producer()
        threading.Timer(0.32, lambda: self.atomic_write(self.root / "footer", VALID_FOOTER)).start()
        result = self.invoke("--snapshot-interval", "0.1", "--tail-lines", "2")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        snapshots = [event for event in self.events(result.stdout) if event["event"] == "snapshot"]
        self.assertTrue(snapshots)
        self.assertEqual(snapshots[0]["log_bytes"], 0)

    def test_process_exit_then_valid_footer_during_grace(self):
        self.producer(0.12)
        threading.Timer(0.28, lambda: self.atomic_write(self.root / "footer", VALID_FOOTER)).start()
        result = self.invoke("--footer-grace", "0.8")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("process_exited", [event["event"] for event in self.events(result.stdout)])

    def test_process_exit_without_footer_prints_bounded_final_tail(self):
        self.producer(0.1)
        (self.root / "log").write_text("one\ntwo\nthree\n")
        result = self.invoke("--tail-lines", "2")
        self.assertEqual(result.returncode, 2)
        self.assertIn("missing_footer", [event["event"] for event in self.events(result.stdout)])
        self.assertEqual(self.payload(result.stdout, "final_tail"), b"two\nthree\n")

    def test_jsonl_metadata_and_length_framing_escape_control_sensitive_values(self):
        odd = self.root / "space = and\nnewline"
        odd.mkdir()
        (odd / "pid").write_text("999999\n")
        (odd / "footer").write_bytes(VALID_FOOTER.replace(b"exit_code=7", b"exit_code=0"))
        result = self.invoke(root=odd)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        events = self.events(result.stdout)
        self.assertTrue(all(event["schema_version"] == "juno.watch-event.v1" for event in events))
        footer_event = next(event for event in events if event["event"] == "footer_valid")
        self.assertEqual(footer_event["footer_path"], str(odd / "footer"))
        self.assertEqual(self.payload(result.stdout, "footer"), (odd / "footer").read_bytes())

    def test_invalid_and_stale_reused_pid_are_rejected(self):
        (self.root / "pid").write_text("not-a-pid\n")
        invalid = self.invoke()
        self.assertEqual(invalid.returncode, 2)
        self.assertTrue(any("positive numeric PID" in event.get("message", "") for event in self.events(invalid.stdout)))

        child = self.producer()
        os.utime(self.root / "pid", (1, 1))
        stale = self.invoke()
        self.assertEqual(stale.returncode, 2)
        self.assertIn(b"stale/reused PID", stale.stdout)
        self.assertIsNone(child.poll())

    def test_empty_log_is_safe_on_missing_footer(self):
        self.producer(0.1)
        (self.root / "log").write_bytes(b"")
        result = self.invoke()
        self.assertEqual(result.returncode, 2)
        missing = next(event for event in self.events(result.stdout) if event["event"] == "missing_footer")
        self.assertEqual(missing["log_bytes"], 0)

    def test_interrupt_stops_only_watcher(self):
        producer = self.producer()
        watcher = subprocess.Popen(self.command(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(0.15)
        watcher.send_signal(signal.SIGINT)
        stdout, _ = watcher.communicate(timeout=2)
        self.assertEqual(watcher.returncode, 130)
        interrupted = next(event for event in self.events(stdout) if event["event"] == "interrupted")
        self.assertEqual(interrupted["producer_action"], "none")
        self.assertIsNone(producer.poll())

    def test_monotonic_deadline_subtracts_slow_identity_work(self):
        watcher = load_watcher()
        clock = SimpleNamespace(value=0.0, sleeps=[])

        def monotonic():
            return clock.value

        def sleep(seconds):
            clock.sleeps.append(seconds)
            clock.value += seconds

        calls = 0

        def identity(_pid):
            nonlocal calls
            calls += 1
            if calls > 1:
                clock.value += 0.08
            if calls == 3:
                (self.root / "footer").write_bytes(VALID_FOOTER)
            return watcher.ProcessIdentity("identity with spaces\nand controls", 0.0, "S")

        (self.root / "pid").write_text("123\n")
        os.utime(self.root / "pid", (1, 1))
        args = SimpleNamespace(
            pid_file=str(self.root / "pid"), log_file=str(self.root / "log"),
            footer_file=str(self.root / "footer"), poll_interval=0.1,
            snapshot_interval=10.0, footer_grace=0.4, tail_lines=2,
        )
        with mock.patch.object(watcher, "process_identity", identity), \
             mock.patch.object(watcher, "pid_exists", return_value=True), \
             mock.patch.object(watcher, "emit"), mock.patch.object(watcher, "print_payload"):
            result = watcher.watch(args, monotonic=monotonic, sleep=sleep)
        self.assertEqual(result, 0)
        self.assertAlmostEqual(clock.sleeps[0], 0.02, places=6)
        self.assertNotIn(0.1, clock.sleeps)

    def write_run(self, run_id="run-1"):
        run_dir = self.root / run_id
        run_dir.mkdir()
        (run_dir / "run.json").write_text(json.dumps({
            "schema_version": "juno.watch-run.v1", "run_id": run_id, "state": "RUNNING"
        }))
        (run_dir / "pid").write_text(str(os.getpid()) + "\n")
        return run_dir

    def follow_command(self, run_id="run-1"):
        return [sys.executable, str(SCRIPT), "follow", "--root", str(self.root), run_id]

    def test_follow_completed_run_from_start_and_propagates_footer_exit(self):
        run_dir = self.write_run()
        content = b"before\n[ANSWER] {\"id\":1,\"time\":\"12:00:00\"}\n  done\n[/ANSWER]\n"
        (run_dir / "combined.log").write_bytes(content)
        (run_dir / "footer").write_bytes(VALID_FOOTER)
        result = subprocess.run(self.follow_command(), capture_output=True, timeout=2)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(result.stdout, content)

    def test_follow_live_run_emits_each_appended_byte_once(self):
        run_dir = self.write_run()
        log = run_dir / "combined.log"
        log.write_bytes(b"first")
        follower = subprocess.Popen(self.follow_command(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(0.1)
        with log.open("ab") as handle:
            handle.write(b" line\nsecond\n"); handle.flush()
        self.atomic_write(run_dir / "footer", VALID_FOOTER.replace(b"exit_code=7", b"exit_code=0"))
        stdout, stderr = follower.communicate(timeout=2)
        self.assertEqual(follower.returncode, 0, stderr)
        self.assertEqual(stdout, b"first line\nsecond\n")

    def test_semantic_formatter_colors_exact_tags_and_structured_errors_only(self):
        watcher = load_watcher()
        stream = io.StringIO()
        formatter = watcher.SemanticFollowFormatter(stream, color=True)
        formatter.feed(
            b'[TOOL] {"id":1,"tool":"bash","status":"done"}\n'
            b'[TOOL_RESPONSE]\n  error failed blocked\n[/TOOL_RESPONSE]\n[/TOOL]\n'
            b'[TOOL] {"id":2,"tool":"bash","status":"error","isError":true}\n'
            b'[TOOL_RESPONSE]\n  no\n[/TOOL_RESPONSE]\n[/TOOL]\n', final=True)
        output = stream.getvalue()
        first, second = output.split('\x1b[2m\x1b[3m[TOOL]', 2)[1:]
        self.assertNotIn(watcher.ANSI_ERROR, first)
        self.assertIn(watcher.ANSI_RESPONSE, first)
        self.assertIn(watcher.ANSI_ERROR, second)

    def test_semantic_formatter_styles_thinking_input_answer_and_strips_embedded_ansi(self):
        watcher = load_watcher()
        stream = io.StringIO()
        formatter = watcher.SemanticFollowFormatter(stream, color=True)
        formatter.feed(
            b'[THINKING] {"id":1,"time":"12:00:00"}\n  think\n[/THINKING : 0.10s]\n'
            b'[TOOL] {"id":2,"tool":"bash","status":"running"}\n[INPUT]\n  \x1b[31mecho hi\x1b[0m\n[/INPUT]\n[/TOOL]\n'
            b'[ANSWER] {"id":3,"time":"12:00:01"}\n  yes\n[/ANSWER]\n', final=True)
        output = stream.getvalue()
        self.assertIn(watcher.ANSI_DIM_ITALIC + "  think", output)
        self.assertIn(watcher.ANSI_INPUT + "  echo hi", output)
        self.assertIn(watcher.ANSI_ANSWER + "  yes", output)
        self.assertNotIn("\x1b[31m", output)

    def test_malformed_unknown_and_arbitrary_logs_pass_through(self):
        watcher = load_watcher()
        raw = "ordinary error text\n[UNKNOWN] x\n[TOOL] not-json\n"
        stream = io.StringIO()
        watcher.SemanticFollowFormatter(stream, color=True).feed(raw.encode(), final=True)
        self.assertEqual(stream.getvalue(), raw)

    def test_follow_waits_for_strict_footer_and_interrupt_does_not_signal_producer(self):
        run_dir = self.write_run()
        (run_dir / "combined.log").write_text("waiting\n")
        (run_dir / "footer").write_text("malformed\n")
        producer = self.producer()
        follower = subprocess.Popen(self.follow_command(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(0.15)
        self.assertIsNone(follower.poll())
        follower.send_signal(signal.SIGINT)
        stdout, stderr = follower.communicate(timeout=2)
        self.assertEqual(follower.returncode, 130, stderr)
        self.assertEqual(stdout, b"waiting\n")
        self.assertIsNone(producer.poll())

    def test_external_fixture_follow_preserves_multiline_semantics(self):
        fixture = (
            '[THINKING] {"id":1,"time":"12:00:00"}\n  plan\n[/THINKING : 0.10s]\n\n'
            '[TOOL] {"id":2,"tool":"bash","status":"done","duration":"0.08s"}\n'
            '[INPUT]\n  printf a\n  printf b\n[/INPUT]\n'
            '[TOOL_RESPONSE]\n  a\n  b\n[/TOOL_RESPONSE]\n[/TOOL]\n\n'
            '[ANSWER] {"id":3,"time":"12:00:01"}\n  done\n[/ANSWER]\n'
        )
        run_dir = self.write_run()
        (run_dir / "combined.log").write_text(fixture)
        (run_dir / "footer").write_bytes(VALID_FOOTER.replace(b"exit_code=7", b"exit_code=0"))
        followed = subprocess.run(self.follow_command(), capture_output=True, text=True, timeout=2)
        self.assertEqual(followed.returncode, 0, followed.stderr)
        self.assertEqual(followed.stdout, fixture)

    def test_follow_large_log_preserves_utf8_across_bounded_chunks(self):
        run_dir = self.write_run()
        content = ("x" * (65536 - 1) + "é" + "y" * 70000 + "\n").encode()
        (run_dir / "combined.log").write_bytes(content)
        (run_dir / "footer").write_bytes(VALID_FOOTER)
        result = subprocess.run(self.follow_command(), capture_output=True, timeout=3)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout, content)

    def test_retired_execution_refuses_without_creating_or_launching(self):
        absent = self.root / "absent"
        marker = self.root / "launched"
        for operation in ("exec", "_produce"):
            result = subprocess.run([sys.executable, str(SCRIPT), operation,
                "--root", str(absent), "--", sys.executable, "-c",
                f"from pathlib import Path; Path({str(marker)!r}).touch()"],
                capture_output=True, timeout=2)
            self.assertEqual(result.returncode, 2)
            self.assertIn(b"execution is retired", result.stderr)
            self.assertFalse(absent.exists())
            self.assertFalse(marker.exists())

    def test_missing_observation_does_not_create_root(self):
        absent = self.root / "absent"
        for operation in ("status", "await", "follow"):
            result = subprocess.run([sys.executable, str(SCRIPT), operation,
                "--root", str(absent), "run-1"], capture_output=True, timeout=2)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(absent.exists())

    def test_observation_preserves_bytes_and_never_claims_task_completion(self):
        run_dir = self.write_run()
        metadata = run_dir / "run.json"
        record = json.loads(metadata.read_text())
        metadata.write_text(json.dumps({**record, "state": "COMPLETED", "exit_code": 0}))
        before = {p.name: p.read_bytes() for p in run_dir.iterdir()}
        command = [sys.executable, str(SCRIPT), "status", "--root", str(self.root), "run-1"]
        result = subprocess.run(command, capture_output=True, timeout=2)
        observed = json.loads(result.stdout)
        self.assertEqual(observed["state"], "UNKNOWN")
        self.assertIsNone(observed["exit_code"])
        self.assertEqual(observed["task_completion"], "not_evaluated")
        self.assertEqual(before, {p.name: p.read_bytes() for p in run_dir.iterdir()})
        (run_dir / "footer").write_bytes(VALID_FOOTER)
        observed = json.loads(subprocess.check_output(command))
        self.assertEqual(observed["exit_code"], 7)
        self.assertEqual(observed["task_completion"], "not_evaluated")
        self.assertEqual(observed["semantic_outcome"], "not_evaluated")
        result = subprocess.run([sys.executable, str(SCRIPT), "await", "--root", str(self.root), "run-1"],
                                capture_output=True, timeout=2)
        self.assertEqual(result.returncode, 7)

    def test_follow_dead_producer_fails_instead_of_hanging(self):
        run_dir = self.write_run()
        (run_dir / "pid").write_text("999999999\n")
        result = subprocess.run(self.follow_command(), capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"execution outcome unknown", result.stderr)

    def test_await_interrupt_does_not_signal_producer_or_mutate_evidence(self):
        run_dir = self.write_run()
        producer = self.producer()
        (run_dir / "pid").write_text(str(producer.pid) + "\n")
        before = {p.name: p.read_bytes() for p in run_dir.iterdir()}
        observer = subprocess.Popen([sys.executable, str(SCRIPT), "await", "--root", str(self.root), "run-1"],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(0.15)
        observer.send_signal(signal.SIGINT)
        observer.communicate(timeout=2)
        self.assertEqual(observer.returncode, 130)
        self.assertIsNone(producer.poll())
        self.assertEqual(before, {p.name: p.read_bytes() for p in run_dir.iterdir()})

    def test_documented_private_run_directories_are_concurrently_isolated(self):
        command = 'mktemp -d "${TMPDIR:-/tmp}/yy-TASK_ID-run.XXXXXX"'
        first = subprocess.check_output(["sh", "-c", command], text=True).strip()
        second = subprocess.check_output(["sh", "-c", command], text=True).strip()
        try:
            self.assertNotEqual(first, second)
            self.assertEqual(os.stat(first).st_mode & 0o077, 0)
            self.assertEqual(os.stat(second).st_mode & 0o077, 0)
        finally:
            os.rmdir(first)
            os.rmdir(second)


if __name__ == "__main__":
    unittest.main()
