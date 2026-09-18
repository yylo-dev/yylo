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
import errno
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
import time
from contextlib import contextmanager
from typing import Any

# Maintenance is package-owned, not a controller-local managed script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
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


def observed_bytes(path: Path) -> tuple[bytes, int] | None:
    """Read one stable regular file without serializing a transaction image.

    Authentication compares these bytes directly with the authenticated archive.
    Journal producers still use snapshot() and retain the exact image contract.
    """
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
    return data, stat.S_IMODE(entry.st_mode)


def snapshot(path: Path) -> dict[str, Any] | None:
    observed = observed_bytes(path)
    return image(*observed) if observed is not None else None


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


def assert_outside_git(directory: Path) -> None:
    # Git exit 128 is ambiguous (not-a-repository, dubious ownership, corrupt
    # metadata). Prove absence of repository markers without parsing stderr.
    for parent in (directory, *directory.parents):
        for name in ('.git', 'HEAD'):
            try:
                (parent / name).lstat()
            except FileNotFoundError:
                continue
            raise Refusal('package_provenance_invalid', 'installation/artifact must be outside Git')


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
        assert_outside_git(directory)
    packed = observed_bytes(artifact)
    if packed is None or digest(packed[0]) != evidence["sha256"]:
        raise Refusal("package_provenance_invalid", "artifact hash mismatch")
    files = {}
    total = 0
    with tarfile.open(fileobj=io.BytesIO(packed[0]), mode="r:gz") as archive:
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
            installed = observed_bytes(safe(root, name))
            if installed is None or installed[0] != data:
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


