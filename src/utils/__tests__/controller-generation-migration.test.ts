import { execFileSync } from 'node:child_process';
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
      await expect(applyControllerGeneration(root, {} as ControllerGenerationPlan)).rejects.toThrow('generation_migration_busy');
      await expect(withControllerGenerationMutation(root, async () => undefined)).rejects.toThrow('generation_migration_busy');
    } finally { await nested(); await first(); }
    await withControllerGenerationMutation(root, async () => undefined);
  });

  it('passes registered real-Git historical/provenance/crash/rollback fixtures', () => {
    const script = path.resolve('src/templates/maintenance/tests/test_controller_generation_migration.py');
    const output = execFileSync('python3', [script], { encoding: 'utf8', timeout: 600_000, maxBuffer: 1024 * 1024 });
    expect(output).not.toContain('FAILED');
  }, 610_000);
});
