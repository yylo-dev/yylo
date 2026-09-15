import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import { Command } from 'commander';
import fs from 'fs-extra';
import os from 'node:os';
import path from 'node:path';
import { execFileSync, spawnSync } from 'node:child_process';
import { resolveController, resolveAutomaticProjectBootstrap } from '../controller-resolver.js';
import { routeControlPlane } from '../control-plane-router.js';
import { configureTaskWorkspaceCommand, invokeTaskWorkspace, invokeLocalTaskBookkeeping } from '../../cli/commands/task.js';
import { configureWorkspaceCommands } from '../../cli/commands/workspace.js';

const script = path.resolve('src/templates/scripts/controller_resolver.py');
const wrapperSource = path.resolve('src/bin/yylo.sh');
let root: string;
let env: NodeJS.ProcessEnv;
const git = (...args: string[]) => execFileSync('git', ['-C', root, ...args], { encoding: 'utf8' }).trim();
const marker = () => path.join(root, '.juno_task/config.json');
const writeMode = (value: unknown) => fs.writeJson(marker(), { controllerWorkspace: value });
const snapshot = () => ({ head: git('rev-parse', 'HEAD'), refs: git('show-ref'), trees: git('worktree', 'list', '--porcelain'), status: git('status', '--porcelain'), index: git('ls-files', '--stage') });

beforeEach(async () => {
  env = { ...process.env };
  for (const key of Object.keys(process.env)) {
    if (key.startsWith('JUNO_') || key.startsWith('YYLO_') || key.startsWith('GIT_')) delete process.env[key];
  }
  root = await fs.mkdtemp(path.join(os.tmpdir(), 'yylo-simple-routing-'));
  git('init', '-q'); git('config', 'user.email', 'fixture@example.test'); git('config', 'user.name', 'Fixture');
  await fs.ensureDir(path.join(root, '.juno_task'));
  await writeMode({ mode: 'simple', version: 1 });
  await fs.writeFile(path.join(root, 'notebook.ipynb'), 'original notebook');
  git('add', '.juno_task/config.json', 'notebook.ipynb'); git('commit', '-qm', 'fixture');
  await fs.writeFile(path.join(root, 'notebook.ipynb'), 'dirty notebook');
});
afterEach(async () => { process.env = env; vi.restoreAllMocks(); await fs.remove(root); });

