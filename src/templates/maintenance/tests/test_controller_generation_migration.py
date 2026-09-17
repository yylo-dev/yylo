#!/usr/bin/env python3
"""Registered real-Git controller transaction, provenance and interruption fixtures."""
import copy
import io
import json
import os
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

    def test_retained_task_pin_is_authenticated_and_attempt_bound(self):
        migration.apply(self.controller, self.plan())
        pin = migration.pinned_task_runtime(self.controller, 'ABC123')
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
