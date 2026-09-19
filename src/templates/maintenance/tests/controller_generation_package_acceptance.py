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
import platform
import shlex
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


def public_launcher_checks(root, controller, candidate_root, candidate_bin, old_root, env, historical):
    """Probe actual installed wrappers; no provider/model or candidate byte edits."""
    bins = json.loads((candidate_root / 'package.json').read_bytes())['bin']
    roles = {'yy': 'generation-admission', 'yylo': 'generation-admission',
             'ypl': 'live-generation-admission', 'feedback-yylo': 'auxiliary-artifact-mapping'}
    assert set(bins) == set(roles), 'Every new package bin needs an explicit safe release probe/classification'
    for name, target in bins.items():
        assert (candidate_bin / name).is_symlink(), f'missing npm launcher: {name}'
        assert (candidate_bin / name).resolve() == candidate_root / target.removeprefix('./'), name
    selected_bin = candidate_bin
    selection_check = 'not-applicable-historical-package-name'
    if not historical:
        active = Path(json.loads((controller / migration.CURRENT).read_bytes())['candidate']['root'])
        selected_bin = root / 'adoption-bin'
        selected_bin.mkdir()
        for name, target in bins.items():
            (selected_bin / name).symlink_to(old_root / target.removeprefix('./'))
        # Import the shipped implementation in a fresh isolated interpreter. The
        # outer harness authenticated its complete package before this point.
        # This tests source-adoption's selector component, not a source build.
        script = r'''
import json, os, sys
from pathlib import Path
from unittest import mock
sys.path.insert(0, sys.argv[1])
import integration_workspace as adoption
old, new, directory, journal = map(Path, sys.argv[2:])
expected = json.loads((new.parents[2] / 'package.json').read_bytes())['bin']
before = {name: os.readlink(directory / name) for name in expected}
replace = adoption.adoption_replace_launcher
for fail_at in range(1, len(expected) + 1):
    calls = [0]
    def injected(path, target):
        calls[0] += 1
        if calls[0] == fail_at: raise OSError('injected selector failure')
        replace(path, target)
    with mock.patch.object(adoption, 'adoption_replace_launcher', side_effect=injected):
        try:
            adoption.adoption_public_launchers(str(old), new)
            raise AssertionError('injected selector failure did not fail')
        except OSError as error:
            assert 'injected selector failure' in str(error)
    assert {name: os.readlink(directory / name) for name in expected} == before
selection = adoption.adoption_public_launchers(str(old), new, journal=journal)
adoption.adoption_verify_public_dispatch(selection)
assert {row['name'] for row in selection['links']} == set(expected)
for name, target in expected.items():
    assert (directory / name).resolve() == new.parents[2] / target.removeprefix('./')
# Mutation control reproduces the escaped incident: yy stays current, ypl old.
ypl = directory / 'ypl'
ypl.unlink(); ypl.symlink_to(before['ypl'])
try:
    adoption.adoption_verify_public_dispatch(selection)
    raise AssertionError('stale ypl was not detected')
except adoption.AdoptionError as error:
    assert 'selector drifted' in str(error)
assert not adoption.adoption_restore_public_launchers(selection), 'foreign successor must be preserved'
assert os.readlink(ypl) == before['ypl']
assert {name: os.readlink(directory / name) for name in expected} == before
selection = adoption.adoption_public_launchers(str(old), new)
adoption.adoption_verify_public_dispatch(selection)
print(json.dumps({'selection': 'passed', 'stale_ypl': 'rejected', 'rollback': 'passed'}))
'''
        checked = subprocess.run([sys.executable, '-I', '-B', '-c', script,
            str(candidate_root / 'dist/templates/scripts'), str(old_root / 'dist/bin/cli.mjs'),
            str(active / 'dist/bin/cli.mjs'), str(selected_bin), str(root / 'selector-preimages.json')],
            cwd=controller, env={**env, 'PATH': str(selected_bin) + os.pathsep + env['PATH']},
            capture_output=True, text=True, timeout=120)
        assert checked.returncode == 0, checked.stdout + checked.stderr
        assert json.loads(checked.stdout.splitlines()[-1]) == {
            'selection': 'passed', 'stale_ypl': 'rejected', 'rollback': 'passed'}
        selection_check = 'passed'
    probe_env = {**env, 'PATH': str(selected_bin) + os.pathsep + env['PATH']}
    for name in ('yy', 'yylo'):
        check = subprocess.run([str(selected_bin / name), '-q', 'scripts', 'generation', 'doctor'],
            cwd=controller, env=probe_env, capture_output=True, text=True, timeout=120)
        assert check.returncode == 0, check.stdout + check.stderr
        rows = [json.loads(line) for line in check.stdout.splitlines() if line.startswith('{')]
        assert rows[-1]['disposition'] == 'ready', rows
    if not historical:
        def readiness(expected_exit):
            check = subprocess.run([str(selected_bin / 'yy'), '-q', 'scripts', 'generation', 'readiness'],
                cwd=controller, env=probe_env, capture_output=True, text=True, timeout=120)
            assert check.returncode == expected_exit, check.stdout + check.stderr
            return [json.loads(line) for line in check.stdout.splitlines() if line.startswith('{')][-1]
        report = readiness(0)
        assert report['disposition'] == 'ready', report
        assert report['checks']['source']['sha'] == git(controller, 'rev-parse', 'product')
        assert report['checks']['source']['remote_verified'] is False
        assert len(report['checks']['launchers']['commands']) == len(bins)
        ypl = selected_bin / 'ypl'
        before = os.readlink(ypl)
        try:
            ypl.unlink(); ypl.symlink_to(old_root / bins['ypl'].removeprefix('./'))
            report = readiness(2)
            assert report['checks']['active']['status'] == 'pass', report
            assert report['checks']['launchers']['status'] == 'action_required', report
            assert os.readlink(ypl) == str(old_root / bins['ypl'].removeprefix('./'))
        finally:
            ypl.unlink(); ypl.symlink_to(before)
        assert readiness(0)['disposition'] == 'ready'
    tools = root / 'launcher-probe-tools'
    tools.mkdir()
    marker = root / 'launcher-provider-called'
    fake_pi = tools / 'pi'
    write(fake_pi, b'''#!/usr/bin/env python3
import os, pathlib, sys
if '--version' in sys.argv:
    print('pi 0.60.0'); raise SystemExit(0)
pathlib.Path(os.environ['FIXTURE_LAUNCHER_PROVIDER_MARKER']).write_text('fixture-only')
print('FIXTURE_LIVE_LAUNCHER_OK')
''')
    fake_pi.chmod(0o755)
    probe_env.update(PATH=str(tools) + os.pathsep + probe_env['PATH'],
                     FIXTURE_LAUNCHER_PROVIDER_MARKER=str(marker))
    request = [str(selected_bin / 'ypl'), '-p', 'offline launcher admission fixture']
    passed = subprocess.run(request, cwd=controller, env=probe_env,
                            capture_output=True, text=True, timeout=120)
    assert passed.returncode == 0 and marker.exists(), passed.stdout[-4000:] + passed.stderr[-4000:]
    marker.unlink()
    inventory = controller / migration.INVENTORY
    before = inventory.read_bytes()
    altered = json.loads(before); altered['packageVersion'] = '999.0.0'
    try:
        write(inventory, altered)
        refused = subprocess.run(request, cwd=controller, env=probe_env,
                                 capture_output=True, text=True, timeout=120)
        assert refused.returncode != 0 and not marker.exists(), refused.stdout + refused.stderr
        assert 'generation' in (refused.stdout + refused.stderr).lower(), refused.stdout + refused.stderr
        assert inventory.read_bytes() == migration.encoded(altered), 'refused launch changed inventory'
    finally:
        inventory.write_bytes(before)
    return {'commands': sorted(bins), 'roles': roles, 'mapping': 'passed',
            'yy_admission': 'passed', 'yylo_admission': 'passed', 'ypl_admission': 'passed',
            'ypl_unsafe_generation': 'refused-before-provider', 'source_selector': selection_check}


