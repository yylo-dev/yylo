import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { EventEmitter } from 'node:events';
import { prepareControllerCommand, releaseControllerCommand } from '../controller-generation-command.js';

const mocks = vi.hoisted(() => ({
  resolve: vi.fn(), metadata: vi.fn(), ensure: vi.fn(), assess: vi.fn(),
  acquire: vi.fn(), release: vi.fn(), spawn: vi.fn(),
}));
vi.mock('node:child_process', () => ({ spawn: mocks.spawn }));
vi.mock('fs-extra', () => ({ default: { pathExists: vi.fn(async () => true) } }));
vi.mock('../controller-resolver.js', () => ({ resolveController: mocks.resolve }));
vi.mock('../script-installer.js', () => ({ ScriptInstaller: { isMetadataOnlyController: mocks.metadata } }));
vi.mock('../controller-generation-startup.js', () => ({
  ensureControllerGeneration: mocks.ensure, assessControllerGeneration: mocks.assess,
  prepareInstalledControllerRepair: vi.fn(),
}));
vi.mock('../controller-generation-migration.js', () => ({
  packagedGenerationRoot: () => '/retained', acquireControllerGenerationReadLease: mocks.acquire,
  applyControllerGeneration: vi.fn(), recoverControllerGeneration: vi.fn(),
  GENERATION_MIGRATION_ROOT: '.juno_task/generation',
}));

const ready = { disposition: 'ready', controller: '/controller' };
const retained = { disposition: 'retained', controller: '/controller', executable: '/retained/dist/bin/cli.mjs', reason: 'candidate unavailable' };
const invoke = () => prepareControllerCommand('/controller', ['task', 'start'], ['task', 'start', 'TASK01']);
let saved: NodeJS.ProcessEnv;
let exitCode: typeof process.exitCode;

beforeEach(() => {
  saved = { ...process.env }; exitCode = process.exitCode;
  delete process.env.YYLO_GENERATION_REDISPATCH;
  mocks.resolve.mockReturnValue({ valid: true, role: 'controller', source: 'registration', path: '/controller' });
  mocks.metadata.mockResolvedValue(true);
  mocks.ensure.mockResolvedValue(ready); mocks.assess.mockResolvedValue(ready);
  mocks.release.mockResolvedValue(undefined); mocks.acquire.mockResolvedValue(mocks.release);
  vi.spyOn(console, 'error').mockImplementation(() => {});
});
afterEach(async () => {
  await releaseControllerCommand();
  process.env = saved; process.exitCode = exitCode; vi.restoreAllMocks();
});

describe('retained dispatch marker lifetime', () => {
  it('ends the hop only after authenticated admission and locked readback', async () => {
    process.env.YYLO_GENERATION_REDISPATCH = retained.executable;
    mocks.ensure.mockImplementation(async () => {
      expect(process.env.YYLO_GENERATION_REDISPATCH).toBe(retained.executable); return ready;
    });
    mocks.assess.mockImplementation(async () => {
      expect(mocks.acquire).toHaveBeenCalledOnce();
      expect(process.env.YYLO_GENERATION_REDISPATCH).toBe(retained.executable); return ready;
    });
    expect(await invoke()).toBe(false);
    expect(process.env.YYLO_GENERATION_REDISPATCH).toBeUndefined();
    expect(mocks.ensure).toHaveBeenCalledOnce(); expect(mocks.assess).toHaveBeenCalledOnce();
    expect(mocks.spawn).not.toHaveBeenCalled();
    // The command still owns its read lease; only the hop marker was consumed.
    expect(mocks.release).not.toHaveBeenCalled();
  });

  it('allows a new agent tool command to make its own single retained hop', async () => {
    process.env.YYLO_GENERATION_REDISPATCH = retained.executable;
    await invoke();
    await releaseControllerCommand();
    mocks.ensure.mockResolvedValue(retained); mocks.assess.mockResolvedValue(retained);
    mocks.spawn.mockImplementation((_node, _argv, options) => {
      expect(options.env.YYLO_GENERATION_REDISPATCH).toBe(retained.executable);
      const child = new EventEmitter(); queueMicrotask(() => child.emit('exit', 17, null)); return child;
    });
    expect(await invoke()).toBe(true);
    expect(mocks.spawn).toHaveBeenCalledOnce();
    expect(mocks.spawn).toHaveBeenCalledWith(process.execPath,
      [retained.executable, 'task', 'start', 'TASK01'], expect.objectContaining({ cwd: '/controller', stdio: 'inherit' }));
    expect(process.exitCode).toBe(17);
  });

  it('still refuses a second hop before an admitted command boundary', async () => {
    process.env.YYLO_GENERATION_REDISPATCH = retained.executable;
    mocks.ensure.mockResolvedValue(retained); mocks.assess.mockResolvedValue(retained);
    await expect(invoke()).rejects.toThrow('generation_dispatch_cycle');
    expect(process.env.YYLO_GENERATION_REDISPATCH).toBe(retained.executable);
    expect(mocks.spawn).not.toHaveBeenCalled(); expect(mocks.release).toHaveBeenCalledOnce();
  });

  it('does not consume the guard when authentication refuses', async () => {
    process.env.YYLO_GENERATION_REDISPATCH = retained.executable;
    mocks.ensure.mockRejectedValue(new Error('package_provenance_invalid'));
    await expect(invoke()).rejects.toThrow('package_provenance_invalid');
    expect(process.env.YYLO_GENERATION_REDISPATCH).toBe(retained.executable);
    expect(mocks.acquire).not.toHaveBeenCalled(); expect(mocks.spawn).not.toHaveBeenCalled();
  });

  it('does not consume the guard on a post-lock generation race', async () => {
    process.env.YYLO_GENERATION_REDISPATCH = retained.executable;
    mocks.assess.mockResolvedValue(retained);
    await expect(invoke()).rejects.toThrow('generation_changed_before_dispatch');
    expect(process.env.YYLO_GENERATION_REDISPATCH).toBe(retained.executable);
    expect(mocks.spawn).not.toHaveBeenCalled(); expect(mocks.release).toHaveBeenCalledOnce();
  });
});
