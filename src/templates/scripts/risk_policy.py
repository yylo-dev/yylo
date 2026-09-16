#!/usr/bin/env python3
"""Deterministic Bolt candidate risk, review, and compact-evidence policy."""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
from typing import Any


SCHEMA = "juno_bolt_risk_policy.v1"
EVIDENCE_SCHEMA = "juno_bolt_candidate_evidence.v1"
RELEASE_SCHEMA = "juno_bolt_release_gate.v1"
RELEASE_AUTH_SCHEMA = "juno_bolt_release_authorization.v1"
RELEASE_PRODUCER_SCHEMA = "juno_bolt_release_gate_producer.v1"
RELEASE_TOOL_ID = "yylo.release-gate"
MANAGED_RUNNER_SCHEMA = "juno_managed_agent_runner.v1"
REVIEW_BINDING_SCHEMA = "juno_managed_review_binding.v1"
REVIEW_RESULT_SCHEMA = "juno_managed_review_result.v3"
REVIEW_REFERENCE_SCHEMA = "juno_queue_review_reference.v1"
FINDING_POLICY_REVISION = "yy_review_finding_policy.v2"
ADMITTED_SCOPE_CLASSIFICATIONS = {
    "requirement_gap", "candidate_bug", "candidate_regression",
    "safety_invariant_violation"}
REJECTED_OBSERVATION_CLASSES = {
    "enhancement", "scope_addition", "design_preference",
    "speculative_hardening", "unrelated_preexisting"}
MAX_REJECTED_OBSERVATIONS = 64
REVIEW_FINDING_IMPACT_CATEGORIES = {
    "bounded_product_defect", "maintainability", "clarity",
    "supported_install", "supported_runtime", "supported_config",
    "core_contract", "product_breaking", "security_privacy",
    "destructive_data_loss",
}
HIGH_IMPACTS = {"supported_install", "supported_runtime", "supported_config",
                "core_contract", "product_breaking"}
CRITICAL_IMPACTS = {"security_privacy", "destructive_data_loss"}
PLAN_PRODUCER_SCHEMA = "juno_bolt_risk_plan_producer.v1"
PLAN_TOOL_ID = "yylo.risk-policy"
EVIDENCE_PRODUCER_SCHEMA = "juno_bolt_candidate_evidence_producer.v1"
EVIDENCE_TOOL_ID = "yylo.risk-policy"
FULL_SUITE_SCHEMA = "juno_merge_queue_full_suite_receipt.v2"
VALIDATION_TIMING_SCHEMA = "juno_validation_timing.v1"
FULL_SUITE_PRODUCER_SCHEMA = "juno_merge_queue_full_suite_producer.v1"
FULL_SUITE_TOOL_ID = "yylo.merge-queue"
FULL_SUITE_CLAIM_SCHEMA = "juno_merge_queue_full_suite_claim.v1"
FULL_SUITE_ADMISSION_SCHEMA = "juno_merge_queue_full_suite_admission.v1"
FULL_SUITE_CLAIM_V2_SCHEMA = "juno_merge_queue_full_suite_claim.v2"
FULL_SUITE_RECEIPT_V3_SCHEMA = "juno_merge_queue_full_suite_receipt.v3"
FULL_SUITE_ADMISSION_V2_SCHEMA = "juno_merge_queue_full_suite_admission.v2"
FULL_SUITE_ROUTING_SCHEMA = "juno_merge_queue_full_suite_routing.v1"
IDENTITY_KEYS = {"task_workspace_config_sha256", "full_suite_config_sha256",
                 "task_validation_commands_sha256"}
IDENTITY_KEYS_ROUTED = IDENTITY_KEYS | {"validation_routing_sha256"}
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
ALLOWED_METRICS = {
    "model_calls", "tool_calls", "failed_calls", "wall_ms", "uncached_tokens",
    "cache_read_tokens", "affected_test_runs", "full_suite_runs", "build_runs",
}
TRANSCRIPT_KEYS = {"transcript", "stdout", "stderr", "prompt", "response", "messages"}
PLAN_KEYS = {
    "schema_version", "producer", "candidate", "policy_identity", "tier", "reasons", "changed_paths", "flags",
    "unknown_flags", "shared_infrastructure", "affected_validation_required",
    "full_suite_required", "reviewer_sequence", "min_reviews", "max_reviews",
    "finding_policy_revision", "release_gate_required", "post_cas", "evidence_limits",
}
EVIDENCE_KEYS = {
    "schema_version", "producer", "created_at", "status", "failure", "candidate", "policy",
    "validation", "reviews", "release_gate", "metrics", "post_cas",
    "semantic_evidence_reused",
}
SEMANTIC_SNAPSHOT_SCHEMA = "juno_semantic_input_snapshot.v1"
SEMANTIC_DECISION_CODES = {
    "hit", "cold_miss", "input_changed", "policy_changed", "runtime_changed",
    "authority_changed", "write_collision", "malformed_evidence", "closure_unknown",
}
SEMANTIC_SNAPSHOT_KEYS = {
    "schema_version", "input_identity", "policy_identity", "runtime_identity",
    "authority_identity", "review_prompt_identity", "closure_unknown",
    "write_collision", "executed",
}
SEMANTIC_LINEAGE_KEYS = {
    "receipt_path", "receipt_sha256", "candidate_sha", "snapshot_sha256",
}
MAX_SEMANTIC_CHANGED_INPUTS = 16


class RiskPolicyError(RuntimeError):
    pass


def canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _valid_semantic_snapshot(value: Any) -> bool:
    if (not isinstance(value, dict) or set(value) != SEMANTIC_SNAPSHOT_KEYS
            or value.get("schema_version") != SEMANTIC_SNAPSHOT_SCHEMA
            or not isinstance(value.get("input_identity"), dict)
            or len(value["input_identity"]) > 10000
            or any(not isinstance(path, str) or not path or len(path.encode()) > 1024
                   or not isinstance(mark, str) or not DIGEST_RE.fullmatch(mark)
                   for path, mark in value["input_identity"].items())
            or any(not isinstance(value.get(key), str) or not DIGEST_RE.fullmatch(value[key])
                   for key in ("policy_identity", "runtime_identity", "authority_identity",
                               "review_prompt_identity"))
            or not isinstance(value.get("closure_unknown"), bool)
            or not isinstance(value.get("write_collision"), bool)):
        return False
    executed = value.get("executed")
    return (isinstance(executed, dict) and set(executed) == {"commands", "wall_ms"}
            and all(isinstance(item, int) and not isinstance(item, bool) and item >= 0
                    for item in executed.values()))


def _valid_semantic_lineage(value: Any, snapshot: dict[str, Any]) -> bool:
    return (isinstance(value, dict) and set(value) == SEMANTIC_LINEAGE_KEYS
            and isinstance(value.get("receipt_path"), str) and bool(value["receipt_path"])
            and isinstance(value.get("receipt_sha256"), str)
            and DIGEST_RE.fullmatch(value["receipt_sha256"]) is not None
            and isinstance(value.get("candidate_sha"), str)
            and SHA_RE.fullmatch(value["candidate_sha"]) is not None
            and value.get("snapshot_sha256") == digest(snapshot))


def semantic_reuse_decision(previous: Any, current: Any, *,
                            max_changed_inputs: int = MAX_SEMANTIC_CHANGED_INPUTS) -> dict[str, Any]:
    """Classify semantic reuse without allowing diagnostics to grant authority.

    Inputs are canonical snapshot/receipt projections, not logs.  The returned
    metrics are recomputed from those immutable inputs on every call, so resume
    cannot accumulate or double-count derived evidence.
    """
    current_valid = _valid_semantic_snapshot(current)
    prior_snapshot = previous.get("snapshot") if isinstance(previous, dict) else None
    lineage = previous.get("source_lineage") if isinstance(previous, dict) else None
    malformed = (not current_valid or (previous is not None and
                 (set(previous) != {"snapshot", "source_lineage"}
                  or not _valid_semantic_snapshot(prior_snapshot)
                  or not _valid_semantic_lineage(lineage, prior_snapshot))))
    changed: list[dict[str, Any]] = []
    code, restart = "cold_miss", "tests"
    if malformed:
        code, restart = "malformed_evidence", "tests"
    elif current["write_collision"]:
        code, restart = "write_collision", "stop"
    elif current["closure_unknown"] or (prior_snapshot is not None
                                         and prior_snapshot["closure_unknown"]):
        code, restart = "closure_unknown", "tests"
    elif prior_snapshot is None:
        code, restart = "cold_miss", "tests"
    elif current["authority_identity"] != prior_snapshot["authority_identity"]:
        code, restart = "authority_changed", "stop"
    elif current["policy_identity"] != prior_snapshot["policy_identity"]:
        code, restart = "policy_changed", "tests"
    elif current["runtime_identity"] != prior_snapshot["runtime_identity"]:
        code, restart = "runtime_changed", "tests"
    else:
        keys = sorted(set(current["input_identity"]) | set(prior_snapshot["input_identity"]))
        changed = [{"field": key, "old": prior_snapshot["input_identity"].get(key),
                    "new": current["input_identity"].get(key)} for key in keys
                   if prior_snapshot["input_identity"].get(key)
                   != current["input_identity"].get(key)]
        prompt_changed = (current["review_prompt_identity"]
                          != prior_snapshot["review_prompt_identity"])
        if changed:
            code, restart = "input_changed", "tests"
        elif prompt_changed:
            changed = [{"field": "review_prompt_identity",
                        "old": prior_snapshot["review_prompt_identity"],
                        "new": current["review_prompt_identity"]}]
            code, restart = "input_changed", "review"
        else:
            code, restart = "hit", "none"
    bounded = changed[:max(0, max_changed_inputs)]
    hit = code == "hit"
    executed = (prior_snapshot["executed"] if hit else
                current.get("executed", {"commands": 0, "wall_ms": 0})
                if current_valid else {"commands": 0, "wall_ms": 0})
    metrics = {
        "executed_commands": 0 if hit or restart == "stop" else executed["commands"],
        "reused_commands": executed["commands"] if hit else 0,
        "avoided_commands": executed["commands"] if hit else 0,
        "wall_ms_avoided": executed["wall_ms"] if hit else 0,
        "phase_reruns": 1 if restart in {"tests", "review"} else 0,
        "whole_tree_fallbacks": 1 if code == "closure_unknown" else 0,
    }
    return {"schema_version": "juno_semantic_reuse_decision.v1", "code": code,
            "disposition": ("reused" if hit else "stopped" if restart == "stop"
                            else "unavailable" if code == "cold_miss" else "invalidated"),
            "restart_phase": restart, "rerun_tests": restart == "tests",
            "rerun_reviews": restart in {"tests", "review"}, "stop": restart == "stop",
            "changed_inputs": bounded,
            "omitted_changed_input_count": max(0, len(changed) - len(bounded)),
            "source_lineage": lineage if not malformed else None, "metrics": metrics}


