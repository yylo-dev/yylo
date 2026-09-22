import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import fs from 'fs-extra';
import path from 'node:path';
import os from 'node:os';
import { createHash } from 'node:crypto';
import { assessControllerGeneration, ensureControllerGeneration, upgradeControllerGeneration, prepareInstalledControllerRepair,
  admitControllerCommand, reuseControllerCommandAdmission } from '../controller-generation-startup.js';
import { assertExternalGenerationPlan, generationCommandKind, generationInvocationContext, prepareControllerCommand } from '../controller-generation-command.js';
import { Command } from 'commander';

const engine = vi.hoisted(() => ({ acquire: vi.fn(), release: vi.fn(), held: vi.fn(), recheck: vi.fn(), plan: vi.fn(), apply: vi.fn(), recover: vi.fn(), ready: vi.fn(), active: vi.fn(), discover: vi.fn(), retain: vi.fn() }));
vi.mock('../controller-generation-migration.js', () => ({
  GENERATION_MIGRATION_ROOT: '.juno_task/runtime/generation-migration',
  prepareControllerGeneration: engine.plan, applyControllerGeneration: engine.apply,
  recoverControllerGeneration: engine.recover, assertControllerGenerationReady: engine.ready,
  packagedGenerationRoot: () => '/package',
  discoverInstalledGeneration: engine.discover,
  retainInstalledGeneration: engine.retain,
  checkActiveControllerGeneration: engine.active,
  acquireControllerGenerationReadLease: engine.acquire,
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
    const evidence = { root: candidate, artifact: path.join(root, 'candidate.tgz'), sha256: 'b'.repeat(64) };
    engine.plan.mockResolvedValue({ id, controller, candidate: evidence, previous: { ...evidence, root: previous }, before: {}, after: {} });
    engine.retain.mockResolvedValue({ evidence });
    engine.apply.mockResolvedValue({ id, outcome: 'completed' });
    engine.ready.mockResolvedValue(undefined);
    engine.active.mockImplementation(async () => ({ controller, projection: controller,
      executable: path.join(candidate, 'dist/bin/cli.mjs') }));
    engine.acquire.mockResolvedValue(Object.assign(engine.release, {
      assertHeld: engine.held, assessActive: engine.active, recheckActive: engine.recheck,
    }));
    engine.recheck.mockResolvedValue(true);
  });
  afterEach(async () => {
    if (oldCache === undefined) delete process.env.npm_config_cache;
    else process.env.npm_config_cache = oldCache;
    await fs.remove(root);
    vi.unstubAllEnvs();
  });

  it.each([{ args: [] }, { args: ['pi'] }])('leaves a neutral agent root below an unrelated marker unmanaged: $args', async ({ args }) => {
    for (const key of ['JUNO_TASK_ROOT', 'JUNO_CONTROLLER_BRANCH', 'JUNO_WORKSPACE_ROLE']) vi.stubEnv(key, '');
    await fs.ensureDir(path.join(root, '.juno_task'));
    const neutral = path.join(root, 'review/agent-root');
    await fs.ensureDir(neutral);
    await expect(prepareControllerCommand(neutral, args, args)).resolves.toBe(false);
    expect(engine.acquire).not.toHaveBeenCalled();
    expect(engine.plan).not.toHaveBeenCalled();
    expect(await fs.readdir(neutral)).toEqual([]);
    vi.stubEnv('JUNO_TASK_ROOT', controller);
    await expect(prepareControllerCommand(neutral, args, args)).rejects.toThrow('cannot inherit controller authority');
  });

  it('refuses malformed workspace registration rather than treating it as neutral', async () => {
    for (const key of ['JUNO_TASK_ROOT', 'JUNO_CONTROLLER_BRANCH', 'JUNO_WORKSPACE_ROLE']) vi.stubEnv(key, '');
    await fs.outputJson(path.join(controller, '.juno_task/config.json'), { controllerWorkspace: { mode: 'invalid' } });
    await expect(prepareControllerCommand(controller, ['pi'], ['pi'])).rejects.toThrow();
    expect(engine.acquire).not.toHaveBeenCalled();
  });

  it('refuses plan destinations inside even malformed repositories without interpreting Git errors', async () => {
    const external = path.join(root, 'private-plan.json');
    await expect(assertExternalGenerationPlan(external)).resolves.toBeUndefined();
    await fs.outputFile(path.join(root, '.git'), 'malformed gitfile');
    await expect(assertExternalGenerationPlan(external)).rejects.toThrow('outside Git');
    await fs.remove(path.join(root, '.git'));
    await fs.outputFile(path.join(root, 'HEAD'), 'malformed bare repo');
    await expect(assertExternalGenerationPlan(external)).rejects.toThrow('outside Git');
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

  it('discovers global npm packages without a hidden lock through full engine authentication', async () => {
    candidate = path.join(root, 'global/lib/node_modules/@yylo/cli');
    await fs.outputJson(path.join(candidate, 'package.json'), { name: '@yylo/cli', version: '0.2.5' });
    const evidence = { root: candidate, artifact: path.join(root, 'cached.tgz'), sha256: 'c'.repeat(64) };
    engine.discover.mockResolvedValue({ evidence });
    expect((await assessControllerGeneration(controller, candidate)).disposition).toBe('migration_required');
    expect(engine.plan).toHaveBeenCalledWith(controller, evidence, expect.any(Object));
    engine.discover.mockResolvedValue({ evidence: null });
    expect(await assessControllerGeneration(controller, candidate)).toMatchObject({ disposition: 'refused' });
    expect(engine.apply).not.toHaveBeenCalled();
  });

  it('treats equal artifact evidence as equal regardless of JSON key insertion order', async () => {
    await fs.outputJson(path.join(controller, '.juno_task/runtime/generation-migration/current.json'), {
      candidate: { sha256: 'b'.repeat(64), artifact: path.join(root, 'candidate.tgz'), root: candidate },
    });
    expect((await assessControllerGeneration(controller, candidate)).disposition).toBe('ready');
    expect(engine.apply).not.toHaveBeenCalled();
  });

  it('admits the selected active root without candidate discovery, planning or mutation', async () => {
    await fs.outputJson(path.join(controller, '.juno_task/runtime/generation-migration/current.json'), {
      candidate: { root: candidate },
    });
    await fs.outputJson(path.join(candidate, 'package.json'), {
      yyloControllerGeneration: { ordinaryDispatch: 'explicit-only-v1' },
    });
    expect((await ensureControllerGeneration(controller, candidate)).disposition).toBe('ready');
    expect(engine.active).toHaveBeenCalledOnce();
    for (const operation of [engine.discover, engine.plan, engine.apply, engine.recover, engine.retain]) {
      expect(operation).not.toHaveBeenCalled();
    }
  });

  it('does not fall through to upgrade when active authentication refuses', async () => {
    await fs.outputJson(path.join(controller, '.juno_task/runtime/generation-migration/current.json'), {
      candidate: { root: candidate },
    });
    engine.active.mockRejectedValue(new Error('active_pin_unverified: attempt pin'));
    expect(await assessControllerGeneration(controller, candidate)).toMatchObject({ disposition: 'refused', code: 'active_pin_unverified' });
    expect(engine.plan).not.toHaveBeenCalled(); expect(engine.apply).not.toHaveBeenCalled();
  });

  it('refuses if active admission names a different executable after the root observation', async () => {
    await fs.outputJson(path.join(controller, '.juno_task/runtime/generation-migration/current.json'), {
      candidate: { root: candidate },
    });
    engine.active.mockResolvedValue({ executable: '/different/runtime/cli.mjs' });
    expect(await assessControllerGeneration(controller, candidate)).toMatchObject({ disposition: 'refused', code: 'generation_changed_before_dispatch' });
    expect(engine.plan).not.toHaveBeenCalled();
  });

  it('applies an authenticated engine plan only through explicit upgrade', async () => {
    expect((await upgradeControllerGeneration(controller, candidate)).disposition).toBe('ready');
    expect(engine.apply).toHaveBeenCalledWith(controller, expect.objectContaining({ id }));
    expect(engine.ready).toHaveBeenCalledWith(controller);
  });
  it('reuses a retained copy for the same global artifact without migrating back to a mutable npm path', async () => {
    const previous = path.join(root, 'previous');
    await fs.outputJson(path.join(controller, '.juno_task/runtime/generation-migration/current.json'), {
      candidate: { root: previous, artifact: path.join(root, 'previous.tgz'), sha256: 'b'.repeat(64) },
    });
    expect((await assessControllerGeneration(controller, candidate)).disposition).toBe('ready');
    expect(engine.plan).toHaveBeenCalledWith(controller, expect.objectContaining({ root: candidate }), expect.any(Object));
    expect(engine.plan).toHaveBeenLastCalledWith(controller, expect.objectContaining({ root: previous }), expect.objectContaining({ root: previous }));
    expect(engine.retain).not.toHaveBeenCalled();
  });

  it('binds explicit repair plans to the retained candidate before owner review', async () => {
    const retained = { root: path.join(root, 'immutable'), artifact: path.join(root, 'retained.tgz'), sha256: 'b'.repeat(64) };
    engine.retain.mockResolvedValue({ evidence: retained });
    await prepareInstalledControllerRepair(controller, candidate);
    expect(engine.plan).toHaveBeenNthCalledWith(1, controller, expect.objectContaining({ root: candidate }), expect.any(Object), true);
    expect(engine.retain).toHaveBeenCalledOnce();
    expect(engine.plan).toHaveBeenLastCalledWith(controller, retained, expect.any(Object), true);
    expect(engine.apply).not.toHaveBeenCalled();
  });

  it('never applies unknown or customized state', async () => {
    engine.plan.mockRejectedValue(new Error('managed_preimage_modified: .juno_task/scripts/task_workspace.py'));
    const result = await assessControllerGeneration(controller, candidate);
    expect(result).toMatchObject({ disposition: 'refused', code: 'managed_preimage_modified' });
    await expect(upgradeControllerGeneration(controller, candidate)).rejects.toThrow('managed_preimage_modified');
    expect(engine.apply).not.toHaveBeenCalled();
  });
  it('resumes a valid owned interrupted journal but doctor leaves it untouched', async () => {
    const fence = path.join(controller, '.juno_task/runtime/generation-migration/fence.json');
    await fs.outputJson(fence, { id, schema_version: 'yylo_controller_generation_transaction.v1' });
    expect((await assessControllerGeneration(controller, candidate)).disposition).toBe('transition_incomplete');
    expect(engine.recover).not.toHaveBeenCalled();
    engine.recover.mockImplementation(async () => { await fs.remove(fence); return { outcome: 'completed' }; });
    expect((await upgradeControllerGeneration(controller, candidate)).disposition).toBe('ready');
    expect(engine.recover).toHaveBeenCalledWith(controller, id);
  });
  it('rolls back only its own failed activation, not a competing writer', async () => {
    engine.apply.mockImplementation(async () => {
      await fs.outputJson(path.join(controller, '.juno_task/runtime/generation-migration/fence.json'), { id });
      throw new Error('operational_readback_failed');
    });
    engine.recover.mockResolvedValue({ outcome: 'rolled_back' });
    await expect(upgradeControllerGeneration(controller, candidate)).rejects.toThrow('operational_readback_failed');
    expect(engine.recover).toHaveBeenCalledWith(controller, id, true);
  });
  it('does not erase or repair malformed fence authority', async () => {
    await fs.outputJson(path.join(controller, '.juno_task/runtime/generation-migration/fence.json'), { id: '../foreign' });
    await expect(upgradeControllerGeneration(controller, candidate)).rejects.toThrow('generation_transition_invalid');
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
    expect(await assessControllerGeneration(controller, candidate)).toMatchObject({ disposition: 'retained', executable: path.join(previous, 'dist/bin/cli.mjs') });
    await fs.writeFile(path.join(previous, 'dist/bin/cli.mjs'), 'tampered executable');
    expect((await assessControllerGeneration(controller, candidate)).disposition).toBe('refused');
    expect(engine.apply).not.toHaveBeenCalled();
  });

  it('reuses only a live same-root command guard with checked unchanged inputs', async () => {
    await fs.outputJson(path.join(controller, '.juno_task/runtime/generation-migration/current.json'), { candidate: { root: candidate } });
    await fs.outputJson(path.join(candidate, 'package.json'), { yyloControllerGeneration: { ordinaryDispatch: 'explicit-only-v1' } });
    expect(await reuseControllerCommandAdmission(controller, candidate)).toBe(false);
    const admission = await admitControllerCommand(controller, candidate);
    try {
      expect(engine.active).toHaveBeenCalledOnce();
      expect(await reuseControllerCommandAdmission(controller, '/other-package')).toBe(false);
      expect(await reuseControllerCommandAdmission('/other-controller', candidate)).toBe(false);
      expect(engine.recheck).not.toHaveBeenCalled();
      expect(await reuseControllerCommandAdmission(controller, candidate)).toBe(true);
      expect(engine.active).toHaveBeenCalledOnce();
      expect(engine.recheck).toHaveBeenCalledOnce();
      engine.recheck.mockResolvedValueOnce(false);
      expect(await reuseControllerCommandAdmission(controller, candidate)).toBe(false);
      expect(await reuseControllerCommandAdmission(controller, candidate)).toBe(false);
      expect(engine.recheck).toHaveBeenCalledTimes(2);
    } finally { await admission.release(); }
    expect(await reuseControllerCommandAdmission(controller, candidate)).toBe(false);
  });

  it('invalidates reuse when Git environment changes during a checked readback', async () => {
    await fs.outputJson(path.join(controller, '.juno_task/runtime/generation-migration/current.json'), { candidate: { root: candidate } });
    await fs.outputJson(path.join(candidate, 'package.json'), { yyloControllerGeneration: { ordinaryDispatch: 'explicit-only-v1' } });
    const original = process.env.GIT_CONFIG_GLOBAL;
    const admission = await admitControllerCommand(controller, candidate);
    try {
      engine.recheck.mockImplementationOnce(async () => {
        process.env.GIT_CONFIG_GLOBAL = '/changed-global-config';
        return true;
      });
      expect(await reuseControllerCommandAdmission(controller, candidate)).toBe(false);
      expect(await reuseControllerCommandAdmission(controller, candidate)).toBe(false);
      expect(engine.recheck).toHaveBeenCalledOnce();
    } finally {
      if (original === undefined) delete process.env.GIT_CONFIG_GLOBAL;
      else process.env.GIT_CONFIG_GLOBAL = original;
      await admission.release();
    }
  });

  it('does not turn guard loss into a reusable assessment', async () => {
    await fs.outputJson(path.join(controller, '.juno_task/runtime/generation-migration/current.json'), { candidate: { root: candidate } });
    await fs.outputJson(path.join(candidate, 'package.json'), { yyloControllerGeneration: { ordinaryDispatch: 'explicit-only-v1' } });
    const admission = await admitControllerCommand(controller, candidate);
    try {
      engine.held.mockImplementationOnce(() => { throw new Error('generation_read_lock_lost'); });
      await expect(reuseControllerCommandAdmission(controller, candidate)).rejects.toThrow('generation_read_lock_lost');
      expect(engine.recheck).not.toHaveBeenCalled();
    } finally { await admission.release(); }
  });

  it('ordinary admission refuses legacy controllers without discovering or mutating anything', async () => {
    await expect(ensureControllerGeneration(controller, candidate)).rejects.toThrow('generation_explicit_upgrade_required');
    for (const operation of [engine.discover, engine.plan, engine.apply, engine.recover, engine.retain]) {
      expect(operation).not.toHaveBeenCalled();
    }
  });

  it('admits only the exact alias reported by the locked active assessment', async () => {
    const selected = path.join(root, 'previous');
    await fs.outputJson(path.join(controller, '.juno_task/runtime/generation-migration/current.json'), { candidate: { root: selected } });
    await fs.outputJson(path.join(selected, 'package.json'), { yyloControllerGeneration: { ordinaryDispatch: 'explicit-only-v1' } });
    engine.active.mockResolvedValue({ controller, projection: controller, executable: path.join(selected, 'dist/bin/cli.mjs'),
      equivalent_executable: path.join(candidate, 'dist/bin/cli.mjs') });
    const admission = await admitControllerCommand(controller, candidate);
    try {
      expect(admission.assessment.disposition).toBe('ready');
      expect(await reuseControllerCommandAdmission(controller, candidate)).toBe(true);
      engine.recheck.mockResolvedValueOnce(false);
      expect(await reuseControllerCommandAdmission(controller, candidate)).toBe(false);
    } finally { await admission.release(); }
    engine.active.mockResolvedValue({ controller, projection: controller, executable: path.join(selected, 'dist/bin/cli.mjs'),
      equivalent_executable: '/different/package/dist/bin/cli.mjs' });
    expect((await ensureControllerGeneration(controller, candidate)).disposition).toBe('retained');
    for (const operation of [engine.discover, engine.plan, engine.apply, engine.recover, engine.retain]) {
      expect(operation).not.toHaveBeenCalled();
    }
  });

  it('ordinary admission authenticates only the selected runtime when global differs', async () => {
    const selected = path.join(root, 'previous');
    await fs.outputJson(path.join(controller, '.juno_task/runtime/generation-migration/current.json'), {
      candidate: { root: selected },
    });
    await fs.outputJson(path.join(selected, 'package.json'), {
      yyloControllerGeneration: { ordinaryDispatch: 'explicit-only-v1' },
    });
    engine.active.mockResolvedValue({ controller, projection: controller, executable: path.join(selected, 'dist/bin/cli.mjs') });
    expect(await ensureControllerGeneration(controller, candidate)).toMatchObject({ disposition: 'retained', executable: path.join(selected, 'dist/bin/cli.mjs') });
    for (const operation of [engine.discover, engine.plan, engine.apply, engine.recover, engine.retain]) {
      expect(operation).not.toHaveBeenCalled();
    }
    // An older package cannot be reentered just because its bytes authenticate.
    await fs.outputJson(path.join(selected, 'package.json'), { name: '@yylo/cli', version: '0.2.3' });
    await expect(ensureControllerGeneration(controller, candidate)).rejects.toThrow('selected legacy runtime');
  });

  it('ordinary admission preserves interrupted transactions and primary refusal', async () => {
    engine.ready.mockRejectedValueOnce(new Error('generation_transition_incomplete: exact journal'));
    await expect(ensureControllerGeneration(controller, candidate)).rejects.toThrow('generation_transition_incomplete');
    expect(engine.active).not.toHaveBeenCalled();
    for (const operation of [engine.discover, engine.plan, engine.apply, engine.recover, engine.retain]) {
      expect(operation).not.toHaveBeenCalled();
    }
  });

  it('keeps diagnostics read-only and explicit maintenance separate', () => {
    for (const args of [['task', 'status'], ['task', 'admission'], ['task', 'preflight'],
      ['task', 'lease-status'], ['task', 'evidence-status'], ['integration', 'status'],
      ['info'], ['where'], ['capabilities']]) {
      expect(generationCommandKind(args)).toBe('read');
    }
    for (const args of [['scripts', 'doctor'], ['integration', 'runtime-doctor'], ['doctor', 'workspace'], ['task', 'doctor']]) {
      expect(generationCommandKind(args)).toBe('diagnostic');
    }
    expect(generationCommandKind(['task', 'start'])).toBe('execute');
    for (const args of [[], ['pi'], ['claude'], ['cursor'], ['codex'], ['gemini'], ['cn']]) {
      expect(generationCommandKind(args)).toBe('execute');
    }
    expect(generationCommandKind(['scripts', 'generation', 'rollback'])).toBe('maintenance');
    expect(generationCommandKind(['migrate', 'runtime-install-rebind'])).toBe('skip');
  });

  it('suppresses notices using parsed options, never prompt contents', () => {
    const program = new Command().option('-q, --quiet').option('-p, --prompt <text>');
    program.command('pi').option('-q, --quiet').option('-p, --prompt <text>');
    const context = (argv: string[]) => generationInvocationContext(program, argv, '/launcher');
    expect(context(['pi', '--quiet', '-p', 'hello']).quiet).toBe(true);
    expect(context(['pi', '-p', '--quiet']).quiet).toBeUndefined();
    expect(context(['pi', '--', '--quiet']).quiet).toBeUndefined();
  });

  it('routes the actual agent cwd using option arity without mutating the execution parser', () => {
    const program = new Command().version('1.0.0').option('--config <path>').option('-w, --cwd <path>');
    const pi = program.command('pi').option('-w, --cwd <path>').option('-f, --prompt-file <path>');
    const parse = (args: string[]) => {
      const { cwd, version } = generationInvocationContext(program, args, '/launcher');
      return { cwd, version };
    };
    expect(parse(['pi', '-f', '--cwd=/not-a-directory-option'])).toEqual({ cwd: '/launcher', version: false });
    expect(parse(['pi', '--', '--cwd=/payload'])).toEqual({ cwd: '/launcher', version: false });
    expect(parse(['pi', '-w', '/first', '--cwd=/actual'])).toEqual({ cwd: '/actual', version: false });
    expect(parse(['pi', '--config', '--cwd=/config-value', '-w', '../neutral'])).toEqual({ cwd: '/neutral', version: false });
    expect(parse(['pi', '-f', '--version']).version).toBe(false);
    expect(parse(['pi', '--version']).version).toBe(true);
    expect(pi.opts()).toEqual({});
    expect(program.opts()).toEqual({});
    program.option('-s, --subagent <name>').option('-p, --prompt [text]').option('--execution-envelope');
    for (const args of [
      ['-s', 'pi', '-p', 'hello', '-w', '/controller'],
      ['--execution-envelope', 'pi', '-w', '/controller'],
    ]) {
      const selected = generationInvocationContext(program, args, '/launcher');
      expect(selected.cwd).toBe('/controller');
      expect(generationCommandKind(selected.commandArgs)).toBe('execute');
    }
  });
});
