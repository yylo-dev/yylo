#!/usr/bin/env python3
"""Structured, supported runner for the task-workspace contract suite."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

SCHEMA = "juno.task_workspace.python_profile.v1"
WEIGHTS_SCHEMA = "juno.task_workspace.duration_weights.v1"


def balanced_shards(test_ids, weights, shard_count):
    """Deterministic longest-processing-time assignment with stable ties."""
    if shard_count < 1:
        raise ValueError("shard_count must be positive")
    shards = [[] for _ in range(shard_count)]
    loads = [0.0 for _ in range(shard_count)]
    for test_id in sorted(test_ids, key=lambda value: (-float(weights.get(value, 1000.0)), value)):
        index = min(range(shard_count), key=lambda candidate: (loads[candidate], candidate))
        shards[index].append(test_id)
        loads[index] += float(weights.get(test_id, 1000.0))
    for shard in shards:
        shard.sort()
    return shards


def load_duration_weights(path):
    if not path:
        return {}, None
    value = json.loads(Path(path).read_text())
    if value.get("schema_version") != WEIGHTS_SCHEMA or not isinstance(value.get("tests"), dict):
        raise ValueError("invalid task-workspace duration weights")
    weights = value["tests"]
    if any(not isinstance(name, str) or not isinstance(duration, (int, float)) or duration < 0
           for name, duration in weights.items()):
        raise ValueError("invalid task-workspace duration weight entry")
    return weights, value.get("source_receipt_sha256")


def load_tests_module(path: Path):
    spec = importlib.util.spec_from_file_location("yylo_task_workspace_contract_tests", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load task-workspace tests: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


class ProfileResult(unittest.TextTestResult):
    def __init__(self, *args, metrics, tier_for, process_metrics, **kwargs):
        super().__init__(*args, **kwargs)
        self.metrics = metrics
        self.tier_for = tier_for
        self.process_metrics = process_metrics
        self.started = {}
        self.fixture_started = {}

    def startTest(self, test):
        self.started[test.id()] = time.monotonic()
        self.process_metrics["current"] = {"argv": [], "count": 0, "git": 0}
        super().startTest(test)

    def stopTest(self, test):
        duration = (time.monotonic() - self.started.pop(test.id())) * 1000
        short = ".".join(test.id().split(".")[-2:])
        processes = self.process_metrics.pop("current", {"argv": [], "count": 0, "git": 0})
        outcome = getattr(test, "_yylo_pending_outcome", "passed")
        argv_identity = hashlib.sha256(json.dumps(
            processes["argv"], sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        commands = {}
        for argv in processes["argv"]:
            executable = Path(argv[0]).name if argv else "unknown"
            command = executable
            if executable == "git":
                arguments = iter(argv[1:])
                for argument in arguments:
                    if argument == "-C":
                        next(arguments, None)
                    elif not argument.startswith("-"):
                        command = f"git:{argument}"
                        break
            commands[command] = commands.get(command, 0) + 1
        output_identity = hashlib.sha256(json.dumps(
            {"id": short, "outcome": outcome}, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        self.metrics.append({
            "id": short,
            "tier": self.tier_for(short),
            "fixture_ms": round(float(getattr(test, "_yylo_fixture_ms", 0.0)), 3),
            "execution_ms": round(max(0.0, duration - float(getattr(test, "_yylo_fixture_ms", 0.0))), 3),
            "wall_ms": round(duration, 3),
            "git_processes": processes["git"],
            "subprocess_processes": processes["count"],
            "process_argv_identity": argv_identity,
            "process_commands": dict(sorted(commands.items())),
            "output_identity": output_identity,
            "outcome": outcome,
        })
        super().stopTest(test)

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._outcome(test, "failed")

    def addError(self, test, err):
        super().addError(test, err)
        self._outcome(test, "error")

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._outcome(test, "skipped")

    def _outcome(self, test, outcome):
        short = ".".join(test.id().split(".")[-2:])
        for row in reversed(self.metrics):
            if row["id"] == short:
                row["outcome"] = outcome
                return
        setattr(test, "_yylo_pending_outcome", outcome)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tests", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--mode", choices=("affected", "seeded", "hermetic", "complete"), required=True)
    parser.add_argument("--test-id", action="append", default=[])
    parser.add_argument("--changed-path", action="append", default=[])
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--duration-weights")
    args = parser.parse_args()

    tests_path = Path(args.tests).resolve()
    module = load_tests_module(tests_path)
    tier_for = getattr(
        module, "fixture_tier_for",
        lambda test_id: "pure" if test_id.split(".", 1)[0] in {
            "SemVerValidationTests", "ValidationProfilesRoundTripTests",
            "MinimumRcLifecycleContractTests",
        } else "hermetic",
    )
    loader = unittest.defaultTestLoader
    discovered = list(flatten(loader.loadTestsFromModule(module)))
    inventory = [".".join(test.id().split(".")[-2:]) for test in discovered]
    if args.shard_count < 1 or not 0 <= args.shard_index < args.shard_count:
        parser.error("invalid shard")
    selected = []
    wanted = set(args.test_id)
    if args.mode == "affected" and not wanted:
        changed = args.changed_path or ["juno-code/src/templates/scripts/task_workspace.py"]
        wanted = set(getattr(module, "affected_fixture_tests")(changed))
    for test in discovered:
        short = ".".join(test.id().split(".")[-2:])
        tier = tier_for(short)
        if wanted and short not in wanted:
            continue
        if args.mode == "seeded" and tier not in {"pure", "seeded-repository", "seeded-controller", "seeded-history"}:
            continue
        if args.mode == "hermetic" and tier not in {"hermetic", "shared-resource"}:
            continue
        selected.append(test)
    weights, weights_identity = load_duration_weights(args.duration_weights)
    plan = balanced_shards(
        [".".join(test.id().split(".")[-2:]) for test in selected],
        weights, args.shard_count,
    )
    selected_ids = set(plan[args.shard_index])
    selected = [test for test in selected
                if ".".join(test.id().split(".")[-2:]) in selected_ids]
    predicted_ms = sum(float(weights.get(test_id, 1000.0))
                       for test_id in plan[args.shard_index])

    git_count = {"value": 0}
    original_git = getattr(module, "git", None)
    if original_git is not None:
        def measured_git(*values, **kwargs):
            git_count["value"] += 1
            return original_git(*values, **kwargs)
        module.git = measured_git

    fixture = getattr(module, "TaskWorkspaceFixture", None)
    if fixture is not None:
        original_setup = fixture.setUp
        def measured_setup(self):
            before_git = git_count["value"]
            started = time.monotonic()
            try:
                return original_setup(self)
            finally:
                self._yylo_fixture_ms = (time.monotonic() - started) * 1000
                self._yylo_git_processes = git_count["value"] - before_git
        fixture.setUp = measured_setup

    metrics = []
    process_metrics = {}
    process_lock = threading.Lock()
    original_popen = subprocess.Popen

    def measured_popen(argv, *values, **kwargs):
        with process_lock:
            current = process_metrics.get("current")
            if current is not None:
                normalized = [str(item) for item in argv] if isinstance(argv, (list, tuple)) else [str(argv)]
                current["argv"].append(normalized)
                current["count"] += 1
                if normalized and Path(normalized[0]).name == "git":
                    current["git"] += 1
        return original_popen(argv, *values, **kwargs)

    subprocess.Popen = measured_popen
    stream = sys.stderr
    runner = unittest.TextTestRunner(
        stream=stream, verbosity=1,
        resultclass=lambda *a, **kw: ProfileResult(
            *a, metrics=metrics, tier_for=tier_for,
            process_metrics=process_metrics, **kw),
    )
    started = time.monotonic()
    try:
        result = runner.run(unittest.TestSuite(selected))
    finally:
        subprocess.Popen = original_popen
    wall_ms = round((time.monotonic() - started) * 1000, 3)
    payload = {
        "schema_version": SCHEMA,
        "mode": args.mode,
        "shard": {"index": args.shard_index, "count": args.shard_count,
                  "predicted_ms": round(predicted_ms, 3),
                  "weights_identity": weights_identity},
        "inventory": inventory,
        "selected": [".".join(test.id().split(".")[-2:]) for test in selected],
        "tests": metrics,
        "wall_ms": wall_ms,
        "success": result.wasSuccessful(),
        "counts": {
            "inventory": len(inventory), "selected": len(selected),
            "failures": len(result.failures), "errors": len(result.errors),
            "skipped": len(result.skipped), "git_processes": git_count["value"],
        },
    }
    Path(args.out).write_text(json.dumps(payload, sort_keys=True) + "\n")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
