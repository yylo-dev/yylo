#!/usr/bin/env python3
"""Content-addressed operation snapshots and phase-local lifecycle read sets.

The snapshot deliberately has no controller checkout, branch, HEAD, or status
identity.  Only explicitly admitted bytes may invalidate an active operation.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

SNAPSHOT_SCHEMA = "juno_operation_snapshot.v1"
READ_SET_SCHEMA = "juno_operation_read_set.v1"
MAX_ATTRIBUTION_ROWS = 32
MAX_READ_SET_UNITS = 256
MAX_PATHS_PER_UNIT = 1024
REQUIRED_PHASES = frozenset({
    "validation", "risk", "review", "documentation", "integration",
})
PHASE_ORDER = {name: index for index, name in enumerate(
    ("validation", "risk", "documentation", "review", "integration"))}
OID_RE = re.compile(r"[0-9a-f]{40,64}")
SUBMISSION_FIELDS = frozenset({
    "task_id", "requirements_sha256", "admitted_scope_sha256",
    "generated_scope_sha256", "base_sha", "tip_sha", "tree_sha",
    "origin_projection_sha256", "hydration_sha256", "dependency_sha256",
    "runtime_sha256", "validation_sha256", "risk_sha256",
})


class OperationSnapshotError(RuntimeError):
    """The admitted operation closure is incomplete, ambiguous, or unsafe."""


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise OperationSnapshotError("operation snapshot contains non-canonical input") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _file_digest(path: Path) -> str:
    value = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                value.update(chunk)
    except OSError as exc:
        raise OperationSnapshotError(f"read-set input is missing or unreadable: {path.name}") from exc
    return value.hexdigest()


def _relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise OperationSnapshotError("unsafe read-set path")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or any(part in {"", ".", ".."} for part in path.parts):
        raise OperationSnapshotError(f"unsafe read-set path: {value}")
    return value


def _bounded_strings(value: Mapping[str, Any], label: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, item in value.items():
        if (not isinstance(key, str) or not key or not isinstance(item, str)
                or len(key.encode()) > 256 or len(item.encode()) > 4096):
            raise OperationSnapshotError(f"{label} is malformed or unbounded")
        result[key] = item
    return dict(sorted(result.items()))


def _commands(values: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    identity_fields = {"id", "cwd", "argv", "timeout_seconds", "max_output_bytes", "resource"}
    # Planner input scopes are represented by each validation unit's exact
    # input-closure identity. They are not execution-command identity and must
    # not leak into the canonical command row, while truly unknown fields still
    # fail closed.
    planner_fields = {"input_paths"}
    for value in values:
        if not isinstance(value, Mapping) or set(value) - identity_fields - planner_fields:
            raise OperationSnapshotError("normalized command is malformed or contains unknown fields")
        command_id, argv = value.get("id"), value.get("argv")
        if (not isinstance(command_id, str) or not command_id or command_id in seen
                or not isinstance(argv, list) or not argv
                or any(not isinstance(arg, str) or len(arg.encode()) > 16384 for arg in argv)):
            raise OperationSnapshotError("normalized command identity or argv is malformed")
        cwd = value.get("cwd", ".")
        if cwd != ".":
            _relative_path(cwd)
        row = {key: value[key] for key in sorted(value) if key in identity_fields}
        _canonical(row)
        rows.append(row); seen.add(command_id)
    return sorted(rows, key=lambda row: row["id"])


def _submission(value: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != SUBMISSION_FIELDS:
        raise OperationSnapshotError("immutable task submission is incomplete or contains unknown fields")
    normalized = _bounded_strings(value, "immutable task submission")
    if (not OID_RE.fullmatch(normalized["base_sha"])
            or not OID_RE.fullmatch(normalized["tip_sha"])
            or not OID_RE.fullmatch(normalized["tree_sha"])
            or any(not re.fullmatch(r"[0-9a-f]{64}", normalized[field])
                   for field in SUBMISSION_FIELDS - {"task_id", "base_sha", "tip_sha", "tree_sha"})):
        raise OperationSnapshotError("immutable task submission identity is malformed")
    return normalized


def compile_operation_snapshot(*, root: Path, candidate: str, target: str,
                               commands: Iterable[Mapping[str, Any]],
                               routing: Mapping[str, Any], environment: Mapping[str, Any],
                               read_sets: Iterable[Mapping[str, Any]],
                               managed_outputs: Mapping[str, Any], discovery: Mapping[str, Any],
                               submission: Mapping[str, Any],
                               observed_controller_head: str | None = None,
                               controller_status: Iterable[str] | None = None) -> dict[str, Any]:
    """Compile exact admitted operation bytes; checkout observations are ignored.

    ``observed_controller_head`` and ``controller_status`` are accepted only as
    migration diagnostics.  Their deliberate exclusion prevents unrelated
    controller movement from becoming operation authority.
    """
    del observed_controller_head, controller_status
    root = Path(root).resolve()
    if not root.is_dir():
        raise OperationSnapshotError("operation snapshot root is missing")
    if not OID_RE.fullmatch(candidate) or not OID_RE.fullmatch(target):
        raise OperationSnapshotError("candidate or target identity is malformed")
    if (not isinstance(discovery, Mapping) or discovery.get("complete") is not True
            or discovery.get("kind") != "exact-import-closure"):
        reason = str(discovery.get("reason", "unknown"))[:128] if isinstance(discovery, Mapping) else "malformed"
        raise OperationSnapshotError(f"read set discovery is incomplete or ambiguous: {reason}")
    normalized_commands = _commands(commands)
    command_ids = {row["id"] for row in normalized_commands}
    normalized_routing = dict(sorted(routing.items()))
    if (set(normalized_routing) != command_ids
            or any(not isinstance(value, str) or not value for value in normalized_routing.values())):
        raise OperationSnapshotError("validation routing is incomplete or ambiguous")
    normalized_environment = _bounded_strings(environment, "admitted environment")
    normalized_managed = _bounded_strings(managed_outputs, "managed-output identity")

    units: list[dict[str, Any]] = []
    identities: set[tuple[str, str]] = set()
    phases: set[str] = set()
    for raw in read_sets:
        if (not isinstance(raw, Mapping) or set(raw) != {"phase", "id", "paths"}
                or raw.get("phase") not in REQUIRED_PHASES
                or not isinstance(raw.get("id"), str) or not raw["id"]
                or not isinstance(raw.get("paths"), list) or not raw["paths"]):
            raise OperationSnapshotError("read set is missing, dynamic, or malformed")
        phase, unit_id = raw["phase"], raw["id"]
        identity = (phase, unit_id)
        if identity in identities:
            raise OperationSnapshotError("read set identity is ambiguous")
        paths = sorted({_relative_path(path) for path in raw["paths"]})
        if len(paths) != len(raw["paths"]) or len(paths) > MAX_PATHS_PER_UNIT:
            raise OperationSnapshotError("read set paths are duplicate or unbounded")
        inputs: dict[str, str] = {}
        for relative in paths:
            lexical = root / relative
            path = lexical.resolve()
            try:
                path.relative_to(root)
            except ValueError as exc:
                raise OperationSnapshotError(f"unsafe read-set path: {relative}") from exc
            cursor = root
            symlinked = False
            for part in PurePosixPath(relative).parts:
                cursor = cursor / part
                if cursor.is_symlink():
                    symlinked = True
                    break
            if symlinked or not path.is_file():
                raise OperationSnapshotError(f"read-set input is missing, dynamic, or ambiguous: {relative}")
            inputs[relative] = _file_digest(path)
        unit = {"schema_version": READ_SET_SCHEMA, "phase": phase,
                "unit_id": unit_id, "inputs": inputs}
        unit["read_set_sha256"] = _digest(unit)
        units.append(unit); identities.add(identity); phases.add(phase)
    if len(units) > MAX_READ_SET_UNITS or phases != REQUIRED_PHASES:
        raise OperationSnapshotError("read set discovery does not cover every dependent phase")
    units.sort(key=lambda row: (PHASE_ORDER[row["phase"]], row["unit_id"]))
    body = {
        "schema_version": SNAPSHOT_SCHEMA,
        "candidate_sha": candidate,
        "target_sha": target,
        "commands": normalized_commands,
        "routing": normalized_routing,
        "environment": normalized_environment,
        "discovery": {"complete": True, "kind": "exact-import-closure"},
        "managed_outputs": normalized_managed,
        "submission": _submission(submission),
        "read_sets": units,
    }
    body["submission_sha256"] = _digest(body["submission"])
    return {**body, "snapshot_sha256": _digest(body)}


def verify_operation_snapshot(snapshot: Any) -> dict[str, Any]:
    reasons: list[dict[str, str]] = []
    if not isinstance(snapshot, dict):
        return {"valid": False, "reasons": [{"code": "SNAPSHOT_MALFORMED"}]}
    body = {key: value for key, value in snapshot.items() if key != "snapshot_sha256"}
    if snapshot.get("schema_version") != SNAPSHOT_SCHEMA:
        reasons.append({"code": "SNAPSHOT_SCHEMA_UNSUPPORTED"})
    if snapshot.get("snapshot_sha256") != _digest(body):
        reasons.append({"code": "SNAPSHOT_DIGEST_MISMATCH"})
    submission = snapshot.get("submission")
    # Finite compatibility reader for v1 snapshots produced before immutable
    # submissions were added. New compilers always emit the bound submission;
    # a partially present or malformed addition is never interpreted as legacy.
    legacy_submission = "submission" not in snapshot and "submission_sha256" not in snapshot
    if (not legacy_submission
            and (not isinstance(submission, dict) or set(submission) != SUBMISSION_FIELDS
                 or snapshot.get("submission_sha256") != _digest(submission))):
        reasons.append({"code": "SUBMISSION_MISSING_OR_TAMPERED"})
    units = snapshot.get("read_sets")
    if not isinstance(units, list):
        reasons.append({"code": "READ_SETS_MISSING"})
    else:
        for unit in units:
            if not isinstance(unit, dict):
                reasons.append({"code": "READ_SET_MALFORMED"}); continue
            unit_body = {key: value for key, value in unit.items() if key != "read_set_sha256"}
            if (unit.get("schema_version") != READ_SET_SCHEMA
                    or unit.get("read_set_sha256") != _digest(unit_body)):
                reasons.append({"code": "READ_SET_TAMPERED", "unit_id": str(unit.get("unit_id", ""))[:128]})
            if len(reasons) >= MAX_ATTRIBUTION_ROWS:
                break
    return {"valid": not reasons, "reasons": reasons[:MAX_ATTRIBUTION_ROWS]}


def write_operation_snapshot(path: Path, snapshot: dict[str, Any]) -> None:
    """Persist one immutable snapshot without overwrite or in-place mutation."""
    verification = verify_operation_snapshot(snapshot)
    if not verification["valid"]:
        raise OperationSnapshotError("refusing to persist an invalid operation snapshot")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _canonical(snapshot) + b"\n"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise OperationSnapshotError("operation snapshot already exists") from exc
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload); stream.flush(); os.fsync(stream.fileno())
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def verify_phase_read_set(root: Path, snapshot: Any, phase: str,
                          unit_id: str) -> dict[str, Any]:
    """Verify live phase bytes immediately before a dependent phase executes."""
    verification = verify_operation_snapshot(snapshot)
    if not verification["valid"]:
        return {"valid": False, "phase": phase, "unit_id": unit_id,
                "reasons": verification["reasons"][:MAX_ATTRIBUTION_ROWS]}
    unit = next((row for row in snapshot["read_sets"]
                 if row["phase"] == phase and row["unit_id"] == unit_id), None)
    if unit is None:
        return {"valid": False, "phase": phase, "unit_id": unit_id,
                "reasons": [{"code": "READ_SET_UNKNOWN"}]}
    root = Path(root).resolve()
    reasons: list[dict[str, str]] = []
    for relative, expected in unit["inputs"].items():
        try:
            safe_relative = _relative_path(relative)
            cursor = root
            symlinked = False
            for part in PurePosixPath(safe_relative).parts:
                cursor = cursor / part
                if cursor.is_symlink():
                    symlinked = True
                    break
            resolved = (root / safe_relative).resolve()
            resolved.relative_to(root)
            observed = "ambiguous" if symlinked else _file_digest(resolved)
        except (OperationSnapshotError, ValueError):
            observed = "missing"
        if observed != expected:
            reasons.append({"code": "READ_SET_INPUT_DRIFT", "path": relative,
                            "expected_sha256": expected, "observed_sha256": observed})
        if len(reasons) >= MAX_ATTRIBUTION_ROWS:
            break
    return {"valid": not reasons, "phase": phase, "unit_id": unit_id,
            "reasons": reasons}


def _units(snapshot: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(row["phase"], row["unit_id"]): row for row in snapshot["read_sets"]}


def phase_invalidation(previous: Any, current: Any) -> list[dict[str, Any]]:
    """Attribute drift to the smallest phase/unit restart set, failing closed."""
    previous_check = verify_operation_snapshot(previous)
    current_check = verify_operation_snapshot(current)
    if not previous_check["valid"] or not current_check["valid"]:
        reasons = (previous_check["reasons"] + current_check["reasons"])[:MAX_ATTRIBUTION_ROWS]
        return [{"phase": "validation", "unit_id": "unknown", "reason": "snapshot_invalid",
                 "attribution": reasons}]
    if previous["candidate_sha"] != current["candidate_sha"]:
        return [{"phase": "validation", "unit_id": "all", "reason": "candidate_drift"}]
    target_drift = previous["target_sha"] != current["target_sha"]
    old, new = _units(previous), _units(current)
    rows: list[dict[str, Any]] = []
    for identity in sorted(set(old) | set(new), key=lambda item: (PHASE_ORDER.get(item[0], 99), item[1])):
        before, after = old.get(identity), new.get(identity)
        if before is None or after is None:
            rows.append({"phase": identity[0], "unit_id": identity[1], "reason": "read_set_membership_drift"})
            continue
        if before["read_set_sha256"] != after["read_set_sha256"]:
            changed = sorted(path for path in set(before["inputs"]) | set(after["inputs"])
                             if before["inputs"].get(path) != after["inputs"].get(path))
            rows.append({"phase": identity[0], "unit_id": identity[1], "reason": "read_set_drift",
                         "changed_paths": changed[:MAX_ATTRIBUTION_ROWS]})
    old_commands = {row["id"]: row for row in previous["commands"]}
    new_commands = {row["id"]: row for row in current["commands"]}
    for command_id in sorted(set(old_commands) | set(new_commands)):
        if (old_commands.get(command_id) != new_commands.get(command_id)
                or previous["routing"].get(command_id) != current["routing"].get(command_id)):
            marker = ("validation", command_id)
            if not any((row["phase"], row["unit_id"]) == marker for row in rows):
                rows.append({"phase": "validation", "unit_id": command_id,
                             "reason": "command_or_routing_drift"})
    if previous["environment"] != current["environment"]:
        rows.append({"phase": "validation", "unit_id": "all", "reason": "environment_drift"})
    if previous["managed_outputs"] != current["managed_outputs"]:
        rows.append({"phase": "integration", "unit_id": "managed-outputs", "reason": "managed_output_drift"})
    if previous.get("submission_sha256") != current.get("submission_sha256"):
        before = previous.get("submission", {})
        after = current.get("submission", {})
        changed = sorted(key for key in SUBMISSION_FIELDS if before.get(key) != after.get(key))
        phase = ("validation" if set(changed) & {
            "admitted_scope_sha256", "generated_scope_sha256", "base_sha", "tip_sha",
            "tree_sha", "origin_projection_sha256", "hydration_sha256",
            "dependency_sha256", "runtime_sha256", "validation_sha256"} else "risk")
        rows.append({"phase": phase, "unit_id": "task-submission",
                     "reason": "submission_drift", "changed_fields": changed})
    if target_drift:
        rows.append({"phase": "integration", "unit_id": "live-target",
                     "reason": "target_drift"})
    if any(row["phase"] == "documentation" for row in rows) and not any(row["phase"] == "review" for row in rows):
        rows.append({"phase": "review", "unit_id": "documentation-dependent",
                     "reason": "active_documentation_drift"})
    rows.sort(key=lambda row: (PHASE_ORDER.get(row["phase"], 99), row["unit_id"]))
    return rows[:MAX_ATTRIBUTION_ROWS]


def compile_identity_operation_snapshot(*, candidate: str, target: str,
                                        commands: Iterable[Mapping[str, Any]],
                                        routing: Mapping[str, Any],
                                        environment: Mapping[str, Any],
                                        phase_units: Iterable[Mapping[str, Any]],
                                        managed_outputs: Mapping[str, Any],
                                        discovery: Mapping[str, Any],
                                        submission: Mapping[str, Any]) -> dict[str, Any]:
    """Compile a snapshot from already content-addressed lifecycle inputs.

    This adapter is for Git/runtime closures whose bytes were discovered by the
    lifecycle compiler rather than read from one checkout. Values are hashed
    once more with their semantic key so OIDs from different namespaces cannot
    alias. Unknown or partial discovery is rejected exactly like path snapshots.
    """
    if (not isinstance(discovery, Mapping) or discovery.get("complete") is not True
            or discovery.get("kind") != "exact-import-closure"):
        reason = str(discovery.get("reason", "unknown"))[:128] if isinstance(discovery, Mapping) else "malformed"
        raise OperationSnapshotError(f"read set discovery is incomplete or ambiguous: {reason}")
    normalized_commands = _commands(commands)
    command_ids = {row["id"] for row in normalized_commands}
    normalized_routing = dict(sorted(routing.items()))
    if (set(normalized_routing) != command_ids
            or any(not isinstance(value, str) or not value for value in normalized_routing.values())):
        raise OperationSnapshotError("validation routing is incomplete or ambiguous")
    units: list[dict[str, Any]] = []
    identities: set[tuple[str, str]] = set()
    phases: set[str] = set()
    for raw in phase_units:
        if (not isinstance(raw, Mapping) or set(raw) != {"phase", "id", "inputs"}
                or raw.get("phase") not in REQUIRED_PHASES
                or not isinstance(raw.get("id"), str) or not raw["id"]
                or not isinstance(raw.get("inputs"), Mapping) or not raw["inputs"]):
            raise OperationSnapshotError("read set is missing, dynamic, or malformed")
        identity = (str(raw["phase"]), str(raw["id"]))
        if identity in identities:
            raise OperationSnapshotError("read set identity is ambiguous")
        bounded = _bounded_strings(raw["inputs"], "read-set identity")
        if len(bounded) > MAX_PATHS_PER_UNIT:
            raise OperationSnapshotError("read set identities are unbounded")
        inputs = {key: _digest({"key": key, "identity": value})
                  for key, value in bounded.items()}
        unit = {"schema_version": READ_SET_SCHEMA, "phase": identity[0],
                "unit_id": identity[1], "inputs": inputs}
        unit["read_set_sha256"] = _digest(unit)
        units.append(unit); identities.add(identity); phases.add(identity[0])
    if len(units) > MAX_READ_SET_UNITS or phases != REQUIRED_PHASES:
        raise OperationSnapshotError("read set discovery does not cover every dependent phase")
    units.sort(key=lambda row: (PHASE_ORDER[row["phase"]], row["unit_id"]))
    body = {"schema_version": SNAPSHOT_SCHEMA, "candidate_sha": candidate,
            "target_sha": target, "commands": normalized_commands,
            "routing": normalized_routing,
            "environment": _bounded_strings(environment, "admitted environment"),
            "discovery": {"complete": True, "kind": "exact-import-closure"},
            "managed_outputs": _bounded_strings(managed_outputs, "managed-output identity"),
            "submission": _submission(submission), "read_sets": units}
    body["submission_sha256"] = _digest(body["submission"])
    if not OID_RE.fullmatch(candidate) or not OID_RE.fullmatch(target):
        raise OperationSnapshotError("candidate or target identity is malformed")
    return {**body, "snapshot_sha256": _digest(body)}