def discover_installed(root: Path, cache: Path) -> dict[str, str] | None:
    """Bounded offline global-npm discovery. Cache keys never establish identity.

    npm global installs omit the hidden lock. Authenticate content-addressed
    cached tarballs against the *complete* installation, not version equality.
    This read-only fallback creates neither npm receipts nor package metadata.
    """
    if root.absolute() != root.resolve() or cache.absolute() != cache.resolve():
        raise Refusal("package_provenance_invalid", "symlinked installation/cache")
    manifest = snapshot(safe(root, 'package.json'))
    if manifest is None:
        return None
    package = decode_json(content(manifest))
    if package.get('name') not in {'@yylo/cli', 'juno-code'}:
        return None
    index = safe(cache, '_cacache/index-v5')
    if not index.is_dir():
        return None
    candidates = {}
    deadline = time.monotonic() + 30
    def check_deadline():
        if time.monotonic() > deadline:
            raise Refusal('package_provenance_bounds', 'offline discovery deadline exceeded')
    count = size = 0
    for directory, directories, filenames in os.walk(index, followlinks=False):
        for name in directories + filenames:
            if (Path(directory) / name).is_symlink():
                raise Refusal('package_provenance_invalid', 'symlinked npm cache index')
        for name in filenames:
            check_deadline()
            p = Path(directory) / name
            # Never open a FIFO/device in blocking mode, including a raced
            # replacement between directory enumeration and descriptor open.
            descriptor = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, 'rb') as stream:
                metadata = os.fstat(stream.fileno())
                if not stat.S_ISREG(metadata.st_mode):
                    raise Refusal('package_provenance_invalid', 'nonregular npm cache index entry')
                count += 1
                size += metadata.st_size
                if count > 20000 or size > 32 * 1024 * 1024:
                    raise Refusal('package_provenance_bounds', 'npm cache index exceeds offline discovery bound')
                bucket = stream.read(32 * 1024 * 1024 + 1)
                if len(bucket) != metadata.st_size:
                    raise Refusal('package_provenance_invalid', 'npm cache index changed during read')
            # cacache buckets are append-only: the last valid entry for a key
            # wins, including tombstones. Bucket SHA-1 protects framing only.
            for line in bucket.splitlines():
                try:
                    checksum, payload = line.split(b'\t', 1)
                    if hashlib.sha1(payload).hexdigest().encode() != checksum:
                        continue
                    row = json.loads(payload)
                    key = row['key']
                    if isinstance(key, str) and (key.startswith('pacote:tarball:')
                            or (key.startswith('make-fetch-happen:request-cache:') and '.tgz' in key)):
                        candidates[key] = row
                except (ValueError, KeyError, TypeError):
                    continue
    seen = set()
    # Stable newest-first order avoids unpacking older versions in the common case.
    rows = sorted(candidates.values(), key=lambda row: str(row.get('time', '')), reverse=True)
    for row in rows:
        check_deadline()
        integrity = row.get('integrity', '')
        if not isinstance(integrity, str) or not re.fullmatch(r'sha512-[A-Za-z0-9+/]+={0,2}', integrity):
            continue
        try:
            checksum = base64.b64decode(integrity[7:], validate=True).hex()
        except ValueError:
            continue
        if len(checksum) != 128 or checksum in seen:
            continue
        seen.add(checksum)
        if len(seen) > 2048:
            raise Refusal('package_provenance_bounds', 'too many offline artifact candidates')
        artifact = safe(cache, f'_cacache/content-v2/sha512/{checksum[:2]}/{checksum[2:4]}/{checksum[4:]}')
        if not artifact.is_file() or artifact.stat().st_size > LIMIT:
            continue
        descriptor = os.open(artifact, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > LIMIT:
                raise Refusal('package_provenance_invalid', 'nonregular or oversized npm artifact')
            packed = stream.read(LIMIT + 1)
            if len(packed) != metadata.st_size:
                raise Refusal('package_provenance_invalid', 'npm artifact changed during read')
        if hashlib.sha512(packed).hexdigest() != checksum:
            continue
        try:
            with tarfile.open(fileobj=io.BytesIO(packed), mode='r:gz') as archive:
                found = None
                unpacked = 0
                for number, member in enumerate(archive):
                    check_deadline()
                    unpacked += member.size
                    if unpacked > LIMIT or number >= 10000:
                        raise Refusal('package_provenance_bounds', 'candidate archive exceeds bounds')
                    if member.name == 'package/package.json':
                        if member.isfile() and member.size <= 1024 * 1024:
                            found = archive.extractfile(member).read()
                        break
                if found != content(manifest):
                    continue
            evidence = {'root': str(root), 'artifact': str(artifact), 'sha256': digest(packed)}
            authenticate(evidence)
            return evidence
        except (Refusal, tarfile.TarError, KeyError, ValueError, EOFError):
            continue
    return None


def retain_installed(evidence: dict[str, str], cache: Path, state: Path) -> dict[str, str]:
    """Keep the activated executable independent of a later npm -g replacement.

    Install the authenticated artifact offline, with lifecycle scripts disabled,
    into an owned immutable content-addressed prefix. Never patch package bytes.
    """
    authenticate(evidence)
    if state.absolute() != state.resolve() or cache.absolute() != cache.resolve():
        raise Refusal('package_provenance_invalid', 'unsafe retained installation/cache path')
    store = safe(state, 'yylo/installed-generations')
    durable_directory(store)
    assert_outside_git(store)
    destination = safe(store, evidence['sha256'])
    result = {'root': str(destination / 'node_modules/@yylo/cli'),
              'artifact': str(destination / 'package.tgz'), 'sha256': evidence['sha256']}
    if destination.exists():
        authenticate(result)
        return result
    with tempfile.TemporaryDirectory(prefix='.install-', dir=store) as temporary:
        staging = Path(temporary)
        artifact = staging / 'package.tgz'
        artifact.write_bytes(Path(evidence['artifact']).read_bytes())
        if digest(artifact.read_bytes()) != evidence['sha256']:
            raise Refusal('package_provenance_invalid', 'artifact changed before retention')
        env = {key: value for key, value in os.environ.items() if not key.startswith(('npm_config_', 'NPM_CONFIG_'))}
        installed = subprocess.run(['npm', 'install', '--offline', '--ignore-scripts', '--no-audit', '--no-fund',
                                    '--cache', str(cache), '--prefix', str(staging), str(artifact)],
                                   stdin=subprocess.DEVNULL, capture_output=True, env=env, timeout=120)
        if installed.returncode:
            raise Refusal('retained_installation_unavailable', 'offline npm retention failed; preserve cache and previous runtime')
        authenticate({'root': str(staging / 'node_modules/@yylo/cli'), 'artifact': str(artifact), 'sha256': evidence['sha256']})
        publish(staging / 'node_modules/@yylo/cli/.yylo-generation-evidence.json', encoded(result), immutable=True)
        try:
            os.rename(staging, destination)
        except OSError as error:
            if error.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                raise
            authenticate(result)  # another writer installed the exact same artifact
        fsync_dir(store)
    authenticate(result)
    return result


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


class AssessmentFacts:
    """Exact authenticated bytes and derived schemas for ONE assessment only.

    Never share this object across plan/readiness calls or a writer boundary.
    Paths, artifact and digest all participate in the key; equal versions or
    artifacts at different installation roots do not authenticate those roots.
    Every caller must still validate each pin's attempt and executable.
    """
    def __init__(self):
        self.packages: dict[bytes, dict[str, Any]] = {}
        self.schemas: dict[bytes, set[str]] = {}

    def package(self, evidence: dict[str, str]) -> dict[str, Any]:
        key = encoded(evidence)
        if key not in self.packages:
            self.packages[key] = authenticate(evidence)
        return self.packages[key]

    def state_schemas(self, evidence: dict[str, str]) -> set[str]:
        key = encoded(evidence)
        if key not in self.schemas:
            self.schemas[key] = state_schemas(self.package(evidence))
        return self.schemas[key]


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


def plan(root: Path, candidate_evidence: dict[str, str], previous_evidence: dict[str, str],
         repair: bool = False) -> dict[str, Any]:
    root = root.resolve()
    assert_ready(root)
    return prepare(root, candidate_evidence, previous_evidence, repair=repair)


def prepare(root: Path, candidate_evidence: dict[str, str], previous_evidence: dict[str, str],
            frozen: dict[str, Any] | None = None, attempt: str | None = None,
            repair: bool = False) -> dict[str, Any]:
    """Pure preparation, also used to authenticate recovery against frozen preimages."""
    authority = registration(root)
    def observe(name):
        if frozen is not None and name in frozen:
            value = frozen[name]
            if value is not None:
                content(value)
            return value
        return snapshot(path_for(root, name, authority))
    facts = AssessmentFacts()
    candidate, previous = facts.package(candidate_evidence), facts.package(previous_evidence)
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
    if inventory.get("packageName") != old_name or (not repair and inventory.get("packageVersion") != old_version):
        raise Refusal("previous_inventory_unverified", "package mismatch")
    check = copy.deepcopy(inventory)
    if adapter == "juno-code-2.1.3-rc.0.32" and check.get("schemaVersion") == 1:
        check["packageName"] = "@yylo/cli"  # bounded adapter AFTER artifact and version verification
    if not compatibility._managed_inventory_identity_valid(check):
        raise Refusal("previous_inventory_unverified", "malformed or forged identity")
    before, after = {}, {}
    review_required = [INVENTORY] if repair and inventory.get('packageVersion') != old_version else []
    for name, record in inventory["assets"].items():
        relative(name)
        if name not in old_assets and name != POLICY:
            continue  # no write ownership over independent skills or localized config
        observed = observe(name)
        if observed is None or observed["sha256"] != record["installedSha256"]:
            if not repair or name == POLICY:
                raise Refusal("managed_preimage_modified", name)
            review_required.append(name)
        if name in old_assets and (observed is None or observed["sha256"] != digest(old_assets[name]["data"])
                                  or record["sourceSha256"] != observed["sha256"]):
            if not repair:
                raise Refusal("previous_inventory_unverified", name)
            review_required.append(name)
    for name in sorted(set(old_assets) | set(new_assets)):
        observed = observe(name)
        if name in old_assets and observed is not None:
            if observed["sha256"] != digest(old_assets[name]["data"]):
                if not repair:
                    raise Refusal("managed_preimage_modified", name)
                review_required.append(name)
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
    if guards[STATE] and state.get("schema_version") not in facts.state_schemas(candidate_evidence) & facts.state_schemas(previous_evidence):
        raise Refusal("shared_state_incompatible", "defer migration; preserve active attempts")
    prior_pins = current.get("active_pins", {})
    if not isinstance(prior_pins, dict):
        raise Refusal("active_pin_unverified", "invalid retained pin map")
    for name, pin in pins.items():
        prior = prior_pins.get(name)
        if prior is not None and prior.get("attempt") == pin["attempt"]:
            retained = facts.package(prior["generation"])
            if (prior.get("executable") != retained["executable"]
                    or state.get("schema_version") not in facts.state_schemas(prior["generation"])):
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
    if repair:
        body.update(repair=True, review_required=sorted(set(review_required)))
    return {**body, "id": digest(encoded(body))}


def assert_ready(root: Path) -> None:
    if safe(root, ROOT + "/fence.json").exists():
        raise Refusal("generation_transition_incomplete", "resume or roll back the exact journal; do not refresh")


@contextmanager
def locked(root: Path, shared: bool = False):
    path = safe(root, ROOT + "/lock")
    durable_directory(path.parent)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
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
        if prepare(root, value["candidate"], value["previous"], attempt=value["attempt"], repair=value.get('repair', False)) != value:
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
                                frozen={**value["before"], **value["guards"]}, attempt=value["attempt"], repair=value.get('repair', False))
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


