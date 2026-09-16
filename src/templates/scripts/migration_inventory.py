#!/usr/bin/env python3
"""Read-only Juno 2.0 -> 2.1 inventory and reviewed policy generator."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

SCHEMA = "juno_migration_inventory.v1"
POLICY_SCHEMA = "juno_migration_policy_bundle.v1"
ANSWERS_SCHEMA = "juno_migration_owner_answers.v1"
DISPOSITIONS = ("keep", "replace", "retire", "externalize", "block")
CONFIG_DISPOSITIONS = ("migrate", "transform", "product-only", "secret", "retire")
CONFIG_FIELD_CLASSIFICATION = {
    "configVersion": "transform", "controllerWorkspace": "transform",
    "defaultSubagent": "migrate", "defaultBackend": "migrate",
    "defaultMaxIterations": "migrate", "defaultModel": "migrate",
    "defaultModels": "migrate", "workflowModels": "migrate", "mainTask": "migrate",
    "logLevel": "migrate", "logFile": "migrate", "verbose": "migrate", "quiet": "migrate",
    "mcpTimeout": "migrate", "mcpRetries": "migrate", "mcpServerPath": "migrate",
    "mcpServerName": "migrate", "hookCommandTimeout": "migrate", "onHourlyLimit": "migrate",
    "interactive": "migrate", "headlessMode": "migrate", "kanbanRegistry": "migrate",
    "promptMacros": "transform", "gitCheckpoint": "migrate",
    "workingDirectory": "product-only", "sessionDirectory": "product-only",
    "gitFlow": "product-only", "autoDependencyUpdate": "product-only",
    "hooks": "product-only", "skipHooks": "product-only",
    "envFilePath": "secret", "envFileCopied": "secret", "lifecycle": "retire",
}
CONTROLLER_PRIVATE_DEFAULTS = [
    ".juno_task/artifacts", ".juno_task/cutover.json", ".juno_task/ledger",
    ".juno_task/logs", ".juno_task/receipts", ".juno_task/specs",
    ".juno_task/state", ".juno_task/tasks", ".juno_task/tasks.md",
    ".juno_task/workflows",
]
SECRET_NAME = re.compile(r"(^|/)(\.env($|\.)|.*(credential|secret|token|password|private[_-]?key).*)", re.I)
VERSION = re.compile(r"(?<![0-9])([0-9]+\.[0-9]+\.[0-9]+)(?![-+0-9A-Za-z.])")


class InventoryError(RuntimeError):
    pass


def run(argv: list[str], cwd: Path, *, check: bool = True) -> str:
    result = subprocess.run(
        argv, cwd=cwd, text=True, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )
    if check and result.returncode:
        raise InventoryError(f"command failed ({' '.join(argv[:3])}): {result.stderr.strip()}")
    return result.stdout


def git(root: Path, *args: str, check: bool = True) -> str:
    return run(["git", "-c", "core.fsmonitor=false", *args], root, check=check).strip()


def exact_repository_root(candidate: Path) -> Path | None:
    candidate = candidate.resolve()
    discovered = git(candidate, "rev-parse", "--show-toplevel", check=False) if candidate.exists() else ""
    return candidate if discovered and Path(discovered).resolve() == candidate else None


def digest(path: Path) -> str | None:
    if not path.is_file() or path.is_symlink():
        return None
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def safe_path(root: Path, relative: str) -> dict[str, Any]:
    path = root / relative
    size = path.stat().st_size if path.is_file() else None
    # Names that can themselves expose credentials are represented by an opaque,
    # stable identity. Contents and environment values are never collected.
    if SECRET_NAME.search(relative):
        return {"path_sha256": hashlib.sha256(relative.encode()).hexdigest(), "redacted": True, "size": size}
    return {"path": relative, "redacted": False, "size": size}


def parse_worktrees(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for line in git(root, "worktree", "list", "--porcelain").splitlines() + [""]:
        if not line:
            if current:
                rows.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            current["path"] = str(Path(value).resolve())
        elif key == "HEAD":
            current["head"] = value
        elif key == "branch":
            current["branch"] = value
        elif key in ("detached", "bare", "prunable", "locked"):
            current[key] = value or True
    return sorted(rows, key=lambda row: row.get("path", ""))


def git_identity(root: Path, product_ref: str | None) -> dict[str, Any]:
    head = git(root, "rev-parse", "HEAD")
    branch = git(root, "symbolic-ref", "-q", "HEAD", check=False) or None
    exact_refs = sorted(filter(None, git(root, "for-each-ref", "--format=%(refname)", "--points-at", head).splitlines()))
    local_refs = {}
    for line in git(root, "for-each-ref", "--format=%(refname)%00%(objectname)", "refs/heads").splitlines():
        name, separator, sha = line.partition("\0")
        if separator:
            local_refs[name] = sha
    upstream = git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}", check=False) or None
    ahead = behind = None
    if upstream:
        counts = git(root, "rev-list", "--left-right", "--count", f"{upstream}...HEAD").split()
        if len(counts) == 2:
            behind, ahead = map(int, counts)
    candidates = sorted(ref for ref in exact_refs if ref.startswith("refs/heads/"))
    selected_head = None
    if product_ref:
        valid_ref = subprocess.run(
            ["git", "check-ref-format", product_ref], cwd=root, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        ).returncode == 0
        if not product_ref.startswith("refs/heads/") or not valid_ref:
            raise InventoryError("product_ref must be a valid full refs/heads/* ref")
        selected_head = git(root, "rev-parse", f"{product_ref}^{{commit}}", check=False) or None
        if not selected_head:
            raise InventoryError(f"product_ref does not resolve: {product_ref}")
    selected_upstream = selected_ahead = selected_behind = None
    if product_ref:
        selected_upstream = git(root, "for-each-ref", "--format=%(upstream)", product_ref, check=False) or None
        if selected_upstream:
            counts = git(root, "rev-list", "--left-right", "--count", f"{selected_upstream}...{product_ref}").split()
            if len(counts) == 2:
                selected_behind, selected_ahead = map(int, counts)
    return {
        "root": str(root),
        "git_common_dir": str(Path(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()),
        "head": head, "branch": branch, "detached": branch is None,
        "refs_at_head": exact_refs, "product_ref_candidates": candidates,
        "local_product_refs": dict(sorted(local_refs.items())),
        "selected_product_ref": product_ref, "selected_product_head": selected_head,
        "checkout_matches_selected_product": selected_head == head if selected_head else None,
        "selected_product_upstream": selected_upstream,
        "selected_product_ahead": selected_ahead, "selected_product_behind": selected_behind,
        "selected_product_diverged": bool(selected_ahead and selected_behind),
        "product_ref_ambiguous": product_ref is None and (branch is None or len(candidates) != 1),
        "upstream": upstream, "ahead": ahead, "behind": behind,
        "diverged": bool(ahead and behind),
        "worktrees": parse_worktrees(root),
    }


def status_inventory(root: Path) -> dict[str, Any]:
    entries = []
    raw = run(["git", "-c", "core.fsmonitor=false", "status", "--porcelain=v1", "-z", "--untracked-files=all"], root)
    items = iter(raw.split("\0"))
    for item in items:
        if not item:
            continue
        code, relative = item[:2], item[3:]
        row = {"code": code, **safe_path(root, relative)}
        if "R" in code or "C" in code:
            original = next(items, "")
            if original:
                row["original"] = safe_path(root, original)
        entries.append(row)
    return {"clean": not entries, "entries": sorted(entries, key=lambda x: (x.get("path", ""), x.get("path_sha256", "")))}


def tracked_controller_roots(root: Path) -> list[dict[str, Any]]:
    tracked = set(nul_git_paths(root, "ls-files"))
    blob_sizes: dict[str, int] = {}
    for line in git(root, "ls-tree", "-r", "-l", "HEAD").splitlines():
        metadata, separator, name = line.partition("\t")
        fields = metadata.split()
        if separator and len(fields) == 4 and fields[3].isdigit():
            blob_sizes[name] = int(fields[3])
    rows = []
    for prefix in CONTROLLER_PRIVATE_DEFAULTS:
        matches = [name for name in tracked if name == prefix or name.startswith(prefix + "/")]
        if matches:
            rows.append({"path": prefix, "tracked_files": len(matches),
                         "tracked_blob_bytes": sum(blob_sizes.get(name, 0) for name in matches)})
    return rows


def nul_git_paths(root: Path, *args: str) -> list[str]:
    return sorted(item for item in run(["git", "-c", "core.fsmonitor=false", *args, "-z"], root).split("\0") if item)


def ignored_group(name: str) -> str:
    if SECRET_NAME.search(name):
        return "redacted-secret-like"
    parts = Path(name).parts
    if len(parts) > 1 and parts[0] == ".juno_task":
        return f".juno_task/{parts[1]}"
    return parts[0] if parts else "<root>"


def ignored_and_heavy(root: Path, threshold: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    tracked = nul_git_paths(root, "ls-files")
    ignored = nul_git_paths(root, "ls-files", "--others", "--ignored", "--exclude-standard")
    untracked = nul_git_paths(root, "ls-files", "--others", "--exclude-standard")
    groups: dict[str, dict[str, Any]] = {}
    for name in ignored:
        group = ignored_group(name)
        row = groups.setdefault(group, {"group": group, "count": 0, "bytes": 0, "size_scan_complete": True})
        row["count"] += 1
        try:
            path = root / name
            if path.is_file() and not path.is_symlink():
                row["bytes"] += path.stat().st_size
        except OSError:
            row["size_scan_complete"] = False
    heavy = []
    for name in sorted(set(tracked + ignored + untracked)):
        path = root / name
        try:
            if path.is_file() and not path.is_symlink() and path.stat().st_size >= threshold:
                heavy.append({"tracked": name in tracked, **safe_path(root, name)})
        except OSError:
            continue
    key = lambda row: (row.get("path", ""), row.get("path_sha256", ""))
    return sorted(groups.values(), key=lambda row: row["group"]), sorted(heavy, key=key)


def custom_project_assets(root: Path) -> list[dict[str, Any]]:
    manifest = read_json(root / ".juno_task/managed-assets.json")
    known = set(manifest.get("assets", {})) if isinstance(manifest.get("assets"), dict) else set()
    tracked = set(nul_git_paths(root, "ls-files"))
    ordinary = set(nul_git_paths(root, "ls-files", "--others", "--exclude-standard"))
    ignored = set(nul_git_paths(root, "ls-files", "--others", "--ignored", "--exclude-standard"))
    roots = (".juno_task/scripts/", ".juno_task/prompts/", ".juno_task/config/")
    rows = []
    for name in sorted((tracked | ordinary | ignored) - known):
        path = root / name
        if name.startswith(roots) and path.is_file():
            identity = safe_path(root, name)
            rows.append({**identity, "kind": name.split("/")[2], "tracked": name in tracked,
                         "ignored": name in ignored,
                         "sha256": None if identity["redacted"] else digest(path)})
    hooks = Path(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "hooks"
    if hooks.is_dir():
        for path in sorted(hooks.iterdir()):
            if path.is_file() and not path.name.endswith(".sample"):
                rows.append({"path": f".git/hooks/{path.name}", "kind": "git-hook", "tracked": False,
                             "ignored": True, "size": path.stat().st_size, "sha256": None})
    return rows


def hook_config_shapes(root: Path) -> list[dict[str, Any]]:
    rows = []
    juno_path = root / ".juno_task/config.json"
    juno = read_json(juno_path)
    if juno_path.is_file():
        hooks = juno.get("hooks")
        hook_events = [{"event": name, "definition_count": len(value) if isinstance(value, list) else None}
                       for name, value in sorted(hooks.items())] if isinstance(hooks, dict) else []
        macros = juno.get("promptMacros")
        macro_shapes = []
        if isinstance(macros, dict):
            for name, value in sorted(macros.items()):
                shape = "path" if isinstance(value, str) and ("/" in value or value.endswith(".md")) else type(value).__name__
                macro_shapes.append({"name": name, "value_shape": shape})
        checkpoint = juno.get("gitCheckpoint")
        checkpoint_keys = sorted(checkpoint) if isinstance(checkpoint, dict) else []
        rows.append({"path": ".juno_task/config.json", "size": juno_path.stat().st_size,
                     "config_version_present": "configVersion" in juno,
                     "lifecycle_present": "lifecycle" in juno,
                     "lifecycle_keys": sorted(juno["lifecycle"]) if isinstance(juno.get("lifecycle"), dict) else [],
                     "controller_workspace_present": "controllerWorkspace" in juno,
                     "controller_workspace_keys": sorted(juno["controllerWorkspace"]) if isinstance(juno.get("controllerWorkspace"), dict) else [],
                     "hook_events": hook_events, "git_checkpoint_keys": checkpoint_keys,
                     "env_file_path_present": bool(juno.get("envFilePath")),
                     "prompt_macros": macro_shapes, "values_collected": False})
    for relative in (".claude/settings.json", ".claude/settings.local.json"):
        path = root / relative
        value = read_json(path)
        hooks = value.get("hooks")
        if not path.is_file() or not isinstance(hooks, dict):
            continue
        events = []
        for event, definitions in sorted(hooks.items()):
            events.append({"event": event, "definition_count": len(definitions) if isinstance(definitions, list) else None})
        rows.append({"path": relative, "size": path.stat().st_size, "events": events,
                     "values_collected": False})
    return rows


def nested_repositories(root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    gitlinks = []
    for line in git(root, "ls-files", "--stage").splitlines():
        mode, sha, stage_path = line.split(None, 2)
        if mode == "160000":
            _, relative = stage_path.split("\t", 1)
            child = root / relative
            exact = exact_repository_root(child)
            gitlinks.append({"path": relative, "recorded_head": sha, "present": child.exists(),
                             "initialized": exact is not None,
                             "actual_head": (git(exact, "rev-parse", "HEAD", check=False) or None) if exact else None,
                             "dirty": bool(git(exact, "status", "--porcelain", check=False)) if exact else None})
    nested = []
    for marker in sorted(root.rglob(".git")):
        parent = marker.parent
        if parent == root or any(parent == root / row["path"] for row in gitlinks):
            continue
        relative = parent.relative_to(root).as_posix()
        nested.append({"path": relative, "head": git(parent, "rev-parse", "HEAD", check=False) or None,
                       "dirty": bool(git(parent, "status", "--porcelain", check=False))})
    return sorted(gitlinks, key=lambda row: row["path"]), sorted(nested, key=lambda row: row["path"])


def controller_identity(root: Path, override: Path | None) -> dict[str, Any]:
    registered = git(root, "config", "--path", "--get", "juno.controller.path", check=False) or None
    branch = git(root, "config", "--get", "juno.controller.branch", check=False) or None
    selected = override.resolve() if override else Path(registered).resolve() if registered else None
    result: dict[str, Any] = {"registered_path": registered, "registered_branch": branch, "selected_path": str(selected) if selected else None,
                              "registration_missing": not registered or not branch}
    exact = exact_repository_root(selected) if selected else None
    if exact:
        selected = exact
        names = [item for item in git(selected, "ls-tree", "-r", "--name-only", "HEAD").splitlines() if item]
        product_paths = [name for name in names if name != ".gitignore" and not name.startswith(".juno_task/")]
        tracked_agent_surface = [name for name in names if any(
            name == prefix or name.startswith(prefix + "/")
            for prefix in ("AGENTS.md", "CLAUDE.md", ".agents", ".claude", ".pi"))]
        result.update({"head": git(selected, "rev-parse", "HEAD", check=False) or None,
                       "branch": git(selected, "symbolic-ref", "-q", "HEAD", check=False) or None,
                       "clean": not bool(git(selected, "status", "--porcelain", check=False)),
                       "git_common_dir": git(selected, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False) or None,
                       "same_repository": (git(selected, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False) or None)
                                          == str(Path(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()),
                       "tracked_count": len(names), "tracked_product_path_count": len(product_paths),
                       "tracked_product_roots": sorted({name.split("/", 1)[0] for name in product_paths}),
                       "tracked_agent_surface": tracked_agent_surface,
                       "tracked_agent_surface_count": len(tracked_agent_surface),
                       "metadata_only": not product_paths})
        result["override_disagrees_with_registration"] = bool(override and registered and Path(registered).resolve() != selected)
        actual_branch = result.get("branch")
        expected_full_branch = f"refs/heads/{branch}" if branch and not branch.startswith("refs/") else branch
        result["registration_path_matches_selected"] = bool(registered and Path(registered).resolve() == selected)
        result["registration_branch_matches_selected"] = bool(expected_full_branch and expected_full_branch == actual_branch)
        result["registration_valid"] = bool(result["registration_path_matches_selected"]
                                             and result["registration_branch_matches_selected"]
                                             and not result["override_disagrees_with_registration"])
    else:
        result["available"] = False
        if selected and selected.exists(): result["invalid_reason"] = "selected path is not an exact Git worktree root"
    return result


def executable_identity(executable: Path | None, fallback_name: str) -> dict[str, Any]:
    selected = executable.resolve() if executable and executable.is_file() else None
    if selected is None:
        discovered = shutil.which(fallback_name)
        selected = Path(discovered).resolve() if discovered else None
    return {"selected": str(selected) if selected else None, "sha256": digest(selected) if selected else None,
            "version": None, "executable_was_run": False}


def runtime_identity(root: Path, executable: Path | None) -> dict[str, Any]:
    candidates = [executable] if executable else []
    candidates += [root / ".venv_juno/bin/yy", root / "node_modules/.bin/yy"]
    selected = next((item.resolve() for item in candidates if item and item.is_file()), None)
    result = executable_identity(selected, "yy")
    result["managed_asset_package_version"] = read_json(root / ".juno_task/managed-assets.json").get("packageVersion")
    return result


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def committed_blob(root: Path, relative: str) -> bytes | None:
    result = subprocess.run(
        ["git", "-C", str(root), "show", f"HEAD:{relative}"], cwd=root,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        timeout=30, check=False, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )
    return result.stdout if result.returncode == 0 else None


def agent_configuration_inventory(root: Path) -> dict[str, Any]:
    config_bytes = committed_blob(root, ".juno_task/config.json")
    config: dict[str, Any] = {}
    if config_bytes is not None:
        try:
            parsed = json.loads(config_bytes)
            if not isinstance(parsed, dict):
                raise InventoryError("committed legacy config must be a JSON object")
            config = parsed
        except json.JSONDecodeError as exc:
            raise InventoryError(f"committed legacy config is malformed: {exc}") from exc
    fields = []
    for name in sorted(config):
        recommendation = CONFIG_FIELD_CLASSIFICATION.get(name)
        if recommendation is None:
            recommendation = "retire"
        fields.append({"name": name, "recommended_disposition": recommendation,
                       "value_type": type(config[name]).__name__})
    macro_entries = []
    raw_macros = config.get("promptMacros")
    if isinstance(raw_macros, dict):
        dictionaries = [(scope, value) for scope, value in raw_macros.items()
                        if scope in {"global", "local"} and isinstance(value, dict)]
        if not dictionaries and all(key not in {"enabled", "order", "maxDepth"} for key in raw_macros):
            dictionaries = [("global", raw_macros)]
        for scope, dictionary in dictionaries:
            for name, value in sorted(dictionary.items()):
                kind = "file" if (isinstance(value, dict) and isinstance(value.get("path"), str)) \
                    or (isinstance(value, str) and value.startswith(".juno_task/prompts/")) else "inline"
                macro_entries.append({"scope": scope, "name": name, "kind": kind,
                                      "value_collected": False})
    plan_bytes = committed_blob(root, ".juno_task/plan.md")
    prompt_assets = []
    for name in git(root, "ls-tree", "-r", "--name-only", "HEAD", "--", ".juno_task/prompts").splitlines():
        data = committed_blob(root, name)
        if data is not None:
            prompt_assets.append({"path": name, "size": len(data),
                                  "sha256": hashlib.sha256(data).hexdigest()})
    environment = []
    for relative in (".env.yylo", ".env.juno"):
        candidate = root / relative
        if candidate.is_file() and not candidate.is_symlink():
            environment.append({"source": relative, "size": candidate.stat().st_size,
                                "mode": candidate.stat().st_mode & 0o777,
                                "values_collected": False, "authorization_required": True})
    return {
        "config": {"present": config_bytes is not None,
                   "size": len(config_bytes) if config_bytes is not None else 0,
                   "sha256": hashlib.sha256(config_bytes).hexdigest() if config_bytes is not None else None,
                   "fields": fields, "values_collected": False},
        "plan": {"present": plan_bytes is not None,
                 "size": len(plan_bytes) if plan_bytes is not None else 0,
                 "sha256": hashlib.sha256(plan_bytes).hexdigest() if plan_bytes is not None else None,
                 "bytes_collected": False},
        "prompt_macros": macro_entries,
        "prompt_assets": prompt_assets,
        "environment_sources": environment,
        "source_head": git(root, "rev-parse", "HEAD"),
    }


def managed_assets(root: Path) -> list[dict[str, Any]]:
    manifest = read_json(root / ".juno_task/managed-assets.json")
    records = manifest.get("assets") if isinstance(manifest.get("assets"), dict) else {}
    rows = []
    for relative, record in sorted(records.items()):
        if not isinstance(record, dict):
            continue
        current_path = root / relative
        is_symlink = current_path.is_symlink()
        current = None if SECRET_NAME.search(relative) else digest(current_path)
        installed = record.get("installedSha256")
        rows.append({"path": relative, "state": "symlink" if is_symlink else "missing" if current is None else "managed" if current == installed else "customized",
                     "current_sha256": current, "installed_sha256": installed, "template_version": record.get("templateVersion")})
    return rows


def required_decisions(payload: dict[str, Any]) -> dict[str, Any]:
    paths: dict[tuple[str, str], dict[str, Any]] = {}
    automatic: list[dict[str, Any]] = []
    generated_markers = ("/__pycache__/", "/.pytest_cache/", "/node_modules/", "/.cache/", "/dist/")
    generated_groups = {".venv_juno", ".pytest_cache", "node_modules", ".juno_task/logs", ".juno_task/runtime"}

    def add(kind: str, name: str, recommendation: str = "keep", reason: str = "owner_policy_required") -> None:
        key = (kind, name)
        row = paths.setdefault(key, {"id": hashlib.sha256(f"{kind}\0{name}".encode()).hexdigest()[:16],
                                     "kind": kind, "path": name, "handling": "owner_review",
                                     "recommended_disposition": recommendation, "reason": reason, "member_count": 0})
        row["member_count"] += 1

    def auto(kind: str, name: str, recommendation: str, reason: str) -> None:
        automatic.append({"id": hashlib.sha256(f"{kind}\0{name}".encode()).hexdigest()[:16], "kind": kind,
                          "path": name, "handling": "automatic", "recommended_disposition": recommendation,
                          "reason": reason})

    present_private = {row["path"] for row in payload["controller_private_roots"]}
    for row in payload["controller_private_roots"]:
        add("controller_private", row["path"], "keep", "durable_controller_state")
    # Ownership policy must classify roots that are currently absent too. They
    # can be created later and must not silently fall outside evacuation or the
    # product boundary merely because the inventoried commit had no files there.
    for name in CONTROLLER_PRIVATE_DEFAULTS:
        if name not in present_private:
            add("controller_private", name, "retire", "absent_but_policy_reserved_controller_state")
    for name in payload.get("controller", {}).get("tracked_agent_surface", []):
        add("controller_agent_surface", name, "externalize", "committed_user_instruction_evidence")
    for row in payload["managed_assets"]:
        if row["state"] != "managed": add("managed_asset", row["path"], "keep", "customized_or_missing_managed_asset")
    for row in payload["custom_project_assets"]:
        name = row.get("path") or f"redacted:{row['path_sha256']}"
        if any(marker in f"/{name}" for marker in generated_markers):
            auto("custom_project_asset", name, "retire", "generated_rebuildable_cache")
        elif name.startswith(".juno_task/scripts/"):
            family = ".juno_task/scripts/tests" if Path(name).name.startswith("test") else ".juno_task/scripts/runtime"
            add("custom_project_asset_group", family, "keep", "review_grouped_legacy_scripts")
        else:
            add("custom_project_asset", name, "keep", "project_specific_asset")
    for row in payload["hook_config_shapes"]: add("hook_config", row["path"])
    for row in payload["agent_configuration"]["config"]["fields"]:
        add("legacy_config_field", row["name"], row["recommended_disposition"],
            "every_legacy_config_field_requires_explicit_disposition")
    for row in payload["gitlinks"]: add("gitlink", row["path"], "keep", "child_first_repository_policy")
    for row in payload["nested_repositories"]: add("nested_repository", row["path"])
    for row in payload["heavy_paths"]:
        add("heavy_path", row.get("path") or f"redacted:{row['path_sha256']}")
    for row in payload["ignored_paths"]:
        name = f"group:{row['group']}"
        if row["group"] in generated_groups:
            auto("ignored_group", name, "retire", "generated_rebuildable_group")
        else:
            add("ignored_group", name, "keep", "ignored_data_requires_owner_retention_decision")
    untracked_count = sum(1 for row in payload["status"]["entries"] if row["code"] == "??")
    if untracked_count:
        add("untracked_group", "all_untracked_paths", "keep", "review_status_inventory_as_one_group")
        paths[("untracked_group", "all_untracked_paths")]["member_count"] = untracked_count
    owner_rows = sorted(paths.values(), key=lambda row: (row["kind"], row["path"]))
    return {
        "identity_fields": ["product_ref", "expected_product_head", "controller_branch", "controller_path",
                            "integration_path", "task_workspace_root", "branch_prefix", "rollback_owner", "cleanup_owner"],
        "policy_fields": ["allowed_paths", "controller_private_paths", "copied_metadata", "focused_validation",
                          "full_suite_validation", "risk_policy"],
        "disposition_choices": list(DISPOSITIONS),
        "config_disposition_choices": list(CONFIG_DISPOSITIONS), "dispositions": owner_rows,
        "automatic_classifications": sorted(automatic, key=lambda row: (row["kind"], row["path"])),
        "classification_counts": {"owner_review": len(owner_rows), "automatic": len(automatic)},
        "separate_authorities": ["prepare_controller", "evacuate_product_metadata", "register_controller", "move_product_ref",
                                 "cleanup_old_controller", "tag_publish_push_deploy", "bind_external_environment"],
    }


def inventory(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(git(args.project.resolve(), "rev-parse", "--show-toplevel")).resolve()
    output = args.output.resolve()
    ignored, heavy = ignored_and_heavy(root, args.heavy_threshold_bytes)
    gitlinks, nested = nested_repositories(root)
    registered = git(root, "config", "--path", "--get", "juno.controller.path", check=False) or None
    protected = [root, Path(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()]
    protected.extend(Path(row["path"]).resolve() for row in parse_worktrees(root))
    protected.extend(root / row["path"] for row in gitlinks + nested)
    if args.controller: protected.append(args.controller.resolve())
    elif registered: protected.append(Path(registered).resolve())
    for candidate in protected:
        try:
            output.relative_to(candidate)
            raise InventoryError("inventory output must be outside all inspected repositories and Git administration directories")
        except ValueError:
            pass
    before = mutation_sentinel(root, args.controller, gitlinks, nested)
    kanban = root / ".juno_task/scripts/kanban.sh"
    controller_record = controller_identity(root, args.controller)
    controller_store = Path(controller_record["selected_path"]) if controller_record.get("available", True) and controller_record.get("selected_path") else root
    task_store = controller_store / ".juno_task/tasks"
    ledger_store = controller_store / ".juno_task/ledger"
    payload: dict[str, Any] = {
        "schema_version": SCHEMA, "operation": "inventory", "outcome": "planned_no_mutation",
        "source_mutation_authorized": False, "git": git_identity(root, args.product_ref), "status": status_inventory(root),
        "controller": controller_record, "runtime": runtime_identity(root, args.runtime),
        "kanban": {"wrapper": str(kanban) if kanban.is_file() else None, "wrapper_sha256": digest(kanban),
                   "runtime": executable_identity(args.kanban_runtime, "juno-kanban"),
                   "canonical_store_root": str(controller_store),
                   "task_store_present": task_store.is_dir(), "task_file_count": sum(1 for _ in task_store.rglob("*.md")) if task_store.is_dir() else 0,
                   "ledger_present": ledger_store.is_dir(), "ledger_file_count": sum(1 for _ in ledger_store.rglob("*.ndjson")) if ledger_store.is_dir() else 0},
        "controller_private_roots": tracked_controller_roots(root), "managed_assets": managed_assets(root),
        "custom_project_assets": custom_project_assets(root),
        "hook_config_shapes": hook_config_shapes(root),
        "agent_configuration": agent_configuration_inventory(root),
        "gitlinks": gitlinks, "nested_repositories": nested, "ignored_paths": ignored, "heavy_paths": heavy,
        "heavy_threshold_bytes": args.heavy_threshold_bytes,
    }
    payload["required_owner_answers"] = required_decisions(payload)
    blockers = []
    if not payload["git"]["selected_product_ref"]:
        blockers.append("explicit_product_ref_required")
    elif not payload["git"]["checkout_matches_selected_product"]:
        blockers.append("inspected_checkout_does_not_match_selected_product_ref")
    payload["inventory_warnings"] = (["inspected_checkout_does_not_match_selected_product_ref"]
                                     if payload["git"]["selected_product_ref"] and
                                     not payload["git"]["checkout_matches_selected_product"] else [])
    payload["policy_generation_block_reasons"] = blockers + ["owner_answers_unresolved"]
    payload["policy_generation_blocked"] = True
    after = mutation_sentinel(root, args.controller, gitlinks, nested)
    if before != after:
        raise InventoryError("source changed during read-only inventory")
    return payload


def mutation_sentinel(root: Path, controller: Path | None, gitlinks: list[dict[str, Any]], nested: list[dict[str, Any]]) -> dict[str, Any]:
    paths = [root]
    registered = git(root, "config", "--path", "--get", "juno.controller.path", check=False) or None
    selected = controller.resolve() if controller else Path(registered).resolve() if registered else None
    if selected and selected.exists(): paths.append(selected)
    paths.extend(root / row["path"] for row in gitlinks + nested if (root / row["path"]).exists())
    rows = []
    for path in sorted(set(item.resolve() for item in paths), key=str):
        exact = exact_repository_root(path)
        if not exact:
            rows.append({"path": str(path), "valid_repository": False})
            continue
        common = Path(git(exact, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        index = Path(git(exact, "rev-parse", "--path-format=absolute", "--git-path", "index"))
        rows.append({"path": str(exact), "valid_repository": True,
                     "head": git(exact, "rev-parse", "HEAD", check=False) or None,
                     "branch": git(path, "symbolic-ref", "-q", "HEAD", check=False) or None,
                     "status": git(exact, "status", "--porcelain=v1", "--untracked-files=all", check=False),
                     "refs": git(exact, "for-each-ref", "--format=%(refname)%00%(objectname)"),
                     "config_sha256": digest(common / "config"), "index_sha256": digest(index)})
    return {"repositories": rows}


def validated_answers(receipt: dict[str, Any], answers: dict[str, Any], inventory_sha256: str | None) -> None:
    if receipt.get("schema_version") != SCHEMA:
        raise InventoryError("unsupported inventory receipt")
    if answers.get("schema_version") != ANSWERS_SCHEMA or answers.get("inventory_sha256") != inventory_sha256:
        raise InventoryError("owner answers do not bind the exact inventory receipt")
    frozen_git = receipt.get("git", {})
    selected_ref = frozen_git.get("selected_product_ref")
    frozen_refs = frozen_git.get("local_product_refs", {})
    if (not isinstance(selected_ref, str) or not selected_ref.startswith("refs/heads/")
            or selected_ref not in frozen_refs
            or frozen_git.get("selected_product_head") != frozen_refs.get(selected_ref)
            or frozen_git.get("selected_product_head") != frozen_git.get("head")
            or frozen_git.get("checkout_matches_selected_product") is not True):
        raise InventoryError("policy generation requires an inspected checkout at the exact selected product ref commit")
    required = receipt["required_owner_answers"]
    missing = [name for name in required["identity_fields"] + required["policy_fields"] if answers.get(name) in (None, "", [])]
    decisions = answers.get("dispositions", {})
    if not isinstance(decisions, dict):
        missing.append("dispositions")
        decisions = {}
    for row in required["dispositions"]:
        value = decisions.get(row["id"])
        allowed = CONFIG_DISPOSITIONS if row.get("kind") == "legacy_config_field" else DISPOSITIONS
        if value not in allowed:
            missing.append(f"dispositions.{row['id']}")
        elif row.get("kind") == "controller_agent_surface" and value not in {"retire", "externalize"}:
            raise InventoryError(
                f"tracked controller agent evidence requires reviewed retire/externalize disposition: {row.get('path')}"
            )
    authorities = answers.get("authorities", {})
    expected_authorities = set(required["separate_authorities"])
    if not isinstance(authorities, dict) or set(authorities) != expected_authorities or any(value not in (True, False) for value in authorities.values()):
        missing.append("authorities")
    for name in required["separate_authorities"]:
        if name not in authorities:
            missing.append(f"authorities.{name}")
    if missing:
        raise InventoryError("owner answers unresolved: " + ", ".join(sorted(set(missing))))
    if any(value == "block" for value in decisions.values()):
        raise InventoryError("owner dispositions contain block; policy generation refused")
    product_ref = answers["product_ref"]
    frozen_refs = receipt["git"].get("local_product_refs", {})
    if product_ref not in frozen_refs:
        raise InventoryError("product_ref must be one of the full local refs frozen in inventory")
    selected_ref = receipt["git"].get("selected_product_ref")
    if selected_ref and product_ref != selected_ref:
        raise InventoryError("product_ref differs from the explicitly frozen selection")
    expected_head = answers["expected_product_head"]
    if not isinstance(expected_head, str) or expected_head != frozen_refs[product_ref] or not re.fullmatch(r"[0-9a-f]{40,64}", expected_head):
        raise InventoryError("expected_product_head does not match the frozen product ref")
    for name in ("controller_path", "integration_path", "task_workspace_root"):
        value = Path(answers[name]).expanduser()
        if not value.is_absolute() or value.resolve() == Path("/"):
            raise InventoryError(f"{name} must be an explicit absolute non-root path")
    for name in ("controller_branch", "branch_prefix"):
        value = answers[name]
        if not isinstance(value, str) or not value.startswith("refs/heads/"):
            raise InventoryError(f"{name} must be a full local ref")


def protected_receipt_paths(receipt: dict[str, Any]) -> list[Path]:
    paths: list[Path] = []
    git_record = receipt.get("git", {})
    controller = receipt.get("controller", {})
    for value in (git_record.get("root"), git_record.get("git_common_dir"), controller.get("selected_path"),
                  controller.get("git_common_dir")):
        if isinstance(value, str) and value:
            paths.append(Path(value).resolve())
    for row in git_record.get("worktrees", []):
        if isinstance(row, dict) and isinstance(row.get("path"), str):
            paths.append(Path(row["path"]).resolve())
    root = Path(git_record["root"]).resolve() if git_record.get("root") else None
    if root:
        for collection in (receipt.get("gitlinks", []), receipt.get("nested_repositories", [])):
            for row in collection:
                if isinstance(row, dict) and isinstance(row.get("path"), str):
                    paths.append((root / row["path"]).resolve())
    return sorted(set(paths), key=str)


def refuse_protected_output(output: Path, receipt: dict[str, Any]) -> None:
    resolved = output.resolve()
    for protected in protected_receipt_paths(receipt):
        try:
            resolved.relative_to(protected)
            raise InventoryError("output must be outside all inventoried repositories and Git administration directories")
        except ValueError:
            pass


def policy_bundle(args: argparse.Namespace) -> dict[str, Any]:
    receipt = read_json(args.inventory.resolve()); answers = read_json(args.answers.resolve())
    refuse_protected_output(args.output, receipt)
    validated_answers(receipt, answers, digest(args.inventory.resolve()))
    private = list(answers["controller_private_paths"])
    copied = list(answers["copied_metadata"])
    agent_source = receipt.get("agent_configuration", {})
    required_agent_paths: list[str] = []
    if agent_source.get("plan", {}).get("present"):
        required_agent_paths.extend([".juno_task/plan.md", ".juno_task/specs"])
    if agent_source.get("prompt_assets"):
        required_agent_paths.append(".juno_task/prompts")
    for required_path in required_agent_paths:
        if required_path not in private:
            private.append(required_path)
        if required_path not in copied:
            copied.append(required_path)
    if not all(isinstance(path, str) and path.startswith(".juno_task/") for path in private + copied):
        raise InventoryError("controller_private_paths and copied_metadata must be .juno_task paths")
    if not set(copied).issubset(set(private)):
        raise InventoryError("copied_metadata must be a subset of controller_private_paths")
    decision_rows = {row["path"]: answers["dispositions"][row["id"]]
                     for row in receipt["required_owner_answers"]["dispositions"]
                     if row.get("kind") != "legacy_config_field"}
    config_field_dispositions = {
        row["path"]: answers["dispositions"][row["id"]]
        for row in receipt["required_owner_answers"]["dispositions"]
        if row.get("kind") == "legacy_config_field"
    }
    inventoried_fields = {row["name"] for row in receipt["agent_configuration"]["config"]["fields"]}
    if set(config_field_dispositions) != inventoried_fields:
        raise InventoryError("every inventoried config field must have exactly one disposition")
    for path in private:
        disposition = decision_rows.get(path)
        if disposition and ((path in copied) != (disposition == "keep")):
            raise InventoryError(f"disposition for {path} contradicts copied_metadata")
    durable_recursive_roots = (
        ".juno_task/archive", ".juno_task/archive-receipts",
        ".juno_task/artifact-ledger", ".juno_task/artifacts",
        ".juno_task/config/umbrella-admissions",
        ".juno_task/document-ledger", ".juno_task/documents",
        ".juno_task/ledger", ".juno_task/objects", ".juno_task/task-scopes",
        ".juno_task/tasks", ".juno_task/wiki",
    )
    recursive = [path for path in durable_recursive_roots if path in copied]
    top_level = [".juno_task/receipts"] + ([".juno_task/specs"] if ".juno_task/specs" in copied else [])
    exact_copied = [path for path in copied if path not in recursive and path not in top_level]
    metadata = {
        "schema_version": "juno_metadata_controller_policy.v1", "controller_branch": answers["controller_branch"],
        "product_ref": answers["product_ref"], "spec_copy_mode": "top_level_files_only", "copied_metadata": copied,
        "generated_metadata": [".gitignore", ".juno_task/config.json", ".juno_task/config/metadata-controller.json",
                               ".juno_task/config/task-workspace.json", ".juno_task/config/integration-workspace.json",
                               ".juno_task/config/risk-policy.json",
                               ".juno_task/receipts/controller-boundary.json", ".juno_task/state/tasks.json"],
        "product_forbidden": private,
        "tracked_exact": [".gitignore", ".juno_task/config.json", *exact_copied, ".juno_task/config/metadata-controller.json",
                          ".juno_task/config/task-workspace.json", ".juno_task/config/integration-workspace.json",
                          ".juno_task/config/risk-policy.json",
                          ".juno_task/receipts/controller-boundary.json", ".juno_task/state/tasks.json"],
        "tracked_recursive": recursive,
        "tracked_top_level_files": top_level,
        "runtime": {"package": "@yylo/cli", "identity_file": ".juno_task/runtime/identity.json",
                    "ignored_roots": [".agents", ".claude", ".env.yylo", ".juno_task/cache",
                                      ".juno_task/locks", ".juno_task/runtime", ".juno_task/scripts",
                                      ".pi", ".venv_juno", "AGENTS.md", "CLAUDE.md"]},
    }
    task = {"schema_version": "juno_task_workspace_config.v1", "repository": ".", "target_ref": answers["product_ref"],
            "workspace_root": answers["task_workspace_root"], "branch_prefix": answers["branch_prefix"],
            "allowed_paths": answers["allowed_paths"], "controller_private_paths": private,
            "focused_validation": answers["focused_validation"], "full_suite_validation": answers["full_suite_validation"]}
    integration = json.loads((Path(__file__).resolve().parents[1] / "config/integration-workspace.json").read_text())
    validate_generated_policies(metadata, task, integration, answers["risk_policy"])
    return {"schema_version": POLICY_SCHEMA, "operation": "generate-policy", "outcome": "generated_from_reviewed_answers",
            "migration_authorized": False,
            "agent_migration": {
                "source": receipt["agent_configuration"],
                "field_dispositions": config_field_dispositions,
                "environment_binding_authorized": bool(answers["authorities"].get("bind_external_environment", False)),
            },
            "inventory_sha256": digest(args.inventory.resolve()), "owner_answers_sha256": digest(args.answers.resolve()),
            "selected_paths": {"controller": answers["controller_path"], "integration": answers["integration_path"]},
            "owners": {"rollback": answers["rollback_owner"], "cleanup": answers["cleanup_owner"]},
            "authorities": answers["authorities"], "dispositions": answers["dispositions"],
            "policies": {"metadata_controller": metadata, "task_workspace": task,
                         "integration_workspace": integration, "risk": answers["risk_policy"]}}


def owner_template(args: argparse.Namespace) -> dict[str, Any]:
    receipt = read_json(args.inventory.resolve())
    if receipt.get("schema_version") != SCHEMA:
        raise InventoryError("unsupported inventory receipt")
    refuse_protected_output(args.output, receipt)
    required = receipt["required_owner_answers"]
    payload: dict[str, Any] = {
        "schema_version": ANSWERS_SCHEMA,
        "inventory_sha256": digest(args.inventory.resolve()),
    }
    for name in required["identity_fields"] + required["policy_fields"]:
        payload[name] = None
    payload["dispositions"] = {row["id"]: None for row in required["dispositions"]}
    payload["authorities"] = {name: None for name in required["separate_authorities"]}
    return payload


def load_sibling(name: str) -> Any:
    path = Path(__file__).resolve().with_name(name)
    spec = importlib.util.spec_from_file_location(f"juno_migration_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise InventoryError(f"cannot load packaged validator: {name}")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def validate_generated_policies(metadata: dict[str, Any], task: dict[str, Any],
                                integration: dict[str, Any], risk: dict[str, Any]) -> None:
    metadata_validator = load_sibling("metadata_controller.py")
    task_validator = load_sibling("task_workspace.py")
    risk_validator = load_sibling("risk_policy.py")
    with tempfile.TemporaryDirectory(prefix="juno-migration-policy-") as temporary:
        root = Path(temporary); config = root / ".juno_task/config"; config.mkdir(parents=True)
        paths = {"metadata-controller.json": metadata, "task-workspace.json": task,
                 "integration-workspace.json": integration, "risk-policy.json": risk}
        for name, value in paths.items():
            (config / name).write_text(json.dumps(value))
        metadata_validator.load_policy(config / "metadata-controller.json")
        task_validator.load_config(root)
        integration_validator = load_sibling("integration_workspace.py")
        integration_validator.load_policy(root)
        risk_validator.load_policy(config / "risk-policy.json")


def atomic_json(output: Path, payload: dict[str, Any]) -> None:
    output = output.resolve()
    if output.exists():
        raise InventoryError(f"output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, indent=2, sort_keys=True, separators=(",", ": ")) + "\n").encode()
    temporary = output.with_name(output.name + f".tmp-{os.getpid()}")
    temporary.write_bytes(data); os.replace(temporary, output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    scan = sub.add_parser("inventory"); scan.add_argument("--project", type=Path, required=True)
    scan.add_argument("--controller", type=Path); scan.add_argument("--product-ref")
    scan.add_argument("--runtime", type=Path); scan.add_argument("--kanban-runtime", type=Path)
    scan.add_argument("--heavy-threshold-bytes", type=int, default=10 * 1024 * 1024); scan.add_argument("--output", type=Path, required=True)
    template = sub.add_parser("owner-template"); template.add_argument("--inventory", type=Path, required=True)
    template.add_argument("--output", type=Path, required=True)
    generate = sub.add_parser("generate-policy"); generate.add_argument("--inventory", type=Path, required=True)
    generate.add_argument("--answers", type=Path, required=True); generate.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = inventory(args) if args.command == "inventory" else owner_template(args) if args.command == "owner-template" else policy_bundle(args)
    atomic_json(args.output, payload)
    print(json.dumps({"outcome": payload.get("outcome", "owner_answers_required"), "output": str(args.output.resolve())}, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (InventoryError, OSError, KeyError, TypeError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        print(f"migration-inventory: {exc}", file=sys.stderr)
        raise SystemExit(2)
