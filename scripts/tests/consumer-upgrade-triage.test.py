"""Read-only triage probes of the current source, not installed RC3 acceptance.

These assertions preserve observed defects until independently scoped fixes replace
this incident probe with positive product regressions. All writes are disposable.
Run: python3 juno-code/scripts/tests/consumer-upgrade-triage.test.py
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / '.juno_task/scripts'
sys.path.insert(0, str(SCRIPTS))


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class IncidentProbes(unittest.TestCase):
    def test_consumer_without_source_tree_requires_missing_twin(self):
        helper = load('task_workflow_helper')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args):
                return subprocess.check_output(['git', '-C', directory, *args], text=True).strip()
            git('init', '-q')
            runtime = root / '.juno_task/scripts/task_workspace.py'
            runtime.parent.mkdir(parents=True)
            runtime.write_text('# consumer runtime fixture\n')
            git('add', '.')
            git('-c', 'user.name=Triage', '-c', 'user.email=triage@example.invalid',
                '-c', 'commit.gpgsign=false', 'commit', '-qm', 'fixture')
            report = helper.grouped_coherence(root, root, git('rev-parse', 'HEAD'),
                                             ['.juno_task/scripts/task_workspace.py'])
            matches = [finding for finding in report['findings']
                       if finding['code'] == 'coherence.runtime_template_mismatch']
            self.assertEqual(1, len(matches))
            self.assertEqual('.juno_task/scripts/task_workspace.py', matches[0]['path'])
            self.assertEqual('juno-code/src/templates/scripts/task_workspace.py',
                             matches[0]['twin'])

    def test_node_normalization_changes_yy_lookup(self):
        runner = load('managed_agent_runner')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outer, node_dir = root / 'isolated', root / 'global'
            outer.mkdir()
            node_dir.mkdir()
            for executable in (outer / 'yy', node_dir / 'yy', node_dir / 'node'):
                executable.write_text('#!/bin/sh\nexit 0\n')
                executable.chmod(0o755)
            with patch.dict(os.environ, {'PATH': f'{outer}{os.pathsep}{node_dir}',
                                         'YYLO_NODE_EXECUTABLE': str(node_dir / 'node')}), \
                    patch.object(runner, 'node_version', return_value='22.22.3'):
                contract, normalized = runner.managed_node_contract()
            self.assertEqual(str(outer / 'yy'), contract['yy_executable'])
            self.assertEqual(str(node_dir / 'yy'), shutil.which('yy', path=normalized))

    def test_derived_metadata_config_retains_product_fields(self):
        runner = load('managed_agent_runner')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.juno_task').mkdir()
            config = {'controllerWorkspace': {'mode': 'metadata-only'},
                      'workingDirectory': '/product', 'sessionDirectory': '/sessions',
                      'autoDependencyUpdate': True, 'hooks': {}, 'model': 'test-model'}
            (root / '.juno_task/config.json').write_text(json.dumps(config))
            out = root / 'output'
            out.mkdir()
            runner.derive_compatible_config(root, out)
            self.assertEqual(config, json.loads((out / 'compatible-config.json').read_text()))


if __name__ == '__main__':
    unittest.main()
