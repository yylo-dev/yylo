#!/usr/bin/env python3
"""Package-owned controller generation maintenance; never execute old local scripts.

The plan API is read-only. Apply/resume/rollback share an exclusive generation lock.
A durable fence covers every multi-file activation; dispatchers must use assert_ready
before selecting local runtime bytes (first-use dispatch is a separate consumer).
Trust inputs are exact installed npm artifacts with an externally verified digest,
not a package-name alias or a mutable directory claiming to be a release.
"""
from __future__ import annotations

import argparse
import ast
import base64
import copy
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import secrets
import subprocess
import sys
import tarfile
import tempfile
from contextlib import contextmanager
from typing import Any

import task_workspace as compatibility
import metadata_controller as endpoints

SCHEMA = "yylo_controller_generation_transaction.v1"
ROOT = ".juno_task/runtime/generation-migration"
INVENTORY = ".juno_task/managed-assets.json"
POLICY = ".juno_task/config/metadata-controller.json"
IDENTITY = ".juno_task/runtime/identity.json"
STATE = ".juno_task/state/tasks.json"
CONFIG = "@worktree-config"
CURRENT = ROOT + "/current.json"
LIMIT = 64 * 1024 * 1024


class Refusal(RuntimeError):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encoded(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def decode_json(data: bytes) -> Any:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise Refusal("duplicate_identity_key", key)
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=unique)


def relative(value: str) -> str:
    if (not isinstance(value, str) or not value or "\\" in value or "\x00" in value
            or PurePosixPath(value).is_absolute() or PurePosixPath(value).as_posix() != value
            or any(p in {"..", ".git"} for p in value.split("/"))):
        raise Refusal("unsafe_destination", str(value))
    return value


def safe(root: Path, name: str) -> Path:
    cursor = root
    for part in relative(name).split("/"):
        cursor = cursor / part
        if cursor.is_symlink():
            raise Refusal("unsafe_symlink", str(cursor))
    return cursor


def snapshot(path: Path) -> dict[str, Any] | None:
    try:
        entry = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1 or entry.st_size > LIMIT:
        raise Refusal("unsafe_file", str(path))
    data = path.read_bytes()
    after = path.stat()
    if (entry.st_ino, entry.st_mtime_ns, entry.st_size) != (after.st_ino, after.st_mtime_ns, after.st_size):
        raise Refusal("concurrent_edit", str(path))
    return {"sha256": digest(data), "mode": stat.S_IMODE(entry.st_mode),
            "bytes": base64.b64encode(data).decode()}


def content(value: dict[str, Any]) -> bytes:
    data = base64.b64decode(value["bytes"], validate=True)
    if digest(data) != value["sha256"]:
        raise Refusal("journal_corrupt", "content digest mismatch")
    return data


def image(data: bytes, mode: int = 0o644) -> dict[str, Any]:
    return {"sha256": digest(data), "mode": mode, "bytes": base64.b64encode(data).decode()}


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise Refusal("registration_unavailable", result.stderr.strip())
    return result.stdout.strip()


def fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def durable_directory(path: Path) -> None:
    missing = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    for directory in reversed(missing):
        directory.mkdir(exist_ok=True)
        fsync_dir(directory.parent)
        fsync_dir(directory)


