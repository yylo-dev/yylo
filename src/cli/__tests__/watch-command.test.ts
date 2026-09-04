import { describe, expect, it, vi } from 'vitest';
import { Command } from 'commander';
import { configureWatchCommand } from '../commands/watch.js';

describe('watch command', () => {
  it('routes exec, status, await, and follow without shell reconstruction', async () => {
    const invoke = vi.fn().mockResolvedValue(undefined);
    const execProgram = new Command().exitOverride();
    configureWatchCommand(execProgram, invoke);
    await execProgram.parseAsync(['node', 'test', 'watch', 'exec', '--detach', '--timeout', '5', '--', 'printf', 'ready']);
    expect(invoke).toHaveBeenCalledWith('exec', ['--detach', '--timeout', '5', '--', 'printf', 'ready']);

    const statusProgram = new Command().exitOverride();
    configureWatchCommand(statusProgram, invoke);
    await statusProgram.parseAsync(['node', 'test', 'watch', 'status', 'run-1']);
    expect(invoke).toHaveBeenCalledWith('status', ['run-1']);

    const awaitProgram = new Command().exitOverride();
    configureWatchCommand(awaitProgram, invoke);
    await awaitProgram.parseAsync(['node', 'test', 'watch', 'await', 'run-1']);
    expect(invoke).toHaveBeenCalledWith('await', ['run-1']);

    const followProgram = new Command().exitOverride();
    configureWatchCommand(followProgram, invoke);
    await followProgram.parseAsync(['node', 'test', 'watch', 'follow', 'run-1']);
    expect(invoke).toHaveBeenCalledWith('follow', ['run-1']);
  });
});
