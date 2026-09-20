"""Authenticated release-input adapter for the separately installed Ledger API.

Only native Ledger APIs write Records. The generation transaction owns the small
binding file; staged revisions and failed-attempt evidence are never rolled back.
"""
from __future__ import annotations
import hashlib
import json
import os
import posixpath
import string
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile

BINDING = ".juno_task/config/package-wiki.json"
SCHEMA = "yylo_package_wiki_sources.v1"


def manifest(package):
    declaration = json.loads(package["files"]["dist/templates/managed-assets.json"])
    if not isinstance(declaration, dict):
        raise ValueError("package_wiki_declaration_invalid")
    spec = declaration.get("ledgerWiki")
    if spec is None:
        return None
    if (not isinstance(spec, dict) or set(spec) != {"schemaVersion", "sources"}
            or spec["schemaVersion"] != SCHEMA or not isinstance(spec["sources"], list)
            or not 1 <= len(spec["sources"]) <= 512):
        raise ValueError("package_wiki_declaration_invalid")
    entries, seen = [], set()
    for source in spec["sources"]:
        if (not isinstance(source, str) or not source.startswith("wiki/") or not source.endswith(".md")
                or any(part in ("", ".", "..") for part in source.split("/"))
                or "\\" in source or source in seen):
            raise ValueError("package_wiki_source_invalid")
        seen.add(source)
        content = package["files"].get("dist/templates/" + source)
        if not isinstance(content, bytes):
            raise ValueError("package_wiki_source_missing:" + source)
        text = content.decode("utf-8")
        title = next((line[2:].strip() for line in text.splitlines() if line.startswith("# ")),
                     PurePosixPath(source).stem)
        entries.append({"key": source.removeprefix("wiki/"), "title": title, "text": text,
                        "sha256": hashlib.sha256(text.encode()).hexdigest()})
    # Convert only explicit intra-package Markdown links; never guess project links.
    by_key = {entry["key"]: entry for entry in entries}
    for entry in entries:
        def rewrite(match):
            target = match.group(1)
            if ":" in target or target.startswith("#") or not target.endswith(".md"):
                return match.group(0)
            key = posixpath.normpath(posixpath.join(posixpath.dirname(entry["key"]), target))
            if key not in by_key:
                raise ValueError("package_wiki_link_unresolved:" + key)
            return "](record:" + record_id(package["package"]["name"], key) + ")"
        entry["text"] = re.sub(r"\]\(([^)]+)\)", rewrite, entry["text"])
        entry["sha256"] = hashlib.sha256(entry["text"].encode()).hexdigest()
    return {"schema_version": "yylo_package_wiki_manifest.v1", "package": package["package"]["name"],
            "version": package["package"]["version"], "artifact_sha256": package["evidence"]["sha256"],
            "entries": sorted(entries, key=lambda row: row["key"])}


def record_id(package, key):
    alphabet = string.ascii_letters + string.digits
    for nonce in range(128):
        number = int.from_bytes(hashlib.sha256(f"{package}\0{key}\0{nonce}".encode()).digest(), "big")
        result = ""
        for _ in range(6):
            number, remainder = divmod(number, 62)
            result += alphabet[remainder]
        if re.fullmatch(r"(?=.*[A-Za-z])(?=.*[0-9])[A-Za-z0-9]{6}", result):
            return result
    raise ValueError("package_wiki_identity_conflict")


