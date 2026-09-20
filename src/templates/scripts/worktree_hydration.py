#!/usr/bin/env python3
"""Small non-echoing primitives for project-owned task hydration workflows."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any


class HydrationError(RuntimeError):
    pass


def project_destination(root: Path, value: str) -> Path:
    relative = PurePosixPath(value)
    if relative.is_absolute() or not value or ".." in relative.parts or ".git" in relative.parts:
        raise HydrationError("destination must be a normalized path inside the task worktree")
    destination = root.joinpath(*relative.parts)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink() or any(parent.is_symlink() for parent in destination.parents if parent != root.parent):
        raise HydrationError("destination contains a symbolic-link component")
    return destination


def copy_env(root: Path, source_value: str, destination_value: str) -> None:
    source = Path(source_value).expanduser()
    if not source.is_absolute():
        raise HydrationError("env source must be one explicitly approved absolute file")
    if source.is_symlink() or not source.is_file():
        raise HydrationError("approved env source is missing, not regular, or symbolic")
    destination = project_destination(root, destination_value)
    data = source.read_bytes()
    if destination.is_file() and destination.read_bytes() == data:
        os.chmod(destination, 0o600)
        return
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


def node_package(root: Path, value: str) -> Path:
    relative = PurePosixPath(value)
    if relative.is_absolute() or not value or ".." in relative.parts or ".git" in relative.parts:
        raise HydrationError("node package cwd must be a normalized worktree path")
    package = root.joinpath(*relative.parts)
    components = [package, *package.parents]
    if (not package.is_dir() or not (package / "package-lock.json").is_file()
            or any(part.is_symlink() for part in components if part != root.parent)
            or (package / "package-lock.json").is_symlink()
            or (package / "node_modules").is_symlink()):
        raise HydrationError("node package cwd, exact lock, or dependency tree is missing or symbolic")
    return package


def lock_digest(package: Path) -> str:
    return hashlib.sha256((package / "package-lock.json").read_bytes()).hexdigest()


def npm_check(package: Path) -> None:
    result = subprocess.run(
        ["npm", "ls", "--depth=0", "--ignore-scripts"], cwd=package,
        stdin=subprocess.DEVNULL, text=True, capture_output=True, check=False,
    )
    if result.returncode:
        raise HydrationError("installed Node dependency tree does not satisfy the exact lock")


def verify_node_lock(root: Path, cwd: str) -> None:
    package = node_package(root, cwd)
    stamp = package / "node_modules/.yylo-package-lock.sha256"
    if not stamp.is_file() or stamp.is_symlink() or stamp.read_text().strip() != lock_digest(package):
        raise HydrationError("installed Node dependencies are missing or stale for package-lock.json")
    npm_check(package)


def hydrate_node(root: Path, cwd: str) -> None:
    package = node_package(root, cwd)
    stamp = package / "node_modules/.yylo-package-lock.sha256"
    # A failed explicit retry must not retain a previous successful lock stamp.
    stamp.unlink(missing_ok=True)
    result = subprocess.run(
        ["npm", "ci", "--no-audit", "--no-fund"], cwd=package,
        stdin=subprocess.DEVNULL, text=True, capture_output=True, check=False,
    )
    if result.returncode:
        raise HydrationError("npm ci failed for exact-lock hydration")
    npm_check(package)
    stamp.parent.mkdir(parents=True, exist_ok=True)
    temporary = stamp.with_name(f".{stamp.name}.{os.getpid()}.tmp")
    temporary.write_text(f"{lock_digest(package)}\n")
    os.replace(temporary, stamp)


def verify_clean(root: Path) -> None:
    result = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=all"],
        stdin=subprocess.DEVNULL, text=True, capture_output=True, check=False,
    )
    if result.returncode or result.stdout:
        raise HydrationError("hydration left tracked or unignored worktree drift")


def git_bytes(root: Path, *args: str, check: bool = True) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *args], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if check and result.returncode:
        raise HydrationError("conflict checkout Git identity is unreadable")
    return result.stdout


def git_text(root: Path, *args: str, check: bool = True) -> str:
    return git_bytes(root, *args, check=check).decode("utf-8", errors="strict").strip()


def regular_identity(path: Path, label: str, *, content: bool = True) -> dict[str, Any]:
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise HydrationError(f"{label} is missing, symbolic, or not regular")
        data = path.read_bytes() if content else b""
    except OSError as exc:
        raise HydrationError(f"{label} is missing or unreadable") from exc
    result = {"path": str(path), "mode": stat.S_IMODE(info.st_mode)}
    if content:
        result.update({"device": info.st_dev, "inode": info.st_ino, "size": info.st_size,
                       "sha256": hashlib.sha256(data).hexdigest()})
    return result


def conflict_checkout_snapshot(root: Path, target_ref: str) -> dict[str, Any]:
    """Describe one intentionally conflicted checkout without normalizing it."""
    if not target_ref.startswith("refs/") or "\x00" in target_ref:
        raise HydrationError("conflict target ref is malformed")
    git_dir = Path(git_text(root, "rev-parse", "--path-format=absolute", "--git-dir"))
    common_dir = Path(git_text(root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    index = Path(git_text(root, "rev-parse", "--path-format=absolute", "--git-path", "index"))
    root_info = root.lstat()
    if root.is_symlink() or not root.is_dir() or git_dir.is_symlink() or common_dir.is_symlink():
        raise HydrationError("conflict checkout root or Git directory is symbolic")
    unmerged_raw = git_bytes(root, "ls-files", "-u", "-z")
    stages: list[dict[str, Any]] = []
    conflict_paths: set[str] = set()
    for entry in unmerged_raw.split(b"\0"):
        if not entry:
            continue
        metadata, separator, encoded_path = entry.partition(b"\t")
        fields = metadata.decode("ascii").split()
        if not separator or len(fields) != 3 or fields[2] not in {"1", "2", "3"}:
            raise HydrationError("conflict checkout has an unreadable unmerged index")
        try:
            relative = encoded_path.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HydrationError("conflict checkout path is not UTF-8") from exc
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts or ".git" in pure.parts:
            raise HydrationError("conflict checkout contains an unsafe unmerged path")
        target = root.joinpath(*pure.parts)
        if any(parent.is_symlink() for parent in target.parents if parent != root.parent):
            raise HydrationError("conflict checkout path contains a symbolic-link component")
        stages.append({"path": relative, "mode": fields[0], "blob": fields[1],
                       "stage": int(fields[2])})
        conflict_paths.add(relative)
    if not stages or any({row["stage"] for row in stages if row["path"] == path} != {1, 2, 3}
                         for path in conflict_paths):
        raise HydrationError("conflict checkout must retain every expected unmerged stage")
    worktree = [{"relative_path": relative,
                 **regular_identity(root.joinpath(*PurePosixPath(relative).parts),
                                    f"conflict path {relative}")}
                for relative in sorted(conflict_paths)]
    return {"schema_version": "juno_conflict_checkout_hydration.v1",
            "root": {"path": str(root), "device": root_info.st_dev, "inode": root_info.st_ino},
            "git_dir": str(git_dir), "git_common_dir": str(common_dir),
            "head": git_text(root, "rev-parse", "HEAD"),
            "head_tree": git_text(root, "rev-parse", "HEAD^{tree}"),
            "merge_head": git_text(root, "rev-parse", "MERGE_HEAD"),
            "orig_head": git_text(root, "rev-parse", "ORIG_HEAD"),
            "target_ref": target_ref, "target_sha": git_text(root, "rev-parse", target_ref),
            "status_porcelain_v2": git_text(root, "status", "--porcelain=v2", "--untracked-files=all"),
            "index": {**regular_identity(index, "conflict checkout index", content=False),
                      "entries_sha256": hashlib.sha256(
                          git_bytes(root, "ls-files", "--stage", "-z")).hexdigest()},
            "conflict_paths": sorted(conflict_paths), "unmerged_stages": stages,
            "conflict_worktree": worktree}


def verify_conflict_checkout(root: Path, target_ref: str, expected_path: Path) -> None:
    if expected_path.is_symlink() or not expected_path.is_file():
        raise HydrationError("conflict hydration expectation is missing or symbolic")
    try:
        expected = json.loads(expected_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HydrationError("conflict hydration expectation is malformed") from exc
    canonical = (json.dumps(expected, sort_keys=True, separators=(",", ":"),
                            ensure_ascii=False) + "\n").encode()
    if expected_path.read_bytes() != canonical:
        raise HydrationError("conflict hydration expectation is not canonical")
    current = conflict_checkout_snapshot(root, target_ref)
    if expected != current:
        changed = sorted(key for key in set(expected) | set(current)
                         if expected.get(key) != current.get(key))
        raise HydrationError("conflict checkout hydration identity drifted: "
                             + ",".join(changed))
    print(json.dumps(current, sort_keys=True, separators=(",", ":")))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    env = sub.add_parser("copy-env")
    env.add_argument("--source", required=True)
    env.add_argument("--destination", required=True)
    for name in ("hydrate-node", "verify-node-lock"):
        node = sub.add_parser(name)
        node.add_argument("--cwd", required=True)
    sub.add_parser("verify-clean")
    snapshot = sub.add_parser("snapshot-conflict-checkout")
    snapshot.add_argument("--target-ref", required=True)
    conflict = sub.add_parser("verify-conflict-checkout")
    conflict.add_argument("--target-ref", required=True)
    conflict.add_argument("--expected-snapshot", required=True)
    args = parser.parse_args()
    root = Path(args.project_root).resolve()
    if not root.is_dir() or subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        text=True, capture_output=True, check=False,
    ).stdout.strip() != str(root):
        raise HydrationError("--project-root must be the exact task Git worktree")
    if args.command == "copy-env":
        copy_env(root, args.source, args.destination)
    elif args.command == "hydrate-node":
        hydrate_node(root, args.cwd)
    elif args.command == "verify-node-lock":
        verify_node_lock(root, args.cwd)
    elif args.command == "verify-clean":
        verify_clean(root)
    elif args.command == "snapshot-conflict-checkout":
        print(json.dumps(conflict_checkout_snapshot(root, args.target_ref),
                         sort_keys=True, separators=(",", ":")))
    else:
        verify_conflict_checkout(root, args.target_ref,
                                 Path(args.expected_snapshot).resolve())
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HydrationError as exc:
        print(f"worktree_hydration: error: {exc}", file=__import__("sys").stderr)
        raise SystemExit(2)