def load_policy(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RiskPolicyError("risk policy is missing or malformed") from exc
    required = {
        "schema_version", "tiers", "low_risk_paths", "high_risk_paths",
        "shared_infrastructure_paths", "high_risk_flags", "release_flags",
        "review_policy", "full_suite_tiers", "post_cas_checks", "limits",
    }
    if not isinstance(value, dict) or set(value) != required or value["schema_version"] != SCHEMA:
        raise RiskPolicyError("risk policy schema is unsupported or ambiguous")
    if value["tiers"] != ["low", "normal", "high", "release"]:
        raise RiskPolicyError("risk tier order is unsupported")
    if value["review_policy"] != {
        "low": {"sequence": [], "min": 0, "max": 0},
        "normal": {"sequence": ["reviewer"], "min": 0, "max": 1},
        "high": {"sequence": ["reviewer_a", "reviewer_b"], "min": 2, "max": 2},
        "release": {"sequence": [], "min": 0, "max": 0},
    }:
        raise RiskPolicyError("review sequence violates Bolt policy")
    for key in (
        "low_risk_paths", "high_risk_paths", "shared_infrastructure_paths",
        "high_risk_flags", "release_flags", "full_suite_tiers", "post_cas_checks",
    ):
        if not isinstance(value[key], list) or any(not isinstance(item, str) or not item for item in value[key]):
            raise RiskPolicyError(f"risk policy {key} must be a nonempty-string list")
    limits = value["limits"]
    if not isinstance(limits, dict) or set(limits) != {
        "max_changed_paths", "max_metrics", "max_receipt_bytes", "max_string_bytes",
    }:
        raise RiskPolicyError("risk policy limits are incomplete")
    if any(not isinstance(v, int) or isinstance(v, bool) or v <= 0 for v in limits.values()):
        raise RiskPolicyError("risk policy limits are invalid")
    return value


def _matches(path: str, patterns: list[str]) -> bool:
    # PurePath.match supplies useful ** semantics; fnmatch retains root-file matches.
    item = PurePosixPath(path)
    return any(item.match(pattern) or fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def _normalize_paths(paths: Any, maximum: int) -> tuple[list[str], bool]:
    if not isinstance(paths, list) or len(paths) > maximum:
        return [], True
    normalized: list[str] = []
    ambiguous = False
    for raw in paths:
        if not isinstance(raw, str) or not raw or "\x00" in raw:
            ambiguous = True
            continue
        if "\\" in raw:
            ambiguous = True
            continue
        candidate = raw
        components = candidate.split("/")
        if (candidate.startswith("/") or re.match(r"^[A-Za-z]:", candidate)
                or candidate in {"", "."} or ".." in components):
            ambiguous = True
            continue
        normalized_path = str(PurePosixPath(candidate))
        if normalized_path in {"", "."}:
            ambiguous = True
            continue
        normalized.append(normalized_path)
    return sorted(set(normalized)), ambiguous or not normalized


LIFECYCLE_INFRASTRUCTURE_PREFIXES = (
    ".juno_task/scripts/",
    "juno-code/src/templates/scripts/",
)


def _classify_paths(policy: dict[str, Any], identity: dict[str, Any],
                    changed_paths: Any, flags: Any = None) -> dict[str, Any]:
    paths, ambiguous = _normalize_paths(changed_paths, policy["limits"]["max_changed_paths"])
    if flags is None:
        flags = []
    if not isinstance(flags, list) or any(not isinstance(flag, str) for flag in flags):
        flags, ambiguous = [], True
    flags = sorted(set(flags))
    known_flags = set(policy["high_risk_flags"]) | set(policy["release_flags"])
    unknown = sorted(set(flags) - known_flags)
    release = bool(set(flags) & set(policy["release_flags"]))
    lifecycle_paths = [p for p in paths
                       if p.startswith(LIFECYCLE_INFRASTRUCTURE_PREFIXES)]
    high_paths = sorted(set(
        [p for p in paths if _matches(p, policy["high_risk_paths"])]
        + lifecycle_paths))
    shared_paths = sorted(set(
        [p for p in paths if _matches(p, policy["shared_infrastructure_paths"])]
        + lifecycle_paths))
    low_only = bool(paths) and all(_matches(p, policy["low_risk_paths"]) for p in paths)
    reasons: list[str] = []
    if ambiguous or unknown:
        tier = "high"; reasons.append("ambiguous_policy_input")
    elif high_paths or set(flags) & set(policy["high_risk_flags"]):
        tier = "high"; reasons.append("high_risk_surface")
    elif low_only:
        tier = "low"; reasons.append("docs_only")
    else:
        tier = "normal"; reasons.append("ordinary_runtime")
    if release:
        reasons.append("release_authority")
    full_suite = tier in policy["full_suite_tiers"] or bool(shared_paths) or release
    review = policy["review_policy"][tier]
    return {
        "schema_version": SCHEMA,
        "producer": {"schema_version": PLAN_PRODUCER_SCHEMA, "tool_id": PLAN_TOOL_ID},
        "candidate": identity,
        "policy_identity": digest(policy),
        "tier": tier,
        "reasons": reasons,
        "changed_paths": paths,
        "flags": flags,
        "unknown_flags": unknown,
        "shared_infrastructure": shared_paths,
        "affected_validation_required": True,
        "full_suite_required": full_suite,
        "reviewer_sequence": review["sequence"],
        "min_reviews": review["min"],
        "max_reviews": review["max"],
        "finding_policy_revision": FINDING_POLICY_REVISION,
        "release_gate_required": release,
        "post_cas": {"semantic_review": False, "checks": policy["post_cas_checks"]},
        "evidence_limits": {"max_metrics": policy["limits"]["max_metrics"],
                            "max_string_bytes": policy["limits"]["max_string_bytes"],
                            "max_receipt_bytes": policy["limits"]["max_receipt_bytes"]},
    }


def _candidate_request(value: Any) -> tuple[Path, str, str, str]:
    keys = {"repository", "candidate_sha", "target_ref", "expected_target_sha"}
    if (not isinstance(value, dict) or set(value) != keys
            or not isinstance(value.get("repository"), str)):
        raise RiskPolicyError("candidate Git identity request contains unknown fields")
    return (Path(value["repository"]), value.get("candidate_sha", ""),
            value.get("target_ref", ""), value.get("expected_target_sha", ""))


def classify(policy: dict[str, Any], candidate_request: dict[str, Any],
             flags: Any = None) -> dict[str, Any]:
    repository, candidate_sha, target_ref, expected_target_sha = _candidate_request(candidate_request)
    identity = candidate_identity(repository, candidate_sha, target_ref, expected_target_sha)
    return _classify_paths(policy, identity, identity["changed_paths"], flags)


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repository), *args], stdin=subprocess.DEVNULL,
                            text=True, capture_output=True)
    if result.returncode:
        raise RiskPolicyError("Git refused candidate identity resolution")
    return result.stdout.strip()


