import { describe, expect, it, vi } from 'vitest';
import { Command } from 'commander';
import { configureWatchCommand, invokeWatch } from '../commands/watch.js';
import { routeControlPlane } from '../../utils/control-plane-router.js';

vi.mock('../../utils/control-plane-router.js', () => ({
  routeControlPlane: vi.fn(() => { throw new Error('read-only route reached'); }),
}));

describe('watch command', () => {
  it.each(['status', 'await', 'follow'] as const)('forwards read-only %s', async (operation) => {
    const invoke = vi.fn().mockResolvedValue(undefined);
    const program = new Command().exitOverride();
    configureWatchCommand(program, invoke);
    await program.parseAsync(['node', 'test', 'watch', operation, 'run-1']);
    expect(invoke).toHaveBeenCalledWith(operation, ['run-1']);
  });

  it.each(['status', 'await', 'follow'] as const)('routes %s through read-only policy', async (operation) => {
    vi.mocked(routeControlPlane).mockClear();
    await expect(invokeWatch(operation, ['run-1'])).rejects.toThrow('read-only route reached');
    expect(routeControlPlane).toHaveBeenCalledWith(process.cwd(), 'kanban');
  });

  it('refuses exec before routing or spawning, including direct invocation', async () => {
    vi.mocked(routeControlPlane).mockClear();
    const invoke = vi.fn();
    const program = new Command().exitOverride();
    configureWatchCommand(program, invoke);
    await expect(program.parseAsync(['node', 'test', 'watch', 'exec', '--detach', '--', 'echo', 'hello']))
      .rejects.toThrow('watch exec is retired');
    expect(invoke).not.toHaveBeenCalled();
    await expect(invokeWatch('exec', ['--', 'echo', 'hello'])).rejects.toThrow('watch exec is retired');
    expect(routeControlPlane).not.toHaveBeenCalled();
  });
});