def runtime_ready(root: Path, repository: Path, running: Path) -> dict[str, Any]:
    """Read-only package-generation admission, independent of product script copies.

    A projected root is used only for preactivation; the real repository must
    still have exact controller registration. No write or gate-disable argument.
    """
    authority = registration(repository)
    marker = decode_json(content(snapshot(safe(root, CURRENT))))
    if marker.get("schema_version") != "yylo_controller_generation.v1":
        raise Refusal("current_generation_unverified", "unknown generation schema")
    facts = AssessmentFacts()
    package = facts.package(marker["candidate"])
    inventory_image = snapshot(safe(root, INVENTORY))
    inventory = decode_json(content(inventory_image))
    identity = decode_json(content(snapshot(safe(root, IDENTITY))))
    policy = decode_json(content(snapshot(safe(root, POLICY))))
    if (marker.get("runtime") != runtime_identity(package) or identity != marker["runtime"]
            or marker.get("inventory_sha256") != inventory_image["sha256"]
            or policy.get("runtime", {}).get("package") != package["package"]["name"]
            or policy.get("controller_branch") != authority["branch"]
            or policy.get("product_ref") != authority["target_ref"]):
        raise Refusal("current_generation_unverified", "identity, policy or inventory mismatch")
    if not compatibility._managed_inventory_identity_valid(inventory):
        raise Refusal("current_generation_unverified", "invalid inventory identity")
    declared = assets(package)
    if set(inventory["assets"]) != set(declared):
        raise Refusal("current_generation_unverified", "incomplete managed asset ownership")
    for name, item in declared.items():
        observed = snapshot(safe(root, name))
        expected = digest(item["data"])
        record = inventory["assets"][name]
        if (observed is None or observed["sha256"] != expected
                or record.get("sourceSha256") != expected or record.get("installedSha256") != expected):
            raise Refusal("managed_preimage_modified", name)
    running_sha = snapshot(running)["sha256"]
    permitted = {digest(package["files"]["dist/templates/scripts/task_workspace.py"])}
    state_image = snapshot(safe(root, STATE))
    state = decode_json(content(state_image)) if state_image else {"tasks": {}}
    if state_image and state.get("schema_version") not in facts.state_schemas(marker["candidate"]):
        raise Refusal("shared_state_incompatible", "preserve active tasks")
    for task_id, pin in marker.get("active_pins", {}).items():
        task = state.get("tasks", {}).get(task_id, {})
        if (task.get("fencing", {}).get("attempt") == pin.get("attempt")
                and (task.get("state") in {"WORKING", "HYDRATING", "HYDRATION_FAILED"}
                     or task.get("fencing", {}).get("state") == "ACTIVE")):
            retained = facts.package(pin["generation"])
            if (pin.get("executable") != retained["executable"]
                    or state.get("schema_version") not in facts.state_schemas(pin["generation"])):
                raise Refusal("active_pin_unverified", task_id)
            permitted.add(digest(retained["files"]["dist/templates/scripts/task_workspace.py"]))
    if running_sha not in permitted:
        raise Refusal("running_generation_unverified", "runtime is not current or attempt-pinned")
    return {"schema_version": "yylo_controller_generation_admission.v1", "controller": str(repository),
            "projection": str(root), "runtime_sha256": running_sha, "executable": package["executable"],
            "package": {"name": package["package"]["name"], "version": package["package"]["version"]}}