describe('Simple shared resolver', () => {
  it('uses installed code without a copied resolver/hook, preserves dirt and roots nested cwd', async () => {
    const nested = path.join(root, 'notes/deeper'); await fs.ensureDir(nested);
    const before = snapshot();
    const result = resolveController(nested);
    expect(result).toMatchObject({ role: 'simple', path: root, current_root: root, invocation_cwd: nested, workspace_mode: 'simple', workspace_version: 1 });
    expect(resolveAutomaticProjectBootstrap(nested).allowed).toBe(false);
    expect(snapshot()).toEqual(before);
    expect(await fs.readFile(path.join(root, 'notebook.ipynb'), 'utf8')).toBe('dirty notebook');
    expect(await fs.pathExists(path.join(root, '.juno_task/scripts'))).toBe(false);
  });

  it.each(['JUNO_TASK_ROOT', 'JUNO_CONTROLLER_BRANCH', 'JUNO_WORKSPACE_ROLE', 'JUNO_CONTROL_EFFECTIVE_ROOT'])('preserves %s mismatch diagnostics despite managed ignore flags', (key) => {
    process.env[key] = '/unrelated';
    expect(() => resolveController(root, 'diagnostic', { ignoreEnvironmentAssertions: true })).toThrow(/assertion mismatch/);
    const child = spawnSync('python3', [script, '--cwd', root, '--ignore-environment-assertions'], { encoding: 'utf8' });
    expect(child.status).toBe(2); expect(child.stderr).toContain('assertion mismatch');
  });

  it.each([null, {}, { mode: 'unknown' }, { mode: 'simple', version: 2 }, { mode: 'simple', version: true }, { mode: 'simple', version: 1, enabled: true }, { mode: 'simple', version: 1, policy: 'managed' }])('refuses invalid marker %j', async (value) => {
    await writeMode(value);
    expect(() => resolveController(root, 'diagnostic', { trustedResolver: true })).toThrow();
  });

  it('refuses partial managed registration and policy remnants', async () => {
    git('config', 'juno.controller.path', root);
    expect(() => resolveController(root)).toThrow(/conflicts with managed/);
    git('config', '--unset', 'juno.controller.path');
    await fs.ensureDir(path.join(root, '.juno_task/config'));
    await fs.writeJson(path.join(root, '.juno_task/config/task-workspace.json'), {});
    expect(() => resolveController(root)).toThrow(/conflicts with managed/);
  });

  it('rejects no-Git Simple folders and nested unrelated Git roots', async () => {
    const nested = path.join(root, 'child'); await fs.ensureDir(nested);
    execFileSync('git', ['-C', nested, 'init', '-q']);
    expect(() => resolveController(nested)).toThrow(/nested Simple/);
    const noGit = await fs.mkdtemp(path.join(os.tmpdir(), 'yylo-simple-no-git-'));
    try {
      await fs.ensureDir(path.join(noGit, '.juno_task'));
      await fs.writeJson(path.join(noGit, '.juno_task/config.json'), { controllerWorkspace: { mode: 'simple', version: 1 } });
      expect(() => resolveController(noGit)).toThrow(/requires a primary Git checkout/);
    } finally { await fs.remove(noGit); }
  });

  it('rejects a linked Simple checkout and symlinked configuration', async () => {
    const linked = `${root}-linked`;
    git('worktree', 'add', '--detach', linked);
    try { expect(() => resolveController(linked)).toThrow(/linked worktrees/); }
    finally { git('worktree', 'remove', linked); }
    const outside = path.join(root, 'outside.json'); await fs.move(marker(), outside);
    await fs.symlink(outside, marker());
    expect(() => resolveController(root)).toThrow(/symlinked/);
  });

  it('rejects managed operations before any script dispatch, including read-like task operations', async () => {
    vi.spyOn(process, 'cwd').mockReturnValue(root); const before = snapshot();
    for (const op of ['diagnostic', 'kanban', 'orchestration'] as const) expect(() => routeControlPlane(root, op)).toThrow(/Simple workspace/);
    await expect(invokeTaskWorkspace('status', 'ABC123')).rejects.toThrow(/Simple workspace/);
    await expect(invokeTaskWorkspace('finish', 'ABC123')).rejects.toThrow(/Simple workspace/);
    expect(snapshot()).toEqual(before);
  });

  it('packaged shell keeps Simple local and refuses managed/inherited routes without bootstrap', async () => {
    const launcher = path.join(root, 'launcher');
    const bin = path.join(launcher, 'bin');
    await fs.ensureDir(bin);
    await fs.copy(wrapperSource, path.join(bin, 'yylo'));
    await fs.copy(script, path.join(launcher, 'templates/scripts/controller_resolver.py'));
    await fs.writeFile(path.join(bin, 'cli.mjs'), 'console.log(JSON.stringify(process.argv.slice(2)))');
    const before = snapshot();
    const run = (args: string[], overrides: NodeJS.ProcessEnv = {}) => spawnSync('bash', [path.join(bin, 'yylo'), ...args], {
      cwd: root, encoding: 'utf8', env: { ...process.env, ...overrides }, timeout: 15000,
    });
    const local = run(['task', 'local', 'get', 'ABC123']);
    expect(local.status, local.stderr).toBe(0);
    expect(JSON.parse(local.stdout)).toEqual(['task', 'local', 'get', 'ABC123']);
    for (const args of [['task', 'status', 'ABC123'], ['merge', 'status'], ['integration', 'status']]) {
      const refused = run(args); expect(refused.status).toBe(2); expect(refused.stderr).toContain('Simple workspace');
    }
    const mismatch = run(['task', 'local', 'list'], { JUNO_TASK_ROOT: '/unrelated' });
    expect(mismatch.status).toBe(2); expect(mismatch.stderr).toContain('assertion mismatch');
    expect(snapshot()).toEqual(before);
    expect(await fs.pathExists(path.join(root, '.venv_juno'))).toBe(false);
    expect(await fs.pathExists(path.join(root, '.juno_task/scripts'))).toBe(false);
  });

  it('emits shell routing without a fabricated managed controller binding', () => {
    const output = execFileSync('python3', [script, '--cwd', root, '--format', 'shell'], { encoding: 'utf8' });
    expect(output).toContain('JUNO_WORKSPACE_ROLE=simple');
    expect(output).toContain('unset JUNO_KANBAN_CONTROLLER_BINDING');
    expect(output).not.toContain('controller_head');
  });

  it('reports Simple diagnostics without managed target or runtime claims', async () => {
    const output = vi.spyOn(console, 'log').mockImplementation(() => undefined);
    const program = new Command(); configureWorkspaceCommands(program, 'test-version');
    await program.parseAsync(['info', '--cwd', root, '--json'], { from: 'user' });
    expect(JSON.parse(String(output.mock.calls[0][0]))).toMatchObject({ mode: 'simple', root, managedDelivery: false, runtime: { ledgerCompatibility: 'not-checked' } });
  });

  it('local commands forward bounded bookkeeping arguments, never start/finish or checkpoints', async () => {
    const local = vi.fn(async () => undefined); const managed = vi.fn(async () => undefined);
    const program = new Command(); configureTaskWorkspaceCommand(program, managed, undefined, local);
    await program.parseAsync(['task', 'local', 'mark', 'done', 'ABC123', '--response', 'locally complete'], { from: 'user' });
    expect(local).toHaveBeenCalledWith(['mark', 'done', '--id', 'ABC123', '--response', 'locally complete']);
    expect(managed).not.toHaveBeenCalled();
  });

  it('forwards to standalone Ledger without installing or changing Git', async () => {
    vi.spyOn(process, 'cwd').mockReturnValue(root); const before = snapshot();
    // Resolver still needs python3 and git; a stub Ledger demonstrates bounded forwarding.
    const bin = path.join(root, 'bin'); await fs.ensureDir(bin);
    await fs.writeFile(path.join(bin, 'yylo-ledger'), '#!/bin/sh\nprintf "%s\\n" "$@" > "$JUNO_TASK_ROOT/args.txt"\n', { mode: 0o755 });
    process.env.PATH = `${bin}:${process.env.PATH}`;
    await invokeLocalTaskBookkeeping(['get', 'ABC123']);
    expect(await fs.readFile(path.join(root, 'args.txt'), 'utf8')).toBe(`-c\n${root}/.juno_task/tasks/config.json\nget\nABC123\n`);
    expect(git('rev-parse', 'HEAD')).toBe(before.head);
    expect(git('ls-files', '--stage')).toBe(before.index);
  });
});
