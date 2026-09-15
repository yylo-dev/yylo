#!/usr/bin/env python3
"""Resolve the canonical Juno controller checkout without changing Git state."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Optional

sys.dont_write_bytecode = True

VALID_ROLES = {"controller", "controller-retired", "task", "integration-owner"}
VALID_ENFORCEMENT = {"off", "warn", "strict"}


def git(cwd: Path, *args: str) -> Optional[str]:
    result = subprocess.run(["git", "-C", str(cwd), *args], text=True, capture_output=True)
    return result.stdout.strip() if result.returncode == 0 else None


def canonical(value: str, base: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def repository_identity(path: Path) -> Optional[str]:
    common = git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return str(Path(common).resolve()) if common else None


def normalized_branch_ref(value: Optional[str]) -> Optional[str]:
    """Compare full and short local branch spellings without weakening ref identity."""
    if not value:
        return value
    return value if value.startswith("refs/heads/") else f"refs/heads/{value}"


def config(cwd: Path, key: str) -> Optional[str]:
    return git(cwd, "config", "--local", "--get", key)


def config_values(cwd: Path, key: str) -> list[str]:
    value = git(cwd, "config", "--local", "--get-all", key)
    return value.splitlines() if value else []


def worktree_config(cwd: Path, key: str) -> Optional[str]:
    """Read checkout-specific persisted identity; never infer it from process env."""
    return git(cwd, "config", "--worktree", "--get", key)


def is_primary_worktree(cwd: Path) -> bool:
    git_dir = git(cwd, "rev-parse", "--path-format=absolute", "--git-dir")
    common = git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return bool(git_dir and common and Path(git_dir).resolve() == Path(common).resolve())


class ResolverError(Exception):
    def __init__(self, message: str, result: dict[str, object]):
        super().__init__(message)
        self.result = result


def fail(message: str, result: dict[str, object]) -> None:
    result["valid"] = False
    result["diagnostics"] = [*result.get("diagnostics", []), message]
    raise ResolverError(message, result)


def resolve_simple(cwd: Path, root: Path, has_git: bool, operation: str) -> Optional[dict[str, object]]:
    """Recognize explicit Simple authority before inherited controller routing."""
    marker = root / ".juno_task/config.json"
    result: dict[str, object] = {
        "path": str(root), "current_root": str(root), "invocation_cwd": str(cwd),
        "resolver": "installed", "source": "workspace-config", "role": "simple",
        "expected_branch": None, "actual_branch": None, "enforcement": "strict",
        "operation": operation, "valid": True, "diagnostics": [],
        "workspace_mode": "simple", "workspace_version": 1,
        "capabilities": ["diagnostics", "local-agent", "ledger", "local-task-bookkeeping"],
    }
    # A Simple marker below/above this Git root must not cross a project boundary.
    for directory in (cwd, *cwd.parents):
        if directory == root:
            continue
        other = directory / ".juno_task/config.json"
        if other.is_file():
            try:
                document = json.loads(other.read_text(encoding="utf-8"))
                mode = document.get("controllerWorkspace", {}) if isinstance(document, dict) else {}
                if isinstance(mode, dict) and mode.get("mode") == "simple":
                    fail(f"nested Simple/Git roots are unsupported: {other}; invoke from its primary project", result)
            except (OSError, ValueError):
                pass
    if not marker.exists():
        return None
    try:
        raw = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        fail(f"invalid workspace configuration {marker}: {exc}", result)
    if not isinstance(raw, dict):
        fail(f"workspace configuration must be an object: {marker}", result)
    workspace = raw.get("controllerWorkspace")
    if "controllerWorkspace" not in raw:
        return None
    if not isinstance(workspace, dict):
        fail(f"invalid controllerWorkspace in {marker}", result)
    # Retain legacy managed/sparse diagnostics in the managed resolver.
    if workspace.get("mode") == "metadata-only":
        if workspace != {"mode": "metadata-only", "policy": ".juno_task/config/metadata-controller.json"}:
            fail(f"invalid metadata-only workspace configuration: {marker}", result)
        return None
    if "mode" not in workspace and workspace.get("enabled") is True:
        return None
    if workspace != {"mode": "simple", "version": 1} or type(workspace.get("version")) is not int:
        fail(f"unknown or contradictory workspace mode/version in {marker}; refusing fallback", result)
    if not has_git or not is_primary_worktree(root):
        fail("Simple MVP requires a primary Git checkout; no-Git and linked worktrees are unsupported", result)
    if marker.is_symlink() or marker.resolve().parent != root / ".juno_task" or (root / ".juno_task").is_symlink():
        fail("Simple workspace configuration must be local, not symlinked", result)
    registration = git(root, "config", "--local", "--get-regexp", r"^juno\.(controller|workspace)\.")
    role = git(root, "config", "--worktree", "--get-regexp", r"^juno\.(controller|workspace)\.")
    remnants = ["metadata-controller.json", "controller-workspace.json", "task-workspace.json", "integration-workspace.json"]
    if registration or role or any((root / ".juno_task/config" / name).exists() for name in remnants):
        fail("Simple marker conflicts with managed registration or policy; use a separately authorized fresh-workspace transition", result)
    for name in ("JUNO_TASK_ROOT", "JUNO_CONTROL_EFFECTIVE_ROOT", "JUNO_CONTROL_INVOCATION_ROOT"):
        value = os.environ.get(name, "").strip()
        if value and canonical(value, root) != root:
            fail(f"{name} assertion mismatch: Simple root is {root}", result)
    for name in ("JUNO_WORKSPACE_ROLE", "JUNO_CONTROL_INVOCATION_ROLE"):
        value = os.environ.get(name, "").strip()
        if value and value != "simple":
            fail(f"{name} assertion mismatch: Simple workspace is not {value}", result)
    if os.environ.get("JUNO_CONTROLLER_BRANCH", "").strip():
        fail("JUNO_CONTROLLER_BRANCH assertion mismatch: Simple has no managed controller branch", result)
    result["actual_branch"] = git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    result["git_common_dir"] = repository_identity(root)
    if operation == "orchestration":
        fail("Simple workspace does not support managed task/merge/integration operations; use yy ledger for local bookkeeping or explicit Git commands", result)
    return result


def resolve(cwd: Path, operation: str, ignore_environment_assertions: bool = False) -> dict[str, object]:
    cwd = cwd.resolve()
    repo_root_text = git(cwd, "rev-parse", "--show-toplevel")
    current_root = Path(repo_root_text).resolve() if repo_root_text else cwd
    simple = resolve_simple(cwd, current_root, bool(repo_root_text), operation)
    if simple is not None:
        return simple
    if ignore_environment_assertions:
        for key in ("JUNO_TASK_ROOT", "JUNO_CONTROLLER_BRANCH", "JUNO_WORKSPACE_ROLE"):
            os.environ.pop(key, None)
    enforcement = os.environ.get("JUNO_WORKSPACE_ENFORCEMENT", "off").strip().lower()
    if enforcement not in VALID_ENFORCEMENT:
        enforcement = "strict"

    explicit = os.environ.get("JUNO_TASK_ROOT", "").strip()
    path_values = config_values(cwd, "juno.controller.path") if repo_root_text else []
    branch_values = config_values(cwd, "juno.controller.branch") if repo_root_text else []
    registration_errors: list[str] = []
    if len(path_values) > 1:
        registration_errors.append("controller registration is ambiguous: juno.controller.path has multiple values")
    if len(branch_values) > 1:
        registration_errors.append("controller registration is ambiguous: juno.controller.branch has multiple values")
    if bool(path_values) != bool(branch_values):
        registration_errors.append("controller registration requires exactly one path and one branch value")
    registered = path_values[0] if len(path_values) == 1 else None
    local_initialized_root = not repo_root_text and (current_root / ".juno_task").is_dir()
    # Environment is routing/assertion only. Persisted controller registration,
    # checkout topology, or an initialized non-Git project determine identity.
    # The latter must not inherit an unrelated parent shell's controller route.
    source = (
        "registration" if registered else
        "primary-worktree" if repo_root_text else
        "non-git-current-root" if local_initialized_root else
        "environment" if explicit else "current-root"
    )
    persisted_controller = canonical(registered, current_root) if registered else (
        current_root if (repo_root_text and is_primary_worktree(current_root)) or local_initialized_root else None
    )
    asserted_controller = canonical(explicit, current_root) if explicit and not local_initialized_root else None
    controller = persisted_controller or asserted_controller or current_root
    expected_branch = branch_values[0] if registered and len(branch_values) == 1 else None
    asserted_branch = os.environ.get("JUNO_CONTROLLER_BRANCH", "").strip() or None
    persisted_role = worktree_config(cwd, "juno.workspace.role") if repo_root_text else None
    role_base = worktree_config(cwd, "juno.workspace.roleBase") if repo_root_text else None
    task_id = worktree_config(cwd, "juno.workspace.taskId") if repo_root_text else None
    manifest_identity = worktree_config(cwd, "juno.workspace.manifestIdentity") if repo_root_text else None
    create_receipt_sha256 = worktree_config(cwd, "juno.workspace.createReceiptSha256") if repo_root_text else None
    expected_paths_sha256 = worktree_config(cwd, "juno.workspace.expectedPathsSha256") if repo_root_text else None
    role_authority = worktree_config(cwd, "juno.workspace.roleAuthority") if repo_root_text else None
    if not repo_root_text:
        role = "controller"
        role_source = "non-git-current-root"
    elif persisted_controller == current_root:
        role = "controller"
        role_source = "controller-registration" if registered else "primary-worktree"
    else:
        role = persisted_role or "unregistered"
        role_source = "worktree-registration" if persisted_role else "missing-worktree-registration"
    asserted_role = os.environ.get("JUNO_WORKSPACE_ROLE", "").strip() or None
    result: dict[str, object] = {
        "path": str(controller), "current_root": str(current_root), "resolver": "installed",
        "source": source, "expected_branch": expected_branch,
        "actual_branch": None, "role": role, "role_source": role_source, "role_base": role_base,
        "task_id": task_id, "manifest_identity": manifest_identity,
        "create_receipt_sha256": create_receipt_sha256,
        "expected_paths_sha256": expected_paths_sha256, "role_authority": role_authority,
        "role_assertion": asserted_role, "enforcement": enforcement,
        "operation": operation, "valid": True, "diagnostics": [], "controller_workspace": None,
        "git_common_dir": None, "controller_ref": None, "controller_head": None,
    }

    if repo_root_text and role in {"task", "integration-owner", "controller-retired"} \
            and not path_values and not branch_values:
        fail(
            "linked product workspace requires exactly one non-empty controller path and branch registration",
            result,
        )

    errors: list[str] = list(registration_errors)
    if not controller.is_dir():
        errors.append(f"configured controller path does not exist: {controller}")
    elif not (controller / ".juno_task").is_dir():
        errors.append(f"configured controller has no .juno_task directory: {controller}")
    else:
        actual_branch = git(controller, "symbolic-ref", "--quiet", "--short", "HEAD")
        actual_full_branch = git(controller, "symbolic-ref", "--quiet", "HEAD")
        result["actual_branch"] = actual_branch
        result["controller_ref"] = actual_full_branch
        result["controller_head"] = git(controller, "rev-parse", "HEAD")
        result["git_common_dir"] = repository_identity(controller)
        if repo_root_text:
            current_identity = repository_identity(current_root)
            controller_identity = repository_identity(controller)
            if not current_identity or current_identity != controller_identity:
                errors.append("configured controller is not a linked worktree of the invoking repository")
        if expected_branch and normalized_branch_ref(actual_full_branch) != normalized_branch_ref(expected_branch):
            errors.append(f"controller branch mismatch: expected {expected_branch!r}, found {actual_branch or 'detached HEAD'!r}")
        workspace_pointer = controller / ".juno_task/config.json"
        try:
            project_config = json.loads(workspace_pointer.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            project_config = {}
        workspace = project_config.get("controllerWorkspace")
        if isinstance(workspace, dict) and workspace.get("enabled") is True:
            policy_relative = workspace.get("policy")
            expected_policy = ".juno_task/config/controller-workspace.json"
            if policy_relative != expected_policy:
                errors.append("controller workspace policy pointer is missing or noncanonical")
            else:
                try:
                    import controller_workspace
                    policy = controller_workspace.load_policy(controller / expected_policy)
                    evidence = controller_workspace.inspect(controller, policy)
                    result["controller_workspace"] = evidence
                    if not evidence["passed"]:
                        failed = sorted(name for name, passed in evidence["checks"].items() if not passed)
                        message = "canonical sparse controller policy refused: " + ",".join(failed)
                        if operation == "diagnostic" and failed == ["clean"]:
                            result["diagnostics"].append(message)
                        else:
                            errors.append(message)
                except (ImportError, controller_workspace.WorkspaceError, OSError) as exc:
                    errors.append(f"canonical sparse controller verification failed: {exc}")
    if asserted_controller and persisted_controller and asserted_controller != persisted_controller:
        errors.append(f"JUNO_TASK_ROOT assertion mismatch: persisted={persisted_controller} asserted={asserted_controller}")
    if asserted_branch and expected_branch and normalized_branch_ref(asserted_branch) != normalized_branch_ref(expected_branch):
        errors.append(f"JUNO_CONTROLLER_BRANCH assertion mismatch: persisted={expected_branch!r} asserted={asserted_branch!r}")
    if role == "unregistered":
        errors.append("linked worktree has no persisted workspace role registration; register it through the lifecycle owner")
    elif role not in VALID_ROLES:
        errors.append(f"invalid persisted workspace role {role!r}; expected controller, controller-retired, task, or integration-owner")
    task_authority = (task_id, manifest_identity, create_receipt_sha256, expected_paths_sha256)
    if role == "task" and not all(task_authority):
        errors.append("task workspace registration is incomplete: exact lifecycle receipt identity is required")
    if role != "task" and any(task_authority):
        errors.append("non-task workspace carries task lifecycle identity")
    if role == "integration-owner" and role_authority != "protected-integration.v1":
        errors.append("integration-owner workspace lacks protected integration authority")
    if role != "integration-owner" and role_authority:
        errors.append("non-integration-owner workspace carries protected role authority")
    if asserted_role and asserted_role not in VALID_ROLES:
        errors.append(f"invalid JUNO_WORKSPACE_ROLE assertion {asserted_role!r}")
    elif asserted_role and asserted_role != role:
        errors.append(f"JUNO_WORKSPACE_ROLE assertion mismatch: persisted={role!r} asserted={asserted_role!r}")

    role_problem = role == "integration-owner" and operation in {"kanban", "orchestration", "session-write"}
    if role_problem:
        message = f"integration-owner workspace refuses {operation} writes; launch from the controller and pass TASK_ROOT explicitly"
        if enforcement == "strict":
            errors.append(message)
        elif enforcement == "warn":
            result["diagnostics"] = [message]
            print(f"controller-resolver: warning: {message}", file=sys.stderr)
    if role == "controller-retired" and operation in {"kanban", "orchestration", "session-write", "product-edit"}:
        errors.append("retired rollback controller is read-only; run writes from the registered metadata controller")

    # Explicit and registered settings are authoritative: invalid values never fall back.
    if errors:
        fail("; ".join(errors), result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--operation", choices=["diagnostic", "kanban", "orchestration", "session-write", "product-edit"], default="diagnostic")
    parser.add_argument("--format", choices=["json", "root", "shell"], default="json")
    parser.add_argument("--register", metavar="PATH", help="Register the canonical controller checkout")
    parser.add_argument("--branch", help="Exact branch of the canonical controller checkout")
    parser.add_argument("--ignore-environment-assertions", action="store_true", help="Ignore managed routing assertions only; Simple assertions always remain authoritative")
    args = parser.parse_args()
    cwd = Path(args.cwd).resolve()
    if bool(args.register) != bool(args.branch):
        parser.error("--register PATH and --branch BRANCH are required together")
    if args.register:
        root = git(cwd, "rev-parse", "--show-toplevel")
        if not root:
            raise SystemExit("controller-resolver: registration requires a Git worktree")
        resolve_simple(cwd, Path(root), True, "orchestration")
        target = canonical(args.register, Path(root))
        resolve_simple(target, target, True, "orchestration")
        target_root = git(target, "rev-parse", "--show-toplevel")
        if not target_root or Path(target_root).resolve() != target:
            raise SystemExit("controller-resolver: registered controller must be an exact Git worktree root")
        if repository_identity(Path(root)) != repository_identity(target):
            raise SystemExit("controller-resolver: registered controller must belong to the invoking repository")
        if not (target / ".juno_task").is_dir():
            raise SystemExit("controller-resolver: registered controller has no .juno_task directory")
        actual_branch = git(target, "symbolic-ref", "--quiet", "--short", "HEAD")
        actual_full_branch = git(target, "symbolic-ref", "--quiet", "HEAD")
        if normalized_branch_ref(actual_full_branch) != normalized_branch_ref(args.branch):
            raise SystemExit(f"controller-resolver: controller branch mismatch: expected {args.branch!r}, found {actual_branch or 'detached HEAD'!r}")
        head = git(target, "rev-parse", "HEAD")
        if not head:
            raise SystemExit("controller-resolver: registered controller requires a readable HEAD")
        existing_paths = config_values(cwd, "juno.controller.path")
        existing_branches = config_values(cwd, "juno.controller.branch")
        if len(existing_paths) > 1 or len(existing_branches) > 1:
            raise SystemExit("controller-resolver: existing controller registration is ambiguous")
        existing_path = existing_paths[0] if existing_paths else None
        existing_branch = existing_branches[0] if existing_branches else None
        if existing_path or existing_branch:
            matching_path = bool(existing_path and canonical(existing_path, Path(root)) == target)
            matching_branch = bool(existing_branch and normalized_branch_ref(existing_branch) == normalized_branch_ref(args.branch))
            if not (matching_path and matching_branch):
                raise SystemExit("controller-resolver: changing an existing controller registration requires `yy migrate registration plan` and an explicitly authorized apply")
        # Canonical controller registration establishes initial audit authority
        # once. Re-registration must never bless commits added since that base.
        subprocess.run(["git", "-C", str(cwd), "config", "--local", "extensions.worktreeConfig", "true"], check=True)
        if not worktree_config(target, "juno.workspace.roleBase"):
            subprocess.run(["git", "-C", str(target), "config", "--worktree", "juno.workspace.roleBase", head], check=True)
        if not existing_path and not existing_branch:
            subprocess.run(["git", "-C", str(cwd), "config", "--local", "juno.controller.path", str(target)], check=True)
            subprocess.run(["git", "-C", str(cwd), "config", "--local", "juno.controller.branch", args.branch], check=True)
    result = resolve(cwd, args.operation, args.ignore_environment_assertions)
    if args.format == "root":
        print(result["path"])
    elif args.format == "shell":
        print(f"export JUNO_TASK_ROOT={shlex.quote(str(result['path']))}")
        print(f"export JUNO_CONTROLLER_SOURCE={shlex.quote(str(result['source']))}")
        print(f"export JUNO_WORKSPACE_ROLE={shlex.quote(str(result['role']))}")
        if result["role"] == "simple":
            print("unset JUNO_KANBAN_CONTROLLER_BINDING")
            return
        binding = {"git_common_dir": result["git_common_dir"],
                   "controller_path": result["path"],
                   "controller_ref": result["controller_ref"],
                   "controller_head": result["controller_head"]}
        print(f"export JUNO_KANBAN_CONTROLLER_BINDING={shlex.quote(json.dumps(binding, sort_keys=True))}")
    else:
        print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except ResolverError as exc:
        print(json.dumps(exc.result, sort_keys=True))
        print(f"controller-resolver: {exc}", file=sys.stderr)
        raise SystemExit(2)
