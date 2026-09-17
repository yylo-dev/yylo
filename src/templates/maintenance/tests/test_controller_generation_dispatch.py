#!/usr/bin/env python3
"""Actual built CLI invocation against two authenticated offline fixture packages.

The CLI bytes are shipped build output; old/new manifests are deliberately small
representative generations, not claims about a historical published tarball.
"""
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

PACKAGE = Path(__file__).resolve().parents[4]
SCRIPTS = PACKAGE / 'src/templates/scripts'
sys.path.insert(0, str(SCRIPTS / 'tests'))
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(PACKAGE / 'src/templates/maintenance'))
import controller_generation_migration as migration
from test_task_workspace import TaskWorkspaceFixture


def write(file, data):
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(data if isinstance(data, bytes) else migration.encoded(data))


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


class PublicGenerationDispatchTests(unittest.TestCase):
    def test_public_doctor_first_use_task_start_and_noop_replay(self):
        fixture = TaskWorkspaceFixture()
        fixture._build_hermetic_fixture()
        self.addCleanup(fixture.tearDown)
        controller, root = fixture.controller, fixture.root
        env = {k: v for k, v in os.environ.items() if not k.startswith(('JUNO_', 'YYLO_', 'GIT_'))}
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        # Install dependencies from the actual packed package, offline and with
        # scripts disabled. Never borrow node_modules from another checkout.
        packed = subprocess.run(['npm', 'pack', '--ignore-scripts', '--json', '--pack-destination', str(root)],
                                cwd=PACKAGE, env=env, check=True, text=True, capture_output=True)
        artifact = root / json.loads(packed.stdout)[0]['filename']
        subprocess.run(['npm', 'install', '--offline', '--ignore-scripts', '--no-audit', '--no-fund',
                        '--prefix', str(root), str(artifact)], env=env, check=True, capture_output=True, text=True)
        board = (controller / '.juno_task/scripts/kanban.sh').read_bytes()
        version = json.loads((PACKAGE / 'package.json').read_text())['version']
        packages = []
        for label in ('previous', 'candidate'):
            package = root / label
            write(package / 'package.json', {'name': '@yylo/cli', 'version': version, 'type': 'module'})
            write(package / 'dist/bin/cli.mjs', (PACKAGE / 'dist/bin/cli.mjs').read_bytes())
            shutil.copytree(PACKAGE / 'dist/templates/scripts', package / 'dist/templates/scripts',
                            ignore=shutil.ignore_patterns('__pycache__', 'tests'))
            shutil.copytree(PACKAGE / 'dist/templates/maintenance', package / 'dist/templates/maintenance',
                            ignore=shutil.ignore_patterns('__pycache__', 'tests'))
            if label == 'previous':
                runtime = package / 'dist/templates/scripts/task_workspace.py'
                runtime.write_bytes(runtime.read_bytes() + b'\n# representative previous runtime generation\n')
            write(package / 'dist/templates/scripts/kanban.sh', board)
            write(package / 'dist/templates/controller-agent/AGENTS.md', f'{label} guidance\n'.encode())
            assets = [{'source': 'scripts/' + file.name, 'destination': '.juno_task/scripts/' + file.name,
                       'type': 'script', 'installClass': 'script'}
                      for file in sorted((package / 'dist/templates/scripts').iterdir())
                      if file.is_file() and (file.suffix == '.py' or file.name == 'kanban.sh')]
            write(package / 'dist/templates/managed-assets.json', {
                'schemaVersion': 2, 'instructionBundle': {'schemaVersion': 'juno_instruction_bundle_declaration.v1', 'semanticVersion': '1.2.0'},
                'assets': assets, 'controllerOutputs': [{'source': 'controller-agent/AGENTS.md', 'destination': 'AGENTS.md', 'type': 'instruction'}]})
            tar = root / (label + '.tgz')
            with tarfile.open(tar, 'w:gz') as archive:
                for file in sorted(package.rglob('*')):
                    if file.is_file(): archive.add(file, arcname='package/' + file.relative_to(package).as_posix(), recursive=False)
            evidence = {'root': str(package), 'artifact': str(tar), 'sha256': migration.digest(tar.read_bytes())}
            write(package / '.yylo-generation-evidence.json', evidence)
            packages.append(evidence)
        previous, candidate = packages
        old = migration.authenticate(previous)
        inventory = {'schemaVersion': 1, 'packageName': '@yylo/cli', 'packageVersion': version, 'assets': {}}
        for relative, item in migration.assets(old).items():
            write(controller / relative, item['data'])
            digest = migration.digest(item['data'])
            inventory['assets'][relative] = {'type': item['type'], 'templateVersion': version, 'sourceSha256': digest, 'installedSha256': digest}
        (controller / '.juno_task/scripts/kanban.sh').chmod(0o755)
        write(controller / migration.INVENTORY, inventory)
        write(controller / migration.IDENTITY, migration.runtime_identity(old))
        git(controller, 'config', '--local', 'juno.controller.branch', 'refs/heads/controller')
        git(controller, 'config', '--worktree', 'juno.controller.runtimeExecutable', old['executable'])
        git(controller, 'config', '--worktree', 'juno.controller.runtimeVersion', version)
        git(controller, 'add', '.juno_task/managed-assets.json')
        git(controller, 'commit', '-m', 'fixture exact previous installed generation')
        write(controller / 's1034-out.txt', b'preserved unrelated output\x00')
        target = git(controller, 'rev-parse', 'product')
        executable = str(Path(candidate['root']) / 'dist/bin/cli.mjs')

        def invoke(*args, cli=executable):
            result = subprocess.run(['node', cli, *args], cwd=controller, env=env,
                                    capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout[-4000:] + result.stderr[-4000:])
            rows = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
            return rows[-1] if rows else None

        subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(controller / '.venv_juno')], check=True, env=env)
        with (controller / '.gitignore').open('a') as out: out.write('/.venv_juno/\n')
        git(controller, 'add', '.gitignore'); git(controller, 'commit', '-m', 'isolated fixture interpreter')
        workflow_path = '.juno_task/config/worktree-hydration.yaml'
        write(fixture.repository / workflow_path, {'schema_version': 'v1', 'workflow_id': 'active-pin-hydration',
            'workflow_class': 'task_hydration', 'steps': [{'id': 'ready', 'name': 'Offline hydration',
                'probe': ['true'], 'command': ['true'], 'timeout_seconds': 30, 'fail_workflow': True,
                'non_interactive': True, 'network': False, 'sensitive': False, 'outputs': []}]})
        git(fixture.repository, 'add', workflow_path)
        runtime_paths = ['.juno_task/scripts/task_workspace.py', 'juno-code/src/templates/scripts/task_workspace.py']
        old_bytes = (Path(previous['root']) / 'dist/templates/scripts/task_workspace.py').read_bytes()
        for relative in runtime_paths: write(fixture.repository / relative, old_bytes)
        git(fixture.repository, 'add', *runtime_paths)
        git(fixture.repository, 'commit', '-m', 'previous source runtime fixture')
        active = invoke('task', 'start', 'Y', cli=str(Path(previous['root']) / 'dist/bin/cli.mjs'))
        self.assertEqual(active['state'], 'WORKING')
        self.assertEqual(active['hydration']['status'], 'passed')
        active_record = json.loads((controller / migration.STATE).read_text())['tasks']['Y']
        for relative in runtime_paths: write(fixture.repository / relative, (SCRIPTS / 'task_workspace.py').read_bytes())
        git(fixture.repository, 'add', *runtime_paths)
        git(fixture.repository, 'commit', '-m', 'candidate source runtime fixture')
        target = git(controller, 'rev-parse', 'product')
        before = (controller / migration.INVENTORY).read_bytes()
        doctor = invoke('scripts', 'generation', 'doctor')
        self.assertEqual(doctor['disposition'], 'migration_required')
        self.assertNotIn('before', doctor)
        self.assertEqual((controller / migration.INVENTORY).read_bytes(), before)
        started = invoke('task', 'start', 'X')
        self.assertEqual(started['state'], 'WORKING')
        self.assertTrue((controller / migration.CURRENT).is_file())
        self.assertEqual((controller / 'AGENTS.md').read_bytes(), b'candidate guidance\n')
        self.assertEqual(json.loads((controller / migration.STATE).read_text())['tasks']['Y'], active_record)
        pin = migration.pinned_task_runtime(controller, 'Y')
        self.assertTrue(pin.get('pinned') or pin.get('retained_pin'))
        self.assertEqual(json.loads((controller / migration.CURRENT).read_text())['active_pins']['Y']['generation'], previous)
        invoke('task', 'lease-heartbeat', 'Y', '--lease-token', active['lease_token'])
        fixture.commit_task('Y', 'src/pinned.txt')
        self.assertEqual(invoke('task', 'finish', 'Y', '--lease-token', active['lease_token'])['state'], 'QUEUED')
        marker = (controller / migration.CURRENT).read_bytes()
        self.assertEqual(invoke('scripts', 'generation', 'doctor')['disposition'], 'ready')
        invoke('task', 'status', 'X')
        self.assertEqual((controller / migration.CURRENT).read_bytes(), marker)
        self.assertEqual((controller / 's1034-out.txt').read_bytes(), b'preserved unrelated output\x00')
        self.assertEqual(git(controller, 'rev-parse', 'product'), target)
        # Unsupported candidate still dispatches the *old executable*, not old
        # scripts under the incompatible caller. Nested shared readers must work.
        unsupported = root / 'unsupported'
        shutil.copytree(Path(candidate['root']), unsupported)
        declaration = unsupported / 'dist/templates/managed-assets.json'
        value = json.loads(declaration.read_text())
        value['instructionBundle']['semanticVersion'] = '99.0.0'
        write(declaration, value)
        bad_tar = root / 'unsupported.tgz'
        with tarfile.open(bad_tar, 'w:gz') as archive:
            for file in sorted(unsupported.rglob('*')):
                if file.is_file(): archive.add(file, arcname='package/' + file.relative_to(unsupported).as_posix(), recursive=False)
        write(unsupported / '.yylo-generation-evidence.json', {'root': str(unsupported), 'artifact': str(bad_tar),
                                                              'sha256': migration.digest(bad_tar.read_bytes())})
        self.assertEqual(invoke('task', 'start', 'Z', cli=str(unsupported / 'dist/bin/cli.mjs'))['state'], 'WORKING')
        self.assertEqual((controller / migration.CURRENT).read_bytes(), marker)
        self.assertEqual((controller / 'AGENTS.md').read_bytes(), b'candidate guidance\n')
        relative = subprocess.run(['node', str(unsupported / 'dist/bin/cli.mjs'), 'pi',
            '-w', os.path.relpath(controller, root), '-f', 'missing-relative-prompt.txt'],
            cwd=root, env=env, capture_output=True, text=True, timeout=120)
        self.assertNotEqual(relative.returncode, 0)  # Missing input must never launch a model.
        self.assertIn('Using retained controller runtime:', relative.stderr)
        clean_stderr = re.sub(r'\x1b\[[0-9;]*m', '', relative.stderr)
        self.assertIn('Working directory: ' + str(root), [line.strip() for line in clean_stderr.splitlines()])
        self.assertNotIn(str(controller / controller.name), relative.stderr)
        failed = []
        for cli in (executable, str(unsupported / 'dist/bin/cli.mjs')):
            failed.append(subprocess.run(['node', cli, 'task', 'start', 'MISSING'], cwd=controller, env=env,
                                         capture_output=True, text=True, timeout=120))
        self.assertNotEqual(failed[0].returncode, 0)
        self.assertEqual(failed[1].returncode, failed[0].returncode)
        self.assertIn(failed[0].stdout, failed[1].stdout)
        self.assertIn(failed[0].stderr, failed[1].stderr)
        self.assertEqual((controller / migration.CURRENT).read_bytes(), marker)


if __name__ == '__main__': unittest.main()
