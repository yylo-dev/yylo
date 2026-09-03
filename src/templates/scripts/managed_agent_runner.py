#!/usr/bin/env python3
"""Canonical foreground launcher for managed worker and reviewer agents."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import selectors
import shlex
import shutil
import signal
import subprocess
import sys

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from invocation_correlation import child_invocation_environment
import tempfile
import time
from typing import Any

SCHEMA = "juno_managed_agent_runner.v1"
REVIEW_BINDING_SCHEMA = "juno_managed_review_binding.v1"
REVIEW_RESULT_SCHEMA = "juno_managed_review_result.v3"
ADMITTED_SCOPE_CLASSIFICATIONS = {
    "requirement_gap", "candidate_bug", "candidate_regression",
    "safety_invariant_violation"}
REJECTED_OBSERVATION_CLASSES = {
    "enhancement", "scope_addition", "design_preference",
    "speculative_hardening", "unrelated_preexisting"}
MAX_REJECTED_OBSERVATIONS = 64
# Provider footers/logs may trail one valid structured result. The raw capture
# gets a bounded tolerance; the extracted canonical result stays strictly
# bounded by REVIEW_RESULT_LIMIT so footers can neither discard a valid
# exhaustive review nor smuggle an unbounded one.
REVIEW_RESULT_LIMIT = 65536
REVIEW_RAW_CAPTURE_LIMIT = 1024 * 1024
REVIEW_FINDING_IMPACT_CATEGORIES = {
    "bounded_product_defect", "maintainability", "clarity",
    "supported_install", "supported_runtime", "supported_config",
    "core_contract", "product_breaking", "security_privacy",
    "destructive_data_loss",
}
BLOCKING_REVIEW_IMPACTS = {
    "supported_install", "supported_runtime", "supported_config",
    "core_contract", "product_breaking", "security_privacy",
    "destructive_data_loss",
}
TERMINAL_RESULT_SCHEMA = "juno_managed_agent_terminal_result.v1"
TERMINAL_STATES = {"completed", "blocked", "incomplete", "failed"}
QUEUE_STATE_PATH = ".juno_task/state/tasks.json"
QUEUE_RECEIPT_ROOT = ".juno_task/state/merge-queue/"
CAPTURE_LIMIT = 4 * 1024 * 1024
TASK_RE = __import__("re").compile(r"[A-Za-z0-9_-]{1,64}\Z")
SHA_RE = __import__("re").compile(r"[0-9a-f]{40}\Z")
CANONICAL_METADATA_WORKSPACE = {
    "mode": "metadata-only", "policy": ".juno_task/config/metadata-controller.json"}
CANONICAL_SPARSE_WORKSPACE = {
    "enabled": True, "policy": ".juno_task/config/controller-workspace.json"}
# Exact committed policy bytes for the one pre-agent-surface migration
# generation. Current policies remain admitted by the general validator below;
# this immutable exception exists only to let that controller review its update.
LEGACY_METADATA_POLICY = b'''{
  "schema_version": "juno_metadata_controller_policy.v1",
  "controller_branch": "refs/heads/juno/controller-metadata-2.1",
  "product_ref": "refs/heads/juno-mono-002",
  "spec_copy_mode": "top_level_files_only",
  "copied_metadata": [
    ".juno_task/cutover.json",
    ".juno_task/config/umbrella-admissions",
    ".juno_task/ledger",
    ".juno_task/wiki",
    ".juno_task/specs",
    ".juno_task/task-scopes",
    ".juno_task/tasks",
    ".juno_task/tasks.md"
  ],
  "generated_metadata": [
    ".gitignore",
    ".juno_task/config.json",
    ".juno_task/config/metadata-controller.json",
    ".juno_task/config/task-workspace.json",
    ".juno_task/config/integration-workspace.json",
    ".juno_task/config/risk-policy.json",
    ".juno_task/receipts/controller-boundary.json",
    ".juno_task/state/tasks.json",
    ".juno_task/wiki/controller/git_worktree_lifecycle.md",
    ".juno_task/wiki/controller/metadata_controller_boundary.md",
    ".juno_task/wiki/controller/parallel_runner_and_spec_review.md",
    ".juno_task/wiki/controller/runtime_migration_and_replacement_contract.md",
    ".juno_task/wiki/controller/task_dependency_hydration.md",
    ".juno_task/wiki/controller/tmux_best_practices.md",
    ".juno_task/wiki/controller/wiki_maintenance.md",
    ".juno_task/wiki/controller/yy_pi_progress.md"
  ],
  "product_forbidden": [
    ".juno_task/artifacts",
    ".juno_task/config/umbrella-admissions",
    ".juno_task/cutover.json",
    ".juno_task/ledger",
    ".juno_task/logs",
    ".juno_task/receipts",
    ".juno_task/specs",
    ".juno_task/state",
    ".juno_task/task-scopes",
    ".juno_task/tasks",
    ".juno_task/tasks.md",
    ".juno_task/workflows",
    ".juno_task/wiki"
  ],
  "tracked_exact": [
    ".gitignore",
    ".juno_task/config.json",
    ".juno_task/cutover.json",
    ".juno_task/config/metadata-controller.json",
    ".juno_task/config/task-workspace.json",
    ".juno_task/config/integration-workspace.json",
    ".juno_task/config/risk-policy.json",
    ".juno_task/receipts/controller-boundary.json",
    ".juno_task/state/tasks.json",
    ".juno_task/tasks.md"
  ],
  "tracked_recursive": [
    ".juno_task/config/umbrella-admissions",
    ".juno_task/ledger",
    ".juno_task/task-scopes",
    ".juno_task/tasks",
    ".juno_task/wiki"
  ],
  "tracked_top_level_files": [
    ".juno_task/receipts",
    ".juno_task/specs"
  ],
  "runtime": {
    "package": "@yylo/cli",
    "identity_file": ".juno_task/runtime/identity.json",
    "ignored_roots": [
      ".juno_task/runtime",
      ".juno_task/scripts",
      ".venv_juno",
      ".env.yylo"
    ]
  }
}
'''
LEGACY_METADATA_POLICY_SHA256 = "ad2c1214fe89c67bd8d3e9646d939c4199b585c1d5ccbffdaf1ef87c43236693"
LEGACY_METADATA_CONTROLLER_BRANCH = "refs/heads/juno/controller-metadata-2.1"
LEGACY_METADATA_PRODUCT_REF = "refs/heads/juno-mono-002"


class RunnerError(RuntimeError):
    pass


def _exact_json_island(data: bytes) -> Optional[dict[str, Any]]:
    """Extract one unambiguous verdict JSON object from prose-wrapped output.

    Reviewers sometimes wrap the exact verdict object in explanatory prose or
    code fences. A format-only loss must not burn a review attempt, but only
    an unambiguous island binds: exactly one candidate object carrying the
    complete required key set. Anything else fails closed to exact parsing.
    """
    text = data.decode("utf-8", errors="replace")
    required = {"schema_version", "candidate_sha", "policy_identity", "reviewer_role",
                "sequence", "verdict", "truncated", "omitted_finding_count",
                "rejection_counters", "findings"}
    decoder = json.JSONDecoder()
    candidates: list[dict[str, Any]] = []
    for index in (i for i, ch in enumerate(text) if ch == "{"):
        try:
            value, _end = decoder.raw_decode(text, index)
        except (ValueError, json.JSONDecodeError):
            continue
        if isinstance(value, dict) and required <= set(value):
            candidates.append(value)
    if len(candidates) == 1:
        return candidates[0]
    return None


def structured_review_result(data: bytes, binding: dict[str, Any]) -> dict[str, Any]:
    if not data or len(data) > REVIEW_RAW_CAPTURE_LIMIT:
        raise RunnerError("structured review result is empty or unbounded")
    try: value = json.loads(data)
    except (UnicodeError, json.JSONDecodeError):
        # Format-only tolerance: one unambiguous JSON island wrapped in prose or
        # trailed by provider footer/log bytes binds; zero or multiple islands
        # stay an exact-JSON failure.
        value = _exact_json_island(data)
        if value is None:
            raise RunnerError("structured review result is not exact JSON") from None
    if len(canonical(value)) > REVIEW_RESULT_LIMIT:
        raise RunnerError("structured review result exceeds the bounded capture contract")
    keys = {"schema_version", "candidate_sha", "policy_identity", "reviewer_role",
            "sequence", "verdict", "truncated", "omitted_finding_count",
            "rejection_counters", "findings"}
    if (not isinstance(value, dict) or set(value) != keys
            or value.get("schema_version") != REVIEW_RESULT_SCHEMA
            or value.get("candidate_sha") != binding.get("candidate_sha")
            or value.get("policy_identity") != binding.get("policy_identity")
            or value.get("reviewer_role") != binding.get("reviewer_role")
            or not isinstance(value.get("sequence"), int)
            or isinstance(value.get("sequence"), bool)
            or value.get("sequence") != binding.get("sequence")
            or value.get("verdict") not in {"pass", "findings"}
            or not isinstance(value.get("truncated"), bool)
            or not isinstance(value.get("omitted_finding_count"), int)
            or isinstance(value.get("omitted_finding_count"), bool)
            or value["omitted_finding_count"] < 0
            or value["truncated"] != (value["omitted_finding_count"] > 0)
            or not isinstance(value.get("findings"), list) or len(value["findings"]) > 32):
        raise RunnerError("structured review result schema/binding is invalid")
    finding_keys = {"code", "severity", "summary", "paths", "symbols", "evidence",
                    "impact", "failure_condition", "acceptance_condition",
                    "impact_categories", "scope_classification", "cited_contract"}
    for finding in value["findings"]:
        if not isinstance(finding, dict):
            raise RunnerError("structured review finding is malformed or unbounded")
        bounded_lists = (isinstance(finding.get("paths"), list)
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
        if (set(finding) != finding_keys
                or not isinstance(finding.get("code"), str) or not finding["code"]
                or finding.get("severity") not in {"low", "medium", "high", "critical"}
                or finding.get("scope_classification") not in ADMITTED_SCOPE_CLASSIFICATIONS
                or not isinstance(finding.get("cited_contract"), str)
                or not finding["cited_contract"]
                or len(finding["cited_contract"].encode()) > 1024
                or not bounded_lists or any(not isinstance(finding.get(field), str)
                    or not finding[field] or len(finding[field].encode()) > 1024
                    for field in ("summary", "evidence", "impact", "failure_condition",
                                  "acceptance_condition"))
                or len(finding["code"].encode()) > 64):
            raise RunnerError("structured review finding is malformed or unbounded")
    counters = value.get("rejection_counters")
    if (not isinstance(counters, dict)
            or set(counters) - REJECTED_OBSERVATION_CLASSES
            or any(not isinstance(count, int) or isinstance(count, bool) or count < 0
                   for count in counters.values())
            or sum(counters.values()) > MAX_REJECTED_OBSERVATIONS):
        raise RunnerError("structured review rejection counters are malformed or unbounded")
    if (value["verdict"] == "pass") != (not value["findings"]):
        raise RunnerError("structured review verdict/findings are contradictory")
    return value


def receipt_review_result(receipt: dict[str, Any], binding: dict[str, Any]) -> dict[str, Any]:
    artifacts = receipt.get("artifacts")
    response = artifacts.get("response") if isinstance(artifacts, dict) else None
    if (not isinstance(response, dict) or set(response) != {"path", "bytes", "sha256"}
            or not isinstance(response.get("path"), str)
            or not isinstance(response.get("bytes"), int) or isinstance(response.get("bytes"), bool)
            or not isinstance(response.get("sha256"), str)
            or not __import__("re").fullmatch(r"[0-9a-f]{64}", response["sha256"])):
        raise RunnerError("predecessor response artifact evidence is missing")
    try: data = Path(response["path"]).read_bytes()
    except OSError as exc: raise RunnerError("predecessor response artifact is missing") from exc
    if sha(data) != response["sha256"] or len(data) != response.get("bytes"):
        raise RunnerError("predecessor response artifact digest/content mismatch")
    return structured_review_result(data, binding)


def review_prompt_contract(binding: dict[str, Any]) -> bytes:
    contract = {"schema_version": REVIEW_RESULT_SCHEMA,
                "candidate_sha": binding["candidate_sha"],
                "policy_identity": binding["policy_identity"],
                "reviewer_role": binding["reviewer_role"], "sequence": binding["sequence"],
                "verdict": "pass", "truncated": False, "omitted_finding_count": 0,
                "rejection_counters": {}, "findings": []}
    return ("\n\n# Managed structured review output\n"
            "Review the complete frozen candidate before answering. Return every independently "
            "actionable ADMITTED finding; do not stop after the first or after a blocker. "
            "Return exactly one JSON object with these top-level fields and no unknown fields: "
            "schema_version, candidate_sha, policy_identity, reviewer_role, sequence, verdict, "
            "truncated, omitted_finding_count, rejection_counters, findings.\n"
            "Scope admission precedes severity. Admit a finding only after proving all four: "
            "Contract (cite the exact approved requirement, acceptance condition, invariant, or "
            "established supported behavior violated), Causality (concrete frozen-candidate "
            "evidence or a candidate-caused regression), Failure (a realistic supported failure "
            "condition), and Impact (the observable product/user consequence). Request only the "
            "smallest repair that restores the cited contract.\n"
            "Each finding must contain exactly {code, severity, summary, paths, symbols, evidence, "
            "impact, failure_condition, acceptance_condition, impact_categories, "
            "scope_classification, cited_contract}. code is its stable "
            "finding ID. severity is low|medium|high|critical. scope_classification is exactly one "
            "of requirement_gap|candidate_bug|candidate_regression|safety_invariant_violation. "
            "cited_contract states the exact requirement, acceptance condition, invariant, or "
            "established supported behavior the finding violates. impact_categories contains 1-4 "
            "of: "
            + ", ".join(sorted(REVIEW_FINDING_IMPACT_CATEGORIES)) + ". paths contains 1-16 entries; "
            "symbols contains 0-16 entries; findings contains at most 32 items. Combine duplicate "
            "symptoms sharing one root cause and keep independently repairable defects separate.\n"
            "An observation that is only a new feature, broader acceptance criterion, architecture "
            "or naming/style preference, refactor, speculative future hardening, an unrelated "
            "pre-existing defect, or an improvement not required by an approved metric is OUT OF "
            "SCOPE: never report it as a finding, never downgrade it to low or medium severity, "
            "and record it only as one increment in rejection_counters, whose keys are exactly "
            "enhancement|scope_addition|design_preference|speculative_hardening|"
            "unrelated_preexisting with integer counts and a total of at most 64. Include no prose "
            "for rejected observations.\n"
            "Set truncated=true and omitted_finding_count>0 whenever the bound prevents a complete "
            "response; never represent a partial review as PASS. Set both to false/0 otherwise. "
            "PASS is allowed only after complete inspection with findings=[] (rejection_counters "
            "may still carry counts). A findings verdict has "
            "1-32 items. Emit no markdown, fences, commentary, provider usage footers, or text "
            "outside the JSON object. "
            "Example PASS object:\n" + canonical(contract).decode()).encode()


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix="." + path.name + ".", delete=False) as out:
        out.write(data); out.flush(); os.fsync(out.fileno()); temporary = Path(out.name)
    os.replace(temporary, path)


def atomic_json(path: Path, value: Any) -> None:
    atomic_bytes(path, canonical(value))


def atomic_idempotent_bytes(path: Path, data: bytes, label: str) -> None:
    """Publish immutable bytes once; concurrent exact publication is a no-op."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix="." + path.name + ".", delete=False) as out:
        out.write(data); out.flush(); os.fsync(out.fileno()); temporary = Path(out.name)
    try:
        try:
            os.link(temporary, path)
        except FileExistsError:
            try: current = path.read_bytes()
            except OSError as exc: raise RunnerError(f"{label} publication is ambiguous") from exc
            if current != data:
                raise RunnerError(f"{label} publication conflicts with existing bytes")
    finally:
        temporary.unlink(missing_ok=True)