def harness_fixture_run(command, controller, env, mode, root):
    if mode == 'tty':
        return subprocess.run(['script', '-q', '-e', '-c', shlex.join(command), '/dev/null'],
            cwd=controller, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180)
    if mode == 'tmux':
        socket = root / 'acceptance-tmux.sock'
        status = root / 'acceptance-tmux-status'
        launcher = root / 'acceptance-tmux-launch.sh'
        status.unlink(missing_ok=True)
        # Re-establish the hermetic fixture environment after tmux's shell;
        # never let shell startup select a real installed harness/provider.
        launch = shlex.join(['env', '-i', *[f'{key}={value}' for key, value in env.items()], *command])
        write(launcher, (launch + '\nrc=$?\nprintf "%s" "$rc" > ' + shlex.quote(str(status)) + '\nexit "$rc"\n').encode())
        tmux = ['tmux', '-S', str(socket), '-f', '/dev/null']
        isolated_env = {key: value for key, value in env.items() if key not in {'TMUX', 'TMUX_PANE'}}
        try:
            subprocess.run([*tmux, 'new-session', '-d', '-s', 'acceptance', '-x', '120', '-y', '40',
                shlex.join(['sh', str(launcher)]), ';', 'set-option', '-g', 'remain-on-exit', 'on'],
                cwd=controller, env=isolated_env, check=True, capture_output=True, text=True, timeout=10)
            deadline = time.monotonic() + 180
            while not status.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert status.exists(), 'isolated tmux fixture did not finish'
            output = subprocess.check_output([*tmux, 'capture-pane', '-p', '-S', '-', '-t', 'acceptance'],
                env=isolated_env, text=True, timeout=10)
            return subprocess.CompletedProcess(command, int(status.read_text()), output, '')
        finally:
            # Only the socket/server this fixture created, never an ambient session.
            subprocess.run([*tmux, 'kill-server'], env=isolated_env, capture_output=True, timeout=10)
    return subprocess.run(command, cwd=controller, env=env, capture_output=True, text=True, timeout=180)