def publish(path: Path, data: bytes, mode: int = 0o600, immutable: bool = False) -> None:
    durable_directory(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=".generation-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        if immutable:
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.is_symlink() or path.read_bytes() != data:
                    raise Refusal("receipt_collision", str(path))
        else:
            os.replace(temporary, path)
        fsync_dir(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def authenticate(evidence: dict[str, str]) -> dict[str, Any]:
    if set(evidence) != {"root", "artifact", "sha256"} or not re.fullmatch(r"[0-9a-f]{64}", evidence["sha256"]):
        raise Refusal("package_provenance_invalid", "exact artifact/root/digest required")
    root = Path(evidence["root"]).absolute()
    if root != root.resolve() or not root.is_dir():
        raise Refusal("package_provenance_invalid", "installed root must be real")
    artifact = Path(evidence["artifact"]).absolute()
    if artifact != artifact.resolve():
        raise Refusal("package_provenance_invalid", "artifact path must not contain symlinks")
    for directory in (root, artifact.parent):
        probe = subprocess.run(["git", "-C", str(directory), "rev-parse", "--absolute-git-dir"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=30)
        if probe.returncode == 0:
            raise Refusal("package_provenance_invalid", "installed artifact must be outside mutable Git worktrees")
    packed = snapshot(artifact)
    if packed is None or packed["sha256"] != evidence["sha256"]:
        raise Refusal("package_provenance_invalid", "artifact hash mismatch")
    files = {}
    total = 0
    with tarfile.open(fileobj=io.BytesIO(content(packed)), mode="r:gz") as archive:
        for member in archive:
            name = relative(member.name.rstrip("/"))
            if not name.startswith("package/") or (not member.isdir() and not member.isfile()):
                raise Refusal("package_provenance_invalid", "unsafe archive member")
            if member.isdir():
                continue
            name = relative(name[len("package/"):])
            if name in files:
                raise Refusal("package_provenance_invalid", "duplicate archive member")
            total += member.size
            if total > LIMIT or len(files) > 10000:
                raise Refusal("package_provenance_invalid", "archive exceeds bounds")
            data = archive.extractfile(member).read()
            installed = snapshot(safe(root, name))
            if installed is None or content(installed) != data:
                raise Refusal("package_provenance_invalid", f"installed package differs: {name}")
            files[name] = data
    for directory, directories, filenames in os.walk(root / "dist", followlinks=False):
        for entry in directories + filenames:
            path = Path(directory) / entry
            name = path.relative_to(root).as_posix()
            if path.is_symlink() or (not path.is_dir() and name not in files):
                raise Refusal("package_provenance_invalid", f"unverified installed execution entry: {name}")
    for name in ("package.json", "dist/bin/cli.mjs", "dist/templates/managed-assets.json"):
        if name not in files:
            raise Refusal("package_provenance_invalid", f"missing {name}")
    package = decode_json(files["package.json"])
    if not compatibility.is_valid_semver(package.get("version")):
        raise Refusal("package_provenance_invalid", "invalid package version")
    return {"evidence": evidence, "files": files, "package": package,
            "executable": str(root / "dist/bin/cli.mjs")}


def assets(package: dict[str, Any]) -> dict[str, dict[str, Any]]:
    declaration = decode_json(package["files"]["dist/templates/managed-assets.json"])
    if not compatibility.instruction_declaration_compatible(declaration.get("schemaVersion"),
                                                            declaration.get("instructionBundle")):
        raise Refusal("instruction_bundle_incompatible", "package declaration")
    result = {}
    rows = [row for row in declaration.get("assets", []) if row.get("type") != "config"]
    rows += declaration.get("controllerOutputs", [])
    for row in rows:
        name, source = relative(row["destination"]), relative(row["source"])
        if name in result:
            raise Refusal("duplicate_destination", name)
        if name.startswith((".pi/skills/", ".claude/skills/", ".agents/skills/")):
            raise Refusal("independent_skill_ownership", name)
        if not (name in {"AGENTS.md", "CLAUDE.md"} or name.startswith(
                (".juno_task/scripts/", ".juno_task/prompts/", ".juno_task/wiki/", ".juno_task/workflows/"))):
            raise Refusal("unsupported_write_ownership", name)
        key = "dist/templates/" + source
        if key not in package["files"]:
            raise Refusal("package_provenance_invalid", f"missing declared source {key}")
        result[name] = {"data": package["files"][key], "type": row["type"]}
    # Package helper scripts are part of executable closure, not independent skills.
    for source, data in package["files"].items():
        prefix = "dist/templates/scripts/"
        if source.startswith(prefix) and source.endswith((".py", ".sh")) and "/tests/" not in source:
            name = ".juno_task/scripts/" + relative(source[len(prefix):])
            if name not in result:
                result[name] = {"data": data, "type": "script"}
    return result


def state_schemas(package: dict[str, Any]) -> set[str]:
    data = package["files"].get("dist/templates/scripts/task_workspace.py", b"")
    tree = ast.parse(data)
    return {node.value.value for node in tree.body if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
            and any(isinstance(target, ast.Name) and target.id in {"STATE_SCHEMA", "BOUNDED_STATE_SCHEMA"}
                    for target in node.targets)}


def registration(root: Path) -> dict[str, str]:
    if Path(git(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise Refusal("registration_invalid", "controller must be exact Git root")
    if Path(git(root, "config", "--local", "--get", "juno.controller.path")).resolve() != root:
        raise Refusal("registration_invalid", "not canonical controller")
    if git(root, "config", "--worktree", "--get", "juno.workspace.role") != "controller":
        raise Refusal("registration_invalid", "not controller role")
    task = decode_json(content(snapshot(safe(root, ".juno_task/config/task-workspace.json"))))
    branch = git(root, "symbolic-ref", "-q", "HEAD")
    expected = git(root, "config", "--local", "--get", "juno.controller.branch")
    if expected.removeprefix("refs/heads/") != branch.removeprefix("refs/heads/"):
        raise Refusal("registration_invalid", "controller branch mismatch")
    return {"branch": branch, "target_ref": task["target_ref"],
            "target_sha": git(root, "rev-parse", f"{task['target_ref']}^{{commit}}"),
            "config_path": git(root, "rev-parse", "--path-format=absolute", "--git-path", "config.worktree")}


def path_for(root: Path, name: str, authority: dict[str, str]) -> Path:
    return Path(authority["config_path"]) if name == CONFIG else safe(root, name)


def runtime_identity(package: dict[str, Any]) -> dict[str, Any]:
    return {"package": package["package"]["name"], "version": package["package"]["version"],
            "executable": package["executable"],
            "executable_sha256": digest(package["files"]["dist/bin/cli.mjs"]),
            "source": "installed-release", "tracked": False}


def admission(root: Path, package: dict[str, Any], proposal: dict[str, Any], authority: dict[str, str],
              operational: bool = False) -> None:
    """Use the candidate's full policy/declaration readers, never the old local runtime."""
    with tempfile.TemporaryDirectory(prefix="yylo-generation-admission-") as temporary:
        projected = root if operational else Path(temporary) / "projected"
        closure = Path(temporary) / "authenticated"
        for name, data in package["files"].items():
            if name.startswith("dist/templates/"):
                destination = closure / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
        if not operational:
            for name, data in proposal.items():
                if name == CONFIG:
                    continue
                path = safe(projected, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content(data))
        code = '''import sys,json
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import task_workspace as t
import metadata_controller as m
root,repository,target=Path(sys.argv[2]),Path(sys.argv[3]),sys.argv[4]
m.load_policy(root/'.juno_task/config/metadata-controller.json')
c=t.load_config(root)
t.derived_output_admission(repository,target,c['allowed_paths'])
t.require_current_runtime(repository,target,root)
assert t._managed_inventory_identity_valid(json.loads((root/'.juno_task/managed-assets.json').read_text()))
'''
        result = subprocess.run([sys.executable, "-I", "-B", "-X", f"pycache_prefix={temporary}/bytecode", "-c", code,
                                 str(closure / "dist/templates/scripts"),
                                 str(projected), str(root), authority["target_sha"]],
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
        if result.returncode:
            raise Refusal("proposed_admission_failed", result.stderr.decode(errors="replace")[-4000:])


def plan(root: Path, candidate_evidence: dict[str, str], previous_evidence: dict[str, str]) -> dict[str, Any]:
    root = root.resolve()
    assert_ready(root)
    return prepare(root, candidate_evidence, previous_evidence)


def prepare(root: Path, candidate_evidence: dict[str, str], previous_evidence: dict[str, str],
            frozen: dict[str, Any] | None = None, attempt: str | None = None) -> dict[str, Any]:
    """Pure preparation, also used to authenticate recovery against frozen preimages."""
    authority = registration(root)
    def observe(name):
        if frozen is not None and name in frozen:
            value = frozen[name]
            if value is not None:
                content(value)
            return value
        return snapshot(path_for(root, name, authority))
    candidate, previous = authenticate(candidate_evidence), authenticate(previous_evidence)
    identity = decode_json(content(observe(IDENTITY)))
    if identity != runtime_identity(previous):
        raise Refusal("previous_identity_unverified", "registered identity does not authenticate previous package")
    if candidate["package"]["name"] != "@yylo/cli":
        raise Refusal("candidate_identity_unsupported", "candidate must be @yylo/cli")
    old_name, old_version = previous["package"]["name"], previous["package"]["version"]
    config_before = observe(CONFIG)
    with tempfile.TemporaryDirectory(prefix="yylo-generation-selector-") as temporary:
        selector = Path(temporary) / "config"
        selector.write_bytes(content(config_before))
        selected = git(root, "config", "--file", str(selector), "--get", "juno.controller.runtimeExecutable")
        selected_version = git(root, "config", "--file", str(selector), "--get", "juno.controller.runtimeVersion")
    if selected != previous["executable"] or selected_version != old_version:
        raise Refusal("previous_identity_unverified", "registered selector differs from previous package")
    if old_name == "juno-code" and old_version == "2.1.3-rc.0.32":
        adapter = "juno-code-2.1.3-rc.0.32"
    elif old_name == "@yylo/cli":
        source = subprocess.run(["git", "-C", str(root), "cat-file", "-e",
                                 authority["target_sha"] + ":juno-code/package.json"],
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
        adapter = "yylo-source-controller" if source.returncode == 0 else "yylo-installed"
    else:
        raise Refusal("historical_identity_unsupported", f"{old_name}@{old_version}")
    old_assets, new_assets = assets(previous), assets(candidate)
    inventory_image = observe(INVENTORY)
    inventory = decode_json(content(inventory_image))
    if inventory.get("packageName") != old_name or inventory.get("packageVersion") != old_version:
        raise Refusal("previous_inventory_unverified", "package mismatch")
    check = copy.deepcopy(inventory)
    if adapter == "juno-code-2.1.3-rc.0.32" and check.get("schemaVersion") == 1:
        check["packageName"] = "@yylo/cli"  # bounded adapter AFTER artifact and version verification
    if not compatibility._managed_inventory_identity_valid(check):
        raise Refusal("previous_inventory_unverified", "malformed or forged identity")
    before, after = {}, {}
    for name, record in inventory["assets"].items():
        relative(name)
        if name not in old_assets and name != POLICY:
            continue  # no write ownership over independent skills or localized config
        observed = observe(name)
        if observed is None or observed["sha256"] != record["installedSha256"]:
            raise Refusal("managed_preimage_modified", name)
        if name in old_assets and (observed["sha256"] != digest(old_assets[name]["data"])
                                  or record["sourceSha256"] != observed["sha256"]):
            raise Refusal("previous_inventory_unverified", name)
    for name in sorted(set(old_assets) | set(new_assets)):
        observed = observe(name)
        if name in old_assets and observed is not None:
            if observed["sha256"] != digest(old_assets[name]["data"]):
                raise Refusal("managed_preimage_modified", name)
        elif observed is not None:
            raise Refusal("new_destination_occupied", name)
        before[name] = observed
        if name in new_assets:
            after[name] = image(new_assets[name]["data"], 0o755 if name.endswith((".py", ".sh")) else 0o644)
        else:
            # Retired assets stay intact, outside the newly active inventory.
            before.pop(name)
    policy_before = observe(POLICY)
    policy = decode_json(content(policy_before))
    if policy.get("runtime", {}).get("package") != old_name:
        raise Refusal("policy_identity_mismatch", "runtime package")
    if policy.get("controller_branch") != authority["branch"] or policy.get("product_ref") != authority["target_ref"]:
        raise Refusal("policy_identity_mismatch", "target/controller reference")
    policy["runtime"]["package"] = "@yylo/cli"
    # Only exact package-declared additions; no wildcard ownership expansion.
    for key in ("generated_metadata", "tracked_exact"):
        policy[key] = sorted(set(policy[key]) | {name for name in new_assets
                            if name not in {"AGENTS.md", "CLAUDE.md"} and not name.startswith(".juno_task/scripts/")})
    new_inventory = {"schemaVersion": 2, "packageName": "@yylo/cli",
                     "packageVersion": candidate["package"]["version"], "assets": {},
                     "instructionBundle": {"semanticVersion": decode_json(candidate["files"][
                         "dist/templates/managed-assets.json"])["instructionBundle"]["semanticVersion"]}}
    for name, row in new_assets.items():
        new_inventory["assets"][name] = {"type": row["type"], "templateVersion": new_inventory["packageVersion"],
                                          "sourceSha256": digest(row["data"]), "installedSha256": digest(row["data"])}
    compatibility._bind_instruction_bundle_identity(new_inventory)
    before.update({POLICY: policy_before, INVENTORY: inventory_image, IDENTITY: observe(IDENTITY)})
    after.update({POLICY: image(encoded(policy)), INVENTORY: image(encoded(new_inventory)),
                  IDENTITY: image(encoded(runtime_identity(candidate)))})
    with tempfile.TemporaryDirectory(prefix="yylo-generation-config-") as temporary:
        config = Path(temporary) / "config"
        config.write_bytes(content(config_before))
        for key, value in (("juno.controller.runtimeExecutable", candidate["executable"]),
                           ("juno.controller.runtimeVersion", candidate["package"]["version"])):
            subprocess.run(["git", "config", "--file", str(config), "--replace-all", key, value], check=True)
        after[CONFIG] = image(config.read_bytes(), config_before["mode"])
    before[CONFIG] = config_before
    before[CURRENT] = observe(CURRENT)
    current = {}
    if before[CURRENT]:
        current = decode_json(content(before[CURRENT]))
        if (current.get("schema_version") != "yylo_controller_generation.v1"
                or current.get("candidate") != previous_evidence
                or current.get("runtime") != runtime_identity(previous)
                or current.get("inventory_sha256") != inventory_image["sha256"]):
            raise Refusal("current_generation_unverified", "preserve unknown generation marker")
    after[CURRENT] = image(encoded({"schema_version": "yylo_controller_generation.v1",
        "candidate": candidate_evidence, "previous": previous_evidence,
        "inventory_sha256": after[INVENTORY]["sha256"],
        "runtime": runtime_identity(candidate), "target": authority["target_sha"],
        "retained_runtime": {"executable": previous["executable"],
            "package_scripts": str(Path(previous_evidence["root"]) / "dist/templates/scripts")}}))
    guards = {}
    for name in (".juno_task/config/task-workspace.json", STATE):
        guards[name] = observe(name)
    state = decode_json(content(guards[STATE])) if guards[STATE] else {"tasks": {}}
    pins = {name: {"state": value.get("state"), "attempt": value.get("fencing", {}).get("attempt"),
                   "executable": previous["executable"], "generation": previous_evidence}
            for name, value in state["tasks"].items()
            if value.get("state") in {"WORKING", "HYDRATING", "HYDRATION_FAILED"}
            or value.get("fencing", {}).get("state") == "ACTIVE"}
    if guards[STATE] and state.get("schema_version") not in state_schemas(candidate) & state_schemas(previous):
        raise Refusal("shared_state_incompatible", "defer migration; preserve active attempts")
    prior_pins = current.get("active_pins", {})
    if not isinstance(prior_pins, dict):
        raise Refusal("active_pin_unverified", "invalid retained pin map")
    for name, pin in pins.items():
        prior = prior_pins.get(name)
        if prior is not None and prior.get("attempt") == pin["attempt"]:
            retained = authenticate(prior["generation"])
            if (prior.get("executable") != retained["executable"]
                    or state.get("schema_version") not in state_schemas(retained)):
                raise Refusal("active_pin_unverified", name)
            pins[name] = prior
    marker = decode_json(content(after[CURRENT]))
    marker["active_pins"] = pins
    after[CURRENT] = image(encoded(marker))
    proposed = {**{key: value for key, value in guards.items() if value}, **after}
    admission(root, candidate, proposed, authority)
    body = {"schema_version": SCHEMA, "controller": str(root), "authority": authority, "adapter": adapter,
            "candidate": candidate_evidence, "previous": previous_evidence, "before": before, "after": after,
            "guards": guards, "active_pins": pins, "attempt": attempt or secrets.token_hex(16)}
    return {**body, "id": digest(encoded(body))}


def assert_ready(root: Path) -> None:
    if safe(root, ROOT + "/fence.json").exists():
        raise Refusal("generation_transition_incomplete", "resume or roll back the exact journal; do not refresh")


@contextmanager
def locked(root: Path):
    path = safe(root, ROOT + "/lock")
    durable_directory(path.parent)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Refusal("generation_migration_busy", "another migration owns the lock") from exc
        yield
    finally:
        os.close(fd)


def check_plan(value: dict[str, Any], root: Path, rollback: bool = False) -> None:
    body = {key: item for key, item in value.items() if key != "id"}
    if value.get("schema_version") != SCHEMA or value.get("controller") != str(root) or value.get("id") != digest(encoded(body)):
        raise Refusal("journal_corrupt", "plan identity mismatch")
    if not isinstance(value.get("attempt"), str) or not re.fullmatch(r"[0-9a-f]{32}", value["attempt"]):
        raise Refusal("journal_corrupt", "invalid transaction attempt")
    if registration(root) != value["authority"]:
        raise Refusal("registration_changed", "controller or product target moved")
    for name, expected in value["guards"].items():
        observed = snapshot(safe(root, name))
        if observed != expected:
            if rollback and name == STATE and observed and expected:
                # A heartbeat/new compatible attempt is not ours to undo or erase.
                if decode_json(content(observed)).get("schema_version") == decode_json(content(expected)).get("schema_version"):
                    continue
            raise Refusal("shared_state_changed", name)


def receipt_value(root: Path, value: dict[str, Any], outcome: str) -> dict[str, Any]:
    return {"schema_version": SCHEMA, "id": value["id"], "outcome": outcome,
               "candidate": value["candidate"], "previous": value["previous"], "active_pins": value["active_pins"],
               "retained_runtime": {"executable": str(Path(value["previous"]["root"]) / "dist/bin/cli.mjs"),
                   "package_scripts": str(Path(value["previous"]["root"]) / "dist/templates/scripts"),
                   "controller_preimages": str(root / ROOT / value["id"] / "previous")}}


def result(root: Path, value: dict[str, Any], outcome: str) -> dict[str, Any]:
    receipt = receipt_value(root, value, outcome)
    publish(safe(root, ROOT + "/" + value["id"] + "/" + outcome + ".json"), encoded(receipt), immutable=True)
    return receipt


def publish_endpoint(root: Path, value: dict[str, Any], name: str,
                     old: dict[str, Any] | None, new: dict[str, Any] | None,
                     rollback: bool, boundary=None, index: int = 0) -> None:
    """Reuse descriptor-bound no-clobber/exchange maintenance primitives.

    Retired/displaced bytes stay in the transaction quarantine. A racing editor's
    inode is never silently discarded by replacement or rollback unlink.
    """
    destination = path_for(root, name, value["authority"])
    durable_directory(destination.parent)
    if name != CONFIG:
        safe(root, name)
    quarantine = safe(root, ROOT + "/" + value["id"] + "/displaced")
    durable_directory(quarantine)
    directory_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    quarantine_fd = os.open(quarantine, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary_name = f".{destination.name}.generation-{value['id'][:16]}-{'rollback' if rollback else 'apply'}"
    try:
        expected = endpoints.endpoint_snapshot_at(directory_fd, destination.name)
        observed = None if expected is None else image(expected[0], stat.S_IMODE(expected[1][2]))
        if observed != old:
            raise Refusal("rollback_race" if rollback else "activation_race", name)
        if new is None:
            if expected is not None:
                endpoints.exact_unlink_endpoint_at(directory_fd, destination.name, expected, quarantine_fd)
        else:
            temporary = destination.parent / temporary_name
            prior_temporary = endpoints.endpoint_snapshot_at(directory_fd, temporary_name)
            if prior_temporary is not None:
                prior_image = image(prior_temporary[0], stat.S_IMODE(prior_temporary[1][2]))
                if prior_image not in (old, new):
                    raise Refusal("activation_race", "unowned temporary endpoint")
                endpoints.exact_unlink_endpoint_at(directory_fd, temporary_name, prior_temporary, quarantine_fd)
            publish(temporary, content(new), new["mode"], immutable=True)
            if boundary:
                boundary(f"staged:{index}")
            endpoints.atomic_endpoint_publish(directory_fd, temporary_name, destination.name, expected, quarantine_fd)
        os.fsync(directory_fd)
    except endpoints.BoundaryError as exc:
        raise Refusal("rollback_race" if rollback else "activation_race", str(exc)) from exc
    finally:
        os.close(directory_fd)
        os.close(quarantine_fd)


def retire_temporary(root: Path, value: dict[str, Any], name: str, rollback: bool) -> None:
    destination = path_for(root, name, value["authority"])
    temporary_name = f".{destination.name}.generation-{value['id'][:16]}-{'rollback' if rollback else 'apply'}"
    temporary = destination.parent / temporary_name
    if not temporary.exists() and not temporary.is_symlink():
        return
    observed = snapshot(temporary)
    if observed not in (value["before"][name], value["after"][name]):
        raise Refusal("activation_race", "unowned leftover temporary endpoint")
    quarantine = safe(root, ROOT + "/" + value["id"] + "/displaced")
    durable_directory(quarantine)
    directory_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    quarantine_fd = os.open(quarantine, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        expected = endpoints.endpoint_snapshot_at(directory_fd, temporary_name)
        if expected is not None:
            if image(expected[0], stat.S_IMODE(expected[1][2])) != observed:
                raise Refusal("activation_race", temporary_name)
            endpoints.exact_unlink_endpoint_at(directory_fd, temporary_name, expected, quarantine_fd)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
        os.close(quarantine_fd)


def transition(root: Path, value: dict[str, Any], rollback: bool = False, boundary=None) -> dict[str, Any]:
    """Resumable exact-preimage writes. A foreign edit leaves the fence and backups intact."""
    check_plan(value, root, rollback=rollback)
    candidate = authenticate(value["candidate"])
    authenticate(value["previous"])
    journal = ROOT + "/" + value["id"]
    fence = safe(root, ROOT + "/fence.json")
    if not fence.exists() or decode_json(fence.read_bytes()).get("id") != value["id"]:
        raise Refusal("fence_mismatch", "recovery requires the owned durable fence")
    if rollback and safe(root, journal + "/completed.json").exists():
        raise Refusal("transaction_already_completed", "a completed generation requires a new reviewed transition")
    source, target = (value["after"], value["before"]) if rollback else (value["before"], value["after"])
    names = list(reversed(sorted(target))) if rollback else sorted(target)
    # Reject all conflicting paths before continuing a partially applied journal.
    for name in names:
        observed = snapshot(path_for(root, name, value["authority"]))
        if observed not in (source[name], target[name]):
            raise Refusal("rollback_race" if rollback else "activation_race", name)
    for index, name in enumerate(names):
        destination = path_for(root, name, value["authority"])
        expected = target[name]
        observed = snapshot(destination)
        if observed == expected:
            retire_temporary(root, value, name, False)
            retire_temporary(root, value, name, True)
            continue
        if observed != source[name]:
            raise Refusal("rollback_race" if rollback else "activation_race", name)
        publish_endpoint(root, value, name, source[name], expected, rollback, boundary, index)
        retire_temporary(root, value, name, not rollback)
        if boundary:
            boundary(f"write:{index}")
    if not rollback:
        proposed = {**{key: item for key, item in value["guards"].items() if item}, **value["after"]}
        admission(root, candidate, proposed, value["authority"], operational=True)
    check_plan(value, root, rollback=rollback)
    for name in names:
        if snapshot(path_for(root, name, value["authority"])) != target[name]:
            raise Refusal("operational_readback_failed", name)
    if boundary:
        boundary("readback")
    receipt = result(root, value, "rolled_back" if rollback else "completed")
    if boundary:
        boundary("receipt")
    release_fence(root, value)
    return receipt


def release_fence(root: Path, value: dict[str, Any]) -> None:
    fence = safe(root, ROOT + "/fence.json")
    expected_fence = snapshot(fence)
    if expected_fence is None or content(expected_fence) != encoded({"id": value["id"], "schema_version": SCHEMA}):
        raise Refusal("fence_mismatch", "fence changed before release")
    quarantine = safe(root, ROOT + "/" + value["id"] + "/displaced")
    durable_directory(quarantine)
    directory_fd = os.open(fence.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    quarantine_fd = os.open(quarantine, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        observed = endpoints.endpoint_snapshot_at(directory_fd, fence.name)
        if observed is None or image(observed[0], stat.S_IMODE(observed[1][2])) != expected_fence:
            raise Refusal("fence_mismatch", "fence changed at release")
        endpoints.exact_unlink_endpoint_at(directory_fd, fence.name, observed, quarantine_fd)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
        os.close(quarantine_fd)


def apply(root: Path, value: dict[str, Any], boundary=None) -> dict[str, Any]:
    root = root.resolve()
    with locked(root):
        assert_ready(root)
        check_plan(value, root)
        # Recompute from authenticated packages and live preimages under the lock.
        if prepare(root, value["candidate"], value["previous"], attempt=value["attempt"]) != value:
            raise Refusal("plan_stale", "exact proposed generation changed")
        journal = ROOT + "/" + value["id"]
        if any(safe(root, journal + "/" + outcome + ".json").exists()
               for outcome in ("completed", "rolled_back")):
            raise Refusal("transaction_terminal", "prepare a fresh attempt; terminal journals cannot be reused")
        publish(safe(root, journal + "/intent.json"), encoded(value), immutable=True)
        if boundary:
            boundary("intent")
        retain_preimages(root, value)
        publish(safe(root, ROOT + "/fence.json"), encoded({"id": value["id"], "schema_version": SCHEMA}), immutable=True)
        if boundary:
            boundary("fence")
        return transition(root, value, boundary=boundary)


def retain_preimages(root: Path, value: dict[str, Any]) -> None:
    for name, old in value["before"].items():
        if old is not None:
            backup = "worktree-config" if name == CONFIG else name
            publish(safe(root, ROOT + "/" + value["id"] + "/previous/" + backup),
                    content(old), old["mode"], immutable=True)


def recover(root: Path, transaction_id: str, rollback: bool = False, boundary=None) -> dict[str, Any]:
    root = root.resolve()
    if not re.fullmatch(r"[0-9a-f]{64}", transaction_id):
        raise Refusal("journal_corrupt", "invalid transaction ID")
    with locked(root):
        value = decode_json(safe(root, ROOT + "/" + transaction_id + "/intent.json").read_bytes())
        outcomes = [outcome for outcome in ("completed", "rolled_back")
                    if safe(root, ROOT + "/" + transaction_id + "/" + outcome + ".json").exists()]
        if len(outcomes) > 1:
            raise Refusal("journal_corrupt", "contradictory terminal receipts")
        terminal = outcomes[0] if outcomes else None
        if terminal:
            rollback = terminal == "rolled_back"
        check_plan(value, root, rollback=rollback)
        if transaction_id != value["id"]:
            raise Refusal("journal_corrupt", "journal path differs from identity")
        # A self-hash is integrity, not write authority. Reconstruct the complete
        # authenticated adapter output against frozen preimages before any writes.
        reconstructed = prepare(root, value["candidate"], value["previous"],
                                frozen={**value["before"], **value["guards"]}, attempt=value["attempt"])
        if reconstructed != value:
            raise Refusal("journal_authority_invalid", "write set is not the authenticated generation projection")
        fence = safe(root, ROOT + "/fence.json")
        if terminal:
            receipt_path = safe(root, ROOT + "/" + transaction_id + "/" + terminal + ".json")
            receipt = decode_json(receipt_path.read_bytes())
            if receipt != receipt_value(root, value, terminal):
                raise Refusal("journal_corrupt", "terminal receipt differs from intent")
            expected = value["before"] if rollback else value["after"]
            for name, item in expected.items():
                if snapshot(path_for(root, name, value["authority"])) != item:
                    raise Refusal("operational_readback_failed", name)
            if fence.exists():
                release_fence(root, value)
            return receipt
        retain_preimages(root, value)
        if not fence.exists():
            # Intent was durable but activation never started. Revalidate and fence.
            for name, old in value["before"].items():
                if snapshot(path_for(root, name, value["authority"])) != old:
                    raise Refusal("plan_stale", name)
            publish(fence, encoded({"id": value["id"], "schema_version": SCHEMA}), immutable=True)
        return transition(root, value, rollback=rollback, boundary=boundary)


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("operation", choices=["plan", "apply", "resume", "rollback", "ready", "hold-lock"])
    parser.add_argument("--controller", type=Path, required=True)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--transaction-id")
    args = parser.parse_args()
    try:
        request = decode_json(args.request.read_bytes()) if args.request else {}
        if args.operation == "hold-lock":
            with locked(args.controller.resolve()):
                assert_ready(args.controller.resolve())
                print(json.dumps({"locked": True}), flush=True)
                sys.stdin.readline()  # release on explicit close or parent process death
            return 0
        if args.operation == "plan":
            answer = plan(args.controller, request["candidate"], request["previous"])
        elif args.operation == "apply":
            answer = apply(args.controller, request)
        elif args.operation in {"resume", "rollback"}:
            answer = recover(args.controller, args.transaction_id, args.operation == "rollback")
        else:
            assert_ready(args.controller)
            answer = {"ready": True}
        print(json.dumps(answer, sort_keys=True))
        return 0
    except (Refusal, endpoints.BoundaryError, OSError, ValueError, KeyError, TypeError, SyntaxError,
            tarfile.TarError, subprocess.SubprocessError) as exc:
        print(json.dumps({"outcome": "refused", "code": getattr(exc, "code", "invalid_generation"),
                          "detail": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