def atomic_idempotent_json(path: Path, value: Any, label: str) -> None:
    atomic_idempotent_bytes(path, canonical(value), label)


def load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        if path.stat().st_size > CAPTURE_LIMIT:
            raise RunnerError(f"{label} exceeds size limit")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RunnerError(f"{label} is missing or malformed") from exc
    if not isinstance(value, dict):
        raise RunnerError(f"{label} must be one JSON object")
    return value


def review_binding(args: argparse.Namespace, identity: dict[str, Any]) -> dict[str, Any] | None:
    if not args.review_binding:
        return None
    path = Path(args.review_binding).resolve()
    value = load_object(path, "review binding")
    if path.read_bytes() != canonical(value):
        raise RunnerError("review binding must use canonical JSON bytes")
    keys = {"schema_version", "candidate_sha", "policy_identity", "reviewer_role",
            "sequence", "predecessor"}
    if (set(value) != keys or value.get("schema_version") != REVIEW_BINDING_SCHEMA
            or value.get("candidate_sha") != identity.get("candidate_sha")
            or not isinstance(value.get("policy_identity"), str)
            or not __import__("re").fullmatch(r"[0-9a-f]{64}", value["policy_identity"])
            or value.get("reviewer_role") not in {"reviewer", "reviewer_a", "reviewer_b"}
            or value.get("sequence") not in {1, 2}):
        raise RunnerError("review binding schema or frozen identity is invalid")
    role, sequence, predecessor = value["reviewer_role"], value["sequence"], value["predecessor"]
    if role in {"reviewer", "reviewer_a"}:
        if sequence != 1 or predecessor is not None:
            raise RunnerError("first reviewer binding requires sequence 1 and no predecessor")
        normalized_predecessor = None
    else:
        if sequence != 2 or not isinstance(predecessor, dict) or set(predecessor) != {
                "receipt_path", "receipt_sha256"}:
            raise RunnerError("Reviewer B binding requires one exact Reviewer A predecessor")
        receipt_path = Path(str(predecessor["receipt_path"])).resolve()
        if not isinstance(predecessor["receipt_sha256"], str) or not __import__("re").fullmatch(
                r"[0-9a-f]{64}", predecessor["receipt_sha256"]):
            raise RunnerError("predecessor receipt digest is invalid")
        try: receipt_bytes = receipt_path.read_bytes()
        except OSError as exc: raise RunnerError("predecessor receipt is missing") from exc
        if len(receipt_bytes) > CAPTURE_LIMIT or sha(receipt_bytes) != predecessor["receipt_sha256"]:
            raise RunnerError("predecessor receipt digest/content mismatch")
        prior = load_object(receipt_path, "predecessor receipt")
        prior_binding = prior.get("review_binding")
        prior_binding_body = ({k: v for k, v in prior_binding.items() if k != "binding_sha256"}
                              if isinstance(prior_binding, dict) else {})
        if (prior.get("schema_version") != SCHEMA or prior.get("mode") != "reviewer"
                or prior.get("state") != "succeeded" or prior.get("semantic_outcome") != "completed"
                or not isinstance(prior_binding, dict)
                or prior_binding.get("schema_version") != REVIEW_BINDING_SCHEMA
                or prior_binding.get("candidate_sha") != value["candidate_sha"]
                or prior_binding.get("policy_identity") != value["policy_identity"]
                or prior_binding.get("reviewer_role") != "reviewer_a"
                or prior_binding.get("sequence") != 1
                or prior_binding.get("predecessor") is not None
                or set(prior_binding) != {"schema_version", "candidate_sha", "policy_identity",
                                          "reviewer_role", "sequence", "predecessor",
                                          "binding_sha256"}
                or prior_binding.get("binding_sha256") != sha(canonical(prior_binding_body))
                or not isinstance(prior.get("tool_id"), str) or prior["tool_id"] == args.tool_id
                or not isinstance(prior.get("session_id"), str) or not prior["session_id"]
                or not isinstance(prior.get("completed_at"), str)):
            raise RunnerError("predecessor is not a canonical Reviewer A receipt")
        prior_result = receipt_review_result(prior, prior_binding)
        if (prior_result.get("truncated")
                or any(set(finding["impact_categories"]) & BLOCKING_REVIEW_IMPACTS
                       for finding in prior_result["findings"])):
            raise RunnerError("Reviewer B requires Reviewer A to have no blocking finding")
        normalized_predecessor = {
            "receipt_sha256": predecessor["receipt_sha256"], "tool_id": prior["tool_id"],
            "session_id": prior["session_id"], "completed_at": prior["completed_at"],
            "binding_sha256": prior_binding.get("binding_sha256"),
        }
    normalized = {"schema_version": REVIEW_BINDING_SCHEMA,
                  "candidate_sha": value["candidate_sha"],
                  "policy_identity": value["policy_identity"], "reviewer_role": role,
                  "sequence": sequence, "predecessor": normalized_predecessor}
    normalized["binding_sha256"] = sha(canonical(normalized))
    return normalized


