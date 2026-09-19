"""Real Ledger + authenticated real-Git generation cutover and rollback contracts."""
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

import test_controller_generation_migration as fixture
from test_controller_generation_migration import migration, write


class PackageWikiGenerationTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.GenerationTests()
        self.f.setUp()
        repository = Path(__file__).resolve().parents[5]
        ledger_source = repository / 'juno_kanban/src'
        self.ledger_source = ledger_source
        if not ledger_source.is_dir():
            raise RuntimeError('hydrate the selected Ledger source worktree before this cross-surface check')
        executable = self.f.root / 'bin/yylo-ledger'
        write(executable, (f'#!{sys.executable}\nimport sys\nsys.path.insert(0, {str(ledger_source)!r})\n'
                           'from yylo_ledger.cli import main\nmain()\n').encode())
        executable.chmod(0o755)
        self.env = mock.patch.dict(os.environ, {'PATH': str(executable.parent) + os.pathsep + os.environ['PATH']})
        self.env.start()
        self.enable_wiki(self.f.candidate)

    def tearDown(self):
        self.env.stop()
        self.f.tearDown()

    def enable_wiki(self, evidence):
        root = Path(evidence['root'])
        path = root / 'dist/templates/managed-assets.json'
        declaration = json.loads(path.read_text())
        declaration['ledgerWiki'] = {'schemaVersion': 'yylo_package_wiki_sources.v1',
                                    'sources': ['wiki/controller/guide.md', 'wiki/controller/recovery.md']}
        write(path, declaration)
        write(root / 'dist/templates/wiki/controller/guide.md', b'# Guide\n[Recovery](recovery.md)\n')
        write(root / 'dist/templates/wiki/controller/recovery.md', b'# Recovery\nPreserve evidence.\n')
        evidence.update(self.f.pack(root))

    def test_cutover_publishes_exact_binding_and_admits_active_runtime(self):
        root = self.f.controller
        legacy = root / '.juno_task/wiki/project-notes.md'
        write(legacy, b'# Private project notes\n')
        plan = self.f.plan()
        self.assertFalse((root / '.juno_task/documents').exists())
        result = migration.apply(root, plan)
        self.assertEqual(result['outcome'], 'completed')
        binding = json.loads((root / migration.package_wiki.BINDING).read_text())
        migration.package_wiki.verify(root, binding)
        self.assertEqual(binding, plan['package_wiki']['binding'])
        self.assertEqual(legacy.read_bytes(), b'# Private project notes\n')
        self.assertFalse((root / '.juno_task/wiki/controller/guide.md').exists())
        migration.runtime_ready(root, root, root / '.juno_task/scripts/task_workspace.py')
        receipt = root / migration.ROOT / plan['id'] / 'package-wiki-receipt.json'
        self.assertTrue(receipt.is_file())

    def test_interruption_after_staging_rolls_back_without_removing_records(self):
        root = self.f.controller
        old_inventory = (root / migration.INVENTORY).read_bytes()
        plan = self.f.plan()
        def crash(label):
            if label == 'package-wiki-staged':
                raise RuntimeError('interrupted after Ledger publication')
        with self.assertRaisesRegex(RuntimeError, 'interrupted'):
            migration.apply(root, plan, boundary=crash)
        self.assertTrue((root / migration.ROOT / 'fence.json').exists())
        result = migration.recover(root, plan['id'], rollback=True)
        self.assertEqual(result['outcome'], 'rolled_back')
        self.assertEqual((root / migration.INVENTORY).read_bytes(), old_inventory)
        self.assertFalse((root / migration.package_wiki.BINDING).exists())
        migration.package_wiki.verify(root, plan['package_wiki']['binding'])
        self.assertFalse((root / migration.ROOT / 'fence.json').exists())

    def test_resume_reuses_staged_records_and_foreign_edit_refuses(self):
        root = self.f.controller
        plan = self.f.plan()
        def crash(label):
            if label == 'package-wiki-staged':
                raise RuntimeError('interrupted')
        with self.assertRaises(RuntimeError):
            migration.apply(root, plan, boundary=crash)
        self.assertEqual(migration.recover(root, plan['id'])['outcome'], 'completed')
        snapshots = list((root / '.juno_task/documents').glob('*/*/*.json'))
        self.assertEqual(len(snapshots), 2)
        # A different package generation upgrades in-place; previous pins survive.
        previous = self.f.candidate
        candidate = self.f.package('upgrade')
        self.enable_wiki(candidate)
        package_path = Path(candidate['root']) / 'package.json'
        package = json.loads(package_path.read_text()); package['version'] = '0.2.5'
        write(package_path, package)
        candidate.update(self.f.pack(Path(candidate['root'])))
        upgraded = migration.plan(root, candidate, previous)
        migration.apply(root, upgraded)
        migration.package_wiki.verify(root, plan['package_wiki']['binding'])
        self.assertEqual(len(list((root / '.juno_task/documents').glob('*/*/*.json'))), 4)
        with mock.patch.object(sys, 'path', [str(self.ledger_source), *sys.path]):
            from yylo_ledger.documents import DocumentStore
            store = DocumentStore(root / '.juno_task')
            identity = upgraded['package_wiki']['binding']['records'][0]['id']
            before = store.get(identity)
            changed = store.update(identity, path='/title', expected=before['title'], replacement='Owner edit',
                                   expected_revision=before['revision'])
            with self.assertRaisesRegex(migration.Refusal, 'package_wiki_preparation_failed'):
                migration.plan(root, candidate, candidate)
            self.assertEqual(store.get(identity), changed)
            self.assertFalse((root / migration.ROOT / 'fence.json').exists())

    def test_authenticated_bootstrap_is_idempotent_and_does_not_activate(self):
        root = self.f.controller
        package_root = Path(self.f.candidate['root'])
        package = migration.authenticate(self.f.candidate)
        write(package_root / '.yylo-generation-evidence.json', self.f.candidate)
        with self.assertRaisesRegex(ValueError, 'existing_runtime_requires_transition'):
            migration.package_wiki.bootstrap(root, package_root)
        write(root / migration.IDENTITY, migration.runtime_identity(package))
        binding = migration.package_wiki.bootstrap(root, package_root)
        self.assertEqual(binding, migration.package_wiki.bootstrap(root, package_root))
        self.assertFalse((root / migration.CURRENT).exists())
        self.assertFalse((root / migration.package_wiki.BINDING).exists())
        self.assertEqual(len(list((root / '.juno_task/documents').glob('*/*/*.json'))), 2)
        bad = dict(binding); bad['version'] = 'unrelated-owner-version'
        write(root / migration.package_wiki.BINDING, bad)
        with self.assertRaisesRegex(ValueError, 'existing_binding_requires_transition'):
            migration.package_wiki.bootstrap(root, package_root)
        self.assertEqual(json.loads((root / migration.package_wiki.BINDING).read_text()), bad)

    def test_unavailable_ledger_fails_before_controller_mutation(self):
        root = self.f.controller
        before = (root / migration.INVENTORY).read_bytes()
        with mock.patch.object(migration.package_wiki, 'invoke', side_effect=ValueError('incompatible Ledger')):
            with self.assertRaisesRegex(migration.Refusal, 'package_wiki_preparation_failed'):
                self.f.plan()
        self.assertEqual((root / migration.INVENTORY).read_bytes(), before)
        self.assertFalse((root / migration.ROOT / 'fence.json').exists())


if __name__ == '__main__':
    unittest.main()
