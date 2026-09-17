#!/usr/bin/env python3
"""Offline pristine packed-CLI lifecycle against representative predecessor data.

Predecessors are explicit fixture artifacts derived from the candidate, not
purported copies of published historical packages. Ledger is a deterministic
external process adapter; controller scripts and the candidate package stay exact.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import time
import subprocess
import sys
import tarfile

PACKAGE = Path(__file__).resolve().parents[4]
SCRIPTS = PACKAGE / 'src/templates/scripts'
sys.path[:0] = [str(SCRIPTS / 'tests'), str(SCRIPTS), str(PACKAGE / 'src/templates/maintenance')]
import controller_generation_migration as migration
from test_task_workspace import TaskWorkspaceFixture, FAKE_KANBAN_SOURCE, task_runtime


def write(file, value):
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(value if isinstance(value, bytes) else migration.encoded(value))


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True, stderr=subprocess.PIPE).strip()


def scenario(artifact, historical=False):
    fixture = TaskWorkspaceFixture()
    fixture._build_hermetic_fixture()
    try:
        root, controller = fixture.root, fixture.controller
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(('JUNO_', 'YYLO_', 'GIT_', 'PI_', 'OPENAI_', 'ANTHROPIC_'))
               and not any(word in k for word in ('TOKEN', 'SECRET', 'PASSWORD', 'API_KEY'))}
        env['npm_config_cache'] = os.environ.get('npm_config_cache', str(Path.home() / '.npm'))
        env['HOME'] = str(root / 'home')
        (root / 'home').mkdir()
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        subprocess.run(['npm', 'install', '--offline', '--ignore-scripts', '--no-audit', '--no-fund',
                        *([] if historical else ['--global']), '--prefix', str(root / 'installed'), str(artifact)],
                       env=env, check=True, capture_output=True, text=True)
        candidate_root = root / ('installed/node_modules/@yylo/cli' if historical else 'installed/lib/node_modules/@yylo/cli')
        candidate_bin = root / ('installed/node_modules/.bin' if historical else 'installed/bin')
        if not historical:
            assert not (candidate_root.parents[1] / '.package-lock.json').exists(), 'global case must exercise missing npm lock'
        candidate = {'root': str(candidate_root), 'artifact': str(artifact), 'sha256': migration.digest(artifact.read_bytes())}
        # The pristine candidate uses npm receipt/cache discovery, not injected evidence.
        migration.authenticate(candidate)
        old_root = root / 'previous'
        shutil.copytree(candidate_root, old_root)
        package = json.loads((old_root / 'package.json').read_text())
        if historical:
            package.update(name='juno-code', version='2.1.3-rc.0.32')
            write(old_root / 'package.json', package)
        declaration = old_root / 'dist/templates/managed-assets.json'
        definition = json.loads(declaration.read_text())
        definition['schemaVersion'] = 1
        definition.pop('instructionBundle', None)
        write(declaration, definition)
        instructions = old_root / 'dist/templates/controller-agent/AGENTS.md'
        instructions.write_bytes(instructions.read_bytes() + b'\nRepresentative predecessor guidance.\n')
        old_tar = root / 'previous.tgz'
        with tarfile.open(old_tar, 'w:gz') as archive:
            for file in sorted(old_root.rglob('*')):
                if file.is_file(): archive.add(file, arcname='package/' + file.relative_to(old_root).as_posix(), recursive=False)
        previous = {'root': str(old_root), 'artifact': str(old_tar), 'sha256': migration.digest(old_tar.read_bytes())}
        write(old_root / '.yylo-generation-evidence.json', previous)
        old = migration.authenticate(previous)
        inventory = {'schemaVersion': 1, 'packageName': package['name'], 'packageVersion': package['version'], 'assets': {}}
        for relative, item in migration.assets(old).items():
            write(controller / relative, item['data'])
            if relative.endswith(('.sh', '.py')): (controller / relative).chmod(0o755)
            sha = migration.digest(item['data'])
            inventory['assets'][relative] = {'type': item['type'], 'templateVersion': package['version'],
                                             'sourceSha256': sha, 'installedSha256': sha}
        write(controller / migration.INVENTORY, inventory)
        write(controller / migration.IDENTITY, migration.runtime_identity(old))
        policy = json.loads((controller / migration.POLICY).read_text())
        policy['runtime']['package'] = package['name']
        write(controller / migration.POLICY, policy)
        git(controller, 'config', '--local', 'juno.controller.branch', 'refs/heads/controller')
        git(controller, 'config', '--worktree', 'juno.controller.runtimeExecutable', old['executable'])
        git(controller, 'config', '--worktree', 'juno.controller.runtimeVersion', package['version'])
        # Actual canonical board wrapper, deterministic external Ledger process.
        version_policy = (candidate_root / 'dist/templates/scripts/juno-toolchain-policy.sh').read_text()
        ledger_version = re.search(r"YYLO_LEDGER_COMPAT_RANGE='([^']+)'", version_policy)[1]
        subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(controller / '.venv_juno')], check=True, env=env)
        ledger = controller / '.venv_juno/bin/yylo-ledger'
        write(ledger, FAKE_KANBAN_SOURCE.replace('@BOARD@', repr(str(fixture.board)))
              .replace('yylo-ledger 2.0.5', 'yylo-ledger ' + ledger_version).encode())
        ledger.chmod(0o755)
        env['VIRTUAL_ENV'] = str(controller / '.venv_juno')
        env['PATH'] = str(ledger.parent) + os.pathsep + env['PATH']
        with (controller / '.gitignore').open('a') as out: out.write('/.venv_juno/\n')
        git(controller, 'add', '.gitignore', '.juno_task')
        git(controller, 'commit', '-m', 'exact representative predecessor controller')
        if historical:
            # A real consumer target, not a second Juno source-controller case:
            # preserve its historical tracked runtime/inventory and never repair
            # or move the product ref as part of controller generation migration.
            git(fixture.repository, 'rm', 'juno-code/package.json',
                'juno-code/src/templates/scripts/task_workspace.py')
            runtime_path = '.juno_task/scripts/task_workspace.py'
            sha = migration.digest((fixture.repository / runtime_path).read_bytes())
            write(fixture.repository / migration.INVENTORY, {
                'schemaVersion': 1, 'packageName': 'juno-code', 'packageVersion': '2.1.3-rc.0.32',
                'assets': {runtime_path: {'type': 'script', 'templateVersion': '2.1.3-rc.0.32',
                                         'sourceSha256': sha, 'installedSha256': sha}}})
            git(fixture.repository, 'add', migration.INVENTORY)
            git(fixture.repository, 'commit', '-m', 'representative historical consumer target')
        else:
            declaration = fixture.repository / 'juno-code/src/templates/managed-assets.json'
            value = json.loads(declaration.read_text())
            value.update(schemaVersion=2, instructionBundle={
                'schemaVersion': 'juno_instruction_bundle_declaration.v1', 'semanticVersion': '1.42.3'})
            write(declaration, value)
            git(fixture.repository, 'add', 'juno-code/src/templates/managed-assets.json')
            git(fixture.repository, 'commit', '-m', 'future compatible source declaration')
        workflow = fixture.repository / '.juno_task/config/worktree-hydration.yaml'
        write(workflow, {'schema_version': 'v1', 'workflow_id': 'controller-upgrade-acceptance',
                         'workflow_class': 'task_hydration', 'steps': [{
                             'id': 'ready', 'name': 'Offline fixture hydration', 'probe': ['true'],
                             'command': ['true'], 'timeout_seconds': 30, 'fail_workflow': True,
                             'non_interactive': True, 'network': False, 'sensitive': False, 'outputs': []}]})
        git(fixture.repository, 'add', '.juno_task/config/worktree-hydration.yaml')
        git(fixture.repository, 'commit', '-m', 'frozen offline task hydration')
        target = git(fixture.repository, 'rev-parse', 'product')
        git(fixture.repository, 'checkout', '--detach')
        write(controller / 's1034-out.txt', b'unrelated output\x00preserved')
        write(controller / '.pi/skills/independent/SKILL.md', b'independent custom skill')
        executable = candidate_root / 'dist/bin/cli.mjs'

        def invoke(*args):
            command = ['node', str(executable)] if historical else [str(candidate_bin / 'yy')]
            result = subprocess.run([*command, *args], cwd=controller,
                                    env={**env, 'PATH': str(candidate_bin) + os.pathsep + env['PATH']},
                                    capture_output=True, text=True, timeout=180)
            if result.returncode:
                try:
                    bound = json.loads((controller / migration.CURRENT).read_text())['candidate']
                    probe = migration.plan(controller, bound, bound)
                    changes = [name for name, value in probe['after'].items() if name != migration.CURRENT and probe['before'].get(name) != value]
                    print('Post-dispatch differing paths:', changes, file=sys.stderr)
                except Exception as error:
                    print('Post-dispatch readback:', str(error), file=sys.stderr)
                for log in (controller / '.juno_task/runtime/task-hydration').rglob('lint.stderr'):
                    print(log.name, log.read_text()[-2000:], file=sys.stderr)
                raise AssertionError(f'{args}: exit {result.returncode}\n{result.stdout[-5000:]}\n{result.stderr[-5000:]}')
            rows = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
            return rows[-1] if rows else None

        before = (controller / migration.INVENTORY).read_bytes()
        assert invoke('scripts', 'generation', 'doctor')['disposition'] == 'migration_required'
        assert (controller / migration.INVENTORY).read_bytes() == before
        task = invoke('task', 'start', 'X')
        assert task['state'] == 'WORKING' and task['hydration']['status'] == 'passed'
        assert git(Path(task['worktree']), 'status', '--porcelain') == ''
        assert invoke('scripts', 'generation', 'doctor')['disposition'] == 'ready'
        assert invoke('scripts', 'doctor')['disposition'] == 'ready'
        assert invoke('task', 'hydrate', 'X', '--lease-token', task['lease_token'])['hydration']['status'] == 'passed'
        # Explicit hydrate selects packaged modules. A subsequent admission must
        # still authenticate the untouched installation (no generated pyc).
        assert invoke('scripts', 'generation', 'doctor')['disposition'] == 'ready'
        assert invoke('integration', 'runtime-doctor')['disposition'] == 'ready'
        assert git(controller, 'rev-parse', 'product') == target
        fixture.commit_task('X')
        finished = invoke('task', 'finish', 'X', '--lease-token', task['lease_token'])
        assert finished['state'] == 'QUEUED'
        landed = invoke('merge', 'land', 'X')
        assert landed['outcome'] == 'GIT_INTEGRATED'
        assert landed['ledger']['board_status'] == 'done'
        assert git(controller, 'show', 'product:src/feature.txt') == 'X'
        assert (controller / 's1034-out.txt').read_bytes() == b'unrelated output\x00preserved'
        assert (controller / '.pi/skills/independent/SKILL.md').read_bytes() == b'independent custom skill'
        if historical:
            # Activation retains an immutable executable. Tamper that *active*
            # dependency, not the now-unused mutable npm installation.
            active_root = Path(json.loads((controller / migration.CURRENT).read_text())['candidate']['root'])
            assert active_root != candidate_root
            dependency = active_root / 'dist/templates/scripts/metadata_controller.py'
            original = dependency.read_bytes()
            marker = root / 'untrusted-import-ran'
            try:
                dependency.write_bytes(f'from pathlib import Path\nPath({str(marker)!r}).write_text("unsafe")\n'.encode())
                try:
                    task_runtime.controller_generation_admission(controller, controller)
                    raise AssertionError('tampered dependency was admitted')
                except task_runtime.TaskWorkspaceError as error:
                    assert 'provenance' in str(error), str(error)
                assert not marker.exists(), 'untrusted installed dependency was executed before authentication'
            finally:
                dependency.write_bytes(original)
        agent_checks = None
        if not historical:
            fake_pi = root / 'tools/pi'
            write(fake_pi, b'''#!/usr/bin/env python3
import json, os, pathlib, sys, time
if '--version' in sys.argv:
    print('pi 0.60.0'); raise SystemExit(0)
if os.environ.get('FIXTURE_AGENT_FAILURE'):
    with pathlib.Path('.gitignore').open('a') as out: out.write('\\n# preserve deterministic agent dirt\\n')
    print('FIXTURE_PRIMARY_ERROR', file=sys.stderr)
    raise SystemExit(7)
if os.environ.get('FIXTURE_AGENT_WAIT'):
    ready = pathlib.Path(os.environ['FIXTURE_AGENT_WAIT']); ready.write_text('ready')
    while not ready.with_suffix('.release').exists(): time.sleep(0.05)
text = 'FIXTURE_PRIMARY_SUCCESS'
if os.environ.get('JUNO_REVIEW_BINDING_JSON'):
    binding = json.loads(os.environ['JUNO_REVIEW_BINDING_JSON'])
    text = json.dumps({**{key: binding[key] for key in ('candidate_sha', 'policy_identity', 'reviewer_role', 'sequence')},
        'schema_version': 'juno_managed_review_result.v3', 'verdict': 'pass', 'truncated': False,
        'omitted_finding_count': 0, 'rejection_counters': {}, 'findings': []})
message = {'role': 'assistant', 'content': [{'type': 'text', 'text': text}],
           'stopReason': 'stop', 'usage': {'input': 1, 'output': 1, 'totalTokens': 2, 'cost': {'total': 0}}}
print(json.dumps({'type': 'session', 'id': 'fixture-session'}))
print(json.dumps({'type': 'agent_end', 'messages': [message]}))
''')
            fake_pi.chmod(0o755)
            agent_env = {**env, 'PATH': str(fake_pi.parent) + os.pathsep + env['PATH']}
            agent_argv = ['node', str(executable), 'pi', '-p', 'deterministic offline fixture']
            ready = root / 'agent-ready'
            child = subprocess.Popen(agent_argv, cwd=controller, env={**agent_env, 'FIXTURE_AGENT_WAIT': str(ready)},
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
            try:
                deadline = time.monotonic() + 90
                while not ready.exists() and child.poll() is None and time.monotonic() < deadline: time.sleep(0.05)
                assert ready.exists(), 'deterministic agent never entered execution'
                request = root / 'competing-migration.json'
                write(request, {})
                writer = subprocess.run([sys.executable, '-I', '-B',
                    str(candidate_root / 'dist/templates/maintenance/controller_generation_migration.py'),
                    'apply', '--controller', str(controller), '--request', str(request)],
                    env=env, capture_output=True, text=True, timeout=30)
                assert writer.returncode == 2 and 'generation_migration_busy' in writer.stdout, writer.stdout + writer.stderr
                ready.with_suffix('.release').write_text('continue')
                success_stdout, success_stderr = child.communicate(timeout=90)
                assert child.returncode == 0 and 'FIXTURE_PRIMARY_SUCCESS' in success_stdout, success_stdout[-3000:] + success_stderr[-3000:]
            finally:
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.communicate(timeout=10)
            # Review launch requires a clean controller; exercise it separately
            # without hiding or removing the other fixture's unrelated output.
            review_fixture = TaskWorkspaceFixture()
            review_fixture._build_hermetic_fixture()
            review_root = root / 'managed-review'
            review_binding = root / 'review-binding.json'
            binding = {'schema_version': 'juno_managed_review_binding.v1',
                'candidate_sha': git(review_fixture.repository, 'rev-parse', 'HEAD'), 'policy_identity': 'a' * 64,
                'reviewer_role': 'reviewer_a', 'sequence': 1, 'predecessor': None}
            write(review_binding, (json.dumps(binding, sort_keys=True, separators=(',', ':')) + '\n').encode())
            review_prompt = root / 'review.md'; write(review_prompt, b'Deterministic offline review fixture.\n')
            review = subprocess.run([sys.executable, str(candidate_root / 'dist/templates/scripts/managed_agent_runner.py'),
                'run', '--mode', 'reviewer', '--controller-root', str(review_fixture.controller),
                '--controller-branch', git(review_fixture.controller, 'symbolic-ref', 'HEAD'),
                '--agent-root', str(review_root / 'agent-root'), '--candidate-root', str(review_fixture.repository),
                '--candidate-sha', binding['candidate_sha'], '--prompt-file', str(review_prompt),
                '--out-dir', str(review_root), '--tool-id', 'fixture_review', '--review-binding', str(review_binding),
                '--external-side-effects', 'forbidden', '--lifecycle-hooks', 'disabled',
                '--timeout-seconds', '90'], cwd=root, env={**agent_env,
                    'PATH': str(fake_pi.parent) + os.pathsep + str(candidate_bin) + os.pathsep + env['PATH']},
                capture_output=True, text=True, timeout=120)
            review_fixture.tearDown()
            assert review.returncode == 0, review.stdout[-4000:] + review.stderr[-4000:]
            failed = subprocess.run(agent_argv, cwd=controller, env={**agent_env, 'FIXTURE_AGENT_FAILURE': '1'},
                                    capture_output=True, text=True, timeout=180)
            assert failed.returncode != 0 and 'FIXTURE_PRIMARY_ERROR' in failed.stdout + failed.stderr, failed.stdout[-3000:] + failed.stderr[-3000:]
            assert 'Controller checkpoint failed' in failed.stderr, failed.stderr[-3000:]
            baseline = subprocess.run(agent_argv, cwd=root / 'home', env={**agent_env, 'FIXTURE_AGENT_FAILURE': '1'},
                                      capture_output=True, text=True, timeout=180)
            assert baseline.returncode == failed.returncode and 'FIXTURE_PRIMARY_ERROR' in baseline.stdout + baseline.stderr
            assert 'Controller checkpoint failed' not in baseline.stderr
            assert 'preserve deterministic agent dirt' in (controller / '.gitignore').read_text()
            agent_checks = {'success_exit': child.returncode, 'failure_exit': failed.returncode,
                            'primary_error': 'preserved', 'secondary_checkpoint': 'warning', 'dirty_bytes': 'preserved',
                            'concurrent_writer': 'fenced', 'neutral_reviewer': 'passed'}
        migration.authenticate(candidate)  # no package-runtime/template edits
        return {'profile': 'historical-consumer-shape' if historical else 'source-controller',
                'predecessor_kind': 'representative-fixture', 'previous_sha256': previous['sha256'],
                'candidate_sha256': candidate['sha256'], 'managed_asset_count': len(migration.assets(migration.authenticate(candidate))),
                'migration': 'automatic', 'doctor': 'ready', 'hydration': 'passed', 'finish': 'QUEUED',
                'native_merge': landed['outcome'], 'independent_bytes': 'preserved', 'candidate_package': 'unchanged', 'agent_checks': agent_checks}
    finally:
        fixture.tearDown()


def main():
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument('--artifact', type=Path, required=True)
    args = parser.parse_args()
    results = [scenario(args.artifact.resolve()), scenario(args.artifact.resolve(), historical=True)]
    print(json.dumps({'schema_version': 'yylo_controller_upgrade_acceptance.v1', 'outcome': 'passed', 'scenarios': results}))


if __name__ == '__main__': main()