def candidate_identity(repository: Path, candidate_sha: str, target_ref: str,
                       expected_target_sha: str) -> dict[str, Any]:
    repository = repository.resolve()
    branch_name = target_ref.removeprefix("refs/heads/")
    if (not repository.is_dir() or not SHA_RE.fullmatch(candidate_sha)
            or not SHA_RE.fullmatch(expected_target_sha)
            or not target_ref.startswith("refs/heads/") or not branch_name):
        raise RiskPolicyError("candidate Git identity request is malformed")
    ref_check = subprocess.run(["git", "-C", str(repository), "check-ref-format", target_ref],
                               stdin=subprocess.DEVNULL, capture_output=True)
    if ref_check.returncode:
        raise RiskPolicyError("target must be one strict full local branch ref")
    resolved_candidate = _git(repository, "rev-parse", "--verify", candidate_sha + "^{commit}")
    resolved_expected = _git(repository, "rev-parse", "--verify", expected_target_sha + "^{commit}")
    target_sha = _git(repository, "show-ref", "--verify", "--hash", target_ref)
    if (resolved_candidate != candidate_sha or resolved_expected != expected_target_sha
            or target_sha != expected_target_sha):
        raise RiskPolicyError("candidate or expected target is not the exact current commit")
    candidate_tree = _git(repository, "rev-parse", candidate_sha + "^{tree}")
    base_tree = _git(repository, "rev-parse", expected_target_sha + "^{tree}")
    parents = _git(repository, "show", "-s", "--format=%P", candidate_sha).split()
    if not parents or any(not SHA_RE.fullmatch(parent) for parent in parents):
        raise RiskPolicyError("candidate parent graph is malformed")
    if len(parents) == 2 and parents[0] == expected_target_sha:
        candidate_kind, source_feature_tip = "target_first_merge", parents[1]
        if _git(repository, "rev-parse", "--verify",
                source_feature_tip + "^{commit}") != source_feature_tip:
            raise RiskPolicyError("merge source feature tip is not an exact commit")
        merge_base = subprocess.run(
            ["git", "-C", str(repository), "merge-base", expected_target_sha,
             source_feature_tip], stdin=subprocess.DEVNULL, text=True, capture_output=True)
        if merge_base.returncode or not SHA_RE.fullmatch(merge_base.stdout.strip()):
            raise RiskPolicyError("target-first merge source has no ordinary shared ancestry")
    else:
        candidate_kind, source_feature_tip = "direct_descendant", candidate_sha
    ancestry = subprocess.run(
        ["git", "-C", str(repository), "merge-base", "--is-ancestor",
         expected_target_sha, candidate_sha], stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if ancestry.returncode != 0:
        raise RiskPolicyError("candidate/source feature tip is not descended from expected target")
    changed = _git(repository, "diff", "--name-only", "--no-renames", "--diff-filter=ACDMRTUXB",
                   expected_target_sha, candidate_sha).splitlines()
    paths, ambiguous = _normalize_paths(changed, 1000000)
    if ambiguous or paths != sorted(set(changed)):
        raise RiskPolicyError("Git produced ambiguous changed paths")
    product = {"candidate_tree": candidate_tree}
    composition = {"base_sha": expected_target_sha, "base_tree": base_tree,
                   "target_ref": target_ref, "target_sha": target_sha, "parents": parents,
                   "candidate_kind": candidate_kind, "source_feature_tip": source_feature_tip}
    return {"candidate_sha": candidate_sha, "candidate_tree": candidate_tree,
            "base_sha": expected_target_sha, "base_tree": base_tree, "target_ref": target_ref,
            "target_sha": target_sha, "parents": parents, "source_feature_tip": source_feature_tip,
            "candidate_kind": candidate_kind, "changed_paths": paths,
            "product_digest": digest(product), "composition_digest": digest(composition)}


def _validate_candidate_identity(value: Any) -> None:
    keys = {"candidate_sha", "candidate_tree", "base_sha", "base_tree", "target_ref",
            "target_sha", "parents", "candidate_kind", "source_feature_tip", "changed_paths",
            "product_digest", "composition_digest"}
    if (not isinstance(value, dict) or set(value) != keys
            or any(not isinstance(value.get(key), str) or not SHA_RE.fullmatch(value[key])
                   for key in ("candidate_sha", "candidate_tree", "base_sha", "base_tree", "target_sha"))
            or not isinstance(value.get("target_ref"), str) or not value["target_ref"].startswith("refs/heads/")
            or not isinstance(value.get("source_feature_tip"), str)
            or not SHA_RE.fullmatch(value["source_feature_tip"])
            or not isinstance(value.get("changed_paths"), list)
            or any(not isinstance(path, str) or not path for path in value["changed_paths"])
            or not isinstance(value.get("parents"), list)
            or not value["parents"]
            or any(not isinstance(parent, str) or not SHA_RE.fullmatch(parent) for parent in value["parents"])
            or value.get("candidate_kind") not in {"direct_descendant", "target_first_merge"}
            or (value["candidate_kind"] == "direct_descendant"
                and value["source_feature_tip"] != value["candidate_sha"])
            or (value["candidate_kind"] == "target_first_merge"
                and (len(value["parents"]) != 2 or value["parents"][0] != value["base_sha"]
                     or value["source_feature_tip"] != value["parents"][1]))
            or value["changed_paths"] != sorted(set(value["changed_paths"]))
            or any(not isinstance(value.get(key), str) or not DIGEST_RE.fullmatch(value[key])
                   for key in ("product_digest", "composition_digest"))):
        raise RiskPolicyError("candidate Git identity schema is invalid")
    if (value["product_digest"] != digest({"candidate_tree": value["candidate_tree"]})
            or value["composition_digest"] != digest({
                "base_sha": value["base_sha"], "base_tree": value["base_tree"],
                "target_ref": value["target_ref"], "target_sha": value["target_sha"],
                "parents": value["parents"], "candidate_kind": value["candidate_kind"],
                "source_feature_tip": value["source_feature_tip"],
            })):
        raise RiskPolicyError("candidate Git identity digests are inconsistent")


def _policy_binding(plan: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(plan, dict) or set(plan) != PLAN_KEYS or plan.get("schema_version") != SCHEMA:
        raise RiskPolicyError("risk plan schema is unsupported or contains unknown fields")
    if (not isinstance(plan.get("policy_identity"), str)
            or not DIGEST_RE.fullmatch(plan["policy_identity"])
            or plan.get("producer") != {"schema_version": PLAN_PRODUCER_SCHEMA,
                                        "tool_id": PLAN_TOOL_ID}
            or plan.get("tier") not in {"low", "normal", "high", "release"}
            or not isinstance(plan.get("reasons"), list)
            or any(not isinstance(item, str) or not item for item in plan["reasons"])
            or not isinstance(plan.get("reviewer_sequence"), list)
            or any(item not in {"reviewer", "reviewer_a", "reviewer_b"}
                   for item in plan["reviewer_sequence"])
            or not isinstance(plan.get("min_reviews"), int)
            or not isinstance(plan.get("max_reviews"), int)
            or not 0 <= plan["min_reviews"] <= plan["max_reviews"] <= len(plan["reviewer_sequence"])
            or not isinstance(plan.get("full_suite_required"), bool)
            or plan.get("finding_policy_revision") != FINDING_POLICY_REVISION
            or not isinstance(plan.get("release_gate_required"), bool)
            or plan.get("affected_validation_required") is not True
            or not isinstance(plan.get("changed_paths"), list)
            or not isinstance(plan.get("flags"), list)
            or not isinstance(plan.get("unknown_flags"), list)
            or not isinstance(plan.get("shared_infrastructure"), list)
            or not isinstance(plan.get("evidence_limits"), dict)
            or set(plan["evidence_limits"]) != {"max_metrics", "max_string_bytes", "max_receipt_bytes"}
            or any(not isinstance(value, int) or isinstance(value, bool) or value <= 0
                   for value in plan["evidence_limits"].values())
            or not isinstance(plan.get("post_cas"), dict)
            or set(plan["post_cas"]) != {"semantic_review", "checks"}
            or plan["post_cas"].get("semantic_review") is not False
            or not isinstance(plan["post_cas"].get("checks"), list)):
        raise RiskPolicyError("risk plan policy binding is malformed")
    _validate_candidate_identity(plan.get("candidate"))
    if plan["changed_paths"] != plan["candidate"]["changed_paths"]:
        raise RiskPolicyError("risk plan paths do not bind its Git candidate")
    return {key: plan[key] for key in (
        "producer", "policy_identity", "tier", "reasons", "reviewer_sequence", "min_reviews",
        "max_reviews", "full_suite_required", "finding_policy_revision", "release_gate_required",
    )}


def _metrics(plan: dict[str, Any], metrics: Any) -> dict[str, int]:
    if (not isinstance(metrics, dict)
            or len(metrics) > plan["evidence_limits"]["max_metrics"]
            or set(metrics) - ALLOWED_METRICS):
        raise RiskPolicyError("metrics are unbounded or contain transcript-like data")
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in metrics.values()):
        raise RiskPolicyError("metrics must be non-negative integers")
    return dict(sorted(metrics.items()))


def _bounded_object(path_value: Any, expected_sha256: Any, plan: dict[str, Any], label: str) -> dict[str, Any]:
    if not isinstance(path_value, str) or not path_value or len(path_value.encode()) > plan["evidence_limits"]["max_string_bytes"]:
        raise RiskPolicyError(f"{label} path is missing or unbounded")
    if not isinstance(expected_sha256, str) or not DIGEST_RE.fullmatch(expected_sha256):
        raise RiskPolicyError(f"{label} digest is invalid")
    path = Path(path_value)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise RiskPolicyError(f"{label} is missing or unreadable") from exc
    if not data or len(data) > plan["evidence_limits"]["max_receipt_bytes"]:
        raise RiskPolicyError(f"{label} is empty or exceeds the evidence bound")
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise RiskPolicyError(f"{label} digest does not match its content")
    try:
        value = json.loads(data)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RiskPolicyError(f"{label} is malformed") from exc
    if not isinstance(value, dict):
        raise RiskPolicyError(f"{label} must be one JSON object")
    if data != canonical(value):
        raise RiskPolicyError(f"{label} must use canonical JSON bytes")
    return value


def _validate_full_suite_execution(receipt: dict[str, Any], command: dict[str, Any],
                                    plan: dict[str, Any]) -> None:
    """Validate timing, resource, execution identity, and bounded streams."""
    timing, resource, execution_identity = (receipt.get("timing"), receipt.get("resource"),
                                             receipt.get("identity"))
    result = receipt.get("result")
    states = timing.get("states") if isinstance(timing, dict) else None
    phase_names = [item.get("state") for item in states] if isinstance(states, list) else []
    terminal = {"PASSED", "FAILED", "TIMED_OUT", "INTERRUPTED", "SETUP_FAILED"}
    legacy_timing_keys = {"schema_version", "states", "wall_duration_ms",
                          "critical_path_contribution_ms"}
    phase_timing_keys = legacy_timing_keys | {
        "resource_wait_ms", "setup_ms", "execution_ms", "settlement_ms",
        "first_failure_ms", "overall_elapsed_ms"}
    timing_keys = frozenset(timing) if isinstance(timing, dict) else frozenset()
    if (not isinstance(timing, dict)
            or timing_keys not in {frozenset(legacy_timing_keys), frozenset(phase_timing_keys)}
            or timing.get("schema_version") != VALIDATION_TIMING_SCHEMA
            or phase_names[:4] != ["WAITING_FOR_RESOURCE", "SETUP", "RUNNING", "TEARDOWN"]
            or len(phase_names) != 5 or phase_names[-1] not in terminal
            or any(not isinstance(item, dict) or set(item) != {"state", "duration_ms"}
                   or not isinstance(item.get("duration_ms"), int)
                   or isinstance(item.get("duration_ms"), bool) or item["duration_ms"] < 0
                   for item in states)
            or not isinstance(timing.get("wall_duration_ms"), int)
            or timing["wall_duration_ms"] < 0
            or timing.get("critical_path_contribution_ms") != timing["wall_duration_ms"]
            or (timing_keys == phase_timing_keys and (
                timing.get("overall_elapsed_ms") != timing["wall_duration_ms"]
                or timing.get("resource_wait_ms") != states[0]["duration_ms"]
                or timing.get("setup_ms") != states[1]["duration_ms"]
                or timing.get("execution_ms") != states[2]["duration_ms"]
                or timing.get("settlement_ms") != states[3]["duration_ms"]
                or (timing.get("first_failure_ms") is not None
                    and (not isinstance(timing.get("first_failure_ms"), int)
                         or isinstance(timing.get("first_failure_ms"), bool)
                         or not 0 <= timing["first_failure_ms"] <= timing["overall_elapsed_ms"]))
                or (phase_names[-1] == "PASSED" and timing.get("first_failure_ms") is not None)
                or (phase_names[-1] != "PASSED"
                    and timing.get("first_failure_ms") != timing["overall_elapsed_ms"])))
            or not isinstance(resource, dict)
            or set(resource) != {"id", "lock_identity_sha256", "wait_timeout_seconds",
                                 "owner_diagnostics"}
            or (resource["lock_identity_sha256"] is not None
                and (not isinstance(resource["lock_identity_sha256"], str)
                     or not DIGEST_RE.fullmatch(resource["lock_identity_sha256"])))
            or not isinstance(execution_identity, dict)
            or set(execution_identity) != {"command_sha256", "cwd_sha256", "policy_sha256",
                                           "candidate_sha", "candidate_tree"}
            or any(not isinstance(execution_identity[key], str)
                   or not DIGEST_RE.fullmatch(execution_identity[key])
                   for key in ("command_sha256", "cwd_sha256", "policy_sha256"))):
        raise RiskPolicyError("full-suite receipt timing provenance is invalid")
    for name in ("stdout", "stderr"):
        stream = result.get(name)
        if (not isinstance(stream, dict) or set(stream) != {"sha256", "tail", "truncated_bytes"}
                or not isinstance(stream.get("sha256"), str)
                or not DIGEST_RE.fullmatch(stream["sha256"])
                or not isinstance(stream.get("tail"), str)
                or len(stream["tail"].encode()) > command["max_output_bytes"] * 4
                or not isinstance(stream.get("truncated_bytes"), int)
                or isinstance(stream.get("truncated_bytes"), bool)
                or stream["truncated_bytes"] < 0):
            raise RiskPolicyError("full-suite receipt stream provenance is invalid")
    _validate_full_suite_retries(result.get("retries"))


def _validate_full_suite_retries(retries: Any) -> None:
    """Strictly validate optional bounded file-level retry evidence.

    Absent retries are the historical shape. Present retries must bind the
    bounded policy, one entry per retried file with ordered attempt evidence,
    and a joined verdict that is exactly "every retried file passed isolated".
    """
    if retries is None:
        return
    attempt_keys = {"exit_code", "timed_out"}
    if (not isinstance(retries, dict)
            or set(retries) != {"policy", "files", "absorbed"}
            or not isinstance(retries.get("policy"), dict)
            or set(retries["policy"]) != {"max_files", "max_attempts_per_file"}
            or not isinstance(retries["policy"].get("max_files"), int)
            or isinstance(retries["policy"].get("max_files"), bool)
            or not 1 <= retries["policy"]["max_files"] <= 8
            or not isinstance(retries["policy"].get("max_attempts_per_file"), int)
            or isinstance(retries["policy"].get("max_attempts_per_file"), bool)
            or not 1 <= retries["policy"]["max_attempts_per_file"] <= 4
            or not isinstance(retries.get("files"), list)
            or not retries["files"]
            or len(retries["files"]) > retries["policy"]["max_files"]
            or not isinstance(retries.get("absorbed"), bool)):
        raise RiskPolicyError("full-suite receipt retry provenance is invalid")
    for entry in retries["files"]:
        if (not isinstance(entry, dict)
                or set(entry) != {"file", "passed", "attempts", "final_tail"}
                or not isinstance(entry.get("file"), str) or not entry["file"]
                or ".test." not in entry["file"]
                or not isinstance(entry.get("passed"), bool)
                or not isinstance(entry.get("final_tail"), str)
                or len(entry["final_tail"].encode()) > 4096
                or not isinstance(entry.get("attempts"), list)
                or not 1 <= len(entry["attempts"]) <= retries["policy"]["max_attempts_per_file"]
                or any(not isinstance(attempt, dict) or set(attempt) != attempt_keys
                       or not isinstance(attempt.get("exit_code"), int)
                       or isinstance(attempt.get("exit_code"), bool)
                       or not isinstance(attempt.get("timed_out"), bool)
                       for attempt in entry["attempts"])
                or entry["passed"] != any(
                    attempt["exit_code"] == 0 and not attempt["timed_out"]
                    for attempt in entry["attempts"])):
            raise RiskPolicyError("full-suite receipt retry provenance is invalid")
    if retries["absorbed"] != all(entry["passed"] for entry in retries["files"]):
        raise RiskPolicyError("full-suite receipt retry verdict is inconsistent")


def verify_full_suite_receipt(reference: Any, plan: dict[str, Any],
                              expected_validation_identity: Any = None,
                              expected_command: Any = None,
                              expected_claim: Any = None,
                              require_success: bool = True,
                              *, candidate: Any = None) -> dict[str, Any]:
    """Reopen and strictly validate one immutable full-suite authority receipt."""
    if not isinstance(reference, dict) or set(reference) != {"receipt_path", "receipt_sha256"}:
        raise RiskPolicyError("full-suite authority must be one exact receipt reference")
    receipt = _bounded_object(reference.get("receipt_path"), reference.get("receipt_sha256"),
                              plan, "full-suite receipt")
    expected_candidate = candidate if candidate is not None else plan["candidate"]
    if (not isinstance(expected_candidate, dict)
            or not isinstance(expected_candidate.get("candidate_sha"), str)
            or not SHA_RE.fullmatch(expected_candidate["candidate_sha"])
            or not isinstance(expected_candidate.get("candidate_tree"), str)
            or not SHA_RE.fullmatch(expected_candidate["candidate_tree"])):
        raise RiskPolicyError("full-suite expected candidate identity is invalid")
    keys = {"schema_version", "producer", "candidate", "policy_identity", "claim",
            "validation_identity", "command", "started_at", "completed_at", "timing",
            "resource", "identity", "result"}
    command_keys = {"id", "cwd", "argv", "timeout_seconds", "max_output_bytes"}
    result_keys = {"exit_code", "timed_out", "stdout", "stderr"}
    identity_keys, identity_keys_routed = IDENTITY_KEYS, IDENTITY_KEYS_ROUTED
    command, result = receipt.get("command"), receipt.get("result")
    timing, resource, execution_identity = (receipt.get("timing"), receipt.get("resource"),
                                             receipt.get("identity"))
    identity = receipt.get("validation_identity")
    claim = receipt.get("claim")
    claim_keys = {"claim_path", "claim_sha256", "token", "attempt_number"}
    if (set(receipt) != keys or receipt.get("schema_version") != FULL_SUITE_SCHEMA
            or receipt.get("producer") != {"schema_version": FULL_SUITE_PRODUCER_SCHEMA,
                                             "tool_id": FULL_SUITE_TOOL_ID}
            or receipt.get("candidate") != {"candidate_sha": expected_candidate["candidate_sha"],
                                              "candidate_tree": expected_candidate["candidate_tree"]}
            or receipt.get("policy_identity") != plan["policy_identity"]
            or not isinstance(claim, dict) or set(claim) != claim_keys
            or not isinstance(claim.get("claim_path"), str) or not claim["claim_path"]
            or not isinstance(claim.get("claim_sha256"), str)
            or not DIGEST_RE.fullmatch(claim["claim_sha256"])
            or not isinstance(claim.get("token"), str) or len(claim["token"]) != 48
            or not isinstance(claim.get("attempt_number"), int)
            or isinstance(claim.get("attempt_number"), bool) or claim["attempt_number"] <= 0
            or (expected_claim is not None and claim != expected_claim)
            or not isinstance(identity, dict)
            or set(identity) not in (identity_keys, identity_keys_routed)
            or any(not isinstance(identity.get(key), str) or not DIGEST_RE.fullmatch(identity[key])
                   for key in identity)
            or (expected_validation_identity is not None and identity != expected_validation_identity)
            or not isinstance(command, dict)
            or set(command) not in (command_keys, command_keys | {"resource"})
            or (expected_command is not None and command != expected_command)
            or not isinstance(command.get("id"), str) or not command["id"]
            or not isinstance(command.get("cwd"), str)
            or not isinstance(command.get("argv"), list) or not command["argv"]
            or any(not isinstance(arg, str) or not arg for arg in command["argv"])
            or not isinstance(command.get("timeout_seconds"), int)
            or isinstance(command.get("timeout_seconds"), bool) or command["timeout_seconds"] <= 0
            or not isinstance(command.get("max_output_bytes"), int)
            or isinstance(command.get("max_output_bytes"), bool) or command["max_output_bytes"] <= 0
            or command["max_output_bytes"] > plan["evidence_limits"]["max_receipt_bytes"]
            or any(not isinstance(receipt.get(key), str) or not receipt[key]
                   or len(receipt[key].encode()) > plan["evidence_limits"]["max_string_bytes"]
                   for key in ("started_at", "completed_at"))
            or not isinstance(result, dict) or set(result) != result_keys
            or not isinstance(result.get("exit_code"), int) or isinstance(result.get("exit_code"), bool)
            or not isinstance(result.get("timed_out"), bool)):
        raise RiskPolicyError("full-suite receipt provenance is invalid")
    _validate_full_suite_execution(receipt, command, plan)
    if _time(receipt["completed_at"]) < _time(receipt["started_at"]):
        raise RiskPolicyError("full-suite receipt completion precedes its start")
    if require_success and (result["timed_out"] or result["exit_code"] != 0):
        raise RiskPolicyError("full-suite receipt is not successful")
    return {"receipt_path": str(Path(reference["receipt_path"]).resolve()),
            "receipt_sha256": reference["receipt_sha256"]}


def verify_full_suite_admission(admission: Any, plan: dict[str, Any],
                                expected_validation_identity: Any = None,
                                expected_command: Any = None,
                                require_success: bool = True,
                                *, candidate: Any = None) -> dict[str, Any]:
    """Verify bounded claim + receipt provenance; queue code adds containment checks."""
    keys = {"schema_version", "state", "attempt_number", "token", "claim",
            "receipt"}
    if (not isinstance(admission, dict) or set(admission) != keys
            or admission.get("schema_version") != FULL_SUITE_ADMISSION_SCHEMA
            or admission.get("state") != "COMPLETE"
            or not isinstance(admission.get("attempt_number"), int)
            or isinstance(admission.get("attempt_number"), bool)
            or admission["attempt_number"] <= 0
            or not isinstance(admission.get("token"), str)
            or len(admission["token"]) != 48
            or not isinstance(admission.get("claim"), dict)
            or set(admission["claim"]) != {"claim_path", "claim_sha256"}
            or not isinstance(admission.get("receipt"), dict)
            or set(admission["receipt"]) != {"receipt_path", "receipt_sha256"}):
        raise RiskPolicyError("full-suite admission provenance is invalid")
    claim_ref = admission["claim"]
    claim = _bounded_object(claim_ref.get("claim_path"), claim_ref.get("claim_sha256"),
                            plan, "full-suite claim")
    claim_keys = {"schema_version", "producer", "task_id", "candidate",
                  "policy_identity", "validation_identity", "command", "token",
                  "attempt_number", "expected_receipt_path"}
    expected_candidate = candidate if candidate is not None else plan["candidate"]
    expected_compact = {"candidate_sha": expected_candidate.get("candidate_sha"),
                        "candidate_tree": expected_candidate.get("candidate_tree")}
    if (set(claim) != claim_keys or claim.get("schema_version") != FULL_SUITE_CLAIM_SCHEMA
            or claim.get("producer") != {"schema_version": FULL_SUITE_PRODUCER_SCHEMA,
                                          "tool_id": FULL_SUITE_TOOL_ID}
            or not isinstance(claim.get("task_id"), str) or not claim["task_id"]
            or claim.get("candidate") != expected_compact
            or claim.get("policy_identity") != plan["policy_identity"]
            or (expected_validation_identity is not None
                and claim.get("validation_identity") != expected_validation_identity)
            or (expected_command is not None and claim.get("command") != expected_command)
            or claim.get("token") != admission["token"]
            or claim.get("attempt_number") != admission["attempt_number"]
            or claim.get("expected_receipt_path") != admission["receipt"]["receipt_path"]):
        raise RiskPolicyError("full-suite claim provenance is invalid")
    compact_claim = {"claim_path": str(Path(claim_ref["claim_path"]).resolve()),
                     "claim_sha256": claim_ref["claim_sha256"],
                     "token": admission["token"],
                     "attempt_number": admission["attempt_number"]}
    receipt = verify_full_suite_receipt(
        admission["receipt"], plan, expected_validation_identity, expected_command,
        compact_claim, require_success, candidate=expected_candidate)
    return {"schema_version": FULL_SUITE_ADMISSION_SCHEMA, "state": "COMPLETE",
            "attempt_number": admission["attempt_number"], "token": admission["token"],
            "claim": {"claim_path": compact_claim["claim_path"],
                      "claim_sha256": compact_claim["claim_sha256"]},
            "receipt": receipt}


def _validate_command_row(command: Any, plan: dict[str, Any]) -> None:
    command_keys = {"id", "cwd", "argv", "timeout_seconds", "max_output_bytes"}
    optional_keys = {"resource", "input_paths"}
    if (not isinstance(command, dict)
            or not command_keys.issubset(command)
            or set(command) - command_keys - optional_keys
            or not isinstance(command.get("id"), str) or not command["id"]
            or not isinstance(command.get("cwd"), str)
            or not isinstance(command.get("argv"), list) or not command["argv"]
            or any(not isinstance(arg, str) or not arg for arg in command["argv"])
            or not isinstance(command.get("timeout_seconds"), int)
            or isinstance(command.get("timeout_seconds"), bool)
            or command["timeout_seconds"] <= 0
            or not isinstance(command.get("max_output_bytes"), int)
            or isinstance(command.get("max_output_bytes"), bool)
            or command["max_output_bytes"] <= 0
            or command["max_output_bytes"] > plan["evidence_limits"]["max_receipt_bytes"]):
        raise RiskPolicyError("full-suite command row provenance is invalid")
    input_paths = command.get("input_paths")
    if input_paths is not None:
        if (not isinstance(input_paths, list) or not input_paths or len(input_paths) > 64
                or any(not isinstance(path, str) or not path
                       or len(path.encode()) > 1024 for path in input_paths)
                or len(set(input_paths)) != len(input_paths)):
            raise RiskPolicyError("full-suite command row provenance is invalid")
        for path in input_paths:
            value = PurePosixPath(path)
            if value.is_absolute() or str(value) != path or any(
                    part in {"", ".", ".."} for part in value.parts):
                raise RiskPolicyError("full-suite command row provenance is invalid")


def _validate_command_suite(commands: Any, plan: dict[str, Any]) -> list[dict[str, Any]]:
    if (not isinstance(commands, list) or not commands or len(commands) > 16
            or len({row.get("id") if isinstance(row, dict) else None for row in commands})
                != len(commands)):
        raise RiskPolicyError("full-suite command suite is empty, unbounded, or duplicated")
    for row in commands:
        _validate_command_row(row, plan)
    return commands


def _validate_routing(routing: Any) -> None:
    if (not isinstance(routing, dict)
            or set(routing) != {"mode", "profile_ids", "authored_path_count"}
            or routing.get("mode") not in {"default", "profile", "union"}
            or not isinstance(routing.get("profile_ids"), list)
            or any(not isinstance(item, str) or not item for item in routing["profile_ids"])
            or not isinstance(routing.get("authored_path_count"), int)
            or isinstance(routing.get("authored_path_count"), bool)
            or routing["authored_path_count"] < 0
            or (routing["mode"] == "default") != (not routing["profile_ids"])
            or (routing["mode"] in {"profile", "union"}) != (bool(routing["profile_ids"]))
            or (routing["mode"] == "profile" and len(routing["profile_ids"]) != 1)):
        raise RiskPolicyError("full-suite routing provenance is invalid")


def _validate_producer_lock(lock: Any, plan: dict[str, Any]) -> None:
    if (not isinstance(lock, dict) or set(lock) != {"path", "kind"}
            or lock.get("kind") != "flock"
            or not isinstance(lock.get("path"), str) or not lock["path"]
            or len(lock["path"].encode()) > plan["evidence_limits"]["max_string_bytes"]):
        raise RiskPolicyError("full-suite producer lock provenance is invalid")


def _claim_v2_binding(claim: Any, plan: dict[str, Any], admission: dict[str, Any],
                      expected_validation_identity: Any, expected_commands: Any,
                      expected_candidate: dict[str, Any]) -> list[dict[str, Any]]:
    """Strictly validate one v2 suite claim and return its command rows."""
    claim_keys = {"schema_version", "producer", "task_id", "candidate",
                  "policy_identity", "validation_identity", "commands", "routing",
                  "producer_lock", "token", "attempt_number", "expected_receipt_paths"}
    if (not isinstance(claim, dict) or set(claim) != claim_keys
            or claim.get("schema_version") != FULL_SUITE_CLAIM_V2_SCHEMA
            or claim.get("producer") != {"schema_version": FULL_SUITE_PRODUCER_SCHEMA,
                                          "tool_id": FULL_SUITE_TOOL_ID}
            or not isinstance(claim.get("task_id"), str) or not claim["task_id"]
            or claim.get("candidate") != {
                "candidate_sha": expected_candidate.get("candidate_sha"),
                "candidate_tree": expected_candidate.get("candidate_tree")}
            or claim.get("policy_identity") != plan["policy_identity"]
            or (expected_validation_identity is not None
                and claim.get("validation_identity") != expected_validation_identity)
            or (expected_commands is not None and claim.get("commands") != expected_commands)):
        raise RiskPolicyError("full-suite suite claim provenance is invalid")
    commands = _validate_command_suite(claim.get("commands"), plan)
    _validate_routing(claim.get("routing"))
    _validate_producer_lock(claim.get("producer_lock"), plan)
    if (claim.get("token") != admission.get("token")
            or claim.get("attempt_number") != admission.get("attempt_number")
            or not isinstance(claim.get("expected_receipt_paths"), list)
            or len(claim["expected_receipt_paths"]) != len(commands)
            or any(not isinstance(path, str) or not path
                   or len(path.encode()) > plan["evidence_limits"]["max_string_bytes"]
                   for path in claim["expected_receipt_paths"])
            or len(set(claim["expected_receipt_paths"])) != len(commands)):
        raise RiskPolicyError("full-suite suite claim receipt binding is invalid")
    return commands


def verify_full_suite_receipt_v3(reference: Any, plan: dict[str, Any],
                                 expected_validation_identity: Any = None,
                                 expected_commands: Any = None,
                                 expected_claim: Any = None,
                                 require_success: bool = True,
                                 *, candidate: Any = None,
                                 expected_command_index: Any = None) -> dict[str, Any]:
    """Reopen and strictly validate one immutable per-command suite receipt."""
    if not isinstance(reference, dict) or set(reference) != {"receipt_path", "receipt_sha256"}:
        raise RiskPolicyError("full-suite authority must be one exact receipt reference")
    receipt = _bounded_object(reference.get("receipt_path"), reference.get("receipt_sha256"),
                              plan, "full-suite receipt")
    expected_candidate = candidate if candidate is not None else plan["candidate"]
    if (not isinstance(expected_candidate, dict)
            or not isinstance(expected_candidate.get("candidate_sha"), str)
            or not SHA_RE.fullmatch(expected_candidate["candidate_sha"])
            or not isinstance(expected_candidate.get("candidate_tree"), str)
            or not SHA_RE.fullmatch(expected_candidate["candidate_tree"])):
        raise RiskPolicyError("full-suite expected candidate identity is invalid")
    keys = {"schema_version", "producer", "candidate", "policy_identity", "claim",
            "validation_identity", "commands", "command_index", "command",
            "started_at", "completed_at", "timing", "resource", "identity", "result"}
    claim_keys = {"claim_path", "claim_sha256", "token", "attempt_number"}
    identity = receipt.get("validation_identity")
    claim = receipt.get("claim")
    commands, command, index = (receipt.get("commands"), receipt.get("command"),
                                receipt.get("command_index"))
    if (set(receipt) != keys
            or receipt.get("schema_version") != FULL_SUITE_RECEIPT_V3_SCHEMA
            or receipt.get("producer") != {"schema_version": FULL_SUITE_PRODUCER_SCHEMA,
                                             "tool_id": FULL_SUITE_TOOL_ID}
            or receipt.get("candidate") != {
                "candidate_sha": expected_candidate["candidate_sha"],
                "candidate_tree": expected_candidate["candidate_tree"]}
            or receipt.get("policy_identity") != plan["policy_identity"]
            or not isinstance(claim, dict) or set(claim) != claim_keys
            or not isinstance(claim.get("claim_path"), str) or not claim["claim_path"]
            or not isinstance(claim.get("claim_sha256"), str)
            or not DIGEST_RE.fullmatch(claim["claim_sha256"])
            or not isinstance(claim.get("token"), str) or len(claim["token"]) != 48
            or not isinstance(claim.get("attempt_number"), int)
            or isinstance(claim.get("attempt_number"), bool) or claim["attempt_number"] <= 0
            or (expected_claim is not None and claim != expected_claim)
            or not isinstance(identity, dict)
            or set(identity) not in (IDENTITY_KEYS, IDENTITY_KEYS_ROUTED)
            or any(not isinstance(identity.get(key), str) or not DIGEST_RE.fullmatch(identity[key])
                   for key in identity)
            or (expected_validation_identity is not None and identity != expected_validation_identity)
            or (expected_commands is not None and commands != expected_commands)
            or not isinstance(index, int) or isinstance(index, bool) or index < 0):
        raise RiskPolicyError("full-suite receipt provenance is invalid")
    commands = _validate_command_suite(commands, plan)
    if (index >= len(commands) or command != commands[index]
            or (expected_command_index is not None and index != expected_command_index)):
        raise RiskPolicyError("full-suite receipt command binding is invalid")
    _validate_command_row(command, plan)
    _validate_full_suite_execution(receipt, command, plan)
    if _time(receipt["completed_at"]) < _time(receipt["started_at"]):
        raise RiskPolicyError("full-suite receipt completion precedes its start")
    if require_success and (receipt["result"]["timed_out"]
                            or receipt["result"]["exit_code"] != 0):
        raise RiskPolicyError("full-suite receipt is not successful")
    return {"receipt_path": str(Path(reference["receipt_path"]).resolve()),
            "receipt_sha256": reference["receipt_sha256"],
            "command_id": command["id"], "command_index": index,
            "exit_code": receipt["result"]["exit_code"],
            "timed_out": receipt["result"]["timed_out"]}


def verify_full_suite_admission_v2(admission: Any, plan: dict[str, Any],
                                   expected_validation_identity: Any = None,
                                   expected_commands: Any = None,
                                   require_success: bool = True,
                                   *, candidate: Any = None) -> dict[str, Any]:
    """Verify one v2 suite admission: one claim, per-command receipts, routing."""
    base_keys = {"schema_version", "state", "attempt_number", "token", "claim"}
    complete_keys = base_keys | {"receipts"}
    failed_keys = complete_keys | {"failure"}
    if (not isinstance(admission, dict)
            or admission.get("schema_version") != FULL_SUITE_ADMISSION_V2_SCHEMA
            or admission.get("state") not in {"COMPLETE", "FAILED"}
            or set(admission) not in (complete_keys, failed_keys)
            or (admission.get("state") == "FAILED") != (set(admission) == failed_keys)
            or not isinstance(admission.get("attempt_number"), int)
            or isinstance(admission.get("attempt_number"), bool)
            or admission["attempt_number"] <= 0
            or not isinstance(admission.get("token"), str) or len(admission["token"]) != 48
            or not isinstance(admission.get("claim"), dict)
            or set(admission["claim"]) != {"claim_path", "claim_sha256"}
            or not isinstance(admission.get("receipts"), list) or not admission["receipts"]):
        raise RiskPolicyError("full-suite suite admission provenance is invalid")
    claim_ref = admission["claim"]
    claim = _bounded_object(claim_ref.get("claim_path"), claim_ref.get("claim_sha256"),
                            plan, "full-suite claim")
    expected_candidate = candidate if candidate is not None else plan["candidate"]
    commands = _claim_v2_binding(claim, plan, admission, expected_validation_identity,
                                 expected_commands, expected_candidate)
    receipts = admission["receipts"]
    if len(receipts) > len(commands):
        raise RiskPolicyError("full-suite suite admission exceeds its claimed commands")
    compact_claim = {"claim_path": str(Path(claim_ref["claim_path"]).resolve()),
                     "claim_sha256": claim_ref["claim_sha256"],
                     "token": admission["token"],
                     "attempt_number": admission["attempt_number"]}
    verified: list[dict[str, Any]] = []
    for index, reference in enumerate(receipts):
        verified.append(verify_full_suite_receipt_v3(
            reference, plan, expected_validation_identity, claim["commands"],
            compact_claim, require_success=False, candidate=expected_candidate,
            expected_command_index=index))
    if admission["state"] == "COMPLETE":
        if len(receipts) != len(commands):
            raise RiskPolicyError("complete suite admission lacks one receipt per command")
        if require_success and any(row["timed_out"] or row["exit_code"] for row in verified):
            raise RiskPolicyError("full-suite suite admission is not successful")
    else:
        failure = admission.get("failure")
        if (not isinstance(failure, dict)
                or set(failure) != {"command_id", "command_index", "exit_code",
                                    "timed_out", "stdout_tail", "stderr_tail"}
                or not isinstance(failure.get("command_id"), str) or not failure["command_id"]
                or not isinstance(failure.get("command_index"), int)
                or isinstance(failure.get("command_index"), bool)
                or failure["command_index"] != len(receipts) - 1
                or not isinstance(failure.get("exit_code"), int)
                or isinstance(failure.get("exit_code"), bool)
                or not isinstance(failure.get("timed_out"), bool)
                or not isinstance(failure.get("stdout_tail"), str)
                or not isinstance(failure.get("stderr_tail"), str)):
            raise RiskPolicyError("full-suite suite failure projection is invalid")
        terminal = verified[-1]
        if (failure["command_id"] != terminal["command_id"]
                or failure["exit_code"] != terminal["exit_code"]
                or failure["timed_out"] != terminal["timed_out"]):
            raise RiskPolicyError("full-suite suite failure does not bind its terminal receipt")
        if not failure["timed_out"] and failure["exit_code"] == 0:
            raise RiskPolicyError("successful terminal receipt cannot be a suite failure")
    observed = [row["receipt_path"] for row in verified]
    if observed != [str(Path(path).resolve()) for path
                    in claim["expected_receipt_paths"][:len(receipts)]]:
        raise RiskPolicyError("full-suite suite receipts are not in their claimed canonical order")
    return {"schema_version": FULL_SUITE_ADMISSION_V2_SCHEMA, "state": admission["state"],
            "attempt_number": admission["attempt_number"], "token": admission["token"],
            "claim": {"claim_path": compact_claim["claim_path"],
                      "claim_sha256": claim_ref["claim_sha256"]},
            "receipts": [{"receipt_path": row["receipt_path"],
                          "receipt_sha256": row["receipt_sha256"]} for row in receipts]}


def verify_full_suite_admission_any(admission: Any, plan: dict[str, Any],
                                    expected_validation_identity: Any = None,
                                    expected_command: Any = None,
                                    require_success: bool = True,
                                    *, candidate: Any = None) -> dict[str, Any]:
    """Dispatch on the admission schema; legacy v1 evidence still verifies."""
    schema = admission.get("schema_version") if isinstance(admission, dict) else None
    if schema == FULL_SUITE_ADMISSION_SCHEMA:
        return verify_full_suite_admission(
            admission, plan, expected_validation_identity, expected_command,
            require_success, candidate=candidate)
    if schema == FULL_SUITE_ADMISSION_V2_SCHEMA:
        return verify_full_suite_admission_v2(
            admission, plan, expected_validation_identity, expected_command,
            require_success, candidate=candidate)
    raise RiskPolicyError("full-suite admission schema is unsupported or ambiguous")


def _validate_persisted_review(value: Any, candidate_sha: str, sequence: int,
                               reviewer: str, max_string_bytes: int) -> None:
    """Validate the compact decision plus its one immutable source reference."""
    keys = {"reviewer", "sequence", "verdict", "finding_count", "advisory_count",
            "blocking_count", "findings", "rejected_observation_count",
            "rejection_counters", "review_reference"}
    if not isinstance(value, dict) or set(value) != keys:
        raise RiskPolicyError("persisted review schema is unsupported or contains unknown fields")
    reference = value.get("review_reference")
    if (value.get("reviewer") != reviewer or value.get("sequence") != sequence
            or value.get("verdict") not in {"pass", "findings"}
            or not isinstance(value.get("finding_count"), int)
            or isinstance(value.get("finding_count"), bool) or value["finding_count"] < 0
            or not isinstance(value.get("advisory_count"), int)
            or isinstance(value.get("advisory_count"), bool) or value["advisory_count"] < 0
            or not isinstance(value.get("blocking_count"), int)
            or isinstance(value.get("blocking_count"), bool) or value["blocking_count"] < 0
            or value["finding_count"] != value["advisory_count"] + value["blocking_count"]
            or not isinstance(value.get("rejected_observation_count"), int)
            or isinstance(value.get("rejected_observation_count"), bool)
            or value["rejected_observation_count"] < 0
            or value["rejected_observation_count"] > MAX_REJECTED_OBSERVATIONS
            or not isinstance(value.get("rejection_counters"), dict)
            or set(value["rejection_counters"]) - REJECTED_OBSERVATION_CLASSES
            or any(not isinstance(count, int) or isinstance(count, bool) or count < 0
                   for count in value["rejection_counters"].values())
            or sum(value["rejection_counters"].values()) != value["rejected_observation_count"]
            or not isinstance(value.get("findings"), list)
            or len(value["findings"]) != value["finding_count"]
            or not isinstance(reference, dict)
            or set(reference) != {"schema_version", "receipt_path", "receipt_sha256"}
            or reference.get("schema_version") != REVIEW_REFERENCE_SCHEMA
            or not isinstance(reference.get("receipt_path"), str)
            or not reference["receipt_path"]
            or len(reference["receipt_path"].encode()) > max_string_bytes
            or not isinstance(reference.get("receipt_sha256"), str)
            or not DIGEST_RE.fullmatch(reference["receipt_sha256"])):
        raise RiskPolicyError("persisted review provenance is invalid")


def _validate_previous(previous: Any, plan: dict[str, Any], identity: dict[str, str]) -> bool:
    if previous is None:
        return False
    if (not isinstance(previous, dict) or set(previous) != EVIDENCE_KEYS
            or previous.get("schema_version") != EVIDENCE_SCHEMA
            or previous.get("producer") != {"schema_version": EVIDENCE_PRODUCER_SCHEMA,
                                             "tool_id": EVIDENCE_TOOL_ID}
            or previous.get("status") != "passed" or previous.get("failure") is not None):
        raise RiskPolicyError("previous evidence schema/status is invalid or contains unknown fields")
    if previous.get("policy") != _policy_binding(plan) or previous.get("post_cas") != plan["post_cas"]:
        raise RiskPolicyError("previous evidence does not bind the current full policy identity")
    prior = previous.get("candidate")
    _validate_candidate_identity(prior)
    validation = previous.get("validation")
    reviews = previous.get("reviews")
    if (not isinstance(validation, dict)
            or set(validation) != {"affected", "full_suite", "full_suite_admission",
                                   "review_dispatches"}
            or validation.get("affected") != "passed"
            or not isinstance(reviews, list)
            or validation.get("review_dispatches") != len(reviews)
            or (plan["full_suite_required"]
                and validation.get("full_suite") not in {"passed", "reused"})
            or (not plan["full_suite_required"]
                and (validation.get("full_suite") != "not_required"
                     or validation.get("full_suite_admission") is not None))):
        raise RiskPolicyError("previous validation evidence is invalid")
    if (previous.get("release_gate") is not None or plan["release_gate_required"]
            or not isinstance(previous.get("metrics"), dict)
            or set(previous["metrics"]) - ALLOWED_METRICS
            or not isinstance(previous.get("created_at"), str)
            or len(previous["created_at"].encode()) > plan["evidence_limits"]["max_string_bytes"]):
        raise RiskPolicyError("previous evidence is not reusable under the current policy")
    reused = previous.get("semantic_evidence_reused")
    if reused is not None and (not isinstance(reused, dict) or set(reused) != {
            "reason", "source_candidate_sha", "source_evidence_sha256",
            "origin_candidate_sha", "origin_reviews"}):
        raise RiskPolicyError("previous reuse projection contains unknown fields")
    if reused is not None and (reused.get("reason") != "product_and_composition_bytes_identical"
            or not isinstance(reused.get("source_candidate_sha"), str)
            or not SHA_RE.fullmatch(reused["source_candidate_sha"])
            or not isinstance(reused.get("source_evidence_sha256"), str)
            or not DIGEST_RE.fullmatch(reused["source_evidence_sha256"])
            or not isinstance(reused.get("origin_candidate_sha"), str)
            or not SHA_RE.fullmatch(reused["origin_candidate_sha"])
            or not isinstance(reused.get("origin_reviews"), list)
            or reviews):
        raise RiskPolicyError("previous reuse projection is malformed")
    if plan["full_suite_required"]:
        source_sha = reused["origin_candidate_sha"] if reused is not None else prior["candidate_sha"]
        verify_full_suite_admission_any(validation.get("full_suite_admission"), plan,
                                        candidate={"candidate_sha": source_sha,
                                                   "candidate_tree": prior["candidate_tree"]})
    evidence_reviews = reused["origin_reviews"] if reused is not None else reviews
    evidence_candidate = reused["origin_candidate_sha"] if reused is not None else prior["candidate_sha"]
    if not (plan["min_reviews"] <= len(evidence_reviews) <= plan["max_reviews"]):
        raise RiskPolicyError("previous review count violates the current policy")
    for index, review in enumerate(evidence_reviews):
        _validate_persisted_review(review, evidence_candidate, index + 1,
                                   plan["reviewer_sequence"][index],
                                   plan["evidence_limits"]["max_string_bytes"])
        rebuilt = _compact_review(
            {"runner_receipt_path": review["review_reference"]["receipt_path"],
             "runner_receipt_sha256": review["review_reference"]["receipt_sha256"]},
            plan["reviewer_sequence"][index], index + 1, evidence_candidate,
            plan["policy_identity"], plan,
        )
        if rebuilt != review:
            raise RiskPolicyError("previous review does not match its immutable source artifacts")
    _validate_review_chain(evidence_reviews)
    if any(review["blocking_count"] for review in evidence_reviews):
        raise RiskPolicyError("previous semantic evidence contains blocking findings")
    _metrics(plan, previous["metrics"])
    return (prior["product_digest"] == identity["product_digest"]
            and prior["composition_digest"] == identity["composition_digest"])


def write_review_binding(path: Path, *, candidate_sha: str, policy_identity: str,
                         reviewer: str, predecessor_receipt: Path | None = None) -> dict[str, str]:
    if reviewer not in {"reviewer", "reviewer_a", "reviewer_b"}:
        raise RiskPolicyError("unknown reviewer role")
    if not SHA_RE.fullmatch(candidate_sha) or not DIGEST_RE.fullmatch(policy_identity):
        raise RiskPolicyError("review binding identities are malformed")
    if reviewer == "reviewer_b":
        if predecessor_receipt is None or not predecessor_receipt.is_file():
            raise RiskPolicyError("Reviewer B requires the exact Reviewer A receipt")
        predecessor = {"receipt_path": str(predecessor_receipt.resolve()),
                       "receipt_sha256": hashlib.sha256(predecessor_receipt.read_bytes()).hexdigest()}
        sequence = 2
    else:
        if predecessor_receipt is not None:
            raise RiskPolicyError("first reviewer cannot declare a predecessor")
        predecessor, sequence = None, 1
    value = {"schema_version": REVIEW_BINDING_SCHEMA, "candidate_sha": candidate_sha,
             "policy_identity": policy_identity, "reviewer_role": reviewer,
             "sequence": sequence, "predecessor": predecessor}
    data = canonical(value); path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(data).hexdigest()}


