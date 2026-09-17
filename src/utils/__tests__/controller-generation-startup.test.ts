import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import fs from 'fs-extra';
import path from 'node:path';
import os from 'node:os';
import { createHash } from 'node:crypto';
import { assessControllerGeneration, ensureControllerGeneration } from '../controller-generation-startup.js';
import { generationCommandKind } from '../controller-generation-command.js';

const engine = vi.hoisted(() => ({ plan: vi.fn(), apply: vi.fn(), recover: vi.fn(), ready: vi.fn() }));
vi.mock('../controller-generation-migration.js', () => ({
  GENERATION_MIGRATION_ROOT: '.juno_task/runtime/generation-migration',
  prepareControllerGeneration: engine.plan, applyControllerGeneration: engine.apply,
  recoverControllerGeneration: engine.recover, assertControllerGenerationReady: engine.ready,
  packagedGenerationRoot: () => '/package',
}));

describe('operation-specific first-use generation dispatch', () => {
  let root: string, controller: string, candidate: string;
  let oldCache: string | undefined;
  const id = 'a'.repeat(64);
  beforeEach(async () => {
    vi.clearAllMocks();
    oldCache = process.env.npm_config_cache;
    root = await fs.mkdtemp(path.join(os.tmpdir(), 'generation-startup-'));
    controller = path.join(root, 'controller'); candidate = path.join(root, 'candidate');
    const previous = path.join(root, 'previous');
    for (const pkg of [candidate, previous]) {
      await fs.outputJson(path.join(pkg, '.yylo-generation-evidence.json'), {
        root: pkg, artifact: path.join(root, `${path.basename(pkg)}.tgz`), sha256: 'b'.repeat(64),
      });
    }
    await fs.outputJson(path.join(controller, '.juno_task/runtime/identity.json'), { executable: path.join(previous, 'dist/bin/cli.mjs') });
    engine.plan.mockResolvedValue({ id, controller, before: {}, after: {} });
    engine.apply.mockResolvedValue({ id, outcome: 'completed' });
    engine.ready.mockResolvedValue(undefined);
  });
  afterEach(async () => {
    if (oldCache === undefined) delete process.env.npm_config_cache;
    else process.env.npm_config_cache = oldCache;
    await fs.remove(root);
  });

  it('doctor assesses the same migration without writes or apply', async () => {
    const before = await fs.readFile(path.join(controller, '.juno_task/runtime/identity.json'));
    expect((await assessControllerGeneration(controller, candidate)).disposition).toBe('migration_required');
    expect(engine.apply).not.toHaveBeenCalled();
    expect(await fs.readFile(path.join(controller, '.juno_task/runtime/identity.json'))).toEqual(before);
  });
  it('recognizes ordinary npm installation receipts only with the exact offline artifact', async () => {
    candidate = path.join(root, 'installed/node_modules/@yylo/cli');
    await fs.outputJson(path.join(candidate, 'package.json'), { name: '@yylo/cli', version: '0.2.4' });
    const bytes = Buffer.from('opaque tarball authenticated by the shared engine');
    const sha512 = createHash('sha512').update(bytes).digest();
    await fs.outputJson(path.join(root, 'installed/node_modules/.package-lock.json'), { packages: {
      'node_modules/@yylo/cli': { version: '0.2.4', integrity: `sha512-${sha512.toString('base64')}` },
    } });
    process.env.npm_config_cache = path.join(root, 'offline-cache');
    const digest = sha512.toString('hex');
    const artifact = path.join(process.env.npm_config_cache, '_cacache/content-v2/sha512', digest.slice(0, 2), digest.slice(2, 4), digest.slice(4));
    await fs.outputFile(artifact, bytes);
    expect((await assessControllerGeneration(controller, candidate)).disposition).toBe('migration_required');
    expect(engine.plan).toHaveBeenCalledWith(controller, { root: candidate, artifact,
      sha256: createHash('sha256').update(bytes).digest('hex') }, expect.any(Object));
    await fs.writeFile(artifact, 'corrupt cache');
    expect(await assessControllerGeneration(controller, candidate)).toMatchObject({ disposition: 'refused', code: 'package_provenance_invalid' });
    expect(engine.apply).not.toHaveBeenCalled();
  });

  it('automatically applies an authenticated engine plan before execution', async () => {
    expect((await ensureControllerGeneration(controller, candidate)).disposition).toBe('ready');
    expect(engine.apply).toHaveBeenCalledWith(controller, expect.objectContaining({ id }));
    expect(engine.ready).toHaveBeenCalledWith(controller);
  });
  it('never applies unknown or customized state', async () => {
    engine.plan.mockRejectedValue(new Error('managed_preimage_modified: .juno_task/scripts/task_workspace.py'));
    const result = await assessControllerGeneration(controller, candidate);
    expect(result).toMatchObject({ disposition: 'refused', code: 'managed_preimage_modified' });
    await expect(ensureControllerGeneration(controller, candidate)).rejects.toThrow('managed_preimage_modified');
    expect(engine.apply).not.toHaveBeenCalled();
  });
  it('resumes a valid owned interrupted journal but doctor leaves it untouched', async () => {
    const fence = path.join(controller, '.juno_task/runtime/generation-migration/fence.json');
    await fs.outputJson(fence, { id, schema_version: 'yylo_controller_generation_transaction.v1' });
    expect((await assessControllerGeneration(controller, candidate)).disposition).toBe('transition_incomplete');
    expect(engine.recover).not.toHaveBeenCalled();
    engine.recover.mockImplementation(async () => { await fs.remove(fence); return { outcome: 'completed' }; });
    expect((await ensureControllerGeneration(controller, candidate)).disposition).toBe('ready');
    expect(engine.recover).toHaveBeenCalledWith(controller, id);
  });
  it('rolls back only its own failed activation, not a competing writer', async () => {
    engine.apply.mockImplementation(async () => {
      await fs.outputJson(path.join(controller, '.juno_task/runtime/generation-migration/fence.json'), { id });
      throw new Error('operational_readback_failed');
    });
    engine.recover.mockResolvedValue({ outcome: 'rolled_back' });
    await expect(ensureControllerGeneration(controller, candidate)).rejects.toThrow('operational_readback_failed');
    expect(engine.recover).toHaveBeenCalledWith(controller, id, true);
  });
  it('does not erase or repair malformed fence authority', async () => {
    await fs.outputJson(path.join(controller, '.juno_task/runtime/generation-migration/fence.json'), { id: '../foreign' });
    await expect(ensureControllerGeneration(controller, candidate)).rejects.toThrow('generation_transition_invalid');
    expect(engine.recover).not.toHaveBeenCalled();
  });
  it('offers retained dispatch only for a byte-verified existing package and controller', async () => {
    const previous = path.join(root, 'previous');
    await fs.remove(path.join(candidate, '.yylo-generation-evidence.json'));
    const hash = (value: string) => createHash('sha256').update(value).digest('hex');
    await fs.outputFile(path.join(previous, 'dist/bin/cli.mjs'), 'old executable');
    await fs.outputFile(path.join(previous, 'dist/templates/scripts/task_workspace.py'), 'old script');
    await fs.outputJson(path.join(previous, 'package.json'), { name: '@yylo/cli', version: '0.2.3' });
    await fs.outputFile(path.join(controller, '.juno_task/scripts/task_workspace.py'), 'old script');
    await fs.outputJson(path.join(controller, '.juno_task/runtime/identity.json'), {
      executable: path.join(previous, 'dist/bin/cli.mjs'), executable_sha256: hash('old executable'),
      source: 'installed-release', tracked: false, package: '@yylo/cli', version: '0.2.3',
    });
    await fs.outputJson(path.join(controller, '.juno_task/managed-assets.json'), {
      schemaVersion: 1, packageName: '@yylo/cli', packageVersion: '0.2.3', assets: {
        '.juno_task/scripts/task_workspace.py': { type: 'script', templateVersion: '0.2.3', sourceSha256: hash('old script'), installedSha256: hash('old script') },
      },
    });
    await fs.outputJson(path.join(controller, '.juno_task/runtime/managed-controller/generation.json'), {
      schema_version: 'juno_managed_controller_runtime.v1', scripts: {
        '.juno_task/scripts/task_workspace.py': { classification: 'exact', source_sha256: hash('old script'), actual_sha256: hash('old script') },
      },
    });
    expect(await ensureControllerGeneration(controller, candidate)).toMatchObject({ disposition: 'retained', executable: path.join(previous, 'dist/bin/cli.mjs') });
    await fs.writeFile(path.join(previous, 'dist/bin/cli.mjs'), 'tampered executable');
    expect((await assessControllerGeneration(controller, candidate)).disposition).toBe('refused');
    expect(engine.apply).not.toHaveBeenCalled();
  });

  it('keeps diagnostics read-only and explicit maintenance separate', () => {
    for (const args of [['task', 'status'], ['scripts', 'doctor'], ['integration', 'runtime-doctor'], ['doctor', 'workspace']]) {
      expect(generationCommandKind(args)).toBe('read');
    }
    expect(generationCommandKind(['task', 'start'])).toBe('execute');
    expect(generationCommandKind(['pi'])).toBe('execute');
    expect(generationCommandKind(['scripts', 'generation', 'rollback'])).toBe('maintenance');
    expect(generationCommandKind(['migrate', 'runtime-install-rebind'])).toBe('skip');
  });
});
