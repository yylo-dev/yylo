"""YYLO exec-boundary diagnostics, not wrapper performance acceptance.

Uses only the standard library, like the controller migration tests.
"""
import ast
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

SERVICES = Path(__file__).resolve().parents[1] / "src/templates/services"


def run_boundary(code, *, origin=None, enabled="1"):
    env = dict(os.environ, PYTHONPATH=str(SERVICES), YYLO_STARTUP_TIMING=enabled,
               YYLO_STARTUP_WRAPPER_EPOCH=origin if origin is not None else f"{time.time():.6f}")
    return subprocess.run([sys.executable, "-B", "-c", code], env=env,
                          text=True, capture_output=True, timeout=10, check=True)


class HarnessHandoffTests(unittest.TestCase):
    def test_successful_exec_is_observed_without_sensitive_payload(self):
        for harness in ("pi", "claude", "codex", "gemini"):
            with self.subTest(harness=harness):
                result = run_boundary(
                    "import environment_boundary as b, subprocess, sys, os; "
                    "p = subprocess.Popen([sys.executable, '-c', 'pass', 'secret [prompt];!', '--resume', 'private-session']); "
                    f"b.record_harness_handoff({harness!r}); p.wait(); "
                    "print('origin-present' if 'YYLO_STARTUP_WRAPPER_EPOCH' in os.environ else 'origin-consumed')"
                )
                self.assertEqual(result.stdout, "origin-consumed\n")
                event = json.loads(result.stderr)
                self.assertEqual(event, {"event": "yylo_harness_handoff", "schema_version": 1,
                    "harness": harness, "launch_index": 1, "clock": "unix_wall",
                    "origin_precision_us": 1, "elapsed_ms": event["elapsed_ms"]})
                self.assertGreaterEqual(event["elapsed_ms"], 0)
                self.assertNotIn("secret", result.stderr)
                self.assertNotIn("private-session", result.stderr)

    def test_invalid_or_backwards_origins_do_not_claim_a_sample(self):
        for origin in ("", "NaN", "Infinity", "-1.0", "1e10", "1.1234567", "999999999999.0"):
            with self.subTest(origin=origin):
                result = run_boundary("import environment_boundary as b; b.record_harness_handoff('pi')", origin=origin)
                self.assertEqual(result.stdout + result.stderr, "")

    def test_service_progress_stops_at_handoff_even_without_timing(self):
        result = run_boundary("""
import os, time
os.environ['YYLO_STARTUP_PROGRESS'] = '1'
import environment_boundary as b
assert 'YYLO_STARTUP_PROGRESS' not in os.environ
time.sleep(4.1)
b.record_harness_handoff('pi')
time.sleep(4.1)
print('finished')
""", enabled='0')
        self.assertEqual(result.stdout, 'finished\n')
        self.assertEqual(result.stderr.splitlines(), ['YYLO: Preparing requested harness executable…'] * 2)

    def test_disabled_diagnostics_are_silent(self):
        result = run_boundary("import environment_boundary as b; b.record_harness_handoff('pi')", enabled="0")
        self.assertEqual(result.stdout + result.stderr, "")

    def test_failed_exec_never_reports_handoff(self):
        result = run_boundary("""
import environment_boundary as b, subprocess
try:
    subprocess.Popen(['/nonexistent/yylo-test-harness'])
    b.record_harness_handoff('pi')
except FileNotFoundError:
    print('original exec error')
""")
        self.assertEqual(result.stdout, "original exec error\n")
        self.assertEqual(result.stderr, "")

    def test_launches_distinguishable_and_broken_diagnostics_optional(self):
        result = run_boundary("""
import environment_boundary as b, sys
b.record_harness_handoff('pi')
b.record_harness_handoff('pi')
class Broken:
    def write(self, value):
        raise BrokenPipeError('closed diagnostic pipe')
sys.stderr = Broken()
b.record_harness_handoff('pi')
print('launch preserved')
sys.stderr = sys.__stderr__
""")
        self.assertEqual([json.loads(line)["launch_index"] for line in result.stderr.splitlines()], [1, 2])
        self.assertEqual(result.stdout, "launch preserved\n")

    def test_provider_environment_filters_origin_even_with_overrides(self):
        result = run_boundary("""
import environment_boundary as b, json
print(json.dumps(b.child_process_environment({'KEEP': 'yes'}, {'YYLO_STARTUP_WRAPPER_EPOCH': '1.0'})))
""")
        self.assertEqual(json.loads(result.stdout), {"KEEP": "yes"})

    def test_public_wrapper_replaces_ambient_origin_without_subprocess(self):
        wrapper = (SERVICES.parents[1] / "bin/yylo.sh").read_text()
        # Literal entry prefix only, not an end-to-end wrapper fixture.
        entry = wrapper.split('# Bounded 0.1 migration:', 1)[0]
        result = subprocess.run(['bash', '-c', entry + '\nprintf "%s" "$YYLO_STARTUP_WRAPPER_EPOCH"'],
                                env=dict(os.environ, YYLO_STARTUP_WRAPPER_EPOCH='1.0'),
                                text=True, capture_output=True, check=True)
        self.assertGreater(float(result.stdout), 1_000_000_000)

    def test_service_marks_harness_popen_not_version_probe(self):
        # Static wiring complements runtime helper tests; not harness correctness.
        for harness in ('pi', 'claude', 'codex', 'gemini'):
            with self.subTest(harness=harness):
                tree = ast.parse((SERVICES / f'{harness}.py').read_text())
                calls = 0
                for node in ast.walk(tree):
                    for field in ('body', 'orelse', 'finalbody'):
                        statements = getattr(node, field, [])
                        if not isinstance(statements, list):
                            continue
                        for index, statement in enumerate(statements):
                            if (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)
                                    and isinstance(statement.value.func, ast.Name)
                                    and statement.value.func.id == 'record_harness_handoff'):
                                previous = statements[index - 1]
                                self.assertIsInstance(previous, ast.Assign)
                                self.assertIsInstance(previous.value, ast.Call)
                                self.assertIsInstance(previous.value.func, ast.Attribute)
                                self.assertEqual(previous.value.func.attr, 'Popen')
                                self.assertEqual(statement.value.args[0].value, harness)
                                calls += 1
                self.assertEqual(calls, 2 if harness == 'pi' else 1)


if __name__ == '__main__':
    unittest.main()
