#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SCRIPTS.parents[1]
sys.path.insert(0, str(SCRIPTS))
import release_train as runtime  # noqa: E402


def run(root: Path, *args: str) -> str:
    return subprocess.check_output(list(args), cwd=root, text=True).strip()


class ReleaseTrainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        subprocess.run(["git", "init", "-b", "product", str(self.root)], check=True, stdout=subprocess.DEVNULL)
        run(self.root, "git", "config", "user.email", "test@example.invalid")
        run(self.root, "git", "config", "user.name", "Test")
        (self.root / ".gitignore").write_text(
            ".juno_task/runtime/\n.juno_task/config/controller-registration.json\n"
            ".agents/\n.claude/\n.pi/\n__pycache__/\n")
        (self.root / ".juno_task/scripts").mkdir(parents=True)
        for source in SCRIPTS.glob("*.py"):
            (self.root / ".juno_task/scripts" / source.name).write_bytes(source.read_bytes())
        template_scripts = self.root / "juno-code/src/templates/scripts"
        (template_scripts / "tests").mkdir(parents=True)
        (template_scripts / "release_train.py").write_bytes((SCRIPTS / "release_train.py").read_bytes())
        (self.root / ".juno_task/scripts/tests").mkdir(parents=True)
        test_bytes = Path(__file__).read_bytes()
        (self.root / ".juno_task/scripts/tests/test_release_train.py").write_bytes(test_bytes)
        (template_scripts / "tests/test_release_train.py").write_bytes(test_bytes)
        (self.root / ".juno_task/config").mkdir(parents=True)
        (self.root / ".juno_task/config/task-workspace.json").write_text(json.dumps({
            "schema_version": "juno_task_workspace_config.v1", "repository": ".",
            "target_ref": "refs/heads/product", "workspace_root": str(self.root / "workspaces"),
            "branch_prefix": "refs/heads/task-", "allowed_paths": ["src"], "selectable_paths": [],
            "controller_private_paths": [".juno_task"],
            "focused_validation": [{"id": "focused", "cwd": "src", "argv": ["true"],
                                    "timeout_seconds": 10, "max_output_bytes": 1024}],
            "full_suite_validation": {"id": "full", "cwd": "src", "argv": ["true"],
                                      "timeout_seconds": 10, "max_output_bytes": 1024}}) + "\n")
        (self.root / ".juno_task/config/risk-policy.json").write_text("{}\n")
        (self.root / "src").mkdir()
        (self.root / "src/.keep").write_text("fixture\n")
        (self.root / "yylo").mkdir()
        (self.root / "juno-code").mkdir(exist_ok=True)
        (self.root / "juno-code/package.json").write_text('{"version":"1.0.0"}\n')
        (self.root / "juno-code/package-lock.json").write_text('{"version":"1.0.0","packages":{"":{"version":"1.0.0"}}}\n')
        (self.root / ".juno_task/state").mkdir()
        self.board_path = self.root / ".juno_task/board.json"
        for task_id in ("OLD", "REQ", "DEP"):
            task_path = self.root / ".juno_task/tasks" / task_id[:2].lower() / f"{task_id}.md"
            task_path.parent.mkdir(parents=True, exist_ok=True)
            task_path.write_text(f"---\nid: {task_id}\nstatus: todo\n---\n")
        self.board = {
            "OLD": {"id": "OLD", "status": "in_progress", "blocked_by": [], "last_modified": "1"},
            "REQ": {"id": "REQ", "status": "in_progress", "blocked_by": [],
                    "related_tasks": ["OLD"], "last_modified": "1"},
            "DEP": {"id": "DEP", "status": "todo", "blocked_by": ["REQ"], "last_modified": "1"},
        }
        self.write_board()
        wrapper = self.root / ".juno_task/scripts/kanban.sh"
        wrapper.write_text("""#!/usr/bin/env python3
import hashlib,json,pathlib,sys
root=pathlib.Path(__file__).resolve().parents[1]; board_path=root/'board.json'
board=json.loads(board_path.read_text()); args=sys.argv[1:]
while args and args[0] in {'-f','--format'}: args=args[2:]
if args and args[0]=='--raw': args=args[1:]
def revision(task):
    return hashlib.sha256(json.dumps(task,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def receipt():
    if '--receipt-file' in args:
        path=pathlib.Path(args[args.index('--receipt-file')+1]); path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps({'fixture':True,'task_id':task_id,'operation':'update',
            'after_sha256':revision(board[task_id])})+'\\n')
if args[0]=='get' and args[1] in board:
    task=dict(board[args[1]])
    task['_related_tasks_details']=[board[item] for item in task.get('related_tasks',[]) if item in board]
    print(json.dumps([task])); raise SystemExit(0)
if args[0]=='history' and args[1] in board:
    task_id=args[1]; after=revision(board[task_id])
    print(json.dumps([{'event_id':'event-'+after[:12], 'task_id':task_id,
        'after_sha256':after, 'event_sha256':hashlib.sha256(('event:'+after).encode()).hexdigest()}])); raise SystemExit(0)
if args[0] in {'mark','update'}:
    task_id=args[args.index('--id')+1] if args[0]=='mark' else args[1]; task=board[task_id]
    if '--expected-revision' in args and args[args.index('--expected-revision')+1] != revision(task):
        print('stale task revision',file=sys.stderr); raise SystemExit(2)
    if args[0]=='mark':
        task['status']=args[1]; task['commit_hash']=args[args.index('--commit')+1]
        task['agent_response']=pathlib.Path(args[args.index('--response-file')+1]).read_text()
    else:
        if '--status' in args: task['status']=args[args.index('--status')+1]
        if '--commit' in args: task['commit_hash']=args[args.index('--commit')+1]
        if '--response-file' in args:
            task['agent_response']=pathlib.Path(args[args.index('--response-file')+1]).read_text()
        fields=task.setdefault('fields',{})
        for index,value in enumerate(args):
            if value=='--field':
                key,encoded=args[index+1].split('=',1); fields[key]=json.loads(encoded)
    task['last_modified']=revision(task); board_path.write_text(json.dumps(board)+'\\n'); receipt()
    print(json.dumps(task)); raise SystemExit(0)
raise SystemExit(2)
""")
        wrapper.chmod(0o755)
        self.state = {"schema_version": "juno_task_workspace_state.v1", "queues": {}, "tasks": {
            "OLD": {"task_id": "OLD", "state": "QUEUED", "target_ref": "refs/heads/product",
                    "enqueue_sequence": 1, "changed_paths": ["src/old"]},
            "REQ": {"task_id": "REQ", "state": "QUEUED", "target_ref": "refs/heads/product",
                    "enqueue_sequence": 2, "changed_paths": ["src/req"]}}}
        self.write_state()
        run(self.root, "git", "add", ".")
        run(self.root, "git", "commit", "-m", "fixture")
        self.base = run(self.root, "git", "rev-parse", "HEAD")
        self.declaration = self.root / "train.json"
        self.write_declaration(["REQ", "DEP"], [{"before": "REQ", "after": "DEP"}])
        run(self.root, "git", "add", "train.json")
        run(self.root, "git", "commit", "-m", "train")
        # Planning base is the product target after committing the declaration.
        self.base = run(self.root, "git", "rev-parse", "HEAD")
        self.write_declaration(["REQ", "DEP"], [{"before": "REQ", "after": "DEP"}])
        self.feasible = mock.patch("merge_queue.merge_plan", return_value={"plan_id": "f" * 64, "ready": True, "findings": []})
        self.feasible.start()
        self.owner = mock.patch("merge_queue.integration_owner_readback", return_value={
            "clean": True, "detached": True, "full_checkout": True, "role": "integration-owner",
            "authority": "protected-integration.v1", "head": self.base, "role_base": self.base, "submodules": []})
        self.owner.start()
        run(self.root, "git", "config", "juno.integration.ownerPath", str(self.root))

    def tearDown(self) -> None:
        self.owner.stop(); self.feasible.stop(); self.temp.cleanup()

    def write_board(self) -> None:
        self.board_path.write_text(json.dumps(self.board) + "\n")

    def write_state(self) -> None:
        (self.root / ".juno_task/state/tasks.json").write_text(json.dumps(self.state) + "\n")

    def write_declaration(self, required: list[str], edges: list[dict[str, str]]) -> None:
        common = run(self.root, "git", "rev-parse", "--path-format=absolute", "--git-common-dir")
        value = {"schema_version": runtime.DECLARATION_SCHEMA, "train_id": "rc-1", "revision": 1,
                 "requested_version": "1.0.1", "target_ref": "refs/heads/product",
                 "planning_base_sha": getattr(self, "base", "0" * 40), "required_tasks": required,
                 "optional_tasks": [], "dependencies": edges, "gates": ["release-full"],
                 "authority": {"controller_common_dir": common,
                               "release_command": "./scripts/release-juno-code.sh --set 1.0.1"},
                 "exclusions": ["push", "publish", "deploy"]}
        self.declaration.write_text(json.dumps(value, sort_keys=True) + "\n")

    def write_bootstrap_declaration(self) -> Path:
        path = self.root / "bootstrap-repair.json"
        path.write_text(json.dumps({
            "schema_version": runtime.BOOTSTRAP_DECLARATION_SCHEMA,
            "operation_id": "bootstrap-1", "revision": 1,
            "target_ref": "refs/heads/product", "planning_base_sha": self.base,
            "authority_task": "OLD", "repair_task": "REQ", "affected_tasks": ["DEP"],
            "exclusions": ["release", "tag", "push", "publish", "deploy", "cleanup"],
        }, sort_keys=True) + "\n")
        return path

    def write_shadow_baseline(self) -> Path:
        path = self.root / "shadow-baseline.json"
        path.write_text(json.dumps({
            "schema_version": "juno_agent_session_telemetry_collection.v1", "session_count": 21,
            "scorecard": {"schema_version": "juno_agent_session_scorecard.v1",
                "session_count": 21, "duplicate_command_executions": 12,
                "model_lifecycle_calls": {"assistant_turns": 100, "model_changes": 2,
                                          "compactions": 3, "provider_errors": 4},
                "cas_count": 9, "cache_read_tokens": 362700000,
                "phase_seconds": {"implementation": 1000, "merge": 200},
                "wait_seconds_by_cause": {"polling": 500}},
        }, sort_keys=True) + "\n")
        return path

    def test_deterministic_non_mutating_json_and_human_projection(self) -> None:
        before = run(self.root, "git", "status", "--porcelain=v1", "--untracked-files=all")
        first = runtime.build_plan(self.root, self.declaration)
        second = runtime.build_plan(self.root, self.declaration)
        self.assertEqual(first, second)
        self.assertIn(first["plan_id"], runtime.human(first))
        self.assertEqual(before, run(self.root, "git", "status", "--porcelain=v1", "--untracked-files=all"))

    def test_fifo_conflict_dependency_blocker_and_parallel_lanes(self) -> None:
        report = runtime.build_plan(self.root, self.declaration)
        self.assertEqual(["OLD"], [row["task_id"] for row in report["fifo"]["older_unrelated"]])
        self.assertIn("queue.older_unrelated", [row["code"] for row in report["blockers"]])
        self.assertIn("dependency.unmet", [row["code"] for row in report["blockers"]])
        self.assertEqual("yy merge next", report["next_command"])
        self.board["DEP"]["blocked_by"] = []; self.write_board()
        self.write_declaration(["REQ", "DEP"], [])
        parallel = runtime.build_plan(self.root, self.declaration)
        self.assertEqual([["REQ", "DEP"]], parallel["parallel_lanes"])

    def test_cycle_is_explicit(self) -> None:
        self.board["REQ"]["blocked_by"] = ["DEP"]; self.write_board()
        self.write_declaration(["REQ", "DEP"], [{"before": "REQ", "after": "DEP"}, {"before": "DEP", "after": "REQ"}])
        report = runtime.build_plan(self.root, self.declaration)
        self.assertIn("dependency.cycle", [row["code"] for row in report["blockers"]])

    def test_stale_kanban_and_target_identity_refuse_shared_gate(self) -> None:
        plan = runtime.build_plan(self.root, self.declaration)
        plan_path = self.declaration.with_suffix(".plan.json"); plan_path.write_text(runtime.canonical(plan))
        self.assertEqual(plan, runtime.check_plan(self.root, plan_path, "merge"))
        with self.assertRaisesRegex(runtime.ReleaseTrainError, "exact FIFO merge head"):
            runtime.check_plan(self.root, plan_path, "merge", "REQ")
        self.board["REQ"]["last_modified"] = "2"; self.write_board()
        with self.assertRaisesRegex(runtime.ReleaseTrainError, "stale"):
            runtime.check_plan(self.root, plan_path, "merge", "REQ")
        self.board["REQ"]["last_modified"] = "1"; self.write_board()
        (self.root / "movement").write_text("x")
        run(self.root, "git", "add", "movement"); run(self.root, "git", "commit", "-m", "move")
        moved = runtime.build_plan(self.root, self.declaration)
        self.assertTrue(moved["target_moved"])

    def test_missing_runtime_blocks(self) -> None:
        (self.root / ".juno_task/scripts/release_train.py").unlink()
        report = runtime.build_plan(self.root, self.declaration)
        self.assertIn("runtime.missing", [row["code"] for row in report["blockers"]])

    def test_clean_ready_release_and_version_gate(self) -> None:
        self.board["REQ"].update(status="done", commit_hash=self.base, blocked_by=[])
        self.board["DEP"].update(status="done", commit_hash=self.base, blocked_by=[])
        self.write_board(); self.state["tasks"] = {}; self.write_state()
        report = runtime.build_plan(self.root, self.declaration)
        self.assertTrue(report["release_ready"])
        plan_path = self.declaration.with_suffix(".plan.json"); plan_path.write_text(runtime.canonical(report))
        self.assertEqual(report, runtime.check_plan(self.root, plan_path, "release", requested_version="1.0.1"))
        with self.assertRaisesRegex(runtime.ReleaseTrainError, "version differs"):
            runtime.check_plan(self.root, plan_path, "release", requested_version="1.0.2")

    def lean_checkout(self) -> None:
        # A lean sparse controller: tracked product files are absent from the
        # working tree while the protected target still owns their content.
        run(self.root, "git", "sparse-checkout", "set", "--no-cone",
            "/.juno_task/**", "/src/**", "/yylo/**", "/train.json")
        self.assertFalse((self.root / "juno-code/package.json").exists())
        self.assertFalse((self.root / "juno-code/package-lock.json").exists())

    def finish_board(self) -> None:
        self.board["REQ"].update(status="done", commit_hash=self.base, blocked_by=[])
        self.board["DEP"].update(status="done", commit_hash=self.base, blocked_by=[])
        self.write_board(); self.state["tasks"] = {}; self.write_state()

    def test_lean_sparse_controller_plans_release_from_target_tree(self) -> None:
        # rejV9U: version identity must come from the protected target
        # generation, so a lean controller with absent product files still
        # reaches release_ready when package/lock identities are exact.
        self.lean_checkout()
        self.finish_board()
        report = runtime.build_plan(self.root, self.declaration)
        self.assertEqual([], report["blockers"])
        self.assertTrue(report["release_ready"])
        self.assertEqual("1.0.0", report["release_preconditions"]["current_version"])
        self.assertEqual({"juno-code/package.json", "juno-code/package-lock.json"},
                         set(report["identities"]["package_sha256"]))
        self.assertTrue(all(report["identities"]["package_sha256"].values()))
        plan_path = self.declaration.with_suffix(".plan.json"); plan_path.write_text(runtime.canonical(report))
        self.assertEqual(report, runtime.check_plan(self.root, plan_path, "release", requested_version="1.0.1"))

    def commit_target_drift(self, mutate) -> None:
        run(self.root, "git", "sparse-checkout", "disable")
        mutate()
        run(self.root, "git", "add", "-A", "juno-code")
        run(self.root, "git", "commit", "-m", "target drift")
        self.base = run(self.root, "git", "rev-parse", "HEAD")
        self.write_declaration(["REQ", "DEP"], [])
        self.lean_checkout()

    def prepare_epoch(self) -> tuple[str, str]:
        """Queue two immutable divergent candidates with exact closure evidence."""
        tree = run(self.root, "git", "rev-parse", f"{self.base}^{{tree}}")
        req = subprocess.check_output(["git", "commit-tree", tree, "-p", self.base], cwd=self.root,
                                      text=True, input="REQ candidate\n").strip()
        old = subprocess.check_output(["git", "commit-tree", tree, "-p", self.base], cwd=self.root,
                                      text=True, input="OLD candidate\n").strip()
        for sequence, (task_id, tip) in enumerate((("OLD", old), ("REQ", req)), 1):
            self.state["tasks"][task_id].update({
                "tip_sha": tip, "enqueue_sequence": sequence,
                "review_ready_closure": {"schema_version": "juno_task_review_ready_closure.v1",
                                         "closure_sha256": "c" * 64,
                                         "tip_sha": tip,
                                         "tree_sha": tree},
                "validation": [{"status": "passed", "receipt_id": task_id}],
            })
        self.write_state()
        self.board["OLD"].update(status="in_progress", blocked_by=[])
        self.board["REQ"].update(status="in_progress", blocked_by=[])
        self.write_board()
        self.write_declaration(["REQ"], [])
        declaration = json.loads(self.declaration.read_text())
        declaration["optional_tasks"] = ["OLD"]
        self.declaration.write_text(json.dumps(declaration, sort_keys=True) + "\n")
        return old, req

    def prepare_three_member_epoch(self) -> list[str]:
        """Reproduce the real three-member stale Ledger projection on real Git tips."""
        tree = run(self.root, "git", "rev-parse", f"{self.base}^{{tree}}")
        tips = []
        for sequence, task_id in enumerate(("OLD", "REQ", "DEP"), 1):
            tip = subprocess.check_output(["git", "commit-tree", tree, "-p", self.base], cwd=self.root,
                                          text=True, input=f"{task_id} candidate\n").strip()
            tips.append(tip)
            self.state["tasks"].setdefault(task_id, {"task_id": task_id, "state": "QUEUED",
                "target_ref": "refs/heads/product", "changed_paths": [f"src/{task_id.lower()}"]})
            self.state["tasks"][task_id].update({"tip_sha": tip, "enqueue_sequence": sequence,
                "review_ready_closure": {"schema_version": "juno_task_review_ready_closure.v1",
                    "closure_sha256": "c" * 64, "tip_sha": tip, "tree_sha": tree},
                "validation": [{"status": "passed", "receipt_id": task_id}]})
            self.board[task_id].update(status="in_progress", blocked_by=[])
        self.write_state(); self.write_board()
        self.write_declaration(["OLD", "REQ", "DEP"], [
            {"before": "OLD", "after": "REQ"}, {"before": "REQ", "after": "DEP"}])
        return tips

    def prepare_four_member_epoch(self) -> list[str]:
        """Extend the stale projection fixture through a fourth exact FIFO member."""
        tips = self.prepare_three_member_epoch()
        task_id = "FOUR"
        task_path = self.root / ".juno_task/tasks/fo/FOUR.md"
        task_path.parent.mkdir(parents=True, exist_ok=True)
        task_path.write_text("---\nid: FOUR\nstatus: in_progress\n---\n")
        tree = run(self.root, "git", "rev-parse", f"{self.base}^{{tree}}")
        tip = subprocess.check_output(["git", "commit-tree", tree, "-p", self.base], cwd=self.root,
                                      text=True, input="FOUR candidate\n").strip()
        tips.append(tip)
        self.state["tasks"][task_id] = {"task_id": task_id, "state": "QUEUED",
            "target_ref": "refs/heads/product", "changed_paths": ["src/four"],
            "tip_sha": tip, "enqueue_sequence": 4,
            "review_ready_closure": {"schema_version": "juno_task_review_ready_closure.v1",
                "closure_sha256": "c" * 64, "tip_sha": tip, "tree_sha": tree},
            "validation": [{"status": "passed", "receipt_id": task_id}]}
        self.board[task_id] = {"id": task_id, "status": "in_progress", "blocked_by": [],
                               "last_modified": "1"}
        self.write_state(); self.write_board()
        self.write_declaration(["OLD", "REQ", "DEP", "FOUR"], [
            {"before": "OLD", "after": "REQ"}, {"before": "REQ", "after": "DEP"},
            {"before": "DEP", "after": "FOUR"}])
        return tips

    def commit_files_tree(self, parent: str, paths: list[str], content: str, message: str) -> str:
        with tempfile.NamedTemporaryFile() as stream:
            index = stream.name
        environment = {**os.environ, "GIT_INDEX_FILE": index,
                       "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
                       "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
                       "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
                       "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z"}
        subprocess.run(["git", "read-tree", parent], cwd=self.root, env=environment, check=True)
        for path in paths:
            blob = subprocess.check_output(["git", "hash-object", "-w", "--stdin"], cwd=self.root,
                                           env=environment, text=True, input=content).strip()
            subprocess.run(["git", "update-index", "--add", "--cacheinfo", "100644", blob, path],
                           cwd=self.root, env=environment, check=True)
        tree = subprocess.check_output(["git", "write-tree"], cwd=self.root,
                                       env=environment, text=True).strip()
        return subprocess.check_output(["git", "commit-tree", tree, "-p", parent], cwd=self.root,
                                       env=environment, text=True, input=message + "\n").strip()

    def prepare_serial_conflict_epoch(self, *, preserve_runtime: bool = False,
                                      later_count: int = 4,
                                      second_conflicts: bool = True) -> tuple[str, str, list[str]]:
        """Build the portable six-member rc7/rc8 order and two-conflict topology."""
        runtime_name = "release_train-fixture.py" if preserve_runtime else "release_train.py"
        conflict_paths = [".juno_task/managed-assets.json", f".juno_task/scripts/{runtime_name}",
            "juno-code/src/cli/__tests__/release-command.test.ts",
            "juno-code/src/cli/commands/release.ts",
            f"juno-code/src/templates/scripts/{runtime_name}"]
        for path in conflict_paths:
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("rc7-rc8 base\n")
        run(self.root, "git", "add", *conflict_paths)
        run(self.root, "git", "commit", "-m", "rc7 rc8 conflict base")
        candidate_base = run(self.root, "git", "rev-parse", "HEAD")
        first = self.commit_files_tree(candidate_base, conflict_paths, "pA6M9l\n", "pA6M9l candidate")
        second_paths = conflict_paths if second_conflicts else ["src/0y4ljs.txt"]
        second = self.commit_files_tree(candidate_base, second_paths, "0y4ljs\n", "0y4ljs candidate")
        for path in conflict_paths:
            (self.root / path).write_text("protected target\n")
        run(self.root, "git", "add", *conflict_paths)
        run(self.root, "git", "commit", "-m", "protected target conflict")
        self.base = run(self.root, "git", "rev-parse", "HEAD")
        # Model the immutable rc7 receipt-bound pA6M9l both-parent composition.
        with tempfile.TemporaryDirectory() as temporary:
            clone = Path(temporary) / "composition"
            run(Path(temporary), "git", "clone", "--quiet", str(self.root), str(clone))
            run(clone, "git", "checkout", "--quiet", "--detach", self.base)
            merge = subprocess.run(["git", "merge", "--no-ff", "--no-commit", first], cwd=clone,
                                   text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            self.assertNotEqual(0, merge.returncode)
            run(clone, "git", "checkout", "--theirs", "--", *conflict_paths)
            run(clone, "git", "add", "-A")
            run(clone, "git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                "commit", "--quiet", "-m", "receipt-bound pA6M9l repair")
            proven = run(clone, "git", "rev-parse", "HEAD")
            run(self.root, "git", "fetch", "--quiet", str(clone), proven)
        proven_tree = run(self.root, "git", "rev-parse", f"{proven}^{{tree}}")
        epoch_root = self.root / ".juno_task/runtime/release-epochs/rc-0"
        receipt_path = epoch_root / "receipts/0003-repair_consumed.json"
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt = {"schema_version": runtime.EPOCH_RECEIPT_SCHEMA, "epoch_id": "rc-0",
                   "transition": "REPAIR_CONSUMED", "detail": {"repair_commit": proven}}
        receipt_path.write_text(json.dumps(receipt, sort_keys=True) + "\n")
        receipt_sha = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
        state = {"epoch_id": "rc-0", "receipts": [{"transition": "REPAIR_CONSUMED",
                 "path": str(receipt_path), "sha256": receipt_sha}], "composition": {"commits": [{
                 "task_id": "pA6M9l", "pre_sha": self.base, "candidate_tip": first,
                 "merge_commit": proven, "post_tree": proven_tree}]}}
        (epoch_root / "state.json").write_text(json.dumps(state, sort_keys=True) + "\n")
        later_ids = ["U2rjMN", "znI3LO", "e99k0C", "GsKDx6"][:later_count]
        later = []
        for task_id in later_ids:
            path = f"src/{task_id}.txt"
            later.append((task_id, self.commit_files_tree(
                candidate_base, [path], task_id + "\n", task_id + " candidate"), [path]))
        members = [("pA6M9l", first, conflict_paths), ("0y4ljs", second, second_paths), *later]
        self.state["tasks"] = {}
        for sequence, (task_id, tip, changed_paths) in enumerate(members, 1):
            task_path = self.root / ".juno_task/tasks" / task_id[:2].lower() / f"{task_id}.md"
            task_path.parent.mkdir(parents=True, exist_ok=True)
            task_path.write_text(f"---\nid: {task_id}\nstatus: in_progress\n---\n")
            self.board[task_id] = {"id": task_id, "status": "in_progress", "blocked_by": [],
                                   "last_modified": "rc7-rc8-fixture"}
            tree = run(self.root, "git", "rev-parse", f"{tip}^{{tree}}")
            self.state["tasks"][task_id] = {"task_id": task_id, "state": "QUEUED",
                "target_ref": "refs/heads/product", "enqueue_sequence": sequence,
                "changed_paths": changed_paths, "tip_sha": tip,
                "review_ready_closure": {"schema_version": "juno_task_review_ready_closure.v1",
                    "closure_sha256": hashlib.sha256(task_id.encode()).hexdigest(),
                    "tip_sha": tip, "tree_sha": tree},
                "validation": [{"status": "passed", "receipt_id": task_id}]}
        self.write_board(); self.write_state()
        self.assertIsNotNone(runtime._proven_forecast_composition(
            self.root, self.root, "pA6M9l", self.base, first, "rc-1"))
        order = [task_id for task_id, _, _ in members]
        self.write_declaration(order, [{"before": order[index], "after": order[index + 1]}
                                       for index in range(len(order) - 1)])
        self.authorize_conflict_envelope()
        return first, second, conflict_paths

    def authorize_conflict_envelope(self) -> tuple[dict, Path]:
        initial = runtime.build_epoch_plan(self.root, self.declaration)
        envelope = initial["conflict_manifest"]["conservative_envelope"]
        authority_path = self.root / "conflict-authority.json"
        authority = {"schema_version": runtime.CONFLICT_AUTHORITY_SCHEMA, "revision": 1,
            "train_id": initial["epoch_id"],
            "input_identity_sha256": runtime.digest(runtime._forecast_input_identity(initial)),
            "logical_sets": [{"set_id": "rc7-rc8-serial", "ordered_task_ids":
                [row["task_id"] for row in envelope["ordered_members"]],
                "permitted_paths": sorted({path for row in envelope["ordered_members"]
                                           for path in row["possible_conflict_paths"]}),
                "classification": "authorization_neutral"}],
            "repair_budget": 1, "grouped_worker": True,
            "risk": {"ambiguous": False, "sensitive": False, "destructive": False,
                     "scope_expansion": False}}
        authority_path.write_text(json.dumps(authority, sort_keys=True) + "\n")
        declaration = json.loads(self.declaration.read_text())
        declaration["conflict_authority"] = {"path": str(authority_path),
            "sha256": hashlib.sha256(authority_path.read_bytes()).hexdigest()}
        self.declaration.write_text(json.dumps(declaration, sort_keys=True) + "\n")
        return authority, authority_path

    def test_epoch_seal_is_complete_immutable_and_idempotent(self) -> None:
        self.prepare_epoch()
        plan = runtime.build_epoch_plan(self.root, self.declaration)
        self.assertEqual(["OLD", "REQ"], [row["task_id"] for row in plan["members"]])
        self.assertEqual("c" * 64, plan["members"][0]["complete_input_identity"]["closure_sha256"])
        sealed = runtime.seal_epoch(self.root, self.declaration)
        self.assertEqual("sealed", sealed["outcome"])
        self.assertEqual("SEALED", sealed["epoch"]["state"])
        self.assertEqual("already_sealed", runtime.seal_epoch(self.root, self.declaration)["outcome"])
        # A post-cutoff queue candidate cannot grow the immutable seal.
        self.state["tasks"]["DEP"] = {"task_id": "DEP", "state": "QUEUED",
            "target_ref": "refs/heads/product", "enqueue_sequence": 3,
            "tip_sha": self.base, "complete_input_identity": "d" * 64,
            "validation": [{"status": "passed"}], "changed_paths": ["src/dep"]}
        self.write_state()
        current = runtime.read_epoch(self.root, "rc-1")
        self.assertEqual(["OLD", "REQ"], [row["task_id"] for row in current["seal"]["members"]])

    def test_wave_economics_and_explicit_selection_route_without_sealing(self) -> None:
        self.prepare_epoch()
        (self.root / ".juno_task/config/risk-policy.json").write_text(json.dumps({
            "shared_infrastructure_paths": ["src/**"], "high_risk_paths": ["src/**"]}) + "\n")
        plan = runtime.build_epoch_plan(self.root, self.declaration)
        economics = plan["economics"]
        self.assertEqual("epoch_delivery", economics["recommendation"])
        self.assertEqual({"focused": 2, "per_candidate_broad": 2,
                          "aggregate_broad": 1, "avoided_broad": 1},
                         economics["command_counts"])
        self.assertEqual(["OLD", "REQ"], economics["membership"])
        selected = runtime.select_epoch_delivery(self.root, self.declaration)
        self.assertEqual("selected", selected["outcome"])
        self.assertEqual("routing_only_explicit_seal_still_required",
                         selected["selection"]["authority"])
        self.assertFalse(runtime.epoch_state_path(self.root, "rc-1").exists())
        self.assertEqual("already_selected",
                         runtime.select_epoch_delivery(self.root, self.declaration)["outcome"])

    def test_epoch_status_projection_is_bounded_and_actionable(self) -> None:
        self.prepare_epoch()
        runtime.seal_epoch(self.root, self.declaration)
        state = runtime.read_epoch(self.root, "rc-1")
        state["receipts"].extend({"transition": "OBSERVED", "sha256": str(index) * 64,
                                  "path": "/private/receipt"}
                                 for index in range(1, 5))
        (runtime.epoch_state_path(self.root, "rc-1")).write_text(
            json.dumps(state, sort_keys=True) + "\n")
        projected = runtime.epoch_status_projection(self.root, "rc-1")
        self.assertEqual("juno_release_epoch_status_projection.v1", projected["schema_version"])
        self.assertEqual("SEALED", projected["state"])
        self.assertEqual(5, projected["receipt_count"])
        self.assertTrue(projected["history_truncated"])
        self.assertIn("drive", projected["next_action"])
        self.assertNotIn("seal", projected)
        self.assertNotIn("receipts", projected)
        self.assertLess(len(runtime.canonical(projected)), 4096)

    def test_epoch_seal_refuses_required_missing_closure_without_state(self) -> None:
        self.prepare_epoch()
        self.state["tasks"]["REQ"].pop("review_ready_closure")
        self.write_state()
        with self.assertRaisesRegex(runtime.ReleaseTrainError, "candidate.complete_input_missing:REQ"):
            runtime.seal_epoch(self.root, self.declaration)
        self.assertFalse(runtime.epoch_state_path(self.root, "rc-1").exists())

    def test_rc7_rc8_serial_conflicts_fit_one_conservative_repair_set(self) -> None:
        first, second, conflict_paths = self.prepare_serial_conflict_epoch()
        before_status = run(self.root, "git", "status", "--porcelain=v1", "--untracked-files=all")
        before_refs = run(self.root, "git", "for-each-ref", "--format=%(refname) %(objectname)")
        before_objects = run(self.root, "git", "count-objects", "-v")
        plan = runtime.build_epoch_plan(self.root, self.declaration)
        repeated = runtime.build_epoch_plan(self.root, self.declaration)
        manifest = plan["conflict_manifest"]
        self.assertEqual(plan, repeated)
        self.assertEqual(["pA6M9l"], [row["task_id"] for row in manifest["conflicts"]])
        self.assertEqual([sorted(conflict_paths)],
                         [row["conflict_paths"] for row in manifest["conflicts"]])
        self.assertEqual([first], [row["candidate_tip"] for row in manifest["conflicts"]])
        self.assertEqual(1, manifest["required_conflict_count"])
        self.assertTrue(manifest["member_accounting_complete"])
        self.assertTrue(manifest["forecast_complete"])
        self.assertFalse(manifest["exact_composition_complete"])
        self.assertTrue(manifest["policy_repair_budget_feasible"])
        self.assertTrue(manifest["repair_budget_feasible"])
        self.assertEqual(1, manifest["required_logical_repair_set_count"])
        self.assertEqual(["0y4ljs", "U2rjMN", "znI3LO", "e99k0C", "GsKDx6"],
                         [row["task_id"] for row in manifest["indeterminate_members"]])
        self.assertEqual(["pA6M9l", "0y4ljs", "U2rjMN", "znI3LO", "e99k0C", "GsKDx6"],
                         [row["task_id"] for row in manifest["compositions"]])
        self.assertEqual("pA6M9l", manifest["unresolved_boundary"]["task_id"])
        envelope = manifest["conservative_envelope"]
        self.assertEqual(["pA6M9l", "0y4ljs", "U2rjMN", "znI3LO", "e99k0C", "GsKDx6"],
                         [row["task_id"] for row in envelope["ordered_members"]])
        self.assertTrue(envelope["complete"])
        self.assertEqual("authorization_neutral_logical_conflict_set.v1",
                         manifest["identity"]["forecast_policy"]["repair_unit"])
        self.assertEqual("frozen_unknown_suffix.v1",
                         manifest["identity"]["forecast_policy"]["logical_conflict_set_grouping"])
        self.assertEqual("explicit_replay_repair_required",
                         manifest["conflicts"][0]["forecast_resolution"])
        self.assertEqual(runtime.digest({key: value for key, value in manifest.items()
                                         if key != "manifest_sha256"}), manifest["manifest_sha256"])
        self.assertEqual(before_status, run(self.root, "git", "status", "--porcelain=v1",
                                            "--untracked-files=all"))
        self.assertEqual(before_refs, run(self.root, "git", "for-each-ref",
                                          "--format=%(refname) %(objectname)"))
        self.assertEqual(before_objects, run(self.root, "git", "count-objects", "-v"))
        sealed = runtime.seal_epoch(self.root, self.declaration)
        self.assertEqual("sealed", sealed["outcome"])
        self.assertTrue(runtime.epoch_state_path(self.root, "rc-1").is_file())
        self.assertEqual(before_refs, run(self.root, "git", "for-each-ref",
                                          "--format=%(refname) %(objectname)"))

    def test_five_member_grouped_repair_refuses_unresolved_suffix_before_consumption(self) -> None:
        self.prepare_serial_conflict_epoch(later_count=3)
        sealed = runtime.seal_epoch(self.root, self.declaration)
        token = sealed["lease_token"]
        state = runtime.drive_epoch(self.root, "rc-1", token)
        self.assertEqual("RECOVERING", state["state"])
        packet = state["conflict"]
        self.assertEqual(["pA6M9l", "0y4ljs", "U2rjMN", "znI3LO", "e99k0C"],
                         packet["logical_conflict_set"]["ordered_task_ids"])
        self.assertEqual(packet["logical_conflict_set"]["ordered_task_ids"],
                         [row["task_id"] for row in packet["frozen_logical_members"]])
        checkout = Path(state["composition"]["worktree"])
        run(checkout, "git", "checkout", "--theirs", "--", *packet["conflict_paths"])
        run(checkout, "git", "add", "-A")
        run(checkout, "git", "-c", "user.name=Fixture", "-c",
            "user.email=fixture@example.invalid", "commit", "--quiet", "-m", "grouped repair")
        receipt_path = self.root / "grouped-worker-receipt.json"
        receipt_path.write_text(json.dumps({
            "schema_version": "juno_managed_agent_runner.v1", "mode": "worker",
            "state": "succeeded", "session_id": "one-model-session",
            "identity": {"admission_kind": "sealed_release_epoch_conflict",
                "epoch_id": "rc-1", "task_id": packet["task_id"],
                "ours_sha": packet["ours_sha"], "theirs_sha": packet["theirs_sha"],
                "conflict_sha256": runtime.digest(packet),
                "logical_conflict_set": packet["logical_conflict_set"]}}, sort_keys=True) + "\n")
        refused = runtime.apply_conflict_repair(self.root, "rc-1", receipt_path, token)
        self.assertEqual("NEEDS_OPERATOR", refused["state"])
        self.assertEqual("repair.grouped_suffix_unresolved",
                         refused["operator_packet"]["reason_code"])
        self.assertEqual("0y4ljs", refused["operator_packet"]["task_id"])
        self.assertFalse(refused["operator_packet"]["receipt_consumed"])
        self.assertNotIn("conflict_repair", refused)
        self.assertNotIn("REPAIR_CONSUMED", [row["transition"] for row in refused["receipts"]])
        projection = runtime.epoch_status_projection(self.root, "rc-1")
        self.assertEqual("NEEDS_OPERATOR", projection["state"])
        self.assertIn("fresh successor epoch", projection["next_action"])
        self.assertIn("do not rerun repair", projection["next_action"])
        self.assertEqual(self.base, run(self.root, "git", "rev-parse", "refs/heads/product"))
        self.assertTrue(receipt_path.is_file())

    def test_five_member_grouped_repair_consumes_only_after_complete_suffix_proof(self) -> None:
        self.prepare_serial_conflict_epoch(later_count=3, second_conflicts=False)
        sealed = runtime.seal_epoch(self.root, self.declaration)
        token = sealed["lease_token"]
        state = runtime.drive_epoch(self.root, "rc-1", token)
        packet = state["conflict"]
        checkout = Path(state["composition"]["worktree"])
        run(checkout, "git", "checkout", "--theirs", "--", *packet["conflict_paths"])
        run(checkout, "git", "add", "-A")
        run(checkout, "git", "-c", "user.name=Fixture", "-c",
            "user.email=fixture@example.invalid", "commit", "--quiet", "-m", "grouped repair")
        receipt_path = self.root / "complete-grouped-worker-receipt.json"
        receipt_path.write_text(json.dumps({
            "schema_version": "juno_managed_agent_runner.v1", "mode": "worker",
            "state": "succeeded", "session_id": "one-model-session",
            "identity": {"admission_kind": "sealed_release_epoch_conflict",
                "epoch_id": "rc-1", "task_id": packet["task_id"],
                "ours_sha": packet["ours_sha"], "theirs_sha": packet["theirs_sha"],
                "conflict_sha256": runtime.digest(packet),
                "logical_conflict_set": packet["logical_conflict_set"]}}, sort_keys=True) + "\n")
        consumed = runtime.apply_conflict_repair(self.root, "rc-1", receipt_path, token)
        proof = consumed["conflict_repair"]["suffix_validation"]
        self.assertEqual("complete", proof["status"])
        self.assertEqual(["0y4ljs", "U2rjMN", "znI3LO", "e99k0C"],
                         proof["ordered_task_ids"])
        self.assertEqual(1, consumed["conflict_repair"]["model_sessions"])
        self.assertIn("REPAIR_CONSUMED", [row["transition"] for row in consumed["receipts"]])
        composed = runtime.compose_epoch(self.root, consumed)
        self.assertEqual("VALIDATING", composed["state"])
        self.assertEqual(5, len(composed["composition"]["commits"]))
        self.assertEqual(proof["final_tree"],
                         run(Path(composed["composition"]["worktree"]), "git",
                             "rev-parse", "HEAD^{tree}"))

    def test_follow_on_conflict_after_consumed_budget_has_truthful_terminal_projection(self) -> None:
        self.prepare_serial_conflict_epoch(later_count=3)
        sealed = runtime.seal_epoch(self.root, self.declaration)
        token = sealed["lease_token"]
        state = runtime.drive_epoch(self.root, "rc-1", token)
        packet = state["conflict"]
        checkout = Path(state["composition"]["worktree"])
        run(checkout, "git", "checkout", "--theirs", "--", *packet["conflict_paths"])
        run(checkout, "git", "add", "-A")
        run(checkout, "git", "-c", "user.name=Fixture", "-c",
            "user.email=fixture@example.invalid", "commit", "--quiet", "-m", "consumed repair")
        head = run(checkout, "git", "rev-parse", "HEAD")
        first = state["seal"]["members"][0]
        state["composition"]["commits"].append({"task_id": first["task_id"],
            "candidate_tip": first["tip_sha"], "pre_sha": packet["ours_sha"],
            "pre_tree": run(checkout, "git", "rev-parse", f"{packet['ours_sha']}^{{tree}}"),
            "merge_commit": head, "post_tree": run(checkout, "git", "rev-parse", "HEAD^{tree}"),
            "parents": run(checkout, "git", "show", "-s", "--format=%P", head).split(),
            "ordering_reason": "bounded_conflict_repair"})
        state["composition"]["tip_sha"] = head
        state["conflict_repair"] = {"schema_version": "juno_release_epoch_grouped_repair.v1",
                                     "repair_commit": head, "model_sessions": 1}
        state.pop("conflict")
        state["state"] = "COMPOSING"
        runtime.atomic_json(runtime.epoch_state_path(self.root, "rc-1"), state)
        terminal = runtime.compose_epoch(self.root, state)
        self.assertEqual("NEEDS_OPERATOR", terminal["state"])
        self.assertEqual("repair.grouped_suffix_unresolved",
                         terminal["operator_packet"]["reason_code"])
        self.assertEqual(1, terminal["operator_packet"]["repair_budget_consumed"])
        self.assertNotIn("conflict", terminal)
        self.assertIn("fresh successor epoch",
                      runtime.epoch_status_projection(self.root, "rc-1")["next_action"])
        with self.assertRaisesRegex(runtime.ReleaseTrainError, "no bounded conflict repair"):
            runtime.apply_conflict_repair(self.root, "rc-1", self.root / "unused.json", token)

    def test_legacy_epoch_identity_adapter_is_deterministic_typed_and_read_only(self) -> None:
        self.prepare_serial_conflict_epoch()
        plan = runtime.build_epoch_plan(self.root, self.declaration)
        native_identity = plan["declaration"]["identity_sha256"]
        source = Path(plan["declaration"]["path"])
        legacy = json.loads(json.dumps(plan))
        legacy["declaration"].pop("identity_sha256")
        before = {"source": source.read_bytes(),
                  "status": run(self.root, "git", "status", "--porcelain=v1", "--untracked-files=all"),
                  "refs": run(self.root, "git", "for-each-ref", "--format=%(refname) %(objectname)"),
                  "objects": run(self.root, "git", "count-objects", "-v")}
        first = runtime._forecast_input_identity(legacy)
        second = runtime._forecast_input_identity(legacy)
        self.assertEqual(first, second)
        self.assertEqual(native_identity, first["declaration_identity_sha256"])
        self.assertEqual("juno_release_epoch_legacy_declaration_identity.v1",
                         first["legacy_declaration_identity"]["schema_version"])
        self.assertEqual(legacy["declaration"]["sha256"],
                         first["legacy_declaration_identity"]["source_sha256"])

        variants = {
            "declaration_reference_ambiguous": {**legacy,
                "declaration": {"path": str(source), "revision": 1}},
            "declaration_identity_ambiguous": {**legacy,
                "declaration": {**legacy["declaration"], "identity_sha256": "ambiguous"}},
            "declaration_bytes_missing": {**legacy,
                "declaration": {**legacy["declaration"], "path": str(self.root / "missing.json")}},
        }
        for code, candidate in variants.items():
            with self.subTest(code=code), self.assertRaisesRegex(
                    runtime.ReleaseTrainError, "legacy_epoch_evidence_incompatible:" + code):
                runtime._forecast_input_identity(candidate)
        tampered = self.root / "legacy-tampered.json"
        tampered.write_bytes(source.read_bytes() + b"\\n")
        candidate = {**legacy, "declaration": {**legacy["declaration"], "path": str(tampered)}}
        with self.assertRaisesRegex(runtime.ReleaseTrainError,
                                    "legacy_epoch_evidence_incompatible:declaration_bytes_tampered"):
            runtime._forecast_input_identity(candidate)
        tampered.unlink()
        self.assertEqual(before["source"], source.read_bytes())
        self.assertEqual(before["status"], run(
            self.root, "git", "status", "--porcelain=v1", "--untracked-files=all"))
        self.assertEqual(before["refs"], run(
            self.root, "git", "for-each-ref", "--format=%(refname) %(objectname)"))
        self.assertEqual(before["objects"], run(self.root, "git", "count-objects", "-v"))

    def test_conflict_authority_refuses_missing_ambiguous_sensitive_scope_and_identity(self) -> None:
        self.prepare_serial_conflict_epoch()
        plan = runtime.build_epoch_plan(self.root, self.declaration)
        envelope = plan["conflict_manifest"]["conservative_envelope"]
        authority_path = Path(plan["conflict_authority"]["path"])
        original = json.loads(authority_path.read_text())
        missing_plan = {**plan, "conflict_authority": None}
        self.assertEqual(["authority.missing"], runtime._conflict_authority(missing_plan, envelope)[1])
        variants = {
            "authority.logical_sets": {**original, "logical_sets": original["logical_sets"] * 2},
            "authority.risk": {**original, "risk": {**original["risk"], "sensitive": True}},
            "authority.scope": {**original, "logical_sets": [{**original["logical_sets"][0],
                "permitted_paths": []}]},
            "authority.input_identity": {**original, "input_identity_sha256": "0" * 64},
        }
        for expected, value in variants.items():
            with self.subTest(expected=expected):
                authority_path.write_text(json.dumps(value, sort_keys=True) + "\n")
                candidate = {**plan, "conflict_authority": {"path": str(authority_path),
                    "sha256": hashlib.sha256(authority_path.read_bytes()).hexdigest()}}
                binding, reasons = runtime._conflict_authority(candidate, envelope)
                self.assertIsNone(binding)
                self.assertIn(expected, reasons)
        authority_path.write_text(json.dumps(original, sort_keys=True) + "\n")

    def test_phase1_acceptance_cli_replays_semantics_and_correlates_watched_producer(self) -> None:
        """The excluded driver performs only one positive typed orchestration."""
        self.prepare_serial_conflict_epoch(preserve_runtime=True)
        plan = runtime.build_epoch_plan(self.root, self.declaration)
        manifest = plan["conflict_manifest"]
        proven = next(row for row in manifest["compositions"]
                      if isinstance(row.get("proven_composition"), dict))
        proof = proven["proven_composition"]
        receipt_path = Path(proof["receipt_path"])
        receipt = json.loads(receipt_path.read_text())
        fixture = {"schema_version": "juno_release_epoch_portable_topology.v2",
            "base_sha": plan["base_sha"], "order": plan["order"],
            "members": [{"task_id": row["task_id"], "tip_sha": row["tip_sha"],
                         "tree_sha": row["tree_sha"]} for row in plan["members"]],
            "serial_conflicts": [row["task_id"] for row in manifest["conflicts"]],
            "proven_compositions": [{"task_id": proven["task_id"],
                "commit": proof["commit"], "tree": proof["tree"],
                "receipt": receipt,
                "receipt_bytes_b64": base64.b64encode(receipt_path.read_bytes()).decode(),
                "receipt_sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest()}]}
        fixture_path = self.root / ".juno_task/runtime/phase-evidence/V9vE0X/orchestration.json"
        fixture_path.parent.mkdir(parents=True, exist_ok=True)
        fixture_path.write_text(runtime.canonical(fixture) + "\n")
        executable = str(Path(sys.executable).resolve())
        script_path = self.root / ".juno_task/scripts/release_train.py"
        orchestrate = [executable, str(script_path), "--controller", str(self.root),
            "phase1-orchestrate", "--declaration", str(self.declaration),
            "--fixture", str(fixture_path), "--task-id", "V9vE0X",
            "--worktree", str(self.root)]
        completed = subprocess.run(orchestrate, cwd=self.root,
            env={**os.environ, "JUNO_TASK_ROOT": str(self.root),
                 "JUNO_WORKSPACE_ROLE": "controller", "JUNO_WORKSPACE_ENFORCEMENT": "strict"},
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=480)
        self.assertEqual(0, completed.returncode, completed.stderr + completed.stdout)
        projection = json.loads(completed.stdout.splitlines()[-1])
        self.assertEqual("PASS", projection["decision"])
        self.assertEqual({"proof", "suite", "evaluation"},
                         set(projection["stage_watch_run_ids"]))
        receipt = json.loads(Path(projection["receipt"]["path"]).read_text())
        self.assertEqual(list(runtime.PHASE1_SUITE_TESTS),
                         receipt["checked_closure"]["suite_manifest"]["tests"])

    def test_phase1_substitution_matrix_is_complete_and_receipt_bound(self) -> None:
        """Every admitted substitution reaches the production refusal path."""
        test_path = Path(__file__).resolve()
        self.assertEqual(
            sorted([*runtime.PHASE1_SUITE_TESTS, runtime.PHASE1_ORCHESTRATION_TEST]),
            runtime._phase1_discovered_tests(test_path))
        self.assertIn(
            "ReleaseTrainTests.test_phase1_substitution_matrix_is_complete_and_receipt_bound",
            runtime.PHASE1_SUITE_MANIFEST["tests"])
        self.assertEqual([runtime.PHASE1_ORCHESTRATION_TEST],
                         runtime.PHASE1_SUITE_MANIFEST["excluded_recursive_tests"])

        # Build one valid, non-recursive proof/suite/evaluation closure.  The suite
        # receipt is verifier-shaped rather than suite-executed so this test can be
        # a member of that same suite without recursively invoking itself.
        self.prepare_serial_conflict_epoch(preserve_runtime=True)
        plan = runtime.build_epoch_plan(self.root, self.declaration)
        manifest = plan["conflict_manifest"]
        proven = next(row for row in manifest["compositions"]
                      if isinstance(row.get("proven_composition"), dict))
        proof = proven["proven_composition"]
        receipt_path = Path(proof["receipt_path"])
        receipt = json.loads(receipt_path.read_text())
        fixture = {"schema_version": "juno_release_epoch_portable_topology.v2",
            "base_sha": plan["base_sha"], "order": plan["order"],
            "members": [{"task_id": row["task_id"], "tip_sha": row["tip_sha"],
                         "tree_sha": row["tree_sha"]} for row in plan["members"]],
            "serial_conflicts": [row["task_id"] for row in manifest["conflicts"]],
            "proven_compositions": [{"task_id": proven["task_id"],
                "commit": proof["commit"], "tree": proof["tree"],
                "receipt": receipt,
                "receipt_bytes_b64": base64.b64encode(receipt_path.read_bytes()).decode(),
                "receipt_sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest()}]}
        evidence_root = self.root / ".juno_task/runtime/phase-evidence/V9vE0X"
        evidence_root.mkdir(parents=True, exist_ok=True)
        fixture_path = evidence_root / "matrix-portable.json"
        fixture_path.write_text(runtime.canonical(fixture) + "\n")
        tip_sha = run(self.root, "git", "rev-parse", "HEAD")
        tree_sha = run(self.root, "git", "rev-parse", "HEAD^{tree}")
        executable = str(Path(sys.executable).resolve())
        script_path = self.root / ".juno_task/scripts/release_train.py"
        original_runtime_file = runtime.__file__
        runtime.__file__ = str(script_path)
        self.addCleanup(setattr, runtime, "__file__", original_runtime_file)
        declaration_ref, authority_ref = runtime._phase1_publish_declaration(
            self.root, "V9vE0X", self.declaration)
        fixture_ref = runtime._phase1_publish_input(
            self.root, "V9vE0X", "fixture", fixture_path)
        snapshot = runtime._phase1_repository_snapshot(self.root)
        input_identity = runtime.phase1_input_identity(
            "V9vE0X", self.root, tip_sha, tree_sha, declaration_ref, fixture_ref,
            executable, script_path, snapshot)
        proof_path = evidence_root / f'phase1-proof-{tip_sha}-{input_identity["input_sha256"]}.json'
        proof_argv = [executable, str(script_path), "--controller", str(self.root),
            "phase1-prove", "--declaration", str(self.declaration),
            "--fixture", str(fixture_path), "--task-id", "V9vE0X",
            "--worktree", str(self.root), "--output", str(proof_path)]
        proof = runtime.phase1_proof(self.root, self.declaration, fixture_path,
                                     "V9vE0X", self.root, proof_path, proof_argv)
        self.assertEqual("PASS", proof["decision"], proof)

        def watch(name: str, argv: list[str], output_identity: str) -> tuple[dict[str, str], Path]:
            root = self.root / ".juno_task/runtime/watch-runs" / f"matrix-{name}"
            root.mkdir(parents=True, exist_ok=True)
            footer = ("schema_version=juno.watch-footer.v1\nexit_code=0\n"
                      "completed_utc=2026-08-28T00:00:00Z\n")
            (root / "footer").write_text(footer)
            (root / "combined.log").write_text(output_identity + "\n")
            (root / "run.json").write_text(json.dumps({"run_id": f"matrix-{name}",
                "state": "COMPLETED", "exit_code": 0, "cwd": str(self.root),
                "argv_sha256": hashlib.sha256(json.dumps(
                    argv, separators=(",", ":")).encode()).hexdigest()}) + "\n")
            return ({"run_id": f"matrix-{name}",
                     "footer_sha256": hashlib.sha256(footer.encode()).hexdigest()}, root)

        def ref(path: Path) -> dict[str, str]:
            return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

        proof_watch, proof_watch_root = watch("proof", proof_argv, proof["proof_sha256"])
        evidence_context = runtime.digest(proof_watch)
        test_blob = runtime._git_blob_hash(
            self.root, tip_sha, runtime.PHASE1_COMMITTED_PATHS[1])
        suite_manifest = {**runtime.PHASE1_SUITE_MANIFEST, "test_blob_sha256": test_blob}
        suite_identity = runtime.digest({"tip_sha": tip_sha, "tree_sha": tree_sha,
            "manifest_sha256": runtime.digest(suite_manifest),
            "evidence_context": evidence_context})
        suite_path = evidence_root / f"phase1-suite-{suite_identity}.json"
        suite_argv = runtime._phase1_suite_argv(
            self.root, self.root, suite_path, evidence_context)
        environment = runtime._phase1_suite_environment(self.root, suite_path)
        committed_test_path = self.root / runtime.PHASE1_COMMITTED_PATHS[1]
        suite_body = {"schema_version": runtime.PHASE1_SUITE_SCHEMA, "decision": "PASS",
            "task_id": "V9vE0X", "tip_sha": tip_sha, "tree_sha": tree_sha,
            "suite_identity_sha256": suite_identity, "evidence_context": evidence_context,
            "producer_argv": suite_argv,
            "suite_command": [executable, str(committed_test_path), *runtime.PHASE1_SUITE_TESTS],
            "suite_cwd": str(committed_test_path.parent), "test_path": str(committed_test_path),
            "test_blob_sha256": test_blob, "suite_manifest": suite_manifest,
            "suite_manifest_sha256": runtime.digest(suite_manifest),
            "discovered_tests": runtime._phase1_discovered_tests(committed_test_path),
            "runtime": runtime._phase1_runtime_identity(executable, script_path),
            "observed_environment": environment,
            "environment_sha256": runtime.digest(environment),
            "result": {"outcome": "PASS", "test_count": len(runtime.PHASE1_SUITE_TESTS),
                       "output_sha256": "a" * 64, "exit_code": 0},
            "blocking_reason_codes": []}
        suite_receipt = {**suite_body, "receipt_sha256": runtime.digest(suite_body)}
        suite_path.write_text(runtime.canonical(suite_receipt) + "\n")
        suite_watch, suite_watch_root = watch("suite", suite_argv, suite_receipt["receipt_sha256"])
        closure = {"schema_version": runtime.PHASE1_CLOSURE_SCHEMA,
                   "proof": ref(proof_path), "watch": proof_watch,
                   "suite": {"receipt": ref(suite_path), "watch": suite_watch}}
        closure_path = evidence_root / "matrix-closure.json"
        closure_path.write_text(runtime.canonical(closure) + "\n")

        def evaluate() -> dict[str, object]:
            current_proof = json.loads(proof_path.read_text())
            current_suite = json.loads(suite_path.read_text())
            current_closure = json.loads(closure_path.read_text())
            checked = runtime._phase1_checked_closure(current_proof, current_closure, current_suite)
            output = evidence_root / f"phase1-evaluation-{runtime.digest(checked)}.json"
            argv = [executable, str(script_path), "--controller", str(self.root),
                    "phase1-accept", "--closure", str(closure_path), "--output", str(output)]
            return runtime.phase1_acceptance_receipt(self.root, closure_path, output, argv)

        evaluation = evaluate()
        self.assertEqual("PASS", evaluation["decision"], evaluation)
        evaluation_path = evidence_root / f'phase1-evaluation-{evaluation["closure_sha256"]}.json'
        evaluation_argv = evaluation["producer_argv"]
        evaluation_watch, evaluation_watch_root = watch(
            "evaluation", evaluation_argv, evaluation["receipt_sha256"])
        final_path = evidence_root / f'phase1-acceptance-{evaluation["closure_sha256"]}.json'
        self.assertEqual("PASS", runtime.phase1_finalize_acceptance(
            self.root, ref(evaluation_path), evaluation_watch, final_path)["decision"])

        original_suite = suite_path.read_bytes()
        suite_substitutions = {
            "help": {**suite_receipt, "producer_argv": [executable, str(script_path), "--help"]},
            "arbitrary_success": {**suite_receipt, "producer_argv": ["/usr/bin/true"]},
            "forged_result": {**suite_receipt, "result": {
                **suite_receipt["result"], "outcome": "FAIL"}},
            "result_count": {**suite_receipt, "result": {
                **suite_receipt["result"], "test_count": len(runtime.PHASE1_SUITE_TESTS) - 1}},
            "argv": {**suite_receipt, "producer_argv": ["substituted"]},
            "cwd": {**suite_receipt, "suite_cwd": str(self.root / "substituted")},
            "runtime": {**suite_receipt, "runtime": {
                **suite_receipt["runtime"], "executable_sha256": "0" * 64}},
            "environment": {**suite_receipt, "observed_environment": {}},
            "manifest_membership": {**suite_receipt, "suite_manifest": {
                **suite_manifest, "tests": suite_manifest["tests"][:-1]}},
        }
        exercised = set()
        for name, candidate in suite_substitutions.items():
            with self.subTest(substitution=name):
                if "observed_environment" in candidate:
                    candidate["environment_sha256"] = runtime.digest(
                        candidate["observed_environment"])
                body = {key: value for key, value in candidate.items()
                        if key != "receipt_sha256"}
                candidate["receipt_sha256"] = runtime.digest(body)
                suite_path.write_text(runtime.canonical(candidate) + "\n")
                closure["suite"]["receipt"] = ref(suite_path)
                closure_path.write_text(runtime.canonical(closure) + "\n")
                refused = evaluate()
                self.assertEqual("FAIL", refused["decision"])
                self.assertIn("suite.closure", refused["blocking_reason_codes"])
                exercised.add(name)
                suite_path.write_bytes(original_suite)
                closure["suite"]["receipt"] = ref(suite_path)
                closure_path.write_text(runtime.canonical(closure) + "\n")

        original_proof = proof_path.read_bytes()
        external_authority = evidence_root / "external-authority.json"
        external_authority.write_bytes(Path(authority_ref["path"]).read_bytes())
        substituted_declaration = json.loads(Path(declaration_ref["path"]).read_text())
        substituted_declaration["conflict_authority"] = {
            "path": str(external_authority), "sha256": authority_ref["sha256"]}
        substituted_declaration_path = evidence_root / "substituted-declaration.json"
        substituted_declaration_path.write_text(runtime.canonical(substituted_declaration) + "\n")
        substituted_proof = json.loads(original_proof)
        substituted_proof["declaration"] = ref(substituted_declaration_path)
        substituted_body = {key: value for key, value in substituted_proof.items()
                            if key != "proof_sha256"}
        substituted_proof["proof_sha256"] = runtime.digest(substituted_body)
        proof_path.write_text(runtime.canonical(substituted_proof) + "\n")
        closure["proof"] = ref(proof_path)
        closure_path.write_text(runtime.canonical(closure) + "\n")
        refused = evaluate()
        self.assertEqual("FAIL", refused["decision"])
        self.assertIn("authority.path_substitution", refused["blocking_reason_codes"])
        exercised.add("authority_path")
        proof_path.write_bytes(original_proof)
        closure["proof"] = ref(proof_path)
        closure_path.write_text(runtime.canonical(closure) + "\n")

        original_runs = {"proof": proof_watch_root, "suite": suite_watch_root,
                         "evaluation": evaluation_watch_root}
        expected_role_fields = {(role, field) for role in original_runs
                                for field in ("argv", "cwd", "footer")}
        role_exercised = set()
        for role, root in original_runs.items():
            for field in ("argv", "cwd", "footer"):
                with self.subTest(role=role, field=field):
                    run_path = root / "run.json"; footer_path = root / "footer"
                    run_bytes = run_path.read_bytes(); footer_bytes = footer_path.read_bytes()
                    if field == "footer":
                        footer_path.write_text("schema_version=juno.watch-footer.v1\nexit_code=1\n")
                    else:
                        record = json.loads(run_bytes)
                        record["argv_sha256" if field == "argv" else "cwd"] = (
                            "0" * 64 if field == "argv" else str(self.root / "substituted"))
                        run_path.write_text(json.dumps(record) + "\n")
                    try:
                        if role == "evaluation":
                            refused = runtime.phase1_finalize_acceptance(
                                self.root, ref(evaluation_path), evaluation_watch, final_path)
                        else:
                            refused = evaluate()
                        self.assertEqual("FAIL", refused["decision"])
                        self.assertIn(f"{role}.watch.identity", refused["blocking_reason_codes"])
                    finally:
                        run_path.write_bytes(run_bytes); footer_path.write_bytes(footer_bytes)
                    role_exercised.add((role, field))
        self.assertEqual(expected_role_fields, role_exercised)
        exercised.add("footer")
        self.assertEqual({"help", "arbitrary_success", "forged_result", "result_count",
            "argv", "cwd", "runtime", "footer", "environment", "authority_path",
            "manifest_membership"}, exercised)

    def test_exact_rc7_rc8_receipts_cover_every_member_without_synthetic_repair(self) -> None:
        configured = os.environ.get("YYLO_RC_EVIDENCE_CONTROLLER")
        if not configured:
            self.skipTest("set YYLO_RC_EVIDENCE_CONTROLLER for the immutable rc7/rc8 canary")
        controller = Path(configured).expanduser().resolve()
        expected = {
            "sot-ledger-wave1-rc7": {
                "repair": "35e932529096d421d4c084fb63b1a4929fed2868de91a33fd8ddda53b17587cf",
                "conflict": "23b6ac9383c494de1a0ccf02457eb055d282e2df7abda31f88e65af164190a6b",
                "worker": "8c33108b5750d690a422fe447234fecd6000bf738f6ca7f72f4855e3df9d8ee0"},
            "sot-ledger-wave1-rc8": {
                "repair": "5d81ec668d92c31a7fe79d13be5bfb84c4d7c950f01be50305ca49c98aee48c4",
                "conflict": "71c4e636cd10697e72dde8ecfed5abb99dec07233be36cee039199ca0bdb2204",
                "worker": "45b02566b7e6ef08595785819e85fe23504b1f74bba0421fac6fd7ce6706f2e5"}}
        states = {}
        for epoch_id, identities in expected.items():
            state_path = controller / ".juno_task/runtime/release-epochs" / epoch_id / "state.json"
            state = json.loads(state_path.read_text())
            states[epoch_id] = state
            receipts = {row["transition"]: row for row in state["receipts"]}
            self.assertEqual(identities["repair"], receipts["REPAIR_CONSUMED"]["sha256"])
            self.assertEqual(identities["conflict"], state["receipts"][-1]["sha256"])
            worker = Path(state["conflict_repair"]["path"])
            self.assertEqual(identities["worker"], hashlib.sha256(worker.read_bytes()).hexdigest())
        rc8 = states["sot-ledger-wave1-rc8"]
        before_status = run(controller, "git", "status", "--porcelain=v1", "--untracked-files=all")
        before_refs = run(controller, "git", "for-each-ref", "--format=%(refname) %(objectname)")
        manifest = runtime.forecast_epoch_conflicts(controller, controller, rc8["seal"])
        self.assertEqual(rc8["seal"]["order"], [row["task_id"] for row in manifest["compositions"]])
        self.assertEqual(["pA6M9l"], [row["task_id"] for row in manifest["conflicts"]])
        self.assertEqual(["0y4ljs", "U2rjMN", "znI3LO", "e99k0C", "GsKDx6"],
                         [row["task_id"] for row in manifest["indeterminate_members"]])
        self.assertTrue(manifest["member_accounting_complete"])
        self.assertTrue(manifest["forecast_complete"])
        self.assertFalse(manifest["exact_composition_complete"])
        self.assertFalse(manifest["repair_budget_feasible"])
        self.assertEqual("NEEDS_OPERATOR", manifest["operator_state"])
        self.assertEqual(["authority.missing"], manifest["authority_reason_codes"])
        self.assertEqual("juno_release_epoch_legacy_declaration_identity.v1",
                         manifest["identity"]["legacy_declaration_identity"]["schema_version"])
        self.assertEqual(1, manifest["required_logical_repair_set_count"])
        self.assertEqual(expected["sot-ledger-wave1-rc7"]["repair"],
                         manifest["conflicts"][0]["proven_composition"]["receipt_sha256"])
        self.assertEqual(before_status, run(controller, "git", "status", "--porcelain=v1",
                                            "--untracked-files=all"))
        self.assertEqual(before_refs, run(controller, "git", "for-each-ref",
                                          "--format=%(refname) %(objectname)"))

        # Receipt drift fails closed without touching the exhausted source epoch.
        with tempfile.TemporaryDirectory() as temporary:
            evidence = Path(temporary)
            copied_root = evidence / ".juno_task/runtime/release-epochs/sot-ledger-wave1-rc7"
            copied_receipt = copied_root / "receipts/0003-repair_consumed.json"
            copied_receipt.parent.mkdir(parents=True)
            source_receipt = Path(states["sot-ledger-wave1-rc7"]["receipts"][2]["path"])
            copied_receipt.write_bytes(source_receipt.read_bytes())
            copied_state = json.loads(json.dumps(states["sot-ledger-wave1-rc7"]))
            copied_state["receipts"] = [{**states["sot-ledger-wave1-rc7"]["receipts"][2],
                "path": str(copied_receipt)}]
            (copied_root / "state.json").write_text(json.dumps(copied_state, sort_keys=True) + "\n")
            row = rc8["seal"]["members"][0]
            self.assertIsNotNone(runtime._proven_forecast_composition(
                evidence, controller, "pA6M9l", rc8["seal"]["base_sha"], row["tip_sha"],
                "sot-ledger-wave1-rc8"))
            copied_receipt.write_text(copied_receipt.read_text() + "tamper\n")
            self.assertIsNone(runtime._proven_forecast_composition(
                evidence, controller, "pA6M9l", rc8["seal"]["base_sha"], row["tip_sha"],
                "sot-ledger-wave1-rc8"))

    def test_bootstrap_repair_is_causal_fenced_preserves_queue_and_cas_once(self) -> None:
        old, req = self.prepare_epoch()
        self.board["REQ"]["blocked_by"] = ["OLD"]
        self.board["DEP"]["blocked_by"] = ["REQ"]
        self.write_board()
        declaration = self.write_bootstrap_declaration()
        plan = runtime.build_bootstrap_plan(self.root, declaration)
        self.assertEqual(["OLD", "REQ"], [row["task_id"] for row in plan["members"]])
        self.assertEqual([], plan["preserved_members"])
        sealed = runtime.seal_bootstrap(self.root, declaration)
        token = sealed["bootstrap_token"]
        with self.assertRaisesRegex(runtime.ReleaseTrainError, "exact bootstrap-repair fencing token"):
            runtime.drive_bootstrap(self.root, "bootstrap-1", "wrong")
        cas_calls = 0
        def fixture_cas(repository: Path, target_ref: str, tip: str, expected: str) -> dict:
            nonlocal cas_calls
            cas_calls += 1
            run(repository, "git", "update-ref", target_ref, tip, expected)
            return {"fixture": "exact-owner-readback"}
        with mock.patch("merge_queue.cas_target", side_effect=fixture_cas), \
             mock.patch("merge_queue.refresh_managed_controller", return_value={"status": "complete"}), \
             mock.patch.object(runtime, "reconcile_bootstrap_members", side_effect=lambda _controller, state: state):
            complete = runtime.drive_bootstrap(self.root, "bootstrap-1", token)
            repeated = runtime.drive_bootstrap(self.root, "bootstrap-1", token)
        self.assertEqual("COMPLETE", complete["state"])
        self.assertEqual(complete, repeated)
        self.assertEqual(1, cas_calls)
        self.assertEqual(1, complete["cas"]["target_move_count"])
        for candidate in (old, req):
            subprocess.run(["git", "merge-base", "--is-ancestor", candidate,
                            complete["cas"]["tip"]], cwd=self.root, check=True)
        self.assertEqual("QUEUED", self.state["tasks"]["OLD"]["state"])
        self.assertEqual("QUEUED", self.state["tasks"]["REQ"]["state"])
        receipt = json.loads(Path(complete["receipt"]["path"]).read_text())
        self.assertEqual("bootstrap_repair_integrated", receipt["reason_code"])
        self.assertTrue(set(runtime.EXTERNAL_ACTIONS).issubset(receipt["excluded_actions"]))

    def test_bootstrap_reconciliation_finalizes_only_exact_ancestry_members(self) -> None:
        self.prepare_epoch()
        self.board["REQ"]["blocked_by"] = ["OLD"]
        self.board["DEP"]["blocked_by"] = ["REQ"]
        self.write_board()
        sealed = runtime.seal_bootstrap(self.root, self.write_bootstrap_declaration())
        state = sealed["state"]
        for member in state["seal"]["members"]:
            run(self.root, "git", "merge", "--no-ff", "--no-edit", member["tip_sha"])
        target = run(self.root, "git", "rev-parse", "HEAD")
        state.update({"state": "COMPLETE", "cas": {"readback": self.base},
                      "receipt": {"receipt_id": "r" * 64}})
        persisted = []
        with mock.patch("merge_queue.finalize_kanban_task", return_value={"outcome": "completed"}) as finalize, \
             mock.patch("merge_queue.persist_attempt", side_effect=lambda _c, attempt, **_k: persisted.append(attempt)):
            reconciled = runtime.reconcile_bootstrap_members(self.root, state)
        self.assertEqual(["OLD", "REQ"], [row["task_id"] for row in reconciled["reconciliation"]["members"]])
        self.assertEqual(self.base, reconciled["reconciliation"]["sealed_target_sha"])
        self.assertEqual(target, reconciled["reconciliation"]["observed_target_sha"])
        self.assertEqual(2, finalize.call_count)
        self.assertEqual(["OLD", "REQ"], [row["task_id"] for row in persisted])
        self.assertTrue(all(row["candidate_sha"] == target for row in persisted))

    def test_bootstrap_repair_refuses_missing_causal_dependency(self) -> None:
        self.prepare_epoch()
        self.board["DEP"]["blocked_by"] = ["REQ"]
        self.write_board()
        with self.assertRaisesRegex(runtime.ReleaseTrainError, "causal chain"):
            runtime.build_bootstrap_plan(self.root, self.write_bootstrap_declaration())

    def test_epoch_composes_history_validates_once_and_cas_once(self) -> None:
        old, req = self.prepare_epoch()
        self.lean_checkout()
        sealed = runtime.seal_epoch(self.root, self.declaration)
        def fixture_cas(repository: Path, target_ref: str, tip: str, expected: str) -> dict:
            run(repository, "git", "update-ref", target_ref, tip, expected)
            return {"fixture": "exact-owner-readback"}
        with mock.patch("merge_queue.cas_target", side_effect=fixture_cas):
            state = runtime.drive_epoch(self.root, "rc-1", sealed["lease_token"])
        self.assertEqual("RELEASE_READY", state["state"])
        self.assertEqual(2, len(state["composition"]["commits"]))
        self.assertEqual(1, state["aggregate"]["aggregate_runs"])
        self.assertEqual(1, state["cas"]["target_move_count"])
        train_checkout = Path(state["composition"]["worktree"])
        self.assertTrue((train_checkout / "juno-code/package.json").is_file())
        self.assertNotEqual("true", run(train_checkout, "git", "config", "--bool", "core.sparseCheckout"))
        self.assertEqual(state["composition"]["tip_sha"], run(self.root, "git", "rev-parse", "refs/heads/product"))
        for tip in (old, req):
            subprocess.run(["git", "merge-base", "--is-ancestor", tip,
                            state["composition"]["tip_sha"]], cwd=self.root, check=True)
        finalization = (self.root / ".juno_task/runtime/release-epoch-finalization/rc-1/journal.json")
        self.assertTrue(finalization.is_file())
        self.assertTrue(all(self.board_path.is_file() for _ in [0]))
        board = json.loads(self.board_path.read_text())
        self.assertTrue(all(board[task_id]["status"] == "done" for task_id in ("OLD", "REQ")))
        # Retry is observation-only and cannot duplicate commits, validation, CAS, or Ledger writes.
        self.assertEqual(state, runtime.drive_epoch(self.root, "rc-1", sealed["lease_token"]))

    def test_three_member_terminal_projection_resumes_after_interruption_and_is_idempotent(self) -> None:
        tips = self.prepare_three_member_epoch()
        sealed = runtime.seal_epoch(self.root, self.declaration)
        cas_calls = 0
        def fixture_cas(repository: Path, target_ref: str, tip: str, expected: str) -> dict:
            nonlocal cas_calls
            cas_calls += 1; run(repository, "git", "update-ref", target_ref, tip, expected)
            return {"fixture": "exact-owner-readback"}
        import merge_queue
        actual_finalize = merge_queue.finalize_kanban_task
        calls = 0
        def interrupted(controller: Path, attempt: dict) -> dict:
            nonlocal calls
            calls += 1
            if calls == 2: raise runtime.ReleaseTrainError("seeded interruption")
            return actual_finalize(controller, attempt)
        with mock.patch("merge_queue.cas_target", side_effect=fixture_cas), \
             mock.patch("merge_queue.finalize_kanban_task", side_effect=interrupted):
            with self.assertRaisesRegex(runtime.ReleaseTrainError, "seeded interruption"):
                runtime.drive_epoch(self.root, "rc-1", sealed["lease_token"])
        self.assertEqual("INTEGRATED", runtime.read_epoch(self.root, "rc-1")["state"])
        with mock.patch("merge_queue.cas_target", side_effect=fixture_cas):
            complete = runtime.drive_epoch(self.root, "rc-1", sealed["lease_token"])
        self.assertEqual("RELEASE_READY", complete["state"]); self.assertEqual(1, cas_calls)
        board = json.loads(self.board_path.read_text())
        for task_id, tip in zip(("OLD", "REQ", "DEP"), tips):
            self.assertEqual("done", board[task_id]["status"])
            self.assertEqual(tip, board[task_id]["commit_hash"])
            self.assertEqual("MERGED", board[task_id]["fields"]["lifecycle_state"])
        repeated = runtime.reconcile_epoch_members(self.root, "rc-1", complete["cas"]["readback"])
        self.assertIsNotNone(repeated["completed_receipt"])

    def prepare_untouched_stale_finalization(self) -> tuple[str, str, list[str]]:
        tips = self.prepare_three_member_epoch()
        sealed = runtime.seal_epoch(self.root, self.declaration)
        def fixture_cas(repository: Path, target_ref: str, tip: str, expected: str) -> dict:
            run(repository, "git", "update-ref", target_ref, tip, expected)
            return {"fixture": "exact-owner-readback"}
        with mock.patch("merge_queue.cas_target", side_effect=fixture_cas), \
             mock.patch.object(runtime, "reconcile_epoch_members", return_value={}):
            complete = runtime.drive_epoch(self.root, "rc-1", sealed["lease_token"])
        predecessor_target = complete["cas"]["readback"]
        for task_id, tip in zip(("OLD", "REQ", "DEP"), tips):
            record = self.state["tasks"][task_id]
            record.update({"state": "MERGED", "queue_attempt": {
                "task_id": task_id, "feature_sha": tip, "outcome": "ALREADY_IN_TARGET"}})
        self.write_state()
        with mock.patch("merge_queue.finalize_kanban_task",
                        side_effect=runtime.ReleaseTrainError("seeded untouched interruption")):
            with self.assertRaisesRegex(runtime.ReleaseTrainError, "seeded untouched interruption"):
                runtime.reconcile_epoch_members(self.root, "rc-1", predecessor_target)
        run(self.root, "git", "commit", "--allow-empty", "-m", "descendant target")
        current_target = run(self.root, "git", "rev-parse", "HEAD")
        return predecessor_target, current_target, tips

    def test_successor_replay_projects_untouched_stale_journal_and_is_idempotent(self) -> None:
        predecessor, current, tips = self.prepare_untouched_stale_finalization()
        self.assertEqual(["rc-1"], runtime._unresolved_finalization_epochs(self.root))
        self.assertEqual("FINALIZATION_INCOMPLETE",
                         runtime.epoch_status_projection(self.root, "rc-1")["state"])
        req_projection_before = runtime.digest(runtime.kanban_task(self.root, "REQ"))
        req_identity_before = runtime.task_runtime.kanban_board_identity(self.root, "REQ")
        import merge_queue
        actual_finalize = merge_queue.finalize_kanban_task
        calls = 0
        def interrupt_after_first(controller: Path, attempt: dict) -> dict:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise runtime.ReleaseTrainError("seeded successor interruption")
            return actual_finalize(controller, attempt)
        with mock.patch("merge_queue.finalize_kanban_task", side_effect=interrupt_after_first):
            with self.assertRaisesRegex(runtime.ReleaseTrainError, "seeded successor interruption"):
                runtime.replay_epoch_finalization_successor(
                    self.root, "rc-1", predecessor, current)
        self.assertNotEqual(req_projection_before,
                            runtime.digest(runtime.kanban_task(self.root, "REQ")))
        self.assertEqual(req_identity_before,
                         runtime.task_runtime.kanban_board_identity(self.root, "REQ"))
        result = runtime.replay_epoch_finalization_successor(
            self.root, "rc-1", predecessor, current)
        self.assertEqual("SUCCESSOR_COMPLETE", json.loads(
            Path(result["completed_receipt"]["path"]).read_text())["transition"])
        superseded = json.loads(Path(
            result["supersession_receipts"]["predecessor_superseded"]["path"]).read_text())
        self.assertEqual("PREDECESSOR_SUPERSEDED", superseded["transition"])
        board = json.loads(self.board_path.read_text())
        for task_id, tip in zip(("OLD", "REQ", "DEP"), tips):
            self.assertEqual(("done", tip, "MERGED"),
                (board[task_id]["status"], board[task_id]["commit_hash"],
                 board[task_id]["fields"]["lifecycle_state"]))
        self.assertEqual(result, runtime.replay_epoch_finalization_successor(
            self.root, "rc-1", predecessor, current))
        self.assertEqual([], runtime._unresolved_finalization_epochs(self.root))
        self.assertEqual("RELEASE_READY", runtime.epoch_status_projection(self.root, "rc-1")["state"])
        predecessor_journal = self.root / ".juno_task/runtime/release-epoch-finalization/rc-1/journal.json"
        self.assertTrue(all(row["status"] == "pending"
                            for row in json.loads(predecessor_journal.read_text())["members"]))

    def test_successor_replay_chains_across_two_more_target_advances_without_reapplying(self) -> None:
        predecessor, first_target, tips = self.prepare_untouched_stale_finalization()
        import merge_queue
        actual_finalize = merge_queue.finalize_kanban_task
        calls: list[str] = []

        def stop_after_first(controller: Path, attempt: dict) -> dict:
            calls.append(attempt["task_id"])
            if len(calls) == 2:
                raise runtime.ReleaseTrainError("first descendant interruption")
            return actual_finalize(controller, attempt)

        with mock.patch("merge_queue.finalize_kanban_task", side_effect=stop_after_first):
            with self.assertRaisesRegex(runtime.ReleaseTrainError, "first descendant interruption"):
                runtime.replay_epoch_finalization_successor(
                    self.root, "rc-1", predecessor, first_target)
        self.assertEqual(["OLD", "REQ"], calls)
        first_journal = next((self.root / ".juno_task/runtime/release-epoch-finalization/rc-1/successors").glob("*/journal.json"))
        first_partial = json.loads(first_journal.read_text())
        old_receipt = first_partial["members"][0]["receipt"]

        run(self.root, "git", "commit", "--allow-empty", "-m", "second descendant target")
        second_target = run(self.root, "git", "rev-parse", "HEAD")
        calls.clear()

        def stop_after_second(controller: Path, attempt: dict) -> dict:
            calls.append(attempt["task_id"])
            if len(calls) == 2:
                raise runtime.ReleaseTrainError("second descendant interruption")
            return actual_finalize(controller, attempt)

        with mock.patch("merge_queue.finalize_kanban_task", side_effect=stop_after_second):
            with self.assertRaisesRegex(runtime.ReleaseTrainError, "second descendant interruption"):
                runtime.replay_epoch_finalization_successor(
                    self.root, "rc-1", first_target, second_target)
        self.assertEqual(["REQ", "DEP"], calls)
        second_journal = next((first_journal.parent / "successors").glob("*/journal.json"))
        second_partial = json.loads(second_journal.read_text())
        self.assertEqual(old_receipt, second_partial["members"][0]["receipt"])
        self.assertEqual(old_receipt, second_partial["members"][0]["carried_receipt"])

        run(self.root, "git", "commit", "--allow-empty", "-m", "third descendant target")
        third_target = run(self.root, "git", "rev-parse", "HEAD")
        calls.clear()
        with mock.patch("merge_queue.finalize_kanban_task", side_effect=lambda c, a: (
                calls.append(a["task_id"]) or actual_finalize(c, a))):
            result = runtime.replay_epoch_finalization_successor(
                self.root, "rc-1", second_target, third_target)
        self.assertEqual(["DEP"], calls)
        self.assertEqual(["complete", "complete", "complete"],
                         [row["status"] for row in result["members"]])
        self.assertEqual(result, runtime.replay_epoch_finalization_successor(
            self.root, "rc-1", second_target, third_target))
        board = json.loads(self.board_path.read_text())
        for task_id, tip in zip(("OLD", "REQ", "DEP"), tips):
            self.assertEqual(("done", tip, "MERGED"),
                (board[task_id]["status"], board[task_id]["commit_hash"],
                 board[task_id]["fields"]["lifecycle_state"]))
        self.assertEqual([], runtime._unresolved_finalization_epochs(self.root))

    def test_successor_replay_refuses_partial_tamper_divergence_and_revision_drift(self) -> None:
        predecessor, current, _tips = self.prepare_untouched_stale_finalization()
        predecessor_root = self.root / ".juno_task/runtime/release-epoch-finalization/rc-1"
        members = predecessor_root / "members"; members.mkdir()
        (members / "0001-OLD.json").write_text("{}\n")
        before = self.board_path.read_bytes()
        with self.assertRaisesRegex(runtime.ReleaseTrainError, "partial mutation without receipt"):
            runtime.replay_epoch_finalization_successor(self.root, "rc-1", predecessor, current)
        self.assertEqual(before, self.board_path.read_bytes())
        (members / "0001-OLD.json").unlink()
        journal = predecessor_root / "journal.json"; original = journal.read_bytes()
        journal.write_bytes(original + b" ")
        with self.assertRaisesRegex(runtime.ReleaseTrainError, "identity drifted|unreadable"):
            runtime.replay_epoch_finalization_successor(self.root, "rc-1", predecessor, current)
        journal.write_bytes(original)
        unrelated = subprocess.check_output(["git", "commit-tree", f"{self.base}^{{tree}}", "-p", self.base],
            cwd=self.root, text=True, input="divergent\n").strip()
        run(self.root, "git", "update-ref", "refs/heads/product", unrelated, current)
        with self.assertRaisesRegex(runtime.ReleaseTrainError, "protected target moved|not a descendant|not in current target ancestry"):
            runtime.replay_epoch_finalization_successor(self.root, "rc-1", predecessor, unrelated)
        run(self.root, "git", "update-ref", "refs/heads/product", current, unrelated)
        self.board["OLD"]["last_modified"] = "revision-drift"; self.write_board()
        with self.assertRaisesRegex(runtime.ReleaseTrainError, "pending member own identity drifted"):
            runtime.replay_epoch_finalization_successor(self.root, "rc-1", predecessor, current)
        self.assertNotEqual(before, self.board_path.read_bytes())

    def test_four_member_terminal_projection_preserves_exact_fifo_and_replays_idempotently(self) -> None:
        tips = self.prepare_four_member_epoch()
        sealed = runtime.seal_epoch(self.root, self.declaration)
        def fixture_cas(repository: Path, target_ref: str, tip: str, expected: str) -> dict:
            run(repository, "git", "update-ref", target_ref, tip, expected)
            return {"fixture": "exact-owner-readback"}
        with mock.patch("merge_queue.cas_target", side_effect=fixture_cas):
            complete = runtime.drive_epoch(self.root, "rc-1", sealed["lease_token"])
        self.assertEqual(["OLD", "REQ", "DEP", "FOUR"], complete["seal"]["order"])
        board = json.loads(self.board_path.read_text())
        for task_id, tip in zip(("OLD", "REQ", "DEP", "FOUR"), tips):
            self.assertEqual(("done", tip, "MERGED"),
                (board[task_id]["status"], board[task_id]["commit_hash"],
                 board[task_id]["fields"]["lifecycle_state"]))
        repeated = runtime.reconcile_epoch_members(self.root, "rc-1", complete["cas"]["readback"])
        self.assertEqual(complete["release_ready"]["required_members"],
                         ["OLD", "REQ", "DEP", "FOUR"])
        self.assertIsNotNone(repeated["completed_receipt"])

    def test_terminal_projection_tamper_and_target_drift_refuse_without_ledger_mutation(self) -> None:
        self.prepare_three_member_epoch(); sealed = runtime.seal_epoch(self.root, self.declaration)
        def fixture_cas(repository: Path, target_ref: str, tip: str, expected: str) -> dict:
            run(repository, "git", "update-ref", target_ref, tip, expected)
            return {"fixture": "exact-owner-readback"}
        with mock.patch("merge_queue.cas_target", side_effect=fixture_cas), \
             mock.patch.object(runtime, "reconcile_epoch_members", return_value={}):
            complete = runtime.drive_epoch(self.root, "rc-1", sealed["lease_token"])
        before = self.board_path.read_bytes()
        cas_path = Path(next(row["path"] for row in complete["receipts"]
                             if row["transition"] == "CAS_COMPLETE"))
        original = cas_path.read_bytes(); cas_path.write_bytes(original + b" ")
        with self.assertRaisesRegex(runtime.ReleaseTrainError, "receipt path or hash drifted"):
            runtime.reconcile_epoch_members(self.root, "rc-1", complete["cas"]["readback"])
        self.assertEqual(before, self.board_path.read_bytes())
        cas_path.write_bytes(original)
        run(self.root, "git", "commit", "--allow-empty", "-m", "target moved")
        moved = run(self.root, "git", "rev-parse", "HEAD")
        with self.assertRaisesRegex(runtime.ReleaseTrainError, "protected target moved"):
            runtime.reconcile_epoch_members(self.root, "rc-1", complete["cas"]["readback"])
        self.assertNotEqual(complete["cas"]["readback"], moved)
        self.assertEqual(before, self.board_path.read_bytes())

    def test_aggregate_exact_lock_hydrates_missing_dependencies_before_gate(self) -> None:
        package = {"name": "fixture", "version": "1.0.0", "private": True}
        lock = {"name": "fixture", "version": "1.0.0", "lockfileVersion": 3,
                "requires": True, "packages": {"": package}}
        (self.root / "juno-code/package.json").write_text(json.dumps(package) + "\n")
        (self.root / "juno-code/package-lock.json").write_text(json.dumps(lock) + "\n")
        (self.root / ".gitignore").write_text("juno-code/node_modules/\n")
        config_path = self.root / ".juno_task/config/task-workspace.json"
        config = json.loads(config_path.read_text())
        config["full_suite_validation"] = {"id": "full", "cwd": "juno-code",
            "argv": ["node", "-e", "process.exit(0)"], "timeout_seconds": 30,
            "max_output_bytes": 1024}
        config_path.write_text(json.dumps(config) + "\n")
        run(self.root, "git", "add", "juno-code", ".gitignore", ".juno_task/config/task-workspace.json")
        run(self.root, "git", "commit", "-m", "exact-lock aggregate fixture")
        self.base = run(self.root, "git", "rev-parse", "HEAD")
        self.prepare_epoch()
        sealed = runtime.seal_epoch(self.root, self.declaration)
        def fixture_cas(repository: Path, target_ref: str, tip: str, expected: str) -> dict:
            run(repository, "git", "update-ref", target_ref, tip, expected)
            return {"fixture": "exact-owner-readback"}
        with mock.patch("merge_queue.cas_target", side_effect=fixture_cas):
            state = runtime.drive_epoch(self.root, "rc-1", sealed["lease_token"])
        self.assertEqual("RELEASE_READY", state["state"], state.get("aggregate"))
        self.assertEqual("executed", state["aggregate"]["hydration"]["decision"])
        stamp = Path(state["composition"]["worktree"]) / "juno-code/node_modules/.yylo-package-lock.sha256"
        self.assertTrue(stamp.is_file())
        self.assertEqual(hashlib.sha256((self.root / "juno-code/package-lock.json").read_bytes()).hexdigest(),
                         stamp.read_text().strip())

    def test_failed_aggregate_has_fenced_receipt_retry_without_duplicate_merge_or_cas(self) -> None:
        self.prepare_epoch()
        marker = self.root / "aggregate-ready"
        config_path = self.root / ".juno_task/config/task-workspace.json"
        config = json.loads(config_path.read_text())
        config["full_suite_validation"]["argv"] = [
            sys.executable, "-c",
            f"import pathlib,sys; sys.exit(0 if pathlib.Path({str(marker)!r}).exists() else 7)",
        ]
        config_path.write_text(json.dumps(config) + "\n")
        sealed = runtime.seal_epoch(self.root, self.declaration)
        failed = runtime.drive_epoch(self.root, "rc-1", sealed["lease_token"])
        self.assertEqual("RECOVERING", failed["state"])
        self.assertEqual("command", failed["aggregate"]["stage"])
        merge_commits = list(failed["composition"]["commits"])
        marker.touch()
        validating = runtime.retry_epoch_aggregate(self.root, "rc-1", sealed["lease_token"])
        self.assertEqual("VALIDATING", validating["state"])
        self.assertEqual("AGGREGATE_RETRY_AUTHORIZED", validating["receipts"][-1]["transition"])
        with self.assertRaisesRegex(runtime.ReleaseTrainError, "no failed aggregate"):
            runtime.retry_epoch_aggregate(self.root, "rc-1", sealed["lease_token"])
        cas_calls = 0
        def fixture_cas(repository: Path, target_ref: str, tip: str, expected: str) -> dict:
            nonlocal cas_calls
            cas_calls += 1
            run(repository, "git", "update-ref", target_ref, tip, expected)
            return {"fixture": "exact-owner-readback"}
        with mock.patch("merge_queue.cas_target", side_effect=fixture_cas):
            complete = runtime.drive_epoch(self.root, "rc-1", sealed["lease_token"])
        self.assertEqual("RELEASE_READY", complete["state"])
        self.assertEqual(2, complete["aggregate"]["aggregate_runs"])
        self.assertEqual(merge_commits, complete["composition"]["commits"])
        self.assertEqual(1, cas_calls)
        self.assertEqual(1, complete["cas"]["target_move_count"])

    def test_required_failure_pauses_and_shadow_is_read_only(self) -> None:
        self.prepare_epoch()
        sealed = runtime.seal_epoch(self.root, self.declaration)
        paused = runtime.eject_epoch_member(self.root, "rc-1", "REQ", "seeded failure", sealed["lease_token"])
        self.assertEqual("PAUSED_REQUIRED", paused["state"])
        # A production decision blocks until one exact installed instruction generation exists.
        missing = runtime.shadow_epoch(self.root, self.declaration, None)
        self.assertEqual("BLOCK", missing["decision"])
        self.assertIn("instruction_bundle.missing_or_invalid", missing["blocking_reason_codes"])
        destinations = ["AGENTS.md", "CLAUDE.md",
            ".agents/skills/example/SKILL.md", ".claude/skills/example/SKILL.md",
            ".pi/skills/example/SKILL.md", ".juno_task/prompts/example.md",
            ".juno_task/wiki/example.md", ".juno_task/workflows/example.yaml",
            ".juno_task/scripts/example.py", ".juno_task/scripts/task_workspace.py",
            ".juno_task/scripts/task_workspace_decisions.py"]
        assets = {}
        for destination in destinations:
            target = self.root / destination
            target.parent.mkdir(parents=True, exist_ok=True)
            content = (destination + "\n").encode()
            target.write_bytes(content)
            digest = hashlib.sha256(content).hexdigest()
            assets[destination] = {"type": "fixture", "templateVersion": "1.0.0",
                "sourceSha256": digest, "installedSha256": digest}
        projected = [{"destination": destination, "type": record["type"],
                      "sourceSha256": record["sourceSha256"],
                      "installedSha256": record["installedSha256"]}
                     for destination, record in sorted(assets.items(), key=lambda item: (
                         0 if item[0].startswith(".") else 1,
                         "".join(char for char in item[0].lower() if char.isalnum()), item[0]))]
        assets_identity = hashlib.sha256(
            json.dumps(projected, separators=(",", ":")).encode()).hexdigest()
        bundle = {"schemaVersion": "juno_instruction_bundle.v1",
            "semanticVersion": "1.0.0", "packageVersion": "1.0.0",
            "assetCount": len(assets), "assetsSha256": assets_identity}
        bundle["bundleSha256"] = hashlib.sha256(
            json.dumps(bundle, separators=(",", ":")).encode()).hexdigest()
        (self.root / ".juno_task" / "managed-assets.json").write_text(json.dumps({
            "schemaVersion": 2, "packageName": "@yylo/cli", "packageVersion": "1.0.0",
            "instructionBundle": bundle, "assets": assets}, sort_keys=True) + "\n")
        baseline = self.write_shadow_baseline()
        before = run(self.root, "git", "status", "--porcelain=v1", "--untracked-files=all")
        report = runtime.shadow_epoch(self.root, self.declaration, baseline)
        self.assertEqual("PASS", report["decision"])
        self.assertEqual(105, report["baseline"]["model_lifecycle_calls"])
        self.assertEqual("juno_instruction_bundle.v1", report["instruction_bundle"]["schemaVersion"])
        self.assertEqual([], report["side_effects"])
        self.assertEqual(before, run(self.root, "git", "status", "--porcelain=v1", "--untracked-files=all"))

        state_path = self.root / ".juno_task/runtime/release-epochs/rc-1/state.json"
        historical = runtime.shadow_epoch(self.root, state_path, baseline)
        self.assertEqual("PASS", historical["decision"])
        self.assertEqual("historical_sealed_epoch", historical["replay_source"]["kind"])
        self.assertEqual("PAUSED_REQUIRED", historical["replay_source"]["state"])
        first_receipt = Path(paused["receipts"][0]["path"])
        first_receipt.write_text(first_receipt.read_text() + "tamper\n")
        with self.assertRaisesRegex(runtime.ReleaseTrainError, "receipt identity"):
            runtime.shadow_epoch(self.root, state_path, baseline)

    def test_recovered_worker_receipt_requires_exact_failed_artifacts(self) -> None:
        source = self.root / "recovery-source"; source.mkdir()
        output = self.root / "recovery-output"; output.mkdir()
        session = "session-recovery"
        stdout = source / "stdout.log"; stdout.write_text("completed\n")
        live = source / "live.log"; live.write_text(f"completed\n{session}\n")
        continuity = source / "continuity.json"
        continuity.write_text(json.dumps({"version": 2, "scopes": {"S": {
            "active": "main", "branches": {"main": {"session_id": session}}}}}) + "\n")
        response = output / "response.txt"; response.write_bytes(stdout.read_bytes())
        identity = {"admission_kind": "sealed_release_epoch_conflict", "task_id": "REQ"}
        launch = {"identity": identity}; launch_path = source / "launch.json"
        launch_path.write_text(json.dumps(launch) + "\n")
        terminal = {"state": "failed", "exit_code": 0}; terminal_path = source / "terminal.json"
        terminal_path.write_text(json.dumps(terminal) + "\n")
        failed = {"schema_version": "juno_managed_agent_runner.v1", "mode": "worker",
                  "state": "failed", "failure": "capture is missing or stale", "exit_code": 0,
                  "timed_out": False, "termination_events": [], "identity": identity}
        failed_path = source / "receipt.json"; failed_path.write_text(json.dumps(failed) + "\n")

        def mark(path: Path) -> dict:
            data = path.read_bytes()
            return {"path": str(path), "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest()}

        receipt = {"capture_source": "receipt_bound_worker_recovery", "session_id": session,
                   "artifacts": {"response": mark(response)},
                   "recovery": {"schema_version": "juno_managed_agent_recovery.v1",
                       "kind": "capture_only_no_model_rerun", "validated_exit_code": 0,
                       "failed_receipt": mark(failed_path), "failed_terminal": mark(terminal_path),
                       "launch": mark(launch_path), "live_log": mark(live), "stdout": mark(stdout),
                       "continuity": mark(continuity)}}
        runtime.validate_recovered_worker_receipt(receipt)
        stdout.write_text("tampered\n")
        with self.assertRaisesRegex(runtime.ReleaseTrainError, "stdout identity mismatch"):
            runtime.validate_recovered_worker_receipt(receipt)

    def test_lean_target_drift_refuses_release(self) -> None:
        self.lean_checkout()
        self.finish_board()
        ready = runtime.build_plan(self.root, self.declaration)
        self.assertTrue(ready["release_ready"])

        def rebuild() -> dict:
            # The setUp owner mock stays bound to the original planning base,
            # so drifted generations also carry a topology blocker; the
            # version gates below must still refuse independently of it.
            return runtime.build_plan(self.root, self.declaration)

        # Real lock drift in the target tree must refuse and rebind the plan.
        self.commit_target_drift(lambda: (self.root / "juno-code/package-lock.json").write_text(
            '{"version":"0.9.9","packages":{"":{"version":"1.0.0"}}}\n'))
        mismatch = rebuild()
        self.assertIn("release.version_identity_mismatch", [row["code"] for row in mismatch["blockers"]])
        self.assertFalse(mismatch["release_ready"])
        self.assertNotEqual(ready["plan_id"], mismatch["plan_id"])
        # A target generation without the product manifest must refuse.
        self.commit_target_drift(lambda: (self.root / "juno-code/package.json").unlink())
        missing = rebuild()
        self.assertIn("release.version_missing", [row["code"] for row in missing["blockers"]])
        self.assertFalse(missing["release_ready"])
        # A target version that does not precede the request must refuse.
        self.commit_target_drift(lambda: (
            (self.root / "juno-code/package.json").write_text('{"version":"1.0.1"}\n'),
            (self.root / "juno-code/package-lock.json").write_text(
                '{"version":"1.0.1","packages":{"":{"version":"1.0.1"}}}\n')))
        not_greater = rebuild()
        self.assertIn("release.version_not_greater", [row["code"] for row in not_greater["blockers"]])
        self.assertFalse(not_greater["release_ready"])


if __name__ == "__main__":
    unittest.main()
