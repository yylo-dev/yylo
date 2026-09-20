import { execFileSync } from 'node:child_process';
import * as childProcess from 'node:child_process';
import * as os from 'node:os';
import * as path from 'node:path';
import fs from 'fs-extra';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  assertControllerGenerationReady, GENERATION_MIGRATION_ROOT,
  prepareControllerGeneration, recoverControllerGeneration, applyControllerGeneration,
  withControllerGenerationMutation, acquireControllerGenerationReadLease, type ControllerGenerationPlan,
} from '../controller-generation-migration.js';
import { ScriptInstaller } from '../script-installer.js';
import { ManagedProjectAssets } from '../managed-project-assets.js';

// Native ESM namespace exports are non-configurable; expose a spyable wrapper
// while keeping every subprocess real (including the guard-death regression).
vi.mock('node:child_process', async importOriginal => {
  const actual = await importOriginal<typeof import('node:child_process')>();
  return { ...actual, spawn: (...args: Parameters<typeof actual.spawn>) => actual.spawn(...args) };
});

describe('controller generation maintenance', () => {
  let root: string;
  beforeEach(async () => { root = await fs.mkdtemp(path.join(os.tmpdir(), 'generation-ts-')); });
  afterEach(async () => { await fs.remove(root); });

  it('fences both managed installers before any possible mutation', async () => {
    await assertControllerGenerationReady(root);
    await fs.outputJson(path.join(root, GENERATION_MIGRATION_ROOT, 'fence.json'), { id: 'incomplete' });
    await expect(assertControllerGenerationReady(root)).rejects.toThrow('generation_transition_incomplete');
    await expect(ScriptInstaller.assertManagedControllerPackageUpdateAllowed(root))
      .rejects.toThrow('generation_transition_incomplete');
    await expect(ManagedProjectAssets.preflight(root)).rejects.toThrow('generation_transition_incomplete');
    expect(await fs.pathExists(path.join(root, '.juno_task/managed-assets.json'))).toBe(false);
  });

  it('does not follow a symlinked fence or runtime parent', async () => {
    const foreign = await fs.mkdtemp(path.join(os.tmpdir(), 'generation-foreign-'));
    try {
      await fs.ensureDir(path.join(root, '.juno_task'));
      await fs.symlink(foreign, path.join(root, '.juno_task/runtime'));
      await expect(assertControllerGenerationReady(root)).rejects.toThrow('symbolic-link');
      expect(await fs.readdir(foreign)).toEqual([]);
    } finally { await fs.remove(foreign); }
  });

  it('maintenance refuses unregistered controllers without invoking broken local admission', async () => {
    await fs.outputFile(path.join(root, '.juno_task/scripts/task_workspace.py'), 'raise Exception("OLD LOCAL RUNTIME EXECUTED")');
    const evidence = { root: '/absent/package', artifact: '/absent/archive', sha256: '0'.repeat(64) };
    await expect(prepareControllerGeneration(root, evidence, evidence)).rejects.toThrow('registration_unavailable');
    await expect(recoverControllerGeneration(root, '../escape')).rejects.toThrow('journal_corrupt');
    expect(await fs.pathExists(path.join(root, GENERATION_MIGRATION_ROOT))).toBe(false);
  });

  it('holds the shared generation lock after installer preflight and through failure cleanup', async () => {
    await fs.ensureDir(path.join(root, '.juno_task'));
    let entered!: () => void;
    let release!: () => void;
    const reached = new Promise<void>(resolve => { entered = resolve; });
    const resume = new Promise<void>(resolve => { release = resolve; });
    const preflight = vi.spyOn(ManagedProjectAssets, 'preflight').mockImplementationOnce(async () => {
      entered();
      await resume;
      throw new Error('stop after preflight probe');
    });
    const installing = ManagedProjectAssets.update(root).then(() => null, error => error as Error);
    try {
      await reached;
      await expect(applyControllerGeneration(root, {} as ControllerGenerationPlan)).rejects.toThrow('generation_migration_busy');
      await expect(ScriptInstaller.installScript(root, 'task_workspace.py', true)).rejects.toThrow('generation_migration_busy');
    } finally {
      release();
      expect((await installing)?.message).toContain('stop after preflight probe');
      preflight.mockRestore();
    }
    await withControllerGenerationMutation(root, () => withControllerGenerationMutation(root, async () => {
      await fs.outputFile(path.join(root, 'lock-released'), 'yes');
    }));
    expect(await fs.readFile(path.join(root, 'lock-released'), 'utf8')).toBe('yes');
  }, 60_000);

  it('allows nested readers but excludes all generation writers until execution ends', async () => {
    const first = await acquireControllerGenerationReadLease(root);
    const nested = await acquireControllerGenerationReadLease(root);
    try {
      first.assertHeld(); nested.assertHeld();
      await expect(applyControllerGeneration(root, {} as ControllerGenerationPlan)).rejects.toThrow('generation_migration_busy');
      await expect(withControllerGenerationMutation(root, async () => undefined)).rejects.toThrow('generation_migration_busy');
    } finally { await nested(); await first(); }
    expect(() => first.assertHeld()).toThrow('generation_read_lock_lost');
    expect(() => nested.assertHeld()).toThrow('generation_read_lock_lost');
    await first(); // Releasing twice cannot restore the authority.
    await withControllerGenerationMutation(root, async () => undefined);
  });

  it('never executes valid poisoned sibling bytecode in its reader guard', async () => {
    const templates = path.join(root, 'fixture/dist/templates');
    for (const directory of ['maintenance', 'scripts']) {
      await fs.copy(path.resolve('src/templates', directory), path.join(templates, directory), {
        filter: file => path.basename(file) !== '__pycache__',
      });
    }
    const engine = path.join(templates, 'maintenance/controller_generation_migration.py');
    const sibling = path.join(templates, 'scripts/task_workspace.py');
    const marker = path.join(root, 'poison-executed');
    execFileSync('python3', ['-E', '-B', '-c', `
import os, pathlib, py_compile, sys
source = pathlib.Path(sys.argv[1]); marker = sys.argv[2]
original = source.read_bytes(); stamp = source.stat()
poison = ('from pathlib import Path\\nPath(' + repr(marker) + ').write_text("unsafe")\\n').encode()
assert len(poison) < len(original)
source.write_bytes(poison + b'#' + b' ' * (len(original) - len(poison) - 1))
os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
py_compile.compile(str(source), doraise=True)
source.write_bytes(original)
os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
`, sibling, marker]);
    const realSpawn = childProcess.spawn;
    const spawn = vi.spyOn(childProcess, 'spawn').mockImplementation((command, args, options) =>
      realSpawn(command, args.map(value => value.endsWith('/controller_generation_migration.py') ? engine : value), options));
    let reader: Awaited<ReturnType<typeof acquireControllerGenerationReadLease>> | undefined;
    let cacheRoot: string | undefined;
    try {
      reader = await acquireControllerGenerationReadLease(root);
      reader.assertHeld();
      const argv = spawn.mock.calls[0]![1] as string[];
      expect(argv).toContain('-E'); expect(argv).toContain('-B'); expect(argv).toContain('-X');
      const prefix = argv.find(value => value.startsWith('pycache_prefix='))!;
      cacheRoot = path.dirname(prefix.slice('pycache_prefix='.length));
      expect((await fs.stat(cacheRoot)).mode & 0o777).toBe(0o700);
      expect(await fs.pathExists(marker)).toBe(false);
    } finally { await reader?.(); spawn.mockRestore(); }
    expect(await fs.pathExists(cacheRoot!)).toBe(false);
    expect(await fs.pathExists(marker)).toBe(false);
  });

  it('invalidates admission when its invocation-owned reader guard exits', async () => {
    const spawn = vi.spyOn(childProcess, 'spawn');
    let reader: Awaited<ReturnType<typeof acquireControllerGenerationReadLease>> | undefined;
    try {
      reader = await acquireControllerGenerationReadLease(root);
      const child = spawn.mock.results[0]!.value as childProcess.ChildProcess;
      reader.assertHeld();
      const closed = new Promise<void>(resolve => child.once('close', () => resolve()));
      child.kill('SIGTERM');
      await closed;
      expect(() => reader!.assertHeld()).toThrow('generation_read_lock_lost');
      await withControllerGenerationMutation(root, async () => undefined);
    } finally { await reader?.(); spawn.mockRestore(); }
  });

  it('passes registered real-Git historical/provenance/crash/rollback fixtures', () => {
    const script = path.resolve('src/templates/maintenance/tests/test_controller_generation_migration.py');
    // Source fixtures now authenticate the full readback closure like consumers;
    // all 56 real-Git cases exceeded the former aggregate 600s subprocess budget.
    const output = execFileSync('python3', [script], { encoding: 'utf8', timeout: 1_000_000, maxBuffer: 1024 * 1024 });
    expect(output).not.toContain('FAILED');
  }, 1_010_000);
});
