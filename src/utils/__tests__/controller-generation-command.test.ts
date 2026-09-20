import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { EventEmitter } from 'node:events';
import { prepareControllerCommand, releaseControllerCommand } from '../controller-generation-command.js';

const mocks = vi.hoisted(() => ({
  resolve: vi.fn(), metadata: vi.fn(), ensure: vi.fn(), assess: vi.fn(), admit: vi.fn(),
  acquire: vi.fn(), release: vi.fn(), assertHeld: vi.fn(), spawn: vi.fn(), readiness: vi.fn(),
}));
vi.mock('../controller-generation-readiness.js', () => ({ controllerGenerationReadiness: mocks.readiness }));
vi.mock('node:child_process', () => ({ spawn: mocks.spawn }));
vi.mock('fs-extra', () => ({ default: { pathExists: vi.fn(async () => true) } }));
vi.mock('../controller-resolver.js', () => ({ resolveController: mocks.resolve }));
vi.mock('../script-installer.js', () => ({ ScriptInstaller: { isMetadataOnlyController: mocks.metadata } }));
vi.mock('../controller-generation-startup.js', () => ({
  admitControllerCommand: mocks.admit, assessControllerGeneration: mocks.assess,
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
  mocks.release.mockResolvedValue(undefined); mocks.acquire.mockResolvedValue(Object.assign(mocks.release, { assertHeld: mocks.assertHeld }));
  mocks.admit.mockImplementation(async () => {
    await mocks.acquire();
    try {
      const assessment = await mocks.ensure();
      mocks.assertHeld();
      return { assessment, release: mocks.release };
    } catch (error) { await mocks.release(); throw error; }
  });
  vi.spyOn(console, 'error').mockImplementation(() => {});
});
afterEach(async () => {
  await releaseControllerCommand();
  process.env = saved; process.exitCode = exitCode; vi.restoreAllMocks();
});

describe('ordinary observation', () => {
  it.each([['task', 'status'], ['task', 'admission'], ['task', 'preflight'],
    ['task', 'lease-status'], ['integration', 'status'], ['info'], ['where'], ['capabilities']])(
    'authenticates %s %s without candidate assessment', async (...args) => {
      mocks.assess.mockRejectedValue(new Error('candidate evidence absent'));
      expect(await prepareControllerCommand('/controller', args, [...args, 'TASK01'])).toBe(false);
      expect(mocks.admit).toHaveBeenCalledOnce();
      expect(mocks.assess).not.toHaveBeenCalled();
      expect(mocks.release).not.toHaveBeenCalled();
    });

  it('forwards exact status payload and exit through the retained executable', async () => {
    mocks.ensure.mockResolvedValue(retained);
    mocks.spawn.mockImplementation(() => {
      const child = new EventEmitter(); queueMicrotask(() => child.emit('exit', 23, null)); return child;
    });
    const args = ['task', 'status', 'TASK01', '--format', 'json', '--raw'];
    expect(await prepareControllerCommand('/controller', args, args, '/invocation')).toBe(true);
    expect(mocks.spawn).toHaveBeenCalledWith(process.execPath, [retained.executable, ...args],
      expect.objectContaining({ cwd: '/invocation', stdio: 'inherit' }));
    expect(process.exitCode).toBe(23);
    expect(mocks.assess).not.toHaveBeenCalled();
    expect(mocks.release).toHaveBeenCalledOnce();
  });

  it('forwards observation signals and removes its handlers after exit', async () => {
    mocks.ensure.mockResolvedValue(retained);
    const listeners = process.listenerCount('SIGTERM');
    const kill = vi.fn();
    mocks.spawn.mockImplementation(() => {
      const child = Object.assign(new EventEmitter(), { kill });
      queueMicrotask(() => {
        process.emit('SIGTERM', 'SIGTERM');
        child.emit('exit', null, 'SIGTERM');
      });
      return child;
    });
    expect(await prepareControllerCommand('/controller', ['task', 'status'], [])).toBe(true);
    expect(kill).toHaveBeenCalledWith('SIGTERM');
    expect(process.exitCode).toBe(143);
    expect(process.listenerCount('SIGTERM')).toBe(listeners);
    expect(mocks.release).toHaveBeenCalledOnce();
  });

  it('refuses a second observation hop', async () => {
    process.env.YYLO_GENERATION_REDISPATCH = retained.executable;
    mocks.ensure.mockResolvedValue(retained);
    await expect(prepareControllerCommand('/controller', ['task', 'status'], []))
      .rejects.toThrow('generation_dispatch_cycle');
    expect(mocks.spawn).not.toHaveBeenCalled();
  });

  it.each(['unsupported_state', 'package_provenance_invalid', 'mixed_runtime'])('refuses %s before status', async code => {
    mocks.ensure.mockRejectedValue(new Error(code));
    await expect(prepareControllerCommand('/controller', ['task', 'status'], [])).rejects.toThrow(code);
    expect(mocks.assess).not.toHaveBeenCalled();
    expect(mocks.spawn).not.toHaveBeenCalled();
  });

  it('keeps explicit doctor on candidate assessment', async () => {
    vi.spyOn(console, 'log').mockImplementation(() => {});
    expect(await prepareControllerCommand('/controller', ['scripts', 'doctor'], [])).toBe(true);
    expect(mocks.assess).toHaveBeenCalledOnce();
    expect(mocks.admit).not.toHaveBeenCalled();
  });
});

describe('optional readiness dispatch', () => {
  it.each(['ready', 'action_required'])('prints structured %s without executing a command', async disposition => {
    const report = { schema_version: 'yylo_controller_readiness.v1', disposition };
    mocks.readiness.mockResolvedValue(report);
    const log = vi.spyOn(console, 'log').mockImplementation(() => {});
    process.exitCode = undefined;
    expect(await prepareControllerCommand('/controller', ['scripts', 'generation', 'readiness'], [])).toBe(true);
    expect(mocks.readiness).toHaveBeenCalledWith('/controller', '/retained');
    expect(log).toHaveBeenCalledWith(JSON.stringify(report));
    expect(process.exitCode).toBe(disposition === 'ready' ? undefined : 2);
    expect(mocks.spawn).not.toHaveBeenCalled();
  });
});

describe('retained dispatch marker lifetime', () => {
  it('ends the hop only after one post-lock authenticated assessment', async () => {
    process.env.YYLO_GENERATION_REDISPATCH = retained.executable;
    mocks.ensure.mockImplementation(async () => {
      expect(mocks.acquire).toHaveBeenCalledOnce();
      expect(process.env.YYLO_GENERATION_REDISPATCH).toBe(retained.executable); return ready;
    });
    expect(await invoke()).toBe(false);
    expect(process.env.YYLO_GENERATION_REDISPATCH).toBeUndefined();
    expect(mocks.ensure).toHaveBeenCalledOnce(); expect(mocks.assess).not.toHaveBeenCalled();
    expect(mocks.spawn).not.toHaveBeenCalled();
    // The command still owns its read lease; only the hop marker was consumed.
    expect(mocks.release).not.toHaveBeenCalled();
  });

  it('refuses a lost reader guard before consuming the marker or dispatching', async () => {
    process.env.YYLO_GENERATION_REDISPATCH = retained.executable;
    mocks.assertHeld.mockImplementationOnce(() => { throw new Error('generation_read_lock_lost'); });
    await expect(invoke()).rejects.toThrow('generation_read_lock_lost');
    expect(process.env.YYLO_GENERATION_REDISPATCH).toBe(retained.executable);
    expect(mocks.spawn).not.toHaveBeenCalled();
    expect(mocks.release).toHaveBeenCalledOnce();
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
    expect(mocks.acquire).toHaveBeenCalledOnce(); expect(mocks.release).toHaveBeenCalledOnce();
    expect(mocks.spawn).not.toHaveBeenCalled();
  });

  it('does not consume the guard on a post-lock generation race', async () => {
    process.env.YYLO_GENERATION_REDISPATCH = retained.executable;
    mocks.ensure.mockRejectedValue(new Error('generation_changed_before_dispatch'));
    await expect(invoke()).rejects.toThrow('generation_changed_before_dispatch');
    expect(process.env.YYLO_GENERATION_REDISPATCH).toBe(retained.executable);
    expect(mocks.spawn).not.toHaveBeenCalled(); expect(mocks.release).toHaveBeenCalledOnce();
  });
});
