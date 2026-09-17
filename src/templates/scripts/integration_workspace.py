#!/usr/bin/env python3
"""Offline integration-owner diagnostics and guarded target synchronization."""
from __future__ import annotations

import argparse
import base64
import difflib
import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import task_workspace

SCHEMA = "juno_integration_workspace.v1"
SOURCE_ADOPTION_SCHEMA = "juno_source_runtime_adoption.v1"
POLICY_SCHEMA = "juno_integration_workspace_policy.v1"
AUTHORITY = "protected-integration.v1"
OWNER_CONFIG = "juno.integration.ownerPath"
LEGACY_OWNER_CONFIG = "juno.gitFlow.integrationCheckout"
SHA_RE = re.compile(r"[0-9a-f]{40,64}\Z")


MANAGED_RUNTIME_SCHEMA = "juno_managed_controller_runtime.v1"
MANAGED_MANIFEST_PATH = "juno-code/src/templates/managed-assets.json"
MANAGED_INSTALLED_MANIFEST_PATH = ".juno_task/managed-assets.json"
MANAGED_PACKAGE_PATH = "juno-code/package.json"
MANAGED_POLICY_PATH = ".juno_task/config/task-workspace.json"
MANAGED_GENERATION_PATH = ".juno_task/runtime/managed-controller/generation.json"
MANAGED_RECEIPT_ROOT = ".juno_task/runtime/managed-controller/receipts"
MANAGED_BACKUP_ROOT = ".juno_task/runtime/managed-controller/backups"
MANAGED_COLLISION_ROOT = ".juno_task/runtime/managed-controller/collisions"
MANAGED_COLLISION_SCHEMA = "juno_managed_controller_collision.v1"
MANAGED_WRITE_SET_SCHEMA = "juno_managed_controller_write_set.v1"
MANAGED_REPAIR_SCHEMA = "juno_managed_runtime_repair.v1"
MANAGED_SHA_RE = re.compile(r"[0-9a-f]{40,64}\Z")
MANAGED_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
VERSION_CACHE_PATH = ".juno_task/.version_check_cache"
INSTALL_REQUIREMENTS_PATH = ".juno_task/scripts/install_requirements.sh"
LEGACY_VERSION_CACHE_MARKERS = (
    b'VERSION_CHECK_CACHE_DIR="${VERSION_CHECK_CACHE_DIR:-${PWD}/.juno_task}"',
    b'VERSION_CHECK_CACHE_DIR="${VERSION_CHECK_CACHE_DIR:-$PWD/.juno_task}"',
)


class ManagedRuntimeError(RuntimeError):
    def __init__(self, message: str, receipt: dict[str, str] | None = None):
        super().__init__(message)
        self.receipt = receipt


class ManagedWriteCollision(ManagedRuntimeError):
    """A destination changed after its expected-old write set was compiled."""

    def __init__(self, path: Path, expected_exists: bool,
                 expected_sha256: str | None, observed: bytes | None):
        super().__init__(f"managed destination changed before overwrite: {path}")
        self.path = path
        self.expected_exists = expected_exists
        self.expected_sha256 = expected_sha256
        self.observed = observed