def active_runtime_ready(root: Path) -> dict[str, Any]:
    """Admit only the bound active generation; never discover or plan a candidate.

    The caller must hold a generation read lease through dispatch/execution.
    This result is an observation, not a transferable authority token.
    """
    assert_ready(root)
    result = runtime_ready(root, root, safe(root, '.juno_task/scripts/task_workspace.py'))
    # runtime_ready authenticates the full active package, inventory and pins.
    # Unlike projected preactivation readiness, live dispatch must also prove
    # the installed executable selector has not diverged from that identity.
    config_path = git(root, 'rev-parse', '--path-format=absolute', '--git-path', 'config.worktree')
    selected = git(root, 'config', '--file', config_path, '--get',
                   'juno.controller.runtimeExecutable')
    version = git(root, 'config', '--file', config_path, '--get',
                  'juno.controller.runtimeVersion')
    if selected != result['executable'] or version != result['package']['version']:
        raise Refusal('current_generation_unverified', 'registered selector differs from active package')
    return result


def pinned_task_runtime(root: Path, task_id: str) -> dict[str, Any]:
    """Authenticate an attempt-bound retained script closure; never infer from expiry."""
    assert_ready(root)
    registration(root)
    current_path = safe(root, CURRENT)
    if not current_path.exists():
        return {"pinned": False}
    current = decode_json(content(snapshot(current_path)))
    if not isinstance(current, dict) or current.get("schema_version") != "yylo_controller_generation.v1":
        raise Refusal("current_generation_unverified", "preserve unknown generation marker")
    admitted = plan(root, current["candidate"], current["candidate"])
    if any(admitted["before"].get(name) != value for name, value in admitted["after"].items() if name != CURRENT):
        raise Refusal("current_generation_unverified", "retained dispatch requires exact current generation")
    pin = current.get("active_pins", {}).get(task_id)
    if pin is None:
        return {"pinned": False}
    state = decode_json(content(snapshot(safe(root, STATE))))
    task = state.get("tasks", {}).get(task_id, {})
    if (task.get("fencing", {}).get("attempt") != pin.get("attempt")
            or task.get("state") not in {"WORKING", "HYDRATING", "HYDRATION_FAILED"}):
        return {"pinned": False}
    retained = authenticate(pin["generation"])
    if (pin.get("executable") != retained["executable"]
            or state.get("schema_version") not in state_schemas(retained)):
        raise Refusal("active_pin_unverified", task_id)
    target = admitted["authority"]["target_sha"]
    source_repository = (compatibility.target_blob(root, target, "juno-code/package.json") is not None
                         or compatibility.target_blob(root, target, "juno-code/src/templates/scripts/task_workspace.py") is not None)
    if source_repository:
        target_runtime = compatibility.target_blob(root, target, ".juno_task/scripts/task_workspace.py")
        if target_runtime != retained["files"]["dist/templates/scripts/task_workspace.py"]:
            # The original attempt/pin remains immutable. An old reader cannot
            # admit a moved Juno source runtime. The fully admitted current
            # reader may continue the SAME attempt only across the shared schema
            # already checked above and by plan's complete current admission.
            return {"pinned": False, "retained_pin": True, "attempt": pin["attempt"],
                    "dispatch": "current-compatible-source-reader", "retained_executable": retained["executable"]}
    return {"pinned": True, "attempt": pin["attempt"], "executable": retained["executable"],
            "script": str(Path(pin["generation"]["root"]) / "dist/templates/scripts/task_workspace.py")}


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("operation", choices=["plan", "repair-plan", "discover", "retain", "apply", "resume", "rollback", "ready", "hold-lock", "hold-read", "task-pin", "runtime-ready", "active-ready"])
    parser.add_argument("--controller", type=Path, required=True)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--transaction-id")
    parser.add_argument("--projection", type=Path)
    parser.add_argument("--running-runtime", type=Path)
    args = parser.parse_args()
    try:
        request = decode_json(args.request.read_bytes()) if args.request else {}
        if args.operation == "runtime-ready":
            if args.running_runtime is None:
                raise Refusal("running_generation_unverified", "runtime path required")
            result = runtime_ready((args.projection or args.controller).resolve(), args.controller.resolve(), args.running_runtime.resolve())
            print(json.dumps(result, sort_keys=True))
            return 0
        elif args.operation in {"hold-lock", "hold-read"}:
            with locked(args.controller.resolve(), shared=args.operation == "hold-read"):
                assert_ready(args.controller.resolve())
                print(json.dumps({"locked": True}), flush=True)
                sys.stdin.readline()  # release on explicit close or parent process death
            return 0
        if args.operation == "active-ready":
            answer = active_runtime_ready(args.controller.resolve())
        elif args.operation == "task-pin":
            answer = pinned_task_runtime(args.controller, request["task_id"])
        elif args.operation == "discover":
            answer = {'evidence': discover_installed(Path(request['root']), Path(request['cache']))}
        elif args.operation == "retain":
            answer = {'evidence': retain_installed(request['evidence'], Path(request['cache']), Path(request['state']))}
        elif args.operation in {"plan", "repair-plan"}:
            answer = plan(args.controller, request["candidate"], request["previous"], repair=args.operation == 'repair-plan')
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
