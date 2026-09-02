#!/usr/bin/env python3
"""Python boundary for the canonical immutable fixture-base v1 contract."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable, Mapping, Optional

SCHEMA = "juno.test.fixture.base.v1"
CONTRACT = "task-workspace.v1"
ROOT_ENV = "YYLO_TEST_FIXTURE_BASE_ROOT"
DISABLE_ENV = "YYLO_TEST_DISABLE_FIXTURE_BASE_CACHE"
BOUND_INPUTS = (
    "fixture_schema", "builder_source_sha256", "task_workspace_sha256",
    "decision_core_sha256", "fixture_helper_sha256", "admission_sha256",
    "product_tree", "controller_tree", "fixture_manifest_sha256", "modes_sha256",
    "policy_sha256", "sparse_checkout_sha256", "git_identity", "platform_class",
    "dependency_lock_sha256", "managed_contract_sha256",
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def seed_key(inputs: Mapping[str, str]) -> str:
    missing = sorted(set(BOUND_INPUTS) - set(inputs))
    unknown = sorted(set(inputs) - set(BOUND_INPUTS))
    if missing or unknown or any(not isinstance(inputs[name], str) or not inputs[name] for name in BOUND_INPUTS):
        raise ValueError(f"fixture identity incomplete missing={missing} unknown={unknown}")
    payload = {"schema": SCHEMA, "contract": CONTRACT,
               "inputs": {name: inputs[name] for name in BOUND_INPUTS}}
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())


def content_manifest(root: Path) -> list[dict]:
    rows = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        entry = path.lstat()
        if path.is_symlink():
            rows.append({"path": relative, "kind": "symlink", "target": os.readlink(path)})
        elif path.is_dir():
            rows.append({"path": relative, "kind": "directory", "mode": stat.S_IMODE(entry.st_mode)})
        elif path.is_file():
            rows.append({"path": relative, "kind": "file", "mode": stat.S_IMODE(entry.st_mode),
                         "sha256": sha256(path.read_bytes())})
    return rows


def manifest_digest(root: Path) -> str:
    return sha256(json.dumps(content_manifest(root), sort_keys=True, separators=(",", ":")).encode())


def make_owner_writable(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError(f"refusing non-directory fixture root: {root}")
    for path in sorted(root.rglob("*"), reverse=True):
        if not path.is_symlink():
            path.chmod(path.stat().st_mode | stat.S_IWUSR)
    root.chmod(root.stat().st_mode | stat.S_IWUSR)


def seal(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if not path.is_symlink():
            path.chmod(path.stat().st_mode & ~0o222)
    root.chmod(root.stat().st_mode & ~0o222)


class MemoizedGitDispatch:
    """Per-test exact-output cache for immutable Git queries.

    Every distinct query crosses the real Git process boundary. Ref/index
    mutations invalidate the cache; working-tree-sensitive reads are never
    cached. This is a test adapter, not product lifecycle behavior.
    """
    CACHEABLE = frozenset({
        "check-ref-format", "ls-files", "ls-tree", "merge-base", "rev-list",
        "rev-parse", "show", "show-ref", "symbolic-ref",
    })
    UNCACHED_READS = frozenset({"diff", "diff-tree", "status"})

    def __init__(self, dispatch):
        self.dispatch = dispatch
        self.cache = {}
        self.cache_hits = 0
        self.real_query_processes = 0

    @staticmethod
    def command_for(argv):
        values = tuple(str(value) for value in argv)
        if not values or Path(values[0]).name != "git":
            return None
        try:
            return values[values.index("-C") + 2]
        except (ValueError, IndexError):
            return None

    def observe_external(self, argv):
        command = self.command_for(argv)
        if command is None or command not in self.CACHEABLE | self.UNCACHED_READS:
            self.cache.clear()

    def __call__(self, argv, cwd, *, check=True):
        values = tuple(str(value) for value in argv)
        command = self.command_for(values)
        if command is None:
            return self.dispatch(argv, cwd, check=check)
        if command in self.CACHEABLE:
            key = (values, str(Path(cwd).resolve()), bool(check))
            cached = self.cache.get(key)
            if cached is not None:
                self.cache_hits += 1
                return subprocess.CompletedProcess(
                    list(argv), cached.returncode, cached.stdout, cached.stderr)
            result = self.dispatch(argv, cwd, check=check)
            self.real_query_processes += 1
            if result.returncode == 0:
                self.cache[key] = subprocess.CompletedProcess(
                    list(argv), result.returncode, result.stdout, result.stderr)
            return result
        if command not in self.UNCACHED_READS:
            self.cache.clear()
        return self.dispatch(argv, cwd, check=check)


class Seed:
    def __init__(self, root: Path, key: str, hit: bool, invalidation: Optional[str] = None):
        self.root, self.key, self.hit, self.invalidation = root, key, hit, invalidation


def _remove_owned_tree(root: Path) -> None:
    class RetryCleanup(Exception):
        pass

    repairs = 0

    def repair(_function, path, exception_info) -> None:
        nonlocal repairs
        error = exception_info[1]
        candidate = Path(path)
        obstruction = candidate if candidate.is_dir() else candidate.parent
        if (not isinstance(error, PermissionError) or repairs >= 32
                or obstruction.is_symlink() or not obstruction.exists()
                or os.path.commonpath((str(root), str(obstruction.resolve()))) != str(root)):
            raise error
        obstruction.chmod(obstruction.stat().st_mode | stat.S_IWUSR | stat.S_IXUSR)
        repairs += 1
        raise RetryCleanup

    while root.exists():
        try:
            shutil.rmtree(root, onerror=repair)
            return
        except RetryCleanup:
            continue


class Instance:
    def __init__(self, root: Path, owned_parent: Path, identity: tuple[int, int]):
        self.root, self._parent, self._identity = root, owned_parent, identity

    def release(self) -> None:
        try: entry = self.root.lstat()
        except FileNotFoundError: return
        parent = self._parent.resolve()
        if (self.root.is_symlink() or not self.root.is_dir()
                or (entry.st_dev, entry.st_ino) != self._identity
                or self.root.resolve().parent != parent):
            raise RuntimeError(f"refusing to delete foreign or aliased fixture root: {self.root}")
        # Instances are writable when cloned. Avoid a second full-tree walk on
        # every test; repair only a genuine permission obstruction introduced
        # by a test while retaining shutil's symlink-safe traversal.
        _remove_owned_tree(self.root)


def default_root() -> Path:
    return Path(os.environ.get(ROOT_ENV, Path(tempfile.gettempdir()).resolve() / "yylo-fixture-bases"))


def ensure_seed(inputs: Mapping[str, str], builder: Callable[[Path], None], *, cache_root: Optional[Path] = None) -> Seed:
    key = seed_key(inputs)
    bases = (cache_root or default_root()).resolve()
    bases.mkdir(parents=True, exist_ok=True)
    target = bases / key

    def verify() -> None:
        value = json.loads((target / "yylo-fixture-base.json").read_text())
        if value.get("schema_version") != SCHEMA or value.get("contract") != CONTRACT or value.get("key") != key:
            raise RuntimeError("identity")
        if value.get("content_sha256") != manifest_digest(target / "topology"):
            raise RuntimeError("corrupt")

    try:
        verify(); return Seed(target, key, True)
    except Exception as exc:
        invalidation = type(exc).__name__ if target.exists() else None
    claim = bases / f".{key}.claim"
    descriptor = None
    deadline = time.monotonic() + 60
    while descriptor is None:
        try: descriptor = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if time.monotonic() >= deadline: raise RuntimeError("fixture seed claim timeout")
            time.sleep(0.02)
            try:
                verify(); return Seed(target, key, True)
            except Exception: pass
    os.close(descriptor)
    staging = Path(tempfile.mkdtemp(prefix=f".{key}.staging-", dir=bases))
    try:
        try:
            verify(); return Seed(target, key, True)
        except Exception: pass
        topology = staging / "topology"; topology.mkdir()
        builder(topology)
        # Modes are part of the content contract, so compute the digest only
        # after the topology has reached its published read-only state.
        seal(topology)
        value = {"schema_version": SCHEMA, "contract": CONTRACT, "key": key,
                 "immutable": True, "inputs": dict(inputs),
                 "content_sha256": manifest_digest(topology)}
        (staging / "yylo-fixture-base.json").write_text(json.dumps(value, sort_keys=True) + "\n")
        seal(staging)
        if target.exists():
            quarantine = bases / f"{key}.corrupt-{time.time_ns()}"
            make_owner_writable(target); target.rename(quarantine); seal(quarantine)
        staging.rename(target)
        verify()
        return Seed(target, key, False, invalidation)
    finally:
        claim.unlink(missing_ok=True)
        if staging.exists():
            make_owner_writable(staging); shutil.rmtree(staging)


def find_seed(bound_inputs: Mapping[str, str], *, cache_root: Optional[Path] = None) -> Optional[Seed]:
    """Find an exact verified published seed matching immutable static inputs."""
    bases = (cache_root or default_root()).resolve()
    if not bases.is_dir(): return None
    for candidate in sorted(bases.iterdir(), key=lambda path: path.name, reverse=True):
        try:
            value = json.loads((candidate / "yylo-fixture-base.json").read_text())
            inputs = value.get("inputs")
            if (value.get("schema_version") != SCHEMA or value.get("contract") != CONTRACT
                    or not isinstance(inputs, dict)
                    or any(inputs.get(name) != expected for name, expected in bound_inputs.items())
                    or value.get("content_sha256") != manifest_digest(candidate / "topology")):
                continue
            return Seed(candidate, str(value["key"]), True)
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return None


def create_instance(seed: Seed, *, parent: Optional[Path] = None) -> Instance:
    owned_parent = (parent or Path(tempfile.gettempdir()).resolve() / "yylo-fixture-overlays").resolve()
    owned_parent.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="task-workspace-", dir=owned_parent)).resolve()
    shutil.copytree(seed.root / "topology", root, dirs_exist_ok=True, symlinks=True)
    make_owner_writable(root)
    entry = root.lstat()
    return Instance(root, owned_parent, (entry.st_dev, entry.st_ino))