def managed_run(argv: list[str], cwd: Path, *, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and result.returncode:
        detail = (result.stderr or result.stdout).decode(errors="replace").strip()
        raise ManagedRuntimeError(detail or f"command failed: {argv!r}")
    return result


def git_bytes(repository: Path, *args: str) -> bytes:
    return managed_run(["git", "-C", str(repository), *args], repository).stdout


def managed_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def managed_exact_commit(repository: Path, value: str, label: str) -> str:
    if not MANAGED_SHA_RE.fullmatch(value):
        raise ManagedRuntimeError(f"{label} is not a full commit SHA")
    observed = git_bytes(repository, "rev-parse", "--verify", f"{value}^{{commit}}").decode().strip()
    if observed != value:
        raise ManagedRuntimeError(f"{label} does not resolve exactly")
    return value


def managed_source_bytes(repository: Path, commit: str, relative: str) -> bytes:
    if relative.startswith("/") or ".." in Path(relative).parts or ".git" in Path(relative).parts:
        raise ManagedRuntimeError(f"unsafe managed source path: {relative}")
    return git_bytes(repository, "show", f"{commit}:{relative}")


def managed_source_json(repository: Path, commit: str, relative: str) -> Any:
    try:
        return json.loads(managed_source_bytes(repository, commit, relative))
    except json.JSONDecodeError as exc:
        raise ManagedRuntimeError(f"invalid target JSON at {relative}: {exc}") from exc


def managed_source_exists(repository: Path, commit: str, relative: str) -> bool:
    return managed_run(
        ["git", "-C", str(repository), "cat-file", "-e", f"{commit}:{relative}"],
        repository, check=False).returncode == 0


def managed_valid_package_version(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.+-]*", value))


def managed_target_provenance(repository: Path, commit: str) -> dict[str, Any]:
    """Resolve one immutable source or checksum-bound installed package generation."""
    has_source_manifest = managed_source_exists(repository, commit, MANAGED_MANIFEST_PATH)
    has_source_package = managed_source_exists(repository, commit, MANAGED_PACKAGE_PATH)
    if has_source_manifest != has_source_package:
        raise ManagedRuntimeError("target managed runtime has mixed ambiguous source provenance")
    if has_source_manifest:
        manifest = managed_source_json(repository, commit, MANAGED_MANIFEST_PATH)
        assets = manifest.get("assets") if isinstance(manifest, dict) else None
        schema = manifest.get("schemaVersion") if isinstance(manifest, dict) else None
        instruction_declaration = manifest.get("instructionBundle") if isinstance(manifest, dict) else None
        declaration_valid = task_workspace.instruction_declaration_compatible(schema, instruction_declaration)
        if not declaration_valid or not isinstance(assets, list):
            raise ManagedRuntimeError("target managed asset definition is invalid; "
                                      + task_workspace.instruction_compatibility_error())
        package = managed_source_json(repository, commit, MANAGED_PACKAGE_PATH)
        version = package.get("version") if isinstance(package, dict) else None
        if (not isinstance(package, dict) or package.get("name") != "@yylo/cli"
                or not managed_valid_package_version(version)):
            raise ManagedRuntimeError("target package name/version is invalid")
        result: dict[str, str] = {}
        seen: set[str] = set()
        for asset in assets:
            required_asset_keys = {"source", "destination", "installClass", "type"}
            if (not isinstance(asset, dict)
                    or set(asset) not in {frozenset(required_asset_keys),
                                         frozenset(required_asset_keys | {"macro"})}
                    or asset.get("installClass") not in {"project", "script", "controller"}
                    or (asset.get("installClass") == "controller"
                        and asset.get("type") not in {"workflow", "prompt"})
                    or not isinstance(asset.get("source"), str)
                    or not isinstance(asset.get("destination"), str)
                    or not isinstance(asset.get("type"), str)
                    or ("macro" in asset and (asset.get("installClass") != "project"
                                               or asset.get("type") != "prompt"
                                               or not isinstance(asset.get("macro"), str)
                                               or not asset["macro"]))):
                raise ManagedRuntimeError("target managed asset entry is invalid")
            destination = asset["destination"]
            if destination in seen:
                raise ManagedRuntimeError("target managed asset destination is duplicated")
            seen.add(destination)
            if asset["installClass"] != "script":
                continue
            expected_source = destination.removeprefix(".juno_task/")
            if (not destination.startswith(".juno_task/scripts/")
                    or asset["type"] != "script" or asset["source"] != expected_source):
                raise ManagedRuntimeError("managed script source/destination mapping is ambiguous")
            result[destination] = f"juno-code/src/templates/{asset['source']}"
        mode = "source"
    else:
        if not managed_source_exists(repository, commit, MANAGED_INSTALLED_MANIFEST_PATH):
            raise ManagedRuntimeError("target managed runtime provenance is absent")
        manifest = managed_source_json(repository, commit, MANAGED_INSTALLED_MANIFEST_PATH)
        schema = manifest.get("schemaVersion") if isinstance(manifest, dict) else None
        expected_keys = {"schemaVersion", "packageName", "packageVersion", "assets"}
        if schema == 2:
            expected_keys.add("instructionBundle")
        if (not isinstance(manifest, dict) or set(manifest) != expected_keys
                or schema not in {1, 2} or manifest.get("packageName") != "@yylo/cli"
                or not managed_valid_package_version(manifest.get("packageVersion"))
                or not isinstance(manifest.get("assets"), dict)):
            raise ManagedRuntimeError("installed managed asset manifest/package identity is invalid")
        version = manifest["packageVersion"]
        result = {}
        for destination, record in manifest["assets"].items():
            if (not isinstance(destination, str) or destination.startswith("/")
                    or ".." in Path(destination).parts or ".git" in Path(destination).parts
                    or not destination.startswith(".juno_task/") or not isinstance(record, dict)
                    or set(record) != {"type", "templateVersion", "sourceSha256", "installedSha256"}
                    or not isinstance(record.get("type"), str)
                    or not managed_valid_package_version(record.get("templateVersion"))
                    or not MANAGED_HASH_RE.fullmatch(record.get("sourceSha256", ""))
                    or not MANAGED_HASH_RE.fullmatch(record.get("installedSha256", ""))):
                raise ManagedRuntimeError("installed managed asset entry is invalid")
            is_script_path = destination.startswith(".juno_task/scripts/")
            if is_script_path != (record["type"] == "script"):
                raise ManagedRuntimeError(f"installed managed script is undeclared: {destination}")
            if not is_script_path:
                continue
            if record["templateVersion"] != version:
                raise ManagedRuntimeError(f"installed managed script package version mismatch: {destination}")
            if record["sourceSha256"] != record["installedSha256"]:
                raise ManagedRuntimeError(f"installed managed script source hash mismatch: {destination}")
            if not managed_source_exists(repository, commit, destination):
                raise ManagedRuntimeError(f"installed managed script destination is missing: {destination}")
            data = managed_source_bytes(repository, commit, destination)
            if managed_sha256(data) != record["installedSha256"]:
                raise ManagedRuntimeError(f"installed managed script manifest drift: {destination}")
            result[destination] = destination
        if schema == 2:
            identity = manifest.get("instructionBundle")
            projected = [{"destination": destination, "type": record.get("type"),
                          "sourceSha256": record.get("sourceSha256"),
                          "installedSha256": record.get("installedSha256")}
                         for destination, record in sorted(manifest["assets"].items())]
            assets_sha = hashlib.sha256(
                json.dumps(projected, separators=(",", ":")).encode()).hexdigest()
            core = {"schemaVersion": identity.get("schemaVersion") if isinstance(identity, dict) else None,
                    "semanticVersion": identity.get("semanticVersion") if isinstance(identity, dict) else None,
                    "packageVersion": identity.get("packageVersion") if isinstance(identity, dict) else None,
                    "assetCount": identity.get("assetCount") if isinstance(identity, dict) else None,
                    "assetsSha256": identity.get("assetsSha256") if isinstance(identity, dict) else None}
            bundle_sha = hashlib.sha256(json.dumps(core, separators=(",", ":")).encode()).hexdigest()
            if (not isinstance(identity, dict)
                    or set(identity) != set(core) | {"bundleSha256"}
                    or identity.get("schemaVersion") != task_workspace.INSTRUCTION_COMPATIBILITY["identitySchema"]
                    or not task_workspace.instruction_version_compatible(identity.get("semanticVersion"))
                    or identity.get("packageVersion") != version
                    or identity.get("assetCount") != len(manifest["assets"])
                    or identity.get("assetsSha256") != assets_sha
                    or identity.get("bundleSha256") != bundle_sha):
                raise ManagedRuntimeError("installed managed instruction bundle is mixed or partial; "
                                          + task_workspace.instruction_compatibility_error())
        mode = "installed"
    if not result:
        raise ManagedRuntimeError("target managed script set is empty or duplicated")
    return {"mode": mode, "package_version": version, "assets": result}


def managed_script_assets(repository: Path, commit: str) -> dict[str, str]:
    return managed_target_provenance(repository, commit)["assets"]


def managed_script_destinations(repository: Path, commit: str) -> list[str]:
    return sorted(managed_script_assets(repository, commit))


def managed_script_source_bytes(repository: Path, commit: str,
                                assets: dict[str, str], destination: str) -> bytes:
    """Read the committed runtime when present, otherwise its packaged template."""
    source = destination
    if not managed_source_exists(repository, commit, destination):
        try:
            source = assets[destination]
        except KeyError as exc:
            raise ManagedRuntimeError(
                f"managed script destination is absent from its manifest: {destination}") from exc
    data = managed_source_bytes(repository, commit, source)
    if source == destination:
        # Re-resolve the installed manifest here so every caller, including
        # historical binding and doctor paths, remains checksum-bound.
        provenance = managed_target_provenance(repository, commit)
        if provenance["mode"] == "installed" and provenance["assets"].get(destination) != destination:
            raise ManagedRuntimeError(f"installed managed script is undeclared: {destination}")
    return data


def managed_package_version(repository: Path, commit: str) -> str:
    return managed_target_provenance(repository, commit)["package_version"]


def managed_policy_generations(repository: Path, previous_sha: str, target_sha: str,
                               current: dict[str, Any], *, policy_dirty: bool
                               ) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve policy bytes without assuming controller-private files live in product Git."""
    previous_exists = managed_source_exists(repository, previous_sha, MANAGED_POLICY_PATH)
    target_exists = managed_source_exists(repository, target_sha, MANAGED_POLICY_PATH)
    if previous_exists and target_exists:
        return (managed_source_json(repository, previous_sha, MANAGED_POLICY_PATH),
                managed_source_json(repository, target_sha, MANAGED_POLICY_PATH))
    if previous_exists != target_exists:
        raise ManagedRuntimeError("task policy generation provenance is ambiguous")

    previous_provenance = managed_target_provenance(repository, previous_sha)
    target_provenance = managed_target_provenance(repository, target_sha)
    if previous_provenance["mode"] != "installed" or target_provenance["mode"] != "installed":
        raise ManagedRuntimeError("task policy generations are absent from product Git")

    records = []
    for commit, provenance in ((previous_sha, previous_provenance),
                               (target_sha, target_provenance)):
        manifest = managed_source_json(repository, commit, MANAGED_INSTALLED_MANIFEST_PATH)
        records.append(manifest["assets"].get(MANAGED_POLICY_PATH))

    missing = [record is None for record in records]
    if any(missing):
        manifests = [managed_source_json(repository, commit, MANAGED_INSTALLED_MANIFEST_PATH)
                     for commit in (previous_sha, target_sha)]
        identities = [(manifest.get("schemaVersion"), manifest.get("packageName"),
                       manifest.get("packageVersion")) for manifest in manifests]
        # Some supported bootstraps omit this controller-private asset. Do not
        # identify that capability by a release number: both commits must carry
        # the exact same well-formed package identity and manifest payload.
        # Together with managed_target_provenance() authenticating every declared
        # installed byte, that admits only a product-only transition and rejects
        # package, asset, or hidden policy-generation ambiguity.
        compatible = (missing == [True, True]
                      and not policy_dirty
                      and identities[0] == identities[1]
                      and manifests[0] == manifests[1])
        if not compatible:
            raise ManagedRuntimeError("installed task policy provenance is invalid")
        return current, current

    for record, provenance in zip(records, (previous_provenance, target_provenance)):
        if (not isinstance(record, dict) or record.get("type") != "config"
                or record.get("templateVersion") != provenance["package_version"]
                or not MANAGED_HASH_RE.fullmatch(record.get("sourceSha256", ""))
                or not MANAGED_HASH_RE.fullmatch(record.get("installedSha256", ""))):
            raise ManagedRuntimeError("installed task policy provenance is invalid")
    if records[0]["sourceSha256"] != records[1]["sourceSha256"]:
        raise ManagedRuntimeError("installed task policy source generation changed without immutable bytes")

    # The controller policy is tracked and clean (enforced by the caller). An
    # unchanged installed template hash proves this package transition has no
    # policy delta, so preserving those authenticated controller bytes is exact.
    return current, current


def managed_safe_path(controller: Path, relative: str) -> Path:
    destination = (controller / relative).resolve()
    try:
        destination.relative_to(controller.resolve())
    except ValueError as exc:
        raise ManagedRuntimeError(f"managed destination escapes controller: {relative}") from exc
    cursor = controller.resolve()
    for part in Path(relative).parts:
        cursor /= part
        if cursor.is_symlink():
            raise ManagedRuntimeError(f"managed destination contains a symbolic link: {relative}")
    return destination


def commit_tree(repository: Path, commit: str) -> str | None:
    value = git(repository, "rev-parse", "--verify", f"{commit}^{{tree}}", check=False)
    return value if SHA_RE.fullmatch(value) else None


def commit_path_bytes(repository: Path, commit: str, relative: str) -> bytes | None:
    result = managed_run(["git", "-C", str(repository), "show", f"{commit}:{relative}"],
                         repository, check=False)
    return result.stdout if result.returncode == 0 else None


def commit_tracks_path(repository: Path, commit: str, relative: str) -> bool:
    return bool(git(repository, "ls-tree", "-r", "--name-only", commit, "--", relative,
                    check=False).splitlines())


def version_cache_commit_evidence(repository: Path, commit: str) -> dict[str, Any]:
    """Bind the two legacy findings to immutable commit-tree evidence."""
    script = commit_path_bytes(repository, commit, INSTALL_REQUIREMENTS_PATH)
    markers = [managed_sha256(marker) for marker in LEGACY_VERSION_CACHE_MARKERS
               if script is not None and marker in script]
    return {
        "commit": commit,
        "tree": commit_tree(repository, commit),
        "legacy_writer": {
            "path": INSTALL_REQUIREMENTS_PATH,
            "present": bool(markers),
            "content_sha256": managed_sha256(script) if script is not None else None,
            "marker_sha256": markers,
        },
        "tracked_cache": {
            "path": VERSION_CACHE_PATH,
            "present": commit_tracks_path(repository, commit, VERSION_CACHE_PATH),
        },
    }


def managed_version_cache_findings(root: Path, workspace: str) -> list[dict[str, str]]:
    """Report exact legacy cache paths without changing checkout bytes."""
    root = root.expanduser().resolve()
    if not root.is_dir():
        return []
    findings: list[dict[str, str]] = []
    script = root / INSTALL_REQUIREMENTS_PATH
    try:
        script_bytes = script.read_bytes()
    except OSError:
        script_bytes = b""
    if any(marker in script_bytes for marker in LEGACY_VERSION_CACHE_MARKERS):
        findings.append({
            "code": "legacy_checkout_local_version_cache_writer",
            "severity": "error",
            "workspace": workspace,
            "path": str(script),
            "message": ("legacy install_requirements.sh writes transient version-check state "
                        "inside the checkout; migrate the exact managed script generation"),
        })
    tracked = managed_run(
        ["git", "-C", str(root), "ls-files", "--error-unmatch", "--", VERSION_CACHE_PATH],
        root, check=False)
    if tracked.returncode == 0:
        cache = root / VERSION_CACHE_PATH
        status = managed_run(
            ["git", "-C", str(root), "status", "--porcelain=v1", "--", VERSION_CACHE_PATH],
            root, check=False).stdout.decode(errors="replace").strip()
        findings.append({
            "code": "tracked_worktree_version_cache",
            "severity": "error",
            "workspace": workspace,
            "path": str(cache),
            "state": "modified" if status else "tracked",
            "message": ("tracked transient version-check cache must be removed by a normal "
                        "task/merge change; no automatic restore or cleanup was performed"),
        })
    return findings


def managed_policy_projection(previous: dict[str, Any], target: dict[str, Any], current: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    if not all(isinstance(value, dict) for value in (previous, target, current)):
        raise ManagedRuntimeError("task policy generations must be JSON objects")
    result = dict(current)
    changed: list[str] = []
    missing = object()
    for key in sorted(set(previous) | set(target)):
        old = previous.get(key, missing)
        new = target.get(key, missing)
        if old == new:
            continue
        observed = current.get(key, missing)
        if observed == new:
            continue
        if observed != old:
            raise ManagedRuntimeError(f"tracked task policy has an overlapping manual change: {key}")
        if new is missing:
            result.pop(key, None)
        else:
            result[key] = new
        changed.append(key)
    return result, changed


def managed_canonical_json(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=False) + "\n").encode()


def managed_atomic_write(path: Path, data: bytes, mode: int | None = None, *,
                         expected_old: tuple[bool, str | None] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data); handle.flush(); os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        if expected_old is not None:
            expected_exists, expected_sha256 = expected_old
            observed = path.read_bytes() if path.is_file() else None
            if ((observed is not None) != expected_exists
                    or (managed_sha256(observed) if observed is not None else None)
                    != expected_sha256):
                raise ManagedWriteCollision(path, expected_exists,
                                            expected_sha256, observed)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def managed_receipt_write(path: Path, value: dict[str, Any]) -> dict[str, str]:
    """Create one immutable receipt; byte-identical retries are read-only."""
    data = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        if path.is_file() and path.read_bytes() == data:
            return {"path": str(path.resolve()), "sha256": managed_sha256(data)}
        raise ManagedRuntimeError(f"immutable managed receipt collision: {path}") from exc
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return {"path": str(path.resolve()), "sha256": managed_sha256(data)}


def managed_preserve_collision_bytes(controller: Path, data: bytes) -> dict[str, str]:
    identity = managed_sha256(data)
    path = managed_safe_path(controller, f"{MANAGED_COLLISION_ROOT}/bytes/{identity}.bin")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        if path.is_file() and path.read_bytes() == data:
            return {"path": str(path), "sha256": identity}
        raise ManagedRuntimeError("immutable managed collision byte-set identity collided") from exc
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data); stream.flush(); os.fsync(stream.fileno())
    return {"path": str(path), "sha256": identity}


def compile_managed_write_set(controller: Path,
                              writes: list[tuple[Path, bytes | None, int | None]]) -> dict[str, Any]:
    """Bind every exact destination to the bytes observed before mutation."""
    rows: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for destination, data, mode in writes:
        destination = destination.resolve()
        if destination in seen:
            raise ManagedRuntimeError("managed destination write set is duplicated")
        destination.relative_to(controller.resolve())
        before = destination.read_bytes() if destination.is_file() else None
        rows.append({"path": str(destination.relative_to(controller.resolve())),
                     "expected_exists": before is not None,
                     "expected_old_sha256": managed_sha256(before) if before is not None else None,
                     "intended_sha256": managed_sha256(data) if data is not None else None,
                     "mode": mode, "_expected_bytes": before})
        seen.add(destination)
    public = [{key: value for key, value in row.items() if not key.startswith("_")}
              for row in rows]
    identity = managed_sha256((json.dumps(public, sort_keys=True,
                                           separators=(",", ":")) + "\n").encode())
    return {"schema_version": MANAGED_WRITE_SET_SCHEMA, "write_set_sha256": identity,
            "destinations": rows}


def managed_allocate_log(workflow: str, task_id: str) -> tuple[Path, Any]:
    safe_workflow = re.sub(r"[^A-Za-z0-9_.-]+", "-", workflow).strip("-") or "runtime"
    safe_task = re.sub(r"[^A-Za-z0-9_.-]+", "-", task_id).strip("-") or "target"
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    for sequence in range(1000):
        suffix = "" if sequence == 0 else f"-{sequence}"
        path = Path("/tmp") / f"yy-{safe_workflow}-{safe_task}-{stamp}-{os.getpid()}{suffix}.log"
        try:
            handle = path.open("x", encoding="utf-8")
            return path, handle
        except FileExistsError:
            continue
        except OSError as exc:
            raise ManagedRuntimeError(f"managed runtime log allocation failed: {exc}") from exc
    raise ManagedRuntimeError("managed runtime log namespace exhausted")


def managed_tracked_policy_dirty(controller: Path) -> bool:
    tracked = managed_run(["git", "-C", str(controller), "ls-files", "--error-unmatch", "--", MANAGED_POLICY_PATH],
                  controller, check=False)
    if tracked.returncode:
        raise ManagedRuntimeError("tracked task policy is absent or ambiguous")
    dirty = managed_run(["git", "-C", str(controller), "status", "--porcelain=v1", "--", MANAGED_POLICY_PATH],
                controller).stdout
    return bool(dirty)


def managed_obsolete_generation_binding(controller: Path, repository: Path, relative: str,
                                          current: bytes, target_sha: str) -> dict[str, str] | None:
    """Recognize bytes previously installed as an exact managed generation.

    A package/bootstrap process can regress ignored controller scripts without
    updating generation.json. Completed local receipt fields are eligible only
    when an exact row's bytes equal immutable Git source in the admitted target
    ancestry. This corroborates the fields; it does not authenticate or sign the
    receipt. A preserved-customization row is eligible only when immutable
    target ancestry independently proves that its exact bytes were once a
    managed implementation; the receipt classification alone is not trusted.
    """
    receipts = managed_safe_path(controller, MANAGED_RECEIPT_ROOT)
    if not receipts.is_dir() or receipts.is_symlink():
        return None
    current_hash = managed_sha256(current)
    for receipt_path in sorted(receipts.glob("*.json"), reverse=True):
        if receipt_path.is_symlink() or not receipt_path.is_file():
            continue
        try:
            if receipt_path.stat().st_size > 1024 * 1024:
                continue
            receipt = json.loads(receipt_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(receipt, dict):
            continue
        receipt_target = receipt.get("target_sha")
        if (receipt.get("schema_version") != MANAGED_RUNTIME_SCHEMA
                or receipt.get("operation") != "refresh" or receipt.get("outcome") != "completed"
                or not isinstance(receipt_target, str) or not MANAGED_SHA_RE.fullmatch(receipt_target)
                or not isinstance(receipt.get("scripts"), list)):
            continue
        if managed_run(["git", "-C", str(repository), "merge-base", "--is-ancestor",
                        receipt_target, target_sha], repository, check=False).returncode:
            continue
        for row in receipt["scripts"]:
            if (not isinstance(row, dict) or row.get("path") != relative
                    or row.get("classification") not in {"exact", "preserved_customization"}
                    or row.get("actual_sha256") != current_hash):
                continue
            if row.get("classification") == "exact" and row.get("source_sha256") != current_hash:
                continue
            source_paths = [relative]
            target_source = managed_script_assets(repository, target_sha).get(relative)
            if target_source and target_source not in source_paths:
                source_paths.append(target_source)
            historical = None
            historical_path = None
            for source_path in source_paths:
                candidates = git_bytes(
                    repository, "rev-list", receipt_target, "--", source_path).decode().splitlines()
                for candidate in candidates:
                    if managed_run(["git", "-C", str(repository), "merge-base", "--is-ancestor",
                                    candidate, receipt_target], repository, check=False).returncode:
                        continue
                    try:
                        if managed_source_bytes(repository, candidate, source_path) == current:
                            historical, historical_path = candidate, source_path
                            break
                    except ManagedRuntimeError:
                        continue
                if historical is not None:
                    break
            if historical is not None:
                return {"classification": (
                            "receipt_bound_obsolete_generation" if row.get("classification") == "exact"
                            else "receipt_bound_historical_generation"),
                        "target_sha": historical, "source_path": historical_path,
                        "receipt_path": str(receipt_path.resolve())}
    return None


def managed_historical_source_binding(repository: Path, relative: str, current: bytes,
                                      previous_sha: str,
                                      target_assets: dict[str, str]) -> dict[str, str] | None:
    """Recognize exact immutable history when a script first becomes managed."""
    source_paths = [relative]
    target_source = target_assets.get(relative)
    if target_source and target_source not in source_paths:
        source_paths.append(target_source)
    for source_path in source_paths:
        candidates = git_bytes(
            repository, "rev-list", previous_sha, "--", source_path).decode().splitlines()
        for candidate in candidates:
            try:
                if managed_source_bytes(repository, candidate, source_path) == current:
                    return {"classification": "immutable_historical_generation",
                            "target_sha": candidate, "source_path": source_path}
            except ManagedRuntimeError:
                continue
    return None


def managed_runtime_plan(controller: Path, repository: Path, previous_sha: str, target_sha: str,
                         approved: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    controller = controller.resolve(); repository = repository.resolve()
    previous_sha = managed_exact_commit(repository, previous_sha, "previous generation")
    target_sha = managed_exact_commit(repository, target_sha, "target generation")
    if managed_run(["git", "-C", str(repository), "merge-base", "--is-ancestor", previous_sha, target_sha],
           repository, check=False).returncode:
        raise ManagedRuntimeError("target generation does not descend from previous generation")
    policy_dirty = managed_tracked_policy_dirty(controller)
    target_assets = managed_script_assets(repository, target_sha)
    previous_assets = managed_script_assets(repository, previous_sha)
    scripts = set(target_assets)
    prior_scripts = set(previous_assets)
    generation_path = managed_safe_path(controller, MANAGED_GENERATION_PATH)
    try:
        prior_generation = json.loads(generation_path.read_text())
    except FileNotFoundError:
        prior_generation = None
    except (OSError, json.JSONDecodeError) as exc:
        raise ManagedRuntimeError(f"managed generation receipt is invalid: {exc}") from exc
    prior_entries = (prior_generation.get("scripts")
                     if isinstance(prior_generation, dict)
                     and prior_generation.get("schema_version") == MANAGED_RUNTIME_SCHEMA
                     and prior_generation.get("target_sha") == previous_sha
                     and isinstance(prior_generation.get("scripts"), dict) else {})
    actions = []
    for relative in sorted(scripts | prior_scripts):
        destination = managed_safe_path(controller, relative)
        old = managed_script_source_bytes(repository, previous_sha, previous_assets, relative) \
            if relative in prior_scripts else None
        new = managed_script_source_bytes(repository, target_sha, target_assets, relative) \
            if relative in scripts else None
        current = destination.read_bytes() if destination.exists() else None
        classification = "exact"
        prior_binding = (managed_obsolete_generation_binding(
            controller, repository, relative, current, target_sha)
            if current is not None and new is not None and current != new else None)
        if (prior_binding is None and current is not None and new is not None
                and current != new and relative not in previous_assets):
            prior_binding = managed_historical_source_binding(
                repository, relative, current, previous_sha, target_assets)
        if new is None:
            if current is not None and current != old:
                raise ManagedRuntimeError(f"customized retired managed runtime is preserved: {relative}")
            outcome = "unchanged" if current is None else "removed"
            classification = "retired"
            actual = None
        elif current is not None and current not in {old, new}:
            prior_entry = prior_entries.get(relative)
            receipt_bound_prior = bool(
                old is not None and old != new and isinstance(prior_entry, dict)
                and prior_entry.get("classification") == "preserved_customization"
                and prior_entry.get("source_sha256") == managed_sha256(old)
                and prior_entry.get("actual_sha256") == managed_sha256(current)
            )
            if receipt_bound_prior:
                prior_template = managed_source_bytes(
                    repository, previous_sha, previous_assets[relative])
                if prior_template == current:
                    prior_binding = {"classification": "receipt_bound_installed_template",
                                     "target_sha": previous_sha}
            target_entry = (prior_generation.get("scripts", {}).get(relative)
                            if isinstance(prior_generation, dict)
                            and prior_generation.get("schema_version") == MANAGED_RUNTIME_SCHEMA
                            and prior_generation.get("target_sha") == target_sha
                            and isinstance(prior_generation.get("scripts"), dict) else None)
            target_bound_preserved = bool(
                isinstance(target_entry, dict)
                and target_entry.get("classification") == "preserved_customization"
                and target_entry.get("source_sha256") == managed_sha256(new)
                and target_entry.get("actual_sha256") == managed_sha256(current))
            approval = (approved or {}).get(relative)
            if target_bound_preserved:
                outcome = "preserved_customization"
                classification = "preserved_customization"
                actual = current
            elif approval is not None:
                expected = {"path": relative, "old_sha256": managed_sha256(old),
                            "current_sha256": managed_sha256(current),
                            "new_sha256": managed_sha256(new)}
                if any(approval.get(key) != value for key, value in expected.items()):
                    raise ManagedRuntimeError(f"stale managed runtime repair identity: {relative}")
                resolution = approval.get("resolution")
                encoded = approval.get("resolved_bytes_base64")
                if resolution not in {"supersede", "preserve"} or not isinstance(encoded, str):
                    raise ManagedRuntimeError(f"malformed managed runtime repair action: {relative}")
                try:
                    resolved = base64.b64decode(encoded, validate=True)
                except (ValueError, TypeError) as exc:
                    raise ManagedRuntimeError(f"malformed managed runtime repair bytes: {relative}") from exc
                if approval.get("resolved_sha256") != managed_sha256(resolved):
                    raise ManagedRuntimeError(f"managed runtime repair result hash mismatch: {relative}")
                if resolution == "supersede" and resolved != new:
                    raise ManagedRuntimeError(f"managed runtime supersede result is not exact: {relative}")
                outcome = "updated"
                classification = "exact" if resolved == new else "preserved_customization"
                actual = resolved
            elif old != new and prior_binding is None:
                raise ManagedRuntimeError(
                    f"customized managed runtime overlaps changed source: {relative}; "
                    "plan recovery with integration runtime-refresh --dry-run")
            elif prior_binding is not None:
                # Exact receipt/source history proves these are managed bytes,
                # including an obsolete generation restored by later bootstrap.
                outcome = "updated"
                classification = "exact"
                actual = new
            else:
                # The packaged source is identical on both sides of the admitted
                # transition, so this owner customization is unrelated to it.
                outcome = "preserved_customization"
                classification = "preserved_customization"
                actual = current
        else:
            outcome = "unchanged" if current == new else "installed" if current is None else "updated"
            actual = new
        actions.append({"path": relative, "classification": classification,
                        "prior_generation_classification": (
                            prior_binding.get("classification") if prior_binding else None),
                        "prior_generation_target_sha": (
                            prior_binding.get("target_sha") if prior_binding else None),
                        "prior_generation_receipt": (
                            prior_binding.get("receipt_path") if prior_binding else None),
                        "before_sha256": managed_sha256(current) if current is not None else None,
                        "actual_sha256": managed_sha256(actual) if actual is not None else None,
                        "source_sha256": managed_sha256(new) if new is not None else None, "bytes": actual,
                        "outcome": outcome})
    # A retry may upgrade the original exact-only generation format, but it must
    # never reclassify drift after a terminal generation as a new customization.
    try:
        existing_generation = json.loads(generation_path.read_text())
    except FileNotFoundError:
        existing_generation = None
    except (OSError, json.JSONDecodeError) as exc:
        raise ManagedRuntimeError(f"managed generation receipt is invalid: {exc}") from exc
    if isinstance(existing_generation, dict) and existing_generation.get("target_sha") == target_sha:
        existing_scripts = existing_generation.get("scripts")
        if not isinstance(existing_scripts, dict):
            raise ManagedRuntimeError("existing managed generation identity has drifted")
        for row in actions:
            if row["source_sha256"] is None:
                continue
            entry = existing_scripts.get(row["path"])
            bound_actual = entry if isinstance(entry, str) else (
                entry.get("actual_sha256") if isinstance(entry, dict)
                and entry.get("source_sha256") == row["source_sha256"]
                and entry.get("classification") in {"exact", "preserved_customization"} else None)
            if (bound_actual != row["before_sha256"]
                    and row.get("prior_generation_classification")
                    != "receipt_bound_obsolete_generation"):
                raise ManagedRuntimeError(f"existing managed generation drift: {row['path']}")
    policy_path = managed_safe_path(controller, MANAGED_POLICY_PATH)
    try:
        current_policy_bytes = policy_path.read_bytes()
        current_policy = json.loads(current_policy_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise ManagedRuntimeError(f"controller task policy is invalid: {exc}") from exc
    previous_policy, target_policy = managed_policy_generations(
        repository, previous_sha, target_sha, current_policy, policy_dirty=policy_dirty)
    projected, changed_fields = managed_policy_projection(previous_policy, target_policy, current_policy)
    if policy_dirty:
        generation_path = managed_safe_path(controller, MANAGED_GENERATION_PATH)
        try:
            generation = json.loads(generation_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ManagedRuntimeError("tracked task policy has uncommitted dirt") from exc
        if (not isinstance(generation, dict) or generation.get("schema_version") != MANAGED_RUNTIME_SCHEMA
                or generation.get("target_sha") != target_sha
                or generation.get("policy_sha256") != managed_sha256(current_policy_bytes)):
            raise ManagedRuntimeError("tracked task policy has uncommitted dirt")
    # A no-op target transition must not normalize owner formatting and create
    # tracked dirt. Canonical bytes are emitted only when admitted fields change.
    projected_bytes = managed_canonical_json(projected) if changed_fields else current_policy_bytes
    return {"controller": str(controller), "repository": str(repository),
            "previous_sha": previous_sha, "target_sha": target_sha,
            "package_version": managed_package_version(repository, target_sha), "scripts": actions,
            "policy": {"path": MANAGED_POLICY_PATH, "before_sha256": managed_sha256(current_policy_bytes),
                       "after_sha256": managed_sha256(projected_bytes), "changed_fields": changed_fields,
                       "bytes": projected_bytes}}


def managed_semantic_diff(old: bytes, current: bytes, new: bytes, resolved: bytes | None) -> dict[str, str]:
    def diff(left: bytes, right: bytes, left_name: str, right_name: str) -> str:
        try:
            before = left.decode("utf-8").splitlines(keepends=True)
            after = right.decode("utf-8").splitlines(keepends=True)
        except UnicodeDecodeError:
            return "binary content; hashes are authoritative\n"
        return "".join(difflib.unified_diff(before, after, fromfile=left_name, tofile=right_name))
    result = {"old_to_prior": diff(old, current, "old-source", "preserved-prior"),
              "old_to_new": diff(old, new, "old-source", "new-source")}
    if resolved is not None:
        result["prior_to_resolved"] = diff(current, resolved, "preserved-prior", "resolved")
    return result


def managed_three_way_merge(old: bytes, current: bytes, new: bytes) -> bytes | None:
    with tempfile.TemporaryDirectory(prefix="yy-managed-repair-") as temporary:
        root = Path(temporary)
        ours, base, theirs = root / "prior", root / "old", root / "new"
        ours.write_bytes(current); base.write_bytes(old); theirs.write_bytes(new)
        result = managed_run(["git", "merge-file", "-p", str(ours), str(base), str(theirs)], root, check=False)
        return result.stdout if result.returncode == 0 else None


def managed_changed_source_overlaps(controller: Path, repository: Path, previous_sha: str,
                                    target_sha: str) -> dict[str, dict[str, Any]]:
    previous_assets = managed_script_assets(repository, previous_sha)
    target_assets = managed_script_assets(repository, target_sha)
    overlaps: dict[str, dict[str, Any]] = {}
    for relative in sorted(set(previous_assets) & set(target_assets)):
        old = managed_script_source_bytes(repository, previous_sha, previous_assets, relative)
        new = managed_script_source_bytes(repository, target_sha, target_assets, relative)
        destination = managed_safe_path(controller, relative)
        current = destination.read_bytes() if destination.is_file() else None
        if current is None or old == new or current in {old, new}:
            continue
        overlaps[relative] = {"path": relative, "old": old, "current": current, "new": new,
                              "old_sha256": managed_sha256(old),
                              "current_sha256": managed_sha256(current),
                              "new_sha256": managed_sha256(new)}
    return overlaps


def managed_runtime_repair_plan(controller: Path, repository: Path, previous_sha: str,
                                target_sha: str, *, task_id: str = "manual") -> dict[str, Any]:
    controller = controller.resolve(); repository = repository.resolve()
    previous_sha = managed_exact_commit(repository, previous_sha, "previous generation")
    target_sha = managed_exact_commit(repository, target_sha, "target generation")
    if managed_run(["git", "-C", str(repository), "merge-base", "--is-ancestor", previous_sha, target_sha],
                   repository, check=False).returncode:
        raise ManagedRuntimeError("target generation does not descend from previous generation")
    overlaps = managed_changed_source_overlaps(controller, repository, previous_sha, target_sha)
    actions: list[dict[str, Any]] = []
    for relative, identity in overlaps.items():
        old, current, new = identity["old"], identity["current"], identity["new"]
        binding = managed_obsolete_generation_binding(controller, repository, relative, current, target_sha)
        merged = new if binding is not None else managed_three_way_merge(old, current, new)
        resolution = "supersede" if binding is not None else "preserve" if merged is not None else "conflict"
        actions.append({"path": relative, "old_sha256": managed_sha256(old),
                        "current_sha256": managed_sha256(current), "new_sha256": managed_sha256(new),
                        "resolution": resolution,
                        "historical_binding": binding,
                        "prior_bytes_base64": base64.b64encode(current).decode(),
                        "resolved_bytes_base64": base64.b64encode(merged).decode() if merged is not None else None,
                        "resolved_sha256": managed_sha256(merged) if merged is not None else None,
                        "semantic_diff": managed_semantic_diff(old, current, new, merged)})
    if not actions:
        raise ManagedRuntimeError("no changed-source managed customization requires repair")
    receipt = {"schema_version": MANAGED_REPAIR_SCHEMA, "operation": "repair-plan",
               "outcome": "conflict" if any(row["resolution"] == "conflict" for row in actions) else "planned",
               "controller": str(controller), "repository": str(repository),
               "previous_sha": previous_sha, "target_sha": target_sha,
               "task_id": task_id, "actions": actions}
    receipt_bytes = (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode()
    receipt_hash = managed_sha256(receipt_bytes)
    path = managed_safe_path(controller, f"{MANAGED_RECEIPT_ROOT}/{receipt_hash}-repair-plan.json")
    if path.exists() and path.read_bytes() != receipt_bytes:
        raise ManagedRuntimeError("immutable managed runtime repair receipt collision")
    reference = managed_receipt_write(path, receipt)
    return {**receipt, "receipt": reference}


def managed_runtime_repair_load(controller: Path, repository: Path, receipt_path: Path,
                                previous_sha: str, target_sha: str) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    path = receipt_path.expanduser().resolve()
    root = managed_safe_path(controller.resolve(), MANAGED_RECEIPT_ROOT)
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise ManagedRuntimeError("repair receipt is outside the managed receipt root") from exc
    try:
        raw = path.read_bytes()
        receipt = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise ManagedRuntimeError(f"managed runtime repair receipt is invalid: {exc}") from exc
    receipt_hash = managed_sha256(raw)
    if path.name != f"{receipt_hash}-repair-plan.json":
        raise ManagedRuntimeError("managed runtime repair receipt immutable identity mismatch")
    required = {"schema_version", "operation", "outcome", "controller", "repository",
                "previous_sha", "target_sha", "task_id", "actions"}
    if (not isinstance(receipt, dict) or set(receipt) != required
            or receipt.get("schema_version") != MANAGED_REPAIR_SCHEMA
            or receipt.get("operation") != "repair-plan" or receipt.get("outcome") != "planned"
            or receipt.get("controller") != str(controller.resolve())
            or receipt.get("repository") != str(repository.resolve())
            or receipt.get("previous_sha") != previous_sha or receipt.get("target_sha") != target_sha
            or not isinstance(receipt.get("actions"), list)):
        raise ManagedRuntimeError("malformed or mismatched managed runtime repair receipt")
    approvals: dict[str, dict[str, Any]] = {}
    for row in receipt["actions"]:
        expected = {"path", "old_sha256", "current_sha256", "new_sha256", "resolution",
                    "historical_binding", "prior_bytes_base64", "resolved_bytes_base64",
                    "resolved_sha256", "semantic_diff"}
        if (not isinstance(row, dict) or set(row) != expected or row.get("resolution") not in {"supersede", "preserve"}
                or not isinstance(row.get("path"), str) or row["path"] in approvals
                or not all(MANAGED_HASH_RE.fullmatch(row.get(key, ""))
                           for key in ("old_sha256", "current_sha256", "new_sha256", "resolved_sha256"))):
            raise ManagedRuntimeError("malformed managed runtime repair action")
        try:
            prior = base64.b64decode(row["prior_bytes_base64"], validate=True)
        except (ValueError, TypeError) as exc:
            raise ManagedRuntimeError("malformed managed runtime repair prior bytes") from exc
        if managed_sha256(prior) != row["current_sha256"]:
            raise ManagedRuntimeError("managed runtime repair prior-byte hash mismatch")
        approvals[row["path"]] = row
    overlaps = managed_changed_source_overlaps(
        controller.resolve(), repository.resolve(), previous_sha, target_sha)
    if set(approvals) != set(overlaps):
        missing = sorted(set(overlaps) - set(approvals))
        extra = sorted(set(approvals) - set(overlaps))
        raise ManagedRuntimeError(
            f"managed runtime repair action set mismatch: missing={missing} extra={extra}")
    for relative, row in approvals.items():
        identity = overlaps[relative]
        for key in ("old_sha256", "current_sha256", "new_sha256"):
            if row.get(key) != identity[key]:
                raise ManagedRuntimeError(f"stale managed runtime repair identity: {relative}")
    return {**receipt, "receipt_sha256": receipt_hash, "receipt_path": str(path)}, approvals


_generation_mutations: ContextVar[frozenset[str]] = ContextVar("generation_mutations", default=frozenset())


@contextmanager
def managed_generation_mutation(controller: Path):
    """Maintenance lock order: integration-target (if needed), then generation.

    The package engine uses this same lock inode. Nested refresh and rollback
    remain inside the source-adoption window; no expiry grants ownership.
    """
    controller = controller.resolve()
    key = str(controller)
    inherited = _generation_mutations.get()
    if key in inherited:
        yield
        return
    lock = managed_safe_path(controller, ".juno_task/runtime/generation-migration/lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    token = None
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ManagedRuntimeError("generation_migration_busy: another generation writer owns the lock") from exc
        fence = managed_safe_path(controller, ".juno_task/runtime/generation-migration/fence.json")
        if fence.exists():
            raise ManagedRuntimeError("generation_transition_incomplete: resume or roll back the exact journal")
        token = _generation_mutations.set(inherited | {key})
        yield
    finally:
        if token is not None:
            _generation_mutations.reset(token)
        os.close(descriptor)


def managed_runtime_refresh(controller: Path, repository: Path, previous_sha: str, target_sha: str,
            *, task_id: str = "target", repair_receipt: Path | None = None) -> dict[str, Any]:
    with managed_generation_mutation(controller):
        return _managed_runtime_refresh_locked(controller, repository, previous_sha, target_sha,
                                              task_id=task_id, repair_receipt=repair_receipt)


def _managed_runtime_refresh_locked(controller: Path, repository: Path, previous_sha: str, target_sha: str,
            *, task_id: str = "target", repair_receipt: Path | None = None) -> dict[str, Any]:
    started = time.time(); started_mono = time.monotonic()
    log_path, log = managed_allocate_log("managed-runtime-refresh", task_id)
    print(f"yy managed-runtime-refresh log: {log_path}", file=sys.stderr, flush=True)
    receipt_path = managed_safe_path(controller.resolve(), f"{MANAGED_RECEIPT_ROOT}/{time.time_ns()}-{os.getpid()}.json")
    receipt: dict[str, Any] = {"schema_version": MANAGED_RUNTIME_SCHEMA, "operation": "refresh",
                              "outcome": "running", "previous_sha": previous_sha,
                              "target_sha": target_sha, "task_id": task_id}
    backups: dict[Path, tuple[bool, bytes, int]] = {}
    mutated: list[tuple[Path, bytes | None]] = []
    write_set: dict[str, Any] | None = None
    reference: dict[str, str] | None = None
    try:
        previous_sha = managed_exact_commit(repository, previous_sha, "previous generation")
        target_sha = managed_exact_commit(repository, target_sha, "target generation")
        if managed_run(["git", "-C", str(repository), "merge-base", "--is-ancestor",
                        previous_sha, target_sha], repository, check=False).returncode:
            raise ManagedRuntimeError("target generation does not descend from previous generation")
        repair = None
        approvals = None
        if repair_receipt is not None:
            repair, approvals = managed_runtime_repair_load(
                controller, repository, repair_receipt, previous_sha, target_sha)
            for row in approvals.values():
                destination = managed_safe_path(controller, row["path"])
                current = destination.read_bytes() if destination.is_file() else None
                if current is None or managed_sha256(current) != row["current_sha256"]:
                    raise ManagedRuntimeError(f"stale managed runtime repair identity: {row['path']}")
                backup = managed_safe_path(
                    controller, f"{MANAGED_BACKUP_ROOT}/{repair['receipt_sha256']}/{row['current_sha256']}.bin")
                if backup.exists() and backup.read_bytes() != current:
                    raise ManagedRuntimeError(f"managed runtime backup collision: {row['path']}")
                if not backup.exists():
                    managed_atomic_write(backup, current, 0o600)
        operation = managed_runtime_plan(controller, repository, previous_sha, target_sha, approvals)
        log.write(f"source target={target_sha} package={operation['package_version']}\n"); log.flush()
        writes = [(managed_safe_path(controller, row["path"]), row["bytes"],
                   None if row["outcome"] == "removed" else 0o755)
                  for row in operation["scripts"]
                  if row["outcome"] in {"installed", "updated", "removed"}]
        if operation["policy"]["before_sha256"] != operation["policy"]["after_sha256"]:
            writes.append((managed_safe_path(controller, MANAGED_POLICY_PATH), operation["policy"]["bytes"], 0o644))
        generation_path = managed_safe_path(controller, MANAGED_GENERATION_PATH)
        generation = {"schema_version": MANAGED_RUNTIME_SCHEMA, "target_sha": target_sha,
                      "package_version": operation["package_version"],
                      "scripts": {row["path"]: {
                          "classification": row["classification"],
                          "source_sha256": row["source_sha256"],
                          "actual_sha256": row["actual_sha256"],
                      } for row in operation["scripts"] if row["source_sha256"] is not None},
                      "policy_sha256": operation["policy"]["after_sha256"]}
        writes.append((generation_path, managed_canonical_json(generation), 0o600))
        write_set = compile_managed_write_set(controller.resolve(), writes)
        rows_by_path = {str((controller.resolve() / row["path"]).resolve()): row
                        for row in write_set["destinations"]}
        for destination, _, _ in writes:
            row = rows_by_path[str(destination.resolve())]
            before = row["_expected_bytes"]
            backups[destination] = (before is not None, before or b"",
                                    destination.stat().st_mode & 0o777
                                    if before is not None else 0)
        for destination, data, mode in writes:
            row = rows_by_path[str(destination.resolve())]
            expected = (row["expected_exists"], row["expected_old_sha256"])
            if data is None:
                observed = destination.read_bytes() if destination.is_file() else None
                if ((observed is not None) != expected[0]
                        or (managed_sha256(observed) if observed is not None else None) != expected[1]):
                    raise ManagedWriteCollision(destination, expected[0], expected[1], observed)
                destination.unlink(missing_ok=True)
                mutated.append((destination, None))
                log.write(f"remove {destination}\n")
            else:
                managed_atomic_write(destination, data, mode, expected_old=expected)
                mutated.append((destination, data))
                log.write(f"write {destination} sha256={managed_sha256(data)}\n")
            log.flush()
        doctor = managed_runtime_inspect(controller, repository, target_sha)
        if not doctor["healthy"]:
            raise ManagedRuntimeError("post-refresh doctor did not reach a coherent generation")
        receipt.update({"outcome": "completed", "package_version": operation["package_version"],
                        "repair_plan": ({"path": repair["receipt_path"],
                                         "sha256": repair["receipt_sha256"]} if repair else None),
                        "scripts": [{key: value for key, value in row.items() if key != "bytes"}
                                    for row in operation["scripts"]],
                        "policy": {key: value for key, value in operation["policy"].items() if key != "bytes"},
                        "doctor": doctor})
    except BaseException as exc:
        # Roll back only writes proven to be ours. A concurrent byte set that
        # appears after one of our writes is never overwritten by recovery.
        for destination, intended in reversed(mutated):
            existed, data, mode = backups[destination]
            try:
                current = destination.read_bytes() if destination.is_file() else None
                intended_identity = managed_sha256(intended) if intended is not None else None
                if ((current is not None) != (intended is not None)
                        or (managed_sha256(current) if current is not None else None)
                        != intended_identity):
                    continue
                if existed:
                    managed_atomic_write(
                        destination, data, mode,
                        expected_old=(current is not None,
                                      managed_sha256(current) if current is not None else None))
                else:
                    destination.unlink(missing_ok=True)
            except (OSError, ManagedRuntimeError):
                pass
        if isinstance(exc, ManagedWriteCollision) and write_set is not None:
            row = next(item for item in write_set["destinations"]
                       if (controller.resolve() / item["path"]).resolve() == exc.path.resolve())
            expected_bytes = row["_expected_bytes"]
            preserved = []
            if expected_bytes is not None:
                preserved.append(managed_preserve_collision_bytes(
                    controller.resolve(), expected_bytes))
            if exc.observed is not None:
                observed_ref = managed_preserve_collision_bytes(
                    controller.resolve(), exc.observed)
                if observed_ref not in preserved:
                    preserved.append(observed_ref)
            receipt.update({"outcome": "collision", "collision": {
                "schema_version": MANAGED_COLLISION_SCHEMA,
                "write_set_sha256": write_set["write_set_sha256"],
                "destinations": [{"path": row["path"],
                                  "expected_exists": exc.expected_exists,
                                  "expected_old_sha256": exc.expected_sha256,
                                  "observed_sha256": (managed_sha256(exc.observed)
                                                      if exc.observed is not None else None),
                                  "preserved_byte_sets": preserved}]}})
        else:
            receipt["outcome"] = "failed"
        receipt.update({"error": str(exc),
                        "termination": "interrupted" if isinstance(exc, KeyboardInterrupt)
                        else "failure"})
    finally:
        finish = time.time(); duration = time.monotonic() - started_mono
        log.write(f"finish outcome={receipt['outcome']} duration_seconds={duration:.6f}\n")
        log.flush(); os.fsync(log.fileno()); log.close()
        log_data = log_path.read_bytes()
        receipt.setdefault("termination", "success")
        receipt.update({"start_time": started, "finish_time": finish,
                        "duration_seconds": duration, "exit_code": 0 if receipt["outcome"] == "completed" else 2,
                        "signal": None, "timed_out": False,
                        "log": {"path": str(log_path), "sha256": managed_sha256(log_data)}})
        reference = managed_receipt_write(receipt_path, receipt)
    result = {**receipt, "receipt": reference}
    if receipt["outcome"] != "completed":
        raise ManagedRuntimeError(receipt.get("error", "managed runtime refresh failed"), reference)
    return result


def managed_runtime_inspect(controller: Path, repository: Path, target_sha: str) -> dict[str, Any]:
    controller = controller.resolve(); repository = repository.resolve()
    target_sha = managed_exact_commit(repository, target_sha, "doctor target generation")
    findings = []
    generation_path = managed_safe_path(controller, MANAGED_GENERATION_PATH)
    generation = None
    try:
        generation = json.loads(generation_path.read_text())
    except (OSError, json.JSONDecodeError):
        findings.append({"code": "managed_generation_receipt_missing_or_invalid", "path": MANAGED_GENERATION_PATH})
    policy_path = managed_safe_path(controller, MANAGED_POLICY_PATH)
    policy_hash = managed_sha256(policy_path.read_bytes()) if policy_path.is_file() else None
    target_assets = managed_script_assets(repository, target_sha)
    expected_paths = sorted(target_assets)
    generation_scripts = generation.get("scripts") if isinstance(generation, dict) else None
    identity_valid = (isinstance(generation, dict)
                      and generation.get("schema_version") == MANAGED_RUNTIME_SCHEMA
                      and generation.get("target_sha") == target_sha
                      and generation.get("package_version") == managed_package_version(repository, target_sha)
                      and generation.get("policy_sha256") == policy_hash
                      and isinstance(generation_scripts, dict)
                      and set(generation_scripts) == set(expected_paths))
    scripts: dict[str, dict[str, Any]] = {}
    for relative in expected_paths:
        source_hash = managed_sha256(managed_script_source_bytes(
            repository, target_sha, target_assets, relative))
        destination = managed_safe_path(controller, relative)
        actual_hash = managed_sha256(destination.read_bytes()) if destination.is_file() else None
        entry = generation_scripts.get(relative) if isinstance(generation_scripts, dict) else None
        entry_valid = (isinstance(entry, dict)
                       and set(entry) == {"classification", "source_sha256", "actual_sha256"}
                       and entry.get("classification") in {"exact", "preserved_customization"}
                       and entry.get("source_sha256") == source_hash
                       and isinstance(entry.get("actual_sha256"), str)
                       and bool(re.fullmatch(r"[0-9a-f]{64}", entry["actual_sha256"])))
        if entry_valid and entry["classification"] == "exact" and entry["actual_sha256"] != source_hash:
            entry_valid = False
        if (entry_valid and entry["classification"] == "preserved_customization"
                and entry["actual_sha256"] == source_hash):
            entry_valid = False
        identity_valid = identity_valid and entry_valid
        classification = entry["classification"] if entry_valid else "unbound"
        bound_actual = entry["actual_sha256"] if entry_valid else source_hash
        scripts[relative] = {"classification": classification, "source_sha256": source_hash,
                             "actual_sha256": actual_hash, "bound_actual_sha256": bound_actual}
        if actual_hash != bound_actual:
            if classification == "preserved_customization":
                code = "managed_preserved_customization_drift"
            else:
                code = "managed_runtime_missing" if actual_hash is None else "managed_runtime_drift"
            findings.append({"code": code, "path": relative, "expected_sha256": bound_actual,
                             "source_sha256": source_hash, "actual_sha256": actual_hash,
                             "classification": classification})
    if not identity_valid:
        findings.append({"code": "managed_generation_identity_drift", "path": MANAGED_GENERATION_PATH})
    findings.extend(managed_version_cache_findings(controller, "controller"))
    owner = registered_owner(repository)
    if owner:
        findings.extend(managed_version_cache_findings(Path(owner), "integration-owner"))
    return {"schema_version": MANAGED_RUNTIME_SCHEMA, "operation": "doctor", "target_sha": target_sha,
            "package_version": managed_package_version(repository, target_sha),
            "policy_sha256": policy_hash, "scripts": scripts,
            "findings": findings, "healthy": not findings}


class IntegrationError(RuntimeError):
    pass


@contextmanager
def integration_target_lock(repository: Path, target_ref: str):
    """Serialize integration-owner maintenance; Git CAS serializes delivery itself."""
    common = Path(git(repository, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    key = hashlib.sha256(f"{common.resolve()}\0{target_ref}".encode()).hexdigest()
    lock = common / "juno-locks/integration-maintenance" / f"{key}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise IntegrationError("another integration-maintenance worker owns this target") from exc
            raise
        yield


def run(argv: list[str], cwd: Path, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, cwd=cwd, text=True, capture_output=True,
                            stdin=subprocess.DEVNULL,
                            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    if check and result.returncode:
        raise IntegrationError(result.stderr.strip() or result.stdout.strip()
                               or f"command failed: {argv!r}")
    return result


def git(root: Path, *args: str, check: bool = True) -> str:
    return run(["git", "-C", str(root), *args], root, check=check).stdout.strip()


def exact_root(path: Path, label: str) -> Path:
    candidate = path.expanduser().resolve()
    top = git(candidate, "rev-parse", "--show-toplevel", check=False)
    if not top or Path(top).resolve() != candidate:
        raise IntegrationError(f"{label} is not an exact Git worktree: {candidate}")
    return candidate


def load_policy(controller: Path) -> tuple[dict[str, Any], dict[str, Any], Path]:
    policy_path = controller / ".juno_task/config/integration-workspace.json"
    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrationError(f"invalid integration workspace policy: {exc}") from exc
    required = {"schema_version", "remote", "owner_role_authority", "receipt_root"}
    if (not isinstance(policy, dict) or set(policy) != required
            or policy.get("schema_version") != POLICY_SCHEMA):
        raise IntegrationError(f"integration policy must contain exactly the {POLICY_SCHEMA} fields")
    if (not isinstance(policy["remote"], str) or not policy["remote"]
            or policy["owner_role_authority"] != AUTHORITY):
        raise IntegrationError("integration policy remote or role authority is invalid")
    receipt_root = Path(policy["receipt_root"])
    if receipt_root.is_absolute() or ".." in receipt_root.parts:
        raise IntegrationError("integration receipt_root must stay inside the controller")
    task_policy = task_workspace.load_config(controller)
    return policy, task_policy, policy_path


def parse_worktrees(repository: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    row: dict[str, Any] = {}
    for line in [*git(repository, "worktree", "list", "--porcelain").splitlines(), ""]:
        if not line:
            if row:
                rows.append(row); row = {}
            continue
        key, _, value = line.partition(" ")
        row[key] = value or True
    return rows


def worktree_config(root: Path, key: str) -> str | None:
    value = git(root, "config", "--worktree", "--get", key, check=False)
    return value or None


def advance_owner_role_base(owner: Path, before: str | None, after: str) -> dict[str, Any]:
    """Advance only the protected owner's exact worktree-local authority baseline."""
    if not SHA_RE.fullmatch(after) or sha(owner, after) != after:
        raise IntegrationError("integration owner roleBase target is not an exact commit")
    observed = worktree_config(owner, "juno.workspace.roleBase")
    if observed != before:
        raise IntegrationError("integration owner roleBase changed under lock")
    if worktree_config(owner, "juno.workspace.role") != "integration-owner":
        raise IntegrationError("integration owner role is not registered")
    if worktree_config(owner, "juno.workspace.roleAuthority") != AUTHORITY:
        raise IntegrationError("integration owner does not carry protected authority")
    git(owner, "config", "--worktree", "juno.workspace.roleBase", after)
    if worktree_config(owner, "juno.workspace.roleBase") != after:
        raise IntegrationError("integration owner roleBase readback failed")
    return {"kind": "advance_role_base", "path": str(owner),
            "before": before, "after": after}


def owner_candidates(repository: Path) -> list[dict[str, Any]]:
    candidates = []
    for row in parse_worktrees(repository):
        raw = row.get("worktree")
        if not isinstance(raw, str) or row.get("prunable") is True:
            continue
        root = Path(raw).resolve()
        if not root.is_dir():
            continue
        if worktree_config(root, "juno.workspace.role") == "integration-owner":
            candidates.append({"path": str(root), "authority": worktree_config(
                root, "juno.workspace.roleAuthority")})
    return sorted(candidates, key=lambda item: item["path"])


def registered_owner(repository: Path) -> str | None:
    value = git(repository, "config", "--local", "--get", OWNER_CONFIG, check=False)
    return str(Path(value).expanduser().resolve()) if value else None


def sha(repository: Path, ref: str) -> str | None:
    value = git(repository, "rev-parse", "--verify", f"{ref}^{{commit}}", check=False)
    return value if SHA_RE.fullmatch(value) else None


def relation(repository: Path, left: str | None, right: str | None) -> dict[str, int | None]:
    if not left or not right:
        return {"ahead": None, "behind": None}
    value = git(repository, "rev-list", "--left-right", "--count", f"{left}...{right}", check=False)
    match = re.fullmatch(r"(\d+)\s+(\d+)", value)
    return ({"ahead": int(match.group(1)), "behind": int(match.group(2))}
            if match else {"ahead": None, "behind": None})


def full_checkout(root: Path) -> tuple[bool, list[str]]:
    reasons = []
    if worktree_config(root, "core.sparseCheckout") == "true":
        reasons.append("sparse_checkout_enabled")
    if any(line.startswith("S ") for line in git(root, "ls-files", "-t", check=False).splitlines()):
        reasons.append("skip_worktree_paths_present")
    return not reasons, reasons


def submodule_state(owner: Path) -> list[dict[str, str]]:
    # The leading byte is semantic (` ` exact, `-`, `+`, or `U`), so do not use
    # the normalized git() helper which intentionally strips surrounding space.
    value = run(["git", "-C", str(owner), "submodule", "status", "--recursive"],
                owner, check=False).stdout.rstrip("\r\n")
    rows = []
    for line in value.splitlines():
        if not line:
            continue
        marker = line[0]
        parts = line[1:].strip().split()
        rows.append({"path": parts[1] if len(parts) > 1 else "",
                     "sha": parts[0] if parts else "",
                     "state": {"-": "uninitialized", "+": "wrong_gitlink",
                               "U": "conflict"}.get(marker, "exact")})
    return rows


def commit_gitlinks(repository: Path, commit: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in git(repository, "ls-tree", "-r", commit, check=False).splitlines():
        metadata, separator, path = line.partition("\t")
        fields = metadata.split()
        if separator and len(fields) == 3 and fields[:2] == ["160000", "commit"]:
            rows.append({"path": path, "sha": fields[2]})
    return sorted(rows, key=lambda row: row["path"])


def local_target_gitlink_evidence(owner: Path, repository: Path, old_sha: str,
                                  target_sha: str) -> dict[str, Any]:
    """Prove a topology-preserving recursive gitlink transition without network I/O."""
    def collect(repo: Path, commit: str, checkout: Path,
                prefix: str = "") -> tuple[list[dict[str, str]], bool]:
        flattened: list[dict[str, str]] = []
        available = True
        for row in commit_gitlinks(repo, commit):
            full_path = f"{prefix}/{row['path']}" if prefix else row["path"]
            child = checkout / row["path"]
            exact = bool(child.is_dir() and sha(child, row["sha"]) == row["sha"])
            flattened.append({"path": full_path, "sha": row["sha"]})
            available = available and exact
            if exact:
                nested, nested_available = collect(child, row["sha"], child, full_path)
                flattened.extend(nested)
                available = available and nested_available
        return sorted(flattened, key=lambda item: item["path"]), available

    old_links, old_available = collect(repository, old_sha, owner)
    target_links, target_available = collect(repository, target_sha, owner)
    old_paths = [row["path"] for row in old_links]
    target_paths = [row["path"] for row in target_links]
    target_by_path = {row["path"]: row["sha"] for row in target_links}
    evidence = [{"path": path, "sha": target_by_path[path],
                 "local_object_available": target_available}
                for path in target_paths]
    return {"topology_preserved": old_paths == target_paths,
            "old": old_links, "target": target_links,
            "old_objects_available": old_available,
            "local_objects_available": target_available,
            "gitlinks": evidence,
            "hydration": "submodule_update_recursive_checkout_no_fetch"}


def target_holders(repository: Path, target_ref: str) -> list[str]:
    return sorted(str(row["worktree"]) for row in parse_worktrees(repository)
                  if row.get("branch") == target_ref)


def status_payload(controller: Path, *, fetch: bool = False) -> dict[str, Any]:
    controller = exact_root(controller, "controller")
    policy, task_policy, policy_path = load_policy(controller)
    repository = task_workspace.product_repository(controller, task_policy)
    target_ref = task_policy["target_ref"]
    remote_ref = f"refs/remotes/{policy['remote']}/{target_ref.removeprefix('refs/heads/')}"
    if fetch:
        run(["git", "-C", str(repository), "fetch", "--no-tags", policy["remote"],
             f"+{target_ref}:{remote_ref}"], repository)
    target_sha = sha(repository, target_ref)
    remote_sha = sha(repository, remote_ref)
    candidates = owner_candidates(repository)
    registered = registered_owner(repository)
    selected = [item for item in candidates if item["path"] == registered]
    owner: dict[str, Any] | None = None
    findings: list[dict[str, str]] = []
    if registered and len(selected) != 1:
        findings.append({"code": "integration_owner_registration_invalid", "severity": "error",
                         "message": "registered integration owner is missing or not protected"})
    elif not registered and len(candidates) != 1:
        findings.append({"code": "integration_owner_missing" if not candidates else "integration_owner_multiple",
                         "severity": "error", "message": f"found {len(candidates)} integration owners"})
    else:
        candidate = selected[0] if selected else candidates[0]
        extras = [item for item in candidates if item["path"] != candidate["path"]]
        if not registered:
            findings.append({"code": "integration_owner_registration_missing", "severity": "warning",
                             "message": "unique protected owner is not explicitly registered"})
        if extras:
            findings.append({"code": "integration_owner_extra", "severity": "warning",
                             "message": f"found {len(extras)} non-canonical protected owner(s)"})
        root = Path(candidate["path"])
        full, reasons = full_checkout(root)
        owner = {**candidate, "head": sha(root, "HEAD"),
                 "role_base": worktree_config(root, "juno.workspace.roleBase"),
                 "detached": git(root, "symbolic-ref", "-q", "HEAD", check=False) == "",
                 "clean": git(root, "status", "--porcelain=v1", "--untracked-files=all") == "",
                 "full_checkout": full, "full_checkout_reasons": reasons,
                 "submodules": submodule_state(root)}
        findings.extend(managed_version_cache_findings(root, "integration-owner"))
        if candidate["authority"] != policy["owner_role_authority"]:
            findings.append({"code": "integration_owner_wrong_authority", "severity": "error",
                             "message": "integration owner authority is not protected"})
        for key, code in (("detached", "integration_owner_attached"),
                          ("clean", "integration_owner_dirty"),
                          ("full_checkout", "integration_owner_sparse")):
            if not owner[key]:
                findings.append({"code": code, "severity": "error", "message": code.replace("_", " ")})
        if owner["head"] != target_sha:
            findings.append({"code": "integration_owner_stale", "severity": "warning",
                             "message": "integration owner HEAD differs from target"})
        role_base = owner["role_base"]
        if not role_base or not sha(repository, role_base):
            findings.append({"code": "integration_owner_role_base_invalid", "severity": "error",
                             "message": "integration owner roleBase is missing or invalid"})
        elif role_base != target_sha:
            severity = ("warning" if target_sha and run([
                "git", "-C", str(repository), "merge-base", "--is-ancestor",
                role_base, target_sha], repository, check=False).returncode == 0 else "error")
            findings.append({"code": "integration_owner_role_base_stale" if severity == "warning"
                             else "integration_owner_role_base_diverged", "severity": severity,
                             "message": "integration owner roleBase differs from target"})
    holders = target_holders(repository, target_ref)
    if holders:
        findings.append({"code": "target_checked_out", "severity": "error",
                         "message": f"target ref is attached in {len(holders)} worktree(s)"})
    rel = relation(repository, target_sha, remote_sha)
    if rel["ahead"] and rel["behind"]:
        findings.append({"code": "remote_diverged", "severity": "error",
                         "message": "local target and cached remote diverged"})
    return {"schema_version": SCHEMA, "operation": "status", "offline": not fetch,
            "controller": str(controller), "repository": str(repository),
            "policy": str(policy_path), "target": {"ref": target_ref, "sha": target_sha,
            "holders": holders}, "remote": {"name": policy["remote"], "ref": remote_ref,
            "sha": remote_sha, **rel}, "integration": {"status": "registered" if registered and owner
            else "unique" if len(candidates) == 1 else "missing" if not candidates else "multiple",
            "registered_path": registered, "candidates": candidates, "owner": owner},
            "findings": findings, "healthy": not any(row["severity"] == "error" for row in findings),
            "ready": bool(owner and owner["head"] == target_sha
                          and owner["role_base"] == target_sha
                          and all(row["state"] == "exact" for row in owner["submodules"])
                          and not any(row["severity"] == "error" for row in findings))}


def write_receipt(path: Path, value: dict[str, Any]) -> dict[str, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(data).hexdigest()}


def json_digest(value: Any) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()


def operation_receipt_path(controller: Path, policy: dict[str, Any], operation: str) -> Path:
    return (controller / policy["receipt_root"] /
            f"{time.time_ns()}-{os.getpid()}-{operation}.json").resolve()


def worktree_identity(root: Path) -> dict[str, Any]:
    full, reasons = full_checkout(root)
    return {"path": str(root.resolve()), "head": sha(root, "HEAD"),
            "clean": git(root, "status", "--porcelain=v1", "--untracked-files=all") == "",
            "detached": git(root, "symbolic-ref", "-q", "HEAD", check=False) == "",
            "full_checkout": full, "full_checkout_reasons": reasons,
            "role": worktree_config(root, "juno.workspace.role"),
            "authority": worktree_config(root, "juno.workspace.roleAuthority")}


def stale_owner_cache_migration(status: dict[str, Any], repository: Path,
                                policy: dict[str, Any]) -> dict[str, Any] | None:
    owner = status["integration"]["owner"]
    target_sha = status["target"]["sha"]
    if not owner or not target_sha or owner["head"] == target_sha:
        return None
    old_sha = owner["head"]
    finding_codes = sorted(row["code"] for row in status["findings"])
    allowed_codes = sorted([
        "legacy_checkout_local_version_cache_writer",
        "tracked_worktree_version_cache",
        "integration_owner_stale",
        "integration_owner_role_base_stale",
    ])
    old_evidence = version_cache_commit_evidence(repository, old_sha)
    target_evidence = version_cache_commit_evidence(repository, target_sha)
    gitlinks = local_target_gitlink_evidence(
        Path(owner["path"]), repository, old_sha, target_sha)
    ancestry = run(["git", "-C", str(repository), "merge-base", "--is-ancestor",
                    old_sha, target_sha], repository, check=False).returncode == 0
    checks = {
        "registered_unique_protected_owner": bool(
            status["integration"]["status"] == "registered"
            and status["integration"]["registered_path"] == owner["path"]
            and len(status["integration"]["candidates"]) == 1),
        "owner_clean_detached_full": bool(
            owner["clean"] and owner["detached"] and owner["full_checkout"]),
        "owner_head_role_base_fast_forward": bool(
            owner["role_base"] == old_sha and ancestry),
        "owner_authority_exact": owner["authority"] == policy["owner_role_authority"],
        "only_legacy_cache_findings": finding_codes == allowed_codes,
        "old_findings_present": bool(old_evidence["legacy_writer"]["present"]
                                     and old_evidence["tracked_cache"]["present"]),
        "target_findings_absent": bool(not target_evidence["legacy_writer"]["present"]
                                       and not target_evidence["tracked_cache"]["present"]),
        "submodule_topology_preserved": gitlinks["topology_preserved"],
        "target_gitlinks_locally_available": gitlinks["local_objects_available"],
    }
    refusals = sorted(key for key, passed in checks.items() if not passed)
    eligible = not refusals
    return {
        "disposition": "stale_owner_legacy_cache_migration.v1",
        "eligible": eligible,
        "old": old_evidence,
        "target": target_evidence,
        "owner": {"path": owner["path"], "head": old_sha,
                  "role_base": owner["role_base"], "role": "integration-owner",
                  "authority": owner["authority"], "clean": owner["clean"],
                  "detached": owner["detached"], "full_checkout": owner["full_checkout"]},
        "finding_codes": finding_codes, "allowed_finding_codes": allowed_codes,
        "checks": checks, "refusals": refusals,
        "ancestry": "fast_forward" if ancestry else "diverged",
        "submodules": gitlinks,
        "expected_final": {"head": target_sha, "tree": target_evidence["tree"],
                           "role_base": target_sha, "role": "integration-owner",
                           "authority": policy["owner_role_authority"],
                           "clean": True, "detached": True, "full_checkout": True,
                           "gitlinks": gitlinks["target"]},
    }


def refresh_owner_inventory(controller: Path, repository: Path) -> dict[str, Any]:
    """Read-only, exact registration/evidence inventory; no owner is disposable."""
    owners = []
    for candidate in owner_candidates(repository):
        root = exact_root(Path(candidate["path"]), "retained owner")
        config = Path(git(root, "rev-parse", "--path-format=absolute", "--git-path", "config.worktree"))
        evidence = []
        # Retained runtime receipts are evidence even when ignored by Git.
        evidence_root = root / ".juno_task/runtime"
        if evidence_root.exists():
            for path in sorted(evidence_root.rglob("*")):
                if path.is_symlink():
                    raise IntegrationError("owner runtime evidence contains a symlink")
                if path.is_file():
                    evidence.append({"path": str(path.relative_to(root)),
                                     "sha256": managed_sha256(path.read_bytes())})
        if str(root) != registered_owner(repository):
            # Historical adoption directories retain sibling receipts and artifacts.
            # Never traverse other worktrees or treat these bytes as cleanup inputs.
            for path in sorted(root.parent.iterdir()):
                if path.is_symlink():
                    raise IntegrationError("historical owner evidence contains a symlink")
                if path.is_file():
                    evidence.append({"path": str(path), "sha256": managed_sha256(path.read_bytes())})
        locks = [str(path) for path in config.parent.glob("*.lock")]
        owners.append({**worktree_identity(root), "locks": locks,
                       "stable_registration": sorted(entry for entry in git(
                           root, "config", "--worktree", "--null", "--list").split("\0")
                           if not entry.startswith("juno.workspace.rolebase\n")),
                       "role_base": worktree_config(root, "juno.workspace.roleBase"),
                       "authority_unambiguous": all(len(git(root, "config", "--worktree", "--get-all", key,
                                                          check=False).splitlines()) == 1
                           for key in ("juno.workspace.role", "juno.workspace.roleAuthority", "juno.workspace.roleBase")),
                       "submodules": submodule_state(root),
                       "registration_sha256": managed_sha256(config.read_bytes()),
                       "evidence": evidence})
    state = task_workspace.read_state(controller)
    producers = sorted(task_id for task_id, record in state["tasks"].items()
                       if record.get("fencing", {}).get("state") == "ACTIVE"
                       or record.get("state") in {"WORKING", "HYDRATING", "HYDRATION_FAILED"})
    owner_processes = []
    if not Path("/proc/self/cwd").exists():
        owner_processes.append({"status": "unknown", "reason": "owner process observation unavailable"})
    else:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cwd = (entry / "cwd").resolve(strict=True)
                if any(cwd == Path(row["path"]) or Path(row["path"]) in cwd.parents for row in owners):
                    owner_processes.append({"pid": entry.name, "cwd": str(cwd),
                                            "stat": (entry / "stat").read_text()})
            except (OSError, RuntimeError):
                continue  # vanished or inaccessible process; registered leases remain authoritative
    return {"registered_owner": registered_owner(repository), "owners": owners,
            "owner_processes": sorted(owner_processes, key=lambda row: str(row)),
            "worktrees": parse_worktrees(repository),
            "task_state_sha256": json_digest(state), "active_producers": producers,
            "task_policy_sha256": managed_sha256((controller / ".juno_task/config/task-workspace.json").read_bytes()),
            "refs": git(repository, "for-each-ref", "--format=%(refname) %(objectname)"),
            "repository_config_sha256": managed_sha256(Path(git(
                repository, "rev-parse", "--path-format=absolute", "--git-path", "config")).read_bytes())}


def assert_refresh_preserved(controller: Path, repository: Path,
                             before: dict[str, Any], owner: Path,
                             review: dict[str, Any]) -> dict[str, Any]:
    if managed_sha256(Path(review["path"]).read_bytes()) != review["sha256"]:
        raise IntegrationError("preserve-only approval evidence changed during apply")
    observed = refresh_owner_inventory(controller, repository)
    def stable(value: dict[str, Any]) -> dict[str, Any]:
        result = dict(value)
        result["owners"] = [({key: value for key, value in row.items()
                             if key not in {"head", "role_base", "submodules", "registration_sha256", "clean"}}
                             if row["path"] == str(owner) else row)
                            for row in value["owners"]]
        result["worktrees"] = [{key: value for key, value in row.items()
                                if key != "HEAD" or row.get("worktree") != str(owner)}
                               for row in value["worktrees"]]
        return result
    if stable(observed) != stable(before) or observed["active_producers"] or observed["owner_processes"]:
        raise IntegrationError("owner refresh preservation readback mismatch")
    return observed


def canonical_owner_refresh(status: dict[str, Any], repository: Path,
                            policy: dict[str, Any], controller: Path,
                            preserve_owners: Path | None) -> dict[str, Any]:
    inventory = refresh_owner_inventory(controller, repository)
    inventory_hash = json_digest(inventory)
    review = None
    if preserve_owners is not None:
        review_path = preserve_owners.expanduser().resolve()
        data = review_path.read_bytes()
        review = {"path": str(review_path), "sha256": managed_sha256(data),
                  "approval": json.loads(data)}
    approval = review["approval"] if review else {}
    owner = status["integration"]["owner"] or {}
    old, target = owner.get("head"), status["target"]["sha"]
    gitlinks = (local_target_gitlink_evidence(Path(owner["path"]), repository, old, target)
                if old and target else {})
    allowed = {"integration_owner_stale", "integration_owner_role_base_stale",
               "integration_owner_extra"}
    checks = {
        "explicit_preserve_only_review": bool(isinstance(approval, dict)
            and set(approval) == {"approved_by", "disposition", "inventory_sha256"}
            and isinstance(approval.get("approved_by"), str) and approval["approved_by"].strip()
            and approval.get("disposition") == "preserve-only"
            and approval.get("inventory_sha256") == inventory_hash),
        "canonical_registration_exact": bool(owner and inventory["registered_owner"] == owner["path"]
            and status["integration"]["status"] == "registered"
            and len(git(repository, "config", "--local", "--get-all", OWNER_CONFIG,
                        check=False).splitlines()) == 1),
        "stale_fast_forward": bool(old and target and old != target
            and owner.get("role_base") == old
            and run(["git", "merge-base", "--is-ancestor", old, target], repository,
                    check=False).returncode == 0),
        "nonlegacy_findings_only": all(row["code"] in allowed for row in status["findings"]),
        "no_active_producers": not inventory["active_producers"] and not inventory["owner_processes"],
        "nonlegacy_source_and_target": bool(old and target and all(
            not evidence["legacy_writer"]["present"] and not evidence["tracked_cache"]["present"]
            for evidence in (version_cache_commit_evidence(repository, old),
                             version_cache_commit_evidence(repository, target)))),
        "all_owners_safe": bool(inventory["owners"]) and all(
            row["clean"] and row["detached"] and row["full_checkout"] and not row["locks"]
            and row["authority_unambiguous"]
            and row["authority"] == policy["owner_role_authority"]
            and row["head"] == row["role_base"]
            and all(link["state"] == "exact" for link in row["submodules"])
            and target and run(["git", "merge-base", "--is-ancestor", row["head"], target],
                                  repository, check=False).returncode == 0
            for row in inventory["owners"]),
        "local_gitlink_transition": bool(gitlinks.get("topology_preserved")
            and gitlinks.get("old_objects_available") and gitlinks.get("local_objects_available")),
        "target_unattached": not status["target"]["holders"],
    }
    refusals = sorted(key for key, passed in checks.items() if not passed)
    return {"disposition": "canonical_owner_refresh.v1", "eligible": not refusals,
            "checks": checks, "refusals": refusals, "inventory": inventory,
            "inventory_sha256": inventory_hash, "preserve_owners": review,
            "submodules": gitlinks,
            "expected_final": {"head": target, "tree": commit_tree(repository, target) if target else None,
                "role_base": target, "role": "integration-owner",
                "authority": policy["owner_role_authority"], "clean": True, "detached": True,
                "full_checkout": True, "gitlinks": gitlinks.get("target", [])}}


def repair_plan(controller: Path, *, canonical_refresh: bool = False,
                preserve_owners: Path | None = None) -> dict[str, Any]:
    controller = exact_root(controller, "controller")
    policy, task_policy, policy_path = load_policy(controller)
    repository = task_workspace.product_repository(controller, task_policy)
    status = status_payload(controller)
    owner = status["integration"]["owner"]
    actions: list[dict[str, Any]] = []
    blockers: list[str] = []
    target_sha = status["target"]["sha"]
    owner_path = owner["path"] if owner else None
    migration = (canonical_owner_refresh(status, repository, policy, controller, preserve_owners)
                 if canonical_refresh else stale_owner_cache_migration(status, repository, policy))
    migration_eligible = bool(migration and migration["eligible"])
    if canonical_refresh:
        blockers.extend(f"canonical_owner_refresh:{reason}" for reason in migration["refusals"])
    elif migration and not migration_eligible:
        blockers.extend(code for code in migration["finding_codes"]
                        if code not in migration["allowed_finding_codes"])
        blockers.extend(f"stale_owner_migration:{reason}"
                        for reason in migration["refusals"])
    attached_owner_repairable = bool(
        owner and not owner["detached"] and owner["clean"] and owner["full_checkout"]
        and owner["head"] == target_sha and owner["role_base"] == target_sha
        and status["target"]["holders"] == [owner_path]
    )
    if not owner:
        blockers.append("canonical_integration_owner_unavailable")
    else:
        if (not owner["clean"] or not owner["full_checkout"]
                or (not owner["detached"] and not attached_owner_repairable)):
            blockers.append("canonical_integration_owner_not_safe")
        if owner["head"] != target_sha:
            actions.append({"kind": "refresh_owner", "path": owner["path"],
                            "before": owner["head"], "after": target_sha})
        if owner["role_base"] != target_sha:
            actions.append({"kind": "advance_role_base", "path": owner["path"],
                            "before": owner["role_base"], "after": target_sha})
        if any(row["state"] != "exact" for row in owner["submodules"]):
            actions.append({"kind": "refresh_submodules", "path": owner["path"],
                            "target": target_sha})
    rows = {str(row.get("worktree")): row for row in parse_worktrees(repository)}
    legacy_owner = git(repository, "config", "--local", "--get",
                       LEGACY_OWNER_CONFIG, check=False)
    if legacy_owner and not canonical_refresh:
        legacy_path = str(Path(legacy_owner).expanduser().resolve())
        legacy_row = rows.get(legacy_path)
        if legacy_row is None or legacy_row.get("prunable") is True:
            actions.append({"kind": "clear_legacy_integration_registration",
                            "repository": str(repository), "key": LEGACY_OWNER_CONFIG,
                            "before": legacy_path})
    for holder in status["target"]["holders"]:
        root = Path(holder)
        identity = worktree_identity(root)
        row = rows.get(holder, {})
        if holder != owner_path or len(status["target"]["holders"]) != 1:
            blockers.append(f"extra_target_holder:{holder}")
        elif (not identity["clean"] or identity["head"] != target_sha
                or identity["role"] != "integration-owner"
                or identity["authority"] != policy["owner_role_authority"]):
            blockers.append(f"unsafe_target_holder:{holder}")
        else:
            actions.append({"kind": "detach_target_holder", "path": holder,
                            "branch": row.get("branch"), "head": identity["head"],
                            "role": identity["role"], "authority": identity["authority"]})
    ignored = {"target_checked_out", "integration_owner_stale",
               "integration_owner_role_base_stale"}
    if migration_eligible:
        ignored.update({"legacy_checkout_local_version_cache_writer",
                        "tracked_worktree_version_cache"})
    if attached_owner_repairable:
        ignored.add("integration_owner_attached")
    blockers.extend(row["code"] for row in status["findings"]
                    if row["severity"] == "error" and row["code"] not in ignored)
    common = Path(git(repository, "rev-parse", "--path-format=absolute",
                      "--git-common-dir")).resolve()
    core = {"schema_version": SCHEMA, "operation": "repair", "controller": str(controller),
            "repository": str(repository), "git_common_dir": str(common),
            "policy": str(policy_path), "policy_sha256": hashlib.sha256(
                policy_path.read_bytes()).hexdigest(), "target": status["target"],
            "registered_owner": status["integration"]["registered_path"],
            "owner": owner, "migration": migration,
            "actions": actions, "blockers": sorted(set(blockers))}
    return {**core, "plan_sha256": json_digest(core)}


def repair(controller: Path, *, dry_run: bool, apply: Path | None,
           canonical_refresh: bool = False,
           preserve_owners: Path | None = None) -> tuple[dict[str, Any], int]:
    controller = exact_root(controller, "controller")
    policy, task_policy, _ = load_policy(controller)
    repository = task_workspace.product_repository(controller, task_policy)
    if preserve_owners and not canonical_refresh:
        raise IntegrationError("--preserve-owners requires --canonical-owner-refresh")
    if dry_run:
        plan = repair_plan(controller, canonical_refresh=canonical_refresh, preserve_owners=preserve_owners)
        receipt = {**plan, "outcome": "planned" if not plan["blockers"] else "refused"}
        if canonical_refresh:
            receipt_path = controller / policy["receipt_root"] / f"{json_digest(receipt)}-owner-refresh-plan.json"
            if receipt_path.exists():
                if json.loads(receipt_path.read_bytes()) != receipt:
                    raise IntegrationError("immutable owner refresh plan collision")
                reference = {"path": str(receipt_path), "sha256": managed_sha256(receipt_path.read_bytes())}
            else:
                reference = managed_receipt_write(receipt_path, receipt)
        else:
            reference = write_receipt(operation_receipt_path(controller, policy, "repair-plan"), receipt)
        return {**receipt, "receipt": reference}, 0 if not plan["blockers"] else 2
    if apply is None:
        raise IntegrationError("repair apply requires a plan receipt")
    try:
        approved = json.loads(apply.expanduser().resolve().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrationError(f"invalid repair plan receipt: {exc}") from exc
    approved_migration = approved.get("migration") or {}
    approved_refresh = approved_migration.get("disposition") == "canonical_owner_refresh.v1"
    if canonical_refresh and not approved_refresh:
        raise IntegrationError("requested owner refresh does not match the approved plan")
    canonical_refresh = approved_refresh
    if canonical_refresh:
        expected_path = (controller / policy["receipt_root"] /
                         f"{json_digest(approved)}-owner-refresh-plan.json").resolve()
        if apply.resolve() != expected_path:
            raise IntegrationError("owner refresh requires the immutable controller plan receipt")
        review = approved_migration.get("preserve_owners")
        if preserve_owners and (not review or preserve_owners.resolve() != Path(review["path"])):
            raise IntegrationError("preserve-only review does not match the approved plan")
        preserve_owners = Path(review["path"]) if review else None
    current = repair_plan(controller, canonical_refresh=canonical_refresh, preserve_owners=preserve_owners)
    approved_core = {key: value for key, value in approved.items()
                     if key not in {"plan_sha256", "outcome"}}
    if (approved.get("operation") != "repair" or approved.get("blockers")
            or approved.get("plan_sha256") != json_digest(approved_core)):
        error = "repair plan was not eligible"
        failed = {"schema_version": SCHEMA, "operation": "repair",
                  "outcome": "failed", "error": error}
        reference = write_receipt(operation_receipt_path(
            controller, policy, "repair-refused"), failed)
        return {**failed, "receipt": reference}, 2
    if approved.get("plan_sha256") != current["plan_sha256"]:
        error = "repair plan identity drifted; generate a new dry-run receipt"
        failed = {"schema_version": SCHEMA, "operation": "repair",
                  "outcome": "failed", "error": error,
                  "approved_plan_sha256": approved.get("plan_sha256"),
                  "current_plan_sha256": current["plan_sha256"]}
        reference = write_receipt(operation_receipt_path(
            controller, policy, "repair-drift"), failed)
        return {**failed, "receipt": reference}, 2
    result_path = operation_receipt_path(controller, policy, "repair-apply")
    result = {**current, "outcome": "running", "phases": []}
    reference = write_receipt(result_path, result)
    try:
        with integration_target_lock(repository, task_policy["target_ref"]):
            locked = repair_plan(controller, canonical_refresh=canonical_refresh, preserve_owners=preserve_owners)
            if locked["plan_sha256"] != current["plan_sha256"]:
                raise IntegrationError("repair plan identity drifted while acquiring target lock")
            migration = locked.get("migration")
            migration_eligible = bool(migration and migration.get("eligible"))
            deferred_role_base: dict[str, Any] | None = None
            if canonical_refresh:
                # Exclusive, durable claim survives interruption and prevents replay even
                # if an operator later restores the original checkout.
                claim = controller / policy["receipt_root"] / f"{locked['plan_sha256']}-consumed.json"
                managed_receipt_write(claim, {"plan": str(apply.resolve()), "result": str(result_path),
                    "rollback": {"automatic": False, "requires_separate_owner_authority": True,
                        "before": migration["inventory"], "target": locked["target"],
                        "gitlinks": migration["submodules"]["old"]}})
                result["rollback_receipt"] = str(claim)
                reference = write_receipt(result_path, result)
            for action in locked["actions"]:
                if migration_eligible and action["kind"] == "advance_role_base":
                    deferred_role_base = action
                    continue
                if canonical_refresh:
                    assert_refresh_preserved(controller, repository, migration["inventory"],
                                             Path(locked["registered_owner"]), migration["preserve_owners"])
                    if sha(repository, task_policy["target_ref"]) != locked["target"]["sha"]:
                        raise IntegrationError("target moved during owner refresh; preserve interrupted checkout")
                    result["pending_action"] = action
                    reference = write_receipt(result_path, result)
                if action["kind"] == "detach_target_holder":
                    root = Path(action["path"])
                    git(root, "switch", "--detach", action["head"])
                elif action["kind"] == "refresh_owner":
                    root = Path(action["path"])
                    if canonical_refresh:
                        git(root, "-c", "protocol.allow=never", "-c", "submodule.recurse=false",
                            "switch", "--detach", action["after"])
                    else:
                        git(root, "switch", "--detach", action["after"])
                elif action["kind"] == "clear_legacy_integration_registration":
                    git(Path(action["repository"]), "config", "--local", "--unset-all",
                        action["key"])
                elif action["kind"] == "advance_role_base":
                    advance_owner_role_base(Path(action["path"]), action["before"], action["after"])
                result["phases"].append({**action, "status": "complete"})
                reference = write_receipt(result_path, result)
            owner = Path(current["registered_owner"])
            if not canonical_refresh:
                git(owner, "submodule", "sync", "--recursive")
            update_args = ["submodule", "update", "--init", "--recursive", "--checkout"]
            if migration_eligible:
                update_args.append("--no-fetch")
            if canonical_refresh:
                assert_refresh_preserved(controller, repository, migration["inventory"], owner,
                                         migration["preserve_owners"])
                update_args.remove("--init")  # all old submodules must already be exact/local
                result["pending_action"] = {"kind": "hydrate_target_submodules_no_fetch"}
                reference = write_receipt(result_path, result)
                git(owner, "-c", "protocol.allow=never", *update_args)
            else:
                git(owner, *update_args)
            if migration_eligible:
                observed_links = sorted(
                    ({"path": row["path"], "sha": row["sha"]}
                     for row in submodule_state(owner)), key=lambda row: row["path"])
                if observed_links != migration["expected_final"]["gitlinks"]:
                    raise IntegrationError("receipt-bound target submodule readback mismatch")
                result["phases"].append({"kind": "hydrate_target_submodules_no_fetch",
                                         "status": "complete",
                                         "gitlinks": observed_links})
                reference = write_receipt(result_path, result)
            if deferred_role_base:
                if canonical_refresh:
                    assert_refresh_preserved(controller, repository, migration["inventory"], owner,
                                             migration["preserve_owners"])
                    if sha(repository, task_policy["target_ref"]) != locked["target"]["sha"]:
                        raise IntegrationError("target moved before roleBase advancement")
                    result["pending_action"] = deferred_role_base
                    reference = write_receipt(result_path, result)
                advance_owner_role_base(owner, deferred_role_base["before"],
                                        deferred_role_base["after"])
                result["phases"].append({**deferred_role_base, "status": "complete"})
                reference = write_receipt(result_path, result)
            after = status_payload(controller)
            if canonical_refresh:
                result["preserved_inventory"] = assert_refresh_preserved(
                    controller, repository, migration["inventory"], owner, migration["preserve_owners"])
                if after["target"] != locked["target"]:
                    raise IntegrationError("target moved during owner refresh")
            if not after["ready"]:
                raise IntegrationError("repair verification did not reach ready state")
            final_readback = None
            if migration_eligible:
                final_owner = after["integration"]["owner"] or {}
                final_readback = {
                    "head": final_owner.get("head"),
                    "tree": commit_tree(repository, final_owner.get("head", "")),
                    "role_base": final_owner.get("role_base"),
                    "role": worktree_config(owner, "juno.workspace.role"),
                    "authority": final_owner.get("authority"),
                    "clean": final_owner.get("clean"), "detached": final_owner.get("detached"),
                    "full_checkout": final_owner.get("full_checkout"),
                    "gitlinks": sorted(
                        ({"path": row["path"], "sha": row["sha"]}
                         for row in final_owner.get("submodules", [])),
                        key=lambda row: row["path"]),
                    "legacy_findings": sorted(row["code"] for row in after["findings"]
                                              if row["code"] in {
                        "legacy_checkout_local_version_cache_writer",
                        "tracked_worktree_version_cache"}),
                    "ready": after["ready"],
                }
                expected = {**migration["expected_final"], "legacy_findings": [], "ready": True}
                if final_readback != expected:
                    raise IntegrationError("stale-owner migration final readback mismatch")
            result.pop("pending_action", None)
            result.update({"outcome": "completed", "status": after,
                           "final_readback": final_readback})
            reference = write_receipt(result_path, result)
            if canonical_refresh:
                reference = managed_receipt_write(result_path.with_suffix(".completed.json"), result)
            return {**result, "receipt": reference}, 0
    except (IntegrationError, ManagedRuntimeError, task_workspace.TaskWorkspaceError, OSError) as exc:
        result.update({"outcome": "failed", "error": str(exc)})
        if canonical_refresh:
            try:
                result["interrupted_status"] = status_payload(controller)
            except (IntegrationError, task_workspace.TaskWorkspaceError, OSError) as observation_error:
                result["observation_error"] = str(observation_error)
            result["rollback_policy"] = "preserve checkout; no automatic reset; separate reviewed recovery required"
        reference = write_receipt(result_path, result)
        if canonical_refresh:
            reference = managed_receipt_write(result_path.with_suffix(".failed.json"), result)
        return {**result, "receipt": reference}, 2


def remote_ref_sha(root: Path, remote: str, ref: str) -> str | None:
    output = run(["git", "-C", str(root), "ls-remote", "--refs", remote, ref],
                 root, check=False).stdout.strip()
    match = re.fullmatch(r"([0-9a-f]{40,64})\s+.+", output)
    return match.group(1) if match else None


def remote_default_ref(root: Path, remote: str) -> str:
    output = run(["git", "-C", str(root), "ls-remote", "--symref", remote, "HEAD"],
                 root, check=False).stdout
    match = re.search(r"^ref:\s+(refs/heads/[^\s]+)\s+HEAD$", output, re.MULTILINE)
    if not match:
        raise IntegrationError(f"cannot resolve default branch for submodule remote {remote}")
    return match.group(1)


def push_plan(controller: Path) -> dict[str, Any]:
    """Create a network-read-only publication plan. Applying it is separately authorized."""
    controller = exact_root(controller, "controller")
    policy, task_policy, policy_path = load_policy(controller)
    repository = task_workspace.product_repository(controller, task_policy)
    status = status_payload(controller)
    blockers = [row["code"] for row in status["findings"] if row["severity"] == "error"]
    owner = status["integration"]["owner"]
    actions: list[dict[str, Any]] = []
    target_ref, target_sha = status["target"]["ref"], status["target"]["sha"]
    if not status["ready"] or not owner or not target_sha:
        blockers.append("integration_owner_not_ready")
    else:
        owner_root = Path(owner["path"])
        for item in owner["submodules"]:
            if item["state"] != "exact" or not item["path"]:
                blockers.append(f"submodule_not_exact:{item['path']}")
                continue
            child = owner_root / item["path"]
            remote, child_ref = "origin", remote_default_ref(child, "origin")
            remote_sha = remote_ref_sha(child, remote, child_ref)
            if remote_sha != item["sha"]:
                if remote_sha and run(["git", "-C", str(child), "merge-base", "--is-ancestor",
                                       remote_sha, item["sha"]], child, check=False).returncode:
                    blockers.append(f"submodule_remote_diverged:{item['path']}")
                actions.append({"kind": "push_submodule", "path": item["path"],
                                "repository": str(child), "remote": remote, "ref": child_ref,
                                "before": remote_sha, "after": item["sha"]})
        remote_sha = remote_ref_sha(repository, policy["remote"], target_ref)
        if remote_sha != target_sha:
            if remote_sha and run(["git", "-C", str(repository), "merge-base", "--is-ancestor",
                                   remote_sha, target_sha], repository, check=False).returncode:
                blockers.append("root_remote_diverged")
            actions.append({"kind": "push_root", "repository": str(owner_root),
                            "remote": policy["remote"], "ref": target_ref,
                            "before": remote_sha, "after": target_sha})
    core = {"schema_version": SCHEMA, "operation": "push", "controller": str(controller),
            "repository": str(repository), "policy": str(policy_path),
            "policy_sha256": hashlib.sha256(policy_path.read_bytes()).hexdigest(),
            "target_ref": target_ref, "target_sha": target_sha,
            "registered_owner": status["integration"]["registered_path"],
            "actions": actions, "blockers": sorted(set(blockers))}
    return {**core, "plan_sha256": json_digest(core)}


def push(controller: Path, *, dry_run: bool, apply: Path | None,
         _lock_held: bool = False,
         _plan_receipt: dict[str, str] | None = None) -> tuple[dict[str, Any], int]:
    controller = exact_root(controller, "controller")
    policy, task_policy, policy_path = load_policy(controller)
    repository = task_workspace.product_repository(controller, task_policy)
    if dry_run:
        plan = push_plan(controller)
        receipt = {**plan, "outcome": "planned" if not plan["blockers"] else "refused"}
        reference = write_receipt(operation_receipt_path(controller, policy, "push-plan"), receipt)
        return {**receipt, "receipt": reference}, 0 if not plan["blockers"] else 2
    if apply is None:
        with integration_target_lock(repository, task_policy["target_ref"]):
            plan = push_plan(controller)
            planned = {**plan, "outcome": "planned" if not plan["blockers"] else "refused"}
            plan_reference = write_receipt(
                operation_receipt_path(controller, policy, "push-plan"), planned)
            if plan["blockers"]:
                terminal = {
                    "schema_version": SCHEMA, "operation": "push",
                    "mode": "plan-and-apply", "outcome": "refused",
                    "final_status": "refused", "plan_sha256": plan["plan_sha256"],
                    "blockers": plan["blockers"], "plan_receipt": plan_reference,
                    "phases": [],
                }
                outcome_reference = write_receipt(
                    operation_receipt_path(controller, policy, "push-outcome"), terminal)
                return {**terminal, "outcome_receipt": outcome_reference,
                        "receipt": outcome_reference}, 2
            return push(controller, dry_run=False, apply=Path(plan_reference["path"]),
                        _lock_held=True, _plan_receipt=plan_reference)
    try:
        approved = json.loads(apply.expanduser().resolve().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrationError(f"invalid push plan receipt: {exc}") from exc
    core_keys = {"schema_version", "operation", "controller", "repository", "policy",
                 "policy_sha256", "target_ref", "target_sha", "registered_owner",
                 "actions", "blockers"}
    if (not isinstance(approved, dict)
            or set(approved) != core_keys | {"plan_sha256", "outcome"}
            or approved.get("schema_version") != SCHEMA
            or approved.get("operation") != "push"
            or approved.get("outcome") != "planned"
            or approved.get("blockers")):
        raise IntegrationError("push plan receipt is not an eligible exact plan")
    core = {key: approved[key] for key in core_keys}
    if approved.get("plan_sha256") != json_digest(core):
        raise IntegrationError("push plan receipt digest is invalid")
    target_ref = task_policy["target_ref"]
    target_sha = sha(repository, target_ref)
    if (approved["controller"] != str(controller)
            or approved["repository"] != str(repository)
            or approved["policy"] != str(policy_path)
            or approved["policy_sha256"] != hashlib.sha256(policy_path.read_bytes()).hexdigest()
            or approved["target_ref"] != target_ref
            or approved["target_sha"] != target_sha
            or approved["registered_owner"] != registered_owner(repository)):
        raise IntegrationError("push plan local identity drifted; generate a new dry-run receipt")
    actions = approved["actions"]
    if not isinstance(actions, list):
        raise IntegrationError("push plan actions are invalid")
    seen_root = False
    child_paths: set[str] = set()
    for index, action in enumerate(actions):
        required = {"kind", "repository", "remote", "ref", "before", "after"}
        if not isinstance(action, dict) or set(action) not in (required, required | {"path"}):
            raise IntegrationError("push plan action shape is invalid")
        if action["kind"] == "push_submodule":
            if seen_root or set(action) != required | {"path"} or action["path"] in child_paths:
                raise IntegrationError("submodule push actions must be unique and precede root")
            child_paths.add(action["path"])
        elif action["kind"] == "push_root":
            if seen_root or index != len(actions) - 1 or set(action) != required:
                raise IntegrationError("exactly one root push action must be last")
            seen_root = True
        else:
            raise IntegrationError("unknown push plan action")
        if (not isinstance(action["repository"], str)
                or not isinstance(action["remote"], str) or not action["remote"]
                or not isinstance(action["ref"], str) or not action["ref"].startswith("refs/heads/")
                or action["before"] is not None and not SHA_RE.fullmatch(action["before"])
                or not isinstance(action["after"], str) or not SHA_RE.fullmatch(action["after"])):
            raise IntegrationError("push plan action identity is invalid")
    if actions and not seen_root:
        raise IntegrationError("push plan with publication actions must end in root push")
    result_path = operation_receipt_path(controller, policy, "push-apply")
    result = {**approved, "outcome": "running", "phases": []}
    if _plan_receipt is not None:
        result["plan_receipt"] = _plan_receipt
    reference = write_receipt(result_path, result)
    def apply_locked() -> tuple[dict[str, Any], int]:
        nonlocal reference
        try:
            status = status_payload(controller)
            if (not status["ready"]
                    or status["integration"]["registered_path"] != approved["registered_owner"]
                    or status["target"]["sha"] != approved["target_sha"]):
                raise IntegrationError("push apply integration identity is no longer ready")
            for action in actions:
                action_root = exact_root(Path(action["repository"]), "push action repository")
                if sha(action_root, action["after"]) != action["after"]:
                    raise IntegrationError(f"push action commit is unavailable: {action['after']}")
                observed = remote_ref_sha(action_root, action["remote"], action["ref"])
                if observed == action["after"]:
                    outcome = "already_complete"
                elif observed != action["before"]:
                    raise IntegrationError(
                        f"remote changed for {action['kind']}:{action.get('path', '')}"
                    )
                else:
                    if action["kind"] == "push_root":
                        root_remote_url = git(repository, "remote", "get-url", policy["remote"])
                        closure = task_workspace.nested_gitlink_remote_closure(
                            repository, approved["target_sha"], root_remote_url)
                        remote_refs = []
                        for item in status["integration"]["owner"]["submodules"]:
                            child = Path(approved["registered_owner"]) / item["path"]
                            child_ref = remote_default_ref(child, "origin")
                            remote_refs.append({
                                "path": item["path"], "sha": item["sha"],
                                "remote": "origin", "ref": child_ref,
                                "observed": remote_ref_sha(child, "origin", child_ref),
                            })
                        refs_available = all(row["observed"] == row["sha"]
                                             for row in remote_refs)
                        if closure["gitlinks"] or remote_refs:
                            result["phases"].append({
                                "kind": "verify_nested_gitlink_remote_closure",
                                "status": "complete",
                                "outcome": "verified"
                                if closure["available"] and refs_available else "refused",
                                "result": {"closure": closure, "remote_refs": remote_refs},
                            })
                            reference = write_receipt(result_path, result)
                        if not closure["available"] or not refs_available:
                            unavailable = next((row for row in remote_refs
                                                if row["observed"] != row["sha"]), None)
                            if unavailable is None:
                                unavailable = next((row for row in closure["gitlinks"]
                                                    if not row.get("available")), {})
                            raise IntegrationError(
                                "nested gitlink remote closure changed before root push: "
                                f"path={unavailable.get('path', '<unknown>')} "
                                f"sha={unavailable.get('sha', '<unknown>')}"
                            )
                    argv = ["git", "-C", str(action_root), "push", "--porcelain",
                            action["remote"], f"{action['after']}:{action['ref']}"]
                    run(argv, action_root)
                    if remote_ref_sha(action_root, action["remote"], action["ref"]) != action["after"]:
                        raise IntegrationError(
                            f"remote readback failed for {action['kind']}:{action.get('path', '')}"
                        )
                    outcome = "pushed"
                result["phases"].append({**action, "status": "complete", "outcome": outcome})
                reference = write_receipt(result_path, result)
            result["outcome"] = "completed"
            reference = write_receipt(result_path, result)
            payload = {**result, "receipt": reference}
            if _plan_receipt is not None:
                payload.update({"mode": "plan-and-apply", "final_status": "completed",
                                "outcome_receipt": reference})
            return payload, 0
        except (IntegrationError, task_workspace.TaskWorkspaceError, OSError) as exc:
            result.update({"outcome": "failed", "error": str(exc)})
            reference = write_receipt(result_path, result)
            payload = {**result, "receipt": reference}
            if _plan_receipt is not None:
                payload.update({"mode": "plan-and-apply", "final_status": "failed",
                                "outcome_receipt": reference})
            return payload, 2
        except (KeyboardInterrupt, SystemExit) as exc:
            result.update({"outcome": "interrupted", "error": type(exc).__name__})
            reference = write_receipt(result_path, result)
            raise

    if _lock_held:
        return apply_locked()
    with integration_target_lock(repository, target_ref):
        return apply_locked()


def sync(controller: Path) -> tuple[dict[str, Any], int]:
    controller = exact_root(controller, "controller")
    policy, task_policy, _ = load_policy(controller)
    repository = task_workspace.product_repository(controller, task_policy)
    target_ref = task_policy["target_ref"]
    receipt_path = (controller / policy["receipt_root"] /
                    f"{time.time_ns()}-{os.getpid()}.json").resolve()
    receipt: dict[str, Any] = {"schema_version": SCHEMA, "operation": "sync",
        "outcome": "running", "phase": "created", "controller": str(controller),
        "repository": str(repository), "target_ref": target_ref, "phases": []}
    reference = write_receipt(receipt_path, receipt)
    try:
        with integration_target_lock(repository, target_ref):
            before = status_payload(controller)
            owner = before["integration"]["owner"]
            blockers = [row for row in before["findings"] if row["severity"] == "error"]
            if not owner or blockers:
                raise IntegrationError("integration preflight refused: " +
                                       ", ".join(row["code"] for row in blockers))
            receipt["phases"].append({"phase": "preflight", "status": "complete", "status": before})
            receipt["phase"] = "preflight"; reference = write_receipt(receipt_path, receipt)
            owner_root = Path(owner["path"])
            remote_ref = before["remote"]["ref"]
            run(["git", "-C", str(repository), "fetch", "--no-tags", policy["remote"],
                 f"+{target_ref}:{remote_ref}"], repository)
            receipt["phases"].append({"phase": "fetch", "status": "complete",
                                      "remote_sha": sha(repository, remote_ref)})
            receipt["phase"] = "fetch"; reference = write_receipt(receipt_path, receipt)
            local = sha(repository, target_ref); remote = sha(repository, remote_ref)
            owner_role_base = worktree_config(owner_root, "juno.workspace.roleBase")
            rel = relation(repository, local, remote)
            if not local or not remote:
                raise IntegrationError("local target or fetched remote ref is unavailable")
            if rel["ahead"] and rel["behind"]:
                raise IntegrationError("local target and remote diverged")
            proposed = remote if rel["behind"] else local
            root_remote_url = git(repository, "remote", "get-url", policy["remote"])
            closure = task_workspace.nested_gitlink_remote_closure(
                repository, proposed, root_remote_url)
            receipt["phases"].append({"phase": "nested_gitlink_closure", "status": "complete",
                                      "result": closure})
            receipt["phase"] = "nested_gitlink_closure"
            reference = write_receipt(receipt_path, receipt)
            if not closure["available"]:
                unavailable = next((row for row in closure["gitlinks"]
                                    if not row.get("available")), {})
                receipt["recovery"] = {
                    "reason": "nested_gitlink_unavailable",
                    "command": "publish the exact child commit through its authorized child integration, then rerun `yy integration sync`",
                    "retry": "yy integration sync",
                    "owner_unchanged": True,
                }
                reference = write_receipt(receipt_path, receipt)
                raise IntegrationError(
                    "nested_gitlink_unavailable: "
                    f"root={proposed} path={unavailable.get('path', '<unknown>')} "
                    f"sha={unavailable.get('sha', '<unknown>')} "
                    f"remote={unavailable.get('remote', '<missing>')} "
                    f"failed_check={unavailable.get('failed_check', 'nested_gitlink_closure')}; "
                    "publish that exact child through its authorized integration and retry `yy integration sync`"
                )
            if rel["behind"]:
                git(repository, "update-ref", target_ref, remote, local)
                target_outcome = "fast_forwarded"
            else:
                target_outcome = "preserved_local_ahead" if rel["ahead"] else "unchanged"
            current = sha(repository, target_ref)
            receipt["phases"].append({"phase": "target", "status": "complete",
                                      "outcome": target_outcome, "before": local, "after": current})
            receipt["phase"] = "target"; reference = write_receipt(receipt_path, receipt)
            git(owner_root, "switch", "--detach", current or "")
            git(owner_root, "submodule", "sync", "--recursive")
            git(owner_root, "submodule", "update", "--init", "--recursive", "--checkout")
            if owner_role_base != current:
                if (not owner_role_base or not current or run([
                        "git", "-C", str(repository), "merge-base", "--is-ancestor",
                        owner_role_base, current], repository, check=False).returncode):
                    raise IntegrationError(
                        "integration sync refuses a stale or divergent integration owner roleBase"
                    )
                authority = advance_owner_role_base(owner_root, owner_role_base, current)
                receipt["phases"].append({"phase": "authority", "status": "complete",
                                          **authority})
                receipt["phase"] = "authority"; reference = write_receipt(receipt_path, receipt)
            if current != local:
                runtime_refresh = managed_runtime_refresh(
                    controller, repository, local, current or "", task_id="integration-sync")
            else:
                runtime_refresh = managed_runtime_inspect(controller, repository, current or "")
                if not runtime_refresh["healthy"]:
                    raise IntegrationError(
                        "managed controller runtime doctor found drift without a new target transition"
                    )
            receipt["phases"].append({"phase": "managed_runtime", "status": "complete",
                                      "result": runtime_refresh})
            receipt["phase"] = "managed_runtime"; reference = write_receipt(receipt_path, receipt)
            after = status_payload(controller)
            if (not after["ready"] or any(item["state"] != "exact"
                    for item in (after["integration"]["owner"] or {}).get("submodules", []))):
                raise IntegrationError("post-sync owner or submodule verification failed")
            receipt["phases"].append({"phase": "verify", "status": "complete", "status": after})
            receipt["phase"] = "complete"; receipt["outcome"] = "completed"
            reference = write_receipt(receipt_path, receipt)
            return {"schema_version": SCHEMA, "operation": "sync", "outcome": "completed",
                    "receipt": reference, "status": after}, 0
    except (IntegrationError, ManagedRuntimeError,
            task_workspace.TaskWorkspaceError, OSError) as exc:
        receipt["outcome"] = "failed"; receipt["error"] = str(exc)
        if isinstance(exc, ManagedRuntimeError) and exc.receipt:
            receipt["managed_runtime_receipt"] = exc.receipt
        reference = write_receipt(receipt_path, receipt)
        return {"schema_version": SCHEMA, "operation": "sync", "outcome": "failed",
                "error": str(exc), "receipt": reference}, 2


def register(controller: Path, owner_path: Path, *, replace: bool = False,
             runtime_executable: Path | None = None,
             runtime_version: str | None = None) -> tuple[dict[str, Any], int]:
    """Bootstrap missing first-run identity, then bind one verified owner.

    Existing identity is evidence, never repair input: partial or differing role,
    routing, and runtime values fail closed instead of being overwritten.
    """
    controller = exact_root(controller, "controller")
    policy, task_policy, _ = load_policy(controller)
    repository = task_workspace.product_repository(controller, task_policy)
    target_ref = task_policy["target_ref"]
    receipt_path = (controller / policy["receipt_root"] /
                    f"{time.time_ns()}-{os.getpid()}-register.json").resolve()
    try:
        owner = exact_root(owner_path, "integration owner")
        common = Path(git(repository, "rev-parse", "--path-format=absolute",
                          "--git-common-dir")).resolve()
        owner_common = Path(git(owner, "rev-parse", "--path-format=absolute",
                                "--git-common-dir")).resolve()
        registered_paths = {str(item.get("worktree")) for item in parse_worktrees(repository)}
        if common != owner_common or str(owner) not in registered_paths:
            raise IntegrationError("integration owner is not a linked worktree of this repository")
        full, reasons = full_checkout(owner)
        if (git(owner, "symbolic-ref", "-q", "HEAD", check=False)
                or git(owner, "status", "--porcelain=v1", "--untracked-files=all") or not full):
            raise IntegrationError("integration owner must be clean, detached, and full: "
                                   + ", ".join(reasons))
        target_sha, owner_head = sha(repository, target_ref), sha(owner, "HEAD")
        if not target_sha or not owner_head:
            raise IntegrationError("integration owner bootstrap requires exact target and owner commits")

        owner_identity = tuple(worktree_config(owner, key) for key in (
            "juno.workspace.role", "juno.workspace.roleAuthority", "juno.workspace.roleBase"))
        seed_owner = owner_identity == (None, None, None)
        expected_owner = ("integration-owner", policy["owner_role_authority"], owner_head)
        if seed_owner and owner_head != target_sha:
            raise IntegrationError("unregistered integration owner HEAD must equal the exact target commit")
        if not seed_owner and owner_identity != expected_owner:
            raise IntegrationError("integration owner identity is partial, tampered, or stale")

        controller_branch = git(controller, "symbolic-ref", "-q", "HEAD", check=False)
        controller_head = sha(controller, "HEAD")
        if not controller_branch or not controller_head:
            raise IntegrationError("controller must be attached to its configured branch")
        controller_role = worktree_config(controller, "juno.workspace.role")
        controller_authority = worktree_config(controller, "juno.workspace.roleAuthority")
        controller_base = worktree_config(controller, "juno.workspace.roleBase")
        seed_controller = controller_role is None and controller_authority is None and controller_base is None
        if not seed_controller and (controller_role != "controller" or controller_authority is not None
                                    or not controller_base or sha(controller, controller_base) != controller_base):
            raise IntegrationError("controller worktree identity is partial or invalid")

        paths = git(repository, "config", "--local", "--get-all", "juno.controller.path",
                    check=False).splitlines()
        branches = git(repository, "config", "--local", "--get-all", "juno.controller.branch",
                       check=False).splitlines()
        if len(paths) > 1 or len(branches) > 1:
            raise IntegrationError("controller routing registration is ambiguous")
        if paths or branches:
            if paths != [str(controller)] or branches != [controller_branch]:
                raise IntegrationError("existing controller routing differs from this verified controller")

        runtime_path = runtime_executable.expanduser().resolve() if runtime_executable else None
        if bool(runtime_path) != bool(runtime_version):
            raise IntegrationError("runtime executable and version must be supplied together")
        if runtime_path and (not runtime_path.is_file() or not managed_valid_package_version(runtime_version)):
            raise IntegrationError("invoking package runtime identity is invalid")
        existing_runtime = worktree_config(controller, "juno.controller.runtimeExecutable")
        existing_version = worktree_config(controller, "juno.controller.runtimeVersion")
        if existing_runtime or existing_version:
            if not runtime_path or existing_runtime != str(runtime_path) or existing_version != runtime_version:
                raise IntegrationError("existing controller runtime identity differs from the invoking package")

        with integration_target_lock(repository, target_ref):
            previous = registered_owner(repository)
            if previous and previous != str(owner) and not replace:
                raise IntegrationError(
                    "a different canonical integration owner is already registered; use --replace"
                )
            git(repository, "config", "--local", "extensions.worktreeConfig", "true")
            seeded: list[str] = []
            if seed_owner:
                for key, value in zip(("role", "roleAuthority", "roleBase"), expected_owner):
                    git(owner, "config", "--worktree", f"juno.workspace.{key}", value)
                    seeded.append(f"owner:{key}")
            if seed_controller:
                for key, value in (("role", "controller"), ("roleBase", controller_head)):
                    git(controller, "config", "--worktree", f"juno.workspace.{key}", value)
                    seeded.append(f"controller:{key}")
            if not paths:
                git(repository, "config", "--local", "juno.controller.path", str(controller))
                git(repository, "config", "--local", "juno.controller.branch", controller_branch)
                seeded.append("repository:controller-routing")
            if runtime_path and not existing_runtime:
                git(controller, "config", "--worktree", "juno.controller.runtimeExecutable",
                    str(runtime_path))
                git(controller, "config", "--worktree", "juno.controller.runtimeVersion",
                    runtime_version)
                seeded.append("controller:runtime")
            git(repository, "config", "--local", OWNER_CONFIG, str(owner))
            if (tuple(worktree_config(owner, key) for key in (
                    "juno.workspace.role", "juno.workspace.roleAuthority", "juno.workspace.roleBase"))
                    != expected_owner
                    or worktree_config(controller, "juno.workspace.role") != "controller"
                    or git(repository, "config", "--local", "--get", "juno.controller.path") != str(controller)
                    or git(repository, "config", "--local", "--get", "juno.controller.branch") != controller_branch
                    or registered_owner(repository) != str(owner)):
                raise IntegrationError("first-run registration exact readback failed")
        receipt = {"schema_version": SCHEMA, "operation": "register", "outcome": "completed",
                   "repository": str(repository), "target_ref": target_ref, "target_sha": target_sha,
                   "previous": previous, "owner": str(owner), "replace": replace,
                   "controller": str(controller), "controller_branch": controller_branch,
                   "seeded": seeded}
        reference = write_receipt(receipt_path, receipt)
        return {**receipt, "receipt": reference, "status": status_payload(controller)}, 0
    except (IntegrationError, task_workspace.TaskWorkspaceError, OSError) as exc:
        receipt = {"schema_version": SCHEMA, "operation": "register", "outcome": "failed",
                   "owner": str(owner_path.expanduser().resolve()), "error": str(exc)}
        reference = write_receipt(receipt_path, receipt)
        return {**receipt, "receipt": reference}, 2


class AdoptionError(RuntimeError):
    pass


def adoption_canonical(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()


def adoption_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def adoption_atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        raise AdoptionError(f"immutable adoption receipt already exists: {path}")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(adoption_canonical(payload))
    os.replace(temporary, path)


def adoption_run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise AdoptionError(detail[-4000:] or f"command failed: {argv!r}")
    return result


def adoption_git(root: Path, *args: str) -> str:
    return adoption_run(["git", "-C", str(root), *args], root).stdout.strip()


def adoption_clean(root: Path, label: str) -> None:
    if adoption_git(root, "status", "--porcelain=v2", "--untracked-files=all"):
        raise AdoptionError(f"source runtime adoption requires a clean {label}")


def adoption_exact_external(path: Path, controller: Path, label: str) -> Path:
    candidate = Path(os.path.abspath(path.expanduser()))
    if not candidate.is_absolute():
        raise AdoptionError(f"{label} must be an absolute path")
    common = Path(adoption_git(controller, "rev-parse", "--git-common-dir")).resolve()
    for protected in (controller.resolve(), common):
        try:
            candidate.relative_to(protected)
        except ValueError:
            continue
        raise AdoptionError(f"{label} must be outside the controller and Git administration directory")
    probe = subprocess.run(["git", "-C", str(candidate.parent), "rev-parse", "--show-toplevel"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
    if probe.returncode == 0:
        raise AdoptionError(f"{label} must be outside every Git worktree or Git ancestor")
    return candidate


def adoption_read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise AdoptionError(f"invalid {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise AdoptionError(f"invalid {label}")
    return value


def adoption_controller_config(controller: Path, key: str) -> str:
    return adoption_git(controller, "config", "--worktree", "--get", key)


def adoption_assert_target(repository: Path, target_ref: str, target_sha: str, owner: Path) -> None:
    if adoption_git(repository, "rev-parse", f"{target_ref}^{{commit}}") != target_sha:
        raise AdoptionError("source runtime adoption refused because the target ref moved")
    if adoption_git(owner, "rev-parse", "HEAD") != target_sha:
        raise AdoptionError("registered integration owner is not at the exact target generation")
    adoption_clean(owner, "integration owner")


def adoption_preserved_owner(repository: Path, owner: Path) -> dict[str, Any]:
    """Evidence only: a retained owner is not a source/build input or a write target."""
    config = Path(adoption_git(owner, "rev-parse", "--path-format=absolute",
                               "--git-path", "config.worktree"))
    return {"registered_path": registered_owner(repository),
            "head": adoption_git(owner, "rev-parse", "HEAD"),
            "role_base": worktree_config(owner, "juno.workspace.roleBase"),
            "registration_sha256": adoption_digest(config), "moved": False}


def adoption_assert_preserved_source(repository: Path, target_ref: str, target_sha: str,
                                     owner: Path, before: dict[str, Any]) -> None:
    if adoption_git(repository, "rev-parse", f"{target_ref}^{{commit}}") != target_sha:
        raise AdoptionError("source runtime adoption refused because the target ref moved")
    if adoption_preserved_owner(repository, owner) != before:
        raise AdoptionError("retained integration owner identity changed; refusing adoption")


def adoption_isolated_source(repository: Path, target_sha: str, destination: Path) -> Path:
    """Create an independently registered exact checkout, never a protected owner.

    Do not reuse ignored dist/node_modules from an integration or task checkout.
    The temporary repository shares only immutable Git objects, not worktree
    registration, task state, hooks, configuration, or dependency directories.
    """
    if not re.fullmatch(r"[0-9a-f]{40}", target_sha):
        raise AdoptionError("isolated source requires an exact commit identity")
    if destination.exists() or destination.is_symlink():
        raise AdoptionError("isolated source destination must be absent")
    adoption_git(repository, "cat-file", "-e", f"{target_sha}^{{commit}}")
    adoption_run(["git", "clone", "--shared", "--no-checkout", "--", str(repository),
                  str(destination)], repository)
    adoption_git(destination, "-c", "core.hooksPath=/dev/null", "checkout", "--detach", target_sha)
    if adoption_git(destination, "rev-parse", "HEAD") != target_sha:
        raise AdoptionError("isolated source checkout identity mismatch")
    adoption_clean(destination, "isolated source")
    return destination


def adoption_build_run(argv: list[str], cwd: Path, timeout_seconds: float = 900) -> None:
    """Bound output memory and terminate only this build's process group on timeout."""
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            # The group can outlive its leader; kill remaining build children too.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            raise AdoptionError("isolated source build timed out") from exc
        if code:
            output.seek(0, os.SEEK_END)
            output.seek(max(0, output.tell() - 4000))
            raise AdoptionError(output.read().decode(errors="replace") or "isolated source build failed")


def adoption_pack_isolated(repository: Path, target_sha: str, artifact: Path) -> None:
    if artifact.exists() or artifact.is_symlink():
        raise AdoptionError("isolated source artifact destination must be absent")
    with tempfile.TemporaryDirectory(prefix="yylo-isolated-source-") as temporary:
        root = Path(temporary)
        source = adoption_isolated_source(repository, target_sha, root / "source")
        package = source / "juno-code"
        if not (package / "package-lock.json").is_file():
            raise AdoptionError("isolated source requires its checked-in package lock")
        # Hydration and prepack build are bounded and task-local; never borrow
        # dependencies or built output from the retained integration owner.
        for argv in (["npm", "ci"], ["npm", "pack", "--pack-destination", str(root)]):
            adoption_build_run(argv, package)
        adoption_clean(source, "built isolated source")
        packs = list(root.glob("*.tgz"))
        if len(packs) != 1 or packs[0].is_symlink():
            raise AdoptionError("isolated source build must produce exactly one artifact")
        with artifact.open("xb") as stream:
            stream.write(packs[0].read_bytes())
            stream.flush()
            os.fsync(stream.fileno())


def adoption_owner_preflight(repository: Path, target_ref: str, target_sha: str,
                             owner: Path) -> dict[str, Any]:
    """Freeze a clean detached owner's rollback identity without moving it."""
    if adoption_git(repository, "rev-parse", f"{target_ref}^{{commit}}") != target_sha:
        raise AdoptionError("source runtime adoption refused because the target ref moved")
    adoption_clean(owner, "integration owner")
    symbolic = subprocess.run(
        ["git", "-C", str(owner), "symbolic-ref", "-q", "HEAD"], cwd=owner,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if symbolic.returncode not in (0, 1):
        raise AdoptionError(symbolic.stderr.strip() or "could not inspect integration owner HEAD")
    if symbolic.returncode == 0:
        raise AdoptionError("source runtime adoption requires a detached integration owner")
    if (worktree_config(owner, "juno.workspace.role") != "integration-owner"
            or worktree_config(owner, "juno.workspace.roleAuthority") != AUTHORITY):
        raise AdoptionError("source runtime adoption requires the registered integration-owner authority")
    before_head = adoption_git(owner, "rev-parse", "HEAD")
    before_base = worktree_config(owner, "juno.workspace.roleBase")
    if before_head != target_sha:
        adoption_git(repository, "merge-base", "--is-ancestor", before_head, target_sha)
        if before_base and before_base != before_head:
            raise AdoptionError("stale integration owner roleBase does not match its clean detached HEAD")
    return {"head": before_head, "role_base": before_base,
            "moved": before_head != target_sha}


def adoption_prepare_owner(repository: Path, target_ref: str, target_sha: str,
                           owner: Path, before: dict[str, Any] | None = None) -> dict[str, Any]:
    """Move only a preflighted clean detached owner to the fixed local target."""
    before = before or adoption_owner_preflight(repository, target_ref, target_sha, owner)
    if before["moved"]:
        adoption_git(owner, "switch", "--detach", target_sha)
        adoption_git(owner, "submodule", "sync", "--recursive")
        adoption_git(owner, "submodule", "update", "--init", "--recursive", "--checkout")
        if before.get("role_base") != target_sha:
            adoption_git(owner, "config", "--worktree", "juno.workspace.roleBase", target_sha)
    adoption_assert_target(repository, target_ref, target_sha, owner)
    return before


def adoption_restore_owner(owner: Path, before: dict[str, Any]) -> bool:
    if not before.get("moved"):
        return True
    adoption_clean(owner, "integration owner")
    adoption_git(owner, "switch", "--detach", before["head"])
    adoption_git(owner, "submodule", "sync", "--recursive")
    adoption_git(owner, "submodule", "update", "--init", "--recursive", "--checkout")
    old_base = before.get("role_base")
    if old_base:
        adoption_git(owner, "config", "--worktree", "juno.workspace.roleBase", old_base)
    else:
        result = subprocess.run(
            ["git", "-C", str(owner), "config", "--worktree", "--unset-all",
             "juno.workspace.roleBase"], cwd=owner, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode not in (0, 5):
            raise AdoptionError(result.stderr.strip() or "could not restore integration owner roleBase")
    return (adoption_git(owner, "rev-parse", "HEAD") == before["head"]
            and worktree_config(owner, "juno.workspace.roleBase") == old_base)


def adoption_declaration_admission(controller: Path, repository: Path, target_sha: str) -> dict[str, Any]:
    """Read-only operation admission, not an assertion based only on runtime parity."""
    managed_target_provenance(repository, target_sha)
    config = task_workspace.load_config(controller)
    try:
        _, admission = task_workspace.derived_output_admission(
            repository, target_sha, config["allowed_paths"])
    except task_workspace.TaskWorkspaceError as exc:
        raise AdoptionError(f"task-start declaration admission refused: {exc}") from exc
    return admission


def adoption_task_start_admission(controller: Path, repository: Path, target_sha: str) -> dict[str, Any]:
    admission = adoption_declaration_admission(controller, repository, target_sha)
    relative = task_workspace.RUNTIME_PATH
    target = managed_source_bytes(repository, target_sha,
                                     f"juno-code/src/templates/scripts/{Path(relative).name}")
    running_path = controller / relative
    running = running_path.read_bytes() if running_path.is_file() else b""
    return {"runtime_path": str(running_path), "target_path": relative,
            "running_sha256": hashlib.sha256(running).hexdigest() if running else None,
            "target_sha256": hashlib.sha256(target).hexdigest(),
            "current": bool(running and running == target),
            "declaration_admission": admission}


def adoption_public_launcher_preflight(old_executable: str) -> list[dict[str, str]]:
    if not Path(old_executable).is_file():
        raise AdoptionError("currently selected controller executable is missing")
    launchers: list[dict[str, str]] = []
    for name in ("yylo", "yy"):
        found = shutil.which(name)
        if not found:
            raise AdoptionError(f"public {name} launcher is unavailable")
        path = Path(found)
        if not path.is_symlink():
            raise AdoptionError(f"public {name} launcher is not an atomically selectable symlink")
        launchers.append({"name": name, "path": str(path.absolute()),
                          "before": os.readlink(path), "before_resolved": str(path.resolve())})
    if len({row["before_resolved"] for row in launchers}) != 1:
        raise AdoptionError("public yy and yylo launchers do not share one prior runtime identity")
    return launchers


def adoption_public_launchers(old_executable: str, new_executable: Path,
                              preflight: list[dict[str, str]] | None = None) -> dict[str, Any]:
    launchers = preflight or adoption_public_launcher_preflight(old_executable)
    new_launcher = new_executable.resolve().with_name("yylo.sh")
    if not new_launcher.is_file() or not new_executable.is_file():
        raise AdoptionError("installed source runtime is missing its public launcher or executable")
    changed: list[dict[str, str]] = []
    try:
        for row in launchers:
            path = Path(row["path"])
            if (not path.is_symlink() or os.readlink(path) != row["before"]
                    or str(path.resolve()) != row["before_resolved"]):
                raise AdoptionError("public runtime selector changed after preflight")
            temporary = path.with_name(f".{path.name}.source-adoption-{os.getpid()}")
            temporary.unlink(missing_ok=True)
            temporary.symlink_to(new_launcher)
            os.replace(temporary, path)
            changed.append(row)
        if any(Path(row["path"]).resolve() != new_launcher for row in launchers):
            raise AdoptionError("public runtime selector exact readback failed")
    except BaseException:
        for row in reversed(changed):
            path = Path(row["path"])
            if path.is_symlink() and path.resolve() == new_launcher:
                temporary = path.with_name(f".{path.name}.source-adoption-abort-{os.getpid()}")
                temporary.unlink(missing_ok=True)
                temporary.symlink_to(row["before"])
                os.replace(temporary, path)
        raise
    return {"executable": str(new_executable.resolve()), "launcher": str(new_launcher),
            "links": launchers}


def adoption_restore_public_launchers(selection: dict[str, Any]) -> bool:
    selected = Path(selection["launcher"]).resolve()
    complete = True
    for row in reversed(selection["links"]):
        path = Path(row["path"])
        if not path.is_symlink() or path.resolve() != selected:
            complete = False
            continue
        temporary = path.with_name(f".{path.name}.source-adoption-rollback-{os.getpid()}")
        temporary.unlink(missing_ok=True)
        temporary.symlink_to(row["before"])
        os.replace(temporary, path)
    return complete and all(Path(row["path"]).is_symlink()
                            and os.readlink(Path(row["path"])) == row["before"]
                            for row in selection["links"])


def adoption_verify_public_dispatch(selection: dict[str, Any]) -> None:
    executable = Path(selection.get("executable", ""))
    launcher = Path(selection.get("launcher", ""))
    if (not executable.is_file() or not launcher.is_file()
            or any(not Path(row["path"]).is_symlink()
                   or Path(row["path"]).resolve() != launcher.resolve()
                   for row in selection.get("links", []))):
        raise AdoptionError("completed source runtime adoption public selector drifted")
    result = adoption_run([str(Path(selection["links"][-1]["path"])), "merge", "--help"],
                          Path.cwd())
    help_text = result.stdout + result.stderr
    native = all(re.search(rf"(?m)^  {name}(?: |$)", help_text)
                 for name in ("status", "land", "project"))
    retired = any(re.search(rf"(?m)^  {name}(?: |$)", help_text)
                  for name in ("arbiter", "drive", "next", "review", "resume"))
    if not native or retired:
        raise AdoptionError("fresh public yy merge --help did not select only native merge commands")


def adoption_replay(receipt_path: Path, controller: Path, repository: Path, target_ref: str,
                    previous_sha: str, target_sha: str, prefix: Path,
                    isolated_source: bool = False) -> dict[str, Any] | None:
    if not receipt_path.exists():
        return None
    receipt = adoption_read_json(receipt_path, "source runtime adoption receipt")
    if (receipt.get("schema_version") != SOURCE_ADOPTION_SCHEMA or receipt.get("operation") != "runtime-adopt-source"
            or receipt.get("outcome") != "completed" or receipt.get("controller") != str(controller)
            or receipt.get("repository") != str(repository) or receipt.get("target_ref") != target_ref
            or receipt.get("previous_sha") != previous_sha or receipt.get("target_sha") != target_sha
            or receipt.get("install_prefix") != str(prefix)
            or receipt.get("source_mode", "integration-owner") != (
                "isolated" if isolated_source else "integration-owner")
            or adoption_git(repository, "rev-parse", f"{target_ref}^{{commit}}") != target_sha):
        raise AdoptionError("prior source runtime adoption receipt conflicts with the exact requested transaction")
    if isolated_source:
        owner = receipt.get("integration_owner", {})
        owner_path = owner.get("path")
        if (not isinstance(owner_path, str) or registered_owner(repository) != owner_path
                or adoption_preserved_owner(repository, Path(owner_path)) != owner.get("before")):
            raise AdoptionError("completed source runtime adoption retained owner identity drifted")
    artifact = Path(receipt.get("artifact", {}).get("path", ""))
    if not artifact.is_file() or adoption_digest(artifact) != receipt.get("artifact", {}).get("sha256"):
        raise AdoptionError("completed source runtime adoption artifact identity drifted")
    install_receipt = Path(receipt.get("install_receipt", {}).get("path", ""))
    if (not install_receipt.is_file()
            or adoption_digest(install_receipt) != receipt.get("install_receipt", {}).get("sha256")):
        raise AdoptionError("completed source runtime adoption install receipt identity drifted")
    ownership = prefix / ".juno-source-adoption-owner.json"
    marker = adoption_read_json(ownership, "source runtime adoption prefix ownership")
    if (marker.get("receipt") != str(receipt_path) or marker.get("target_sha") != target_sha
            or marker.get("install_receipt_sha256") != adoption_digest(install_receipt)):
        raise AdoptionError("completed source runtime adoption prefix ownership drifted")
    configured = adoption_controller_config(controller, "juno.controller.runtimeExecutable")
    if configured != receipt.get("dispatch", {}).get("executable"):
        raise AdoptionError("completed source runtime adoption controller selection drifted")
    adoption_verify_public_dispatch(receipt.get("dispatch", {}))
    doctor = managed_runtime_inspect(controller, repository, target_sha)
    generation = adoption_task_start_admission(controller, repository, target_sha)
    if not doctor["healthy"] or not generation["current"]:
        raise AdoptionError("completed source runtime adoption no longer passes runtime admission")
    return {**receipt, "replay": "idempotent", "verified": True}


def adoption_generation_assessment(controller: Path, executable: Path) -> dict[str, Any]:
    """Report public generation readiness separately from source-script equality."""
    try:
        assessed = subprocess.run(["node", str(executable), "-q", "scripts", "generation", "doctor"],
                                  cwd=controller, stdin=subprocess.DEVNULL, capture_output=True,
                                  text=True, timeout=120)
        rows = [json.loads(line) for line in assessed.stdout.splitlines() if line.startswith('{')]
        if rows and isinstance(rows[-1], dict):
            disposition = rows[-1].get('disposition')
            expected = 2 if disposition in {'refused', 'transition_incomplete'} else 0
            if (disposition in {'ready', 'refused', 'retained', 'migration_required', 'transition_incomplete'}
                    and assessed.returncode == expected):
                return rows[-1]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return {"disposition": "unavailable"}


def _source_runtime_adopt_locked(args: argparse.Namespace, controller: Path,
                                 policy: dict[str, Any], repository: Path,
                                 target_ref: str) -> dict[str, Any]:
    started = time.monotonic()
    target_sha = managed_exact_commit(repository, args.target_sha, "target generation")
    previous_sha = managed_exact_commit(repository, args.previous_sha, "previous generation")
    output = adoption_exact_external(args.output, controller, "adoption receipt")
    prefix = adoption_exact_external(args.install_prefix, controller, "runtime install prefix")
    prior = adoption_replay(output, controller, repository, target_ref,
                            previous_sha, target_sha, prefix,
                            isolated_source=bool(getattr(args, "isolated_source", False)))
    if prior is not None:
        return prior
    if output.exists() or output.is_symlink():
        raise AdoptionError(f"immutable adoption receipt collision: {output}")
    if prefix.exists() or prefix.is_symlink():
        raise AdoptionError(f"runtime install prefix must be fresh and absent: {prefix}")
    adoption_clean(controller, "metadata controller")
    owner_value = registered_owner(repository)
    if not owner_value:
        raise AdoptionError("source runtime adoption requires a registered integration owner")
    owner = Path(owner_value).resolve()
    isolated_source = bool(getattr(args, "isolated_source", False))
    if not isolated_source:
        adoption_clean(owner, "integration owner")
    adoption_git(repository, "merge-base", "--is-ancestor", previous_sha, target_sha)
    generation_path = controller / MANAGED_GENERATION_PATH
    generation = adoption_read_json(generation_path, "managed runtime generation")
    if generation.get("target_sha") != previous_sha:
        raise AdoptionError("--previous-sha does not match the currently admitted managed generation")
    before_doctor = managed_runtime_inspect(controller, repository, previous_sha)
    if not before_doctor["healthy"]:
        raise AdoptionError("current managed generation is unhealthy; refusing source adoption")
    package = managed_source_json(repository, target_sha, MANAGED_PACKAGE_PATH)
    version = package.get("version") if isinstance(package, dict) else None
    if package.get("name") != "@yylo/cli" or not managed_valid_package_version(version):
        raise AdoptionError("target source package identity is invalid")
    branch = adoption_git(controller, "symbolic-ref", "-q", "HEAD")
    old_executable = adoption_controller_config(controller, "juno.controller.runtimeExecutable")
    old_version = adoption_controller_config(controller, "juno.controller.runtimeVersion")
    old_identity = controller / ".juno_task/runtime/identity.json"
    launcher_before = adoption_public_launcher_preflight(old_executable)
    before = {"controller_head": adoption_git(controller, "rev-parse", "HEAD"),
              "controller_tree": adoption_git(controller, "write-tree"),
              "runtime_executable": old_executable, "runtime_version": old_version,
              "runtime_identity_sha256": adoption_digest(old_identity) if old_identity.is_file() else None,
              "managed_generation_sha256": adoption_digest(generation_path)}
    artifact = output.with_name(f"{output.stem}-{target_sha[:12]}.tgz")
    install_receipt = output.with_name(f"{output.stem}-install.json")
    rollback_receipt = output.with_name(f"{output.stem}-rollback.json")
    if any(path.exists() or path.is_symlink() for path in (artifact, install_receipt, rollback_receipt)):
        raise AdoptionError("source runtime adoption sidecar path already exists")
    # Full target declaration/start compatibility must pass before owner movement,
    # packing, installation or rebind. Script-byte equality is not admission.
    adoption_declaration_admission(controller, repository, target_sha)
    # Freeze owner rollback identity only after every output, prefix,
    # generation, package, controller, launcher, and ancestry preflight passes.
    owner_before = (adoption_preserved_owner(repository, owner) if isolated_source else
                    adoption_owner_preflight(repository, target_ref, target_sha, owner))

    def assert_source() -> None:
        if isolated_source:
            adoption_assert_preserved_source(repository, target_ref, target_sha, owner, owner_before)
        else:
            adoption_assert_target(repository, target_ref, target_sha, owner)

    if isolated_source:
        assert_source()
    rebound = False
    prefix_owned = False
    dispatch: dict[str, Any] | None = None
    try:
        if isolated_source:
            # No topology reconciliation or global task inactivity is needed:
            # neither retained worktrees nor task state are inputs to this build.
            adoption_pack_isolated(repository, target_sha, artifact)
        else:
            # Legacy owner movement remains explicit and inside rollback scope.
            adoption_prepare_owner(repository, target_ref, target_sha, owner, owner_before)
            with tempfile.TemporaryDirectory(prefix="yylo-source-pack-") as temporary:
                adoption_run(["npm", "pack", "--pack-destination", temporary], owner / "juno-code")
                packs = list(Path(temporary).glob("*.tgz"))
                if len(packs) != 1:
                    raise AdoptionError("npm pack did not produce exactly one source artifact")
                artifact.write_bytes(packs[0].read_bytes())
        assert_source()
        if os.environ.get("YYLO_SOURCE_ADOPTION_TEST_MUTATE_ARTIFACT") == "1":
            artifact.write_bytes(artifact.read_bytes() + b"drift")
        metadata = Path(__file__).with_name("metadata_controller.py")
        adoption_run([sys.executable, str(metadata), "runtime-install-rebind", "--root", str(controller),
             "--branch", branch, "--runtime-version", version, "--install-prefix", str(prefix),
             "--artifact", str(artifact), "--output", str(install_receipt)], controller)
        rebound = True
        install = adoption_read_json(install_receipt, "runtime install/rebind receipt")
        new_executable = Path(install.get("runtime", {}).get("executable", ""))
        if (install.get("install_prefix") != str(prefix) or not new_executable.is_file()):
            raise AdoptionError("runtime install receipt does not own the requested fresh prefix")
        ownership = prefix / ".juno-source-adoption-owner.json"
        marker = {"schema_version": SOURCE_ADOPTION_SCHEMA, "receipt": str(output),
                  "target_sha": target_sha,
                  "install_receipt_sha256": adoption_digest(install_receipt)}
        with ownership.open("x", encoding="utf-8") as stream:
            stream.write(adoption_canonical(marker).decode())
        prefix_owned = True
        dispatch = adoption_public_launchers(old_executable, new_executable, launcher_before)
        adoption_verify_public_dispatch(dispatch)
        assert_source()
        if os.environ.get("YYLO_SOURCE_ADOPTION_TEST_FAIL_AFTER_REBIND") == "1":
            raise AdoptionError("injected interruption after runtime rebind")
        refresh = managed_runtime_refresh(controller, repository, previous_sha, target_sha,
                                             task_id="source-adoption")
        assert_source()
        doctor = managed_runtime_inspect(controller, repository, target_sha)
        admission = adoption_task_start_admission(controller, repository, target_sha)
        if not doctor["healthy"] or not admission["current"]:
            raise AdoptionError("source runtime adoption did not reach task-start admission")
        # Script equality is source admission, not complete controller readiness.
        # In particular older rebinds may retain another instruction inventory.
        # Diagnose through the installed public boundary, never silently repair
        # customized preimages or claim that an unactivated generation is ready.
        generation_assessment = adoption_generation_assessment(controller, new_executable)
        payload = {"schema_version": SOURCE_ADOPTION_SCHEMA, "operation": "runtime-adopt-source",
                   "outcome": "completed",
                   "controller_generation": generation_assessment,
                   "controller_ready": generation_assessment.get('disposition') == 'ready',
                   "safe_next_action": ("yy task start TASK_ID" if generation_assessment.get('disposition') in {'ready', 'migration_required'}
                                        else "yy scripts generation doctor; review an explicit scripts generation repair-plan if the predecessor is mixed"), "controller": str(controller),
                   "repository": str(repository), "target_ref": target_ref,
                   "previous_sha": previous_sha, "target_sha": target_sha,
                   "package_version": version,
                   "artifact": {"path": str(artifact), "sha256": adoption_digest(artifact),
                                "size_bytes": artifact.stat().st_size},
                   "install_prefix": str(prefix), "install_receipt": {"path": str(install_receipt),
                                "sha256": adoption_digest(install_receipt)},
                   "refresh_receipt": refresh["receipt"], "doctor": {"healthy": True},
                   "runtime_generation": admission, "dispatch": dispatch,
                   "source_mode": "isolated" if isolated_source else "integration-owner",
                   "integration_owner": {"path": str(owner), "before": owner_before,
                                         "after_head": owner_before["head"] if isolated_source else target_sha},
                   "rollback_identity": before,
                   "duration_seconds": time.monotonic() - started,
                   "operator_steps": {"before": 5, "after": 2,
                                      "before_flow": "pack, install/rebind, refresh, doctor, task start",
                                      "after_flow": "runtime-adopt-source, task start"},
                   "product_ref_mutation": False, "publication": False}
        adoption_atomic_write(output, payload)
        return payload
    except BaseException as exc:
        rollback: dict[str, Any] = {"attempted": rebound or owner_before["moved"],
                                     "complete": not rebound and not owner_before["moved"]}
        try:
            dispatch_restored = True if dispatch is None else adoption_restore_public_launchers(dispatch)
            runtime_restored = not rebound
            receipt_reference = None
            if rebound:
                expected_new = dispatch["executable"] if dispatch else str(
                    prefix / "node_modules/@yylo/cli/dist/bin/cli.mjs")
                if adoption_controller_config(controller, "juno.controller.runtimeExecutable") == expected_new:
                    metadata = Path(__file__).with_name("metadata_controller.py")
                    adoption_run([sys.executable, str(metadata), "runtime-rebind", "--root", str(controller),
                         "--branch", branch, "--runtime", old_executable,
                         "--runtime-version", old_version, "--output", str(rollback_receipt)], controller)
                    runtime_restored = True
                    receipt_reference = {"path": str(rollback_receipt),
                                         "sha256": adoption_digest(rollback_receipt)}
            prefix_removed = not prefix.exists()
            if prefix_owned:
                marker = prefix / ".juno-source-adoption-owner.json"
                owned = marker.is_file() and adoption_read_json(
                    marker, "source runtime adoption prefix ownership").get("receipt") == str(output)
                if owned:
                    shutil.rmtree(prefix)
                prefix_removed = not prefix.exists()
            owner_restored = (adoption_preserved_owner(repository, owner) == owner_before
                              if isolated_source else adoption_restore_owner(owner, owner_before))
            rollback.update({"complete": bool(dispatch_restored and runtime_restored
                                               and prefix_removed and owner_restored
                                               and adoption_controller_config(controller, "juno.controller.runtimeExecutable") == old_executable
                                               and adoption_controller_config(controller, "juno.controller.runtimeVersion") == old_version),
                             "public_dispatch_restored": dispatch_restored,
                             "prefix_removed_by_owner": prefix_owned and prefix_removed,
                             "owner_restored": owner_restored})
            if receipt_reference:
                rollback["receipt"] = receipt_reference
        except BaseException as rollback_exc:
            rollback.update({"complete": False, "error": str(rollback_exc)})
        failure = {"schema_version": SOURCE_ADOPTION_SCHEMA, "operation": "runtime-adopt-source",
                   "outcome": "failed_rolled_back" if rollback["complete"] else "failed_rollback_incomplete",
                   "controller": str(controller), "repository": str(repository),
                   "previous_sha": previous_sha, "target_sha": target_sha,
                   "error": str(exc), "rollback": rollback, "rollback_identity": before,
                   "duration_seconds": time.monotonic() - started,
                   "operator_steps": {"before": 5, "after": 2},
                   "product_ref_mutation": False, "publication": False}
        if not output.exists():
            adoption_atomic_write(output, failure)
        raise AdoptionError(f"{exc}; rollback_complete={rollback['complete']}; receipt={output}") from exc


def source_runtime_adopt(args: argparse.Namespace) -> dict[str, Any]:
    controller = exact_root(args.controller, "controller")
    _, policy, _ = load_policy(controller)
    repository = task_workspace.product_repository(controller, policy).resolve()
    target_ref = policy["target_ref"]
    # One target lock serializes output/prefix preflight with every source
    # adoption and target mutation, closing receipt and ownership TOCTOU races.
    with integration_target_lock(repository, target_ref), managed_generation_mutation(controller):
        return _source_runtime_adopt_locked(
            args, controller, policy, repository, target_ref)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(allow_abbrev=False)
    root.add_argument("--controller", type=Path, default=Path.cwd())
    commands = root.add_subparsers(dest="operation", required=True)
    status = commands.add_parser("status", allow_abbrev=False)
    status.add_argument("--fetch", action="store_true")
    commands.add_parser("sync", allow_abbrev=False)
    runtime_doctor = commands.add_parser("runtime-doctor", allow_abbrev=False)
    runtime_doctor.add_argument("--target-sha")
    source_adopt = commands.add_parser("runtime-adopt-source", allow_abbrev=False)
    source_adopt.add_argument("--previous-sha", required=True)
    source_adopt.add_argument("--target-sha", required=True)
    source_adopt.add_argument("--install-prefix", type=Path, required=True)
    source_adopt.add_argument("--output", type=Path, required=True)
    source_adopt.add_argument("--isolated-source", action="store_true",
                              help="build an exact private source checkout; never move the retained owner")
    runtime_refresh = commands.add_parser("runtime-refresh", allow_abbrev=False)
    runtime_refresh.add_argument("--previous-sha", required=True)
    runtime_refresh.add_argument("--target-sha")
    repair_mode = runtime_refresh.add_mutually_exclusive_group()
    repair_mode.add_argument("--dry-run", action="store_true",
                             help="persist a non-mutating changed-source overlap repair plan")
    repair_mode.add_argument("--apply", type=Path,
                             help="apply one exact immutable overlap repair plan")
    register_command = commands.add_parser("register", allow_abbrev=False)
    register_command.add_argument("owner", type=Path)
    register_command.add_argument("--replace", action="store_true")
    register_command.add_argument("--runtime-executable", type=Path)
    register_command.add_argument("--runtime-version")
    for name in ("repair", "push"):
        command = commands.add_parser(name, allow_abbrev=False)
        mode = command.add_mutually_exclusive_group(required=name == "repair")
        mode.add_argument("--dry-run", action="store_true")
        mode.add_argument("--apply", type=Path)
        if name == "repair":
            command.add_argument("--canonical-owner-refresh", action="store_true",
                                 help="explicit offline non-legacy owner refresh; requires reviewed preserve-only inventory")
            command.add_argument("--preserve-owners", type=Path,
                                 help="owner approval JSON: approved_by, disposition=preserve-only, inventory_sha256")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.operation == "status":
            payload, code = status_payload(args.controller, fetch=args.fetch), 0
        elif args.operation == "sync":
            payload, code = sync(args.controller)
        elif args.operation == "runtime-adopt-source":
            payload, code = source_runtime_adopt(args), 0
        elif args.operation in {"runtime-doctor", "runtime-refresh"}:
            controller = exact_root(args.controller, "controller")
            _, task_policy, _ = load_policy(controller)
            repository = task_workspace.product_repository(controller, task_policy)
            target_sha = args.target_sha or sha(repository, task_policy["target_ref"])
            if not target_sha:
                raise IntegrationError("managed runtime target commit is unavailable")
            if args.operation == "runtime-doctor":
                payload = managed_runtime_inspect(controller, repository, target_sha)
                code = 0 if payload["healthy"] else 2
            elif args.dry_run:
                payload = managed_runtime_repair_plan(
                    controller, repository, args.previous_sha, target_sha, task_id="manual")
                code = 0 if payload["outcome"] == "planned" else 2
            else:
                payload = managed_runtime_refresh(
                    controller, repository, args.previous_sha, target_sha, task_id="manual",
                    repair_receipt=args.apply)
                code = 0
        elif args.operation == "register":
            payload, code = register(
                args.controller, args.owner, replace=args.replace,
                runtime_executable=args.runtime_executable, runtime_version=args.runtime_version)
        elif args.operation == "repair":
            payload, code = repair(args.controller, dry_run=args.dry_run, apply=args.apply,
                                   canonical_refresh=args.canonical_owner_refresh,
                                   preserve_owners=args.preserve_owners)
        else:
            payload, code = push(args.controller, dry_run=args.dry_run, apply=args.apply)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return code
    except (IntegrationError, ManagedRuntimeError, AdoptionError,
            task_workspace.TaskWorkspaceError, OSError, json.JSONDecodeError) as exc:
        print(f"integration-workspace: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
