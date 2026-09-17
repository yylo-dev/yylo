#!/usr/bin/env python3
"""Real-Git acceptance tests for the metadata-only controller boundary."""
from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "metadata_controller.py"
POLICY = Path(__file__).resolve().parents[2] / "config/metadata-controller.json"
SPEC = importlib.util.spec_from_file_location("metadata_controller", SCRIPT)
assert SPEC and SPEC.loader
mc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mc)


def command(*argv: str, cwd: Path) -> str:
    result = subprocess.run(argv, cwd=cwd, text=True, capture_output=True, check=True)
    return result.stdout.strip()


def write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)


def npm_pack(path: Path, version: str, name: str = "@yylo/cli") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest = json.dumps({"name": name, "version": version}).encode()
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo("package/package.json")
        info.size = len(manifest)
        archive.addfile(info, io.BytesIO(manifest))
        cli = b"fixture"
        info = tarfile.TarInfo("package/dist/bin/cli.mjs")
        info.size = len(cli)
        info.mode = 0o755
        archive.addfile(info, io.BytesIO(cli))


class MetadataControllerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="juno-metadata-controller-test-"))
        self.repo = self.temp / "repo"
        self.repo.mkdir()
        command("git", "init", "-b", "juno-mono-002", cwd=self.repo)
        command("git", "config", "user.name", "Test User", cwd=self.repo)
        command("git", "config", "user.email", "test@example.invalid", cwd=self.repo)
        write(self.repo / "README.md", "product\n")
        write(self.repo / "src/product.ts", "export const product = true;\n")
        command("git", "add", ".", cwd=self.repo)
        command("git", "commit", "-m", "product target", cwd=self.repo)
        self.product_head = command("git", "rev-parse", "HEAD", cwd=self.repo)

        command("git", "switch", "-c", "legacy-controller", cwd=self.repo)
        write(self.repo / ".juno_task/tasks/TASK.md", "task\n")
        write(self.repo / ".juno_task/ledger/2026.ndjson", "{}\n")
        write(self.repo / ".juno_task/specs/P1.md", "spec\n")
        write(self.repo / ".juno_task/specs/workflows/run/attempt.json", "{}\n")
        write(self.repo / ".juno_task/tasks.md", "index\n")
        write(self.repo / ".juno_task/cutover.json", "{}\n")
        write(self.repo / ".juno_task/wiki/project_runbook.md", "# Project runbook\n")
        write(self.repo / ".juno_task/wiki/domain/operator.md", "# Domain operator\n")
        write(self.repo / "juno-code/package.json", "{}\n")
        command("git", "add", ".", cwd=self.repo)
        command("git", "commit", "-m", "legacy full controller", cwd=self.repo)
        self.old_head = command("git", "rev-parse", "HEAD", cwd=self.repo)

        self.runtime = self.temp / "installed/dist/bin/yy"
        write(self.runtime, "#!/bin/sh\nprintf 'yylo 2.0.32\\n'\n")
        self.runtime.chmod(self.runtime.stat().st_mode | stat.S_IXUSR)
        write(self.temp / "installed/dist/templates/scripts/controller_resolver.py", "# runtime resolver\n")
        write(self.temp / "installed/dist/templates/scripts/task_workspace.py", "# task workspace\n")
        packaged_wiki = POLICY.parents[1] / "wiki/controller"
        for name in mc.CORE_CONTROLLER_WIKI:
            source = packaged_wiki / name
            write(self.temp / "installed/dist/templates/wiki/controller" / name,
                  source.read_text() if source.is_file() else f"# {name}\n")
        self.new_controller = self.temp / "metadata-controller"
        self.policy = mc.load_policy(POLICY)
        self.plan_path = self.temp / "plan.json"
        self.task_policy = self.temp / "reviewed/task-workspace.json"
        self.integration_policy = self.temp / "reviewed/integration-workspace.json"
        self.risk_policy = self.temp / "reviewed/risk-policy.json"
        write(self.task_policy, (POLICY.parent / "task-workspace.json").read_text())
        write(self.integration_policy, (POLICY.parent / "integration-workspace.json").read_text())
        write(self.risk_policy, (POLICY.parent / "risk-policy.json").read_text())

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def migration_args(self, **changes: object) -> argparse.Namespace:
        values: dict[str, object] = {
            "old_controller": self.repo,
            "old_branch": "refs/heads/legacy-controller",
            "expected_old_head": self.old_head,
            "new_controller": self.new_controller,
            "new_branch": "refs/heads/juno/controller-metadata-v1",
            "product_ref": "refs/heads/juno-mono-002",
            "expected_product_head": self.product_head,
            "runtime": self.runtime,
            "runtime_version": "2.0.32",
            "output": self.plan_path,
            "policy_bundle": None,
            "task_workspace_policy": self.task_policy,
            "integration_workspace_policy": self.integration_policy,
            "risk_policy": self.risk_policy,
        }
        values.update(changes)
        return argparse.Namespace(**values)

    def prepare(self) -> dict[str, object]:
        mc.migration_plan(self.migration_args(), self.policy)
        return mc.prepare(
            argparse.Namespace(plan=self.plan_path, output=self.temp / "prepare.json"), self.policy
        )

    def test_rich_agent_migration_preserves_plan_and_safe_config_without_activating_product_fields(self) -> None:
        legacy_config = {
            "defaultMaxIterations": 23,
            "promptMacros": {"global": {"ship": {"path": ".juno_task/prompts/ship.md"}}},
            "hooks": {"START_RUN": {"commands": ["touch product-only"]}},
            "workingDirectory": "/stale/product", "envFilePath": ".env.juno",
            "lifecycle": {"enabled": True}, "controllerWorkspace": {"enabled": True},
        }
        config_bytes = mc.canonical(legacy_config)
        plan_bytes = ("# Historical plan\n" + "large phase\n" * 500).encode()
        prompt_bytes = b"Run the reviewed release workflow.\n"
        (self.repo / ".juno_task/config.json").write_bytes(config_bytes)
        (self.repo / ".juno_task/plan.md").write_bytes(plan_bytes)
        (self.repo / ".juno_task/prompts").mkdir(parents=True, exist_ok=True)
        (self.repo / ".juno_task/prompts/ship.md").write_bytes(prompt_bytes)
        command("git", "add", ".juno_task/config.json", ".juno_task/plan.md", ".juno_task/prompts/ship.md", cwd=self.repo)
        command("git", "commit", "-m", "rich legacy agent state", cwd=self.repo)
        self.old_head = command("git", "rev-parse", "HEAD", cwd=self.repo)

        rich_policy = json.loads(json.dumps(self.policy))
        for name in (".juno_task/plan.md", ".juno_task/prompts"):
            if name not in rich_policy["copied_metadata"]:
                rich_policy["copied_metadata"].append(name)
            if name not in rich_policy["product_forbidden"]:
                rich_policy["product_forbidden"].append(name)
        rich_policy["tracked_exact"].append(".juno_task/plan.md")
        rich_policy["tracked_recursive"].append(".juno_task/prompts")
        rich_policy_path = self.temp / "reviewed/rich-metadata-policy.json"
        write(rich_policy_path, json.dumps(rich_policy))
        rich_policy = mc.load_policy(rich_policy_path)
        bundle = self.temp / "reviewed/rich-policy-bundle.json"
        source = {
            "config": {"present": True, "size": len(config_bytes), "sha256": mc.bytes_digest(config_bytes),
                       "fields": [{"name": name} for name in sorted(legacy_config)], "values_collected": False},
            "plan": {"present": True, "size": len(plan_bytes), "sha256": mc.bytes_digest(plan_bytes),
                     "bytes_collected": False},
            "prompt_assets": [{"path": ".juno_task/prompts/ship.md", "size": len(prompt_bytes),
                               "sha256": mc.bytes_digest(prompt_bytes)}],
            "environment_sources": [{"source": ".env.juno", "values_collected": False,
                                     "authorization_required": True}],
            "source_head": self.old_head,
        }
        dispositions = {
            "defaultMaxIterations": "migrate", "promptMacros": "transform",
            "hooks": "product-only", "workingDirectory": "product-only",
            "envFilePath": "secret", "lifecycle": "retire", "controllerWorkspace": "transform",
        }
        write(bundle, json.dumps({
            "schema_version": "juno_migration_policy_bundle.v1", "operation": "generate-policy",
            "outcome": "generated_from_reviewed_answers",
            "agent_migration": {"source": source, "field_dispositions": dispositions,
                                "environment_binding_authorized": False},
            "policies": {"metadata_controller": rich_policy,
                         "task_workspace": json.loads(self.task_policy.read_text()),
                         "integration_workspace": json.loads(self.integration_policy.read_text()),
                         "risk": json.loads(self.risk_policy.read_text())},
        }))
        mc.migration_plan(self.migration_args(
            policy_bundle=bundle, task_workspace_policy=None,
            integration_workspace_policy=None, risk_policy=None), rich_policy)
        prepare_args = argparse.Namespace(plan=self.plan_path, output=self.temp / "rich-prepare.json")
        first_prepare = mc.prepare(prepare_args, rich_policy)
        self.assertEqual(mc.prepare(prepare_args, rich_policy), first_prepare)

        migrated = json.loads((self.new_controller / ".juno_task/config.json").read_text())
        self.assertEqual(migrated["defaultMaxIterations"], 23)
        self.assertEqual(migrated["agentProfile"]["version"], 1)
        self.assertEqual(migrated["agentProfile"]["promptAssetRoot"], ".juno_task/prompts")
        self.assertEqual(migrated["agentProfile"]["roleHooks"]["product"], legacy_config["hooks"])
        self.assertEqual(migrated["promptMacros"]["global"]["ship"], {"path": "ship.md"})
        for rejected in ("hooks", "workingDirectory", "envFilePath", "lifecycle"):
            self.assertNotIn(rejected, migrated)
        compact = (self.new_controller / ".juno_task/plan.md").read_text()
        self.assertLess(len(compact), 1024)
        evidence_path = next((self.new_controller / ".juno_task/specs").glob("legacy-plan-*.md"))
        self.assertEqual(evidence_path.read_bytes(), plan_bytes)
        self.assertEqual((self.new_controller / ".juno_task/prompts/ship.md").read_bytes(), prompt_bytes)
        diagnostic = mc.inspect(self.new_controller, rich_policy,
                                expected_branch="refs/heads/juno/controller-metadata-v1",
                                require_active=False)["agent_profile_diagnostic"]
        self.assertEqual(diagnostic["status"], "valid")

    def test_prepare_creates_unrelated_metadata_only_controller_and_preserves_product(self) -> None:
        payload = self.prepare()
        self.assertTrue(payload["root_commit"])
        self.assertEqual(command("git", "rev-list", "--count", "HEAD", cwd=self.new_controller), "1")
        self.assertFalse((self.new_controller / "README.md").exists())
        self.assertFalse((self.new_controller / "yylo").exists())
        self.assertTrue((self.new_controller / ".juno_task/tasks/TASK.md").is_file())
        self.assertFalse((self.new_controller / ".juno_task/specs/workflows").exists())
        self.assertTrue((self.new_controller / ".juno_task/runtime/identity.json").is_file())
        self.assertTrue((self.new_controller / ".juno_task/scripts/controller_resolver.py").is_file())
        self.assertEqual(payload["runtime_scripts"]["file_count"], 2)
        self.assertEqual(payload["controller_wiki"]["file_count"], len(mc.CORE_CONTROLLER_WIKI))
        for name in mc.CORE_CONTROLLER_WIKI:
            self.assertTrue((self.new_controller / ".juno_task/wiki/controller" / name).is_file())
        self.assertEqual(
            (self.new_controller / ".juno_task/wiki/project_runbook.md").read_text(),
            "# Project runbook\n",
        )
        self.assertEqual(
            (self.new_controller / ".juno_task/wiki/domain/operator.md").read_text(),
            "# Domain operator\n",
        )
        self.assertTrue((self.new_controller / ".gitignore").is_file())
        self.assertTrue((self.new_controller / ".juno_task/state/tasks.json").is_file())
        self.assertTrue((self.new_controller / ".juno_task/receipts/controller-boundary.json").is_file())
        self.assertEqual(command("git", "config", "--worktree", "--get", "core.sparseCheckout",
                                 cwd=self.new_controller), "false")
        self.assertIn(".juno_task/scripts/", (self.new_controller / ".gitignore").read_text())
        self.assertIn(".juno_task/cache/", (self.new_controller / ".gitignore").read_text())
        self.assertIn(".juno_task/locks/", (self.new_controller / ".gitignore").read_text())
        self.assertIn(".juno_task/transactions/", (self.new_controller / ".gitignore").read_text())
        self.assertIn("/AGENTS.md", (self.new_controller / ".gitignore").read_text())
        self.assertIn("/.agents/", (self.new_controller / ".gitignore").read_text())
        generated_config = json.loads((self.new_controller / ".juno_task/config.json").read_text())
        self.assertEqual(
            generated_config["controllerWorkspace"]["policy"],
            ".juno_task/config/metadata-controller.json",
        )
        self.assertTrue((self.new_controller / ".juno_task/config/metadata-controller.json").is_file())
        integration_policy = self.new_controller / ".juno_task/config/integration-workspace.json"
        self.assertEqual(json.loads(integration_policy.read_text()),
                         json.loads(self.integration_policy.read_text()))
        self.assertIn(".juno_task/config/integration-workspace.json",
                      command("git", "ls-files", cwd=self.new_controller).splitlines())
        risk_policy = self.new_controller / ".juno_task/config/risk-policy.json"
        self.assertTrue(risk_policy.is_file())
        self.assertEqual(json.loads(risk_policy.read_text()),
                         json.loads(self.risk_policy.read_text()))
        self.assertIn(".juno_task/config/risk-policy.json",
                      command("git", "ls-files", cwd=self.new_controller).splitlines())
        boundary = json.loads((self.new_controller / ".juno_task/receipts/controller-boundary.json").read_text())
        self.assertGreater(len(boundary["preserved_metadata"]["entries"]), 2)
        write(self.new_controller / ".juno_task/scripts/generated.py", "print('generated')\n")
        write(self.new_controller / ".juno_task/cache/kanban.sqlite3", "cache\n")
        write(self.new_controller / ".juno_task/locks/task.lock", "lock\n")
        self.assertEqual(command("git", "status", "--porcelain", cwd=self.new_controller), "")
        self.assertEqual(
            command("git", "rev-parse", "refs/heads/juno-mono-002", cwd=self.repo),
            self.product_head,
        )

        product_worktree = self.temp / "feature-x"
        command("git", "worktree", "add", "--detach", str(product_worktree), self.product_head, cwd=self.repo)
        self.assertFalse((product_worktree / ".juno_task").exists())
        product_evidence = mc.product_boundary(
            product_worktree, "refs/heads/juno-mono-002", self.product_head, self.policy
        )
        self.assertTrue(product_evidence["passed"])

        # Controller metadata mutation has no effect on concurrently open product worktrees.
        write(self.new_controller / ".juno_task/tasks/TASK.md", "updated task\n")
        command("git", "add", ".juno_task/tasks/TASK.md", cwd=self.new_controller)
        command("git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "update task", cwd=self.new_controller)
        descendant = mc.inspect(
            self.new_controller,
            self.policy,
            expected_branch="refs/heads/juno/controller-metadata-v1",
            require_active=False,
        )
        self.assertTrue(descendant["passed"])
        self.assertTrue(descendant["checks"]["root_boundary"])
        self.assertNotEqual(descendant["root_commit"], descendant["head"])
        self.assertEqual(command("git", "status", "--porcelain", cwd=product_worktree), "")
        self.assertEqual(command("git", "rev-parse", "HEAD", cwd=product_worktree), self.product_head)

    def test_plan_rejects_unsafe_or_package_colliding_legacy_wiki_entries(self) -> None:
        cases = [
            (".juno_task/wiki/private.txt", "not Markdown\n", "Markdown files only"),
            (
                ".juno_task/wiki/controller/git_worktree_lifecycle.md",
                "# stale package copy\n",
                "package-owned controller namespace",
            ),
        ]
        for index, (relative, content, message) in enumerate(cases):
            with self.subTest(relative=relative):
                write(self.repo / relative, content)
                command("git", "add", relative, cwd=self.repo)
                command("git", "commit", "-m", f"unsafe wiki {index}", cwd=self.repo)
                head = command("git", "rev-parse", "HEAD", cwd=self.repo)
                with self.assertRaisesRegex(mc.BoundaryError, message):
                    mc.migration_plan(
                        self.migration_args(
                            expected_old_head=head,
                            output=self.temp / f"unsafe-{index}.json",
                        ),
                        self.policy,
                    )
                command("git", "reset", "--hard", self.old_head, cwd=self.repo)

    def test_plan_rejects_legacy_wiki_symlink(self) -> None:
        link = self.repo / ".juno_task/wiki/domain-link.md"
        link.symlink_to("project_runbook.md")
        command("git", "add", str(link.relative_to(self.repo)), cwd=self.repo)
        command("git", "commit", "-m", "unsafe wiki symlink", cwd=self.repo)
        head = command("git", "rev-parse", "HEAD", cwd=self.repo)
        with self.assertRaisesRegex(mc.BoundaryError, "symlinks or gitlinks"):
            mc.migration_plan(
                self.migration_args(expected_old_head=head, output=self.temp / "symlink.json"),
                self.policy,
            )

    def test_sparse_materialization_and_root_ignore_contract_fail_before_admission(self) -> None:
        self.prepare()
        command("git", "sparse-checkout", "set", "--no-cone", "/.juno_task/", cwd=self.new_controller)
        sparse = mc.inspect(self.new_controller, self.policy,
                            expected_branch="refs/heads/juno/controller-metadata-v1",
                            require_active=False)
        self.assertFalse(sparse["passed"])
        self.assertFalse(sparse["checks"]["gitignore_materialized"])
        command("git", "sparse-checkout", "disable", cwd=self.new_controller)

        ignore = self.new_controller / ".gitignore"
        ignore.write_text(ignore.read_text().replace("/.claude/\n", ""))
        command("git", "add", ".gitignore", cwd=self.new_controller)
        command("git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                "commit", "-m", "omit required root ignore", cwd=self.new_controller)
        missing = mc.inspect(self.new_controller, self.policy,
                             expected_branch="refs/heads/juno/controller-metadata-v1",
                             require_active=False)
        self.assertFalse(missing["passed"])
        self.assertFalse(missing["checks"]["root_agent_ignores"])
        self.assertEqual(missing["missing_root_agent_ignores"], ["/.claude/"])

    def test_inspect_refuses_missing_packaged_controller_wiki(self) -> None:
        self.prepare()
        missing = self.new_controller / ".juno_task/wiki/controller/task_dependency_hydration.md"
        missing.unlink()
        command("git", "add", "-u", cwd=self.new_controller)
        command("git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                "commit", "-m", "remove controller wiki", cwd=self.new_controller)
        inspected = mc.inspect(self.new_controller, self.policy,
                               expected_branch="refs/heads/juno/controller-metadata-v1",
                               require_active=False)
        self.assertFalse(inspected["passed"])
        self.assertFalse(inspected["checks"]["controller_wiki_core"])

    def test_agent_surface_repair_is_reviewed_hash_bound_and_preserves_committed_evidence(self) -> None:
        self.prepare()
        evidence = {
            "AGENTS.md": "owner agents evidence\n",
            "CLAUDE.md": "owner claude evidence\n",
            ".claude/skills/owner/SKILL.md": "owner skill evidence\n",
        }
        for relative, content in evidence.items():
            write(self.new_controller / relative, content)
        command("git", "add", "-f", *evidence, cwd=self.new_controller)
        command("git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                "commit", "-m", "retain tracked owner agent evidence", cwd=self.new_controller)
        evidence_head = command("git", "rev-parse", "HEAD", cwd=self.new_controller)
        inspected = mc.inspect(self.new_controller, self.policy,
                               expected_branch="refs/heads/juno/controller-metadata-v1",
                               require_active=False)
        self.assertFalse(inspected["passed"])
        self.assertFalse(inspected["checks"]["agent_surface_untracked"])
        self.assertEqual(inspected["tracked_agent_surface"], sorted(evidence))

        args = argparse.Namespace(
            root=self.new_controller, branch="refs/heads/juno/controller-metadata-v1",
            expected_head=evidence_head, product_ref="refs/heads/juno-mono-002",
            expected_product_head=self.product_head, disposition="keep",
            output=self.temp / "invalid-agent-plan.json")
        with self.assertRaisesRegex(mc.BoundaryError, "reviewed disposition"):
            mc.agent_surface_repair_plan(args, self.policy)
        args.disposition = "externalize"; args.output = self.temp / "agent-plan.json"
        plan = mc.agent_surface_repair_plan(args, self.policy)
        self.assertTrue(plan["evidence"]["preserved_in_parent_commit"])
        self.assertEqual(plan["changes"]["remove"], sorted(evidence))

        with self.assertRaisesRegex(mc.BoundaryError, "requires --authorize"):
            mc.agent_surface_repair_apply(argparse.Namespace(
                plan=args.output, output=self.temp / "noauth.json", authorize=False), self.policy)
        self.assertEqual(command("git", "rev-parse", "HEAD", cwd=self.new_controller), evidence_head)

        approved = args.output.read_bytes(); tampered = json.loads(approved)
        tampered["reviewed_disposition"] = "retire"; args.output.write_text(json.dumps(tampered))
        with self.assertRaisesRegex(mc.BoundaryError, "hash-bound"):
            mc.agent_surface_repair_apply(argparse.Namespace(
                plan=args.output, output=self.temp / "tampered.json", authorize=True), self.policy)
        args.output.write_bytes(approved)
        self.assertEqual(command("git", "rev-parse", "HEAD", cwd=self.new_controller), evidence_head)

        write(self.new_controller / "AGENTS.md", "drift must survive refusal\n")
        with self.assertRaisesRegex(mc.BoundaryError, "clean controller"):
            mc.agent_surface_repair_apply(argparse.Namespace(
                plan=args.output, output=self.temp / "dirty.json", authorize=True), self.policy)
        self.assertEqual((self.new_controller / "AGENTS.md").read_text(), "drift must survive refusal\n")
        command("git", "restore", "AGENTS.md", cwd=self.new_controller)

        collision = self.temp / "agent-collision.json"; collision.write_text("{}\n")
        with self.assertRaisesRegex(mc.BoundaryError, "fresh before mutation"):
            mc.agent_surface_repair_apply(argparse.Namespace(
                plan=args.output, output=collision, authorize=True), self.policy)
        self.assertEqual(command("git", "rev-parse", "HEAD", cwd=self.new_controller), evidence_head)
        self.assertEqual(collision.read_text(), "{}\n")

        receipt = mc.agent_surface_repair_apply(argparse.Namespace(
            plan=args.output, output=self.temp / "agent-apply.json", authorize=True), self.policy)
        self.assertTrue(receipt["evidence_preserved_in_parent_commit"])
        self.assertEqual(receipt["removed_paths"], sorted(evidence))
        for relative, content in evidence.items():
            self.assertFalse((self.new_controller / relative).exists())
            self.assertEqual(command("git", "show", f"{evidence_head}:{relative}", cwd=self.new_controller),
                             content.rstrip("\n"))
        verified = mc.agent_surface_repair_verify(argparse.Namespace(
            plan=args.output, output=self.temp / "agent-verify.json"), self.policy)
        self.assertTrue(verified["passed"])
        self.assertEqual(command("git", "rev-parse", "refs/heads/juno-mono-002", cwd=self.repo),
                         self.product_head)

    def test_agent_surface_repair_tolerates_living_controller_drift(self) -> None:
        self.prepare()
        # A living controller: activated role, and a reviewed task policy that
        # legitimately evolved after cutover so the frozen root boundary
        # receipt digest no longer matches (generated_contract false forever;
        # legacy roots add root_preservation). The hermetic evacuation must
        # still plan, apply, and verify against its own frozen identities.
        command("git", "config", "--worktree", "juno.workspace.role", "controller",
                cwd=self.new_controller)
        task_policy_path = self.new_controller / ".juno_task/config/task-workspace.json"
        value = json.loads(task_policy_path.read_text())
        value["allowed_paths"] = [*value.get("allowed_paths", []), "pkg"]
        task_policy_path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
        command("git", "add", ".juno_task/config/task-workspace.json", cwd=self.new_controller)
        command("git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                "commit", "-m", "evolve reviewed task policy", cwd=self.new_controller)
        evidence = {"AGENTS.md": "owner agents evidence\n",
                    ".pi/skills/owner/SKILL.md": "owner skill evidence\n"}
        for relative, content in evidence.items():
            write(self.new_controller / relative, content)
        command("git", "add", "-f", *evidence, cwd=self.new_controller)
        command("git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                "commit", "-m", "retain tracked owner agent evidence", cwd=self.new_controller)
        evidence_head = command("git", "rev-parse", "HEAD", cwd=self.new_controller)
        inspected = mc.inspect(self.new_controller, self.policy,
                               expected_branch="refs/heads/juno/controller-metadata-v1",
                               require_active=False)
        self.assertFalse(inspected["checks"]["generated_contract"])
        self.assertFalse(inspected["checks"]["role"])
        args = argparse.Namespace(
            root=self.new_controller, branch="refs/heads/juno/controller-metadata-v1",
            expected_head=evidence_head, product_ref="refs/heads/juno-mono-002",
            expected_product_head=self.product_head, disposition="retire",
            output=self.temp / "living-agent-plan.json")
        plan = mc.agent_surface_repair_plan(args, self.policy)
        self.assertEqual(plan["changes"]["remove"], sorted(evidence))
        receipt = mc.agent_surface_repair_apply(argparse.Namespace(
            plan=args.output, output=self.temp / "living-agent-apply.json",
            authorize=True), self.policy)
        self.assertEqual(receipt["removed_paths"], sorted(evidence))
        verified = mc.agent_surface_repair_verify(argparse.Namespace(
            plan=args.output, output=self.temp / "living-agent-verify.json"), self.policy)
        self.assertTrue(verified["passed"])
        # Unrelated defects still refuse the repair: a product marker is not
        # living-controller drift.
        write(self.new_controller / "README.md", "product leak\n")
        write(self.new_controller / "AGENTS.md", "fresh agent evidence\n")
        command("git", "add", "-f", "README.md", "AGENTS.md", cwd=self.new_controller)
        command("git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                "commit", "-m", "track product marker", cwd=self.new_controller)
        leak_head = command("git", "rev-parse", "HEAD", cwd=self.new_controller)
        args.expected_head = leak_head
        args.output = self.temp / "leak-agent-plan.json"
        with self.assertRaisesRegex(mc.BoundaryError, "refuses unrelated controller defects"):
            mc.agent_surface_repair_plan(args, self.policy)

    def test_policy_updated_controller_repairs_only_hash_bound_retired_config(self) -> None:
        self.prepare()
        config_path = self.new_controller / ".juno_task/config.json"
        retired_config = {
            "defaultSubagent": "pi",
            "gitCheckpoint": {"include": [".juno_task/tasks"]},
            "promptMacros": {"global": {"owner-instruction": "preserve this evidence"}},
            "controllerWorkspace": mc.RETIRED_CONTROLLER_WORKSPACE,
        }
        config_path.write_bytes(mc.canonical(retired_config))
        command("git", "add", ".juno_task/config.json", cwd=self.new_controller)
        command("git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                "commit", "-m", "retain retired generated config", cwd=self.new_controller)
        retired_head = command("git", "rev-parse", "HEAD", cwd=self.new_controller)
        preserved_before = command("git", "ls-tree", "-r", "HEAD", cwd=self.new_controller).splitlines()
        preserved_before = [row for row in preserved_before if not row.endswith("\t.juno_task/config.json")]
        evidence = mc.inspect(self.new_controller, self.policy,
                              expected_branch="refs/heads/juno/controller-metadata-v1",
                              require_active=False)
        self.assertFalse(evidence["passed"])
        self.assertFalse(evidence["checks"]["generated_contract"])

        repair_plan = self.temp / "config-repair-plan.json"
        plan_args = argparse.Namespace(
            root=self.new_controller, branch="refs/heads/juno/controller-metadata-v1",
            expected_head=retired_head, product_ref="refs/heads/juno-mono-002",
            expected_product_head=self.product_head, output=repair_plan)
        planned = mc.config_repair_plan(plan_args, self.policy)
        self.assertFalse(planned["apply_authorized"])
        self.assertFalse(planned["preservation"]["product_ref_mutation"])

        with self.assertRaisesRegex(mc.BoundaryError, "requires --authorize"):
            mc.config_repair_apply(argparse.Namespace(
                plan=repair_plan, output=self.temp / "noauth-apply.json", authorize=False), self.policy)
        self.assertFalse((self.temp / "noauth-apply.json.intent.json").exists())
        collision = self.temp / "collision-apply.json"; collision.write_text("{}\n")
        with self.assertRaisesRegex(mc.BoundaryError, "fresh before mutation"):
            mc.config_repair_apply(argparse.Namespace(
                plan=repair_plan, output=collision, authorize=True), self.policy)
        self.assertEqual(command("git", "rev-parse", "HEAD", cwd=self.new_controller), retired_head)

        config_path.write_text("{}\n")
        with self.assertRaisesRegex(mc.BoundaryError, "clean controller"):
            mc.config_repair_apply(argparse.Namespace(
                plan=repair_plan, output=self.temp / "dirty-apply.json", authorize=True), self.policy)
        command("git", "restore", ".juno_task/config.json", cwd=self.new_controller)

        task_path = self.new_controller / ".juno_task/tasks/TASK.md"
        task_path.write_text("new work after planning\n")
        command("git", "add", ".juno_task/tasks/TASK.md", cwd=self.new_controller)
        command("git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                "commit", "-m", "advance controller after plan", cwd=self.new_controller)
        with self.assertRaisesRegex(mc.BoundaryError, "neither the frozen"):
            mc.config_repair_apply(argparse.Namespace(
                plan=repair_plan, output=self.temp / "stale-apply.json", authorize=True), self.policy)
        retired_head = command("git", "rev-parse", "HEAD", cwd=self.new_controller)
        preserved_before = command("git", "ls-tree", "-r", "HEAD", cwd=self.new_controller).splitlines()
        preserved_before = [row for row in preserved_before if not row.endswith("\t.juno_task/config.json")]
        repair_plan = self.temp / "config-repair-plan-current.json"
        plan_args.expected_head = retired_head; plan_args.output = repair_plan
        mc.config_repair_plan(plan_args, self.policy)

        approved_bytes = repair_plan.read_bytes()
        tampered = json.loads(approved_bytes); tampered["correction"]["before_sha256"] = "0" * 64
        repair_plan.write_text(json.dumps(tampered))
        with self.assertRaisesRegex(mc.BoundaryError, "hash-bound"):
            mc.config_repair_apply(argparse.Namespace(
                plan=repair_plan, output=self.temp / "tampered-apply.json", authorize=True), self.policy)
        repair_plan.write_bytes(approved_bytes)

        apply_output = self.temp / "config-repair.json"
        receipt = mc.config_repair_apply(argparse.Namespace(
            plan=repair_plan, output=apply_output, authorize=True), self.policy)
        self.assertTrue(receipt["preservation_verified"])
        self.assertTrue((self.temp / "config-repair.json.intent.json").is_file())
        common = Path(command("git", "rev-parse", "--path-format=absolute", "--git-common-dir",
                              cwd=self.new_controller))
        self.assertTrue((common / "juno-repository-writer.lock").is_file())
        self.assertTrue((common / "juno-controller-config-repair.lock").is_file())
        self.assertEqual(receipt["changed_paths"], [".juno_task/config.json"])
        repaired_config = json.loads(config_path.read_text())
        self.assertEqual(repaired_config["controllerWorkspace"], mc.CANONICAL_CONTROLLER_WORKSPACE)
        for key in ("defaultSubagent", "gitCheckpoint", "promptMacros"):
            self.assertEqual(repaired_config[key], retired_config[key])
        self.assertEqual(json.loads(command(
            "git", "show", f"{retired_head}:.juno_task/config.json", cwd=self.new_controller)),
            retired_config)
        self.assertEqual(json.loads(repair_plan.read_text())["correction"]["before"], retired_config)
        first_receipt = apply_output.read_bytes(); apply_output.unlink()
        retried = mc.config_repair_apply(argparse.Namespace(
            plan=repair_plan, output=apply_output, authorize=True), self.policy)
        self.assertEqual(retried["new_head"], receipt["new_head"])
        self.assertEqual(apply_output.read_bytes(), first_receipt)
        preserved_after = command("git", "ls-tree", "-r", "HEAD", cwd=self.new_controller).splitlines()
        preserved_after = [row for row in preserved_after if not row.endswith("\t.juno_task/config.json")]
        self.assertEqual(preserved_after, preserved_before)
        self.assertEqual(command("git", "symbolic-ref", "HEAD", cwd=self.new_controller),
                         "refs/heads/juno/controller-metadata-v1")
        self.assertEqual(command("git", "rev-parse", "refs/heads/juno-mono-002", cwd=self.repo),
                         self.product_head)
        self.assertEqual(command("git", "status", "--porcelain", cwd=self.new_controller), "")
        admitted = mc.inspect(self.new_controller, self.policy,
                              expected_branch="refs/heads/juno/controller-metadata-v1",
                              require_active=False)
        self.assertTrue(admitted["passed"])

    def test_config_repair_refuses_lifecycle_and_unknown_workspace_shapes(self) -> None:
        self.prepare()
        config_path = self.new_controller / ".juno_task/config.json"
        for index, value in enumerate((
            {"lifecycle": {"enabled": True}, "controllerWorkspace": mc.RETIRED_CONTROLLER_WORKSPACE},
            {"controllerWorkspace": {"mode": "metadata-only", "policy": "foreign.json"}},
        )):
            config_path.write_bytes(mc.canonical(value))
            command("git", "add", ".juno_task/config.json", cwd=self.new_controller)
            command("git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "-m", f"invalid config {index}", cwd=self.new_controller)
            head = command("git", "rev-parse", "HEAD", cwd=self.new_controller)
            with self.assertRaisesRegex(mc.BoundaryError, "limited to the policy-updated"):
                mc.config_repair_plan(argparse.Namespace(
                    root=self.new_controller, branch="refs/heads/juno/controller-metadata-v1",
                    expected_head=head, product_ref="refs/heads/juno-mono-002",
                    expected_product_head=self.product_head,
                    output=self.temp / f"invalid-plan-{index}.json"), self.policy)

    def test_forbidden_product_path_and_wrong_identity_fail_closed(self) -> None:
        self.prepare()
        write(self.new_controller / "juno-code/package.json", "{}\n")
        command("git", "add", "juno-code/package.json", cwd=self.new_controller)
        evidence = mc.inspect(
            self.new_controller,
            self.policy,
            expected_branch="refs/heads/juno/controller-metadata-v1",
            require_active=False,
        )
        self.assertFalse(evidence["passed"])
        self.assertFalse(evidence["checks"]["staged_boundary"])

        with self.assertRaisesRegex(mc.BoundaryError, "reviewed policy"):
            mc.migration_plan(
                self.migration_args(new_branch="refs/heads/unreviewed-controller"), self.policy
            )

    def test_plan_binds_reviewed_policy_content_and_prepare_rejects_source_drift(self) -> None:
        planned = mc.migration_plan(self.migration_args(), self.policy)
        reviewed = planned["reviewed_policies"]
        self.assertEqual(reviewed["source"]["kind"], "explicit_paths")
        self.assertEqual(reviewed["task_workspace"]["sha256"],
                         mc.digest(mc.validate_task_policy(json.loads(self.task_policy.read_text()))))
        task = json.loads(self.task_policy.read_text())
        task["workspace_root"] = str(self.temp / "different-task-worktrees")
        self.task_policy.write_text(json.dumps(task))
        with self.assertRaisesRegex(mc.BoundaryError, "changed after planning"):
            mc.prepare(argparse.Namespace(plan=self.plan_path, output=self.temp / "prepare.json"), self.policy)
        self.assertFalse(self.new_controller.exists())

    def test_policy_bundle_is_accepted_and_missing_reviewed_policies_are_refused(self) -> None:
        bundle = self.temp / "reviewed/policy-bundle.json"
        bundle_value = {
            "schema_version": "juno_migration_policy_bundle.v1",
            "operation": "generate-policy",
            "outcome": "generated_from_reviewed_answers",
            "agent_migration": {
                "source": {
                    "config": {"present": False, "size": 0, "sha256": None, "fields": [], "values_collected": False},
                    "plan": {"present": False, "size": 0, "sha256": None, "bytes_collected": False},
                    "prompt_assets": [], "environment_sources": [], "source_head": self.old_head,
                },
                "field_dispositions": {}, "environment_binding_authorized": False,
            },
            "policies": {
                "metadata_controller": json.loads(json.dumps(self.policy)),
                "task_workspace": json.loads(self.task_policy.read_text()),
                "integration_workspace": json.loads(self.integration_policy.read_text()),
                "risk": json.loads(self.risk_policy.read_text()),
            },
        }
        bundle_value["policies"]["metadata_controller"]["generated_metadata"] = list(
            reversed(bundle_value["policies"]["metadata_controller"]["generated_metadata"])
        )
        bundle_value["policies"]["metadata_controller"]["runtime"]["ignored_roots"] = list(
            reversed(bundle_value["policies"]["metadata_controller"]["runtime"]["ignored_roots"])
        )
        write(bundle, json.dumps(bundle_value))
        planned = mc.migration_plan(self.migration_args(
            policy_bundle=bundle, task_workspace_policy=None,
            integration_workspace_policy=None, risk_policy=None), self.policy)
        self.assertEqual(planned["reviewed_policies"]["source"]["path"], str(bundle.resolve()))
        self.assertEqual(planned["reviewed_policies"]["source"]["kind"], "policy_bundle")
        self.assertEqual(mc.policy_from_plan_bundle(self.plan_path), self.policy)

        self.plan_path = self.temp / "missing-plan.json"
        with self.assertRaisesRegex(mc.BoundaryError, "requires --policy-bundle"):
            mc.migration_plan(self.migration_args(
                task_workspace_policy=None, integration_workspace_policy=None,
                risk_policy=None), self.policy)

    def test_continuing_boundary_rejects_nested_specs_and_arbitrary_state_receipts(self) -> None:
        self.prepare()
        write(self.new_controller / ".juno_task/specs/workflows/run.json", "{}\n")
        write(self.new_controller / ".juno_task/state/arbitrary.json", "{}\n")
        write(self.new_controller / ".juno_task/receipts/nested/arbitrary.json", "{}\n")
        command("git", "add", "-f", ".juno_task/specs/workflows/run.json", ".juno_task/state/arbitrary.json",
                ".juno_task/receipts/nested/arbitrary.json", cwd=self.new_controller)
        staged = mc.inspect(
            self.new_controller,
            self.policy,
            expected_branch="refs/heads/juno/controller-metadata-v1",
            require_active=False,
        )
        self.assertFalse(staged["passed"])
        self.assertFalse(staged["checks"]["staged_boundary"])
        command("git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "invalid metadata", cwd=self.new_controller)
        committed = mc.inspect(
            self.new_controller,
            self.policy,
            expected_branch="refs/heads/juno/controller-metadata-v1",
            require_active=False,
        )
        self.assertFalse(committed["passed"])
        self.assertEqual(
            set(committed["forbidden_tracked"]),
            {
                ".juno_task/specs/workflows/run.json",
                ".juno_task/state/arbitrary.json",
                ".juno_task/receipts/nested/arbitrary.json",
            },
        )
        details = {item["path"]: item for item in committed["forbidden_tracked_details"]}
        self.assertEqual(details[".juno_task/specs/workflows/run.json"], {
            "path": ".juno_task/specs/workflows/run.json",
            "reason": "unattributed_nested_path",
            "rule": "tracked_top_level_files:.juno_task/specs:direct_children_only",
        })
        self.assertEqual(details[".juno_task/state/arbitrary.json"]["rule"],
                         "metadata_controller:tracked_path_classes")

    def test_historical_attribution_requires_an_exact_text_reference(self) -> None:
        path = ".juno_task/specs/backend/artifacts/E2E/observation.md"
        self.assertTrue(mc.exact_text_reference(f"Artifact: `{path}`.", path))
        self.assertFalse(mc.exact_text_reference(f"Artifact: `{path}.bak`.", path))
        self.assertFalse(mc.exact_text_reference(f"Artifact: `old/{path}`.", path))

    def test_historical_reference_bound_nested_artifacts_are_immutable_and_attributed(self) -> None:
        self.prepare()
        root = self.new_controller
        artifact = ".juno_task/specs/backend/artifacts/E2E/observation.md"
        aggregate = ".juno_task/specs/backend/artifacts/E2E/observation.aggregate.json"
        write(root / artifact, "Aggregate: `observation.aggregate.json`.\n")
        write(root / aggregate, "{}\n")
        task = root / ".juno_task/tasks/TASK.md"
        task.write_text(task.read_text() + f"Artifact: `{artifact}`.\n")
        command("git", "add", artifact, aggregate, ".juno_task/tasks/TASK.md", cwd=root)
        command("git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                "commit", "-m", "checkpoint attributed evidence", cwd=root)
        admitted = mc.inspect(root, self.policy,
                              expected_branch="refs/heads/juno/controller-metadata-v1",
                              require_active=False)
        self.assertTrue(admitted["checks"]["tracked_boundary"])
        self.assertNotIn(artifact, admitted["forbidden_tracked"])
        self.assertNotIn(aggregate, admitted["forbidden_tracked"])

        (root / aggregate).write_text('{"changed":true}\n')
        command("git", "add", aggregate, cwd=root)
        command("git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                "commit", "-m", "unattributed artifact rewrite", cwd=root)
        refused = mc.inspect(root, self.policy,
                             expected_branch="refs/heads/juno/controller-metadata-v1",
                             require_active=False)
        detail = next(item for item in refused["forbidden_tracked_details"]
                      if item["path"] == aggregate)
        self.assertEqual(detail["reason"], "unattributed_nested_path")
        self.assertEqual(detail["rule"],
                         "tracked_top_level_files:.juno_task/specs:direct_children_only")

    def test_legacy_operational_metadata_is_narrowly_admitted(self) -> None:
        self.prepare()
        legacy = json.loads(json.dumps(self.policy))
        for root in mc.LEGACY_OPERATIONAL_METADATA:
            for field in ("copied_metadata", "product_forbidden", "tracked_recursive"):
                legacy[field] = [value for value in legacy[field] if value != root]
        write(self.new_controller / ".juno_task/task-scopes/ab/abc123.json", "{}\n")
        write(self.new_controller / ".juno_task/config/umbrella-admissions/abc123.json", "{}\n")
        write(self.new_controller / ".juno_task/config/umbrella-other/abc123.json", "{}\n")
        command("git", "add", "-f", ".juno_task/task-scopes", ".juno_task/config/umbrella-admissions",
                ".juno_task/config/umbrella-other", cwd=self.new_controller)
        command("git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                "commit", "-m", "legacy operational metadata", cwd=self.new_controller)
        evidence = mc.inspect(self.new_controller, legacy,
                              expected_branch="refs/heads/juno/controller-metadata-v1",
                              require_active=False)
        self.assertEqual(evidence["forbidden_tracked"],
                         [".juno_task/config/umbrella-other/abc123.json"])

    def test_verification_rejects_deletion_of_all_canonical_metadata(self) -> None:
        self.prepare()
        command("git", "rm", "-r", ".juno_task/tasks", ".juno_task/ledger", ".juno_task/specs", cwd=self.new_controller)
        command("git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "delete canonical metadata", cwd=self.new_controller)
        evidence = mc.inspect(
            self.new_controller,
            self.policy,
            expected_branch="refs/heads/juno/controller-metadata-v1",
            require_active=False,
        )
        self.assertFalse(evidence["passed"])
        self.assertFalse(evidence["checks"]["canonical_metadata_present"])
        self.assertEqual(
            set(evidence["missing_canonical_prefixes"]),
            {".juno_task/tasks", ".juno_task/ledger", ".juno_task/specs"},
        )

    def test_verification_rejects_required_generated_file_deletion(self) -> None:
        self.prepare()
        command(
            "git", "rm",
            ".juno_task/config/metadata-controller.json",
            ".juno_task/config/task-workspace.json",
            ".juno_task/config/integration-workspace.json",
            ".juno_task/config/risk-policy.json",
            ".juno_task/state/tasks.json",
            cwd=self.new_controller,
        )
        command("git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "delete generated controls", cwd=self.new_controller)
        evidence = mc.inspect(
            self.new_controller,
            self.policy,
            expected_branch="refs/heads/juno/controller-metadata-v1",
            require_active=False,
        )
        self.assertFalse(evidence["passed"])
        self.assertFalse(evidence["checks"]["required_generated_present"])
        self.assertEqual(
            set(evidence["missing_required_generated"]),
            {
                ".juno_task/config/metadata-controller.json",
                ".juno_task/config/task-workspace.json",
                ".juno_task/config/integration-workspace.json",
                ".juno_task/config/risk-policy.json",
                ".juno_task/state/tasks.json",
            },
        )

    def test_verification_rejects_tracked_risk_policy_byte_drift(self) -> None:
        self.prepare()
        risk_path = self.new_controller / ".juno_task/config/risk-policy.json"
        value = json.loads(risk_path.read_text()); value["release_flags"] = ["forged-release"]
        risk_path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
        command("git", "add", ".juno_task/config/risk-policy.json", cwd=self.new_controller)
        command("git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                "commit", "-m", "drift risk policy", cwd=self.new_controller)
        evidence = mc.inspect(self.new_controller, self.policy,
                              expected_branch="refs/heads/juno/controller-metadata-v1",
                              require_active=False)
        self.assertFalse(evidence["passed"])
        self.assertFalse(evidence["checks"]["generated_contract"])

    def test_prepare_receipt_collision_refuses_before_branch_or_worktree_mutation(self) -> None:
        mc.migration_plan(self.migration_args(), self.policy)
        prepare_output = self.temp / "prepare.json"
        prepare_output.write_text("{}\n")
        with self.assertRaisesRegex(mc.BoundaryError, "existing prepare receipt is not an exact completed retry"):
            mc.prepare(argparse.Namespace(plan=self.plan_path, output=prepare_output), self.policy)
        self.assertFalse(self.new_controller.exists())
        self.assertFalse(mc.ref_exists(self.repo, "refs/heads/juno/controller-metadata-v1"))
        self.assertEqual(command("git", "rev-parse", "refs/heads/juno-mono-002", cwd=self.repo), self.product_head)

    def set_runtime_version_output(self, stdout: str, stderr: str) -> None:
        write(
            self.runtime,
            "#!/usr/bin/env python3\nimport sys\n"
            f"sys.stdout.write({stdout!r})\nsys.stderr.write({stderr!r})\n",
        )
        self.runtime.chmod(self.runtime.stat().st_mode | stat.S_IXUSR)

    def canonical_runtime_banner(self, version: str) -> str:
        return (
            f"\n🎯 YYLO v{version} - TypeScript CLI\n"
            "   Node.js v22.22.3 on darwin\n"
            f"   Working directory: {self.runtime.parent.resolve()}\n\n"
        )

    def test_runtime_identity_accepts_current_and_compatible_version_output(self) -> None:
        version = "2.1.3-rc.0.11"
        accepted = {
            "bare machine": (f"{version}\n", ""),
            "canonical human": (f"{version}\n", self.canonical_runtime_banner(version)),
            "prefixed machine": (f"yylo {version}\n", ""),
        }
        for label, (stdout, stderr) in accepted.items():
            with self.subTest(label=label):
                self.set_runtime_version_output(stdout, stderr)
                identity = mc.runtime_identity(self.runtime, version, self.repo)
                self.assertEqual(identity["version"], version)
                self.assertEqual(identity["executable"], str(self.runtime.resolve()))

    def test_runtime_identity_rejects_noncanonical_version_output(self) -> None:
        version = "2.1.3-rc.0.11"
        banner = self.canonical_runtime_banner(version)
        cases = {
            "wrong version": (
                "2.1.3-rc.0.10\n",
                self.canonical_runtime_banner("2.1.3-rc.0.10"),
            ),
            "malformed banner": (f"{version}\n", banner.replace("Node.js", "Node")),
            "ambiguous stdout": (f"{version}\nyylo {version}\n", banner),
            "unexpected stderr": (f"{version}\n", banner + "unexpected\n"),
        }
        for label, (stdout, stderr) in cases.items():
            with self.subTest(label=label):
                self.set_runtime_version_output(stdout, stderr)
                with self.assertRaisesRegex(mc.BoundaryError, "runtime identity mismatch"):
                    mc.runtime_identity(self.runtime, version, self.repo)

    def test_runtime_inside_any_linked_worktree_is_rejected(self) -> None:
        linked = self.temp / "linked-product"
        command("git", "worktree", "add", "--detach", str(linked), self.product_head, cwd=self.repo)
        mutable_runtime = linked / "bin/yy"
        execution_marker = self.temp / "mutable-runtime-executed"
        write(mutable_runtime, f"#!/bin/sh\ntouch '{execution_marker}'\nprintf 'yylo 2.0.32\\n'\n")
        mutable_runtime.chmod(mutable_runtime.stat().st_mode | stat.S_IXUSR)
        with self.assertRaisesRegex(mc.BoundaryError, "linked worktree|mutable Git worktree"):
            mc.runtime_identity(mutable_runtime, "2.0.32", self.repo)
        self.assertFalse(execution_marker.exists())

    def test_nvm_git_ancestor_uses_supported_fresh_prefix_install_and_rebind(self) -> None:
        self.prepare()
        nvm = self.temp / "home/.nvm"
        nvm.mkdir(parents=True)
        command("git", "init", cwd=nvm)
        nvm_runtime = nvm / "versions/node/v22/lib/node_modules/@yylo/cli/dist/bin/cli.mjs"
        write(nvm_runtime, "#!/bin/sh\nprintf 'yylo 2.0.33-rc.0.10\\n'\n")
        nvm_runtime.chmod(nvm_runtime.stat().st_mode | stat.S_IXUSR)
        execution_marker = self.temp / "nvm-runtime-executed"
        write(nvm_runtime, f"#!/bin/sh\ntouch '{execution_marker}'\nprintf 'yylo 2.0.33-rc.0.10\\n'\n")
        with self.assertRaisesRegex(mc.BoundaryError, "runtime-install-rebind --help"):
            mc.runtime_identity(nvm_runtime, "2.0.33-rc.0.10", self.new_controller)
        self.assertFalse(execution_marker.exists())

        prefix = self.temp / "durable-runtimes/2.0.33-rc.0.10"
        receipt_path = self.temp / "nvm-install-rebind.json"
        fake_npm = self.temp / "nvm/versions/node/v22/bin/npm"
        write(fake_npm, "#!/bin/sh\nexit 99\n")
        fake_npm.chmod(fake_npm.stat().st_mode | stat.S_IXUSR)
        original_which = mc.shutil.which
        original_run = mc.run
        pack_argv: list[str] = []

        def install_fixture(argv: list[str], cwd: Path, check: bool = True, **kwargs: object) -> subprocess.CompletedProcess[str]:
            if argv and argv[0] == str(fake_npm) and "pack" in argv:
                pack_argv.extend(argv)
                destination = Path(argv[argv.index("--pack-destination") + 1])
                tarball = destination / "yylo-2.0.33-rc.0.10.tgz"
                tarball.write_bytes(b"exact fixture artifact")
                data = tarball.read_bytes()
                evidence = [{"version": "2.0.33-rc.0.10", "filename": tarball.name,
                             "integrity": "sha512-" + mc.base64.b64encode(mc.hashlib.sha512(data).digest()).decode(),
                             "shasum": mc.hashlib.sha1(data).hexdigest()}]
                return subprocess.CompletedProcess(argv, 0, json.dumps(evidence), "")
            if argv and argv[0] == str(fake_npm):
                installed = prefix / "node_modules/@yylo/cli"
                write(installed / "package.json", json.dumps({"name": "@yylo/cli", "version": "2.0.33-rc.0.10"}) + "\n")
                executable = installed / "dist/bin/cli.mjs"
                write(executable, "#!/bin/sh\nprintf 'yylo 2.0.33-rc.0.10\\n'\n")
                executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
                return subprocess.CompletedProcess(argv, 0, "installed\n", "")
            return original_run(argv, cwd, check, **kwargs)

        try:
            mc.shutil.which = lambda name: str(fake_npm) if name == "npm" else original_which(name)
            mc.run = install_fixture
            receipt = mc.runtime_install_rebind(argparse.Namespace(
                root=self.new_controller,
                branch="refs/heads/juno/controller-metadata-v1",
                runtime_version="2.0.33-rc.0.10",
                install_prefix=prefix,
                output=receipt_path,
            ), self.policy)
        finally:
            mc.shutil.which = original_which
            mc.run = original_run

        expected_package = (prefix / "node_modules/@yylo/cli").resolve()
        expected_runtime = (expected_package / "dist/bin/cli.mjs").resolve()
        self.assertIn("@yylo/cli@2.0.33-rc.0.10", pack_argv)
        evidence = json.loads((expected_package / ".yylo-generation-evidence.json").read_text())
        self.assertEqual(evidence["root"], str(expected_package))
        retained = Path(evidence["artifact"])
        self.assertEqual(retained.read_bytes(), b"exact fixture artifact")
        self.assertEqual(evidence["sha256"], mc.file_digest(retained))
        self.assertEqual(json.loads((expected_package / "package.json").read_text()),
                         {"name": "@yylo/cli", "version": "2.0.33-rc.0.10"})
        self.assertEqual(receipt["runtime"]["executable"], str(expected_runtime))
        self.assertEqual(receipt["operation"], "runtime-install-rebind")
        self.assertRegex(receipt["artifact"]["integrity"], r"^sha512-")
        replay = mc.runtime_install_rebind(argparse.Namespace(
            root=self.new_controller,
            branch="refs/heads/juno/controller-metadata-v1",
            runtime_version="2.0.33-rc.0.10",
            install_prefix=prefix,
            output=receipt_path,
        ), self.policy)
        self.assertEqual(replay, receipt)
        self.assertTrue(receipt_path.is_file())
        self.assertEqual(command("git", "status", "--porcelain", cwd=self.new_controller), "")
        self.assertEqual(command("git", "config", "--worktree", "--get", "juno.controller.runtimeExecutable", cwd=self.new_controller), str(expected_runtime))

    def test_runtime_install_rebind_removes_failed_fresh_prefix(self) -> None:
        self.prepare()
        prefix = self.temp / "failed-runtime"
        fake_npm = self.temp / "bin/npm"
        write(fake_npm, "#!/bin/sh\nexit 1\n")
        fake_npm.chmod(fake_npm.stat().st_mode | stat.S_IXUSR)
        original_which = mc.shutil.which
        try:
            mc.shutil.which = lambda name: str(fake_npm) if name == "npm" else original_which(name)
            failure_receipt = self.temp / "failed-runtime.json"
            with self.assertRaisesRegex(mc.BoundaryError, "exact runtime artifact is unavailable"):
                mc.runtime_install_rebind(argparse.Namespace(
                    root=self.new_controller,
                    branch="refs/heads/juno/controller-metadata-v1",
                    runtime_version="2.0.33",
                    install_prefix=prefix,
                    output=failure_receipt,
                ), self.policy)
            failure = json.loads(failure_receipt.read_text())
            self.assertEqual(failure["outcome"], "failed_rolled_back")
            self.assertTrue(failure["rollback"]["fresh_prefix_removed"])
        finally:
            mc.shutil.which = original_which
        self.assertFalse(prefix.exists())
        self.assertEqual(command("git", "status", "--porcelain", cwd=self.new_controller), "")

    def test_local_runtime_artifact_success_replay_and_receipt_provenance(self) -> None:
        self.prepare()
        version = "2.0.33-rc.0.33"
        artifact = self.temp / f"release/yylo-{version}.tgz"
        npm_pack(artifact, version)
        prefix = self.temp / "local-runtime"
        receipt_path = self.temp / "local-runtime.json"
        fake_npm = self.temp / "bin/local-npm"
        write(fake_npm, "#!/bin/sh\nexit 98\n")
        fake_npm.chmod(0o755)
        original_which, original_run = mc.shutil.which, mc.run
        install_argv: list[str] = []

        def install_fixture(argv: list[str], cwd: Path, check: bool = True, **kwargs: object) -> subprocess.CompletedProcess[str]:
            if argv and argv[0] == str(fake_npm):
                install_argv.extend(argv)
                installed = prefix / "node_modules/@yylo/cli"
                write(installed / "package.json", json.dumps({"name": "@yylo/cli", "version": version}) + "\n")
                executable = installed / "dist/bin/cli.mjs"
                write(executable, f"#!/bin/sh\nprintf 'yylo {version}\\n'\n")
                executable.chmod(0o755)
                return subprocess.CompletedProcess(argv, 0, "installed\n", "")
            return original_run(argv, cwd, check, **kwargs)

        try:
            mc.shutil.which = lambda name: str(fake_npm) if name == "npm" else original_which(name)
            mc.run = install_fixture
            args = argparse.Namespace(root=self.new_controller,
                                      branch="refs/heads/juno/controller-metadata-v1",
                                      runtime_version=version, install_prefix=prefix,
                                      artifact=artifact, output=receipt_path)
            receipt = mc.runtime_install_rebind(args, self.policy)
            replay = mc.runtime_install_rebind(args, self.policy)
        finally:
            mc.shutil.which, mc.run = original_which, original_run

        self.assertEqual(replay, receipt)
        self.assertEqual(receipt["artifact"]["source"], "local")
        self.assertEqual(receipt["artifact"]["path"], str(artifact.resolve()))
        self.assertEqual(receipt["artifact"]["sha256"], mc.file_digest(artifact))
        self.assertEqual(receipt["artifact"]["size_bytes"], artifact.stat().st_size)
        self.assertEqual(receipt["installation"]["package"], "@yylo/cli")
        self.assertEqual(receipt["runtime"]["executable_sha256"], mc.file_digest(Path(receipt["runtime"]["executable"])))
        self.assertIn("--offline", install_argv)
        self.assertNotIn(str(artifact.resolve()), install_argv)
        self.assertFalse(receipt["rollback"]["attempted"])
        self.assertEqual(command("git", "status", "--porcelain", cwd=self.new_controller), "")

    def test_local_runtime_artifact_rejects_unsafe_identity_and_hash_drift_before_install(self) -> None:
        self.prepare()
        version = "2.0.33-rc.0.33"
        directory = self.temp / "artifact-directory"
        directory.mkdir()
        with self.assertRaisesRegex(mc.BoundaryError, "regular non-symlink"):
            mc.authenticate_local_runtime_artifact(directory, version, self.new_controller)
        wrong = self.temp / "wrong.tgz"
        npm_pack(wrong, version, "other-package")
        with self.assertRaisesRegex(mc.BoundaryError, "name/version"):
            mc.authenticate_local_runtime_artifact(wrong, version, self.new_controller)
        symlink = self.temp / "artifact-link.tgz"
        symlink.symlink_to(wrong)
        with self.assertRaisesRegex(mc.BoundaryError, "symlink"):
            mc.authenticate_local_runtime_artifact(symlink, version, self.new_controller)
        malformed = self.temp / "malformed.tgz"; malformed.write_bytes(b"not a tarball")
        with self.assertRaisesRegex(mc.BoundaryError, "valid npm pack tarball"):
            mc.authenticate_local_runtime_artifact(malformed, version, self.new_controller)
        inside_git = self.repo / "runtime.tgz"; npm_pack(inside_git, version)
        with self.assertRaisesRegex(mc.BoundaryError, "outside every Git worktree"):
            mc.authenticate_local_runtime_artifact(inside_git, version, self.new_controller)
        inside_git.unlink()

        artifact = self.temp / "drift.tgz"
        npm_pack(artifact, version)
        prefix = self.temp / "drift-prefix"
        output = self.temp / "drift.json"
        original_verify = mc.verify_local_runtime_artifact
        original_which = mc.shutil.which
        fake_npm = self.temp / "bin/drift-npm"
        write(fake_npm, "#!/bin/sh\nexit 97\n"); fake_npm.chmod(0o755)

        def drift(evidence: dict[str, object], repository: Path) -> bytes:
            artifact.write_bytes(artifact.read_bytes() + b"drift")
            return original_verify(evidence, repository)

        try:
            mc.verify_local_runtime_artifact = drift
            mc.shutil.which = lambda name: str(fake_npm) if name == "npm" else original_which(name)
            with self.assertRaisesRegex(mc.BoundaryError, "changed after authentication"):
                mc.runtime_install_rebind(argparse.Namespace(
                    root=self.new_controller, branch="refs/heads/juno/controller-metadata-v1",
                    runtime_version=version, install_prefix=prefix, artifact=artifact, output=output), self.policy)
        finally:
            mc.verify_local_runtime_artifact = original_verify
            mc.shutil.which = original_which
        failure = json.loads(output.read_text())
        self.assertTrue(failure["rollback"]["complete"])
        self.assertFalse(prefix.exists())

        clean_artifact = self.temp / "dirty-controller.tgz"; npm_pack(clean_artifact, version)
        tracked = self.new_controller / ".juno_task/config.json"
        tracked.write_text(tracked.read_text() + " ")
        dirty_prefix = self.temp / "dirty-prefix"; dirty_output = self.temp / "dirty-output.json"
        try:
            with self.assertRaisesRegex(mc.BoundaryError, "clean metadata controller"):
                mc.runtime_install_rebind(argparse.Namespace(
                    root=self.new_controller, branch="refs/heads/juno/controller-metadata-v1",
                    runtime_version=version, install_prefix=dirty_prefix,
                    artifact=clean_artifact, output=dirty_output), self.policy)
        finally:
            command("git", "checkout", "--", ".juno_task/config.json", cwd=self.new_controller)
        self.assertFalse(dirty_prefix.exists())
        self.assertFalse(dirty_output.exists())

    def test_local_runtime_artifact_rebind_failure_restores_controller_and_prefix(self) -> None:
        self.prepare()
        version = "2.0.33-rc.0.33"
        artifact = self.temp / "rollback.tgz"; npm_pack(artifact, version)
        prefix = self.temp / "rollback-prefix"; output = self.temp / "rollback.json"
        fake_npm = self.temp / "bin/rollback-npm"; write(fake_npm, "#!/bin/sh\nexit 96\n"); fake_npm.chmod(0o755)
        original_which, original_run = mc.shutil.which, mc.run

        def install_fixture(argv: list[str], cwd: Path, check: bool = True, **kwargs: object) -> subprocess.CompletedProcess[str]:
            if argv and argv[0] == str(fake_npm):
                package = prefix / "node_modules/@yylo/cli"
                write(package / "package.json", json.dumps({"name": "@yylo/cli", "version": version}))
                executable = package / "dist/bin/cli.mjs"
                write(executable, f"#!/bin/sh\nprintf 'yylo {version}\\n'\n"); executable.chmod(0o755)
                return subprocess.CompletedProcess(argv, 0, "", "")
            return original_run(argv, cwd, check, **kwargs)
        try:
            mc.shutil.which = lambda name: str(fake_npm) if name == "npm" else original_which(name)
            mc.run = install_fixture
            os.environ["JUNO_RUNTIME_REBIND_TEST_FAIL_AFTER_CONFIG"] = "1"
            with self.assertRaisesRegex(mc.BoundaryError, "injected runtime rebind failure"):
                mc.runtime_install_rebind(argparse.Namespace(
                    root=self.new_controller, branch="refs/heads/juno/controller-metadata-v1",
                    runtime_version=version, install_prefix=prefix, artifact=artifact, output=output), self.policy)
        finally:
            os.environ.pop("JUNO_RUNTIME_REBIND_TEST_FAIL_AFTER_CONFIG", None)
            mc.shutil.which, mc.run = original_which, original_run
        failure = json.loads(output.read_text())
        self.assertTrue(failure["rollback"]["complete"])
        self.assertFalse(prefix.exists())
        self.assertEqual(command("git", "status", "--porcelain", cwd=self.new_controller), "")

    def agent_config_fixture(self) -> tuple[Path, dict[str, object]]:
        self.prepare()
        root = self.new_controller
        command("git", "config", "--worktree", "juno.workspace.role", "controller", cwd=root)
        command("git", "config", "--local", "juno.controller.path", str(root), cwd=root)
        command("git", "config", "--local", "juno.controller.branch", self.policy["controller_branch"], cwd=root)
        entrypoint = self.policy_test_runtime_entrypoint()
        package = mc.package_policy_source(root, str(entrypoint))
        command("git", "config", "--worktree", "juno.controller.runtimeVersion", package["version"], cwd=root)
        command("git", "config", "--worktree", "juno.controller.runtimeExecutable", str(entrypoint), cwd=root)
        value = {"controllerWorkspace": {"mode": "metadata-only"},
                 "workingDirectory": str(root), "sessionDirectory": "/legacy/product",
                 "hooks": {"START_RUN": {"commands": ["touch forbidden"]}},
                 "lifecycle": {"enabled": True}, "defaultMaxIterations": 9,
                 "agentProfile": {"version": 1, "promptAssetRoot": ".juno_task/prompts"},
                 "promptMacros": {"global": {"test": "preserved preference"}}}
        write(root / mc.CONFIG_PATH, json.dumps(value))
        command("git", "add", mc.CONFIG_PATH, cwd=root)
        command("git", "commit", "-m", "legacy retained product config", cwd=root)
        return root, value

    def test_agent_config_plan_apply_preserves_preferences_parent_and_product(self) -> None:
        root, before = self.agent_config_fixture()
        head = command("git", "rev-parse", "HEAD", cwd=root)
        original = (root / mc.CONFIG_PATH).read_bytes()
        plan_path = self.temp / "agent-config-plan.json"
        plan = mc.agent_config_plan(argparse.Namespace(root=root, output=plan_path), self.policy)
        self.assertEqual((root / mc.CONFIG_PATH).read_bytes(), original)
        self.assertEqual(plan["agent_config"]["field_dispositions"]["workingDirectory"], "product-only")
        args = argparse.Namespace(plan=plan_path, output=self.temp / "agent-config-apply.json", authorize=True)
        applied = mc.config_repair_apply(args, self.policy)
        after = json.loads((root / mc.CONFIG_PATH).read_bytes())
        for field in ("agentProfile", "promptMacros", "defaultMaxIterations"):
            self.assertEqual(after[field], before[field])
        self.assertFalse(mc.CONTROLLER_PRODUCT_CONFIG_FIELDS.intersection(after))
        self.assertNotIn("lifecycle", after)
        self.assertEqual(after["controllerWorkspace"], mc.CANONICAL_CONTROLLER_WORKSPACE)
        self.assertEqual(command("git", "show", f"{head}:{mc.CONFIG_PATH}", cwd=root), original.decode())
        self.assertEqual(command("git", "rev-parse", self.policy["product_ref"], cwd=root), self.product_head)
        self.assertEqual(command("git", "status", "--porcelain", cwd=root), "")
        self.assertEqual(mc.config_repair_apply(args, self.policy)["new_head"], applied["new_head"])

    def test_agent_config_refuses_dirty_stale_unauthorized_and_runtime_drift(self) -> None:
        root, before = self.agent_config_fixture()
        plan_path = self.temp / "agent-plan.json"
        args = argparse.Namespace(root=root, output=plan_path)
        original = (root / mc.CONFIG_PATH).read_bytes()
        write(root / "notebook.ipynb", "preserve dirty notebook")
        with self.assertRaisesRegex(mc.BoundaryError, "clean controller"):
            mc.agent_config_plan(args, self.policy)
        self.assertEqual((root / "notebook.ipynb").read_text(), "preserve dirty notebook")
        command("git", "add", "notebook.ipynb", cwd=root)
        command("git", "commit", "-m", "fixture owner preserves notebook", cwd=root)
        mc.agent_config_plan(args, self.policy)
        apply = argparse.Namespace(plan=plan_path, output=self.temp / "apply.json", authorize=False)
        with self.assertRaisesRegex(mc.BoundaryError, "authorize-config-repair"):
            mc.config_repair_apply(apply, self.policy)
        apply.authorize = True
        command("git", "config", "--worktree", "juno.controller.runtimeVersion", "0.0.0-mismatch", cwd=root)
        with self.assertRaisesRegex(mc.BoundaryError, "runtime binding is incompatible"):
            mc.config_repair_apply(apply, self.policy)
        self.assertEqual((root / mc.CONFIG_PATH).read_bytes(), original)
        package = mc.package_policy_source(root, str(self.policy_test_runtime_entrypoint()))
        command("git", "config", "--worktree", "juno.controller.runtimeVersion", package["version"], cwd=root)
        before["defaultMaxIterations"] = 7
        write(root / mc.CONFIG_PATH, json.dumps(before))
        with self.assertRaisesRegex(mc.BoundaryError, "clean controller"):
            mc.config_repair_apply(apply, self.policy)
        command("git", "add", mc.CONFIG_PATH, cwd=root)
        command("git", "commit", "-m", "stale preimage", cwd=root)
        with self.assertRaisesRegex(mc.BoundaryError, "neither the frozen"):
            mc.config_repair_apply(apply, self.policy)
        self.assertEqual(json.loads((root / mc.CONFIG_PATH).read_bytes()), before)

    def test_agent_config_unknown_secret_and_forged_dispositions_refuse(self) -> None:
        root, value = self.agent_config_fixture()
        for field in ("unknown", "envFilePath", "envFileCopied"):
            with self.assertRaisesRegex(mc.BoundaryError, "unknown/secret fields"):
                mc.agent_config_correction({**value, field: "DO_NOT_PRINT_VALUE"})
        plan_path = self.temp / "agent-plan.json"
        plan = mc.agent_config_plan(argparse.Namespace(root=root, output=plan_path), self.policy)
        plan["agent_config"]["field_dispositions"]["workingDirectory"] = "migrate"
        plan.pop("plan_sha256")
        plan["plan_sha256"] = mc.digest(plan)
        plan_path.write_bytes(mc.canonical(plan))
        with self.assertRaisesRegex(mc.BoundaryError, "exact derived field dispositions"):
            mc.validate_config_repair_plan(plan_path)

    def test_workspace_only_repair_cannot_produce_rejected_product_config(self) -> None:
        self.prepare()
        root = self.new_controller
        write(root / mc.CONFIG_PATH, json.dumps({"controllerWorkspace": mc.RETIRED_CONTROLLER_WORKSPACE,
                                                "workingDirectory": str(root)}))
        command("git", "add", mc.CONFIG_PATH, cwd=root)
        command("git", "commit", "-m", "retired pointer with product field", cwd=root)
        with self.assertRaisesRegex(mc.BoundaryError, "would retain product-only fields"):
            mc.config_repair_plan(argparse.Namespace(root=root, branch=self.policy["controller_branch"],
                product_ref=self.policy["product_ref"], expected_head=command("git", "rev-parse", "HEAD", cwd=root),
                expected_product_head=self.product_head, output=self.temp / "old-repair.json"), self.policy)

    def policy_test_runtime_entrypoint(self) -> Path:
        source_entrypoint = Path(__file__).resolve().parents[3] / "bin/cli.ts"
        if source_entrypoint.is_file():
            return source_entrypoint
        # Managed-asset acceptance installs runtime bytes without a surrounding
        # npm package. Materialize a private exact package layout so these tests
        # still exercise package/source binding rather than weakening it.
        package = self.temp / "yylo-policy-test-package"
        templates = package / "src/templates"
        (templates / "scripts").mkdir(parents=True, exist_ok=True)
        (templates / "config").mkdir(parents=True, exist_ok=True)
        (package / "src/bin").mkdir(parents=True, exist_ok=True)
        (package / "package.json").write_text('{"name":"@yylo/cli","version":"0.0.0-test"}\n')
        for name in ("metadata_controller.py", "task_workspace.py", "risk_policy.py", "integration_workspace.py"):
            shutil.copyfile(SCRIPT.with_name(name), templates / "scripts" / name)
        shutil.copyfile(POLICY.parent / "integration-workspace.json",
                        templates / "config/integration-workspace.json")
        entrypoint = package / "src/bin/cli.ts"
        entrypoint.write_text("// exact private migration test package entrypoint\n")
        return entrypoint

    def legacy_policy_controller(self) -> tuple[Path, bytes]:
        self.prepare()
        root = self.new_controller
        branch = "refs/heads/juno/controller-metadata-v1"
        command("git", "config", "--worktree", "juno.workspace.role", "controller", cwd=root)
        command("git", "config", "--local", "juno.controller.path", str(root), cwd=root)
        command("git", "config", "--local", "juno.controller.branch", branch, cwd=root)
        runtime_entrypoint = self.policy_test_runtime_entrypoint()
        package_source = mc.package_policy_source(root, str(runtime_entrypoint))
        command("git", "config", "--worktree", "juno.controller.runtimeVersion",
                package_source["version"], cwd=root)
        command("git", "config", "--worktree", "juno.controller.runtimeExecutable",
                package_source["runtime_entrypoint"], cwd=root)
        config_path = root / mc.CONFIG_PATH
        config = json.loads(config_path.read_bytes())
        config["gitCheckpoint"] = {"include": [".juno_task/tasks"]}
        config_path.write_text(json.dumps(config, separators=(",", ":")) + "\n")
        policy_path = root / mc.POLICY_PATH
        value = json.loads(policy_path.read_bytes())
        value["generated_metadata"].remove(mc.INTEGRATION_POLICY_PATH)
        value["tracked_exact"].remove(mc.INTEGRATION_POLICY_PATH)
        legacy = (json.dumps(value, separators=(",", ":")) + "\n").encode()
        policy_path.write_bytes(legacy)
        (root / mc.INTEGRATION_POLICY_PATH).unlink()
        command("git", "add", "-A", "--", mc.CONFIG_PATH, mc.POLICY_PATH, mc.INTEGRATION_POLICY_PATH, cwd=root)
        command("git", "commit", "-m", "legacy metadata policy", cwd=root)
        return root, legacy

    def policy_plan(self, root: Path, name: str = "metadata-policy-plan.json") -> tuple[Path, dict[str, object]]:
        path = self.temp / name
        payload = mc.policy_migration_plan(argparse.Namespace(root=root, output=path))
        return path, payload

    def test_metadata_policy_plan_is_read_only_and_apply_creates_one_exact_clean_commit(self) -> None:
        root, legacy = self.legacy_policy_controller()
        before = command("git", "rev-parse", "HEAD", cwd=root)
        plan_path, plan = self.policy_plan(root)
        self.assertEqual(command("git", "rev-parse", "HEAD", cwd=root), before)
        self.assertEqual(command("git", "status", "--porcelain=v2", "--untracked-files=all", cwd=root), "")
        self.assertEqual(plan["policy_before_sha256"], mc.bytes_digest(legacy))
        self.assertEqual(plan["changed_paths"], sorted(mc.POLICY_MIGRATION_PATHS))
        self.assertEqual(plan["semantic_additions"], [
            {"field": "generated_metadata", "value": mc.INTEGRATION_POLICY_PATH},
            {"field": "tracked_exact", "value": mc.INTEGRATION_POLICY_PATH},
        ])
        receipt = mc.policy_migration_apply(argparse.Namespace(
            plan=plan_path, output=self.temp / "metadata-policy-apply.json", authorize=True))
        self.assertEqual(receipt["outcome"], "migrated")
        self.assertEqual(command("git", "rev-parse", "HEAD^", cwd=root), before)
        self.assertEqual(command("git", "diff", "--name-only", "HEAD^", "HEAD", cwd=root).splitlines(),
                         sorted(mc.POLICY_MIGRATION_PATHS))
        self.assertEqual(command("git", "status", "--porcelain=v2", "--untracked-files=all", cwd=root), "")
        migrated = (root / mc.POLICY_PATH).read_bytes()
        # The compact owner formatting remains compact; only two array strings were inserted.
        self.assertNotIn(b"\n  ", migrated)
        self.assertEqual(json.loads(migrated)["controller_branch"], "refs/heads/juno/controller-metadata-v1")
        self.assertEqual((root / mc.INTEGRATION_POLICY_PATH).read_bytes(),
                         (POLICY.parent / "integration-workspace.json").read_bytes())

        idempotent_plan, no_change = self.policy_plan(root, "metadata-policy-idempotent-plan.json")
        self.assertEqual(no_change["outcome"], "already_migrated_no_mutation")
        tip = command("git", "rev-parse", "HEAD", cwd=root)
        noop = mc.policy_migration_apply(argparse.Namespace(
            plan=idempotent_plan, output=self.temp / "metadata-policy-idempotent.json", authorize=True))
        self.assertEqual(noop["outcome"], "already_migrated_noop")
        replay = mc.policy_migration_apply(argparse.Namespace(
            plan=plan_path, output=self.temp / "metadata-policy-apply.json", authorize=True))
        self.assertEqual(replay["new_head"], tip)
        self.assertEqual(command("git", "rev-parse", "HEAD", cwd=root), tip)

    def test_metadata_policy_apply_refuses_stale_tampered_dirty_and_alternate_index(self) -> None:
        root, _ = self.legacy_policy_controller()
        plan_path, _ = self.policy_plan(root)
        original = plan_path.read_bytes()
        tampered = json.loads(original); tampered["policy_result_sha256"] = "0" * 64
        plan_path.write_text(json.dumps(tampered))
        with self.assertRaisesRegex(mc.BoundaryError, "hash-bound plan"):
            mc.policy_migration_apply(argparse.Namespace(
                plan=plan_path, output=self.temp / "tampered.json", authorize=True))
        plan_path.write_bytes(original)

        write(root / ".juno_task/tasks/unrelated.md", "dirt\n")
        with self.assertRaisesRegex(mc.BoundaryError, "clean controller"):
            mc.policy_migration_apply(argparse.Namespace(
                plan=plan_path, output=self.temp / "dirty.json", authorize=True))
        (root / ".juno_task/tasks/unrelated.md").unlink()

        for variable, value in (("GIT_INDEX_FILE", str(self.temp / "alternate-index")),
                                ("GIT_DIR", str(self.temp / "redirected-git-dir")),
                                ("GIT_OBJECT_DIRECTORY", str(self.temp / "redirected-objects"))):
            previous = os.environ.get(variable)
            os.environ[variable] = value
            try:
                with self.assertRaisesRegex(mc.BoundaryError, f"Git environment overrides.*{variable}"):
                    mc.policy_migration_apply(argparse.Namespace(
                        plan=plan_path, output=self.temp / f"{variable}.json", authorize=True))
            finally:
                if previous is None: os.environ.pop(variable, None)
                else: os.environ[variable] = previous

        write(root / ".juno_task/tasks/stale.md", "stale\n")
        command("git", "add", ".juno_task/tasks/stale.md", cwd=root)
        command("git", "commit", "-m", "move controller head", cwd=root)
        with self.assertRaisesRegex(mc.BoundaryError, "not the exact completed"):
            mc.policy_migration_apply(argparse.Namespace(
                plan=plan_path, output=self.temp / "stale-head.json", authorize=True))

    def test_metadata_policy_refuses_wrong_role_root_symlink_and_policy_runtime_product_drift(self) -> None:
        root, _ = self.legacy_policy_controller()
        command("git", "config", "--worktree", "juno.workspace.role", "task", cwd=root)
        with self.assertRaisesRegex(mc.BoundaryError, "active metadata-only controller role"):
            self.policy_plan(root)
        command("git", "config", "--worktree", "juno.workspace.role", "controller", cwd=root)
        runtime_version = command("git", "config", "--worktree", "--get",
                                  "juno.controller.runtimeVersion", cwd=root)
        command("git", "config", "--worktree", "juno.controller.runtimeVersion", "0.0.0", cwd=root)
        with self.assertRaisesRegex(mc.BoundaryError, "registered controller runtime differs"):
            self.policy_plan(root)
        command("git", "config", "--worktree", "juno.controller.runtimeVersion", runtime_version, cwd=root)
        link = self.temp / "controller-link"; link.symlink_to(root, target_is_directory=True)
        with self.assertRaisesRegex(mc.BoundaryError, "symbolic-link controller root"):
            self.policy_plan(link)
        nested = root / ".juno_task/config/.git"; nested.mkdir()
        with self.assertRaisesRegex(mc.BoundaryError, "nested repository"):
            self.policy_plan(root)
        nested.rmdir()
        command("git", "config", "--local", "juno.controller.path", str(self.repo), cwd=root)
        with self.assertRaisesRegex(mc.BoundaryError, "registration"):
            self.policy_plan(root)
        command("git", "config", "--local", "juno.controller.path", str(root), cwd=root)

        plan_path, _ = self.policy_plan(root)
        original_source = mc.package_policy_source
        try:
            mc.package_policy_source = lambda candidate, runtime=None: {
                **original_source(candidate, runtime), "version": "package-drift"}
            with self.assertRaisesRegex(mc.BoundaryError, "registered controller runtime differs"):
                mc.policy_migration_apply(argparse.Namespace(
                    plan=plan_path, output=self.temp / "package-drift.json", authorize=True))
        finally:
            mc.package_policy_source = original_source
        generation = root / ".juno_task/runtime/managed-controller/generation.json"
        write(generation, json.dumps({"schema_version": "juno_managed_controller_runtime.v1",
                                     "package_version": "9.9.9", "target_sha": "a" * 40, "scripts": {}}))
        with self.assertRaisesRegex(mc.BoundaryError, "generation does not match"):
            mc.policy_migration_apply(argparse.Namespace(
                plan=plan_path, output=self.temp / "runtime-drift.json", authorize=True))
        generation.unlink()

        (root / mc.POLICY_PATH).write_bytes((root / mc.POLICY_PATH).read_bytes() + b" ")
        with self.assertRaisesRegex(mc.BoundaryError, "clean controller"):
            mc.policy_migration_apply(argparse.Namespace(
                plan=plan_path, output=self.temp / "policy-drift.json", authorize=True))
        command("git", "restore", "--", mc.POLICY_PATH, cwd=root)

        tree = command("git", "rev-parse", "refs/heads/juno-mono-002^{tree}", cwd=root)
        moved = command("git", "commit-tree", tree, "-p", self.product_head, "-m", "move product", cwd=root)
        command("git", "update-ref", "refs/heads/juno-mono-002", moved, self.product_head, cwd=root)
        with self.assertRaisesRegex(mc.BoundaryError, "stale"):
            mc.policy_migration_apply(argparse.Namespace(
                plan=plan_path, output=self.temp / "product-drift.json", authorize=True))

    def test_metadata_policy_concurrent_worktree_mutation_fails_before_commit_and_checkpoint_still_refuses_policy(self) -> None:
        root, _ = self.legacy_policy_controller()
        plan_path, _ = self.policy_plan(root)
        pause = str(self.temp / "pause")
        env = {**os.environ, "JUNO_METADATA_POLICY_MIGRATION_TEST_PAUSE_FILE": pause}
        process = subprocess.Popen([
            "python3", str(SCRIPT), "metadata-policy-apply", "--plan", str(plan_path),
            "--output", str(self.temp / "race-apply.json"), "--authorize-metadata-policy-migration",
        ], cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        for _ in range(500):
            if Path(pause + ".ready").exists(): break
            import time; time.sleep(0.01)
        else:
            process.kill(); self.fail("migration apply did not reach deterministic race seam")
        (root / mc.POLICY_PATH).write_bytes((root / mc.POLICY_PATH).read_bytes() + b" ")
        Path(pause + ".release").write_text("release\n")
        stdout, stderr = process.communicate(timeout=15)
        self.assertNotEqual(process.returncode, 0, stdout)
        self.assertIn("clean controller", stderr)
        self.assertEqual(command("git", "rev-parse", "HEAD", cwd=root), json.loads(plan_path.read_text())["head"])
        self.assertEqual(command("git", "diff", "--cached", "--name-only", cwd=root), "")

        checkpoint = SCRIPT.with_name("controller_checkpoint.py")
        result = subprocess.run(["python3", str(checkpoint), "--root", str(root), "plan"],
                                text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checkpoint include is missing required canonical roots", result.stderr)
        self.assertIn("safe_next_action=add", result.stderr)

    def test_metadata_policy_holds_real_index_lock_and_rejects_concurrent_staging_before_commit(self) -> None:
        root, _ = self.legacy_policy_controller()
        plan_path, plan = self.policy_plan(root)
        pause = str(self.temp / "index-pause")
        process = subprocess.Popen([
            "python3", str(SCRIPT), "metadata-policy-apply", "--plan", str(plan_path),
            "--output", str(self.temp / "index-race-apply.json"), "--authorize-metadata-policy-migration",
        ], cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
           env={**os.environ, "JUNO_METADATA_POLICY_MIGRATION_TEST_INDEX_PAUSE_FILE": pause})
        for _ in range(500):
            if Path(pause + ".ready").exists(): break
            import time; time.sleep(0.01)
        else:
            process.kill(); self.fail("migration apply did not acquire the deterministic index lock")
        task = root / ".juno_task/tasks/TASK.md"; task.write_text("concurrent staging\n")
        staged = subprocess.run(["git", "add", ".juno_task/tasks/TASK.md"], cwd=root,
                                text=True, capture_output=True)
        self.assertNotEqual(staged.returncode, 0)
        self.assertIn("index.lock", staged.stderr)
        Path(pause + ".release").write_text("release\n")
        stdout, stderr = process.communicate(timeout=15)
        self.assertNotEqual(process.returncode, 0, stdout)
        self.assertIn("clean controller", stderr)
        self.assertEqual(command("git", "rev-parse", "HEAD", cwd=root), plan["head"])
        self.assertEqual(command("git", "diff", "--cached", "--name-only", cwd=root), "")
        index_lock = Path(command("git", "rev-parse", "--path-format=absolute", "--git-path", "index.lock", cwd=root))
        self.assertFalse(index_lock.exists())

    def test_metadata_policy_same_plan_recovers_owned_precommit_index_lock(self) -> None:
        root, _ = self.legacy_policy_controller()
        plan_path, plan = self.policy_plan(root)
        index = Path(command("git", "rev-parse", "--path-format=absolute", "--git-path", "index", cwd=root))
        index_lock = Path(str(index) + ".lock")
        with tempfile.TemporaryDirectory() as temporary:
            prepared = Path(temporary) / "index"
            env = {"GIT_INDEX_FILE": str(prepared)}
            mc.git(root, "read-tree", plan["head"], env=env)
            mc.add_blob(root, env, mc.POLICY_PATH, plan["policy_result_utf8"].encode())
            mc.add_blob(root, env, mc.INTEGRATION_POLICY_PATH,
                        plan["source"]["integration_source_utf8"].encode())
            result_tree = mc.git(root, "write-tree", env=env)
            index_lock.write_bytes(prepared.read_bytes())
        common = Path(command("git", "rev-parse", "--path-format=absolute", "--git-common-dir", cwd=root))
        ownership = mc.persist_index_lock_ownership(
            common, plan["plan_sha256"], index_lock, result_tree, index)
        receipt = self.temp / "precommit-recovery.json"
        recovered = mc.policy_migration_apply(argparse.Namespace(
            plan=plan_path, output=receipt, authorize=True))
        self.assertEqual(recovered["old_head"], plan["head"])
        self.assertFalse(index_lock.exists())
        self.assertFalse(ownership.exists())
        self.assertEqual(command("git", "status", "--porcelain=v2", cwd=root), "")

    def test_metadata_policy_same_plan_recovers_completed_commit_with_stranded_index_and_temp(self) -> None:
        root, _ = self.legacy_policy_controller()
        plan_path, plan = self.policy_plan(root)
        receipt_path = self.temp / "recovery-apply.json"
        mc.policy_migration_apply(argparse.Namespace(plan=plan_path, output=receipt_path, authorize=True))
        receipt_path.unlink()
        index = Path(command("git", "rev-parse", "--path-format=absolute", "--git-path", "index", cwd=root))
        index_lock = Path(str(index) + ".lock")
        # Reproduce interruption after atomic exchange but before displaced-index
        # cleanup: prepared bytes are at index and the exact old index is at lock.
        index_lock.write_bytes(b"exact displaced pre-migration index bytes")
        common = Path(command("git", "rev-parse", "--path-format=absolute", "--git-common-dir", cwd=root))
        ownership = mc.index_lock_ownership_path(common, plan["plan_sha256"])
        mc.atomic_receipt(ownership, {
            "schema_version": "juno_metadata_policy_index_ownership.v2",
            "plan_sha256": plan["plan_sha256"],
            "expected_tree": command("git", "rev-parse", "HEAD^{tree}", cwd=root),
            "index_lock": mc.index_lock_identity(index),
            "displaced_index": mc.index_lock_identity(index_lock),
        })
        stale = mc.migration_temporary_endpoints(root, plan["plan_sha256"])[0]
        # Crash immediately after exchange leaves the retired policy preimage at
        # the plan-owned temporary endpoint.
        stale.write_bytes(plan["policy_before_utf8"].encode())
        recovered = mc.policy_migration_apply(argparse.Namespace(
            plan=plan_path, output=receipt_path, authorize=True))
        self.assertEqual(recovered["new_head"], command("git", "rev-parse", "HEAD", cwd=root))
        self.assertFalse(index_lock.exists())
        self.assertFalse(ownership.exists())
        self.assertFalse(stale.exists())
        self.assertEqual(command("git", "status", "--porcelain=v2", "--untracked-files=all", cwd=root), "")
        self.assertEqual(command("git", "rev-parse", "HEAD^", cwd=root), plan["head"])

    def test_metadata_policy_exact_unlink_restores_racing_replacement(self) -> None:
        directory = self.temp / "unlink-race"
        directory.mkdir()
        target = directory / "temporary"
        target.write_bytes(b"owned")
        descriptor = os.open(directory, os.O_RDONLY)
        expected = mc.endpoint_snapshot_at(descriptor, target.name)
        self.assertIsNotNone(expected)
        quarantine = self.temp / "quarantine"
        quarantine.mkdir()
        quarantine_fd = os.open(quarantine, os.O_RDONLY)
        original = mc.rename_noreplace_between
        injected = False
        def race(source_fd: int, source: str, destination_fd: int, destination: str) -> None:
            nonlocal injected
            if not injected:
                injected = True
                os.rename(target, directory / "preserved-owned")
                target.write_bytes(b"attacker")
            original(source_fd, source, destination_fd, destination)
        mc.rename_noreplace_between = race
        try:
            with self.assertRaisesRegex(mc.BoundaryError, "identity raced before quarantine"):
                mc.exact_unlink_endpoint_at(descriptor, target.name, expected, quarantine_fd)
        finally:
            mc.rename_noreplace_between = original
            os.close(quarantine_fd)
            os.close(descriptor)
        self.assertEqual(target.read_bytes(), b"attacker")
        self.assertEqual((directory / "preserved-owned").read_bytes(), b"owned")

    def test_metadata_policy_endpoint_writer_completes_partial_writes(self) -> None:
        path = self.temp / "partial-write"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        original = mc.os.write
        calls = 0
        def partial(fd: int, data: object) -> int:
            nonlocal calls
            calls += 1
            view = memoryview(data)
            return original(fd, view[:max(1, len(view) // 2)])
        mc.os.write = partial
        try:
            mc.write_all(descriptor, b"complete endpoint bytes")
        finally:
            mc.os.write = original
            os.close(descriptor)
        self.assertGreater(calls, 1)
        self.assertEqual(path.read_bytes(), b"complete endpoint bytes")

    def test_metadata_policy_index_exchange_refuses_in_place_byte_race(self) -> None:
        directory = self.temp / "index-byte-race"
        directory.mkdir()
        index = directory / "index"
        index_lock = directory / "index.lock"
        index.write_bytes(b"reviewed-real-index")
        index_lock.write_bytes(b"prepared-index")
        reviewed = mc.index_lock_identity(index)
        self.assertIsNotNone(reviewed)
        # Preserve the inode while changing bytes after the reviewed snapshot.
        with index.open("r+b") as stream:
            stream.seek(0)
            stream.write(b"raced!!!")
            stream.truncate()
        with self.assertRaisesRegex(mc.BoundaryError, "bytes or identity raced"):
            mc.atomic_index_publish(index_lock, index, reviewed)
        self.assertEqual(index.read_bytes(), b"raced!!!")
        self.assertEqual(index_lock.read_bytes(), b"prepared-index")

    def test_metadata_policy_recovery_refuses_same_tree_index_identity_drift(self) -> None:
        root, _ = self.legacy_policy_controller()
        plan_path, plan = self.policy_plan(root)
        receipt = self.temp / "index-identity-apply.json"
        mc.policy_migration_apply(argparse.Namespace(plan=plan_path, output=receipt, authorize=True))
        receipt.unlink()
        index = Path(command("git", "rev-parse", "--path-format=absolute", "--git-path", "index", cwd=root))
        common = Path(command("git", "rev-parse", "--path-format=absolute", "--git-common-dir", cwd=root))
        ownership = mc.persist_index_lock_ownership(
            common, plan["plan_sha256"], index, command("git", "rev-parse", "HEAD^{tree}", cwd=root), index)
        before = index.read_bytes()
        with index.open("r+b") as stream:
            stream.write(before)
            stream.flush()
            os.fsync(stream.fileno())
        self.assertEqual(command("git", "write-tree", cwd=root), command("git", "rev-parse", "HEAD^{tree}", cwd=root))
        with self.assertRaisesRegex(mc.BoundaryError, "ownership is stranded or unowned"):
            mc.policy_migration_apply(argparse.Namespace(plan=plan_path, output=receipt, authorize=True))
        self.assertTrue(ownership.exists())

    def test_metadata_policy_recovery_refuses_substituted_commit_identity(self) -> None:
        root, _ = self.legacy_policy_controller()
        plan_path, plan = self.policy_plan(root)
        receipt = self.temp / "identity-apply.json"
        mc.policy_migration_apply(argparse.Namespace(plan=plan_path, output=receipt, authorize=True))
        tip = command("git", "rev-parse", "HEAD", cwd=root)
        tree = command("git", "rev-parse", "HEAD^{tree}", cwd=root)
        message = command("git", "show", "-s", "--format=%B", cwd=root)
        environment = {**os.environ, "GIT_AUTHOR_NAME": "Juno Metadata Policy Migration",
                       "GIT_AUTHOR_EMAIL": "juno-controller@local.invalid",
                       "GIT_AUTHOR_DATE": "2001-01-01T00:00:00+00:00",
                       "GIT_COMMITTER_NAME": "Juno Metadata Policy Migration",
                       "GIT_COMMITTER_EMAIL": "juno-controller@local.invalid",
                       "GIT_COMMITTER_DATE": "2001-01-01T00:00:00+00:00"}
        replacement = subprocess.run(
            ["git", "commit-tree", tree, "-p", plan["head"], "-m", message], cwd=root,
            env=environment, text=True, capture_output=True, check=True).stdout.strip()
        command("git", "update-ref", plan["branch"], replacement, tip, cwd=root)
        receipt.unlink()
        with self.assertRaisesRegex(mc.BoundaryError, "not the exact completed"):
            mc.policy_migration_apply(argparse.Namespace(plan=plan_path, output=receipt, authorize=True))

    def test_metadata_policy_recovery_refuses_unowned_index_lock(self) -> None:
        root, _ = self.legacy_policy_controller()
        plan_path, plan = self.policy_plan(root)
        receipt = self.temp / "unowned-apply.json"
        mc.policy_migration_apply(argparse.Namespace(plan=plan_path, output=receipt, authorize=True))
        receipt.unlink()
        index = Path(command("git", "rev-parse", "--path-format=absolute", "--git-path", "index", cwd=root))
        index_lock = Path(str(index) + ".lock")
        # Even an identical prepared tree is not ownership proof without the
        # migration's durable inode/hash marker.
        index_lock.write_bytes(index.read_bytes())
        with self.assertRaisesRegex(mc.BoundaryError, "busy or unowned"):
            mc.policy_migration_apply(argparse.Namespace(plan=plan_path, output=receipt, authorize=True))
        index_lock.unlink()

    def test_metadata_policy_direct_endpoint_race_is_detected_without_overwrite(self) -> None:
        root, _ = self.legacy_policy_controller()
        plan_path, plan = self.policy_plan(root)
        pause = str(self.temp / "endpoint-pause")
        process = subprocess.Popen([
            "python3", str(SCRIPT), "metadata-policy-apply", "--plan", str(plan_path),
            "--output", str(self.temp / "endpoint-race.json"), "--authorize-metadata-policy-migration",
        ], cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
           env={**os.environ, "JUNO_METADATA_POLICY_MIGRATION_TEST_ENDPOINT_PAUSE_FILE": pause})
        for _ in range(500):
            ready = Path(pause + ".ready")
            if ready.exists(): break
            import time; time.sleep(0.01)
        else:
            process.kill(); self.fail("migration did not reach endpoint publication seam")
        relative = ready.read_text().strip(); endpoint = root / relative
        endpoint.write_text("owner raced bytes\n")
        Path(pause + ".release").write_text("release\n")
        _, stderr = process.communicate(timeout=15)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("endpoint raced", stderr)
        self.assertEqual(endpoint.read_text(), "owner raced bytes\n")
        self.assertNotEqual(command("git", "rev-parse", "HEAD", cwd=root), plan["head"])

    def test_metadata_policy_endpoint_symlink_race_is_refused_without_unlinking(self) -> None:
        root, _ = self.legacy_policy_controller()
        plan_path, _ = self.policy_plan(root)
        pause = str(self.temp / "endpoint-symlink-pause")
        process = subprocess.Popen([
            "python3", str(SCRIPT), "metadata-policy-apply", "--plan", str(plan_path),
            "--output", str(self.temp / "endpoint-symlink-race.json"),
            "--authorize-metadata-policy-migration",
        ], cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
           env={**os.environ, "JUNO_METADATA_POLICY_MIGRATION_TEST_ENDPOINT_PAUSE_FILE": pause})
        for _ in range(500):
            ready = Path(pause + ".ready")
            if ready.exists(): break
            import time; time.sleep(0.01)
        else:
            process.kill(); self.fail("migration did not reach endpoint symlink seam")
        endpoint = root / ready.read_text().strip()
        target = self.temp / "endpoint-symlink-target"
        target.write_bytes(endpoint.read_bytes())
        endpoint.unlink(); endpoint.symlink_to(target)
        Path(pause + ".release").write_text("release\n")
        _, stderr = process.communicate(timeout=15)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("unsafe endpoint", stderr)
        self.assertTrue(endpoint.is_symlink())
        self.assertEqual(endpoint.resolve(), target.resolve())

    def test_runtime_rebind_is_local_and_rollback_is_plan_only(self) -> None:
        self.prepare()
        before_head = command("git", "rev-parse", "HEAD", cwd=self.new_controller)
        before_tree = command("git", "write-tree", cwd=self.new_controller)
        newer = self.temp / "installed-2033/bin/yy"
        write(newer, "#!/bin/sh\nprintf 'yylo 2.0.33\\n'\n")
        newer.chmod(newer.stat().st_mode | stat.S_IXUSR)
        receipt = mc.runtime_rebind(
            argparse.Namespace(
                root=self.new_controller,
                branch="refs/heads/juno/controller-metadata-v1",
                runtime=newer,
                runtime_version="2.0.33",
                output=self.temp / "runtime-rebind.json",
            ),
            self.policy,
        )
        self.assertFalse(receipt["tracked_changes"])
        self.assertEqual(command("git", "rev-parse", "HEAD", cwd=self.new_controller), before_head)
        self.assertEqual(command("git", "write-tree", cwd=self.new_controller), before_tree)
        replay = mc.runtime_rebind(
            argparse.Namespace(
                root=self.new_controller,
                branch="refs/heads/juno/controller-metadata-v1",
                runtime=newer,
                runtime_version="2.0.33",
                output=self.temp / "runtime-rebind.json",
            ), self.policy)
        self.assertEqual(replay, receipt)

        cutover = mc.transition_plan(
            argparse.Namespace(plan=self.plan_path, output=self.temp / "cutover.json"),
            self.policy,
            False,
        )
        self.assertFalse(cutover["registration_change_authorized"])
        command("git", "config", "--worktree", "juno.workspace.role", "controller", cwd=self.new_controller)
        rollback = mc.transition_plan(
            argparse.Namespace(plan=self.plan_path, output=self.temp / "rollback.json"),
            self.policy,
            True,
        )
        self.assertEqual(rollback["outcome"], "planned_no_mutation")
        self.assertFalse(rollback["product_ref_mutation"])
        self.assertEqual(command("git", "rev-parse", "refs/heads/juno-mono-002", cwd=self.repo), self.product_head)

    def test_runtime_rebind_accepts_exact_semver_prerelease(self) -> None:
        self.prepare()
        newer = self.temp / "installed-prerelease/bin/yy"
        write(newer, "#!/bin/sh\nprintf 'yylo 2.1.3-rc.0.10\\n'\n")
        newer.chmod(newer.stat().st_mode | stat.S_IXUSR)
        receipt = mc.runtime_rebind(argparse.Namespace(
            root=self.new_controller,
            branch="refs/heads/juno/controller-metadata-v1",
            runtime=newer,
            runtime_version="2.1.3-rc.0.10",
            output=self.temp / "runtime-prerelease-rebind.json",
        ), self.policy)
        self.assertEqual(receipt["runtime"]["version"], "2.1.3-rc.0.10")
        self.assertEqual(command(
            "git", "config", "--worktree", "--get", "juno.controller.runtimeVersion",
            cwd=self.new_controller), "2.1.3-rc.0.10")

    def test_runtime_rebind_preflight_and_failure_restore_identity(self) -> None:
        self.prepare()
        runtime_file = self.new_controller / ".juno_task/runtime/identity.json"
        old_identity = runtime_file.read_bytes()
        old_version = command("git", "config", "--worktree", "--get", "juno.controller.runtimeVersion", cwd=self.new_controller)
        old_executable = command("git", "config", "--worktree", "--get", "juno.controller.runtimeExecutable", cwd=self.new_controller)
        newer = self.temp / "installed-transaction/bin/yy"
        write(newer, "#!/bin/sh\nprintf 'yylo 2.0.33\\n'\n")
        newer.chmod(newer.stat().st_mode | stat.S_IXUSR)

        args = argparse.Namespace(
            root=self.new_controller,
            branch="refs/heads/juno/controller-metadata-v1",
            runtime=newer,
            runtime_version="2.0.33",
            output=self.temp / "rebind-collision.json",
        )
        write(self.new_controller / "dirty.txt", "dirty\n")
        with self.assertRaisesRegex(mc.BoundaryError, "requires a clean"):
            mc.runtime_rebind(args, self.policy)
        (self.new_controller / "dirty.txt").unlink()

        args.output.write_text("{}\n")
        with self.assertRaisesRegex(mc.BoundaryError, "immutable receipt collision"):
            mc.runtime_rebind(args, self.policy)
        self.assertEqual(runtime_file.read_bytes(), old_identity)
        self.assertEqual(command("git", "config", "--worktree", "--get", "juno.controller.runtimeVersion", cwd=self.new_controller), old_version)
        self.assertEqual(command("git", "config", "--worktree", "--get", "juno.controller.runtimeExecutable", cwd=self.new_controller), old_executable)

        failing_args = argparse.Namespace(**{**vars(args), "output": self.temp / "rebind-write-failure.json"})
        original_atomic = mc.atomic_receipt
        try:
            mc.atomic_receipt = lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("receipt write failed"))
            with self.assertRaisesRegex(OSError, "receipt write failed"):
                mc.runtime_rebind(failing_args, self.policy)
        finally:
            mc.atomic_receipt = original_atomic
        self.assertEqual(runtime_file.read_bytes(), old_identity)
        self.assertEqual(command("git", "config", "--worktree", "--get", "juno.controller.runtimeVersion", cwd=self.new_controller), old_version)
        self.assertEqual(command("git", "config", "--worktree", "--get", "juno.controller.runtimeExecutable", cwd=self.new_controller), old_executable)
        self.assertEqual(command("git", "status", "--porcelain", cwd=self.new_controller), "")

        injected_args = argparse.Namespace(**{**vars(args), "output": self.temp / "rebind-injected-failure.json"})
        os.environ["JUNO_RUNTIME_REBIND_TEST_FAIL_AFTER_CONFIG"] = "1"
        try:
            with self.assertRaisesRegex(mc.BoundaryError, "injected runtime rebind failure"):
                mc.runtime_rebind(injected_args, self.policy)
        finally:
            os.environ.pop("JUNO_RUNTIME_REBIND_TEST_FAIL_AFTER_CONFIG", None)
        self.assertEqual(runtime_file.read_bytes(), old_identity)
        self.assertEqual(command("git", "config", "--worktree", "--get", "juno.controller.runtimeVersion", cwd=self.new_controller), old_version)
        self.assertEqual(command("git", "config", "--worktree", "--get", "juno.controller.runtimeExecutable", cwd=self.new_controller), old_executable)


if __name__ == "__main__":
    unittest.main()
