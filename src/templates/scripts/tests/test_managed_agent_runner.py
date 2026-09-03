#!/usr/bin/env python3
from __future__ import annotations
import hashlib, importlib.util, json, os
from pathlib import Path
import shutil, signal, subprocess, sys, tempfile, time, unittest
from unittest import mock

RUNNER = Path(__file__).resolve().parents[1] / "managed_agent_runner.py"
RUNNER_SPEC = importlib.util.spec_from_file_location("managed_agent_runner_for_test", RUNNER)
assert RUNNER_SPEC and RUNNER_SPEC.loader
runner = importlib.util.module_from_spec(RUNNER_SPEC); RUNNER_SPEC.loader.exec_module(runner)
RISK = RUNNER.with_name("risk_policy.py")
RISK_SPEC = importlib.util.spec_from_file_location("risk_policy_for_runner_test", RISK)
assert RISK_SPEC and RISK_SPEC.loader
risk = importlib.util.module_from_spec(RISK_SPEC); RISK_SPEC.loader.exec_module(risk)


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


class ManagedAgentRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="managed-agent-test-"))
        self.controller = self.tmp / "controller"; self.controller.mkdir()
        subprocess.run(["git", "init", "-b", "controller", str(self.controller)], check=True, stdout=subprocess.DEVNULL)
        (self.controller / ".juno_task/prompts").mkdir(parents=True)
        (self.controller / ".juno_task/prompts/reflect.md").write_text("controller reflect\n")
        self.env_target = self.tmp / "controller.env"; self.env_target.write_text("MANAGED_TEST_SECRET=not-for-receipts\n")
        os.symlink(self.env_target, self.controller / ".env.yylo")
        config = {"defaultSubagent":"pi", "defaultModel":":configured", "defaultModels":{"pi":":configured"},
                  "envFilePath":".env.yylo", "promptMacros":{"global":{"reflect":{"path":".juno_task/prompts/reflect.md"}},"local":{}}}
        (self.controller / ".juno_task/config.json").write_text(json.dumps(config) + "\n")
        (self.controller / ".juno_task/state").mkdir()
        (self.controller / runner.QUEUE_STATE_PATH).write_text("{}\n")
        subprocess.run(["git", "-C", str(self.controller), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.controller), "-c", "user.name=T", "-c", "user.email=t@t", "commit", "-m", "controller"], check=True, stdout=subprocess.DEVNULL)
        self.candidate = self.tmp / "candidate"; self.candidate.mkdir()
        subprocess.run(["git", "init", "-b", "task", str(self.candidate)], check=True, stdout=subprocess.DEVNULL)
        (self.candidate / "allowed.txt").write_text("base\n")
        subprocess.run(["git", "-C", str(self.candidate), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.candidate), "-c", "user.name=T", "-c", "user.email=t@t", "commit", "-m", "base"], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "-C", str(self.candidate), "branch", "target"], check=True)
        (self.candidate / "src/security").mkdir(parents=True)
        (self.candidate / "src/security/auth.ts").write_text("candidate\n")
        subprocess.run(["git", "-C", str(self.candidate), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.candidate), "-c", "user.name=T", "-c", "user.email=t@t", "commit", "-m", "candidate"], check=True, stdout=subprocess.DEVNULL)
        self.bin = self.tmp / "bin"; self.bin.mkdir()
        self.stale_bin = self.tmp / ".venv_juno/bin"; self.stale_bin.mkdir(parents=True)
        stale_node = self.stale_bin / "node"
        stale_node.write_text("#!/usr/bin/env bash\nif [ \"${1:-}\" = -p ]; then echo 18.15.0; else exit 86; fi\n")
        stale_node.chmod(0o755)
        fake = self.bin / "yy"
        fake.write_text("""#!/usr/bin/env python3
import json, os, pathlib, shutil, subprocess, sys, time
assert sys.argv[1:4] == ['pi','--no-hooks','--config']; assert '-f' in sys.argv and '-p' not in sys.argv
assert pathlib.Path(shutil.which('node')).resolve()==pathlib.Path(os.environ['YYLO_NODE_EXECUTABLE']).resolve()
assert not sys.stdin.read(1)
if os.environ.get('PI_MODEL') or os.environ.get('JUNO_MODEL'): raise SystemExit(91)
assert os.environ.get('JUNO_CONTROLLER_CHECKPOINT_ACTIVE')=='1'
assert os.environ.get('YYLO_PROJECT_BOOTSTRAP_WRITES')=='0'
config=json.loads(pathlib.Path(sys.argv[4]).read_text())
launcher_config=json.loads((pathlib.Path.cwd()/'.juno_task/config.json').read_text())
expected_launcher=dict(config)
expected_launcher['controllerWorkspace']={'mode':'metadata-only','policy':'.juno_task/config/metadata-controller.json'}
assert launcher_config==expected_launcher
assert config['defaultModel']==':configured' and config['defaultModels']['pi']==':configured'
assert pathlib.Path(config['envFilePath']).is_absolute() and pathlib.Path(config['envFilePath']).read_text().startswith('MANAGED_TEST_SECRET=')
macro=pathlib.Path(config['promptMacros']['global']['reflect']['path'])
assert macro.is_absolute() and macro.read_text()=='controller reflect\\n'
prompt=pathlib.Path(sys.argv[sys.argv.index('-f')+1]).read_text()
settled_recovery='settled-worker-recovery' in prompt
if not settled_recovery:
 print('out-before', flush=True); print('err-before', file=sys.stderr, flush=True)
if 'orphan-wait:' in prompt:
 marker=pathlib.Path(prompt.split('orphan-wait:',1)[1].splitlines()[0].strip())
 grand_code=("import pathlib,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
             "time.sleep(1.2); pathlib.Path(%r).write_text('late mutation')" % str(marker))
 child_code=("import os,pathlib,signal,subprocess,sys,time; "
  "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
  "g=subprocess.Popen([sys.executable,'-c',%r],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
  "pathlib.Path(%r).write_text(str(os.getpid())+' '+str(g.pid)); time.sleep(30)" % (grand_code,str(marker)+'.pids'))
 subprocess.Popen([sys.executable,'-c',child_code],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
if 'orphan-wait:' in prompt:
 for _ in range(200): print('tool-heartbeat', file=sys.stderr, flush=True); time.sleep(.05)
else: time.sleep(10 if 'signal-wait' in prompt else .35)
if 'transport-fail' in prompt: raise SystemExit(7)
tool_id=os.environ.get('JUNO_TOOL_ID','managed_agent_runner')
binding=json.loads(os.environ['JUNO_REVIEW_BINDING_JSON']) if os.environ.get('JUNO_REVIEW_BINDING_JSON') else None
findings=([{'code':'FAKE_FINDING','severity':'high','summary':'fixture finding',
 'paths':['src/runtime.py'],'symbols':['run'],'evidence':'frozen candidate evidence',
 'impact':'supported runtime broken','failure_condition':'supported invocation',
 'acceptance_condition':'restore runtime','impact_categories':['supported_runtime'],
 'scope_classification':'candidate_bug',
 'cited_contract':'PDR 2.2 reviewer-scope and anti-scope-creep gate'}]
          if binding and 'review-findings' in prompt else [])
review_result=({'schema_version':'juno_managed_review_result.v3','candidate_sha':binding['candidate_sha'],
 'policy_identity':binding['policy_identity'],'reviewer_role':binding['reviewer_role'],
 'sequence':binding['sequence'],'verdict':'findings' if findings else 'pass',
 'truncated':False,'omitted_finding_count':0,
 'rejection_counters':{'enhancement':1} if findings else {},'findings':findings} if binding else 'answer')
if binding:
 raw=json.dumps(review_result,sort_keys=True,separators=(',',':'))
 review_result=(raw+'\\r\\n \\t' if 'fixture-crlf' in prompt else raw+'\\n')
payload={'session_id':'session-one' if tool_id=='managed_agent_runner' else 'session-'+tool_id,'result':review_result}
if 'typed-' in prompt:
 payload['terminal_outcome']={'schema_version':'juno_managed_agent_terminal_result.v1','state':prompt.split('typed-',1)[1].split()[0]}
if 'semantic-fail' in prompt: payload['is_error']=True
if 'empty' in prompt: payload['result']=''
if settled_recovery:
 root=pathlib.Path(os.environ['TASK_ROOT'])
 (root/'allowed.txt').write_text('settled recovery result\\n')
 subprocess.run(['git','-C',str(root),'add','allowed.txt'],check=True)
 subprocess.run(['git','-C',str(root),'-c','user.name=T','-c','user.email=t@t','commit','-m','settled recovery'],check=True,stdout=subprocess.DEVNULL)
 metadata=pathlib.Path(os.environ['YYLO_SESSION_METADATA_DIRECTORY']); metadata.mkdir(parents=True,exist_ok=True)
 (metadata/'session_continuity.v2.json').write_text(json.dumps({'version':2,'scopes':{'S':{'active':'main','branches':{'main':{'session_id':'session-settled-run'}}}}})+'\\n')
 print('completed\\n\\nsettled worker recovered',flush=True)
else:
 pathlib.Path(os.environ['JUNO_SUBAGENT_CAPTURE_PATH']).write_text(json.dumps(payload)+'\\n')
 print('out-after', flush=True)
""")
        fake.chmod(0o755)
        self.prompt = self.tmp / "input.md"; self.prompt.write_text("ok\n")

    def tearDown(self): shutil.rmtree(self.tmp, ignore_errors=True)

    def install_metadata_controller_contract(self):
        config_path = self.controller / ".juno_task/config.json"
        config = json.loads(config_path.read_text())
        config["controllerWorkspace"] = dict(runner.CANONICAL_METADATA_WORKSPACE)
        config_path.write_bytes(runner.canonical(config))
        policy = json.loads((RUNNER.parents[1] / "config/metadata-controller.json").read_text())
        policy["controller_branch"] = "refs/heads/controller"
        for field in ("copied_metadata", "generated_metadata", "product_forbidden", "tracked_exact",
                      "tracked_recursive", "tracked_top_level_files"):
            policy[field] = sorted(policy[field])
        policy["runtime"]["ignored_roots"] = sorted(policy["runtime"]["ignored_roots"])
        policy_path = self.controller / ".juno_task/config/metadata-controller.json"
        policy_path.parent.mkdir(exist_ok=True)
        policy_path.write_bytes(runner.canonical(policy))
        scripts = self.controller / ".juno_task/scripts"; scripts.mkdir(exist_ok=True)
        resolver = scripts / "controller_resolver.py"
        resolver.write_text("""#!/usr/bin/env python3
import json, pathlib
print(json.dumps({'path':str(pathlib.Path.cwd().resolve()),'role':'controller',
 'source':'fixture','valid':True,'diagnostics':[],'controller_workspace':None}))
""")
        resolver.chmod(0o755)
        subprocess.run(["git", "-C", str(self.controller), "add", ".juno_task"], check=True)
        subprocess.run(["git", "-C", str(self.controller), "-c", "user.name=T",
                        "-c", "user.email=t@t", "commit", "-m", "metadata controller"],
                       check=True, stdout=subprocess.DEVNULL)
        return config_path, policy_path

    def legacy_policy_bytes(self):
        policy = json.loads((RUNNER.parents[1] / "config/metadata-controller.json").read_text())
        policy["controller_branch"] = runner.LEGACY_METADATA_CONTROLLER_BRANCH
        policy["product_ref"] = runner.LEGACY_METADATA_PRODUCT_REF
        policy["runtime"]["ignored_roots"] = [
            ".juno_task/runtime", ".juno_task/scripts", ".venv_juno", ".env.yylo"]
        data = (json.dumps(policy, indent=2, ensure_ascii=False) + "\n").encode()
        self.assertEqual(data, runner.LEGACY_METADATA_POLICY)
        self.assertEqual(hashlib.sha256(data).hexdigest(), runner.LEGACY_METADATA_POLICY_SHA256)
        return data

    def test_controller_identity_binds_only_queue_owned_dirty_state(self):
        state = self.controller / runner.QUEUE_STATE_PATH
        state.write_text('{"state":"reviewing"}\n')
        receipt = self.controller / runner.QUEUE_RECEIPT_ROOT / "T1/candidate/attempt-1/receipt.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_text('{"outcome":"passed"}\n')
        before = runner.controller_identity(self.controller)
        self.assertEqual([item["path"] for item in before["queue_state"]],
                         [runner.QUEUE_RECEIPT_ROOT + "T1/candidate/attempt-1/receipt.json",
                          runner.QUEUE_STATE_PATH])
        with self.assertRaisesRegex(runner.RunnerError, "canonical resolver identity"):
            runner.managed_controller_binding(before)
        before["resolver"] = {"policy_identity": {"fixture": "identity"}}
        binding = runner.managed_controller_binding(before)
        self.assertEqual(binding["schema_version"], "juno_managed_controller_binding.v1")
        self.assertEqual(binding["queue_state"], before["queue_state"])
        state.write_text('{"state":"reviewed"}\n')
        after = runner.controller_identity(self.controller)
        self.assertNotEqual(before, after)
        (self.controller / ".juno_task/config.json").write_text("{}\n")
        with self.assertRaisesRegex(runner.RunnerError, "controller is missing"):
            runner.controller_identity(self.controller)

    def test_resolver_policy_accepts_only_exact_bound_cleanliness_failure(self):
        expected = "canonical sparse controller policy refused: clean"
        result = subprocess.CompletedProcess([], 2, "", "controller-resolver: " + expected + "\n")
        resolved = {"valid": False, "diagnostics": [expected]}
        workspace = {"passed": False, "checks": {"clean": False, "root_exact": True}}
        self.assertTrue(runner.resolver_policy_passes(result, resolved, workspace, True))
        self.assertFalse(runner.resolver_policy_passes(result, resolved, workspace, False))
        wrong = {"passed": False, "checks": {"clean": False, "root_exact": False}}
        self.assertFalse(runner.resolver_policy_passes(result, resolved, wrong, True))
        other = subprocess.CompletedProcess([], 2, "", "controller-resolver: another failure\n")
        self.assertFalse(runner.resolver_policy_passes(other, resolved, workspace, True))

    def test_pretty_metadata_controller_launches_with_null_sparse_evidence(self):
        _, policy_path = self.install_metadata_controller_contract()
        policy_path.write_text(json.dumps(json.loads(policy_path.read_text()), indent=2) + "\n")
        subprocess.run(["git", "-C", str(self.controller), "add", str(policy_path)], check=True)
        subprocess.run(["git", "-C", str(self.controller), "-c", "user.name=T",
                        "-c", "user.email=t@t", "commit", "-m", "pretty policy"],
                       check=True, stdout=subprocess.DEVNULL)
        (self.controller / runner.QUEUE_STATE_PATH).write_text('{"state":"reviewing"}\n')
        queue_receipt = (self.controller / runner.QUEUE_RECEIPT_ROOT
                         / "T1/candidate/attempt-1/receipt.json")
        queue_receipt.parent.mkdir(parents=True)
        queue_receipt.write_text('{"outcome":"passed"}\n')
        out = self.tmp / "metadata-launch"
        result = subprocess.run(self.command(out), env=self.env(), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads((out / "receipt.json").read_text())
        resolver = receipt["controller_before"]["resolver"]
        self.assertEqual(resolver["policy_identity"], {
            "schema_version": "juno_metadata_controller_policy.v1",
            "policy_sha256": hashlib.sha256(policy_path.read_bytes()).hexdigest(),
            "controller_branch": "refs/heads/controller",
        })
        self.assertTrue(resolver["passed"])
        self.assertTrue(resolver["queue_state_bound"])
        self.assertEqual([item["path"] for item in receipt["controller_before"]["queue_state"]],
                         [runner.QUEUE_RECEIPT_ROOT + "T1/candidate/attempt-1/receipt.json",
                          runner.QUEUE_STATE_PATH])

    def test_exact_legacy_metadata_controller_generation_launches(self):
        config_path, policy_path = self.install_metadata_controller_contract()
        policy_path.write_bytes(self.legacy_policy_bytes())
        subprocess.run(["git", "-C", str(self.controller), "add", str(policy_path)], check=True)
        subprocess.run(["git", "-C", str(self.controller), "-c", "user.name=T",
                        "-c", "user.email=t@t", "commit", "-m", "legacy policy"],
                       check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "-C", str(self.controller), "branch", "-m",
                        "juno/controller-metadata-2.1"], check=True)
        self.assertEqual(json.loads(config_path.read_text())["controllerWorkspace"],
                         runner.CANONICAL_METADATA_WORKSPACE)
        out = self.tmp / "legacy-metadata-launch"
        result = subprocess.run(self.command(out), env=self.env(), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        identity = json.loads((out / "receipt.json").read_text())[
            "controller_before"]["resolver"]["policy_identity"]
        self.assertEqual(identity, {
            "schema_version": "juno_metadata_controller_policy.v1",
            "policy_sha256": runner.LEGACY_METADATA_POLICY_SHA256,
            "controller_branch": runner.LEGACY_METADATA_CONTROLLER_BRANCH,
        })

    def test_legacy_metadata_controller_accepts_only_exact_bytes_and_identity(self):
        root = self.tmp / "legacy-policy-cases"
        policy_path = root / runner.CANONICAL_METADATA_WORKSPACE["policy"]
        policy_path.parent.mkdir(parents=True)
        exact = self.legacy_policy_bytes()
        policy_path.write_bytes(exact)
        identity = runner.metadata_controller_policy_identity(
            root, runner.LEGACY_METADATA_CONTROLLER_BRANCH)
        self.assertEqual(identity["policy_sha256"], runner.LEGACY_METADATA_POLICY_SHA256)

        original = json.loads(exact)
        near_misses = []
        for mutate in (
            lambda value: value["runtime"]["ignored_roots"].append(".agents"),
            lambda value: value.update({"extra": True}),
            lambda value: value.pop("product_ref"),
            lambda value: value.update({"product_ref": "refs/heads/not-the-product"}),
            lambda value: value.update({"controller_branch": "refs/heads/controller"}),
        ):
            value = json.loads(json.dumps(original))
            mutate(value)
            near_misses.append((json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode())
        near_misses.extend((runner.canonical(original), exact.rstrip(b"\n"), b"{malformed\n"))
        for data in near_misses:
            policy_path.write_bytes(data)
            with self.assertRaisesRegex(runner.RunnerError, "missing or malformed"):
                runner.metadata_controller_policy_identity(
                    root, runner.LEGACY_METADATA_CONTROLLER_BRANCH)

        policy_path.unlink()
        target = root / "legacy-target.json"; target.write_bytes(exact)
        policy_path.symlink_to(target)
        with self.assertRaisesRegex(runner.RunnerError, "missing or malformed"):
            runner.metadata_controller_policy_identity(
                root, runner.LEGACY_METADATA_CONTROLLER_BRANCH)

        policy_path.unlink(); policy_path.write_bytes(exact)
        with self.assertRaisesRegex(runner.RunnerError, "branch mismatch"):
            runner.metadata_controller_policy_identity(root, "refs/heads/controller")

    def test_metadata_controller_malformed_mismatched_and_sparse_contracts_refuse(self):
        config_path, policy_path = self.install_metadata_controller_contract()
        policy = json.loads(policy_path.read_text())
        policy["controller_branch"] = "refs/heads/not-controller"
        policy_path.write_bytes(runner.canonical(policy))
        subprocess.run(["git", "-C", str(self.controller), "add", str(policy_path)], check=True)
        subprocess.run(["git", "-C", str(self.controller), "-c", "user.name=T",
                        "-c", "user.email=t@t", "commit", "-m", "mismatched policy"],
                       check=True, stdout=subprocess.DEVNULL)
        self.assertEqual(json.loads(config_path.read_text())["controllerWorkspace"],
                         runner.CANONICAL_METADATA_WORKSPACE)
        resolver_result = subprocess.run(
            [sys.executable, str(self.controller / ".juno_task/scripts/controller_resolver.py")],
            cwd=self.controller, capture_output=True, text=True)
        self.assertEqual(resolver_result.returncode, 0, resolver_result.stderr)
        self.assertIsNone(json.loads(resolver_result.stdout)["controller_workspace"])
        with self.assertRaisesRegex(runner.RunnerError, "policy branch mismatch"):
            runner.controller_identity(self.controller.resolve())

        policy_path.write_text("{malformed\n")
        subprocess.run(["git", "-C", str(self.controller), "add", str(policy_path)], check=True)
        subprocess.run(["git", "-C", str(self.controller), "-c", "user.name=T",
                        "-c", "user.email=t@t", "commit", "-m", "malformed policy"],
                       check=True, stdout=subprocess.DEVNULL)
        with self.assertRaisesRegex(runner.RunnerError, "policy is missing or malformed"):
            runner.controller_identity(self.controller.resolve())

        config = json.loads(config_path.read_text())
        config["controllerWorkspace"]["policy"] = ".juno_task/config/not-metadata.json"
        config_path.write_bytes(runner.canonical(config))
        subprocess.run(["git", "-C", str(self.controller), "add", str(config_path)], check=True)
        subprocess.run(["git", "-C", str(self.controller), "-c", "user.name=T",
                        "-c", "user.email=t@t", "commit", "-m", "mismatched pointer"],
                       check=True, stdout=subprocess.DEVNULL)
        with self.assertRaisesRegex(runner.RunnerError, "resolver/policy refused"):
            runner.controller_identity(self.controller.resolve())

        config["controllerWorkspace"] = dict(runner.CANONICAL_SPARSE_WORKSPACE)
        config_path.write_bytes(runner.canonical(config))
        sparse = self.controller / ".juno_task/config/controller-workspace.json"
        sparse.write_text("{}\n")
        subprocess.run(["git", "-C", str(self.controller), "add", str(config_path), str(sparse)], check=True)
        subprocess.run(["git", "-C", str(self.controller), "-c", "user.name=T",
                        "-c", "user.email=t@t", "commit", "-m", "sparse null evidence"],
                       check=True, stdout=subprocess.DEVNULL)
        with self.assertRaisesRegex(runner.RunnerError, "resolver/policy refused"):
            runner.controller_identity(self.controller.resolve())

    def test_derived_child_config_translates_canonical_sparse_controller_contract(self):
        config_path = self.controller / ".juno_task/config.json"
        config = json.loads(config_path.read_text())
        config["controllerWorkspace"] = {
            "enabled": True,
            "policy": ".juno_task/config/controller-workspace.json",
        }
        config_path.write_text(json.dumps(config) + "\n")
        out = self.tmp / "derived-compatibility"; out.mkdir()
        contract, _ = runner.derive_compatible_config(self.controller, out)
        derived = json.loads(Path(contract["derived"]["path"]).read_text())
        self.assertEqual(derived["controllerWorkspace"], {
            "mode": "metadata-only",
            "policy": ".juno_task/config/metadata-controller.json",
        })
        self.assertEqual(contract["transformations"], [{
            "setting": "controllerWorkspace",
            "reason": "neutral managed child compatibility",
            "source_contract": "canonical-sparse",
            "derived_contract": "metadata-only",
        }])

    def command(self, out: Path, prompt: Path | None = None):
        branch_ref = git(self.controller, "symbolic-ref", "HEAD")
        return [sys.executable, str(RUNNER), "run", "--mode", "reviewer", "--controller-root", str(self.controller),
                "--controller-branch", branch_ref, "--agent-root", str(out / "agent-root"),
                "--prompt-file", str(prompt or self.prompt), "--out-dir", str(out), "--candidate-sha", git(self.candidate, "rev-parse", "HEAD"),
                "--candidate-root", str(self.candidate), "--external-side-effects", "forbidden", "--lifecycle-hooks", "disabled"]

    def env(self):
        canonical_node = shutil.which("node", path=os.environ["PATH"])
        assert canonical_node
        return {**os.environ,
                "PATH": os.pathsep.join((str(self.stale_bin), str(self.bin), os.environ["PATH"])),
                "YYLO_NODE_EXECUTABLE": canonical_node,
                "PI_MODEL": "forbidden", "JUNO_MODEL": "forbidden"}

    def test_required_terminal_result_is_receipt_bound_and_typed(self):
        prompt = self.tmp / "typed.md"; prompt.write_text("typed-blocked\n")
        blocked = self.tmp / "typed-blocked"
        result = subprocess.run(self.command(blocked, prompt) + ["--require-terminal-result"],
                                env=self.env(), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads((blocked / "receipt.json").read_text())
        self.assertEqual(receipt["effective_hook_policy"], {
            "schema_version": "juno_managed_hook_policy.v1",
            "external_side_effects": "forbidden", "lifecycle_hooks": "disabled",
            "enforcement": "yy_pi_no_hooks",
        })
        terminal = receipt["terminal_result"]
        self.assertEqual(terminal["state"], "blocked")
        self.assertEqual(terminal["session_id"], receipt["session_id"])
        self.assertEqual(terminal["response_sha256"], receipt["artifacts"]["response"]["sha256"])
        self.assertEqual(terminal["identity_sha256"], runner.sha(runner.canonical(receipt["identity"]).rstrip(b"\n")))

        missing = self.tmp / "typed-missing"
        refused = subprocess.run(self.command(missing) + ["--require-terminal-result"],
                                 env=self.env(), capture_output=True, text=True)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("lacks required typed terminal outcome", refused.stderr)

    def test_conflicting_parent_and_venv_path_use_canonical_yy_node_runtime(self):
        out = self.tmp / "node-contract"
        inherited = self.env()
        self.assertEqual("18.15.0", subprocess.check_output(
            ["node", "-p", "process.versions.node"], env=inherited, text=True).strip())
        result = subprocess.run(self.command(out), env=inherited, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        launch = json.loads((out / "launch.json").read_text())
        node = launch["environment_contract"]["node_runtime"]
        self.assertEqual(Path(inherited["YYLO_NODE_EXECUTABLE"]), Path(node["executable"]))
        self.assertEqual("18.15.0", node["path_node_version_before"])
        self.assertNotEqual("18.15.0", node["version"])
        self.assertIn("PATH", launch["environment_contract"]["explicit_key_names"])

    def test_unsupported_canonical_node_fails_before_managed_child_with_diagnostics(self):
        out = self.tmp / "unsupported-node"
        inherited = self.env()
        inherited["YYLO_NODE_EXECUTABLE"] = str(self.stale_bin / "node")
        result = subprocess.run(self.command(out), env=inherited, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"canonical executable: {self.stale_bin / 'node'}", result.stderr)
        self.assertIn("canonical version: 18.15.0", result.stderr)
        self.assertIn("PATH node executable:", result.stderr)
        self.assertIn("required version: Node.js >=20.10", result.stderr)
        self.assertFalse((out / "launch.json").exists())
        self.assertFalse((out / "stdout.log").exists())

    def test_live_separate_and_labelled_streams_before_exit(self):
        out = self.tmp / "stream"
        proc = subprocess.Popen(self.command(out), env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not (out / "stdout.log").exists(): time.sleep(.01)
        while time.monotonic() < deadline and b"out-before" not in (out / "stdout.log").read_bytes(): time.sleep(.01)
        self.assertIsNone(proc.poll()); self.assertIn(b"err-before", (out / "stderr.log").read_bytes())
        self.assertEqual(proc.wait(timeout=3), 0)
        assert proc.stdout and proc.stderr; proc.stdout.close(); proc.stderr.close()
        self.assertEqual((out / "stdout.log").read_bytes(), b"out-before\nout-after\n")
        combined = (out / "combined.log").read_bytes(); self.assertIn(b"[stdout] out-before\n", combined); self.assertIn(b"[stderr] err-before\n", combined)
        receipt = json.loads((out / "receipt.json").read_text()); self.assertEqual(receipt["session_id"], "session-one")
        self.assertFalse(receipt["timed_out"])
        live = receipt["live_log"]
        self.assertTrue(live["path"].startswith("/tmp/yy-managed-reviewer-managed_agent_runner-"))
        self.assertEqual(hashlib.sha256(Path(live["path"]).read_bytes()).hexdigest(), live["sha256"])
        config_contract = receipt["compatible_config"]
        derived = json.loads(Path(config_contract["derived"]["path"]).read_text())
        launch = json.loads((out / "launch.json").read_text())
        launcher_payload = json.loads(Path(launch["launcher_config"]["path"]).read_text())
        self.assertEqual("metadata-only", launcher_payload["controllerWorkspace"]["mode"])
        self.assertEqual(derived["defaultModel"], launcher_payload["defaultModel"])
        self.assertIn("YYLO_PROJECT_BOOTSTRAP_WRITES",
                      launch["environment_contract"]["explicit_key_names"])
        self.assertEqual({x["setting"] for x in config_contract["path_mappings"]},
                         {"envFilePath", "promptMacros.global.reflect.path"})
        self.assertEqual(derived["defaultModel"], ":configured")
        self.assertEqual(derived["envFilePath"], str(self.controller.resolve() / ".env.yylo"))
        self.assertEqual(derived["promptMacros"]["global"]["reflect"]["path"],
                         str(self.controller.resolve() / ".juno_task/prompts/reflect.md"))
        self.assertEqual(Path(derived["envFilePath"]).resolve(), self.env_target.resolve())
        self.assertNotIn("not-for-receipts", (out / "receipt.json").read_text())
        self.assertEqual(receipt["compatible_config_sha256"], json.loads((out / "terminal.json").read_text())["compatible_config_sha256"])
        self.assertFalse((out / "active.json").exists()); self.assertEqual((out / "response.txt").read_text(), "answer")

    def test_timeout_is_distinct_and_has_terminal_live_log_receipt(self):
        prompt = self.tmp / "timeout.md"; prompt.write_text("signal-wait")
        out = self.tmp / "timeout-out"
        result = subprocess.run(self.command(out, prompt) + ["--timeout-seconds", "0.1"],
                                env=self.env(), capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        terminal = json.loads((out / "terminal.json").read_text())
        self.assertTrue(terminal["timed_out"])
        self.assertEqual(terminal["exit_code"], -15)
        self.assertEqual(terminal["exit_signal"], "SIGTERM")
        self.assertEqual(terminal["termination_events"][0]["reason"], "timeout")
        self.assertIn("exit_signal=SIGTERM timed_out=true", result.stderr)
        self.assertEqual(hashlib.sha256(Path(terminal["live_log"]["path"]).read_bytes()).hexdigest(),
                         terminal["live_log"]["sha256"])

    def test_transport_and_semantic_fail_without_artificial_wait(self):
        for text in ("transport-fail", "semantic-fail", "empty"):
            prompt = self.tmp / f"{text}.md"; prompt.write_text(text)
            started = time.monotonic(); result = subprocess.run(self.command(self.tmp / ("out-" + text), prompt), env=self.env(), capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0); self.assertLess(time.monotonic() - started, 1.5)
            terminal = json.loads((self.tmp / ("out-" + text) / "terminal.json").read_text()); self.assertEqual(terminal["state"], "failed")

    def test_structured_review_parser_accepts_terminal_whitespace_and_is_strict(self):
        binding = {"candidate_sha": "1" * 40, "policy_identity": "2" * 64,
                   "reviewer_role": "reviewer_a", "sequence": 1}
        passing = {"schema_version": runner.REVIEW_RESULT_SCHEMA,
                   "candidate_sha": binding["candidate_sha"],
                   "policy_identity": binding["policy_identity"],
                   "reviewer_role": "reviewer_a", "sequence": 1,
                   "verdict": "pass", "truncated": False, "omitted_finding_count": 0,
                   "rejection_counters": {}, "findings": []}
        raw = json.dumps(passing, separators=(",", ":")).encode()
        for data in (raw, raw + b"\n", raw + b"\r\n \t"):
            self.assertEqual(passing, runner.structured_review_result(data, binding))
        finding = {**passing, "verdict": "findings", "findings": [
            {"code": "AUTH_BYPASS", "severity": "critical", "summary": "Missing guard",
             "paths": ["src/auth.py"], "symbols": ["authorize"], "evidence": "guard absent",
             "impact": "authorization bypass", "failure_condition": "protected call",
             "acceptance_condition": "restore guard", "impact_categories": ["security_privacy"],
             "scope_classification": "safety_invariant_violation",
             "cited_contract": "PDR 21 safety invariants"}]}
        self.assertEqual(finding, runner.structured_review_result(json.dumps(finding).encode(), binding))

        unknown = {**passing, "unknown": True}
        bad_severity = json.loads(json.dumps(finding))
        bad_severity["findings"][0]["severity"] = "urgent"
        # Prose-wrapped single islands are now accepted format-only tolerance
        # (verified by the island tests below); ambiguity and schema violations
        # stay exact-JSON failures.
        invalid = (json.dumps(unknown).encode(), json.dumps(bad_severity).encode(),
                   raw + b"\n" + raw,
                   b"{malformed", json.dumps({**passing, "sequence": True}).encode())
        for data in invalid:
            with self.assertRaises(runner.RunnerError):
                runner.structured_review_result(data, binding)

    def test_structured_review_parser_accepts_one_unambiguous_prose_wrapped_island(self):
        binding = {"candidate_sha": "1" * 40, "policy_identity": "2" * 64,
                   "reviewer_role": "reviewer_b", "sequence": 2}
        verdict = {"schema_version": runner.REVIEW_RESULT_SCHEMA,
                   "candidate_sha": binding["candidate_sha"],
                   "policy_identity": binding["policy_identity"],
                   "reviewer_role": "reviewer_b", "sequence": 2,
                   "verdict": "pass", "truncated": False, "omitted_finding_count": 0,
                   "rejection_counters": {}, "findings": []}
        body = json.dumps(verdict)
        prose = (f"I verified all prior findings against the tip.\n\n"
                 f"Summary of the pass:\n\n```text\nJUNO_REVIEW_VERDICT: PASS\n```\n\n"
                 f"{body}\n").encode()
        self.assertEqual(verdict, runner.structured_review_result(prose, binding))
        finding_verdict = {**verdict, "verdict": "findings", "findings": [
            {"code": "NESTED", "severity": "medium",
             "summary": "braces { inside } summaries stay one island", "paths": ["src/a.py"],
             "symbols": [], "evidence": "bounded defect", "impact": "bounded impact",
             "failure_condition": "edge case", "acceptance_condition": "handle edge case",
             "impact_categories": ["bounded_product_defect"],
             "scope_classification": "candidate_bug",
             "cited_contract": "PDR 2.1 bootstrap gate"}]}
        nested = ("review prose\n" + json.dumps(finding_verdict) + "\nfooter\n").encode()
        self.assertEqual(finding_verdict,
                         runner.structured_review_result(nested, binding))

    def test_structured_review_parser_rejects_ambiguous_or_keyless_islands(self):
        binding = {"candidate_sha": "1" * 40, "policy_identity": "2" * 64,
                   "reviewer_role": "reviewer_b", "sequence": 2}
        verdict = {"schema_version": runner.REVIEW_RESULT_SCHEMA,
                   "candidate_sha": binding["candidate_sha"],
                   "policy_identity": binding["policy_identity"],
                   "reviewer_role": "reviewer_b", "sequence": 2,
                   "verdict": "pass", "truncated": False, "omitted_finding_count": 0,
                   "rejection_counters": {}, "findings": []}
        other = {**verdict, "sequence": 3}
        two_islands = (json.dumps(verdict) + "\n" + json.dumps(other) + "\n").encode()
        unrelated_objects = b'{"summary": "prose quote with json"} and {"note": 1}\n'
        missing_keys = b'{"verdict": "pass"}\n'
        # two_islands is ambiguous prose (no exact parse, two islands);
        # unrelated_objects has no island; missing_keys is valid JSON with an
        # incomplete schema. All fail closed; only the reason differs.
        for data, pattern in ((two_islands, "is not exact JSON"),
                              (unrelated_objects, "is not exact JSON"),
                              (missing_keys, "schema/binding is invalid")):
            with self.assertRaisesRegex(runner.RunnerError, pattern):
                runner.structured_review_result(data, binding)

    def test_structured_review_parser_tolerates_bounded_provider_footer_bytes(self):
        binding = {"candidate_sha": "1" * 40, "policy_identity": "2" * 64,
                   "reviewer_role": "reviewer_a", "sequence": 1}
        verdict = {"schema_version": runner.REVIEW_RESULT_SCHEMA,
                   "candidate_sha": binding["candidate_sha"],
                   "policy_identity": binding["policy_identity"],
                   "reviewer_role": "reviewer_a", "sequence": 1,
                   "verdict": "pass", "truncated": False, "omitted_finding_count": 0,
                   "rejection_counters": {"enhancement": 2}, "findings": []}
        body = json.dumps(verdict).encode()
        # A valid exhaustive structured result must survive provider footer/log
        # bytes that push the raw capture past the old 64 KiB exact bound.
        footer = (b"\nprovider usage footer\n" + b"x" * 70000 + b"\n")
        self.assertGreater(len(body) + len(footer), 65536)
        self.assertLess(len(body) + len(footer), runner.REVIEW_RAW_CAPTURE_LIMIT)
        self.assertEqual(verdict,
                         runner.structured_review_result(body + footer, binding))
        # The raw capture itself stays bounded: an unbounded flood still fails.
        with self.assertRaisesRegex(runner.RunnerError, "empty or unbounded"):
            runner.structured_review_result(body + b"y" * (1024 * 1024 + 1), binding)
        # And an extracted result larger than the strict bound still fails.
        oversized = {**verdict, "rejection_counters": {}}
        oversized["findings"] = [{"code": f"F{i}", "severity": "low",
            "summary": "s" * 1024, "paths": ["src/a.py"], "symbols": [],
            "evidence": "e" * 1024, "impact": "i" * 1024,
            "failure_condition": "f" * 1024, "acceptance_condition": "a" * 1024,
            "impact_categories": ["clarity"],
            "scope_classification": "candidate_bug",
            "cited_contract": "c" * 1024} for i in range(32)]
        self.assertGreater(len(runner.canonical(oversized)), 65536)
        with self.assertRaisesRegex(runner.RunnerError, "bounded capture contract"):
            runner.structured_review_result(json.dumps(oversized).encode(), binding)

    def test_structured_review_parser_requires_admitted_scope_fields(self):
        binding = {"candidate_sha": "1" * 40, "policy_identity": "2" * 64,
                   "reviewer_role": "reviewer_a", "sequence": 1}
        base = {"schema_version": runner.REVIEW_RESULT_SCHEMA,
                "candidate_sha": binding["candidate_sha"],
                "policy_identity": binding["policy_identity"],
                "reviewer_role": "reviewer_a", "sequence": 1,
                "verdict": "findings", "truncated": False, "omitted_finding_count": 0,
                "rejection_counters": {}, "findings": [
                    {"code": "F0", "severity": "high", "summary": "gap",
                     "paths": ["src/a.py"], "symbols": [], "evidence": "missing guard",
                     "impact": "runtime broken", "failure_condition": "invoke path",
                     "acceptance_condition": "restore guard",
                     "impact_categories": ["supported_runtime"],
                     "scope_classification": "candidate_bug",
                     "cited_contract": "PDR 2.2 scope gate"}]}
        self.assertEqual(base, runner.structured_review_result(
            json.dumps(base).encode(), binding))
        for mutate in (lambda v: v["findings"][0].pop("scope_classification"),
                       lambda v: v["findings"][0].__setitem__("scope_classification", "enhancement"),
                       lambda v: v["findings"][0].__setitem__("cited_contract", ""),
                       lambda v: v.__setitem__("rejection_counters", {"nice_to_have": 1}),
                       lambda v: v.pop("rejection_counters")):
            variant = json.loads(json.dumps(base))
            mutate(variant)
            with self.assertRaisesRegex(runner.RunnerError,
                                        "malformed or unbounded|admitted scope|schema/binding"):
                runner.structured_review_result(json.dumps(variant).encode(), binding)

    def test_reviewer_stdout_finalizer_requires_exact_result_and_fresh_single_session(self):
        binding = {"candidate_sha": "1" * 40, "policy_identity": "2" * 64,
                   "reviewer_role": "reviewer_a", "sequence": 1}
        passing = {"schema_version": runner.REVIEW_RESULT_SCHEMA,
                   "candidate_sha": binding["candidate_sha"],
                   "policy_identity": binding["policy_identity"],
                   "reviewer_role": "reviewer_a", "sequence": 1,
                   "verdict": "pass", "truncated": False, "omitted_finding_count": 0,
                   "rejection_counters": {}, "findings": []}
        root = self.tmp / "stdout-finalizer"; root.mkdir()
        capture = root / "capture.json"; stdout = root / "stdout.log"
        metadata = root / "session_metadata"; metadata.mkdir()
        started_ns = time.time_ns()
        stdout.write_bytes(runner.canonical(passing))
        (metadata / "session_continuity.v2.json").write_text(json.dumps({
            "version": 2, "scopes": {"SCOPE_TEST": {"active": "main", "branches": {
                "main": {"session_id": "fresh-review-session"}}}}}))
        self.assertEqual("managed_stdout_finalizer", runner.finalize_managed_capture(
            capture, stdout, metadata, binding, started_ns))
        payload = json.loads(capture.read_text())
        self.assertEqual("fresh-review-session", payload["session_id"])
        self.assertEqual("managed_stdout_finalizer", payload["capture_source"])
        self.assertEqual(passing, json.loads(payload["result"]))

        capture.unlink(); stdout.write_text("log prefix\n" + json.dumps(passing))
        # Format-only prose wrapping of one unambiguous verdict now binds.
        self.assertEqual("managed_stdout_finalizer", runner.finalize_managed_capture(
            capture, stdout, metadata, binding, started_ns))
        capture.unlink(); stdout.write_text(
            json.dumps(passing) + "\n" + json.dumps(passing) + "\n")
        with self.assertRaisesRegex(runner.RunnerError, "structured review result"):
            runner.finalize_managed_capture(capture, stdout, metadata, binding, started_ns)
        with self.assertRaisesRegex(runner.RunnerError, "capture is missing"):
            runner.finalize_managed_capture(capture, stdout, metadata, None, started_ns)

    def test_settled_worker_capture_recovery_is_exact_atomic_and_fail_closed(self):
        root = self.tmp / "settled-worker"; root.mkdir()
        capture = root / "capture.json"
        stdout = root / "stdout.log"
        metadata = root / "session_metadata"; metadata.mkdir()
        continuity = metadata / "session_continuity.v2.json"
        started_ns = time.time_ns()
        stdout.write_text("completed\n\nworker finished exactly once\n")
        continuity.write_text(json.dumps({"version": 2, "scopes": {"S": {
            "active": "main", "branches": {"main": {
                "session_id": "session-settled"}}}}}) + "\n")
        before = runner.fingerprint(self.candidate)
        (self.candidate / "allowed.txt").write_text("settled result\n")
        subprocess.run(["git", "-C", str(self.candidate), "add", "allowed.txt"], check=True)
        subprocess.run(["git", "-C", str(self.candidate), "-c", "user.name=T",
                        "-c", "user.email=t@t", "commit", "-m", "settled result"],
                       check=True, stdout=subprocess.DEVNULL)
        after = runner.fingerprint(self.candidate)
        identity = {"task_id": "T1", "expected_paths": ["allowed.txt"],
                    "admission_kind": "historical_creation", "before": before,
                    "worker_attempt": {"task_id": "T1", "tool_id": "yy_task_implementation",
                    "out_dir": str(root.resolve()), "prompt_sha256": "1" * 64}}
        source, payload = runner.recover_settled_worker_capture(
            capture, stdout, metadata, identity, before, after, started_ns,
            exit_code=0, timed_out=False, interrupted=0, termination_events=[],
            process_settled=True)
        self.assertEqual("settled_worker_recovery", source)
        self.assertEqual("session-settled", payload["session_id"])
        self.assertEqual("completed", payload["terminal_outcome"]["state"])
        self.assertEqual(after["head"], payload["recovery_binding"]["commit_sha"])
        original = capture.read_bytes()
        repeated = runner.recover_settled_worker_capture(
            capture, stdout, metadata, identity, before, after, started_ns,
            exit_code=0, timed_out=False, interrupted=0, termination_events=[],
            process_settled=True)
        self.assertEqual((source, payload), repeated)
        self.assertEqual(original, capture.read_bytes())

        cases = (
            ("nonzero", {"exit_code": 7}, "nonzero_exit"),
            ("timeout", {"timed_out": True}, "incomplete_settlement"),
            ("interrupted", {"interrupted": signal.SIGTERM}, "interrupted"),
            ("descendants", {"termination_events": [{"reason": "live"}]}, "incomplete_settlement"),
            ("live-process", {"process_settled": False}, "incomplete_settlement"),
        )
        capture.unlink()
        defaults = {"exit_code": 0, "timed_out": False, "interrupted": 0,
                    "termination_events": [], "process_settled": True}
        for label, changed, reason in cases:
            with self.subTest(label=label), self.assertRaisesRegex(runner.RunnerError, reason):
                runner.recover_settled_worker_capture(
                    capture, stdout, metadata, identity, before, after, started_ns,
                    **{**defaults, **changed})
            self.assertFalse(capture.exists())

        with self.assertRaisesRegex(runner.RunnerError, "dirty_state"):
            runner.recover_settled_worker_capture(
                capture, stdout, metadata, identity, before,
                {**after, "status": "? dirty.txt"}, started_ns, **defaults)
        with self.assertRaisesRegex(runner.RunnerError, "mismatched_identity"):
            runner.recover_settled_worker_capture(
                capture, stdout, metadata,
                {**identity, "worker_attempt": {**identity["worker_attempt"],
                                                  "task_id": "WRONG"}},
                before, after, started_ns, **defaults)
        with self.assertRaisesRegex(runner.RunnerError, "incomplete_result"):
            runner.recover_settled_worker_capture(
                capture, stdout, metadata, identity, before, before, started_ns, **defaults)
        old = started_ns - 1_000_000
        os.utime(continuity, ns=(old, old))
        with self.assertRaisesRegex(runner.RunnerError, "stale_capture"):
            runner.recover_settled_worker_capture(
                capture, stdout, metadata, identity, before, after, started_ns, **defaults)
        os.utime(continuity, None)

        provider = {key: value for key, value in payload.items()
                    if key not in {"capture_source", "recovery_binding"}}
        provider["capture_source"] = "provider_capture"
        runner.atomic_json(capture, provider)
        (root / "capture-terminal.json").unlink()
        provider_bytes = capture.read_bytes()
        accepted_source, accepted = runner.recover_settled_worker_capture(
            capture, stdout, metadata, identity, before, after, started_ns, **defaults)
        self.assertEqual("provider_capture_recovered_stale", accepted_source)
        self.assertEqual(provider, accepted)
        self.assertEqual(provider_bytes, capture.read_bytes())
        capture.unlink(); (root / "capture-terminal.json").unlink()

        stdout.write_text("completed\nfirst\n")
        continuity.write_text(json.dumps({"version": 2, "scopes": {
            "A": {"active": "main", "branches": {"main": {"session_id": "one"}}},
            "B": {"active": "main", "branches": {"main": {"session_id": "two"}}}}}) + "\n")
        with self.assertRaisesRegex(runner.RunnerError, "ambiguous_capture"):
            runner.recover_settled_worker_capture(
                capture, stdout, metadata, identity, before, after, started_ns, **defaults)
        self.assertFalse(capture.exists())

    def prepare_receipt_bound_conflict(self):
        scripts = self.controller / ".juno_task/scripts"; scripts.mkdir(parents=True, exist_ok=True)
        helper = scripts / "worktree_hydration.py"
        helper.write_bytes(RUNNER.with_name("worktree_hydration.py").read_bytes())
        helper.chmod(0o755)
        exclude = self.controller / ".git/info/exclude"
        exclude.write_text(exclude.read_text()
                           + "\n.juno_task/runtime/\n.juno_task/scripts/worktree_hydration.py\n")
        managed = self.candidate / ".juno_task/managed-assets.json"
        fixture = self.candidate / ".juno_task/scripts/tests/test_task_workspace.py"
        managed.parent.mkdir(parents=True, exist_ok=True); managed.write_text('{"base":true}\n')
        fixture.parent.mkdir(parents=True, exist_ok=True); fixture.write_text("base fixture\n")
        subprocess.run(["git", "-C", str(self.candidate), "add", str(managed), str(fixture)], check=True)
        subprocess.run(["git", "-C", str(self.candidate), "-c", "user.name=T",
                        "-c", "user.email=t@t", "commit", "-m", "managed base"],
                       check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "-C", str(self.candidate), "branch", "-f", "target", "HEAD"], check=True)
        base = git(self.candidate, "rev-parse", "target")
        managed.write_text('{"ours":true}\n')
        subprocess.run(["git", "-C", str(self.candidate), "add", str(managed)], check=True)
        subprocess.run(["git", "-C", str(self.candidate), "-c", "user.name=T",
                        "-c", "user.email=t@t", "commit", "-m", "conflict ours"],
                       check=True, stdout=subprocess.DEVNULL)
        ours = git(self.candidate, "rev-parse", "HEAD")
        subprocess.run(["git", "-C", str(self.candidate), "checkout", "-b", "receipt-theirs", "target"],
                       check=True, stdout=subprocess.DEVNULL)
        managed = self.candidate / ".juno_task/managed-assets.json"
        fixture = self.candidate / ".juno_task/scripts/tests/test_task_workspace.py"
        managed.parent.mkdir(parents=True, exist_ok=True); managed.write_text('{"theirs":true}\n')
        fixture.write_text("theirs fixture\n")
        subprocess.run(["git", "-C", str(self.candidate), "add", str(managed), str(fixture)], check=True)
        subprocess.run(["git", "-C", str(self.candidate), "-c", "user.name=T",
                        "-c", "user.email=t@t", "commit", "-m", "conflict theirs"],
                       check=True, stdout=subprocess.DEVNULL)
        theirs = git(self.candidate, "rev-parse", "HEAD")
        subprocess.run(["git", "-C", str(self.candidate), "checkout", "task"],
                       check=True, stdout=subprocess.DEVNULL)
        merge = subprocess.run(["git", "-C", str(self.candidate), "merge", "--no-ff", "--no-commit",
                                theirs], capture_output=True, text=True)
        self.assertNotEqual(0, merge.returncode)
        epoch_id = "receipt-bound-conflict"
        authority = self.tmp / "conflict-authority.json"
        runner.atomic_json(authority, {"fixture": "authority"})
        authority_ref = runner.evidence(authority)
        managed_path = ".juno_task/managed-assets.json"
        logical = {"set_id": "managed", "ordered_task_ids": ["T1"],
                   "permitted_paths": [managed_path], "classification": "authorization_neutral",
                   "authority_sha256": authority_ref["sha256"]}
        conflict = {"schema_version": "juno_release_epoch_conflict.v2", "task_id": "T1",
                    "base_sha": base, "ours_sha": ours, "theirs_sha": theirs,
                    "conflict_paths": [managed_path], "admitted_paths": [managed_path],
                    "dependency_edges": [], "requirements_sha256": "1" * 64,
                    "repair_budget": 1,
                    "authority": "declaration_bound_authorization_neutral_logical_set",
                    "logical_conflict_set": logical,
                    "validation_root": {"cwd": "juno-code", "timeout_seconds": 30}}
        manifest_body = {"authority_binding": {"path": str(authority),
                         "sha256": authority_ref["sha256"]}}
        manifest = {**manifest_body,
                    "manifest_sha256": runner.sha(runner.canonical(manifest_body).rstrip(b"\n"))}
        receipt_body = {"schema_version": "juno_release_epoch_receipt.v1",
                        "epoch_id": epoch_id, "sequence": 1, "transition": "CONFLICT",
                        "created_utc": "2026-08-29T00:00:00Z", "previous_state": "COMPOSING",
                        "detail": conflict}
        receipt_body["receipt_id"] = runner.sha(runner.canonical(receipt_body).rstrip(b"\n"))
        receipt = (self.controller / ".juno_task/runtime/release-epochs" / epoch_id
                   / "receipts/0001-conflict.json")
        runner.atomic_json(receipt, receipt_body)
        state = {"schema_version": "juno_release_epoch_state.v1", "epoch_id": epoch_id,
                 "state": "RECOVERING", "seal": {"target_ref": "refs/heads/target",
                 "base_sha": base, "fencing_token_sha256": "2" * 64,
                 "members": [{"task_id": "T1", "changed_paths": [managed_path,
                              ".juno_task/scripts/tests/test_task_workspace.py"]}],
                 "conflict_manifest": manifest},
                 "composition": {"worktree": str(self.candidate), "tip_sha": ours},
                 "conflict": conflict, "receipts": [{"path": str(receipt),
                 "sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
                 "receipt_id": receipt_body["receipt_id"], "transition": "CONFLICT"}]}
        state_path = receipt.parents[1] / "state.json"; runner.atomic_json(state_path, state)
        args = runner.argparse.Namespace(
            task_id="T1", candidate_sha=theirs, agent_root=str(self.candidate),
            tool_id="managed_agent_runner", out_dir=str(self.tmp / "conflict-worker"),
            prompt_file=str(self.prompt), create_receipt=None, verify_receipt=None,
            edit_preflight_receipt=None)
        return args, state_path, authority, receipt

    def test_conflict_admission_binds_epoch_receipt_stages_index_target_and_worker(self):
        args, state_path, authority, receipt = self.prepare_receipt_bound_conflict()
        identity, before = runner.release_conflict_admission(
            args, runner.controller_identity(self.controller))
        self.assertEqual("sealed_release_epoch_conflict", identity["admission_kind"])
        self.assertEqual([".juno_task/managed-assets.json"],
                         identity["conflict_checkout"]["conflict_paths"])
        self.assertEqual([".juno_task/scripts/tests/test_task_workspace.py"],
                         [row["path"] for row in identity["applied_paths"]])
        self.assertEqual({1, 2, 3}, {row["stage"] for row in
                                     identity["conflict_checkout"]["unmerged_stages"]})
        self.assertEqual(hashlib.sha256(receipt.read_bytes()).hexdigest(),
                         identity["authority_receipt"]["sha256"])
        self.assertEqual(hashlib.sha256(authority.read_bytes()).hexdigest(),
                         identity["conflict_authority"]["sha256"])
        self.assertEqual(before["head"], identity["composition_tip"])
        self.assertEqual(str((self.tmp / "conflict-worker").resolve()),
                         identity["worker_attempt"]["out_dir"])

        original_state = state_path.read_bytes()
        cases = []
        (self.candidate / "unbound.txt").write_text("unbound\n")
        cases.append(("unbound dirt", lambda: (self.candidate / "unbound.txt").unlink()))
        for label, restore in cases:
            with self.subTest(label=label), self.assertRaises(runner.RunnerError):
                runner.release_conflict_admission(args, runner.controller_identity(self.controller))
            restore()
        state = json.loads(original_state)
        state["seal"]["fencing_token_sha256"] = "3" * 64
        runner.atomic_json(state_path, state)
        changed, _ = runner.release_conflict_admission(args, runner.controller_identity(self.controller))
        self.assertNotEqual(identity["epoch_fencing_token_sha256"],
                            changed["epoch_fencing_token_sha256"])
        state_path.write_bytes(original_state)
        stale_args = runner.argparse.Namespace(**{**vars(args),
            "out_dir": str(self.tmp / "stale-worker-attempt")})
        stale, _ = runner.release_conflict_admission(
            stale_args, runner.controller_identity(self.controller))
        self.assertNotEqual(identity["worker_attempt"], stale["worker_attempt"])

        (self.candidate / "unbound.txt").write_text("refuse before provider\n")
        run_args = runner.argparse.Namespace(**{**vars(args), "mode": "worker",
            "controller_root": str(self.controller), "controller_branch": "controller",
            "candidate_root": None, "review_binding": None, "authority_map": None,
            "external_side_effects": "forbidden", "lifecycle_hooks": "disabled",
            "timeout_seconds": 30.0, "require_terminal_result": False})
        with self.assertRaises(runner.RunnerError):
            runner.run(run_args)
        self.assertFalse((Path(run_args.out_dir) / "launch.json").exists())
        self.assertFalse((Path(run_args.out_dir) / "stdout.log").exists())

    def test_conflict_admission_refuses_resolution_before_admission(self):
        args, *_ = self.prepare_receipt_bound_conflict()
        path = self.candidate / ".juno_task/managed-assets.json"
        path.write_text('{"resolved":true}\n')
        subprocess.run(["git", "-C", str(self.candidate), "add", str(path)], check=True)
        with self.assertRaises(runner.RunnerError):
            runner.release_conflict_admission(args, runner.controller_identity(self.controller))

    def test_conflict_admission_refuses_changed_index_stage_and_symlink(self):
        args, *_ = self.prepare_receipt_bound_conflict()
        blob = subprocess.check_output(
            ["git", "-C", str(self.candidate), "hash-object", "-w", "--stdin"],
            input=b"substituted stage\n").decode().strip()
        line = f"100644 {blob} 2\t.juno_task/managed-assets.json\n"
        subprocess.run(["git", "-C", str(self.candidate), "update-index", "--index-info"],
                       input=line, text=True, check=True)
        with self.assertRaises(runner.RunnerError):
            runner.release_conflict_admission(args, runner.controller_identity(self.controller))

    def test_conflict_admission_refuses_extra_conflict_and_target_drift(self):
        args, *_ = self.prepare_receipt_bound_conflict()
        stages = runner.git(self.candidate, "ls-files", "-u").splitlines()
        blobs = [line.split()[1] for line in stages[:3]]
        extra = "".join(f"100644 {blob} {stage}\textra.txt\n"
                        for stage, blob in enumerate(blobs, 1))
        subprocess.run(["git", "-C", str(self.candidate), "update-index", "--index-info"],
                       input=extra, text=True, check=True)
        (self.candidate / "extra.txt").write_text("extra conflict\n")
        with self.assertRaises(runner.RunnerError):
            runner.release_conflict_admission(args, runner.controller_identity(self.controller))

    def test_conflict_admission_refuses_symbolic_conflict_path(self):
        args, *_ = self.prepare_receipt_bound_conflict()
        path = self.candidate / ".juno_task/managed-assets.json"
        path.unlink(); path.symlink_to(self.candidate / "allowed.txt")
        with self.assertRaises(runner.RunnerError):
            runner.release_conflict_admission(args, runner.controller_identity(self.controller))

    def test_conflict_admission_refuses_protected_target_drift(self):
        args, *_ = self.prepare_receipt_bound_conflict()
        subprocess.run(["git", "-C", str(self.candidate), "update-ref", "refs/heads/target",
                        runner.git(self.candidate, "rev-parse", "HEAD")], check=True)
        with self.assertRaises(runner.RunnerError):
            runner.release_conflict_admission(args, runner.controller_identity(self.controller))

    def test_conflict_worker_hydrates_exact_lock_against_identical_unmerged_snapshot(self):
        scripts = self.controller / ".juno_task/scripts"; scripts.mkdir(parents=True)
        helper = scripts / "worktree_hydration.py"
        helper.write_bytes(RUNNER.with_name("worktree_hydration.py").read_bytes())
        helper.chmod(0o755)
        root = self.candidate / "juno-code"; root.mkdir()
        package = {"name": "fixture", "version": "1.0.0", "private": True}
        lock_body = {"name": "fixture", "version": "1.0.0", "lockfileVersion": 3,
                     "requires": True, "packages": {"": package}}
        (root / "package.json").write_text(json.dumps(package) + "\n")
        lock = root / "package-lock.json"; lock.write_text(json.dumps(lock_body) + "\n")
        (self.candidate / ".gitignore").write_text("juno-code/node_modules/\n")
        (self.candidate / "allowed.txt").write_text("ours\n")
        subprocess.run(["git", "-C", str(self.candidate), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.candidate), "-c", "user.name=T",
                        "-c", "user.email=t@t", "commit", "-m", "ours lock"],
                       check=True, stdout=subprocess.DEVNULL)
        ours = git(self.candidate, "rev-parse", "HEAD")
        subprocess.run(["git", "-C", str(self.candidate), "checkout", "-b", "hydration-theirs", "target"],
                       check=True, stdout=subprocess.DEVNULL)
        (self.candidate / "allowed.txt").write_text("theirs\n")
        subprocess.run(["git", "-C", str(self.candidate), "add", "allowed.txt"], check=True)
        subprocess.run(["git", "-C", str(self.candidate), "-c", "user.name=T",
                        "-c", "user.email=t@t", "commit", "-m", "theirs"],
                       check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "-C", str(self.candidate), "checkout", "task"],
                       check=True, stdout=subprocess.DEVNULL)
        merge = subprocess.run(["git", "-C", str(self.candidate), "merge", "--no-ff", "--no-commit",
                                "hydration-theirs"], capture_output=True, text=True)
        self.assertNotEqual(0, merge.returncode)
        snapshot = runner._conflict_checkout_snapshot(
            self.controller, self.candidate, "refs/heads/target")
        identity = {"admission_kind": "sealed_release_epoch_conflict",
                    "target_ref": "refs/heads/target", "conflict_checkout": snapshot,
                    "validation_root": {"cwd": "juno-code", "timeout_seconds": 30}}
        expectation = self.tmp / "conflict-expectation.json"
        first = runner.hydrate_conflict_validation_root(
            self.controller, self.candidate, identity, expectation)
        second = runner.hydrate_conflict_validation_root(
            self.controller, self.candidate, identity, expectation)
        self.assertEqual("passed", first["decision"])
        self.assertEqual(first["conflict_checkout_sha256"], second["conflict_checkout_sha256"])
        self.assertEqual(hashlib.sha256(lock.read_bytes()).hexdigest(), first["lock_sha256"])
        self.assertEqual(ours, git(self.candidate, "rev-parse", "HEAD"))
        self.assertEqual(["allowed.txt"], git(
            self.candidate, "diff", "--name-only", "--diff-filter=U").splitlines())
        before = runner.fingerprint(self.candidate)
        (self.candidate / "allowed.txt").write_text("tampered\n")
        tampered = (self.candidate / "allowed.txt").read_bytes()
        with self.assertRaisesRegex(runner.RunnerError, "hydration/probe"):
            runner.hydrate_conflict_validation_root(
                self.controller, self.candidate, identity, expectation)
        self.assertEqual(tampered, (self.candidate / "allowed.txt").read_bytes())
        self.assertEqual(before, runner.fingerprint(self.candidate))

    def test_worker_capture_recovery_binds_failed_run_without_model_rerun(self):
        ours = git(self.candidate, "rev-parse", "HEAD")
        subprocess.run(["git", "-C", str(self.candidate), "checkout", "-b", "recovery-theirs", "target"],
                       check=True, stdout=subprocess.DEVNULL)
        (self.candidate / "allowed.txt").write_text("theirs\n")
        subprocess.run(["git", "-C", str(self.candidate), "add", "allowed.txt"], check=True)
        subprocess.run(["git", "-C", str(self.candidate), "-c", "user.name=T", "-c", "user.email=t@t",
                        "commit", "-m", "theirs"], check=True, stdout=subprocess.DEVNULL)
        theirs = git(self.candidate, "rev-parse", "HEAD")
        subprocess.run(["git", "-C", str(self.candidate), "checkout", "task"],
                       check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "-C", str(self.candidate), "-c", "user.name=T", "-c", "user.email=t@t",
                        "merge", "--no-ff", "recovery-theirs", "-m", "recovered merge"],
                       check=True, stdout=subprocess.DEVNULL)

        source = self.tmp / "failed-worker"; source.mkdir()
        prompt = source / "prompt.md"; prompt.write_text("repair\n")
        compatible = source / "compatible-config.json"; compatible.write_text("{}\n")
        launcher = source / "launcher-root/.juno_task/config.json"
        launcher.parent.mkdir(parents=True); launcher.write_text("{}\n")
        stdout = source / "stdout.log"; stdout.write_text("worker completed\n")
        (source / "stderr.log").write_text("")
        (source / "combined.log").write_text("worker completed\n")
        metadata = source / "session_metadata"; metadata.mkdir()
        continuity = metadata / "session_continuity.v2.json"
        continuity.write_text(json.dumps({"version": 2, "scopes": {"S": {
            "active": "main", "branches": {"main": {"session_id": "session-recovery"}}}}}) + "\n")
        live = self.tmp / "immutable-live.log"
        live.write_text("worker completed\nsession-recovery\n")
        before = {"root": str(self.candidate.resolve()), "head": ours,
                  "branch_ref": "refs/heads/task",
                  "git_common_dir": str((self.candidate / git(self.candidate, "rev-parse", "--git-common-dir")).resolve()),
                  "status": "conflicted", "index_sha256": "1" * 64}
        identity = {"admission_kind": "sealed_release_epoch_conflict", "before": before,
                    "ours_sha": ours, "theirs_sha": theirs, "task_id": "T1",
                    "expected_paths": ["allowed.txt"]}
        argv = ["yy", "pi", "--no-hooks"]
        policy = {"schema_version": "juno_managed_hook_policy.v1",
                  "external_side_effects": "forbidden", "lifecycle_hooks": "disabled",
                  "enforcement": "yy_pi_no_hooks"}
        launch = {"schema_version": runner.SCHEMA, "mode": "worker", "identity": identity,
                  "agent_root": str(self.candidate.resolve()), "argv": argv,
                  "argv_sha256": runner.sha(runner.shlex.join(argv).encode()),
                  "prompt": runner.evidence(prompt),
                  "compatible_config": {"derived": runner.evidence(compatible)},
                  "launcher_config": runner.evidence(launcher), "effective_hook_policy": policy}
        runner.atomic_json(source / "launch.json", launch)
        terminal = {"schema_version": runner.SCHEMA, "state": "failed",
                    "semantic_outcome": "failed", "failure": "capture is missing or stale",
                    "exit_code": 0, "timed_out": False, "exit_signal": None,
                    "interrupted_signal": None, "termination_events": []}
        runner.atomic_json(source / "terminal.json", terminal)
        failed = {**terminal, "mode": "worker", "identity": identity,
                  "tool_id": "managed_agent_runner", "effective_hook_policy": policy,
                  "launch": runner.evidence(source / "launch.json"),
                  "live_log": runner.evidence(live)}
        runner.atomic_json(source / "receipt.json", failed)
        out = self.tmp / "recovered-worker"
        args = type("Args", (), {"failed_receipt": str(source / "receipt.json"),
                                  "out_dir": str(out)})()
        self.assertEqual(0, runner.recover_worker_capture(args))
        receipt = json.loads((out / "receipt.json").read_text())
        self.assertEqual(("succeeded", "receipt_bound_worker_recovery", "session-recovery"),
                         (receipt["state"], receipt["capture_source"], receipt["session_id"]))
        self.assertEqual("capture_only_no_model_rerun", receipt["recovery"]["kind"])
        self.assertEqual([ours, theirs], git(self.candidate, "show", "-s", "--format=%P").split())

        live.write_text("tampered\n")
        with self.assertRaisesRegex(runner.RunnerError, "live log artifact identity mismatch"):
            runner.recover_worker_capture(type("Args", (), {
                "failed_receipt": str(source / "receipt.json"),
                "out_dir": str(self.tmp / "tampered-recovery")})())

    def test_bound_review_canonicalizes_accepted_crlf_response_artifact(self):
        candidate_sha = git(self.candidate, "rev-parse", "HEAD")
        policy = risk.load_policy(RUNNER.parents[1] / "config/risk-policy.json")
        request = {"repository": str(self.candidate), "candidate_sha": candidate_sha,
                   "target_ref": "refs/heads/target",
                   "expected_target_sha": git(self.candidate, "rev-parse", "target")}
        plan = risk.classify(policy, request)
        binding_path = self.tmp / "crlf-binding.json"
        risk.write_review_binding(binding_path, candidate_sha=candidate_sha,
                                  policy_identity=plan["policy_identity"],
                                  reviewer="reviewer_a")
        prompt = self.tmp / "crlf.md"; prompt.write_text("fixture-crlf\n" + "large-secret-marker-" * 5000)
        out = self.tmp / "crlf-review"
        command = risk.reviewer_command(
            RUNNER.parent, controller_root=self.controller,
            controller_branch="refs/heads/controller", candidate_root=self.candidate,
            candidate_sha=candidate_sha, prompt_file=prompt, out_dir=out,
            reviewer="reviewer_a", task_id="T1", review_binding_path=binding_path,
        )
        result = subprocess.run(command, env=self.env(), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        response = (out / "response.txt").read_bytes()
        parsed = json.loads(response)
        self.assertEqual(runner.canonical(parsed), response)
        self.assertNotIn(b"\r", response)
        shipped_prompt = (out / "prompt.md").read_text()
        for instruction in ("code, severity, summary, paths", "low|medium|high|critical",
                            "scope_classification", "cited_contract", "rejection_counters",
                            "Scope admission precedes severity", "OUT OF SCOPE",
                            "PASS is allowed", "findings verdict", "no markdown", "at most 32"):
            self.assertIn(instruction, shipped_prompt)
        receipt_path = out / "receipt.json"
        self.assertLess(receipt_path.stat().st_size, policy["limits"]["max_receipt_bytes"])
        receipt_text = receipt_path.read_text()
        self.assertNotIn("large-secret-marker", receipt_text)
        self.assertNotIn('"echo"', receipt_text)
        compact = risk._compact_review(
            {"runner_receipt_path": str(receipt_path),
             "runner_receipt_sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest()},
            "reviewer_a", 1, candidate_sha, plan["policy_identity"], plan,
        )
        self.assertEqual("pass", compact["verdict"])

    def test_risk_reviewer_command_executes_canonical_a_then_bound_b(self):
        candidate_sha = git(self.candidate, "rev-parse", "HEAD")
        policy = risk.load_policy(RUNNER.parents[1] / "config/risk-policy.json")
        request = {"repository": str(self.candidate), "candidate_sha": candidate_sha,
                   "target_ref": "refs/heads/target",
                   "expected_target_sha": git(self.candidate, "rev-parse", "target")}
        plan = risk.classify(policy, request); policy_id = plan["policy_identity"]
        binding_a = self.tmp / "binding-a.json"
        risk.write_review_binding(binding_a, candidate_sha=candidate_sha,
                                  policy_identity=policy_id, reviewer="reviewer_a")
        out_a = self.tmp / "review-a"
        command_a = risk.reviewer_command(
            RUNNER.parent, controller_root=self.controller,
            controller_branch="refs/heads/controller", candidate_root=self.candidate,
            candidate_sha=candidate_sha, prompt_file=self.prompt, out_dir=out_a,
            reviewer="reviewer_a", task_id="T1", review_binding_path=binding_a,
        )
        first = subprocess.run(command_a, env=self.env(), capture_output=True, text=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        receipt_a = json.loads((out_a / "receipt.json").read_text())
        self.assertEqual(("reviewer_a", 1, None),
                         (receipt_a["review_binding"]["reviewer_role"],
                          receipt_a["review_binding"]["sequence"],
                          receipt_a["review_binding"]["predecessor"]))

        binding_b = self.tmp / "binding-b.json"
        risk.write_review_binding(binding_b, candidate_sha=candidate_sha,
                                  policy_identity=policy_id, reviewer="reviewer_b",
                                  predecessor_receipt=out_a / "receipt.json")
        out_b = self.tmp / "review-b"
        command_b = risk.reviewer_command(
            RUNNER.parent, controller_root=self.controller,
            controller_branch="refs/heads/controller", candidate_root=self.candidate,
            candidate_sha=candidate_sha, prompt_file=self.prompt, out_dir=out_b,
            reviewer="reviewer_b", task_id="T1", review_binding_path=binding_b,
        )
        second = subprocess.run(command_b, env=self.env(), capture_output=True, text=True)
        self.assertEqual(second.returncode, 0, second.stderr)
        receipt_b = json.loads((out_b / "receipt.json").read_text())
        predecessor = receipt_b["review_binding"]["predecessor"]
        self.assertEqual(hashlib.sha256((out_a / "receipt.json").read_bytes()).hexdigest(),
                         predecessor["receipt_sha256"])
        self.assertEqual(receipt_a["session_id"], predecessor["session_id"])
        self.assertNotEqual(receipt_a["tool_id"], receipt_b["tool_id"])
        review_inputs = []
        for out in (out_a, out_b):
            receipt_path = out / "receipt.json"
            review_inputs.append({"runner_receipt_path": str(receipt_path),
                                  "runner_receipt_sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest()})
        suite_path = (self.tmp / "full-suite.json").resolve()
        claim_path = (self.tmp / "full-suite-claim.json").resolve()
        token = "a" * 48
        identity = {"task_workspace_config_sha256": "1" * 64,
                    "full_suite_config_sha256": "2" * 64,
                    "task_validation_commands_sha256": "3" * 64}
        command = {"id": "full", "cwd": ".", "argv": ["test"],
                   "timeout_seconds": 60, "max_output_bytes": 4096}
        claim = {"schema_version": risk.FULL_SUITE_CLAIM_SCHEMA,
                 "producer": {"schema_version": risk.FULL_SUITE_PRODUCER_SCHEMA,
                              "tool_id": risk.FULL_SUITE_TOOL_ID}, "task_id": "T1",
                 "candidate": {"candidate_sha": plan["candidate"]["candidate_sha"],
                               "candidate_tree": plan["candidate"]["candidate_tree"]},
                 "policy_identity": plan["policy_identity"], "validation_identity": identity,
                 "command": command, "token": token, "attempt_number": 1,
                 "expected_receipt_path": str(suite_path)}
        claim_path.write_bytes(risk.canonical(claim))
        claim_ref = {"claim_path": str(claim_path),
                     "claim_sha256": hashlib.sha256(claim_path.read_bytes()).hexdigest()}
        suite = {"schema_version": risk.FULL_SUITE_SCHEMA,
                 "producer": {"schema_version": risk.FULL_SUITE_PRODUCER_SCHEMA,
                              "tool_id": risk.FULL_SUITE_TOOL_ID},
                 "candidate": {"candidate_sha": plan["candidate"]["candidate_sha"],
                               "candidate_tree": plan["candidate"]["candidate_tree"]},
                 "policy_identity": plan["policy_identity"],
                 "claim": {**claim_ref, "token": token, "attempt_number": 1},
                 "validation_identity": identity, "command": command,
                 "started_at": "2026-08-09T00:00:00Z",
                 "completed_at": "2026-08-09T00:00:01Z",
                 "timing": {"schema_version": "juno_validation_timing.v1",
                            "states": [{"state": name, "duration_ms": 1} for name in
                                       ("WAITING_FOR_RESOURCE", "SETUP", "RUNNING",
                                        "TEARDOWN", "PASSED")],
                            "wall_duration_ms": 5,
                            "critical_path_contribution_ms": 5},
                 "resource": {"id": "fixture", "lock_identity_sha256": None,
                              "wait_timeout_seconds": 1, "owner_diagnostics": None},
                 "identity": {"command_sha256": "0" * 64, "cwd_sha256": "0" * 64,
                              "policy_sha256": "0" * 64,
                              "candidate_sha": plan["candidate"]["candidate_sha"],
                              "candidate_tree": plan["candidate"]["candidate_tree"]},
                 "result": {"exit_code": 0, "timed_out": False,
                            "stdout": {"sha256": hashlib.sha256(b"").hexdigest(),
                                       "tail": "", "truncated_bytes": 0},
                            "stderr": {"sha256": hashlib.sha256(b"").hexdigest(),
                                       "tail": "", "truncated_bytes": 0}}}
        suite_path.write_bytes(risk.canonical(suite))
        suite_ref = {"receipt_path": str(suite_path),
                     "receipt_sha256": hashlib.sha256(suite_path.read_bytes()).hexdigest()}
        admission = {"schema_version": risk.FULL_SUITE_ADMISSION_SCHEMA,
                     "state": "COMPLETE", "attempt_number": 1, "token": token,
                     "claim": claim_ref, "receipt": suite_ref}
        evidence = risk.finalize(
            plan, request,
            affected_tests_passed=True, full_suite_admission=admission, reviews=review_inputs,
            metrics={"model_calls": 2}, policy=policy,
        )
        self.assertEqual("passed", evidence["status"])
        self.assertEqual([1, 2], [item["sequence"] for item in evidence["reviews"]])

    def test_reviewer_b_refuses_when_a_response_contains_findings(self):
        candidate_sha = git(self.candidate, "rev-parse", "HEAD")
        policy = risk.load_policy(RUNNER.parents[1] / "config/risk-policy.json")
        request = {"repository": str(self.candidate), "candidate_sha": candidate_sha,
                   "target_ref": "refs/heads/target",
                   "expected_target_sha": git(self.candidate, "rev-parse", "target")}
        plan = risk.classify(policy, request)
        finding_prompt = self.tmp / "finding.md"; finding_prompt.write_text("review-findings\n")
        binding_a = self.tmp / "finding-binding-a.json"
        risk.write_review_binding(binding_a, candidate_sha=candidate_sha,
                                  policy_identity=plan["policy_identity"],
                                  reviewer="reviewer_a")
        out_a = self.tmp / "finding-review-a"
        command_a = risk.reviewer_command(
            RUNNER.parent, controller_root=self.controller,
            controller_branch="refs/heads/controller", candidate_root=self.candidate,
            candidate_sha=candidate_sha, prompt_file=finding_prompt, out_dir=out_a,
            reviewer="reviewer_a", task_id="T1", review_binding_path=binding_a,
        )
        first = subprocess.run(command_a, env=self.env(), capture_output=True, text=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        response = json.loads((out_a / "response.txt").read_text())
        self.assertEqual("findings", response["verdict"])

        binding_b = self.tmp / "finding-binding-b.json"
        risk.write_review_binding(binding_b, candidate_sha=candidate_sha,
                                  policy_identity=plan["policy_identity"],
                                  reviewer="reviewer_b",
                                  predecessor_receipt=out_a / "receipt.json")
        out_b = self.tmp / "finding-review-b"
        command_b = risk.reviewer_command(
            RUNNER.parent, controller_root=self.controller,
            controller_branch="refs/heads/controller", candidate_root=self.candidate,
            candidate_sha=candidate_sha, prompt_file=self.prompt, out_dir=out_b,
            reviewer="reviewer_b", task_id="T1", review_binding_path=binding_b,
        )
        second = subprocess.run(command_b, env=self.env(), capture_output=True, text=True)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("no blocking finding", second.stderr)
        self.assertFalse((out_b / "launch.json").exists())

    def test_signal_is_forwarded_and_interruption_evidence_is_typed(self):
        prompt = self.tmp / "signal.md"; prompt.write_text("signal-wait")
        out = self.tmp / "signal-out"
        proc = subprocess.Popen(self.command(out, prompt), env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                if json.loads((out / "active.json").read_text()).get("process_group_id"): break
            except (OSError, json.JSONDecodeError): pass
            time.sleep(.01)
        proc.send_signal(signal.SIGINT); self.assertNotEqual(proc.wait(timeout=3), 0)
        assert proc.stdout and proc.stderr; proc.stdout.close(); proc.stderr.close()
        terminal = json.loads((out / "terminal.json").read_text())
        self.assertEqual(terminal["state"], "interrupted")
        self.assertEqual(terminal["interrupted_signal"], "SIGINT")
        self.assertFalse(terminal["timed_out"])
        self.assertLess(terminal["exit_code"], 0)
        self.assertEqual(hashlib.sha256(Path(terminal["live_log"]["path"]).read_bytes()).hexdigest(),
                         terminal["live_log"]["sha256"])
        self.assertFalse((out / "active.json").exists())

    def test_signal_escalates_across_child_and_grandchild_before_terminal_receipt(self):
        marker = self.candidate / "cancelled-agent-late-mutation"
        prompt = self.tmp / "orphan.md"; prompt.write_text(f"orphan-wait:{marker}\n")
        out = self.tmp / "orphan-out"
        proc = subprocess.Popen(self.command(out, prompt), env=self.env(),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        pids_path = Path(str(marker) + ".pids")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not pids_path.exists(): time.sleep(.01)
        self.assertTrue(pids_path.exists(), "adversarial descendants did not launch")
        descendant_pids = [int(value) for value in pids_path.read_text().split()]
        proc.terminate(); self.assertNotEqual(proc.wait(timeout=4), 0)
        assert proc.stdout and proc.stderr; proc.stdout.close(); proc.stderr.close()
        terminal = json.loads((out / "terminal.json").read_text())
        self.assertEqual(terminal["state"], "interrupted")
        self.assertEqual(terminal["interrupted_signal"], "SIGTERM")
        self.assertEqual(terminal["child_pid"], terminal["process_group_id"])
        self.assertTrue(any(event["signal"] == "SIGKILL" for event in terminal["termination_events"]))
        for child_pid in descendant_pids:
            with self.assertRaises(ProcessLookupError): os.kill(child_pid, 0)
        # Simulate a subsequent finish/merge gate. The cancelled producer's delayed
        # grandchild must be dead before this gate starts and cannot collide later.
        gate_started = time.monotonic(); time.sleep(1.35)
        self.assertFalse(marker.exists())
        self.assertLess(terminal["producer_elapsed_seconds"], time.monotonic() - gate_started + 4)
        footer = (out / "receipt.json").read_text()
        self.assertIn('"process_group_id":', footer)
        self.assertIn('"termination_events":', footer)

    def test_parent_output_pipe_loss_kills_owned_group_before_return(self):
        marker = self.candidate / "pipe-loss-late-mutation"
        prompt = self.tmp / "pipe-loss.md"; prompt.write_text(f"orphan-wait:{marker}\n")
        out = self.tmp / "pipe-loss-out"
        proc = subprocess.Popen(self.command(out, prompt), env=self.env(),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        pids_path = Path(str(marker) + ".pids")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not pids_path.exists(): time.sleep(.01)
        self.assertTrue(pids_path.exists())
        assert proc.stderr and proc.stdout
        proc.stderr.close()
        self.assertNotEqual(proc.wait(timeout=4), 0); proc.stdout.close()
        terminal = json.loads((out / "terminal.json").read_text())
        self.assertTrue(any(event["reason"] == "output_pipe_or_log_loss"
                            for event in terminal["termination_events"]))
        time.sleep(1.35)
        self.assertFalse(marker.exists())

    def test_worker_admission_and_changed_path_authority(self):
        common = str((self.candidate / git(self.candidate, "rev-parse", "--git-common-dir")).resolve())
        create = {"task_id":"T1", "worktree":str(self.candidate), "branch_ref":"refs/heads/task", "git_common_dir":common,
                  "expected_paths":["allowed.txt"], "workspace_manifest_identity":"m"}
        paths=[]
        for name, value in (("create", create), ("verify", {"passed":True,"task_id":"T1"}), ("edit", {"passed":True,"task_id":"T1"})):
            p=self.tmp/f"{name}.json"; p.write_text(json.dumps(value)); paths.append(p)
        out=self.tmp/"worker"; cmd=[sys.executable,str(RUNNER),"run","--external-side-effects","forbidden","--lifecycle-hooks","disabled","--mode","worker","--controller-root",str(self.controller),"--controller-branch","controller",
             "--agent-root",str(self.candidate),"--prompt-file",str(self.prompt),"--out-dir",str(out),"--task-id","T1",
             "--create-receipt",str(paths[0]),"--verify-receipt",str(paths[1]),"--edit-preflight-receipt",str(paths[2])]
        result=subprocess.run(cmd,env=self.env(),capture_output=True,text=True); self.assertEqual(result.returncode,0,result.stderr)
        receipt=json.loads((out/"receipt.json").read_text()); self.assertEqual(receipt["identity"]["unexpected_paths"],[])
        environment = receipt["environment_contract"]
        self.assertEqual(environment["workspace_role"], "task")
        self.assertIsNone(environment["worker_admission_kind"])

    def test_run_recovers_missing_worker_capture_after_exact_settlement(self):
        common = str((self.candidate / git(
            self.candidate, "rev-parse", "--git-common-dir")).resolve())
        before = git(self.candidate, "rev-parse", "HEAD")
        receipt_dir = self.tmp / "settled-run-receipts"; receipt_dir.mkdir()
        create = {"task_id": "T1", "worktree": str(self.candidate),
                  "branch_ref": "refs/heads/task", "git_common_dir": common,
                  "expected_paths": ["allowed.txt"], "workspace_manifest_identity": "m"}
        values = (("create.json", create),
                  ("verify.json", {"passed": True, "task_id": "T1"}),
                  ("edit.json", {"passed": True, "task_id": "T1"}))
        paths = []
        for name, value in values:
            path = receipt_dir / name; runner.atomic_json(path, value); paths.append(path)
        prompt = self.tmp / "settled-worker-recovery.md"
        prompt.write_text("settled-worker-recovery\n")
        out = self.tmp / "settled-run"
        command = [sys.executable, str(RUNNER), "run", "--mode", "worker",
                   "--controller-root", str(self.controller), "--controller-branch", "controller",
                   "--agent-root", str(self.candidate), "--prompt-file", str(prompt),
                   "--out-dir", str(out), "--tool-id", "yy_task_implementation",
                   "--task-id", "T1", "--create-receipt", str(paths[0]),
                   "--verify-receipt", str(paths[1]), "--edit-preflight-receipt", str(paths[2]),
                   "--require-terminal-result", "--external-side-effects", "forbidden",
                   "--lifecycle-hooks", "disabled"]
        completed = subprocess.run(command, env=self.env(), capture_output=True, text=True)
        self.assertEqual(0, completed.returncode, completed.stderr)
        receipt = json.loads((out / "receipt.json").read_text())
        self.assertEqual("settled_worker_recovery", receipt["capture_source"])
        self.assertEqual("completed", receipt["terminal_result"]["state"])
        self.assertEqual("session-settled-run", receipt["session_id"])
        self.assertEqual(1, int(git(self.candidate, "rev-list", "--count",
                                    f"{before}..HEAD")))
        self.assertFalse(git(self.candidate, "status", "--porcelain=v1"))
        terminal = json.loads((out / "capture-terminal.json").read_text())
        self.assertEqual(receipt["artifacts"]["capture"]["sha256"],
                         terminal["capture_sha256"])
        self.assertEqual(receipt["artifacts"]["capture_terminal"]["path"],
                         str((out / "capture-terminal.json").resolve()))

    def test_historical_creation_worker_requires_exact_receipt_and_workspace_identity(self):
        subprocess.run(["git", "-C", str(self.candidate), "config", "extensions.worktreeConfig", "true"], check=True)
        workspace = {"role": "task", "taskId": "T1", "manifestIdentity": "manifest-1",
                     "createReceiptSha256": "1" * 64}
        # Historical authority binds creation order, which may be intentionally
        # non-lexical when managed outputs were appended after initial scope.
        expected = ["z-appended-managed-output.txt", "allowed.txt"]
        paths_sha = hashlib.sha256(json.dumps(expected, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest()
        workspace["expectedPathsSha256"] = paths_sha
        for key, value in workspace.items():
            subprocess.run(["git", "-C", str(self.candidate), "config", "--worktree",
                            f"juno.workspace.{key}", value], check=True)
        common = str((self.candidate / git(self.candidate, "rev-parse", "--git-common-dir")).resolve())
        tip = git(self.candidate, "rev-parse", "HEAD")
        receipt_dir = self.tmp / "historical-receipts"; receipt_dir.mkdir()
        create = {"schema_version": "juno_managed_task_run_create.v1", "task_id": "T1",
                  "worktree": str(self.candidate.resolve()), "branch_ref": "refs/heads/task",
                  "git_common_dir": common, "expected_paths": expected,
                  "expected_paths_sha256": paths_sha, "admission_kind": "historical_creation",
                  "workspace_role": "task", "clean_tip_sha": tip,
                  "creation_receipt_sha256": "1" * 64,
                  "workspace_manifest_identity": "manifest-1"}
        create_path = receipt_dir / "create-receipt.json"; runner.atomic_json(create_path, create)
        verify = {"schema_version": "juno_managed_task_run_verify.v1", "task_id": "T1",
                  "passed": True, "workspace_role": "task", "tip_sha": tip,
                  "create_receipt_sha256": hashlib.sha256(create_path.read_bytes()).hexdigest(),
                  "hydration_manifest_sha256": "2" * 64, "dependency_evidence": []}
        verify_path = receipt_dir / "verify-receipt.json"; runner.atomic_json(verify_path, verify)
        edit = {"schema_version": "juno_managed_task_run_edit_preflight.v1", "task_id": "T1",
                "passed": True, "workspace_role": "task", "tip_sha": tip,
                "create_receipt_sha256": hashlib.sha256(create_path.read_bytes()).hexdigest(),
                "verify_receipt_sha256": hashlib.sha256(verify_path.read_bytes()).hexdigest(),
                "allowed_paths_sha256": paths_sha}
        edit_path = receipt_dir / "edit-preflight-receipt.json"; runner.atomic_json(edit_path, edit)
        receipt_paths = (create_path, verify_path, edit_path)
        args = runner.argparse.Namespace(task_id="T1", create_receipt=str(create_path),
            verify_receipt=str(verify_path), edit_preflight_receipt=str(edit_path),
            candidate_sha=None, agent_root=str(self.candidate))
        identity, _mark = runner.validate_worker(args, runner.controller_identity(self.controller))
        self.assertEqual(identity["admission_kind"], "historical_creation")
        out = self.tmp / "historical-worker"
        command = [sys.executable, str(RUNNER), "run", "--mode", "worker",
                   "--controller-root", str(self.controller), "--controller-branch", "controller",
                   "--agent-root", str(self.candidate), "--prompt-file", str(self.prompt),
                   "--out-dir", str(out), "--task-id", "T1", "--create-receipt", str(create_path),
                   "--verify-receipt", str(verify_path), "--edit-preflight-receipt", str(edit_path),
                   "--external-side-effects", "forbidden", "--lifecycle-hooks", "disabled"]
        launched = subprocess.run(command, env=self.env(), capture_output=True, text=True)
        self.assertEqual(launched.returncode, 0, launched.stderr)
        launch = json.loads((out / "launch.json").read_text())
        self.assertEqual(launch["environment_contract"]["workspace_role"], "task")
        self.assertEqual(launch["identity"]["admission_kind"], "historical_creation")

        original = create_path.read_bytes()
        for field, value, message in (
                ("task_id", "WRONG", "identity mismatch"),
                ("worktree", str(self.tmp / "wrong"), "identity mismatch"),
                ("branch_ref", "refs/heads/wrong", "branch/common"),
                ("git_common_dir", str(self.tmp), "branch/common"),
                ("admission_kind", "arbitrary", "unsupported"),
                ("admission_supersession_sha256", "4" * 64, "creation authority"),
                ("expected_paths_sha256", "3" * 64, "creation authority"),
                ("workspace_role", "controller", "creation authority")):
            changed = dict(create); changed[field] = value; runner.atomic_json(create_path, changed)
            before = {path: path.read_bytes() for path in receipt_paths}
            with self.assertRaisesRegex(runner.RunnerError, message):
                runner.validate_worker(args, runner.controller_identity(self.controller))
            self.assertEqual({path: path.read_bytes() for path in receipt_paths}, before)
            create_path.write_bytes(original)
        for changed_paths, message in (
                (expected[:-1], "creation authority"),
                ([*expected, expected[-1]], "path admission is malformed"),
                (list(reversed(expected)), "creation authority")):
            changed = dict(create); changed["expected_paths"] = changed_paths
            runner.atomic_json(create_path, changed)
            before = {path: path.read_bytes() for path in receipt_paths}
            with self.assertRaisesRegex(runner.RunnerError, message):
                runner.validate_worker(args, runner.controller_identity(self.controller))
            self.assertEqual({path: path.read_bytes() for path in receipt_paths}, before)
            create_path.write_bytes(original)
        verify_path.write_text(json.dumps(verify, indent=2) + "\n")
        with self.assertRaisesRegex(runner.RunnerError, "bytes are not canonical"):
            runner.validate_worker(args, runner.controller_identity(self.controller))

    def test_conflict_worker_role_is_bound_to_validated_admission(self):
        args = runner.argparse.Namespace(
            mode="worker", controller_root=str(self.controller), controller_branch="controller",
            agent_root=str(self.candidate), task_id="T1", tool_id="managed_agent_runner",
            authority_map=None)
        node = {"executable": "/managed/node", "version": "22.0.0"}
        with mock.patch.object(runner, "managed_node_contract", return_value=(node, "/managed/bin")):
            ordinary_env, ordinary_contract = runner.clean_environment(
                args, self.tmp / "ordinary-capture", self.tmp / "ordinary-metadata",
                identity={"task_id": "T1"})
            conflict_env, conflict_contract = runner.clean_environment(
                args, self.tmp / "conflict-capture", self.tmp / "conflict-metadata",
                identity={"task_id": "T1", "admission_kind": "sealed_release_epoch_conflict"})
            with self.assertRaisesRegex(runner.RunnerError, "unsupported admission kind"):
                runner.clean_environment(
                    args, self.tmp / "bad-capture", self.tmp / "bad-metadata",
                    identity={"task_id": "T1", "admission_kind": "unfenced_conflict"})
        self.assertEqual(ordinary_env["JUNO_WORKSPACE_ROLE"], "task")
        self.assertEqual(ordinary_contract["workspace_role"], "task")
        self.assertIsNone(ordinary_contract["worker_admission_kind"])
        self.assertEqual(conflict_env["JUNO_WORKSPACE_ROLE"], "controller")
        self.assertEqual(conflict_contract["workspace_role"], "controller")
        self.assertEqual(conflict_contract["worker_admission_kind"],
                         "sealed_release_epoch_conflict")

    def test_missing_traversal_and_configured_file_drift_are_refused(self):
        config_path = self.controller / ".juno_task/config.json"
        original = json.loads(config_path.read_text())
        for bad_path in ("missing.env", "../controller.env"):
            config = dict(original); config["envFilePath"] = bad_path
            config_path.write_text(json.dumps(config) + "\n")
            subprocess.run(["git", "-C", str(self.controller), "add", str(config_path)], check=True)
            subprocess.run(["git", "-C", str(self.controller), "-c", "user.name=T", "-c", "user.email=t@t", "commit", "-m", "bad fixture"], check=True, stdout=subprocess.DEVNULL)
            result = subprocess.run(self.command(self.tmp / ("bad-" + bad_path.replace("/", "_"))), env=self.env(), capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0); self.assertIn("envFilePath", result.stderr)
            config_path.write_text(json.dumps(original) + "\n")
            subprocess.run(["git", "-C", str(self.controller), "add", str(config_path)], check=True)
            subprocess.run(["git", "-C", str(self.controller), "-c", "user.name=T", "-c", "user.email=t@t", "commit", "-m", "restore fixture"], check=True, stdout=subprocess.DEVNULL)
        out = self.tmp / "source-drift"
        proc = subprocess.Popen(self.command(out), env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not (out / "stdout.log").exists(): time.sleep(.01)
        while time.monotonic() < deadline and b"out-before" not in (out / "stdout.log").read_bytes(): time.sleep(.01)
        self.env_target.write_text("MANAGED_TEST_SECRET=changed\n")
        self.assertNotEqual(proc.wait(timeout=3), 0)
        assert proc.stdout and proc.stderr; proc.stdout.close(); proc.stderr.close()
        terminal = json.loads((out / "terminal.json").read_text())
        self.assertIn("identity drifted", terminal["failure"])

    def test_real_config_loader_and_public_yy_help_accept_neutral_root(self):
        out = self.tmp / "real-config-smoke"
        result = subprocess.run(self.command(out), env=self.env(), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        config_path = json.loads((out / "launch.json").read_text())["compatible_config"]["derived"]["path"]
        smoke_env = {k:v for k,v in os.environ.items() if not k.startswith(("PI_", "JUNO_")) and k != "TASK_ROOT"}
        smoke_env["YYLO_PROJECT_BOOTSTRAP_WRITES"] = "0"
        repository = RUNNER.parents[2]
        tsx = repository / "juno-code/node_modules/.bin/tsx"
        loader = repository / "juno-code/src/core/config.ts"
        if tsx.is_file() and loader.is_file():
            script = ("import {loadConfig} from " + json.dumps(str(loader)) + ";"
                      "void(async()=>{const c=await loadConfig({baseDir:process.argv[2],configFile:process.argv[1]});"
                      "if(c.defaultModel!==':configured'||c.promptMacros.global.reflect!=='controller reflect\\n'||!c.envFilePath.startsWith('/'))process.exit(81);})().catch(e=>{console.error(e);process.exit(82)});")
            loaded = subprocess.run([str(tsx), "--eval", script, config_path, str(out / "agent-root")],
                                    cwd=out / "launcher-root", env=smoke_env, stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(loaded.returncode, 0, loaded.stderr)
        real_path = os.pathsep.join(x for x in os.environ["PATH"].split(os.pathsep) if Path(x).resolve() != self.bin.resolve())
        real_yy = shutil.which("yy", path=real_path)
        if real_yy:
            smoke = subprocess.run([real_yy, "pi", "--config", config_path, "-w", str(out / "agent-root"), "--help"],
                                   cwd=out / "launcher-root", env=smoke_env, stdin=subprocess.DEVNULL,
                                   capture_output=True, text=True, timeout=30)
            self.assertEqual(smoke.returncode, 0, smoke.stderr)
            self.assertNotIn("failed to read path", smoke.stderr.lower())

    def test_prompt_and_candidate_drift_refused(self):
        out=self.tmp/"drift"; (self.candidate/"dirty").write_text("x")
        result=subprocess.run(self.command(out),env=self.env(),capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0); self.assertFalse((out/"launch.json").exists())


if __name__ == "__main__": unittest.main()