def reviewer_command(script_dir: Path, *, controller_root: Path, controller_branch: str,
                     candidate_root: Path, candidate_sha: str, prompt_file: Path,
                     out_dir: Path, reviewer: str, task_id: str,
                     review_binding_path: Path) -> list[str]:
    if reviewer not in {"reviewer", "reviewer_a", "reviewer_b"}:
        raise RiskPolicyError("unknown reviewer role")
    runner = script_dir / "managed_agent_runner.py"
    return [sys.executable, str(runner), "run", "--mode", "reviewer",
            "--controller-root", str(controller_root), "--controller-branch", controller_branch,
            "--agent-root", str(out_dir / "agent-root"), "--candidate-root", str(candidate_root),
            "--candidate-sha", candidate_sha, "--prompt-file", str(prompt_file),
            "--out-dir", str(out_dir), "--task-id", task_id,
            "--review-binding", str(review_binding_path),
            "--tool-id", f"bolt_{reviewer}"]


def normalize_review_finding(finding: dict[str, Any]) -> dict[str, Any]:
    """Verify and normalize one review finding against the exact compiled policy.

    Scope admission precedes severity: a finding without an admitted scope
    classification and a cited contract fails closed here and can never reach
    severity normalization, deduplication, advisory persistence, or repair
    disposition. Rejected observations are never findings; they live only in
    the bounded aggregate result-level rejection counters.
    """
    if finding["scope_classification"] not in ADMITTED_SCOPE_CLASSIFICATIONS:
        raise RiskPolicyError(
            "review finding lacks an admitted scope classification: "
            f"{finding.get('scope_classification')!r}")
    categories = finding["impact_categories"]
    if set(categories) & CRITICAL_IMPACTS:
        severity = "critical"
    elif set(categories) & HIGH_IMPACTS:
        severity = "high"
    elif "bounded_product_defect" in categories:
        severity = "medium"
    else:
        severity = "low"
    blocking = severity in {"high", "critical"}
    identity = {key: finding[key] for key in (
        "code", "paths", "symbols", "failure_condition", "acceptance_condition")}
    return {**finding, "reviewer_severity": finding["severity"],
            "normalized_severity": severity, "blocking": blocking,
            "finding_digest": digest(identity),
            "finding_policy_revision": FINDING_POLICY_REVISION}


