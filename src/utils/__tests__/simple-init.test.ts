import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { execFileSync, spawnSync } from 'node:child_process';
import { Command } from 'commander';
import { planSimpleInit, applySimpleInit, writeSimpleInitPlan } from '../simple-init.js';
import { resolveController } from '../controller-resolver.js';
import { configureInitCommand } from '../../cli/commands/init.js';
import { SIMPLE_FILES } from '../../templates/simple-workspace.js';

let root: string;
let originalEnv: NodeJS.ProcessEnv;
const git = (...args: string[]) => execFileSync('git', ['-C', root, ...args], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();
const snap = () => ({ head: git('rev-parse', 'HEAD'), refs: git('show-ref'), worktrees: git('worktree', 'list', '--porcelain'), index: git('ls-files', '--stage') });
beforeEach(async () => {
  originalEnv = { ...process.env };
  for (const key of Object.keys(process.env)) if (key.startsWith('JUNO_') || key.startsWith('YYLO_') || key.startsWith('GIT_')) delete process.env[key];
  root = await fs.mkdtemp(path.join(os.tmpdir(), 'yylo-simple-init-'));
  git('init', '-q'); git('config', 'user.email', 'fixture@example.test'); git('config', 'user.name', 'Fixture');
  await fs.writeFile(path.join(root, 'notebook.ipynb'), 'original');
  git('add', 'notebook.ipynb'); git('commit', '-qm', 'fixture');
  await fs.writeFile(path.join(root, 'notebook.ipynb'), 'dirty notebook');
});
afterEach(async () => { vi.restoreAllMocks(); process.env = originalEnv; await fs.rm(root, { recursive: true, force: true }); });

describe('fresh Simple initialization', () => {
  it('previews without writes and applies only named local assets, preserving Git and user bytes', async () => {
    await fs.writeFile(path.join(root, 'AGENTS.md'), 'Use project tests.');
    await fs.writeFile(path.join(root, 'CLAUDE.md'), 'Read the notebooks.');
    await fs.writeFile(path.join(root, '.gitignore'), '*.csv\n');
    const before = snap(); const entries = await fs.readdir(root);
    const plan = await planSimpleInit(root);
    expect(plan.outcome).toBe('ready'); expect(await fs.readdir(root)).toEqual(entries);
    expect(await applySimpleInit(plan)).toBe('initialized');
    expect(snap()).toEqual(before);
    expect(await fs.readFile(path.join(root, 'notebook.ipynb'), 'utf8')).toBe('dirty notebook');
    expect(await fs.readFile(path.join(root, 'AGENTS.md'), 'utf8')).toBe('Use project tests.');
    expect(await fs.readFile(path.join(root, 'CLAUDE.md'), 'utf8')).toBe('Read the notebooks.');
    expect(await fs.readFile(path.join(root, '.gitignore'), 'utf8')).toBe('*.csv\n');
    expect((await fs.readdir(path.join(root, '.juno_task'))).sort()).toEqual(['.gitignore', 'config.json', 'simple-agent-guidance.md', 'simple-init.json']);
    for (const [name, bytes] of Object.entries(SIMPLE_FILES)) expect(await fs.readFile(path.join(root, '.juno_task', name), 'utf8')).toBe(bytes);
    const guidance = await fs.readFile(path.join(root, '.juno_task/simple-agent-guidance.md'), 'utf8');
    expect(guidance).toContain('Done is not managed delivery');
    expect(guidance).toContain('without\nfile isolation');
    expect(guidance).toContain('No-Git execution');
    expect(guidance).toContain('Never convert an existing managed installation');
    expect(resolveController(root)).toMatchObject({ role: 'simple', path: root });
    expect(await applySimpleInit(await planSimpleInit(root))).toBe('already-initialized');
    expect(snap()).toEqual(before);
  });

  it('keeps durable Ledger paths Git-eligible and ignores only disposable/secret paths', async () => {
    await applySimpleInit(await planSimpleInit(root));
    for (const name of ['tasks/aa/ABC123.md', 'ledger/aa/event.json', 'documents/aa/r.json', 'artifacts/aa/r.json', 'artifact-ledger/aa/r.json', 'objects/sha256/aa/hash', 'archive/pack']) {
      expect(spawnSync('git', ['-C', root, 'check-ignore', '--no-index', `.juno_task/${name}`]).status, name).toBe(1);
    }
    for (const name of ['cache/kanban.sqlite3', 'locks/aa/ABC123.lock', 'logs/run.log', 'runtime/run.json', 'secrets/.env.yylo', 'sessions/secret.json']) {
      expect(spawnSync('git', ['-C', root, 'check-ignore', '--no-index', `.juno_task/${name}`]).status, name).toBe(0);
    }
  });

  it('requires explicit prior Git initialization and supports an unborn Git repository', async () => {
    const folder = await fs.mkdtemp(path.join(os.tmpdir(), 'yylo-simple-empty-'));
    try {
      await expect(planSimpleInit(folder)).rejects.toThrow(/Run git init explicitly/);
      expect(await fs.readdir(folder)).toEqual([]);
      execFileSync('git', ['init', '-q', folder]);
      const plan = await planSimpleInit(folder); expect(plan.identity.head).toBeNull();
      await applySimpleInit(plan);
      expect(spawnSync('git', ['-C', folder, 'rev-parse', '--verify', 'HEAD']).status).not.toBe(0);
    } finally { await fs.rm(folder, { recursive: true, force: true }); }
  });

  it.each(['AGENTS.md', 'CLAUDE.md'])('refuses managed %s instructions without rewriting them', async (name) => {
    await fs.writeFile(path.join(root, name), 'Start with yy task start TASK_ID in another worktree.');
    await expect(planSimpleInit(root)).rejects.toThrow(/managed-lifecycle guidance/);
    expect(await fs.readFile(path.join(root, name), 'utf8')).toContain('yy task start');
    await expect(fs.stat(path.join(root, '.juno_task'))).rejects.toThrow();
  });

  it('refuses ignore rules hiding durable metadata rather than editing them', async () => {
    await fs.writeFile(path.join(root, '.gitignore'), '.juno_task/\n');
    await expect(planSimpleInit(root)).rejects.toThrow(/hide durable Simple data/);
    expect(await fs.readFile(path.join(root, '.gitignore'), 'utf8')).toBe('.juno_task/\n');
  });

  it('refuses existing metadata and symlinked root instructions', async () => {
    await fs.mkdir(path.join(root, '.juno_task'));
    await fs.writeFile(path.join(root, '.juno_task/untracked.txt'), 'preserve');
    await expect(planSimpleInit(root)).rejects.toThrow(/conflicts with fresh initialization/);
    expect(await fs.readFile(path.join(root, '.juno_task/untracked.txt'), 'utf8')).toBe('preserve');
    await fs.symlink('notebook.ipynb', path.join(root, 'AGENTS.md'));
    await expect(planSimpleInit(root)).rejects.toThrow(/non-symlink/);
  });

  it('refuses registered managed repos, nested directories and inherited unrelated authority', async () => {
    git('config', 'juno.controller.path', root);
    await expect(planSimpleInit(root)).rejects.toThrow(/Managed registration conflicts/);
    git('config', '--unset', 'juno.controller.path');
    const nested = path.join(root, 'notes'); await fs.mkdir(nested);
    await expect(planSimpleInit(nested)).rejects.toThrow(/Git top-level/);
    process.env.JUNO_TASK_ROOT = '/unrelated';
    await expect(planSimpleInit(root)).rejects.toThrow(/assertion mismatch/);
  });

  it('refuses linked worktrees and symlinked metadata without writing through them', async () => {
    const linked = `${root}-linked`;
    git('worktree', 'add', '--detach', linked);
    try { await expect(planSimpleInit(linked)).rejects.toThrow(/linked worktrees/); }
    finally { git('worktree', 'remove', linked); }
    const outside = await fs.mkdtemp(path.join(os.tmpdir(), 'yylo-simple-outside-'));
    try {
      await fs.symlink(outside, path.join(root, '.juno_task'));
      await expect(planSimpleInit(root)).rejects.toThrow(/regular local directory/);
      expect(await fs.readdir(outside)).toEqual([]);
    } finally { await fs.rm(outside, { recursive: true, force: true }); }
  });

  it('admits only one concurrent initializer and never overwrites the winning bytes', async () => {
    const plan = await planSimpleInit(root);
    const results = await Promise.allSettled([applySimpleInit(plan), applySimpleInit(plan)]);
    expect(results.filter((result) => result.status === 'fulfilled')).toHaveLength(1);
    expect((await planSimpleInit(root)).outcome).toBe('already-initialized');
    expect(await fs.readFile(path.join(root, 'notebook.ipynb'), 'utf8')).toBe('dirty notebook');
  });

  it('refuses stale plans after HEAD, config or preserved file changes', async () => {
    const plan = await planSimpleInit(root);
    await fs.writeFile(path.join(root, 'AGENTS.md'), 'new instruction');
    await expect(applySimpleInit(plan)).rejects.toThrow(/Stale or modified/);
    const next = await planSimpleInit(root);
    git('config', 'user.name', 'Changed');
    await expect(applySimpleInit(next)).rejects.toThrow(/Stale or modified/);
    const headPlan = await planSimpleInit(root);
    git('commit', '--allow-empty', '-qm', 'move head');
    await expect(applySimpleInit(headPlan)).rejects.toThrow(/Stale or modified/);
  });

  it('rejects tampered output paths and concurrent destination creation', async () => {
    const plan = await planSimpleInit(root);
    const bad = structuredClone(plan); bad.files['../notebook.ipynb'] = 'overwrite';
    await expect(applySimpleInit(bad)).rejects.toThrow(/Stale or modified/);
    await fs.mkdir(path.join(root, '.juno_task'));
    await expect(applySimpleInit(plan)).rejects.toThrow(/conflicts with fresh initialization/);
    expect(await fs.readFile(path.join(root, 'notebook.ipynb'), 'utf8')).toBe('dirty notebook');
  });

  it('preserves interrupted writes and blocks resolution until explicit recovery', async () => {
    const plan = await planSimpleInit(root);
    const write = fs.writeFile.bind(fs);
    vi.spyOn(fs, 'writeFile').mockImplementation(async (...args: Parameters<typeof fs.writeFile>) => {
      if (String(args[0]).endsWith('/.juno_task/config.json')) throw new Error('injected interrupted write');
      return write(...args);
    });
    await expect(applySimpleInit(plan)).rejects.toThrow(/injected/);
    vi.restoreAllMocks();
    expect(await fs.readFile(path.join(root, '.juno_task/simple-agent-guidance.md'), 'utf8')).toContain('Simple workspace');
    await expect(planSimpleInit(root)).rejects.toThrow(/Interrupted or active/);
    expect(() => resolveController(root)).toThrow(/incomplete or active Simple/);
    expect(await fs.readFile(path.join(root, 'notebook.ipynb'), 'utf8')).toBe('dirty notebook');
  });

  it('writes plans exclusively outside the project and CLI defaults to preview', async () => {
    const plan = await planSimpleInit(root);
    await expect(writeSimpleInitPlan(path.join(root, 'plan.json'), plan)).rejects.toThrow(/outside/);
    const external = await fs.mkdtemp(path.join(os.tmpdir(), 'yylo-init-plan-'));
    try {
      const file = path.join(external, 'plan.json'); await writeSimpleInitPlan(file, plan);
      await expect(writeSimpleInitPlan(file, plan)).rejects.toThrow();
      const program = new Command(); configureInitCommand(program);
      const log = vi.spyOn(console, 'log').mockImplementation(() => undefined);
      await program.parseAsync(['init', '--mode', 'simple', '--directory', root], { from: 'user' });
      expect(JSON.parse(String(log.mock.calls[0][0])).outcome).toBe('ready');
      await expect(fs.stat(path.join(root, '.juno_task'))).rejects.toThrow();
      await program.parseAsync(['init', '--mode', 'simple', '--apply-plan', file], { from: 'user' });
      expect(JSON.parse(String(log.mock.calls[1][0])).outcome).toBe('initialized');
    } finally { await fs.rm(external, { recursive: true, force: true }); }
  });
});
