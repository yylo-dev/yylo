import { execFileSync } from 'node:child_process';
import * as os from 'node:os';
import * as path from 'node:path';
import fs from 'fs-extra';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import {
  assertControllerGenerationReady, GENERATION_MIGRATION_ROOT,
  prepareControllerGeneration, recoverControllerGeneration,
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

  it('passes registered real-Git historical/provenance/crash/rollback fixtures', () => {
    const script = path.resolve('src/templates/scripts/tests/test_controller_generation_migration.py');
    const output = execFileSync('python3', [script], { encoding: 'utf8', timeout: 300_000, maxBuffer: 1024 * 1024 });
    expect(output).not.toContain('FAILED');
  }, 310_000);
});