def scenario(artifact, historical=False, handoff_report=None):
    fixture = TaskWorkspaceFixture()
    fixture._build_hermetic_fixture()
    try:
        root, controller = fixture.root, fixture.controller
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(('JUNO_', 'YYLO_', 'GIT_', 'PI_', 'OPENAI_', 'ANTHROPIC_'))
               and not any(word in k for word in ('TOKEN', 'SECRET', 'PASSWORD', 'API_KEY'))}
        env['npm_config_cache'] = os.environ.get('npm_config_cache', str(Path.home() / '.npm'))
        env['HOME'] = str(root / 'home')
        env['XDG_STATE_HOME'] = str(root / 'state')
        (root / 'home').mkdir()
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        install_started = time.monotonic_ns()
        subprocess.run(['npm', 'install', '--offline', '--ignore-scripts', '--no-audit', '--no-fund',
                        *([] if historical else ['--global']), '--prefix', str(root / 'installed'), str(artifact)],
                       env=env, check=True, capture_output=True, text=True)
        installation_ms = (time.monotonic_ns() - install_started) / 1e6
        upgrade_samples = []
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
            invoke_started = time.monotonic_ns()
            result = subprocess.run([*command, *args], cwd=controller,
                                    env={**env, 'PATH': str(candidate_bin) + os.pathsep + env['PATH']},
                                    capture_output=True, text=True, timeout=180)
            if args == ('scripts', 'generation', 'upgrade'):
                upgrade_samples.append({'elapsed_ms': (time.monotonic_ns() - invoke_started) / 1e6,
                    'exit': result.returncode, 'entry': 'node' if historical else 'public-wrapper'})
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

        if handoff_report is not None:
            # Synthetic isolated workload, not lifecycle receipts or live tasks.
            # Upgrade must bind every applicable attempt to the authenticated
            # full predecessor package, as in the structural migration tests.
            state = json.loads((controller / migration.STATE).read_text()) if (controller / migration.STATE).exists() else {
                'schema_version': 'juno_task_workspace_state.v1', 'tasks': {}}
            for number in range(78):
                state['tasks'][f'PIN{number:03}'] = {'state': 'WORKING', 'fencing': {'attempt': 1, 'state': 'ACTIVE'}}
            write(controller / migration.STATE, state)
        before = (controller / migration.INVENTORY).read_bytes()
        assert invoke('scripts', 'generation', 'doctor')['disposition'] == 'migration_required'
        assert (controller / migration.INVENTORY).read_bytes() == before
        # Installing a newer global package must not activate it on ordinary use.
        command = ['node', str(executable)] if historical else [str(candidate_bin / 'yy')]
        ordinary = subprocess.run([*command, 'task', 'start', 'X'], cwd=controller,
                                  env={**env, 'PATH': str(candidate_bin) + os.pathsep + env['PATH']},
                                  capture_output=True, text=True, timeout=180)
        assert ordinary.returncode != 0 and 'generation_explicit_upgrade_required' in ordinary.stderr, ordinary.stderr
        assert (controller / migration.INVENTORY).read_bytes() == before
        assert not (controller / migration.CURRENT).exists()
        assert invoke('scripts', 'generation', 'upgrade')['disposition'] == 'ready'
        # Same-artifact explicit repeat must be safe and leave coherent selectors.
        assert invoke('scripts', 'generation', 'upgrade')['disposition'] == 'ready'
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
        task_state_before_launchers = (controller / migration.STATE).read_bytes()
        public_launchers = public_launcher_checks(root, controller, candidate_root, candidate_bin, old_root, env, historical)
        assert (controller / migration.STATE).read_bytes() == task_state_before_launchers, 'launcher probes changed task pins/state'
        migration.authenticate(json.loads((controller / migration.CURRENT).read_bytes())['candidate'])
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
if os.environ.get('FIXTURE_HANDOFF_ENTRY'):
    entered_ns = time.monotonic_ns()
    stdin_payload = sys.stdin.read()
    with open(os.environ['FIXTURE_HANDOFF_ENTRY'], 'a') as out:
        out.write(json.dumps({'monotonic_ns': entered_ns, 'argv': sys.argv[1:], 'stdin': stdin_payload, 'cwd': os.getcwd()}) + '\\n')