def git(root: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if check and result.returncode:
        raise RunnerError(f"git identity check failed: {shlex.join(args)}")
    return result.stdout.strip()


def fingerprint(root: Path) -> dict[str, Any]:
    return {"root": str(root), "head": git(root, "rev-parse", "HEAD"),
            "branch_ref": git(root, "symbolic-ref", "-q", "HEAD", check=False),
            "git_common_dir": str((root / git(root, "rev-parse", "--git-common-dir")).resolve()),
            "status": git(root, "status", "--porcelain=v2", "--untracked-files=all"),
            "index_sha256": sha(git(root, "ls-files", "--stage").encode())}


def resolver_policy_passes(result: subprocess.CompletedProcess[str], resolved: Any,
                           workspace: Any, queue_state_bound: bool) -> bool:
    if not isinstance(resolved, dict) or not isinstance(workspace, dict) \
            or not isinstance(workspace.get("checks"), dict):
        return False
    if result.returncode == 0:
        return resolved.get("valid") is True and workspace.get("passed") is True
    failed = sorted(name for name, passed in workspace["checks"].items() if passed is not True)
    expected = "canonical sparse controller policy refused: clean"
    return (queue_state_bound and result.returncode == 2
            and result.stderr.strip() == "controller-resolver: " + expected
            and resolved.get("valid") is False
            and resolved.get("diagnostics") == [expected]
            and workspace.get("passed") is False and failed == ["clean"])


def legacy_metadata_controller_policy(data: bytes) -> dict[str, Any]:
    """Recognize the one receipt-bound policy that predates agent-surface roots."""
    try:
        value = json.loads(data)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("legacy policy is not JSON") from exc
    if (not isinstance(value, dict) or data != LEGACY_METADATA_POLICY
            or value.get("schema_version") != "juno_metadata_controller_policy.v1"
            or value.get("controller_branch") != LEGACY_METADATA_CONTROLLER_BRANCH
            or value.get("product_ref") != LEGACY_METADATA_PRODUCT_REF):
        raise ValueError("policy is not the exact supported legacy generation")
    return value


def metadata_controller_policy_identity(root: Path, branch_ref: str) -> dict[str, str]:
    policy_path = root / CANONICAL_METADATA_WORKSPACE["policy"]
    try:
        import importlib.util
        if policy_path.is_symlink() or not policy_path.is_file():
            raise OSError("policy must be one regular non-symlink file")
        validator_path = Path(__file__).resolve().with_name("metadata_controller.py")
        spec = importlib.util.spec_from_file_location(
            "juno_managed_metadata_controller_validator", validator_path)
        if spec is None or spec.loader is None:
            raise ImportError("validator loader is unavailable")
        validator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(validator)
        policy_bytes = policy_path.read_bytes()
        if not policy_bytes or len(policy_bytes) > CAPTURE_LIMIT:
            raise ValueError("policy bytes are empty or unbounded")
        try:
            policy = validator.load_policy(policy_path)
        except Exception as exc:
            # Only the trusted validator's boundary refusal may enter the exact
            # legacy recognizer. Import/runtime failures remain terminal.
            if type(exc).__name__ != "BoundaryError":
                raise
            policy = legacy_metadata_controller_policy(policy_bytes)
        # The metadata-controller validator owns semantic canonicality. Policy
        # files are tracked operator-readable configuration and may use either
        # pretty or compact JSON; bind the exact validated bytes in the launch
        # identity instead of imposing receipt-style serialization on them.
    except (ImportError, OSError, UnicodeError, ValueError) as exc:
        raise RunnerError("canonical metadata controller policy is missing or malformed") from exc
    except Exception as exc:
        if type(exc).__name__ != "BoundaryError":
            raise
        raise RunnerError("canonical metadata controller policy is missing or malformed") from exc
    if policy.get("controller_branch") != branch_ref:
        raise RunnerError("canonical metadata controller policy branch mismatch")
    return {"schema_version": str(policy["schema_version"]),
            "policy_sha256": sha(policy_bytes),
            "controller_branch": str(policy["controller_branch"])}


def controller_identity(root: Path) -> dict[str, Any]:
    mark: dict[str, Any] = fingerprint(root)
    config = root / ".juno_task/config.json"
    if not config.is_file():
        raise RunnerError("controller is missing its config or is dirty")
    if mark["status"]:
        unstaged = sorted(filter(None, git(root, "diff", "--name-only").splitlines()))
        staged = sorted(filter(None, git(root, "diff", "--cached", "--name-only").splitlines()))
        untracked = sorted(filter(None, git(
            root, "ls-files", "--others", "--exclude-standard").splitlines()))
        dirty_paths = sorted(set(unstaged + untracked))
        allowed = all(path == QUEUE_STATE_PATH or path.startswith(QUEUE_RECEIPT_ROOT)
                      for path in dirty_paths)
        files = [root / path for path in dirty_paths]
        if (not dirty_paths or staged or not allowed
                or any(path.is_symlink() or not path.is_file() for path in files)):
            raise RunnerError("controller is missing its config or is dirty")
        # The merge queue must durably publish REVIEWING before dispatch.  Bind
        # that one queue-owned worktree change so it may be dirty but cannot
        # mutate while the managed agent is running.
        mark["queue_state"] = [
            {"path": relative, "sha256": sha(path.read_bytes())}
            for relative, path in zip(dirty_paths, files)
        ]
    mark["config_sha256"] = sha(config.read_bytes())
    resolver = root / ".juno_task/scripts/controller_resolver.py"
    if resolver.is_file():
        controller_config = load_object(config, "controller config")
        configured_workspace = controller_config.get("controllerWorkspace")
        resolver_env = {k: v for k, v in os.environ.items() if not k.startswith(("PI_", "JUNO_")) and k != "TASK_ROOT"}
        resolver_env.update({"JUNO_TASK_ROOT": str(root), "JUNO_WORKSPACE_ROLE": "controller", "JUNO_WORKSPACE_ENFORCEMENT": "strict"})
        result = subprocess.run([sys.executable, str(resolver), "--cwd", str(root), "--operation", "orchestration"],
                                cwd=root, env=resolver_env, stdin=subprocess.DEVNULL, capture_output=True, text=True)
        try: resolved = json.loads(result.stdout)
        except json.JSONDecodeError: resolved = {}
        workspace = resolved.get("controller_workspace") if isinstance(resolved, dict) else None
        queue_state_bound = bool(mark.get("queue_state"))
        resolver_base_passes = (
            isinstance(resolved, dict)
            and Path(str(resolved.get("path"))).resolve() == root
            and resolved.get("role") == "controller")
        if configured_workspace == CANONICAL_METADATA_WORKSPACE:
            accepted = (resolver_base_passes and result.returncode == 0
                        and resolved.get("valid") is True
                        and resolved.get("diagnostics") == [] and workspace is None)
            policy_identity = (metadata_controller_policy_identity(root, mark["branch_ref"])
                               if accepted else None)
        elif configured_workspace == CANONICAL_SPARSE_WORKSPACE:
            accepted = (resolver_base_passes
                        and (root / CANONICAL_SPARSE_WORKSPACE["policy"]).is_file()
                        and resolver_policy_passes(
                            result, resolved, workspace, queue_state_bound))
            policy_identity = workspace.get("policy_identity") if accepted else None
        else:
            accepted = False
            policy_identity = None
        if not accepted or not isinstance(policy_identity, dict) or not policy_identity:
            raise RunnerError("canonical controller resolver/policy refused launch")
        mark["resolver"] = {"source": resolved.get("source"), "role": resolved.get("role"),
                            "policy_identity": policy_identity,
                            "passed": True, "queue_state_bound": queue_state_bound}
    return mark


def safe_out_dir(path: Path) -> Path:
    path = path.resolve()
    path.mkdir(parents=True, exist_ok=True)
    allowed = {"session_metadata"}
    existing = {x.name for x in path.iterdir()}
    if existing - allowed:
        raise RunnerError("output directory is not exclusively owned by this run")
    if path.is_symlink() or (path / ".git").exists() or (path / ".juno_task").exists():
        raise RunnerError("output directory must be neutral")
    return path


def evidence(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {"path": str(path.resolve()), "bytes": len(data), "sha256": sha(data)}


def configured_file_evidence(path: Path, lexical_path: Path, label: str) -> dict[str, Any]:
    try:
        lexical_stat = lexical_path.lstat()
        resolved_stat = path.stat()
        if not path.is_file():
            raise RunnerError(f"{label} is not a regular file")
        data = path.read_bytes()
    except OSError as exc:
        raise RunnerError(f"{label} is missing or unreadable") from exc
    if len(data) > CAPTURE_LIMIT:
        raise RunnerError(f"{label} exceeds size limit")
    return {"lexical_path": str(lexical_path), "resolved_path": str(path), "bytes": len(data),
            "sha256": sha(data), "lexical_mode": lexical_stat.st_mode,
            "lexical_mtime_ns": lexical_stat.st_mtime_ns, "resolved_device": resolved_stat.st_dev,
            "resolved_inode": resolved_stat.st_ino, "resolved_mtime_ns": resolved_stat.st_mtime_ns,
            "symlink_target": os.readlink(lexical_path) if lexical_path.is_symlink() else None}


def resolve_configured_file(controller_root: Path, raw: Any, label: str) -> tuple[str, dict[str, Any]]:
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise RunnerError(f"{label} has malformed path form")
    supplied = Path(raw)
    if supplied.is_absolute():
        lexical = Path(os.path.abspath(supplied))
    else:
        lexical = Path(os.path.abspath(controller_root / supplied))
        try:
            lexical.relative_to(controller_root)
        except ValueError as exc:
            raise RunnerError(f"{label} traverses outside controller root") from exc
    resolved = lexical.resolve(strict=False)
    file_mark = configured_file_evidence(resolved, lexical, label)
    return str(lexical), {"setting": label, "configured_path": raw, **file_mark}


def derive_compatible_config(controller_root: Path, out: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    source = controller_root / ".juno_task/config.json"
    source_mark = configured_file_evidence(source.resolve(strict=False), source, "controller config")
    config = load_object(source, "controller config")
    mappings: list[dict[str, Any]] = []
    transformations: list[dict[str, str]] = []
    workspace = config.get("controllerWorkspace")
    if (isinstance(workspace, dict) and workspace.get("enabled") is True
            and workspace.get("policy") == ".juno_task/config/controller-workspace.json"):
        config["controllerWorkspace"] = {
            "mode": "metadata-only",
            "policy": ".juno_task/config/metadata-controller.json",
        }
        transformations.append({
            "setting": "controllerWorkspace",
            "reason": "neutral managed child compatibility",
            "source_contract": "canonical-sparse",
            "derived_contract": "metadata-only",
        })
    if "envFilePath" in config:
        config["envFilePath"], mark = resolve_configured_file(controller_root, config["envFilePath"], "envFilePath")
        mappings.append(mark)
    macros = config.get("promptMacros")
    if macros is not None:
        if not isinstance(macros, dict):
            raise RunnerError("promptMacros must be an object")
        for scope in ("global", "local"):
            dictionary = macros.get(scope)
            if dictionary is None:
                continue
            if not isinstance(dictionary, dict):
                raise RunnerError(f"promptMacros.{scope} must be an object")
            for name, value in dictionary.items():
                label = f"promptMacros.{scope}.{name}.path"
                if isinstance(value, str):
                    continue
                if not isinstance(value, dict):
                    raise RunnerError(f"promptMacros.{scope}.{name} has malformed value")
                has_path = isinstance(value.get("path"), str) and bool(value["path"].strip())
                has_text = isinstance(value.get("text"), str) and bool(value["text"].strip())
                if has_path == has_text:
                    raise RunnerError(f"promptMacros.{scope}.{name} must define exactly one of path or text")
                if has_path:
                    value["path"], mark = resolve_configured_file(controller_root, value["path"], label)
                    mappings.append(mark)
    derived = out / "compatible-config.json"
    atomic_json(derived, config)
    derived_mark = evidence(derived)
    derived_mark["identity"] = configured_file_evidence(derived.resolve(), derived, "derived config")
    contract = {"schema_version": "juno_managed_compatible_config.v1", "source": source_mark,
                "derived": derived_mark, "path_mappings": mappings,
                "transformations": transformations}
    contract["sha256"] = sha(canonical(contract))
    return contract, mappings


def verify_compatible_config(contract: dict[str, Any]) -> None:
    source = contract["source"]
    current_source = configured_file_evidence(Path(source["resolved_path"]), Path(source["lexical_path"]), "controller config")
    derived = contract["derived"]
    current_derived = evidence(Path(derived["path"]))
    current_derived["identity"] = configured_file_evidence(Path(derived["path"]), Path(derived["path"]), "derived config")
    if current_source != source or current_derived != derived:
        raise RunnerError("controller or derived config identity drifted during launch")
    for expected in contract["path_mappings"]:
        current = configured_file_evidence(Path(expected["resolved_path"]), Path(expected["lexical_path"]), expected["setting"])
        if {"setting": expected["setting"], "configured_path": expected["configured_path"], **current} != expected:
            raise RunnerError(f"configured source file identity drifted: {expected['setting']}")


def _conflict_checkout_snapshot(controller_root: Path, root: Path,
                                target_ref: str) -> dict[str, Any]:
    helper = controller_root / ".juno_task/scripts/worktree_hydration.py"
    if helper.is_symlink() or not helper.is_file():
        raise RunnerError("conflict checkout hydration verifier is missing")
    completed = subprocess.run(
        [sys.executable, str(helper), "--project-root", str(root),
         "snapshot-conflict-checkout", "--target-ref", target_ref],
        cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=30, text=True,
    )
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError:
        value = None
    if completed.returncode or not isinstance(value, dict):
        raise RunnerError("conflict checkout hydration identity is invalid")
    return value


def _verified_conflict_receipt(controller_root: Path, state: dict[str, Any],
                               conflict: dict[str, Any]) -> dict[str, Any]:
    matches = [row for row in state.get("receipts", []) if isinstance(row, dict)
               and row.get("transition") == "CONFLICT"]
    if len(matches) != 1 or matches[0] != state.get("receipts", [])[-1]:
        raise RunnerError("conflict worker authority receipt is missing or ambiguous")
    reference = matches[0]
    path = Path(str(reference.get("path", ""))).resolve()
    expected_root = (controller_root / ".juno_task/runtime/release-epochs"
                     / str(state.get("epoch_id")) / "receipts").resolve()
    try:
        data = path.read_bytes()
        receipt = json.loads(data)
    except (OSError, json.JSONDecodeError) as exc:
        raise RunnerError("conflict worker authority receipt is unreadable") from exc
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_id"}
    if (path.is_symlink() or path.parent != expected_root
            or sha(data) != reference.get("sha256")
            or receipt.get("schema_version") != "juno_release_epoch_receipt.v1"
            or receipt.get("epoch_id") != state.get("epoch_id")
            or receipt.get("transition") != "CONFLICT"
            or receipt.get("detail") != conflict
            or sha(canonical(unsigned).rstrip(b"\n")) != receipt.get("receipt_id")
            or receipt.get("receipt_id") != reference.get("receipt_id")):
        raise RunnerError("conflict worker authority receipt identity drifted")
    return {**evidence(path), "receipt_id": receipt["receipt_id"]}


def release_conflict_admission(args: argparse.Namespace, controller: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    root = Path(args.agent_root).resolve()
    controller_root = Path(controller["root"]).resolve()
    if not args.candidate_sha or not SHA_RE.fullmatch(args.candidate_sha):
        raise RunnerError("conflict worker requires the sealed candidate SHA")
    states = []
    epoch_root = controller_root / ".juno_task/runtime/release-epochs"
    for path in sorted(epoch_root.glob("*/state.json")):
        try:
            state = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        composition = state.get("composition") if isinstance(state, dict) else None
        conflict = state.get("conflict") if isinstance(state, dict) else None
        if (state.get("state") == "RECOVERING" and isinstance(composition, dict)
                and isinstance(conflict, dict)
                and Path(str(composition.get("worktree", ""))).resolve() == root
                and conflict.get("task_id") == args.task_id
                and conflict.get("theirs_sha") == args.candidate_sha):
            states.append((path, state, conflict))
    if len(states) != 1:
        raise RunnerError("conflict worker requires one exact frozen release-epoch packet")
    state_path, state, conflict = states[0]
    seal = state.get("seal") if isinstance(state.get("seal"), dict) else {}
    composition = state.get("composition") if isinstance(state.get("composition"), dict) else {}
    target_ref, base_sha = seal.get("target_ref"), seal.get("base_sha")
    mark = fingerprint(root)
    if (state_path.is_symlink() or not state_path.is_file()
            or state_path.read_bytes() != canonical(state)
            or not isinstance(target_ref, str) or git(root, "rev-parse", target_ref) != base_sha
            or composition.get("tip_sha") != conflict.get("ours_sha")
            or mark["head"] != conflict.get("ours_sha")
            or git(root, "rev-parse", "MERGE_HEAD", check=False) != args.candidate_sha
            or git(root, "rev-parse", "ORIG_HEAD", check=False) != conflict.get("ours_sha")):
        raise RunnerError("conflict worker checkout does not match the frozen repair packet")
    admitted = sorted(conflict.get("admitted_paths") or [])
    logical_set = conflict.get("logical_conflict_set")
    validation_root = conflict.get("validation_root")
    manifest = seal.get("conflict_manifest")
    authority = (manifest or {}).get("authority_binding") if isinstance(manifest, dict) else None
    authority_path = Path(str((authority or {}).get("path", ""))).resolve()
    member = next((row for row in seal.get("members", []) if isinstance(row, dict)
                   and row.get("task_id") == args.task_id), None)
    expected_changed = sorted((member or {}).get("changed_paths") or [])
    observed_changed = sorted(filter(None, git(root, "diff", "--name-only",
                                               conflict["ours_sha"]).splitlines()))
    if (conflict.get("schema_version") != "juno_release_epoch_conflict.v2"
            or not isinstance(logical_set, dict)
            or logical_set.get("classification") != "authorization_neutral"
            or logical_set.get("ordered_task_ids", [None])[0] != args.task_id
            or sorted(logical_set.get("permitted_paths") or []) != admitted
            or not isinstance(validation_root, dict) or not isinstance(member, dict)
            or observed_changed != expected_changed
            or not isinstance(authority, dict) or authority_path.is_symlink()
            or not authority_path.is_file() or sha(authority_path.read_bytes()) != authority.get("sha256")
            or authority.get("sha256") != logical_set.get("authority_sha256")
            or (manifest or {}).get("manifest_sha256") != sha(canonical({
                key: value for key, value in manifest.items() if key != "manifest_sha256"
            }).rstrip(b"\n"))):
        raise RunnerError("conflict worker requires one declaration-bound logical conflict set")
    conflict_paths = sorted(conflict.get("conflict_paths") or [])
    applied_paths = []
    for relative in sorted(set(expected_changed) - set(conflict_paths)):
        lexical = root.joinpath(*Path(relative).parts)
        try:
            info = lexical.lstat()
            content = lexical.read_bytes()
        except OSError as exc:
            raise RunnerError("conflict checkout applied path is missing") from exc
        index_fields = git(root, "ls-files", "--stage", "--", relative).split()
        candidate_fields = git(root, "ls-tree", conflict["theirs_sha"], "--", relative).split()
        if (lexical.is_symlink() or not lexical.is_file()
                or any(parent.is_symlink() for parent in lexical.parents if parent != root.parent)
                or len(index_fields) < 3 or index_fields[2] != "0"
                or len(candidate_fields) < 3
                or index_fields[:2] != [candidate_fields[0], candidate_fields[2]]
                or sha(content) != sha(subprocess.check_output(
                    ["git", "-C", str(root), "cat-file", "blob", index_fields[1]]))):
            raise RunnerError("conflict checkout applied path identity drifted")
        applied_paths.append({"path": relative, "mode": info.st_mode,
                              "blob": index_fields[1], "sha256": sha(content)})
    snapshot = _conflict_checkout_snapshot(controller_root, root, target_ref)
    if (any(line.startswith("? ") for line in
            str(snapshot.get("status_porcelain_v2", "")).splitlines())
            or snapshot.get("conflict_paths") != conflict_paths
            or snapshot.get("head") != conflict.get("ours_sha")
            or snapshot.get("merge_head") != conflict.get("theirs_sha")
            or snapshot.get("orig_head") != conflict.get("ours_sha")
            or snapshot.get("target_sha") != base_sha):
        raise RunnerError("conflict checkout hydration identity is not receipt-bound")
    stage_by_path = {path: {row["stage"]: row for row in snapshot["unmerged_stages"]
                            if row["path"] == path}
                     for path in snapshot["conflict_paths"]}
    merge_base = git(root, "merge-base", conflict["ours_sha"], conflict["theirs_sha"])
    for path, stages in stage_by_path.items():
        expected_blobs = {stage: git(root, "rev-parse", f"{commit}:{path}") for stage, commit in
                          ((1, merge_base), (2, conflict["ours_sha"]),
                           (3, conflict["theirs_sha"]))}
        if set(stages) != {1, 2, 3} or any(stages[stage]["blob"] != blob
                                           for stage, blob in expected_blobs.items()):
            raise RunnerError("conflict checkout unmerged stages drifted")
    authority_receipt = _verified_conflict_receipt(controller_root, state, conflict)
    worker_attempt = {"tool_id": args.tool_id, "out_dir": str(Path(args.out_dir).resolve()),
                      "prompt_sha256": sha(Path(args.prompt_file).resolve().read_bytes()),
                      "candidate_sha": args.candidate_sha, "task_id": args.task_id}
    admission = {"task_id": args.task_id, "expected_paths": admitted,
                 "admission_kind": "sealed_release_epoch_conflict",
                 "epoch_id": state.get("epoch_id"),
                 "epoch_fencing_token_sha256": seal.get("fencing_token_sha256"),
                 "epoch_state": evidence(state_path), "authority_receipt": authority_receipt,
                 "conflict_manifest_sha256": (manifest or {}).get("manifest_sha256"),
                 "conflict_authority": evidence(authority_path),
                 "conflict_sha256": sha(canonical(conflict).rstrip(b"\n")), "before": mark,
                 "conflict_checkout": snapshot, "applied_paths": applied_paths,
                 "target_ref": target_ref,
                 "base_sha": base_sha, "composition_tip": composition.get("tip_sha"),
                 "ours_sha": conflict["ours_sha"], "theirs_sha": conflict["theirs_sha"],
                 "logical_conflict_set": logical_set, "validation_root": validation_root,
                 "worker_attempt": worker_attempt}
    return admission, mark


def _canonical_receipt(path: Path, label: str) -> tuple[dict[str, Any], dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        raise RunnerError(f"{label} must be an immutable regular file")
    data = path.read_bytes()
    value = load_object(path, label)
    if data != canonical(value):
        raise RunnerError(f"{label} bytes are not canonical")
    return value, {"path": str(path), "sha256": sha(data)}


def validate_worker(args: argparse.Namespace, controller: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    if not args.task_id or not TASK_RE.fullmatch(args.task_id):
        raise RunnerError("worker mode requires a safe task ID")
    paths = [Path(x).resolve() if x else None for x in (args.create_receipt, args.verify_receipt, args.edit_preflight_receipt)]
    if all(x is None for x in paths) and args.candidate_sha:
        return release_conflict_admission(args, controller)
    if any(x is None for x in paths):
        raise RunnerError("worker mode requires create, verify, and edit-preflight receipts")
    assert all(path is not None for path in paths)
    create_path, verify_path, edit_path = paths
    create = load_object(create_path, "create receipt")
    verify = load_object(verify_path, "verify receipt")
    edit = load_object(edit_path, "edit-preflight receipt")
    admission_kind = create.get("admission_kind")
    if admission_kind == "historical_creation":
        create, create_ref = _canonical_receipt(create_path, "create receipt")
        verify, verify_ref = _canonical_receipt(verify_path, "verify receipt")
        edit, edit_ref = _canonical_receipt(edit_path, "edit-preflight receipt")
        if ([path.name for path in paths] != ["create-receipt.json", "verify-receipt.json", "edit-preflight-receipt.json"]
                or len({path.parent for path in paths}) != 1):
            raise RunnerError("worker receipts must be exact colocated lifecycle receipts")
    else:
        create_ref, verify_ref, edit_ref = (evidence(path) for path in paths)
    root = Path(args.agent_root).resolve()
    mark = fingerprint(root)
    if mark["status"] or create.get("task_id") != args.task_id or Path(str(create.get("worktree"))).resolve() != root:
        raise RunnerError("worker admission identity mismatch")
    if create.get("branch_ref") != mark["branch_ref"] or create.get("git_common_dir") != mark["git_common_dir"]:
        raise RunnerError("worker branch/common-directory authority mismatch")
    if admission_kind not in (None, "historical_creation"):
        raise RunnerError("worker admission kind is unsupported")
    if verify.get("passed") is not True or edit.get("passed") is not True:
        raise RunnerError("worker verify/edit-preflight authority did not pass")
    for receipt in (verify, edit):
        if receipt.get("task_id") not in (None, args.task_id):
            raise RunnerError("worker receipt task identity mismatch")
    expected_paths = create.get("expected_paths")
    if (not isinstance(expected_paths, list) or not expected_paths
            or any(not isinstance(value, str) or not value for value in expected_paths)
            or len(expected_paths) != len(set(expected_paths))
            or (admission_kind != "historical_creation"
                and expected_paths != sorted(expected_paths))):
        raise RunnerError("worker path admission is malformed")
    if admission_kind == "historical_creation":
        role = git(root, "config", "--worktree", "--get", "juno.workspace.role", check=False)
        task = git(root, "config", "--worktree", "--get", "juno.workspace.taskId", check=False)
        manifest = git(root, "config", "--worktree", "--get", "juno.workspace.manifestIdentity", check=False)
        creation_sha = git(root, "config", "--worktree", "--get", "juno.workspace.createReceiptSha256", check=False)
        paths_sha = git(root, "config", "--worktree", "--get", "juno.workspace.expectedPathsSha256", check=False)
        expected_sha = sha(json.dumps(expected_paths, sort_keys=True, separators=(",", ":")).encode())
        if (create.get("schema_version") != "juno_managed_task_run_create.v1"
                or create.get("workspace_role") != "task" or role != "task" or task != args.task_id
                or create.get("workspace_manifest_identity") != manifest
                or "admission_supersession_sha256" in create
                or create.get("creation_receipt_sha256") != creation_sha
                or create.get("expected_paths_sha256") != paths_sha or expected_sha != paths_sha
                or create.get("clean_tip_sha") != mark["head"]):
            raise RunnerError("historical task creation authority mismatch")
        if (verify.get("schema_version") != "juno_managed_task_run_verify.v1"
                or verify.get("workspace_role") != "task" or verify.get("tip_sha") != mark["head"]
                or verify.get("create_receipt_sha256") != create_ref["sha256"]
                or not isinstance(verify.get("dependency_evidence"), list)
                or not isinstance(verify.get("hydration_manifest_sha256"), str)
                or not __import__("re").fullmatch(r"[0-9a-f]{64}", verify["hydration_manifest_sha256"])):
            raise RunnerError("historical task verify/hydration authority mismatch")
        if (edit.get("schema_version") != "juno_managed_task_run_edit_preflight.v1"
                or edit.get("workspace_role") != "task" or edit.get("tip_sha") != mark["head"]
                or edit.get("create_receipt_sha256") != create_ref["sha256"]
                or edit.get("verify_receipt_sha256") != verify_ref["sha256"]
                or edit.get("allowed_paths_sha256") != expected_sha):
            raise RunnerError("historical task edit-preflight authority mismatch")
    worker_attempt = {
        "task_id": args.task_id,
        "tool_id": getattr(args, "tool_id", None),
        "out_dir": str(Path(args.out_dir).resolve()) if getattr(args, "out_dir", None) else None,
        "prompt_sha256": (sha(Path(args.prompt_file).resolve().read_bytes())
                          if getattr(args, "prompt_file", None) else None),
    }
    admission = {"task_id": args.task_id, "expected_paths": expected_paths,
                 "admission_kind": admission_kind,
                 "create": create_ref, "verify": verify_ref, "edit_preflight": edit_ref,
                 "manifest_identity": create.get("workspace_manifest_identity"), "before": mark,
                 "worker_attempt": worker_attempt}
    return admission, mark


def validate_reviewer(args: argparse.Namespace, controller: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    if not args.candidate_sha or not SHA_RE.fullmatch(args.candidate_sha) or not args.candidate_root:
        raise RunnerError("reviewer mode requires candidate SHA and root")
    candidate = Path(args.candidate_root).resolve()
    mark = fingerprint(candidate)
    if mark["head"] != args.candidate_sha or mark["status"]:
        raise RunnerError("review candidate is not the clean exact tip")
    agent = Path(args.agent_root).resolve()
    if agent != (Path(args.out_dir).resolve() / "agent-root"):
        raise RunnerError("reviewer agent root must be the launcher-owned neutral agent-root")
    agent.mkdir(parents=True, exist_ok=False)
    return {"candidate_sha": args.candidate_sha, "candidate_root": str(candidate), "before": mark}, mark


def managed_controller_binding(mark: dict[str, Any]) -> dict[str, Any] | None:
    if not mark.get("queue_state"):
        return None
    resolver = mark.get("resolver")
    policy_identity = resolver.get("policy_identity") if isinstance(resolver, dict) else None
    if not isinstance(policy_identity, dict) or not policy_identity:
        raise RunnerError("queue-owned dirty controller requires canonical resolver identity")
    return {"schema_version": "juno_managed_controller_binding.v1",
            "root": mark["root"], "head": mark["head"],
            "branch_ref": mark["branch_ref"], "config_sha256": mark["config_sha256"],
            "policy_identity": policy_identity,
            "queue_state": mark["queue_state"]}


def node_version(executable: str | None) -> str:
    if not executable:
        return "unknown"
    try:
        result = subprocess.run(
            [executable, "-p", "process.versions.node"], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def supported_node_version(version: str) -> bool:
    match = __import__("re").fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:[-+].*)?", version)
    return bool(match and (int(match.group(1)) > 20 or
                           (int(match.group(1)) == 20 and int(match.group(2)) >= 10)))


def managed_node_contract() -> tuple[dict[str, str], str]:
    supplied = os.environ.get("YYLO_NODE_EXECUTABLE", "")
    yy_executable = shutil.which("yy")
    path_node = shutil.which("node")
    path_version = node_version(path_node)
    yy_sibling = str(Path(yy_executable).parent / "node") if yy_executable else ""
    yy_sibling_version = node_version(yy_sibling)
    source = "yy_environment"
    if supplied:
        supplied_path = Path(supplied)
        canonical = str(supplied_path) if supplied_path.is_absolute() else ""
        canonical_version = node_version(canonical)
    elif supported_node_version(yy_sibling_version):
        canonical, canonical_version, source = yy_sibling, yy_sibling_version, "yy_sibling"
    elif supported_node_version(path_version):
        canonical, canonical_version, source = path_node or "", path_version, "supported_path_fallback"
    else:
        canonical, canonical_version, source = "", "unknown", "unavailable"
    if (not canonical or not Path(canonical).is_file() or not os.access(canonical, os.X_OK)
            or not supported_node_version(canonical_version) or not yy_executable):
        raise RunnerError(
            "managed Node runtime contract is missing or unsupported; "
            f"canonical executable: {canonical or supplied or 'not set'}; "
            f"canonical version: {canonical_version}; "
            f"PATH node executable: {path_node or 'not found'}; "
            f"PATH node version: {path_version}; yy executable: {yy_executable or 'not found'}; "
            f"yy sibling Node: {yy_sibling or 'not found'} ({yy_sibling_version}); "
            "required version: Node.js >=20.10; "
            "invoke this managed operation through a supported yy launcher")
    node_dir = str(Path(canonical).parent)
    entries = os.environ.get("PATH", "").split(os.pathsep)
    normalized = [node_dir]
    canonical_dir = Path(node_dir).resolve()
    for entry in entries:
        if not entry:
            continue
        try:
            if Path(entry).resolve() == canonical_dir:
                continue
        except OSError:
            pass
        normalized.append(entry)
    return ({"executable": canonical, "version": canonical_version, "source": source,
             "yy_executable": str(Path(yy_executable).absolute()),
             "path_node_before": path_node or "not found", "path_node_version_before": path_version,
             "required_version": ">=20.10"}, os.pathsep.join(normalized))


def clean_environment(args: argparse.Namespace, capture: Path, metadata: Path,
                      binding: dict[str, Any] | None = None,
                      controller_mark: dict[str, Any] | None = None,
                      identity: dict[str, Any] | None = None) -> tuple[dict[str, str], dict[str, Any]]:
    node_contract, normalized_path = managed_node_contract()
    removed = sorted(k for k in os.environ if k.startswith(("PI_", "JUNO_")) or k == "TASK_ROOT")
    env = {k: v for k, v in os.environ.items() if k not in removed}
    explicit = {"JUNO_TASK_ROOT": str(Path(args.controller_root).resolve()),
                "JUNO_CONTROLLER_BRANCH": args.controller_branch.removeprefix("refs/heads/"),
                "JUNO_WORKSPACE_ROLE": "controller", "JUNO_WORKSPACE_ENFORCEMENT": "strict",
                "JUNO_SUBAGENT_CAPTURE_PATH": str(capture), "JUNO_TOOL_ID": args.tool_id,
                "YYLO_SESSION_METADATA_DIRECTORY": str(metadata),
                "JUNO_CONTROLLER_CHECKPOINT_ACTIVE": "1", "PYTHONUNBUFFERED": "1",
                "YYLO_NODE_EXECUTABLE": node_contract["executable"],
                "PATH": normalized_path}
    explicit["YYLO_PROJECT_BOOTSTRAP_WRITES"] = "0"
    worker_admission_kind = None
    if args.mode == "worker":
        if not isinstance(identity, dict):
            raise RunnerError("worker environment requires a validated admission identity")
        worker_admission_kind = identity.get("admission_kind")
        if worker_admission_kind not in (None, "historical_creation", "sealed_release_epoch_conflict"):
            raise RunnerError("worker environment has an unsupported admission kind")
        workspace_role = ("controller" if worker_admission_kind == "sealed_release_epoch_conflict"
                          else "task")
        explicit.update({"TASK_ROOT": str(Path(args.agent_root).resolve()), "JUNO_AGENT_TASK_ID": args.task_id,
                         "JUNO_WORKSPACE_ROLE": workspace_role})
        if args.authority_map:
            explicit["JUNO_LIFECYCLE_AUTHORITY_MAP"] = str(Path(args.authority_map).resolve())
    if binding is not None:
        explicit["JUNO_REVIEW_BINDING_JSON"] = canonical(binding).decode().strip()
    controller_binding = managed_controller_binding(controller_mark or {})
    if controller_binding is not None:
        explicit["JUNO_MANAGED_CONTROLLER_BINDING_JSON"] = canonical(
            controller_binding).decode().strip()
    env.update(explicit)
    env = child_invocation_environment(
        env, launch_surface="managed_agent_runner", task_id=args.task_id or None,
        source=os.environ,
    )
    contract = {"schema_version": "juno_managed_environment.v1", "removed_key_names": removed,
                "explicit_key_names": sorted(explicit), "configured_defaults": True,
                "workspace_role": explicit["JUNO_WORKSPACE_ROLE"],
                "worker_admission_kind": worker_admission_kind,
                "node_runtime": node_contract}
    contract["sha256"] = sha(canonical(contract))
    return env, contract


def hydrate_conflict_validation_root(controller_root: Path, agent_root: Path,
                                       identity: dict[str, Any],
                                       expectation_path: Path | None = None) -> dict[str, Any]:
    """Probe/hydrate one exact-lock conflict-worker validation root before model launch."""
    if identity.get("admission_kind") != "sealed_release_epoch_conflict":
        return {"schema_version": "juno_managed_worker_hydration.v1",
                "decision": "not_applicable"}
    validation = identity.get("validation_root")
    cwd = validation.get("cwd") if isinstance(validation, dict) else None
    if (not isinstance(cwd, str) or not cwd or Path(cwd).is_absolute()
            or ".." in Path(cwd).parts):
        raise RunnerError("conflict validation root is malformed")
    lock = agent_root / cwd / "package-lock.json"
    helper = controller_root / ".juno_task/scripts/worktree_hydration.py"
    snapshot = identity.get("conflict_checkout")
    if (not helper.is_file() or not lock.is_file() or not isinstance(snapshot, dict)
            or expectation_path is None):
        raise RunnerError("conflict validation exact lock or hydration authority is missing")
    atomic_json(expectation_path, snapshot)
    base = [sys.executable, str(helper), "--project-root", str(agent_root)]
    verify = [*base, "verify-conflict-checkout", "--target-ref", identity["target_ref"],
              "--expected-snapshot", str(expectation_path)]
    output = bytearray()
    timeout = int(validation.get("timeout_seconds", 3600))
    def execute(command: list[str]) -> subprocess.CompletedProcess[bytes]:
        completed = subprocess.run(command, cwd=agent_root, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        output.extend(completed.stdout[-65536:])
        return completed
    initial = execute(verify)
    if initial.returncode:
        detail = initial.stdout.decode("utf-8", errors="replace")[-512:].strip()
        raise RunnerError("conflict validation exact-lock hydration/probe failed: " + detail)
    probe = execute([*base, "verify-node-lock", "--cwd", cwd])
    if probe.returncode:
        hydrated = execute([*base, "hydrate-node", "--cwd", cwd])
        if hydrated.returncode:
            detail = hydrated.stdout.decode("utf-8", errors="replace")[-512:].strip()
            raise RunnerError("conflict validation exact-lock hydration/probe failed: " + detail)
    final = execute(verify)
    if final.returncode:
        detail = final.stdout.decode("utf-8", errors="replace")[-512:].strip()
        raise RunnerError("conflict validation exact-lock hydration/probe failed: " + detail)
    return {"schema_version": "juno_managed_worker_hydration.v1", "decision": "passed",
            "cwd": cwd, "lock_sha256": sha(lock.read_bytes()),
            "conflict_checkout_sha256": sha(canonical(snapshot).rstrip(b"\n")),
            "expectation": evidence(expectation_path),
            "output_sha256": sha(bytes(output)),
            "git_status_sha256": sha(str(snapshot["status_porcelain_v2"]).encode())}


def finalize_managed_capture(capture: Path, stdout_path: Path, metadata: Path,
                             binding: dict[str, Any] | None, started_ns: int) -> str:
    if capture.is_file() and capture.stat().st_mtime_ns >= started_ns:
        return "provider_capture"
    if binding is None:
        raise RunnerError("capture is missing or stale")
    try:
        response = stdout_path.read_bytes()
    except OSError as exc:
        raise RunnerError("capture is missing or stale") from exc
    if not response or len(response) > CAPTURE_LIMIT:
        raise RunnerError("capture is missing or stale")
    # A managed reviewer has an exact JSON response contract.  This permits the
    # outer process owner to finalize a capture consumed by the inner shell
    # backend, without accepting logs, prose, or worker output as a substitute.
    structured_review_result(response, binding)
    continuity_path = metadata / "session_continuity.v2.json"
    if not continuity_path.is_file() or continuity_path.stat().st_mtime_ns < started_ns:
        raise RunnerError("capture is missing or stale")
    continuity = load_object(continuity_path, "session continuity")
    scopes = continuity.get("scopes")
    if continuity.get("version") != 2 or not isinstance(scopes, dict) or len(scopes) != 1:
        raise RunnerError("capture is missing or stale")
    scope = next(iter(scopes.values()))
    active = scope.get("active") if isinstance(scope, dict) else None
    branches = scope.get("branches") if isinstance(scope, dict) else None
    branch = branches.get(active) if isinstance(branches, dict) and isinstance(active, str) else None
    session = branch.get("session_id") if isinstance(branch, dict) else None
    if not isinstance(session, str) or not session.strip():
        raise RunnerError("capture is missing or stale")
    atomic_json(capture, {"session_id": session.strip(), "result": response.decode("utf-8"),
                          "is_error": False, "capture_source": "managed_stdout_finalizer"})
    return "managed_stdout_finalizer"


def _worker_capture_error(reason: str, detail: str) -> RunnerError:
    return RunnerError(f"worker_capture_recovery_{reason}: {detail}")


def _worker_terminal_state(response: bytes) -> str:
    try: lines = response.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise _worker_capture_error("mismatched_capture", "worker response is not UTF-8") from exc
    first = next((line.strip().lower() for line in lines if line.strip()), "")
    if first not in {"completed", "blocked", "incomplete", "failed"}:
        raise _worker_capture_error(
            "ambiguous_capture", "worker response lacks one exact leading terminal state")
    return first


def _validate_recovered_worker_subject(identity: dict[str, Any], before: dict[str, Any],
                                       after: dict[str, Any], state: str) -> list[str]:
    attempt = identity.get("worker_attempt")
    if (not isinstance(attempt, dict) or attempt.get("task_id") != identity.get("task_id")
            or not isinstance(attempt.get("tool_id"), str) or not attempt["tool_id"]
            or not isinstance(attempt.get("out_dir"), str) or not attempt["out_dir"]
            or not isinstance(attempt.get("prompt_sha256"), str)
            or not __import__("re").fullmatch(r"[0-9a-f]{64}", attempt["prompt_sha256"])):
        raise _worker_capture_error("mismatched_identity", "task/attempt identity is incomplete")
    if (after.get("root") != before.get("root")
            or after.get("branch_ref") != before.get("branch_ref")
            or after.get("git_common_dir") != before.get("git_common_dir")):
        raise _worker_capture_error("mismatched_identity", "worktree identity changed: "
                                    f"before={before.get('root')}@{before.get('branch_ref')}:{before.get('git_common_dir')} "
                                    f"after={after.get('root')}@{after.get('branch_ref')}:{after.get('git_common_dir')}")
    if after.get("status"):
        raise _worker_capture_error("dirty_state", "worker worktree is not clean")
    root = Path(str(after["root"]))
    if state != "completed":
        if after.get("head") != before.get("head"):
            raise _worker_capture_error("incomplete_result", "non-completed worker changed commit")
        return []
    admission_kind = identity.get("admission_kind")
    if admission_kind == "sealed_release_epoch_conflict":
        ours, theirs = identity.get("ours_sha"), identity.get("theirs_sha")
        parents = git(root, "show", "-s", "--format=%P", after["head"]).split()
        if (len(parents) != 2 or ours not in parents or theirs not in parents):
            raise _worker_capture_error("mismatched_identity", "conflict worker commit lacks exact parents")
        comparison = str(ours)
    else:
        if git(root, "rev-list", "--count", f"{before['head']}..{after['head']}") != "1":
            raise _worker_capture_error("incomplete_result", "worker did not produce exactly one commit")
        comparison = before["head"]
    changed = sorted(filter(None, git(root, "diff", "--name-only",
                                      f"{comparison}..{after['head']}").splitlines()))
    allowed = identity.get("expected_paths")
    if not isinstance(allowed, list) or any(not isinstance(path, str) for path in allowed):
        raise _worker_capture_error("mismatched_identity", "path admission is malformed")
    unexpected = [path for path in changed if not any(
        path == admitted or path.startswith(admitted.rstrip("/") + "/") for admitted in allowed)]
    if unexpected:
        raise _worker_capture_error("mismatched_identity", "worker changed paths outside admission")
    return changed


def recover_settled_worker_capture(
        capture: Path, stdout_path: Path, metadata: Path, identity: dict[str, Any],
        before: dict[str, Any], after: dict[str, Any], started_ns: int, *,
        exit_code: int, timed_out: bool, interrupted: int,
        termination_events: list[dict[str, Any]], process_settled: bool,
) -> tuple[str, dict[str, Any]]:
    """Recover one exact worker result only after process and Git settlement."""
    if exit_code != 0:
        raise _worker_capture_error("nonzero_exit", f"provider exited {exit_code}")
    if interrupted:
        raise _worker_capture_error("interrupted", f"provider was interrupted by signal {interrupted}")
    if timed_out or termination_events or not process_settled:
        raise _worker_capture_error("incomplete_settlement", "provider process group did not settle cleanly")
    try: response = stdout_path.read_bytes()
    except OSError as exc:
        raise _worker_capture_error("missing_capture", "bounded worker stdout is missing") from exc
    if not response or len(response) > CAPTURE_LIMIT:
        raise _worker_capture_error("missing_capture", "bounded worker stdout is missing or unbounded")
    continuity_path = metadata / "session_continuity.v2.json"
    try:
        if (continuity_path.is_symlink() or not continuity_path.is_file()
                or continuity_path.stat().st_mtime_ns < started_ns):
            raise OSError("continuity is absent, symbolic, or stale")
        session = continuity_session(continuity_path)
    except (OSError, RunnerError) as exc:
        reason = "ambiguous_capture" if "malformed" in str(exc) else "stale_capture"
        raise _worker_capture_error(reason, "session settlement identity is not exact") from exc
    state = _worker_terminal_state(response)
    if state == "failed":
        raise _worker_capture_error("incomplete_result", "worker declared failed")
    changed = _validate_recovered_worker_subject(identity, before, after, state)
    attempt = identity["worker_attempt"]
    binding = {
        "schema_version": "juno_managed_worker_capture_binding.v1",
        "task_id": identity["task_id"], "attempt": attempt,
        "session_id": session, "worktree": after["root"],
        "before_sha": before["head"], "commit_sha": after["head"],
        "changed_paths": changed, "response_sha256": sha(response),
    }
    expected = {"session_id": session, "result": response.decode("utf-8"),
                "is_error": state == "failed",
                "terminal_outcome": {"schema_version": TERMINAL_RESULT_SCHEMA, "state": state},
                "capture_source": "settled_worker_recovery", "recovery_binding": binding}
    existed = capture.exists()
    if existed:
        current = load_object(capture, "worker capture")
        required = {key: expected[key] for key in ("session_id", "result", "is_error",
                                                    "terminal_outcome")}
        if any(current.get(key) != value for key, value in required.items()):
            raise _worker_capture_error("mismatched_capture", "existing capture disagrees with settled result")
        payload = current
        source = "provider_capture_recovered_stale"
        if current.get("capture_source") == "settled_worker_recovery":
            if current.get("recovery_binding") != binding:
                raise _worker_capture_error("mismatched_capture", "recovery binding changed")
            source = "settled_worker_recovery"
    else:
        atomic_idempotent_json(capture, expected, "worker capture")
        payload = load_object(capture, "worker capture")
        if payload != expected:
            raise _worker_capture_error("mismatched_capture", "concurrent capture publication differed")
        source = "settled_worker_recovery"
    terminal = {"schema_version": "juno_managed_worker_capture_terminal.v1",
                "state": "published", "capture_sha256": sha(capture.read_bytes()),
                "capture_source": source, "recovery_binding": binding}
    atomic_idempotent_json(capture.with_name("capture-terminal.json"), terminal,
                           "worker capture terminal receipt")
    return source, payload


def verified_artifact(mark: Any, label: str, *, limit: int = CAPTURE_LIMIT) -> tuple[Path, bytes]:
    if (not isinstance(mark, dict) or not isinstance(mark.get("path"), str)
            or not isinstance(mark.get("sha256"), str)
            or not __import__("re").fullmatch(r"[0-9a-f]{64}", mark["sha256"])):
        raise RunnerError(f"{label} evidence is malformed")
    path = Path(mark["path"]).resolve()
    try:
        if path.is_symlink() or not path.is_file():
            raise OSError("not a regular file")
        data = path.read_bytes()
    except OSError as exc:
        raise RunnerError(f"{label} artifact is missing") from exc
    if not data or len(data) > limit or sha(data) != mark["sha256"]:
        raise RunnerError(f"{label} artifact identity mismatch")
    if "bytes" in mark and mark.get("bytes") != len(data):
        raise RunnerError(f"{label} artifact size mismatch")
    return path, data


def continuity_session(path: Path) -> str:
    continuity = load_object(path, "session continuity")
    scopes = continuity.get("scopes")
    if continuity.get("version") != 2 or not isinstance(scopes, dict) or len(scopes) != 1:
        raise RunnerError("session continuity identity is malformed")
    scope = next(iter(scopes.values()))
    active = scope.get("active") if isinstance(scope, dict) else None
    branches = scope.get("branches") if isinstance(scope, dict) else None
    branch = branches.get(active) if isinstance(branches, dict) and isinstance(active, str) else None
    session = branch.get("session_id") if isinstance(branch, dict) else None
    if not isinstance(session, str) or not session.strip():
        raise RunnerError("session continuity identity is malformed")
    return session.strip()


def recover_worker_capture(args: argparse.Namespace) -> int:
    """Finalize one successful worker whose immutable provider capture was absent.

    This path never invokes a model. It binds the original failed receipt, its
    launch/log evidence, continuity record, and the already-clean both-parent
    result into a distinct canonical receipt.
    """
    source_receipt = Path(args.failed_receipt).resolve()
    source_root = source_receipt.parent
    failed_bytes = source_receipt.read_bytes()
    failed = load_object(source_receipt, "failed managed-worker receipt")
    if failed_bytes != canonical(failed):
        raise RunnerError("failed managed-worker receipt must have canonical immutable bytes")
    if (source_receipt.name != "receipt.json"
            or failed.get("schema_version") != SCHEMA or failed.get("mode") != "worker"
            or failed.get("state") != "failed" or failed.get("semantic_outcome") != "failed"
            or failed.get("failure") != "capture is missing or stale"
            or failed.get("exit_code") != 0 or failed.get("timed_out") is not False
            or failed.get("exit_signal") is not None or failed.get("interrupted_signal") is not None
            or failed.get("termination_events") != []):
        raise RunnerError("failed receipt is not an eligible capture-only worker failure")
    launch_path, launch_bytes = verified_artifact(failed.get("launch"), "failed launch")
    if launch_path != (source_root / "launch.json").resolve():
        raise RunnerError("failed launch is not colocated with its immutable receipt")
    launch = load_object(launch_path, "failed launch")
    if launch_bytes != canonical(launch):
        raise RunnerError("failed launch bytes are not canonical")
    identity = launch.get("identity")
    if (launch.get("schema_version") != SCHEMA or launch.get("mode") != "worker"
            or launch.get("effective_hook_policy") != failed.get("effective_hook_policy")
            or identity != failed.get("identity")
            or not isinstance(identity, dict)
            or identity.get("admission_kind") != "sealed_release_epoch_conflict"):
        raise RunnerError("failed launch/receipt identity mismatch")
    argv = launch.get("argv")
    if (not isinstance(argv, list) or not argv or not all(isinstance(value, str) for value in argv)
            or launch.get("argv_sha256") != sha(shlex.join(argv).encode())):
        raise RunnerError("failed launch argv identity mismatch")
    for label, mark in (("prompt", launch.get("prompt")),
                        ("derived config", (launch.get("compatible_config") or {}).get("derived")),
                        ("launcher config", launch.get("launcher_config"))):
        verified_artifact(mark, label)

    terminal_path = source_root / "terminal.json"
    terminal = load_object(terminal_path, "failed terminal")
    if terminal_path.read_bytes() != canonical(terminal):
        raise RunnerError("failed terminal bytes are not canonical")
    for key, value in terminal.items():
        if failed.get(key) != value:
            raise RunnerError("failed terminal/receipt identity mismatch")

    stdout_path = source_root / "stdout.log"
    stdout = stdout_path.read_bytes()
    if not stdout or len(stdout) > CAPTURE_LIMIT or not stdout.decode("utf-8").strip():
        raise RunnerError("bounded worker stdout response is missing or malformed")
    live_path, live = verified_artifact(failed.get("live_log"), "failed live log",
                                        limit=64 * CAPTURE_LIMIT)
    if stdout not in live:
        raise RunnerError("worker stdout is not bound by the immutable live log")
    continuity_path = source_root / "session_metadata/session_continuity.v2.json"
    session = continuity_session(continuity_path)
    if session.encode() not in live:
        raise RunnerError("session continuity is not bound by the immutable live log")

    agent_root = Path(str(launch.get("agent_root"))).resolve()
    before = identity.get("before")
    if not isinstance(before, dict) or Path(str(before.get("root"))).resolve() != agent_root:
        raise RunnerError("failed worker root identity mismatch")
    after = fingerprint(agent_root)
    if after["status"] or after["branch_ref"] != before.get("branch_ref") \
            or after["git_common_dir"] != before.get("git_common_dir"):
        raise RunnerError("recovered worker checkout is not clean or identity-bound")
    ours, theirs = identity.get("ours_sha"), identity.get("theirs_sha")
    if not SHA_RE.fullmatch(str(ours)) or not SHA_RE.fullmatch(str(theirs)):
        raise RunnerError("failed conflict parent identity is malformed")
    parents = git(agent_root, "show", "-s", "--format=%P", after["head"]).split()
    if len(parents) != 2 or ours not in parents or theirs not in parents:
        raise RunnerError("recovered worker did not preserve the required both-parent commit")
    changed = sorted(filter(None, git(agent_root, "diff", "--name-only", f"{ours}..{after['head']}").splitlines()))
    allowed = identity.get("expected_paths")
    if not isinstance(allowed, list) or any(not isinstance(path, str) for path in allowed):
        raise RunnerError("failed conflict path admission is malformed")
    unexpected = [path for path in changed if not any(
        path == admitted or path.startswith(admitted.rstrip("/") + "/") for admitted in allowed)]
    if unexpected:
        raise RunnerError("recovered worker changed paths outside conflict admission")

    out = safe_out_dir(Path(args.out_dir))
    response_path = out / "response.txt"; atomic_bytes(response_path, stdout)
    recovery = {
        "schema_version": "juno_managed_agent_recovery.v1",
        "kind": "capture_only_no_model_rerun",
        "failed_receipt": evidence(source_receipt),
        "failed_terminal": evidence(terminal_path),
        "launch": evidence(launch_path),
        "live_log": {"path": str(live_path), "bytes": len(live), "sha256": sha(live)},
        "stdout": evidence(stdout_path),
        "continuity": evidence(continuity_path),
        "validated_exit_code": 0,
    }
    completed = now()
    receipt = {
        "schema_version": SCHEMA, "state": "succeeded", "mode": "worker",
        "semantic_outcome": "completed", "completed_at": completed, "exit_code": 0,
        "timed_out": False, "exit_signal": None, "interrupted_signal": None,
        "termination_events": [], "session_id": session,
        "capture_source": "receipt_bound_worker_recovery", "safe_next_action": "consume_receipt",
        "tool_id": failed.get("tool_id"), "effective_hook_policy": failed.get("effective_hook_policy"),
        "identity": {**identity, "changed_paths": changed, "unexpected_paths": []},
        "subject_after": after, "argv": argv, "argv_sha256": launch["argv_sha256"],
        "command_sha256": launch["argv_sha256"], "review_binding": None,
        "artifacts": {"response": evidence(response_path)}, "recovery": recovery,
    }
    terminal_out = {key: receipt[key] for key in (
        "schema_version", "state", "semantic_outcome", "completed_at", "exit_code",
        "timed_out", "exit_signal", "interrupted_signal", "termination_events", "session_id",
        "capture_source", "safe_next_action")}
    atomic_json(out / "terminal.json", terminal_out)
    atomic_json(out / "receipt.json", receipt)
    print(json.dumps({"receipt": str((out / "receipt.json").resolve()),
                      "session_id": session, "recovered_without_model_rerun": True}))
    return 0


def group_active(pgid: int) -> bool:
    try: os.killpg(pgid, 0); return True
    except ProcessLookupError: return False
    except PermissionError: return True


def terminate_group(pgid: int, signum: int) -> None:
    try: os.killpg(pgid, signum)
    except ProcessLookupError: pass


def record_group_signal(events: list[dict[str, Any]], pgid: int, signum: int,
                        reason: str, started: float) -> None:
    events.append({"at": now(), "elapsed_seconds": round(time.monotonic() - started, 3),
                   "process_group_id": pgid, "signal": signal.Signals(signum).name,
                   "reason": reason})
    terminate_group(pgid, signum)


def terminate_group_and_wait(events: list[dict[str, Any]], pgid: int, reason: str,
                             started: float, grace_seconds: float = .5) -> None:
    """Terminate the owned producer group and do not return while it is live."""
    if not group_active(pgid):
        return
    record_group_signal(events, pgid, signal.SIGTERM, reason, started)
    deadline = time.monotonic() + grace_seconds
    while group_active(pgid) and time.monotonic() < deadline:
        time.sleep(.02)
    if group_active(pgid):
        record_group_signal(events, pgid, signal.SIGKILL, reason + "_escalation", started)
        deadline = time.monotonic() + 2
        while group_active(pgid) and time.monotonic() < deadline:
            time.sleep(.02)
    if group_active(pgid):
        raise RunnerError(f"managed process group {pgid} remains active after SIGKILL")


def log_component(value: str, fallback: str) -> str:
    cleaned = __import__("re").sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-.")
    return cleaned[:64] or fallback


def allocate_live_log(workflow: str, task: str) -> tuple[Path, Any]:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    base = f"yy-{log_component(workflow, 'agent')}-{log_component(task, 'task')}-{stamp}"
    for suffix in ("", *[f"-{number}" for number in range(1, 100)]):
        path = Path("/tmp") / f"{base}{suffix}.log"
        try:
            handle = os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600),
                               "wb", buffering=0)
            print(f"yy long run log: {path}", file=sys.stderr, flush=True)
            return path, handle
        except FileExistsError:
            continue
        except OSError as exc:
            raise RunnerError(f"cannot allocate long-run log {path}: {exc}") from exc
    raise RunnerError(f"cannot allocate unique long-run log for {base}")


def announce_completion(started: float, code: int, timed_out: bool, path: Path) -> tuple[str, float]:
    finished = now(); elapsed = round(time.monotonic() - started, 3)
    exit_signal = signal.Signals(-code).name if code < 0 and -code in signal.valid_signals() else "none"
    try:
        print("yy long run complete: "
              f"finish_time={finished} duration_seconds={elapsed} exit_code={code} "
              f"exit_signal={exit_signal} timed_out={'true' if timed_out else 'false'} log_path={path}",
              file=sys.stderr, flush=True)
    except OSError:
        # Parent/observer pipe loss cannot prevent durable terminal evidence.
        pass
    return finished, elapsed


def pump(proc: subprocess.Popen[bytes], stdout_path: Path, stderr_path: Path,
         combined_path: Path, live: Any, timeout_seconds: float,
         interrupted: Any, termination_events: list[dict[str, Any]], started: float) -> bool:
    selector = selectors.DefaultSelector()
    assert proc.stdout and proc.stderr
    for stream, label in ((proc.stdout, b"stdout"), (proc.stderr, b"stderr")):
        os.set_blocking(stream.fileno(), False); selector.register(stream, selectors.EVENT_READ, label)
    with stdout_path.open("wb", buffering=0) as stdout, stderr_path.open("wb", buffering=0) as stderr, combined_path.open("wb", buffering=0) as combined:
        pending = {b"stdout": b"", b"stderr": b""}
        deadline = time.monotonic() + timeout_seconds
        timed_out = False
        cancellation_deadline: float | None = None
        while selector.get_map():
            if interrupted() and cancellation_deadline is None:
                cancellation_deadline = time.monotonic() + .5
            if cancellation_deadline is not None and time.monotonic() >= cancellation_deadline and group_active(proc.pid):
                record_group_signal(termination_events, proc.pid, signal.SIGKILL,
                                    "wrapper_signal_escalation", started)
                cancellation_deadline = float("inf")
            if not timed_out and time.monotonic() >= deadline:
                timed_out = True
                record_group_signal(termination_events, proc.pid, signal.SIGTERM, "timeout", started)
                cancellation_deadline = time.monotonic() + .5
            for key, _ in selector.select(.05):
                chunk = os.read(key.fd, 65536)
                label = key.data
                if not chunk:
                    selector.unregister(key.fileobj)
                    if pending[label]: combined.write(b"[" + label + b"] " + pending[label]); pending[label] = b""
                    continue
                (stdout if label == b"stdout" else stderr).write(chunk)
                try:
                    live.write(chunk)
                    sys.stderr.buffer.write(chunk); sys.stderr.buffer.flush()
                except OSError as exc:
                    record_group_signal(termination_events, proc.pid, signal.SIGKILL,
                                        "output_pipe_or_log_loss", started)
                    raise RunnerError(f"long-run log write failed: {exc}") from exc
                data = pending[label] + chunk
                lines = data.splitlines(keepends=True)
                if lines and not lines[-1].endswith((b"\n", b"\r")):
                    pending[label] = lines.pop()
                else: pending[label] = b""
                for line in lines: combined.write(b"[" + label + b"] " + line)
        return timed_out


def run(args: argparse.Namespace) -> int:
    if args.external_side_effects != "forbidden" or args.lifecycle_hooks != "disabled":
        raise RunnerError(
            "managed launch requires external-side-effects=forbidden and lifecycle-hooks=disabled"
        )
    effective_hook_policy = {
        "schema_version": "juno_managed_hook_policy.v1",
        "external_side_effects": "forbidden",
        "lifecycle_hooks": "disabled",
        "enforcement": "yy_pi_no_hooks",
    }
    out = safe_out_dir(Path(args.out_dir)); metadata = out / "session_metadata"; metadata.mkdir(exist_ok=True)
    controller_root = Path(args.controller_root).resolve()
    controller_before = controller_identity(controller_root)
    expected_branch = args.controller_branch if args.controller_branch.startswith("refs/") else "refs/heads/" + args.controller_branch
    if controller_before["branch_ref"] != expected_branch:
        raise RunnerError("controller branch identity mismatch")
    identity, subject_before = (validate_worker(args, controller_before) if args.mode == "worker" else validate_reviewer(args, controller_before))
    if not TASK_RE.fullmatch(args.tool_id):
        raise RunnerError("tool id is malformed")
    if args.mode != "reviewer" and args.review_binding:
        raise RunnerError("review binding is valid only in reviewer mode")
    binding = review_binding(args, identity) if args.mode == "reviewer" else None
    source_prompt = Path(args.prompt_file).resolve()
    prompt_data = source_prompt.read_bytes()
    if binding is not None:
        prompt_data += review_prompt_contract(binding)
    if not prompt_data or len(prompt_data) > CAPTURE_LIMIT:
        raise RunnerError("prompt must be nonempty and bounded")
    try: prompt_echo = prompt_data.decode("utf-8")
    except UnicodeDecodeError as exc: raise RunnerError("prompt must be exact UTF-8") from exc
    prompt = out / "prompt.md"; atomic_bytes(prompt, prompt_data)
    capture = out / "capture.json"; response_path = out / "response.txt"
    stdout_path, stderr_path, combined_path = out / "stdout.log", out / "stderr.log", out / "combined.log"
    compatible_config, _ = derive_compatible_config(controller_root, out)
    launcher = out / "launcher-root"; launcher.mkdir()
    launcher_config = launcher / ".juno_task/config.json"
    launcher_config.parent.mkdir()
    launcher_payload = load_object(
        Path(compatible_config["derived"]["path"]), "derived compatible config")
    launcher_payload["controllerWorkspace"] = {
        "mode": "metadata-only", "policy": ".juno_task/config/metadata-controller.json"}
    atomic_json(launcher_config, launcher_payload)
    agent_root = Path(args.agent_root).resolve()
    # Managed workers/reviewers must not execute user-owned lifecycle hooks.
    # Their environment, prompt, and output contract are already closed by this
    # process owner, and sparse controllers may intentionally omit hook targets.
    env, env_contract = clean_environment(
        args, capture, metadata, binding, controller_before, identity)
    validation_hydration = hydrate_conflict_validation_root(
        controller_root, agent_root, identity, out / "conflict-checkout-hydration.json")
    if identity.get("admission_kind") == "sealed_release_epoch_conflict":
        revalidated, _ = release_conflict_admission(args, controller_before)
        if revalidated != identity:
            raise RunnerError("conflict worker authority drifted before provider dispatch")
    argv = [env_contract["node_runtime"]["yy_executable"], "pi", "--no-hooks", "--config",
            compatible_config["derived"]["path"], "-w", str(agent_root), "-f", str(prompt)]
    prompt_evidence = evidence(prompt)
    if binding is None:
        prompt_evidence["echo"] = prompt_echo
    launch = {"schema_version": SCHEMA, "mode": args.mode, "started_at": now(), "controller": controller_before,
              "identity": identity, "launcher_root": str(launcher), "agent_root": str(agent_root),
              "launcher_config": evidence(launcher_config),
              "tool_id": args.tool_id, "review_binding": binding,
              "prompt": prompt_evidence, "compatible_config": compatible_config, "argv": argv,
              "validation_hydration": validation_hydration,
              "effective_hook_policy": effective_hook_policy,
              "argv_sha256": sha(shlex.join(argv).encode()), "environment_contract": env_contract}
    atomic_json(out / "launch.json", launch)
    active = {"schema_version": SCHEMA, "state": "active", "mode": args.mode, "run_root": str(out), "started_at": launch["started_at"]}
    atomic_json(out / "active.json", active)
    live_log_path, live_log = allocate_live_log(
        f"managed-{args.mode}", args.task_id or args.tool_id)
    proc: subprocess.Popen[bytes] | None = None; interrupted = 0
    producer_completed = False
    timed_out = False
    termination_events: list[dict[str, Any]] = []
    old_handlers: dict[int, Any] = {}
    started = time.monotonic()
    def forward(signum: int, _frame: Any) -> None:
        nonlocal interrupted
        interrupted = interrupted or signum
        if proc is not None:
            record_group_signal(termination_events, proc.pid, signum,
                                "wrapper_signal", started)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        old_handlers[sig] = signal.signal(sig, forward)
    started_ns = time.time_ns()
    try:
        proc = subprocess.Popen(argv, cwd=launcher, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        active["child_pid"] = proc.pid; active["process_group_id"] = proc.pid
        active["owner_pid"] = os.getpid(); atomic_json(out / "active.json", active)
        timed_out = pump(proc, stdout_path, stderr_path, combined_path,
                         live_log, args.timeout_seconds, lambda: interrupted,
                         termination_events, started)
        code = proc.wait()
        if group_active(proc.pid):
            terminate_group_and_wait(termination_events, proc.pid,
                                     "producer_exit_with_live_descendants", started)
        live_log.close()
        producer_completed = True
        producer_completed_at, producer_elapsed = announce_completion(
            started, code, timed_out, live_log_path)
        if interrupted:
            raise RunnerError(f"managed child interrupted by signal {interrupted}")
        if timed_out:
            raise RunnerError(f"managed child timed out after {args.timeout_seconds} seconds")
        if code:
            raise RunnerError(f"managed child exited {code}")
        if prompt.read_bytes() != prompt_data:
            raise RunnerError("prompt drifted during launch")
        subject_after = fingerprint(Path(identity.get("candidate_root") or args.agent_root).resolve())
        if args.mode == "worker" and (not capture.is_file()
                or capture.stat().st_mtime_ns < started_ns):
            capture_source, payload = recover_settled_worker_capture(
                capture, stdout_path, metadata, identity, subject_before, subject_after,
                started_ns, exit_code=code, timed_out=timed_out, interrupted=interrupted,
                termination_events=termination_events, process_settled=not group_active(proc.pid))
        else:
            capture_source = finalize_managed_capture(
                capture, stdout_path, metadata, binding, started_ns)
            payload = load_object(capture, "capture")
        session = payload.get("session_id"); response = payload.get("result")
        if not isinstance(session, str) or not session.strip() or not isinstance(response, str) or not response.strip():
            raise RunnerError("capture session/response is empty or malformed")
        if (binding is not None and binding.get("predecessor") is not None
                and binding["predecessor"].get("session_id") == session.strip()):
            raise RunnerError("Reviewer B session must be distinct from Reviewer A")
        if payload.get("is_error") is True or str(payload.get("subtype", "")).lower() in {"error", "failure", "failed"}:
            raise RunnerError("managed child reported semantic failure")
        structured_result = (structured_review_result(response.encode(), binding)
                             if binding is not None else None)
        atomic_bytes(response_path, canonical(structured_result) if structured_result is not None
                     else response.encode())
        declared_terminal = payload.get("terminal_outcome")
        if declared_terminal is not None and (not isinstance(declared_terminal, dict)
                or set(declared_terminal) != {"schema_version", "state"}
                or declared_terminal.get("schema_version") != TERMINAL_RESULT_SCHEMA
                or declared_terminal.get("state") not in TERMINAL_STATES):
            raise RunnerError("capture terminal outcome is malformed")
        if args.require_terminal_result and declared_terminal is None:
            raise RunnerError("capture lacks required typed terminal outcome")
        terminal_result = None if declared_terminal is None else {
            "schema_version": TERMINAL_RESULT_SCHEMA,
            "state": declared_terminal["state"],
            "workflow_step_digest": os.environ.get("JUNO_WORKFLOW_STEP_DIGEST", ""),
            "session_id": session.strip(),
            "identity_sha256": sha(canonical(identity).rstrip(b"\n")),
            "response_sha256": sha(response_path.read_bytes()),
        }
        controller_after = controller_identity(controller_root)
        verify_compatible_config(compatible_config)
        if controller_after != controller_before:
            raise RunnerError("controller mutated during managed launch")
        if args.mode == "reviewer" and subject_after != subject_before:
            raise RunnerError("review candidate mutated during managed launch")
        if args.mode == "worker":
            allowed = identity["expected_paths"]
            changed = sorted(set(git(Path(args.agent_root), "diff", "--name-only", identity["before"]["head"], subject_after["head"]).splitlines()))
            unexpected = [p for p in changed if not any(p == a or p.startswith(a.rstrip("/") + "/") for a in allowed)]
            conflict_worker = identity.get("admission_kind") == "sealed_release_epoch_conflict"
            parents = git(Path(args.agent_root), "show", "-s", "--format=%P", subject_after["head"]).split()
            conflict_identity_failed = (conflict_worker and
                (identity["ours_sha"] not in parents or identity["theirs_sha"] not in parents))
            branch_failed = (not conflict_worker and
                             subject_after["branch_ref"] != subject_before["branch_ref"])
            if (branch_failed or conflict_identity_failed
                    or subject_after["git_common_dir"] != subject_before["git_common_dir"]
                    or subject_after["status"] or unexpected):
                raise RunnerError("worker post-launch changed-path or identity authority failed")
            identity["changed_paths"] = changed; identity["unexpected_paths"] = unexpected
        terminal = {"schema_version": SCHEMA, "state": "succeeded", "completed_at": now(), "exit_code": 0,
                    "elapsed_seconds": round(time.monotonic() - started, 3), "session_id": session.strip(),
                    "producer_completed_at": producer_completed_at,
                    "producer_elapsed_seconds": producer_elapsed, "timed_out": False,
                    "child_pid": proc.pid, "process_group_id": proc.pid,
                    "exit_signal": signal.Signals(-code).name if code < 0 else None,
                    "termination_events": termination_events,
                    "live_log": {"path": str(live_log_path), "sha256": sha(live_log_path.read_bytes())},
                    "semantic_outcome": (terminal_result or {}).get("state", "completed"),
                    "terminal_result": terminal_result,
                    "compatible_config_sha256": compatible_config["sha256"],
                    "capture_source": capture_source,
                    "capture_recovery": ({"decision": "recovered",
                        "binding": payload.get("recovery_binding"),
                        "terminal_receipt": evidence(out / "capture-terminal.json")}
                        if (out / "capture-terminal.json").is_file()
                        else {"decision": "not_applicable"}),
                    "safe_next_action": "consume_receipt"}
        artifacts = {name: evidence(path) for name, path in (("prompt", prompt), ("launch", out / "launch.json"),
                    ("stdout", stdout_path), ("stderr", stderr_path), ("combined", combined_path),
                    ("capture", capture), ("response", response_path))}
        capture_terminal = out / "capture-terminal.json"
        if capture_terminal.is_file():
            artifacts["capture_terminal"] = evidence(capture_terminal)
        if binding is None:
            artifacts["prompt"]["echo"] = prompt_echo
        receipt = {**terminal, "mode": args.mode, "controller_before": controller_before, "controller_after": controller_after,
                   "tool_id": args.tool_id, "review_binding": binding,
                   "identity": identity, "subject_after": subject_after, "argv": argv, "argv_sha256": launch["argv_sha256"],
                   "compatible_config": compatible_config, "terminal_result": terminal_result,
                   "effective_hook_policy": effective_hook_policy,
                   "environment_contract": {**env_contract, "explicitly_set_key_names": env_contract["explicit_key_names"]},
                   "command_sha256": launch["argv_sha256"], "cwd": str(launcher), "artifacts": artifacts}
        atomic_json(out / "receipt.json", receipt); atomic_json(out / "terminal.json", terminal); (out / "active.json").unlink()
        print(json.dumps({"receipt": str((out / "receipt.json").resolve()), "session_id": session.strip(), "response": response}))
        return 0
    except Exception as exc:
        cleanup_error: Exception | None = None
        if proc is not None and group_active(proc.pid):
            try: terminate_group_and_wait(termination_events, proc.pid, "runner_failure", started)
            except Exception as group_exc: cleanup_error = group_exc
        if proc is not None and proc.returncode is None:
            try: proc.wait(timeout=2)
            except subprocess.TimeoutExpired: cleanup_error = cleanup_error or RunnerError("managed producer did not reap")
        try: live_log.close()
        except OSError: pass
        exit_code = proc.returncode if proc and proc.returncode is not None else 1
        if proc is not None and not producer_completed:
            producer_completed_at, producer_elapsed = announce_completion(
                started, exit_code, timed_out, live_log_path)
        else:
            producer_completed_at = locals().get("producer_completed_at", now())
            producer_elapsed = locals().get("producer_elapsed", round(time.monotonic() - started, 3))
        failure = str(cleanup_error or exc)[:512]
        reason_code = (failure.split(":", 1)[0] if failure.startswith("worker_capture_recovery_")
                       else "managed_agent_failure")
        terminal = {"schema_version": SCHEMA, "state": "interrupted" if interrupted else "failed", "completed_at": now(),
                    "exit_code": exit_code, "timed_out": timed_out,
                    "child_pid": proc.pid if proc else None,
                    "process_group_id": proc.pid if proc else None,
                    "exit_signal": signal.Signals(-exit_code).name if exit_code < 0 else None,
                    "interrupted_signal": signal.Signals(interrupted).name if interrupted else None,
                    "termination_events": termination_events,
                    "producer_completed_at": producer_completed_at,
                    "producer_elapsed_seconds": producer_elapsed,
                    "live_log": {"path": str(live_log_path), "sha256": sha(live_log_path.read_bytes())},
                    "elapsed_seconds": round(time.monotonic() - started, 3), "semantic_outcome": "failed",
                    "compatible_config_sha256": compatible_config["sha256"],
                    "failure_type": type(cleanup_error or exc).__name__,
                    "failure": failure, "reason_code": reason_code,
                    "safe_next_action": "inspect_terminal_and_start_fresh_output_directory"}
        atomic_json(out / "terminal.json", terminal); atomic_json(out / "receipt.json", {
            **terminal, "mode": args.mode, "identity": identity, "tool_id": args.tool_id,
            "review_binding": binding, "effective_hook_policy": effective_hook_policy,
            "launch": evidence(out / "launch.json"),
        })
        (out / "active.json").unlink(missing_ok=True)
        raise
    finally:
        for sig, handler in old_handlers.items(): signal.signal(sig, handler)


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(description=__doc__, allow_abbrev=False); sub = top.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run", allow_abbrev=False)
    p.add_argument("--mode", choices=("worker", "reviewer"), required=True)
    for name in ("controller-root", "controller-branch", "agent-root", "prompt-file", "out-dir"):
        p.add_argument("--" + name, required=True)
    p.add_argument("--tool-id", default="managed_agent_runner")
    p.add_argument("--task-id"); p.add_argument("--create-receipt"); p.add_argument("--task-root-receipt", dest="create_receipt")
    p.add_argument("--verify-receipt"); p.add_argument("--edit-preflight-receipt"); p.add_argument("--authority-map")
    p.add_argument("--candidate-sha"); p.add_argument("--candidate-root")
    p.add_argument("--review-binding")
    p.add_argument("--require-terminal-result", action="store_true")
    p.add_argument("--external-side-effects", choices=("forbidden",), default="forbidden")
    p.add_argument("--lifecycle-hooks", choices=("disabled",), default="disabled")
    p.add_argument("--timeout-seconds", type=float, default=7200.0)
    recover = sub.add_parser("recover-worker-capture", allow_abbrev=False)
    recover.add_argument("--failed-receipt", required=True)
    recover.add_argument("--out-dir", required=True)
    return top


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "recover-worker-capture":
            return recover_worker_capture(args)
        return run(args)
    except RunnerError as exc:
        print(f"managed_agent_runner.py: {exc}", file=sys.stderr); return 1
    except (OSError, ValueError) as exc:
        print(f"managed_agent_runner.py: launch refused: {exc}", file=sys.stderr); return 1


if __name__ == "__main__":
    raise SystemExit(main())
