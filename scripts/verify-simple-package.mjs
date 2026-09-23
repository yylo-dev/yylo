#!/usr/bin/env node
// Offline package-bound Simple acceptance. Agent dispatch is separately stubbed
// in simple-startup.test.ts. Harness dependencies are installed offline from the
// exact project lock; Simple startup must never invoke a provider or installer.
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync, existsSync, symlinkSync, rmSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const source = process.cwd();
const temporary = mkdtempSync(path.join(os.tmpdir(), 'yylo-simple-package-'));
const env = { ...process.env };
for (const key of Object.keys(env)) if (/^(JUNO_|YYLO_|GIT_)/u.test(key)) delete env[key];
const ledger = process.env.YYLO_TEST_LEDGER_EXECUTABLE;
assert.ok(ledger && path.isAbsolute(ledger), 'Set YYLO_TEST_LEDGER_EXECUTABLE to an explicitly selected compatible installed Ledger');
function run(command, args, cwd, extra = {}) {
  return spawnSync(command, args, { cwd, env: { ...env, ...extra }, encoding: 'utf8', timeout: 60_000, maxBuffer: 1024 * 1024 });
}
function ok(result, label) {
  assert.equal(result.status, 0, `${label}: ${result.stderr}\n${result.stdout}`);
  return result.stdout.trim();
}
function git(root, ...args) { return ok(run('git', ['-C', root, ...args], root), `git ${args[0]}`); }
function snapshot(root) {
  return { head: git(root, 'rev-parse', 'HEAD'), refs: git(root, 'show-ref'),
    index: git(root, 'ls-files', '--stage'), worktrees: git(root, 'worktree', 'list', '--porcelain'),
    notebook: readFileSync(path.join(root, 'lesson.ipynb'), 'utf8'),
    guidance: readFileSync(path.join(root, 'AGENTS.md'), 'utf8') };
}
try {
  const pack = JSON.parse(ok(run('npm', ['pack', '--json', '--ignore-scripts', '--pack-destination', temporary], source), 'pack'))[0];
  ok(run('tar', ['-xzf', path.join(temporary, pack.filename), '-C', temporary], temporary), 'extract');
  const packageRoot = path.join(temporary, 'package');
  writeFileSync(path.join(packageRoot, 'package-lock.json'), readFileSync(path.join(source, 'package-lock.json')));
  ok(run('npm', ['ci', '--offline', '--ignore-scripts', '--omit=dev', '--no-audit', '--no-fund'], packageRoot), 'exact-lock offline harness dependencies');
  const cli = path.join(packageRoot, 'dist/bin/cli.mjs');
  const bin = path.join(temporary, 'bin'); mkdirSync(bin);
  symlinkSync(ledger, path.join(bin, 'yylo-ledger'));
  env.PATH = `${bin}:${env.PATH ?? ''}`;
  env.XDG_STATE_HOME = path.join(temporary, 'state');
  // Any startup/delegation attempt to provision is a test failure, not a real install.
  const forbidden = path.join(temporary, 'installer-called');
  for (const name of ['npm', 'pip', 'pip3', 'uv']) writeFileSync(path.join(bin, name),
    `#!/bin/sh\necho called >> '${forbidden}'\nexit 97\n`, { mode: 0o755 });
  const yy = (cwd, args, extra) => run(process.execPath, [cli, ...args], cwd, extra);
  const root = path.join(temporary, 'project'); mkdirSync(root);
  git(root, 'init', '-q'); git(root, 'config', 'user.email', 'simple@example.test'); git(root, 'config', 'user.name', 'Fixture');
  writeFileSync(path.join(root, 'lesson.ipynb'), 'original notebook\n');
  writeFileSync(path.join(root, 'AGENTS.md'), 'Preserve notebooks; run local project tests.\n');
  git(root, 'add', 'lesson.ipynb', 'AGENTS.md'); git(root, 'commit', '-qm', 'fixture');
  writeFileSync(path.join(root, 'lesson.ipynb'), 'dirty notebook\n');
  writeFileSync(path.join(root, 'untracked.csv'), 'private fixture data\n');
  const before = snapshot(root);
  const planPath = path.join(temporary, 'simple-plan.json');
  ok(yy(root, ['init', '--mode', 'simple', '--directory', root, '--plan-file', planPath]), 'preview');
  assert.equal(existsSync(path.join(root, '.juno_task')), false);
  ok(yy(root, ['init', '--mode', 'simple', '--apply-plan', planPath]), 'apply');
  assert.deepEqual(snapshot(root), before);
  const config = JSON.parse(readFileSync(path.join(root, '.juno_task/config.json'), 'utf8'));
  assert.deepEqual(config.controllerWorkspace, { mode: 'simple', version: 1 });
  const guidance = readFileSync(path.join(root, '.juno_task/simple-agent-guidance.md'), 'utf8');
  assert.match(guidance, /Done is not managed delivery/u);
  assert.match(guidance, /without\s+file isolation/u);
  assert.equal(existsSync(path.join(root, '.juno_task/scripts')), false);
  const nested = path.join(root, 'notes/deeper'); mkdirSync(nested, { recursive: true });
  const info = JSON.parse(ok(yy(nested, ['info', '--json']), 'nested info'));
  assert.match(JSON.stringify(info), /simple/u);
  assert.match(JSON.stringify(info), new RegExp(root));
  for (const args of [['task', 'start', 'ABC123'], ['task', 'finish', 'ABC123'], ['merge', 'land', 'ABC123'], ['integration', 'sync']]) {
    const refused = yy(nested, args);
    assert.notEqual(refused.status, 0, `${args.join(' ')} must refuse`);
    assert.match(refused.stdout + refused.stderr, /Simple|simple/u);
  }
  const mismatch = yy(nested, ['ledger', 'list'], { JUNO_TASK_ROOT: path.join(temporary, 'unrelated') });
  assert.notEqual(mismatch.status, 0);
  assert.match(mismatch.stdout + mismatch.stderr, /mismatch|unrelated/u);
  const created = ok(yy(nested, ['ledger', 'create', 'Packaged Simple round trip']), 'create local task');
  // The public alias may frame one record as NDJSON or a singleton JSON array.
  const decoded = JSON.parse(created); const task = Array.isArray(decoded) ? decoded[0] : decoded;
  assert.match(task.id, /^(?:task_)?[A-Za-z0-9]{6}$/u);
  ok(yy(nested, ['task', 'local', 'get', task.id]), 'local get');
  ok(yy(nested, ['task', 'local', 'mark', 'done', task.id, '--response', 'Local bookkeeping only']), 'local mark');
  const read = ok(yy(nested, ['ledger', 'get', task.id]), 'readback');
  const readback = JSON.parse(read);
  assert.equal((Array.isArray(readback) ? readback[0] : readback).status, 'done');
  assert.equal(existsSync(path.join(nested, '.juno_task')), false);
  assert.equal(existsSync(path.join(root, '.juno_task/state/tasks.json')), false);
  assert.deepEqual(snapshot(root), before);
  assert.equal(readFileSync(path.join(root, 'untracked.csv'), 'utf8'), 'private fixture data\n');
  // An independent child must neither inherit nor mutate this parent's board.
  const parentBoard = ok(yy(root, ['ledger', 'list', '-f', 'json']), 'parent board');
  const child = path.join(root, 'independent'); mkdirSync(child);
  git(child, 'init', '-q');
  const uninitialized = yy(child, ['ledger', 'get', task.id]);
  assert.notEqual(uninitialized.status, 0, 'uninitialized child must not read parent task');
  const childPlan = path.join(temporary, 'child-plan.json');
  ok(yy(child, ['init', '--mode', 'simple', '--plan-file', childPlan]), 'nested independent preview');
  ok(yy(child, ['init', '--mode', 'simple', '--apply-plan', childPlan]), 'nested independent apply');
  const childNotes = path.join(child, 'notes'); mkdirSync(childNotes);
  const childInfo = JSON.parse(ok(yy(childNotes, ['info', '--json']), 'child info'));
  assert.equal(childInfo.root, child);
  const childRecord = JSON.parse(ok(yy(childNotes, ['ledger', 'create', 'Independent child task']), 'child Ledger create'));
  const childId = (Array.isArray(childRecord) ? childRecord[0] : childRecord).id;
  ok(yy(childNotes, ['ledger', 'get', childId]), 'child Ledger read');
  assert.notEqual(yy(root, ['ledger', 'get', childId]).status, 0, 'child task must not enter parent board');
  assert.equal(ok(yy(root, ['ledger', 'list', '-f', 'json']), 'parent unchanged'), parentBoard);
  assert.equal(existsSync(forbidden), false, 'Simple must never invoke an installer');
  // Git commits are possible, but only because this test explicitly requests one.
  git(root, 'add', 'lesson.ipynb'); git(root, 'commit', '-qm', 'explicit notebook commit');
  assert.notEqual(git(root, 'rev-parse', 'HEAD'), before.head);
  assert.equal(git(root, 'show', 'HEAD:lesson.ipynb'), 'dirty notebook');
  console.log('Simple tarball acceptance passed: dirty/nested workspace, Ledger round trip, guards, explicit Git, no provisioning or provider calls.');
} finally {
  rmSync(temporary, { recursive: true, force: true });
}