if '--session' in sys.argv and sys.argv[sys.argv.index('--session') + 1] == 'fixture-missing-session':
    print('requested fixture session missing', file=sys.stderr)
    raise SystemExit(7)
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
            if handoff_report is not None:
                # Real public wrappers and shipped service, isolated executable
                # instead of a live harness/provider. Every sample is a new process.
                samples = []
                # External monotonic timestamp immediately before exec of the
                # wrapper. Tmux server/PTY creation is separately measured, not
                # mistaken for YYLO overhead. The wrapper's own origin is still
                # independently set inside Bash and never supplied by this helper.
                exec_boundary = root / 'external-wrapper-exec.py'
                write(exec_boundary, b'''import os, pathlib, sys, time
pathlib.Path(sys.argv[1]).write_text(str(time.monotonic_ns()))
os.execvpe(sys.argv[2], sys.argv[2:], dict(os.environ))
''')
                prompt = 'fixture [brackets]; punctuation! exact once'
                marker_before = (controller / migration.CURRENT).read_bytes()
                for alias in ('yy', 'yylo'):
                    for mode, arguments in (
                        ('fresh', ['pi', '-p', prompt]),
                        ('resume', ['pi', '--resume', 'fixture-disposable-session', prompt]),
                        ('default', ['-s', 'pi', '-p', prompt]),
                        ('quiet', ['pi', '--quiet', '-p', prompt]),
                        ('tty', ['pi', '-p', prompt]),
                        ('tmux', ['pi', '-p', prompt]),
                    ):
                        for repetition in range(3):
                            entry = root / f'handoff-{alias}-{mode}-{repetition}.jsonl'
                            origin = root / f'origin-{alias}-{mode}-{repetition}'
                            started = time.monotonic_ns()
                            result = harness_fixture_run([sys.executable, str(exec_boundary), str(origin), str(candidate_bin / alias), *arguments], controller,
                                {**agent_env, 'PATH': str(fake_pi.parent) + os.pathsep + str(candidate_bin) + os.pathsep + env['PATH'],
                                 'FIXTURE_HANDOFF_ENTRY': str(entry), 'YYLO_STARTUP_TIMING': '1'}, mode, root)
                            observations = [json.loads(line) for line in entry.read_text().splitlines()] if entry.exists() else []
                            wrapper_started = int(origin.read_text()) if origin.exists() else None
                            sample = {'alias': alias, 'mode': mode, 'repetition': repetition,
                                'exit': result.returncode, 'entries': observations,
                                'launcher_setup_ms': (wrapper_started - started) / 1e6 if wrapper_started else None,
                                'elapsed_ms': (observations[0]['monotonic_ns'] - wrapper_started) / 1e6 if observations and wrapper_started else None,
                                'stdout': result.stdout, 'stderr': result.stderr}
                            diagnostic_events = []
                            clean_output = re.sub(r'\x1b\[[0-9;]*m', '', result.stdout + result.stderr)
                            decoder = json.JSONDecoder()
                            for match in re.finditer(r'\{', clean_output):
                                try:
                                    event, _ = decoder.raw_decode(clean_output[match.start():])
                                    if isinstance(event, dict) and event.get('event') == 'yylo_harness_handoff':
                                        diagnostic_events.append(event)
                                except json.JSONDecodeError:
                                    pass
                            sample['diagnostics'] = diagnostic_events
                            samples.append(sample)
                            write(handoff_report, {'endpoint': 'external harness entry', 'clock': 'monotonic',
                                'cache': 'OS cache not flushed; first and repeated new-process launches',
                                'controller_pins': len(json.loads(marker_before)['active_pins']),
                                'pin_shape': 'synthetic isolated ACTIVE attempts; full authenticated predecessor package',
                                'hardware': {'platform': platform.platform(), 'cpu_count': os.cpu_count(),
                                    'load': os.getloadavg(), 'python': sys.version, 'node': subprocess.check_output(['node', '--version'], text=True).strip()},
                                'upgrade_samples': upgrade_samples, 'installation_ms': installation_ms,
                                'samples': samples})
                            assert result.returncode == 0 and len(observations) == 1, sample
                            if mode != 'quiet':
                                assert len(diagnostic_events) == 1, sample
                                # Successful OS exec vs first harness instruction
                                # include interpreter/scheduler latency; neither
                                # endpoint includes harness-internal readiness.
                                assert abs(diagnostic_events[0]['elapsed_ms'] - sample['elapsed_ms']) < 250, sample
                            assert observations[0]['cwd'] == str(controller), sample
                            argv = observations[0]['argv']
                            assert argv.count(prompt) + (observations[0]['stdin'] == prompt) == 1, sample
                            if mode == 'resume':
                                assert argv[argv.index('--session') + 1] == 'fixture-disposable-session', sample
                            assert (controller / migration.CURRENT).read_bytes() == marker_before
                missing_session_checks = []
                for alias in ('yy', 'yylo'):
                    for repetition in range(3):
                        entry = root / f'missing-{alias}-{repetition}.jsonl'
                        missing = harness_fixture_run([str(candidate_bin / alias), 'pi', '--resume',
                            'fixture-missing-session', prompt], controller,
                            {**agent_env, 'PATH': str(fake_pi.parent) + os.pathsep + str(candidate_bin) + os.pathsep + env['PATH'],
                             'FIXTURE_HANDOFF_ENTRY': str(entry)}, 'redirected', root)
                        observations = [json.loads(line) for line in entry.read_text().splitlines()] if entry.exists() else []
                        check = {'alias': alias, 'repetition': repetition, 'exit': missing.returncode,
                            'launches': len(observations), 'stderr': missing.stderr, 'stdout': missing.stdout}
                        missing_session_checks.append(check)
                        report = json.loads(handoff_report.read_text())
                        write(handoff_report, {**report, 'missing_session_checks': missing_session_checks})
                        assert missing.returncode != 0 and len(observations) == 1, check
                        argv = observations[0]['argv']
                        assert argv[argv.index('--session') + 1] == 'fixture-missing-session', check
                # Measurements are retained even when a budget fails. Never kill
                # a valid launch merely because the acceptance budget elapsed.
                assert all(row['elapsed_ms'] <= 5000 for row in samples), 'T_handoff budget missed; inspect retained report'
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
        candidate_asset_count = len(migration.assets(migration.authenticate(candidate)))  # pristine before npm replacement
        second_upgrade = None
        if handoff_report is not None:
            # Build a distinct representative successor OUTSIDE the installed
            # package, then use genuine npm global replacement. Same version,
            # different exact artifact: version equality must not grant trust.
            successor_source = root / 'successor-source'
            shutil.copytree(candidate_root, successor_source)
            instruction = successor_source / 'dist/templates/controller-agent/AGENTS.md'
            instruction.write_bytes(instruction.read_bytes() + b'\nRepresentative second generation.\n')
            packed = subprocess.run(['npm', 'pack', '--offline', '--ignore-scripts', '--json',
                '--pack-destination', str(root), str(successor_source)], env=env,
                check=True, capture_output=True, text=True, timeout=120)
            successor_tar = root / json.loads(packed.stdout)[0]['filename']
            before_marker = (controller / migration.CURRENT).read_bytes()
            bound_before = json.loads(before_marker)['candidate']
            prior_pins = json.loads(before_marker)['active_pins']
            install_started = time.monotonic_ns()
            subprocess.run(['npm', 'install', '--offline', '--ignore-scripts', '--no-audit', '--no-fund',
                '--global', '--prefix', str(root / 'installed'), str(successor_tar)], env=env,
                check=True, capture_output=True, text=True, timeout=120)
            second_install_ms = (time.monotonic_ns() - install_started) / 1e6
            migration.authenticate(bound_before)  # retained predecessor survived npm removal
            refusal = subprocess.run([str(candidate_bin / 'yy'), 'pi', '-f', 'missing-after-second-install.txt'],
                cwd=controller, env={**env, 'PATH': str(candidate_bin) + os.pathsep + env['PATH']},
                capture_output=True, text=True, timeout=180)
            assert refusal.returncode != 0 and 'Using retained controller runtime:' in refusal.stderr, refusal.stderr
            assert (controller / migration.CURRENT).read_bytes() == before_marker
            assert invoke('scripts', 'generation', 'upgrade')['disposition'] == 'ready'
            after = json.loads((controller / migration.CURRENT).read_text())
            assert after['candidate']['sha256'] == migration.digest(successor_tar.read_bytes())
            assert after['previous'] == bound_before and after['active_pins'] == prior_pins
            migration.authenticate(bound_before)
            migration.authenticate(after['candidate'])
            for arguments in [('scripts', 'generation', 'doctor'), ('scripts', 'doctor'), ('integration', 'runtime-doctor')]:
                assert invoke(*arguments)['disposition'] == 'ready'
            second_upgrade = {'artifact_sha256': after['candidate']['sha256'],
                'installation_ms': second_install_ms, 'ordinary_before_activation': 'retained-unchanged',
                'retained_predecessor': 'authenticated', 'active_pins': 'preserved', 'doctors': 'ready'}
            report = json.loads(handoff_report.read_text())
            write(handoff_report, {**report, 'second_global_upgrade': second_upgrade})
        return {'profile': 'historical-consumer-shape' if historical else 'source-controller',
                'predecessor_kind': 'representative-fixture', 'previous_sha256': previous['sha256'],
                'installation_ms': installation_ms, 'upgrade_samples': upgrade_samples,
                'candidate_sha256': candidate['sha256'], 'managed_asset_count': candidate_asset_count,
                'migration': 'explicit', 'ordinary_pre_activation': 'refused_unchanged', 'doctor': 'ready', 'hydration': 'passed', 'finish': 'QUEUED',
                'native_merge': landed['outcome'], 'independent_bytes': 'preserved',
                'candidate_package': 'unchanged-before-explicit-npm-replacement' if second_upgrade else 'unchanged',
                'second_global_upgrade': second_upgrade, 'agent_checks': agent_checks,
                'public_launchers': public_launchers}
    finally:
        fixture.tearDown()


def main():
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument('--artifact', type=Path, required=True)
    parser.add_argument('--handoff-report', type=Path)
    parser.add_argument('--upgrade-repetitions', type=int, choices=(0, 3), default=0)
    args = parser.parse_args()
    if args.handoff_report and args.handoff_report.exists():
        parser.error('--handoff-report must name a new external file')
    results = [scenario(args.artifact.resolve(), handoff_report=args.handoff_report), scenario(args.artifact.resolve(), historical=True)]
    if args.upgrade_repetitions:
        results.extend(scenario(args.artifact.resolve()) for _ in range(args.upgrade_repetitions - 1))
        upgrades = [row['upgrade_samples'][0] for row in results if row['profile'] == 'source-controller']
        if args.handoff_report:
            report = json.loads(args.handoff_report.read_text())
            write(args.handoff_report, {**report, 'isolated_upgrade_repetitions': upgrades})
        assert len(upgrades) == 3 and all(row['entry'] == 'public-wrapper' and row['elapsed_ms'] <= 60000 for row in upgrades), upgrades
    print(json.dumps({'schema_version': 'yylo_controller_upgrade_acceptance.v1', 'outcome': 'passed', 'scenarios': results}))


if __name__ == '__main__': main()
