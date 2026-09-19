#!/usr/bin/env python3
"""Registered real-Git controller transaction, provenance and interruption fixtures."""
import copy
import io
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[2] / 'scripts'
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / 'tests'))
import controller_generation_migration as migration
from real_git_fixture import install_juno_admission_fixture


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else migration.encoded(data))


class GenerationTests(unittest.TestCase):
    def package(self, label, historical=False, incompatible=False):
        root = self.root / label
        version = '2.1.3-rc.0.32' if historical else ('0.2.3' if label == 'previous' else '0.2.4')
        write(root / 'package.json', {'name': 'juno-code' if historical else '@yylo/cli', 'version': version})
        write(root / 'dist/bin/cli.mjs', b'// installed exact fixture executable\n')
        for name in ('task_workspace.py', 'metadata_controller.py', 'task_workspace_decisions.py',
                     'task_workflow_helper.py', 'workflow_run_evidence.py', 'operation_snapshot.py'):
            data = (SCRIPTS / name).read_bytes()
            if incompatible and name == 'task_workspace.py':
                data = data.replace(b'juno_task_workspace_state.v1', b'future_state.v7')
            write(root / 'dist/templates/scripts' / name, data)
        write(root / 'dist/templates/scripts/fixture.sh', f'#!/bin/sh\necho {label}\n'.encode())
        write(root / 'dist/templates/controller-agent/AGENTS.md', f'{label} instructions\n'.encode())
        definition = {'schemaVersion': 1 if historical else 2, 'assets': [
            {'source': 'scripts/fixture.sh', 'destination': '.juno_task/scripts/fixture.sh',
             'type': 'script', 'installClass': 'script'}],
            'controllerOutputs': [{'source': 'controller-agent/AGENTS.md', 'destination': 'AGENTS.md',
                                   'type': 'instruction'}]}
        if not historical:
            definition['instructionBundle'] = {'schemaVersion': 'juno_instruction_bundle_declaration.v1',
                                               'semanticVersion': '1.2.0'}
        if label == 'candidate':
            write(root / 'dist/templates/prompts/new.md', b'new prompt\n')
            definition['assets'].append({'source': 'prompts/new.md', 'destination': '.juno_task/prompts/new.md',
                                         'type': 'prompt', 'installClass': 'controller'})
        write(root / 'dist/templates/managed-assets.json', definition)
        return self.pack(root)

    def pack(self, root):
        artifact = self.root / (root.name + '.tgz')
        with tarfile.open(artifact, 'w:gz') as archive:
            for path in sorted(root.rglob('*')):
                if path.is_file():
                    archive.add(path, arcname='package/' + path.relative_to(root).as_posix(), recursive=False)
        return {'root': str(root), 'artifact': str(artifact), 'sha256': migration.digest(artifact.read_bytes())}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.controller = self.root / 'controller'
        self.controller.mkdir()
        git(self.controller, 'init', '-b', 'controller')
        git(self.controller, 'config', 'user.email', 'test@example.com')
        git(self.controller, 'config', 'user.name', 'Test')
        git(self.controller, 'config', 'extensions.worktreeConfig', 'true')
        git(self.controller, 'config', '--local', 'juno.controller.path', str(self.controller))
        git(self.controller, 'config', '--local', 'juno.controller.branch', 'refs/heads/controller')
        git(self.controller, 'config', '--worktree', 'juno.workspace.role', 'controller')
        install_juno_admission_fixture(self.controller, (SCRIPTS / 'task_workspace.py').read_bytes())
        write(self.controller / '.gitignore', b'.juno_task/runtime/\n')
        self.previous = self.package('previous')
        self.candidate = self.package('candidate')
        self.install(self.previous)

    def install(self, evidence):
        package = migration.authenticate(evidence)
        inventory = {'schemaVersion': 1, 'packageName': package['package']['name'],
                     'packageVersion': package['package']['version'], 'assets': {}}
        for name, entry in migration.assets(package).items():
            write(self.controller / name, entry['data'])
            sha = migration.digest(entry['data'])
            inventory['assets'][name] = {'type': entry['type'], 'templateVersion': inventory['packageVersion'],
                                         'sourceSha256': sha, 'installedSha256': sha}
        write(self.controller / migration.INVENTORY, inventory)
        write(self.controller / migration.IDENTITY, migration.runtime_identity(package))
        git(self.controller, 'config', '--worktree', 'juno.controller.runtimeExecutable', package['executable'])
        git(self.controller, 'config', '--worktree', 'juno.controller.runtimeVersion', package['package']['version'])
        policy = json.loads((SCRIPTS.parent / 'config/metadata-controller.json').read_text())
        policy['controller_branch'], policy['product_ref'] = 'refs/heads/controller', 'refs/heads/product'
        policy['runtime']['package'] = package['package']['name']
        write(self.controller / migration.POLICY, policy)
        task = {'schema_version': 'juno_task_workspace_config.v1', 'repository': '.',
                'target_ref': 'refs/heads/product', 'workspace_root': str(self.root / 'tasks'),
                'branch_prefix': 'refs/heads/task-', 'allowed_paths': ['src', '.juno_task', 'juno-code', '.agents'],
                'selectable_paths': [], 'controller_private_paths': ['.juno_task/tasks'],
                'focused_validation': [{'id': 'ok', 'cwd': 'src', 'argv': ['true'],
                                        'timeout_seconds': 5, 'max_output_bytes': 1024}],
                'full_suite_validation': {'id': 'all', 'cwd': 'src', 'argv': ['true'],
                                         'timeout_seconds': 5, 'max_output_bytes': 1024}}
        write(self.controller / '.juno_task/config/task-workspace.json', task)
        write(self.controller / migration.STATE, {'schema_version': 'juno_task_workspace_state.v1',
               'queues': {}, 'tasks': {'ABC123': {'state': 'WORKING', 'fencing': {'state': 'ACTIVE', 'attempt': 3},
                                                'hydration': {'status': 'passed', 'immutable': 'keep'}}}})
        git(self.controller, 'add', '.')
        git(self.controller, 'commit', '--allow-empty', '-m', 'fixture generation')
        git(self.controller, 'branch', '-f', 'product', 'HEAD')

    def tearDown(self):
        self.temp.cleanup()

    def plan(self):
        return migration.plan(self.controller, self.candidate, self.previous)

    def test_readiness_context_authenticates_all_launchers_without_writes(self):
        package = Path(self.candidate['root'])
        bins = {'yy': 'dist/bin/yylo.sh', 'yylo': 'dist/bin/yylo.sh',
                'ypl': 'dist/bin/ypl.sh', 'feedback-yylo': 'dist/bin/feedback.sh'}
        manifest = json.loads((package / 'package.json').read_text())
        manifest['bin'] = bins
        write(package / 'package.json', manifest)
        for target in bins.values():
            write(package / target, b'#!/bin/sh\nexit 0\n')
            (package / target).chmod(0o755)
        evidence = self.pack(package)
        write(package / '.yylo-generation-evidence.json', evidence)
        homes = [self.root / 'machine-a', self.root / 'machine-b']
        for home in homes:
            home.mkdir()
            for name, target in bins.items():
                (home / name).symlink_to(package / target)
        def observe(home):
            # Retain Git on PATH for registered local-source observation.
            with mock.patch.dict(os.environ, {'PATH': str(home) + os.pathsep + os.environ['PATH']}):
                return migration.diagnostic_context(self.controller, package)
        def snapshot():
            return {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file() and not p.is_symlink()}
        before = snapshot()
        first = observe(homes[0])
        self.assertEqual(first['source']['sha'], git(self.controller, 'rev-parse', 'product'))
        self.assertFalse(first['source']['remote_verified'])
        self.assertEqual(first['launchers']['status'], 'pass')
        self.assertEqual(len(first['launchers']['commands']), 4)
        self.assertEqual(snapshot(), before)
        (homes[1] / 'ypl').unlink()
        (homes[1] / 'ypl').symlink_to(package / 'dist/bin/yylo.sh')
        self.assertEqual(observe(homes[1])['launchers']['status'], 'action_required')
        self.assertEqual(observe(homes[0])['launchers']['status'], 'pass')
        (package / 'dist/bin/ypl.sh').write_bytes(b'#!/bin/sh\nexit 1\n')
        self.assertEqual(observe(homes[0])['launchers']['status'], 'action_required')
        write(package / '.yylo-generation-evidence.json', {'invalid': True})
        self.assertEqual(observe(homes[0])['launchers']['status'], 'action_required')

    def test_authentication_compares_raw_bytes_without_building_journal_images(self):
        with mock.patch.object(migration, 'snapshot', side_effect=AssertionError('journal image on auth path')):
            package = migration.authenticate(self.candidate)
        self.assertEqual(package['files']['dist/bin/cli.mjs'], b'// installed exact fixture executable\n')
        installed = Path(self.candidate['root']) / 'dist/bin/cli.mjs'
        installed.write_bytes(b'tampered executable\n')
        with self.assertRaisesRegex(migration.Refusal, 'installed package differs'):
            migration.authenticate(self.candidate)

    def test_raw_authentication_still_rejects_artifact_tampering_and_hardlinks(self):
        artifact = Path(self.candidate['artifact'])
        original = artifact.read_bytes()
        artifact.write_bytes(b'tampered artifact')
        with self.assertRaisesRegex(migration.Refusal, 'artifact hash mismatch'):
            migration.authenticate(self.candidate)
        artifact.write_bytes(original)
        os.link(artifact, self.root / 'artifact-hardlink')
        with self.assertRaisesRegex(migration.Refusal, 'unsafe_file'):
            migration.authenticate(self.candidate)

    def test_raw_file_observation_preserves_image_contract_and_race_refusal(self):
        file = self.root / 'observed'
        file.write_bytes(b'exact bytes\n')
        file.chmod(0o600)
        self.assertEqual(migration.snapshot(file), migration.image(b'exact bytes\n', 0o600))
        original_read = Path.read_bytes
        def raced_read(path):
            data = original_read(path)
            if path == file:
                path.write_bytes(data + b'concurrent edit')
            return data
        with mock.patch.object(Path, 'read_bytes', raced_read):
            with self.assertRaisesRegex(migration.Refusal, 'concurrent_edit'):
                migration.observed_bytes(file)

    def test_global_npm_cache_discovery_authenticates_complete_installed_package(self):
        import hashlib
        import base64
        cache = self.root / 'cache'
        packed = Path(self.candidate['artifact']).read_bytes()
        digest = hashlib.sha512(packed).hexdigest()
        target = cache / '_cacache/content-v2/sha512' / digest[:2] / digest[2:4] / digest[4:]
        write(target, packed)
        entry = migration.encoded({'key': 'pacote:tarball:file:/external/release.tgz',
            'integrity': 'sha512-' + base64.b64encode(bytes.fromhex(digest)).decode(), 'time': 1}).rstrip(b'\n')
        write(cache / '_cacache/index-v5/aa/bb/key', b'\n' + hashlib.sha1(entry).hexdigest().encode() + b'\t' + entry + b'\n')
        result = migration.discover_installed(Path(self.candidate['root']), cache)
        self.assertEqual(result['sha256'], self.candidate['sha256'])
        self.assertEqual(result['artifact'], str(target))
        write(Path(self.candidate['root']) / 'dist/bin/cli.mjs', b'tampered')
        self.assertIsNone(migration.discover_installed(Path(self.candidate['root']), cache))
        write(target, b'corrupt cache')
        self.assertIsNone(migration.discover_installed(Path(self.candidate['root']), cache))

    def test_retention_survives_replacement_of_the_mutable_global_package(self):
        retained = migration.retain_installed(self.candidate, self.root / 'npm-cache', self.root / 'state')
        self.assertNotEqual(retained['root'], self.candidate['root'])
        self.assertEqual(retained, migration.retain_installed(self.candidate, self.root / 'npm-cache', self.root / 'state'))
        write(Path(self.candidate['root']) / 'dist/bin/cli.mjs', b'replaced by next global install')
        self.assertEqual(migration.authenticate(retained)['package']['version'], '0.2.4')
        self.assertEqual(json.loads((Path(retained['root']) / '.yylo-generation-evidence.json').read_text()), retained)

    def test_global_cache_discovery_refuses_fifo_and_deadline_without_blocking(self):
        cache = self.root / 'cache'
        index = cache / '_cacache/index-v5/aa/bb'
        index.mkdir(parents=True)
        fifo = index / 'fifo'
        os.mkfifo(fifo)
        with self.assertRaisesRegex(migration.Refusal, 'nonregular npm cache index'):
            migration.discover_installed(Path(self.candidate['root']), cache)
        fifo.unlink()
        write(index / 'entry', b'broken framing\n')
        with mock.patch.object(migration.time, 'monotonic', side_effect=[0, 31]):
            with self.assertRaisesRegex(migration.Refusal, 'deadline exceeded'):
                migration.discover_installed(Path(self.candidate['root']), cache)
        self.assertIsNone(migration.discover_installed(Path(self.candidate['root']), cache))

    def test_mixed_inventory_requires_explicit_reviewed_repair_and_preserves_preimages(self):
        p = self.controller / migration.INVENTORY
        value = json.loads(p.read_text())
        value['packageVersion'] = '0.2.3-rc.3'
        write(p, value)
        write(self.controller / 'AGENTS.md', b'custom instruction to preserve')
        with self.assertRaisesRegex(migration.Refusal, 'previous_inventory_unverified'):
            self.plan()
        plan = migration.plan(self.controller, self.candidate, self.previous, repair=True)
        self.assertTrue(plan['repair'])
        self.assertIn('AGENTS.md', plan['review_required'])
        before = (self.controller / 'AGENTS.md').read_bytes()
        migration.apply(self.controller, plan)
        self.assertEqual((self.controller / migration.ROOT / plan['id'] / 'previous/AGENTS.md').read_bytes(), before)
        self.assertEqual(json.loads(p.read_text())['packageVersion'], '0.2.4')
        self.assertEqual(migration.plan(self.controller, self.candidate, self.candidate)['active_pins']['ABC123']['attempt'], 3)

    def test_mixed_repair_refuses_a_stale_reviewed_preimage(self):
        plan = migration.plan(self.controller, self.candidate, self.previous, repair=True)
        write(self.controller / 'AGENTS.md', b'changed after owner review')
        with self.assertRaisesRegex(migration.Refusal, 'plan_stale'):
            migration.apply(self.controller, plan)
        self.assertFalse((self.controller / migration.ROOT / 'fence.json').exists())

    def test_complete_generation_preserves_dirty_files_skills_state_and_target(self):
        write(self.controller / 'unrelated.txt', b'dirty unrelated\x00')
        write(self.controller / '.pi/skills/independent/SKILL.md', b'independent dirty skill')
        state = (self.controller / migration.STATE).read_bytes()
        target = git(self.controller, 'rev-parse', 'product')
        plan = self.plan()
        self.assertFalse((self.controller / migration.ROOT).exists())
        self.assertEqual(plan['active_pins']['ABC123']['attempt'], 3)
        result = migration.apply(self.controller, plan)
        self.assertEqual(result['outcome'], 'completed')
        migration.assert_ready(self.controller)
        self.assertEqual((self.controller / migration.STATE).read_bytes(), state)
        self.assertEqual((self.controller / 'unrelated.txt').read_bytes(), b'dirty unrelated\x00')
        self.assertEqual((self.controller / '.pi/skills/independent/SKILL.md').read_bytes(), b'independent dirty skill')
        self.assertEqual(git(self.controller, 'rev-parse', 'product'), target)
        self.assertEqual(git(self.controller, 'config', '--worktree', '--get', 'juno.controller.runtimeVersion'), '0.2.4')
        self.assertTrue((self.controller / migration.ROOT / plan['id'] / 'previous/AGENTS.md').is_file())

    def test_exact_active_artifact_alias_is_authenticated_and_checked_for_reuse(self):
        migration.apply(self.controller, self.plan())
        alias = self.root / 'global-install'
        shutil.copytree(self.candidate['root'], alias)
        inputs = migration.AdmissionInputs()
        token = migration.admission_inputs.set(inputs)
        try:
            with mock.patch.object(migration, 'authenticate', wraps=migration.authenticate) as auth, \
                    mock.patch.object(migration, 'discover_installed', side_effect=AssertionError('no discovery')), \
                    mock.patch.object(migration, 'plan', side_effect=AssertionError('no planning')):
                result = migration.active_runtime_ready(self.controller, invocation_root=alias)
            self.assertEqual(auth.call_count, 2)  # active and predecessor, not a third archive extraction
            self.assertEqual(result['equivalent_executable'], str(alias / 'dist/bin/cli.mjs'))
            self.assertNotEqual(result['executable'], result['equivalent_executable'])
        finally:
            migration.admission_inputs.reset(token)
        self.assertTrue(inputs.unchanged())
        file = alias / 'dist/bin/cli.mjs'
        stamp = file.stat()
        data = file.read_bytes()
        file.write_bytes(b'!' + data[1:])
        os.utime(file, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        self.assertFalse(inputs.unchanged())  # content checks, not stat-only equivalence

    def test_different_or_unsafe_alias_never_admits_the_invoking_executable(self):
        migration.apply(self.controller, self.plan())
        for kind in ('changed', 'missing', 'poisoned-bytecode', 'symlink', 'git-owned'):
            with self.subTest(kind=kind):
                alias = self.root / ('alias-' + kind)
                shutil.copytree(self.candidate['root'], alias)
                script = alias / 'dist/bin/cli.mjs'
                if kind == 'changed':
                    script.write_bytes(b'not the active artifact')
                elif kind == 'missing':
                    script.unlink()
                elif kind == 'poisoned-bytecode':
                    write(alias / 'dist/templates/scripts/__pycache__/poison.pyc', b'never execute')
                elif kind == 'symlink':
                    script.unlink()
                    script.symlink_to(Path(self.candidate['root']) / 'dist/bin/cli.mjs')
                else:
                    git(alias, 'init')
                result = migration.active_runtime_ready(self.controller, invocation_root=alias)
                self.assertNotIn('equivalent_executable', result)
                self.assertEqual(result['executable'], str(Path(self.candidate['root']) / 'dist/bin/cli.mjs'))

    def test_many_pins_authenticate_each_retained_generation_once_per_plan(self):
        state = json.loads((self.controller / migration.STATE).read_text())
        for number in range(77):
            state['tasks'][f'PIN{number:03}'] = {'state': 'WORKING', 'fencing': {'attempt': 3}}
        write(self.controller / migration.STATE, state)
        first = self.plan()
        migration.apply(self.controller, first)
        with mock.patch.object(migration, 'authenticate', wraps=migration.authenticate) as authenticate, \
                mock.patch.object(migration, 'state_schemas', wraps=migration.state_schemas) as schemas:
            second = migration.plan(self.controller, self.candidate, self.candidate)
        self.assertEqual(second['active_pins'], first['active_pins'])
        self.assertEqual(authenticate.call_count, 2)
        self.assertEqual(schemas.call_count, 1)  # identical authenticated source bytes
        self.assertEqual(sum(call.args[0] == self.previous for call in authenticate.call_args_list), 1)
        # The optimization is invocation-local, never a stale trust cache.
        write(Path(self.previous['root']) / 'dist/bin/cli.mjs', b'changed after earlier assessment')
        with self.assertRaisesRegex(migration.Refusal, 'package_provenance_invalid'):
            migration.plan(self.controller, self.candidate, self.candidate)

    def test_schema_parsing_scales_with_sources_not_pins(self):
        migration.apply(self.controller, self.plan())
        marker = json.loads((self.controller / migration.CURRENT).read_text())
        original_pin = marker['active_pins']['ABC123']
        for count in (1, 100, 1000):
            with self.subTest(pins=count):
                state = json.loads((self.controller / migration.STATE).read_text())
                state['tasks'] = {f'PIN{i:04}': {'state': 'WORKING', 'fencing': {'attempt': 3}}
                                  for i in range(count)}
                marker['active_pins'] = {name: copy.deepcopy(original_pin) for name in state['tasks']}
                write(self.controller / migration.STATE, state)
                write(self.controller / migration.CURRENT, marker)
                for _ in range(2):  # independent operations must parse afresh
                    with mock.patch.object(migration.ast, 'parse', wraps=migration.ast.parse) as parse:
                        result = migration.prepare(self.controller, self.candidate, self.candidate)
                    self.assertEqual(parse.call_count, 1)
                    self.assertEqual(result['active_pins'], marker['active_pins'])

    def test_distinct_authenticated_pin_sources_and_invalid_sources(self):
        migration.apply(self.controller, self.plan())
        marker = json.loads((self.controller / migration.CURRENT).read_text())
        path = Path(self.previous['root']) / 'dist/templates/scripts/task_workspace.py'
        original = path.read_bytes()
        for source, error in ((original + b'\n# distinct source\n', None),
                              (original.replace(b'juno_task_workspace_state.v1', b'future_state.v7'),
                               migration.Refusal),
                              (b'def invalid syntax', SyntaxError)):
            with self.subTest(error=error):
                write(path, source)
                evidence = self.pack(Path(self.previous['root']))
                marker['active_pins']['ABC123']['generation'] = evidence
                write(self.controller / migration.CURRENT, marker)
                with mock.patch.object(migration.ast, 'parse', wraps=migration.ast.parse) as parse:
                    if error:
                        with self.assertRaises(error):
                            migration.prepare(self.controller, self.candidate, self.candidate)
                    else:
                        result = migration.prepare(self.controller, self.candidate, self.candidate)
                        self.assertEqual(result['active_pins'], marker['active_pins'])
                self.assertEqual(parse.call_count, 2)
        # A successful earlier assessment cannot authorize subsequently changed bytes.
        write(path, original)
        with self.assertRaisesRegex(migration.Refusal, 'package_provenance_invalid'):
            migration.prepare(self.controller, self.candidate, self.candidate)

    def test_plan_and_runtime_work_scales_with_distinct_evidence_not_pin_count(self):
        migration.apply(self.controller, self.plan())
        # A second real transition retains both the original predecessor and
        # the intermediate generation. No fabricated production receipts.
        state = json.loads((self.controller / migration.STATE).read_text())
        state['tasks']['NEW001'] = {'state': 'WORKING', 'fencing': {'attempt': 1}}
        write(self.controller / migration.STATE, state)
        successor = self.package('successor')
        migration.apply(self.controller, migration.plan(self.controller, successor, self.candidate))
        marker = json.loads((self.controller / migration.CURRENT).read_text())
        originals = [marker['active_pins']['ABC123'], marker['active_pins']['NEW001']]
        running = Path(successor['root']) / 'dist/templates/scripts/task_workspace.py'
        for count in (1, 78, 156):
            for generations in (1, 2):
                if generations > count:
                    continue
                with self.subTest(pins=count, generations=generations):
                    pins = {f'T{n:05}': copy.deepcopy(originals[n % generations]) for n in range(count)}
                    marker['active_pins'] = pins
                    write(self.controller / migration.CURRENT, marker)
                    state['tasks'] = {name: {'state': 'WORKING', 'fencing': {'attempt': pin['attempt']}}
                                      for name, pin in pins.items()}
                    write(self.controller / migration.STATE, state)
                    for operation in ('plan', 'runtime_ready', 'checked-reader'):
                        with self.subTest(operation=operation), \
                                mock.patch.object(migration, 'authenticate', wraps=migration.authenticate) as auth, \
                                mock.patch.object(migration, 'state_schemas', wraps=migration.state_schemas) as schemas:
                            if operation == 'plan':
                                result = migration.plan(self.controller, successor, successor)
                                self.assertEqual(result['active_pins'], pins)
                            elif operation == 'runtime_ready':
                                migration.runtime_ready(self.controller, self.controller, running)
                            else:
                                inputs = migration.AdmissionInputs()
                                token = migration.admission_inputs.set(inputs)
                                try:
                                    migration.active_runtime_ready(self.controller)
                                finally:
                                    migration.admission_inputs.reset(token)
                                self.assertTrue(inputs.unchanged())
                            self.assertEqual(auth.call_count, generations + 1)
                            # All generations authenticate separately, but their
                            # identical schema source is parsed only once.
                            self.assertEqual(schemas.call_count, 1)
                    # Even the last pin must be checked after earlier identical
                    # evidence hits; neither schema nor executable checks vanish.
                    last = next(reversed(pins))
                    pins[last]['executable'] = '/foreign/executable'
                    write(self.controller / migration.CURRENT, marker)
                    for operation in (lambda: migration.plan(self.controller, successor, successor),
                                      lambda: migration.runtime_ready(self.controller, self.controller, running),
                                      lambda: migration.active_runtime_ready(self.controller)):
                        with self.assertRaisesRegex(migration.Refusal, 'active_pin_unverified'):
                            operation()

    def test_runtime_readback_keeps_active_lease_pins_outside_working_state(self):
        migration.apply(self.controller, self.plan())
        state = json.loads((self.controller / migration.STATE).read_text())
        state['tasks']['ABC123']['state'] = 'QUEUED'
        write(self.controller / migration.STATE, state)
        running = Path(self.candidate['root']) / 'dist/templates/scripts/task_workspace.py'
        migration.runtime_ready(self.controller, self.controller, running)
        write(Path(self.previous['root']) / 'dist/bin/cli.mjs', b'tampered active lease runtime')
        with self.assertRaisesRegex(migration.Refusal, 'package_provenance_invalid'):
            migration.runtime_ready(self.controller, self.controller, running)

    def test_active_readiness_never_discovers_or_plans_candidate(self):
        migration.apply(self.controller, migration.plan(self.controller, self.candidate, self.previous))
        with mock.patch.object(migration, 'plan', side_effect=AssertionError('ordinary planning')), \
                mock.patch.object(migration, 'prepare', side_effect=AssertionError('ordinary preparation')), \
                mock.patch.object(migration, 'discover_installed', side_effect=AssertionError('candidate discovery')), \
                mock.patch.object(migration, 'authenticate', wraps=migration.authenticate) as authenticate:
            result = migration.active_runtime_ready(self.controller)
        self.assertEqual(result['executable'], str(Path(self.candidate['root']) / 'dist/bin/cli.mjs'))
        # The fixture has one active attempt pinned to the predecessor.
        self.assertEqual(authenticate.call_count, 2)
        self.assertEqual({call.args[0]['root'] for call in authenticate.call_args_list},
                         {self.candidate['root'], self.previous['root']})

    def test_reader_reuse_binds_all_authenticated_inputs_without_reauthentication(self):
        migration.apply(self.controller, self.plan())
        inputs = migration.AdmissionInputs()
        token = migration.admission_inputs.set(inputs)
        try:
            migration.active_runtime_ready(self.controller)
        finally:
            migration.admission_inputs.reset(token)
        with mock.patch.object(migration, 'authenticate', side_effect=AssertionError('duplicate authentication')), \
                mock.patch.object(migration, 'state_schemas', side_effect=AssertionError('duplicate schema parsing')):
            self.assertTrue(inputs.unchanged())
            for path in (self.controller / migration.CURRENT, self.controller / migration.STATE,
                         self.controller / migration.IDENTITY, self.controller / migration.INVENTORY,
                         self.controller / '.juno_task/scripts/task_workspace.py',
                         Path(self.candidate['root']) / 'dist/bin/cli.mjs',
                         Path(self.previous['root']) / 'dist/bin/cli.mjs', Path(self.previous['artifact'])):
                with self.subTest(path=path):
                    original = path.read_bytes()
                    try:
                        path.write_bytes(original + b' ')
                        self.assertFalse(inputs.unchanged())
                    finally:
                        path.write_bytes(original)
            for added in (self.controller / migration.ROOT / 'fence.json',
                          Path(self.candidate['root']) / 'dist/poison.pyc',
                          Path(self.previous['root']) / 'dist/poison.pyc',
                          Path(self.previous['root']) / '.git'):
                with self.subTest(added=added):
                    try:
                        write(added, b'not authenticated')
                        self.assertFalse(inputs.unchanged())
                    finally:
                        added.unlink()
            git(self.controller, 'config', '--worktree', 'juno.controller.runtimeVersion', 'foreign')
            self.assertFalse(inputs.unchanged())
            git(self.controller, 'config', '--worktree', 'juno.controller.runtimeVersion', '0.2.4')
            self.assertTrue(inputs.unchanged())

    def test_reader_protocol_keeps_private_inputs_until_eof_and_invalidates_changes(self):
        migration.apply(self.controller, self.plan())
        process = subprocess.Popen([sys.executable, '-B', migration.__file__, 'hold-read',
                                    '--controller', str(self.controller)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(json.loads(process.stdout.readline()), {'locked': True})
            for request, key, expected in [('recheck-active', 'unchanged', False), ('assess-active', 'assessment', None),
                                           ('recheck-active', 'unchanged', True)]:
                process.stdin.write(request + '\n'); process.stdin.flush()
                response = json.loads(process.stdout.readline())
                self.assertIn(key, response)
                if request == 'assess-active':
                    self.assertNotIn('inputs', response['assessment'])
                else:
                    self.assertIs(response[key], expected)
            with self.assertRaisesRegex(migration.Refusal, 'generation_migration_busy'):
                with migration.locked(self.controller):
                    pass
            write(Path(self.previous['root']) / 'dist/poison.pyc', b'not executed')
            process.stdin.write('recheck-active\n'); process.stdin.flush()
            self.assertEqual(json.loads(process.stdout.readline()), {'unchanged': False})
            process.stdin.write('recheck-active\n'); process.stdin.flush()
            self.assertEqual(json.loads(process.stdout.readline()), {'unchanged': False})
            process.stdin.write('{"unchanged":true}\n'); process.stdin.flush()
            self.assertIn('generation_reader_request_invalid', json.loads(process.stdout.readline())['error'])
            process.stdin.write('assess-active\n'); process.stdin.flush()
            self.assertIn('package_provenance_invalid', json.loads(process.stdout.readline())['error'])
            process.stdin.write('recheck-active\n'); process.stdin.flush()
            self.assertEqual(json.loads(process.stdout.readline()), {'unchanged': False})
        finally:
            process.stdin.close()
            process.wait(timeout=10)
            process.stdout.close(); process.stderr.close()
        self.assertEqual(process.returncode, 0)
        with migration.locked(self.controller):
            pass

    def test_active_readiness_refuses_changed_executable_and_version_selectors(self):
        migration.apply(self.controller, migration.plan(self.controller, self.candidate, self.previous))
        for key, bad, good in (
                ('runtimeExecutable', '/foreign/cli.mjs', str(Path(self.candidate['root']) / 'dist/bin/cli.mjs')),
                ('runtimeVersion', 'untrusted-version', '0.2.4')):
            with self.subTest(selector=key):
                git(self.controller, 'config', '--worktree', 'juno.controller.' + key, bad)
                with self.assertRaisesRegex(migration.Refusal, 'registered selector'):
                    migration.active_runtime_ready(self.controller)
                git(self.controller, 'config', '--worktree', 'juno.controller.' + key, good)
        migration.active_runtime_ready(self.controller)

    def test_active_readiness_refuses_fence_without_recovery_or_authentication(self):
        migration.apply(self.controller, migration.plan(self.controller, self.candidate, self.previous))
        write(self.controller / migration.ROOT / 'fence.json', {'id': 'a' * 64})
        with mock.patch.object(migration, 'authenticate', side_effect=AssertionError('must check fence first')), \
                mock.patch.object(migration, 'recover', side_effect=AssertionError('ordinary recovery')):
            with self.assertRaises(migration.Refusal):
                migration.active_runtime_ready(self.controller)

    def test_active_readiness_does_not_reuse_authentication_between_calls(self):
        migration.apply(self.controller, migration.plan(self.controller, self.candidate, self.previous))
        migration.active_runtime_ready(self.controller)
        executable = Path(self.candidate['root']) / 'dist/bin/cli.mjs'
        executable.write_bytes(b'tampered executable')
        with self.assertRaises(migration.Refusal):
            migration.active_runtime_ready(self.controller)

    def test_runtime_facts_do_not_survive_invocation_or_ignore_bytecode(self):
        migration.apply(self.controller, self.plan())
        running = Path(self.candidate['root']) / 'dist/templates/scripts/task_workspace.py'
        migration.runtime_ready(self.controller, self.controller, running)
        write(Path(self.previous['root']) / 'dist/templates/scripts/__pycache__/poison.pyc', b'poison')
        with self.assertRaisesRegex(migration.Refusal, 'package_provenance_invalid'):
            migration.runtime_ready(self.controller, self.controller, running)

    def test_exact_evidence_facts_do_not_alias_roots_or_skip_schema_pins(self):
        facts = migration.AssessmentFacts()
        # Equal digest/version does not admit an unauthenticated sibling root.
        facts.package(self.previous)
        foreign = {**self.previous, 'root': str(self.root / 'foreign')}
        with self.assertRaises(migration.Refusal):
            facts.package(foreign)
        incompatible = self.package('incompatible', incompatible=True)
        self.assertNotIn('juno_task_workspace_state.v1', facts.state_schemas(incompatible))
        self.assertIn('juno_task_workspace_state.v1', facts.state_schemas(self.previous))

    def test_retained_task_pin_is_authenticated_and_attempt_bound(self):
        migration.apply(self.controller, self.plan())
        with mock.patch.object(migration, 'plan', side_effect=AssertionError('ordinary task planning')), \
                mock.patch.object(migration, 'authenticate', wraps=migration.authenticate) as authenticate:
            pin = migration.pinned_task_runtime(self.controller, 'ABC123')
        self.assertEqual(authenticate.call_count, 2)
        self.assertTrue(pin['pinned'])
        self.assertEqual(pin['script'], str(Path(self.previous['root']) / 'dist/templates/scripts/task_workspace.py'))
        state = json.loads((self.controller / migration.STATE).read_text())
        state['tasks']['ABC123']['fencing']['attempt'] = 4
        write(self.controller / migration.STATE, state)
        self.assertFalse(migration.pinned_task_runtime(self.controller, 'ABC123')['pinned'])
        state['tasks']['ABC123']['fencing']['attempt'] = 3
        write(self.controller / migration.STATE, state)
        write(Path(self.previous['root']) / 'dist/bin/cli.mjs', b'foreign executable')
        with self.assertRaisesRegex(migration.Refusal, 'package_provenance_invalid'):
            migration.pinned_task_runtime(self.controller, 'ABC123')

    def test_runtime_readback_checks_full_identity_and_unpinned_runtime_bytes(self):
        migration.apply(self.controller, self.plan())
        running = Path(self.candidate['root']) / 'dist/templates/scripts/task_workspace.py'
        self.assertEqual(migration.runtime_ready(self.controller, self.controller, running)['schema_version'],
                         'yylo_controller_generation_admission.v1')
        identity = self.controller / migration.IDENTITY
        original = identity.read_bytes()
        value = json.loads(original)
        value['executable_sha256'] = '0' * 64
        write(identity, value)
        with self.assertRaisesRegex(migration.Refusal, 'current_generation_unverified'):
            migration.runtime_ready(self.controller, self.controller, running)
        identity.write_bytes(original)
        foreign = self.root / 'foreign-runtime.py'
        foreign.write_text('# not admitted\n')
        with self.assertRaisesRegex(migration.Refusal, 'running_generation_unverified'):
            migration.runtime_ready(self.controller, self.controller, foreign)

    def test_historical_adapter_requires_exact_verified_package(self):
        self.previous = self.package('historical', historical=True)
        self.install(self.previous)
        plan = self.plan()
        self.assertEqual(plan['adapter'], 'juno-code-2.1.3-rc.0.32')
        migration.apply(self.controller, plan)
        policy = json.loads((self.controller / migration.POLICY).read_text())
        self.assertEqual(policy['runtime']['package'], '@yylo/cli')
        self.assertIn('.juno_task/prompts/new.md', policy['tracked_exact'])

    def test_custom_managed_script_refuses_without_writes(self):
        write(self.controller / '.juno_task/scripts/fixture.sh', b'custom')
        with self.assertRaisesRegex(migration.Refusal, 'managed_preimage_modified'):
            self.plan()
        self.assertFalse((self.controller / migration.ROOT).exists())

    def test_occupied_new_destination_refuses_even_when_bytes_match(self):
        write(self.controller / '.juno_task/prompts/new.md', b'new prompt\n')
        with self.assertRaisesRegex(migration.Refusal, 'new_destination_occupied'):
            self.plan()
        self.assertFalse((self.controller / migration.ROOT).exists())

    def test_forged_or_malformed_package_identity_refuses(self):
        for evidence in ({**self.candidate, 'sha256': '0' * 64}, {**self.candidate, 'sha256': 'malformed'}):
            with self.subTest(evidence=evidence), self.assertRaisesRegex(migration.Refusal, 'package_provenance_invalid'):
                migration.plan(self.controller, evidence, self.previous)
        write(Path(self.candidate['root']) / 'dist/bin/cli.mjs', b'forged')
        with self.assertRaisesRegex(migration.Refusal, 'installed package differs'):
            self.plan()

    def test_unsupported_historical_version_is_not_a_name_alias(self):
        previous = self.package('unsupported', historical=True)
        package_path = Path(previous['root']) / 'package.json'
        write(package_path, {'name': 'juno-code', 'version': '2.1.3-rc.0.33'})
        self.previous = self.pack(Path(previous['root']))
        self.install(self.previous)
        with self.assertRaisesRegex(migration.Refusal, 'historical_identity_unsupported'):
            self.plan()

    def test_shared_state_incompatible_defers_without_lease_mutation(self):
        self.candidate = self.package('candidate', incompatible=True)
        before = (self.controller / migration.STATE).read_bytes()
        with self.assertRaisesRegex(migration.Refusal, 'shared_state_incompatible'):
            self.plan()
        self.assertEqual((self.controller / migration.STATE).read_bytes(), before)
        self.assertFalse((self.controller / migration.ROOT).exists())

    def test_full_admission_refuses_before_activation(self):
        task_path = self.controller / '.juno_task/config/task-workspace.json'
        task = json.loads(task_path.read_text())
        task['schema_version'] = 'unsupported'
        write(task_path, task)
        with self.assertRaisesRegex(migration.Refusal, 'proposed_admission_failed'):
            self.plan()
        self.assertFalse((self.controller / migration.ROOT).exists())

    def test_broken_old_admission_is_never_used_by_maintenance(self):
        old_script = Path(self.previous['root']) / 'dist/templates/scripts/task_workspace.py'
        old_script.write_bytes(old_script.read_bytes() +
            b'\ndef derived_output_admission(*args, **kwargs):\n    raise RuntimeError("old admission deadlock")\n')
        self.previous = self.pack(Path(self.previous['root']))
        self.install(self.previous)
        target = self.root / 'new-target'
        git(self.controller, 'worktree', 'add', '--detach', str(target), 'product')
        write(target / '.juno_task/scripts/task_workspace.py', (SCRIPTS / 'task_workspace.py').read_bytes())
        git(target, 'add', '.')
        git(target, 'commit', '-m', 'new target while old controller admission is broken')
        git(self.controller, 'branch', '-f', 'product', git(target, 'rev-parse', 'HEAD'))
        plan = self.plan()
        self.assertEqual(migration.apply(self.controller, plan)['outcome'], 'completed')
        self.assertNotIn(b'old admission deadlock', (self.controller / '.juno_task/scripts/task_workspace.py').read_bytes())

    def test_operational_admission_failure_remains_fenced_and_can_roll_back(self):
        plan = self.plan()
        actual = migration.admission
        def fail(root, package, proposal, authority, operational=False):
            if operational:
                raise migration.Refusal('operational_admission_failed', 'injected')
            return actual(root, package, proposal, authority)
        with mock.patch.object(migration, 'admission', side_effect=fail):
            with self.assertRaisesRegex(migration.Refusal, 'operational_admission_failed'):
                migration.apply(self.controller, plan)
        with self.assertRaises(migration.Refusal):
            migration.assert_ready(self.controller)
        self.assertFalse((self.controller / migration.ROOT / plan['id'] / 'completed.json').exists())
        self.assertEqual(migration.recover(self.controller, plan['id'], rollback=True)['outcome'], 'rolled_back')

    def test_extra_installed_import_refuses_before_execution(self):
        marker = self.root / 'untrusted-executed'
        module = Path(self.candidate['root']) / 'dist/templates/scripts/subprocess.py'
        module.write_text(f'from pathlib import Path\nPath({str(marker)!r}).write_text("unsafe")\n')
        with self.assertRaisesRegex(migration.Refusal, 'unverified installed execution entry'):
            self.plan()
        self.assertFalse(marker.exists())

    def test_admission_uses_captured_authenticated_closure_after_installed_directory_changes(self):
        value = self.plan()
        package = migration.authenticate(self.candidate)
        marker = self.root / 'untrusted-executed'
        module = Path(self.candidate['root']) / 'dist/templates/scripts/subprocess.py'
        module.write_text(f'from pathlib import Path\nPath({str(marker)!r}).write_text("unsafe")\n')
        migration.admission(self.controller, package,
                            {**{k: v for k, v in value['guards'].items() if v}, **value['after']}, value['authority'])
        self.assertFalse(marker.exists())

    def test_real_runtime_admission_refuses_candidate_target_mismatch_before_activation(self):
        runtime = Path(self.candidate['root']) / 'dist/templates/scripts/task_workspace.py'
        runtime.write_bytes(runtime.read_bytes() + b'\n# changed candidate lifecycle bytes\n')
        self.candidate = self.pack(Path(self.candidate['root']))
        with self.assertRaisesRegex(migration.Refusal, 'proposed_admission_failed'):
            self.plan()
        self.assertFalse((self.controller / migration.ROOT).exists())

    def test_forged_recovery_cannot_expand_or_change_authenticated_write_set(self):
        for name in ('unrelated.txt', '.juno_task/scripts/fixture.sh'):
            with self.subTest(name=name):
                if name == 'unrelated.txt':
                    write(self.controller / name, b'preserve unrelated')
                value = self.plan()
                value['before'][name] = migration.snapshot(self.controller / name)
                value['after'][name] = migration.image(b'forged overwrite')
                value.pop('id')
                value['id'] = migration.digest(migration.encoded(value))
                journal = self.controller / migration.ROOT / value['id']
                write(journal / 'intent.json', value)
                before = (self.controller / name).read_bytes()
                with self.assertRaisesRegex(migration.Refusal, 'journal_authority_invalid'):
                    migration.recover(self.controller, value['id'])
                self.assertEqual((self.controller / name).read_bytes(), before)
                self.assertFalse((journal / 'previous').exists())
                self.assertFalse((self.controller / migration.ROOT / 'fence.json').exists())

    def test_consecutive_upgrades_preserve_existing_attempt_pins(self):
        first = self.plan()
        migration.apply(self.controller, first)
        state_path = self.controller / migration.STATE
        state = json.loads(state_path.read_text())
        state['tasks']['NEW456'] = {'state': 'WORKING', 'fencing': {'state': 'ACTIVE', 'attempt': 1}}
        write(state_path, state)
        old_candidate = self.candidate
        self.previous = old_candidate
        self.candidate = self.package('third')
        second = self.plan()
        self.assertEqual(second['active_pins']['ABC123'], first['active_pins']['ABC123'])
        self.assertEqual(second['active_pins']['NEW456']['generation'], old_candidate)
        migration.apply(self.controller, second)

    def test_resume_honors_rollback_receipt_before_releasing_fence(self):
        value = self.plan()
        def crash_write(boundary):
            if boundary == 'readback':
                raise InterruptedError()
        with self.assertRaises(InterruptedError):
            migration.apply(self.controller, value, boundary=crash_write)
        def crash_receipt(boundary):
            if boundary == 'receipt':
                raise InterruptedError()
        with self.assertRaises(InterruptedError):
            migration.recover(self.controller, value['id'], rollback=True, boundary=crash_receipt)
        outcome = migration.recover(self.controller, value['id'])
        self.assertEqual(outcome['outcome'], 'rolled_back')
        journal = self.controller / migration.ROOT / value['id']
        self.assertFalse((journal / 'completed.json').exists())
        self.assertTrue((journal / 'rolled_back.json').exists())
        for name, expected in value['before'].items():
            self.assertEqual(migration.snapshot(migration.path_for(self.controller, name, value['authority'])), expected)

    def test_rollback_retry_has_distinct_attempt_and_independent_recovery(self):
        first = self.plan()
        def crash(boundary):
            if boundary == 'readback':
                raise InterruptedError()
        with self.assertRaises(InterruptedError):
            migration.apply(self.controller, first, boundary=crash)
        migration.recover(self.controller, first['id'], rollback=True)
        with self.assertRaisesRegex(migration.Refusal, 'transaction_terminal'):
            migration.apply(self.controller, first, boundary=crash)
        self.assertFalse((self.controller / migration.ROOT / 'fence.json').exists())
        second = self.plan()
        self.assertNotEqual(first['id'], second['id'])
        with self.assertRaises(InterruptedError):
            migration.apply(self.controller, second, boundary=crash)
        self.assertEqual(migration.recover(self.controller, second['id'])['outcome'], 'completed')
        self.assertFalse((self.controller / migration.ROOT / first['id'] / 'completed.json').exists())
        self.assertFalse((self.controller / migration.ROOT / second['id'] / 'rolled_back.json').exists())
        self.assertFalse((self.controller / migration.ROOT / 'fence.json').exists())

    def test_directory_parents_are_durable_before_activation(self):
        value = self.plan()
        seen = []
        actual = migration.fsync_dir
        def sync(path):
            seen.append(path)
            actual(path)
        def boundary(name):
            if name == 'fence':
                self.assertIn(self.controller / '.juno_task/runtime', seen)
                self.assertIn(self.controller / migration.ROOT, seen)
                self.assertIn(self.controller / migration.ROOT / value['id'], seen)
                raise InterruptedError()
        with mock.patch.object(migration, 'fsync_dir', side_effect=sync):
            with self.assertRaises(InterruptedError):
                migration.apply(self.controller, value, boundary=boundary)
        migration.recover(self.controller, value['id'], rollback=True)

    def test_simultaneous_migration_refuses_lock_owner(self):
        plan = self.plan()
        with migration.locked(self.controller):
            with self.assertRaisesRegex(migration.Refusal, 'generation_migration_busy'):
                migration.apply(self.controller, plan)

    def test_stale_preimage_and_task_state_refuse_apply(self):
        plan = self.plan()
        write(self.controller / migration.STATE, {'new': 'concurrent task state'})
        with self.assertRaisesRegex(migration.Refusal, 'shared_state_changed'):
            migration.apply(self.controller, plan)
        self.assertFalse((self.controller / migration.ROOT / 'fence.json').exists())

    def test_resume_at_every_durable_activation_boundary(self):
        plan = self.plan()
        boundaries = ['intent', 'fence', *[f'staged:{i}' for i in range(len(plan['after']))],
                      *[f'write:{i}' for i in range(len(plan['after']))], 'readback', 'receipt']
        for boundary in boundaries:
            with self.subTest(boundary=boundary):
                def crash(observed):
                    if observed == boundary:
                        raise InterruptedError('simulated termination')
                with self.assertRaises(InterruptedError):
                    migration.apply(self.controller, plan, boundary=crash)
                if boundary != 'intent':
                    with self.assertRaisesRegex(migration.Refusal, 'generation_transition_incomplete'):
                        migration.assert_ready(self.controller)
                outcome = migration.recover(self.controller, plan['id'])
                self.assertEqual(outcome['outcome'], 'completed')
                # Each crash scenario gets its own registered controller/generation.
                self.tearDown()
                self.setUp()
                plan = self.plan()

    def test_real_process_death_releases_lock_but_keeps_fence(self):
        plan = self.plan()
        pid = os.fork()
        if pid == 0:
            def die(boundary):
                if boundary == 'write:0':
                    os._exit(97)
            migration.apply(self.controller, plan, boundary=die)
            os._exit(98)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 97)
        with self.assertRaisesRegex(migration.Refusal, 'generation_transition_incomplete'):
            migration.assert_ready(self.controller)
        self.assertEqual(migration.recover(self.controller, plan['id'])['outcome'], 'completed')

    def test_rollback_allows_compatible_task_heartbeat_without_rewriting_it(self):
        plan = self.plan()
        def crash(boundary):
            if boundary == 'readback':
                raise InterruptedError()
        with self.assertRaises(InterruptedError):
            migration.apply(self.controller, plan, boundary=crash)
        state_path = self.controller / migration.STATE
        state = json.loads(state_path.read_text())
        state['tasks']['ABC123']['fencing']['heartbeat_seq'] = 19
        write(state_path, state)
        changed = state_path.read_bytes()
        migration.recover(self.controller, plan['id'], rollback=True)
        self.assertEqual(state_path.read_bytes(), changed)

    def test_terminal_replay_revalidates_real_postimages(self):
        plan = self.plan()
        migration.apply(self.controller, plan)
        self.assertEqual(migration.recover(self.controller, plan['id'])['outcome'], 'completed')
        write(self.controller / 'AGENTS.md', b'foreign post-completion edit')
        with self.assertRaisesRegex(migration.Refusal, 'operational_readback_failed'):
            migration.recover(self.controller, plan['id'])
        self.assertEqual((self.controller / 'AGENTS.md').read_bytes(), b'foreign post-completion edit')

    def test_unsupported_instruction_schema_and_forged_inventory_refuse(self):
        declaration = Path(self.candidate['root']) / 'dist/templates/managed-assets.json'
        value = json.loads(declaration.read_text())
        value['instructionBundle']['semanticVersion'] = '2.0.0'
        write(declaration, value)
        self.candidate = self.pack(Path(self.candidate['root']))
        with self.assertRaisesRegex(migration.Refusal, 'instruction_bundle_incompatible'):
            self.plan()
        self.candidate = self.package('candidate')
        inventory = self.controller / migration.INVENTORY
        value = json.loads(inventory.read_text())
        value['assets']['AGENTS.md']['sourceSha256'] = '0' * 64
        write(inventory, value)
        with self.assertRaisesRegex(migration.Refusal, 'previous_inventory_unverified'):
            self.plan()
        self.assertFalse((self.controller / migration.ROOT).exists())

    def test_rollback_preserves_foreign_edits_and_leaves_fence(self):
        plan = self.plan()
        def crash(boundary):
            if boundary == 'readback':
                raise InterruptedError()
        with self.assertRaises(InterruptedError):
            migration.apply(self.controller, plan, boundary=crash)
        write(self.controller / 'AGENTS.md', b'concurrent independent edit')
        with self.assertRaisesRegex(migration.Refusal, 'rollback_race'):
            migration.recover(self.controller, plan['id'], rollback=True)
        self.assertEqual((self.controller / 'AGENTS.md').read_bytes(), b'concurrent independent edit')
        with self.assertRaises(migration.Refusal):
            migration.assert_ready(self.controller)

    def test_atomic_rollback_race_preserves_replacement_inode(self):
        plan = self.plan()
        def crash(boundary):
            if boundary == 'readback':
                raise InterruptedError()
        with self.assertRaises(InterruptedError):
            migration.apply(self.controller, plan, boundary=crash)
        original = migration.endpoints.atomic_endpoint_publish
        def race(directory_fd, temporary_name, name, expected, quarantine_fd):
            if name == 'AGENTS.md':
                write(self.controller / name, b'concurrent at atomic boundary')
            return original(directory_fd, temporary_name, name, expected, quarantine_fd)
        with mock.patch.object(migration.endpoints, 'atomic_endpoint_publish', side_effect=race):
            with self.assertRaisesRegex(migration.Refusal, 'rollback_race'):
                migration.recover(self.controller, plan['id'], rollback=True)
        self.assertEqual((self.controller / 'AGENTS.md').read_bytes(), b'concurrent at atomic boundary')
        self.assertTrue((self.controller / migration.ROOT / 'fence.json').exists())

    def test_rollback_after_staging_preserves_old_generation_and_quarantines_temp(self):
        plan = self.plan()
        def crash(boundary):
            if boundary == 'staged:0':
                raise InterruptedError()
        with self.assertRaises(InterruptedError):
            migration.apply(self.controller, plan, boundary=crash)
        migration.recover(self.controller, plan['id'], rollback=True)
        self.assertEqual(list(self.controller.rglob('*.generation-*')), [])
        self.assertFalse((self.controller / migration.ROOT / 'fence.json').exists())

    def test_bounded_rollback_restores_exact_generation(self):
        plan = self.plan()
        def crash(boundary):
            if boundary == 'readback':
                raise InterruptedError()
        with self.assertRaises(InterruptedError):
            migration.apply(self.controller, plan, boundary=crash)
        self.assertEqual(migration.recover(self.controller, plan['id'], rollback=True)['outcome'], 'rolled_back')
        for name, old in plan['before'].items():
            self.assertEqual(migration.snapshot(migration.path_for(self.controller, name, plan['authority'])), old)
        migration.assert_ready(self.controller)

    def test_symlink_and_duplicate_destinations_refuse(self):
        (self.controller / 'AGENTS.md').unlink()
        (self.controller / 'AGENTS.md').symlink_to(self.root / 'missing')
        with self.assertRaisesRegex(migration.Refusal, 'unsafe_symlink'):
            self.plan()
        declaration_path = Path(self.candidate['root']) / 'dist/templates/managed-assets.json'
        value = json.loads(declaration_path.read_text())
        value['assets'].append(value['assets'][0])
        write(declaration_path, value)
        with self.assertRaisesRegex(migration.Refusal, 'duplicate_destination'):
            migration.assets(migration.authenticate(self.pack(Path(self.candidate['root']))))


if __name__ == '__main__':
    unittest.main()