def invoke(root, action, **inputs):
    """No yy redispatch (or active-generation recursion) during fenced activation."""
    with tempfile.TemporaryDirectory(prefix="yylo-package-wiki-") as temporary:
        command = ["yylo-ledger", "wiki", action]
        for name, value in inputs.items():
            path = Path(temporary) / (name + ".json")
            path.write_text(json.dumps(value), encoding="utf-8")
            command.extend(["--" + name.replace("_", "-") + "-file", str(path)])
        result = subprocess.run(command, cwd=root, env={**os.environ, "JUNO_TASK_ROOT": str(root)},
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
        if result.returncode:
            # Do not echo package payload or potentially sensitive store errors.
            raise ValueError(f"package_wiki_ledger_refused:{action}:exit={result.returncode}; "
                             "install a compatible reviewed Ledger; preserve the current generation")
        try:
            return json.loads(result.stdout)
        except (ValueError, UnicodeError) as exc:
            raise ValueError("package_wiki_ledger_protocol_invalid") from exc


def prepare(root, package, frozen=None):
    source = manifest(package)
    if source is None:
        if frozen is not None:
            raise ValueError("unexpected_package_wiki_plan")
        return None
    if frozen is None:
        plan = invoke(root, "plan-package", manifest=source)
    else:
        if set(frozen) != {"manifest", "plan", "binding"} or frozen["manifest"] != source:
            raise ValueError("package_wiki_frozen_source_changed")
        plan = frozen["plan"]
    binding = invoke(root, "bind-package", manifest=source, plan=plan)
    validate_binding(package, binding)
    result = {"manifest": source, "plan": plan, "binding": binding}
    if frozen is not None and result != frozen:
        raise ValueError("package_wiki_frozen_binding_changed")
    return result


def stage(root, prepared):
    receipt = invoke(root, "publish-package", manifest=prepared["manifest"], plan=prepared["plan"])
    binding = prepared["binding"]
    if (not isinstance(receipt, dict) or any(receipt.get(key) != binding[key] for key in
            ("package", "version", "artifact_sha256", "manifest_sha256"))
            or [{k: row[k] for k in ("key", "id", "revision", "payload_sha256")}
                for row in receipt.get("records", [])] != binding["records"]):
        raise ValueError("package_wiki_publication_readback_failed")
    verify(root, binding)
    return receipt


def validate_binding(package, binding):
    source = manifest(package)
    source_sha = hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True,
                                           separators=(",", ":")).encode()).hexdigest()
    if (not isinstance(binding, dict) or set(binding) != {"schema_version", "package", "version",
            "artifact_sha256", "manifest_sha256", "records"}
            or binding["schema_version"] != "yylo_package_wiki_binding.v1"
            or binding["manifest_sha256"] != source_sha
            or any(binding[name] != source[name] for name in ("package", "version", "artifact_sha256"))
            or not isinstance(binding["records"], list)
            or len(binding["records"]) != len(source["entries"])):
        raise ValueError("package_wiki_binding_source_mismatch")
    for pin, entry in zip(binding["records"], source["entries"]):
        if (set(pin) != {"key", "id", "revision", "payload_sha256"}
                or pin["key"] != entry["key"] or pin["payload_sha256"] != entry["sha256"]
                or pin["id"] != record_id(source["package"], entry["key"])
                or type(pin["revision"]) is not int or pin["revision"] < 1):
            raise ValueError("package_wiki_binding_source_mismatch")


def verify(root, binding):
    if invoke(root, "verify-package", binding=binding) != binding:
        raise ValueError("package_wiki_binding_readback_failed")


def bootstrap(root, package_root):
    import controller_generation_migration as generation
    root, package_root = Path(root).resolve(), Path(package_root).resolve()
    if (root / generation.CURRENT).exists():
        raise ValueError("package_wiki_use_generation_transition")
    evidence = json.loads((package_root / ".yylo-generation-evidence.json").read_text())
    if Path(evidence["root"]).resolve() != package_root:
        raise ValueError("package_wiki_bootstrap_identity_mismatch")
    package = generation.authenticate(evidence)
    identity = root / generation.IDENTITY
    if identity.exists() and json.loads(identity.read_text()) != generation.runtime_identity(package):
        raise ValueError("package_wiki_existing_runtime_requires_transition")
    prepared = prepare(root, package)
    if prepared is None:
        raise ValueError("package_wiki_declaration_missing")
    path = root / BINDING
    if path.exists() and json.loads(path.read_text()) != prepared["binding"]:
        raise ValueError("package_wiki_existing_binding_requires_transition")
    stage(root, prepared)
    return prepared["binding"]


def main():
    import argparse
    import sys
    parser = argparse.ArgumentParser()
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--bootstrap", type=Path)
    actions.add_argument("--verify-binding", type=Path)
    parser.add_argument("--package-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.bootstrap is not None:
            result = bootstrap(args.bootstrap, args.package_root)
        else:
            import controller_generation_migration as generation
            evidence = json.loads((args.package_root / '.yylo-generation-evidence.json').read_text())
            if Path(evidence['root']).resolve() != args.package_root.resolve():
                raise ValueError('package_wiki_package_root_mismatch')
            package = generation.authenticate(evidence)
            result = json.loads((args.verify_binding / BINDING).read_text())
            validate_binding(package, result)
            verify(args.verify_binding, result)
        print(json.dumps(result, sort_keys=True))
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print("package_wiki_bootstrap_refused: " + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    with tempfile.TemporaryDirectory(prefix="yylo-wiki-bytecode-") as cache:
        sys.pycache_prefix = cache
        sys.dont_write_bytecode = True
        raise SystemExit(main())