def _compact_review(value: Any, expected_reviewer: str, sequence: int,
                    candidate_sha: str, policy_identity: str,
                    plan: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or any(key in value for key in TRANSCRIPT_KEYS):
        raise RiskPolicyError("review evidence must be compact and transcript-free")
    allowed = {"runner_receipt_path", "runner_receipt_sha256"}
    if set(value) != allowed:
        raise RiskPolicyError("review evidence fields do not match the strict schema")
    receipt = _bounded_object(value["runner_receipt_path"], value["runner_receipt_sha256"],
                              plan, "managed runner receipt")
    identity = receipt.get("identity")
    binding = receipt.get("review_binding")
    binding_body = ({k: v for k, v in binding.items() if k != "binding_sha256"}
                    if isinstance(binding, dict) else {})
    if (TRANSCRIPT_KEYS & set(receipt)
            or receipt.get("schema_version") != MANAGED_RUNNER_SCHEMA
            or receipt.get("mode") != "reviewer" or receipt.get("state") != "succeeded"
            or receipt.get("semantic_outcome") != "completed"
            or not isinstance(identity, dict) or identity.get("candidate_sha") != candidate_sha
            or not isinstance(binding, dict)
            or set(binding) != {"schema_version", "candidate_sha", "policy_identity",
                                "reviewer_role", "sequence", "predecessor", "binding_sha256"}
            or binding.get("schema_version") != REVIEW_BINDING_SCHEMA
            or binding.get("candidate_sha") != candidate_sha
            or binding.get("policy_identity") != policy_identity
            or binding.get("reviewer_role") != expected_reviewer
            or binding.get("sequence") != sequence
            or binding.get("binding_sha256") != digest(binding_body)):
        raise RiskPolicyError("managed runner receipt does not bind the successful frozen candidate")
    session_id = receipt.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise RiskPolicyError("managed runner session identity is missing")
    if len(session_id.encode("utf-8")) > plan["evidence_limits"]["max_string_bytes"]:
        raise RiskPolicyError("review session identity exceeds compact evidence limit")
    artifacts = receipt.get("artifacts")
    response = artifacts.get("response") if isinstance(artifacts, dict) else None
    prompt = artifacts.get("prompt") if isinstance(artifacts, dict) else None
    if (not isinstance(response, dict) or set(response) != {"path", "bytes", "sha256"}
            or not isinstance(response.get("bytes"), int)
            or isinstance(response.get("bytes"), bool)
            or not isinstance(prompt, dict)
            or not {"path", "bytes", "sha256"} <= set(prompt)
            or not isinstance(prompt.get("bytes"), int)
            or isinstance(prompt.get("bytes"), bool)):
        raise RiskPolicyError("managed runner request/result artifact evidence is missing")
    try:
        prompt_bytes = Path(prompt["path"]).read_bytes()
    except OSError as exc:
        raise RiskPolicyError("managed runner review request is missing") from exc
    if (not prompt_bytes or len(prompt_bytes) > 524288
            or prompt["bytes"] != len(prompt_bytes)
            or hashlib.sha256(prompt_bytes).hexdigest() != prompt["sha256"]):
        raise RiskPolicyError("managed runner review request identity is invalid")
    result = _bounded_object(response.get("path"), response.get("sha256"), plan,
                             "managed runner review result")
    result_keys = {"schema_version", "candidate_sha", "policy_identity", "reviewer_role",
                   "sequence", "verdict", "truncated", "omitted_finding_count",
                   "rejection_counters", "findings"}
    if (set(result) != result_keys or result.get("schema_version") != REVIEW_RESULT_SCHEMA
            or result.get("candidate_sha") != candidate_sha
            or result.get("policy_identity") != policy_identity
            or result.get("reviewer_role") != expected_reviewer
            or result.get("sequence") != sequence
            or result.get("verdict") not in {"pass", "findings"}
            or not isinstance(result.get("truncated"), bool)
            or not isinstance(result.get("omitted_finding_count"), int)
            or isinstance(result.get("omitted_finding_count"), bool)
            or result["omitted_finding_count"] < 0
            or result["truncated"] != (result["omitted_finding_count"] > 0)
            or not isinstance(result.get("findings"), list) or len(result["findings"]) > 32
            or response["bytes"] != Path(response["path"]).stat().st_size):
        raise RiskPolicyError("managed runner review result schema/binding is invalid")
    finding_keys = {"code", "severity", "summary", "paths", "symbols", "evidence",
                    "impact", "failure_condition", "acceptance_condition",
                    "impact_categories", "scope_classification", "cited_contract"}
    normalized_findings = []
    for finding in result["findings"]:
        if not isinstance(finding, dict):
            raise RiskPolicyError("managed runner review finding is malformed or unbounded")
        valid_lists = (isinstance(finding.get("paths"), list)
                       and 1 <= len(finding["paths"]) <= 16
                       and all(isinstance(item, str) and item and len(item.encode()) <= 256
                               for item in finding["paths"])
                       and isinstance(finding.get("symbols"), list)
                       and len(finding["symbols"]) <= 16
                       and all(isinstance(item, str) and item and len(item.encode()) <= 256
                               for item in finding["symbols"])
                       and isinstance(finding.get("impact_categories"), list)
                       and 1 <= len(finding["impact_categories"]) <= 4
                       and len(set(finding["impact_categories"])) == len(finding["impact_categories"])
                       and set(finding["impact_categories"]) <= REVIEW_FINDING_IMPACT_CATEGORIES)
        if (set(finding) != finding_keys or not valid_lists
                or not isinstance(finding.get("code"), str) or not finding["code"]
                or len(finding["code"].encode()) > 64
                or finding.get("severity") not in {"low", "medium", "high", "critical"}
                or finding.get("scope_classification") not in ADMITTED_SCOPE_CLASSIFICATIONS
                or not isinstance(finding.get("cited_contract"), str)
                or not finding["cited_contract"]
                or len(finding["cited_contract"].encode()) > 1024
                or any(not isinstance(finding.get(field), str) or not finding[field]
                       or len(finding[field].encode()) > 1024
                       for field in ("summary", "evidence", "impact", "failure_condition",
                                     "acceptance_condition"))):
            raise RiskPolicyError("managed runner review finding is malformed or unbounded")
        normalized_findings.append(normalize_review_finding(finding))
    counters = result.get("rejection_counters")
    if (not isinstance(counters, dict)
            or set(counters) - REJECTED_OBSERVATION_CLASSES
            or any(not isinstance(count, int) or isinstance(count, bool) or count < 0
                   for count in counters.values())
            or sum(counters.values()) > MAX_REJECTED_OBSERVATIONS):
        raise RiskPolicyError("managed runner review rejection counters are malformed or unbounded")
    if result["truncated"]:
        raise RiskPolicyError("managed runner review result is truncated")
    if (result["verdict"] == "pass") != (not result["findings"]):
        raise RiskPolicyError("managed runner review verdict/findings are contradictory")
    deduplicated = {finding["finding_digest"]: finding for finding in normalized_findings}
    if len(deduplicated) != len(normalized_findings):
        normalized_findings = [deduplicated[key] for key in sorted(deduplicated)]
    blocking_count = sum(1 for finding in normalized_findings if finding["blocking"])
    tool_id, completed_at = receipt.get("tool_id"), receipt.get("completed_at")
    if (not isinstance(tool_id, str) or not tool_id or not isinstance(completed_at, str)
            or not completed_at
            or len(tool_id.encode()) > plan["evidence_limits"]["max_string_bytes"]
            or len(completed_at.encode()) > plan["evidence_limits"]["max_string_bytes"]):
        raise RiskPolicyError("managed runner tool/timestamp provenance is missing")
    return {"reviewer": expected_reviewer, "sequence": sequence,
            "verdict": result["verdict"], "finding_count": len(normalized_findings),
            "advisory_count": len(normalized_findings) - blocking_count,
            "blocking_count": blocking_count, "findings": normalized_findings,
            "rejected_observation_count": sum(result["rejection_counters"].values()),
            "rejection_counters": dict(sorted(result["rejection_counters"].items())),
            "review_reference": {"schema_version": REVIEW_REFERENCE_SCHEMA,
                                 "receipt_path": str(Path(value["runner_receipt_path"]).resolve()),
                                 "receipt_sha256": value["runner_receipt_sha256"]}}


def _time(value: str) -> dt.datetime:
    try: parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc: raise RiskPolicyError("review completion timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise RiskPolicyError("review completion timestamp must include timezone")
    return parsed


def _review_provenance(review: dict[str, Any]) -> dict[str, Any]:
    """Read ordering provenance from the single immutable review reference."""
    reference = review["review_reference"]
    try:
        data = Path(reference["receipt_path"]).read_bytes()
        receipt = json.loads(data)
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        raise RiskPolicyError("review reference is missing or unreadable") from exc
    binding = receipt.get("review_binding") if isinstance(receipt, dict) else None
    if (hashlib.sha256(data).hexdigest() != reference["receipt_sha256"]
            or not isinstance(binding, dict)):
        raise RiskPolicyError("review reference provenance is invalid")
    return {"receipt_sha256": reference["receipt_sha256"],
            "tool_id": receipt.get("tool_id"), "session_id": receipt.get("session_id"),
            "completed_at": receipt.get("completed_at"),
            "binding_sha256": binding.get("binding_sha256"),
            "predecessor": binding.get("predecessor")}


def _validate_review_chain(reviews: list[dict[str, Any]]) -> None:
    provenance = [_review_provenance(review) for review in reviews]
    if len({row["session_id"] for row in provenance}) != len(provenance):
        raise RiskPolicyError("review sessions must be distinct and ordered")
    if len({row["tool_id"] for row in provenance}) != len(provenance):
        raise RiskPolicyError("review tool identities must be distinct")
    if not provenance:
        return
    if provenance[0]["predecessor"] is not None:
        raise RiskPolicyError("first review must not declare a predecessor")
    _time(provenance[0]["completed_at"])
    for prior, current in zip(provenance, provenance[1:]):
        expected = {key: prior[key] for key in (
            "receipt_sha256", "tool_id", "session_id", "completed_at", "binding_sha256")}
        if (current["predecessor"] != expected
                or _time(current["completed_at"]) < _time(prior["completed_at"])):
            raise RiskPolicyError("review predecessor/order provenance is invalid")


def _release_gate(value: Any, plan: dict[str, Any], identity: dict[str, str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
            "receipt_path", "receipt_sha256", "authorization_path"}:
        raise RiskPolicyError("release gate evidence must identify one exact receipt")
    receipt = _bounded_object(value["receipt_path"], value["receipt_sha256"], plan,
                              "release gate receipt")
    keys = {"schema_version", "producer_schema", "tool_id", "candidate_sha",
            "policy_identity", "authority_id", "owner_authorization_sha256", "validation"}
    if (set(receipt) != keys or receipt.get("schema_version") != RELEASE_SCHEMA
            or receipt.get("producer_schema") != RELEASE_PRODUCER_SCHEMA
            or receipt.get("tool_id") != RELEASE_TOOL_ID
            or receipt.get("candidate_sha") != identity["candidate_sha"]
            or receipt.get("policy_identity") != plan["policy_identity"]
            or receipt.get("validation") != "passed"
            or not isinstance(receipt.get("owner_authorization_sha256"), str)
            or not DIGEST_RE.fullmatch(receipt["owner_authorization_sha256"])
            or not isinstance(receipt.get("authority_id"), str) or not receipt["authority_id"]
            or len(receipt["authority_id"].encode()) > plan["evidence_limits"]["max_string_bytes"]):
        raise RiskPolicyError("release gate receipt lacks identical-candidate policy authority")
    authorization = _bounded_object(value["authorization_path"],
                                    receipt["owner_authorization_sha256"], plan,
                                    "owner authorization")
    auth_keys = {"schema_version", "candidate_sha", "policy_identity", "owner_id",
                 "authorized_scopes"}
    if (set(authorization) != auth_keys
            or authorization.get("schema_version") != RELEASE_AUTH_SCHEMA
            or authorization.get("candidate_sha") != identity["candidate_sha"]
            or authorization.get("policy_identity") != plan["policy_identity"]
            or authorization.get("owner_id") != receipt["authority_id"]
            or authorization.get("authorized_scopes") != ["local_release"]):
        raise RiskPolicyError("release owner authorization provenance is invalid")
    return {**receipt, "receipt_path": str(Path(value["receipt_path"]).resolve()),
            "receipt_sha256": value["receipt_sha256"],
            "authorization_path": str(Path(value["authorization_path"]).resolve())}


def _revalidate_evidence_reviews(evidence: dict[str, Any], plan: dict[str, Any],
                                 candidate_sha: str) -> list[dict[str, Any]]:
    reviews = evidence.get("reviews")
    if (not isinstance(reviews, list)
            or not (plan["min_reviews"] <= len(reviews) <= plan["max_reviews"])):
        raise RiskPolicyError("candidate evidence review count violates current policy")
    rebuilt: list[dict[str, Any]] = []
    for index, review in enumerate(reviews):
        _validate_persisted_review(review, candidate_sha, index + 1,
                                   plan["reviewer_sequence"][index],
                                   plan["evidence_limits"]["max_string_bytes"])
        compact = _compact_review(
            {"runner_receipt_path": review["review_reference"]["receipt_path"],
             "runner_receipt_sha256": review["review_reference"]["receipt_sha256"]},
            plan["reviewer_sequence"][index], index + 1, candidate_sha,
            plan["policy_identity"], plan,
        )
        if compact != review:
            raise RiskPolicyError("candidate evidence review differs from source artifacts")
        rebuilt.append(compact)
    _validate_review_chain(rebuilt)
    if any(item["blocking_count"] for item in rebuilt):
        raise RiskPolicyError("candidate evidence contains blocking review findings")
    return rebuilt


def verify_candidate_evidence(policy: dict[str, Any], candidate_request: dict[str, Any],
                              flags: Any, evidence_reference: dict[str, Any]) -> dict[str, Any]:
    """Fresh Git + artifact verifier intended for merge-queue import."""
    plan = classify(policy, candidate_request, flags)
    if (not isinstance(evidence_reference, dict)
            or set(evidence_reference) != {"receipt_path", "receipt_sha256"}):
        raise RiskPolicyError("candidate evidence must be one exact canonical receipt reference")
    evidence = _bounded_object(evidence_reference.get("receipt_path"),
                               evidence_reference.get("receipt_sha256"), plan,
                               "candidate risk receipt")
    identity = plan["candidate"]
    if (set(evidence) != EVIDENCE_KEYS or evidence.get("schema_version") != EVIDENCE_SCHEMA
            or evidence.get("producer") != {"schema_version": EVIDENCE_PRODUCER_SCHEMA,
                                             "tool_id": EVIDENCE_TOOL_ID}
            or evidence.get("candidate") != identity
            or evidence.get("policy") != _policy_binding(plan)
            or evidence.get("post_cas") != plan["post_cas"]):
        raise RiskPolicyError("candidate evidence does not bind the fresh Git plan")
    if evidence.get("status") != "passed" or evidence.get("failure") is not None:
        return {"plan": plan, "eligible": False}
    validation = evidence.get("validation")
    reused = evidence.get("semantic_evidence_reused")
    expected_full_suite = (("reused" if reused is not None else "passed")
                           if plan["full_suite_required"] else "not_required")
    if (not isinstance(validation, dict)
            or set(validation) != {"affected", "full_suite", "full_suite_admission",
                                   "review_dispatches"}
            or validation.get("affected") != "passed"
            or validation.get("full_suite") != expected_full_suite
            or not isinstance(evidence.get("metrics"), dict)):
        raise RiskPolicyError("candidate evidence validation is incomplete")
    if plan["full_suite_required"]:
        receipt_candidate = identity
        if reused is not None:
            receipt_candidate = {"candidate_sha": reused.get("origin_candidate_sha"),
                                 "candidate_tree": identity["candidate_tree"]}
        verify_full_suite_admission_any(validation.get("full_suite_admission"), plan,
                                        candidate=receipt_candidate)
    elif validation.get("full_suite_admission") is not None:
        raise RiskPolicyError("full-suite receipt is forbidden when the suite is not required")
    _metrics(plan, evidence["metrics"])
    if reused is not None:
        if not _validate_previous(evidence, plan, identity):
            raise RiskPolicyError("reused candidate evidence does not match fresh Git identity")
        return {"plan": plan, "eligible": True}
    reviews = _revalidate_evidence_reviews(evidence, plan, identity["candidate_sha"])
    if validation["review_dispatches"] != len(reviews):
        raise RiskPolicyError("candidate evidence review dispatch count is inconsistent")
    if plan["release_gate_required"]:
        gate = evidence.get("release_gate")
        if not isinstance(gate, dict):
            raise RiskPolicyError("candidate evidence release authority is missing")
        gate_input = {key: gate.get(key) for key in
                      ("receipt_path", "receipt_sha256", "authorization_path")}
        if _release_gate(gate_input, plan, identity) != gate:
            raise RiskPolicyError("candidate evidence release authority drifted")
    elif evidence.get("release_gate") is not None:
        raise RiskPolicyError("non-release candidate evidence contains release authority")
    return {"plan": plan, "eligible": True}


def _evidence(plan: dict[str, Any], identity: dict[str, str], metrics: dict[str, int],
              *, status: str, failure: str | None, affected: str, full_suite: str,
              full_suite_admission: dict[str, Any] | None = None,
              reviews: list[dict[str, Any]] | None = None,
              release_gate: dict[str, Any] | None = None,
              reused: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"schema_version": EVIDENCE_SCHEMA,
            "producer": {"schema_version": EVIDENCE_PRODUCER_SCHEMA,
                         "tool_id": EVIDENCE_TOOL_ID},
            "created_at": utc_now(), "status": status,
            "failure": failure, "candidate": identity, "policy": _policy_binding(plan),
            "validation": {"affected": affected, "full_suite": full_suite,
                           "full_suite_admission": full_suite_admission,
                           "review_dispatches": len(reviews or [])},
            "reviews": reviews or [], "release_gate": release_gate, "metrics": metrics,
            "post_cas": plan["post_cas"], "semantic_evidence_reused": reused}


def finalize(plan: dict[str, Any], candidate_request: dict[str, Any], *, affected_tests_passed: bool,
             full_suite_admission: Any, reviews: Any, metrics: Any,
             previous: dict[str, Any] | None = None,
             release_gate: Any = None, policy: dict[str, Any]) -> dict[str, Any]:
    _policy_binding(plan)
    if digest(policy) != plan["policy_identity"]:
        raise RiskPolicyError("risk plan does not bind the current full policy identity")
    try:
        expected_plan = classify(policy, candidate_request, plan["flags"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RiskPolicyError("current risk policy cannot reproduce the plan") from exc
    if expected_plan != plan:
        raise RiskPolicyError("risk plan fields drifted from the current full policy")
    if not isinstance(affected_tests_passed, bool):
        raise RiskPolicyError("affected validation outcome must be a strict boolean")
    repository, candidate_sha, target_ref, expected_target_sha = _candidate_request(candidate_request)
    identity = candidate_identity(repository, candidate_sha, target_ref, expected_target_sha)
    if identity != plan["candidate"]:
        raise RiskPolicyError("risk plan no longer binds the frozen Git candidate")
    compact_metrics = _metrics(plan, metrics)
    if not affected_tests_passed:
        return _evidence(plan, identity, compact_metrics, status="failed",
                         failure="affected_validation_failed", affected="failed",
                         full_suite="not_run")
    previous_sha = None
    if previous is not None:
        if not isinstance(previous, dict) or set(previous) != {"receipt_path", "receipt_sha256"}:
            raise RiskPolicyError("previous evidence must be one exact canonical receipt reference")
        previous_sha = previous.get("receipt_sha256")
        previous = _bounded_object(previous.get("receipt_path"), previous_sha, plan,
                                   "previous risk receipt")
    reusable = _validate_previous(previous, plan, identity)
    suite_admission = None
    if plan["full_suite_required"] and full_suite_admission is not None:
        suite_admission = verify_full_suite_admission_any(full_suite_admission, plan)
    elif not plan["full_suite_required"] and full_suite_admission is not None:
        raise RiskPolicyError("full-suite receipt is forbidden when the suite is not required")
    if plan["full_suite_required"] and suite_admission is None and not reusable:
        return _evidence(plan, identity, compact_metrics, status="failed",
                         failure="full_suite_failed_or_missing", affected="passed",
                         full_suite="missing")
    if reusable:
        prior = previous["candidate"]
        prior_reuse = previous["semantic_evidence_reused"]
        origin_reviews = (prior_reuse["origin_reviews"] if prior_reuse is not None
                          else previous["reviews"])
        origin_candidate = (prior_reuse["origin_candidate_sha"] if prior_reuse is not None
                            else prior["candidate_sha"])
        return _evidence(
            plan, identity, compact_metrics, status="passed", failure=None, affected="passed",
            full_suite="reused" if plan["full_suite_required"] else "not_required",
            full_suite_admission=(previous["validation"]["full_suite_admission"]
                                  if plan["full_suite_required"] else None),
            reused={"reason": "product_and_composition_bytes_identical",
                    "source_candidate_sha": prior["candidate_sha"],
                    "source_evidence_sha256": previous_sha,
                    "origin_candidate_sha": origin_candidate,
                    "origin_reviews": origin_reviews},
        )
    if not isinstance(reviews, list) or len(reviews) > plan["max_reviews"]:
        raise RiskPolicyError("review count violates the deterministic policy bounds")
    compact_reviews: list[dict[str, Any]] = []
    for index, review in enumerate(reviews):
        compact_reviews.append(_compact_review(
            review, plan["reviewer_sequence"][index], index + 1,
            identity["candidate_sha"], plan["policy_identity"], plan,
        ))
        _validate_review_chain(compact_reviews)
        if compact_reviews[-1]["blocking_count"]:
            return _evidence(plan, identity, compact_metrics, status="failed",
                             failure="review_findings", affected="passed",
                             full_suite="passed" if plan["full_suite_required"] else "not_required",
                             full_suite_admission=suite_admission,
                             reviews=compact_reviews)
    if len(compact_reviews) < plan["min_reviews"]:
        raise RiskPolicyError("review count violates the deterministic policy bounds")
    gate = None
    if plan["release_gate_required"]:
        gate = _release_gate(release_gate, plan, identity)
    elif release_gate is not None:
        raise RiskPolicyError("release gate evidence is forbidden for a non-release plan")
    return _evidence(plan, identity, compact_metrics, status="passed", failure=None,
                     affected="passed",
                     full_suite="passed" if plan["full_suite_required"] else "not_required",
                     full_suite_admission=suite_admission,
                     reviews=compact_reviews, release_gate=gate)


def atomic_receipt(path: Path, value: dict[str, Any], policy: dict[str, Any]) -> None:
    data = canonical(value)
    if len(data) > policy["limits"]["max_receipt_bytes"]:
        raise RiskPolicyError("compact evidence exceeds receipt size limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix="." + path.name + ".", delete=False) as out:
        out.write(data); out.flush(); os.fsync(out.fileno()); temporary = Path(out.name)
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--target-ref", required=True)
    parser.add_argument("--expected-target-sha", required=True)
    parser.add_argument("--flag", action="append", default=[])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        policy = load_policy(args.policy)
        value = classify(policy, {"repository": str(args.repository),
                                  "candidate_sha": args.candidate_sha,
                                  "target_ref": args.target_ref,
                                  "expected_target_sha": args.expected_target_sha}, args.flag)
        if args.output: atomic_receipt(args.output, value, policy)
        else: print(json.dumps(value, sort_keys=True))
        return 0
    except RiskPolicyError as exc:
        print(f"risk_policy.py: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
