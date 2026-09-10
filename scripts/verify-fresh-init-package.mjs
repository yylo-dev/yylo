#!/usr/bin/env node
import assert from 'node:assert/strict';
import { execFileSync, spawnSync } from 'node:child_process';
import { access, mkdir, mkdtemp, readFile, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

const root = process.cwd();
const temporary = await mkdtemp(path.join(os.tmpdir(), 'yylo-fresh-init-package-'));
const packDirectory = path.join(temporary, 'pack');
const prefix = path.join(temporary, 'installed');
const project = path.join(temporary, 'project');
const state = path.join(temporary, 'state');
await Promise.all([mkdir(packDirectory), mkdir(project), mkdir(state)]);

function run(command, args, options = {}) {
  return spawnSync(command, args, {
    cwd: options.cwd,
    env: options.env,
    encoding: 'utf8',
    timeout: 300_000,
    maxBuffer: 20 * 1024 * 1024,
  });
}

function expectOk(result, label) {
  assert.equal(result.status, 0,
    `${label} failed\nstdout:\n${result.stdout}\nstderr:\n${result.stderr}`);
}

try {
  const packed = JSON.parse(execFileSync(
    'npm', ['pack', '--json', '--ignore-scripts', '--pack-destination', packDirectory],
    { cwd: root, encoding: 'utf8' },
  ))[0];
  const archive = path.join(packDirectory, packed.filename);
  const packageFiles = new Set(packed.files.map((entry) => entry.path));
  const retiredAssets = [
    'dist/templates/scripts/release_train.py',
    'dist/templates/scripts/tests/test_release_train.py',
    'dist/templates/wiki/controller/sealed_release_epochs.md',
  ];
  for (const retired of retiredAssets) {
    assert.equal(packageFiles.has(retired), false, `packed retired release-train asset: ${retired}`);
  }
  execFileSync('npm', ['install', '--ignore-scripts', '--prefix', prefix, archive], {
    cwd: temporary,
    stdio: 'pipe',
  });
  const yy = path.join(prefix, 'node_modules', '.bin', 'yy');
  const env = {
    ...process.env,
    XDG_STATE_HOME: state,
    PATH: `${path.join(prefix, 'node_modules', '.bin')}:${process.env.PATH ?? ''}`,
  };
  for (const key of [
    'JUNO_TASK_ROOT', 'JUNO_CONTROLLER_BRANCH', 'JUNO_WORKSPACE_ROLE',
    'JUNO_CONTROLLER_SOURCE', 'JUNO_CONTROL_INVOCATION_ROOT', 'JUNO_CONTROL_INVOCATION_ROLE',
  ]) delete env[key];

  expectOk(run('git', ['init', '-b', 'main'], { cwd: project, env }), 'git init');
  expectOk(run(yy, ['init', '--task', 'Fresh installed package canary', '--subagent', 'pi'], {
    cwd: project,
    env,
  }), 'installed yy init');
  for (const retired of [
    '.juno_task/scripts/release_train.py',
    '.juno_task/scripts/tests/test_release_train.py',
    '.juno_task/wiki/controller/sealed_release_epochs.md',
  ]) {
    await assert.rejects(access(path.join(project, retired)),
      undefined, `fresh init installed retired release-train asset: ${retired}`);
  }
  for (const instruction of ['AGENTS.md', 'CLAUDE.md']) {
    assert.doesNotMatch(await readFile(path.join(project, instruction), 'utf8'),
      /release[-_ ]train|sealed release epoch/iu,
      `fresh init installed retired release-train instruction: ${instruction}`);
  }
  const info = run(yy, ['info', '--json'], { cwd: project, env });
  expectOk(info, 'installed yy info');
  const topology = JSON.parse(info.stdout);
  assert.equal(topology.healthy, true, JSON.stringify(topology.findings, null, 2));
  assert.equal(topology.controller.valid, true);
  assert.equal(topology.invocation.role, 'controller');
  assert.equal(topology.integration.status, 'registered');
  assert.equal(topology.integration.owner.role, 'integration-owner');
  assert.equal(topology.integration.owner.roleAuthority, 'protected-integration.v1');
  assert.equal(topology.integration.owner.head, topology.target.sha);

  const doctor = run(yy, ['doctor', 'workspace'], { cwd: project, env });
  expectOk(doctor, 'installed yy doctor workspace');
  assert.match(doctor.stdout, /OK: no workspace topology findings/u);
  console.log('Installed tarball fresh init produced a healthy controller and protected integration owner.');
} finally {
  await rm(temporary, { recursive: true, force: true });
}
