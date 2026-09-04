import { Command } from 'commander';
import { describe, expect, it, vi } from 'vitest';
import { configureReleaseTrainCommand } from '../commands/release.js';

describe('release train CLI', () => {
  it.each(['plan', 'status', 'inspect'] as const)('forwards %s with stable projection options', async (operation) => {
    const invoke = vi.fn(async () => undefined);
    const program = new Command().exitOverride();
    configureReleaseTrainCommand(program, invoke);
    await program.parseAsync(['node', 'yy', 'release', 'train', operation, '/train.json',
      '--json', '--output', '/plan.json']);
    expect(invoke).toHaveBeenCalledWith(operation, '/train.json', ['--json', '--output', '/plan.json']);
  });

  it('forwards explicit seal and fenced drive without weakening token identity', async () => {
    const invoke = vi.fn(async () => undefined);
    const program = new Command().exitOverride();
    configureReleaseTrainCommand(program, invoke);
    await program.parseAsync(['node', 'yy', 'release', 'train', 'select', '/train.json', '--json']);
    expect(invoke).toHaveBeenLastCalledWith('select', '/train.json', ['--json']);
    await program.parseAsync(['node', 'yy', 'release', 'train', 'seal', '/train.json', '--json']);
    expect(invoke).toHaveBeenLastCalledWith('seal', '/train.json', ['--json']);
    await program.parseAsync(['node', 'yy', 'release', 'train', 'drive', 'epoch-1',
      '--epoch-token', 'exact-token', '--json']);
    expect(invoke).toHaveBeenLastCalledWith('drive', 'epoch-1',
      ['--epoch-token', 'exact-token', '--json']);
  });

  it('labels observation, epoch mutation, and external authority boundaries in help', () => {
    const program = new Command();
    configureReleaseTrainCommand(program, async () => undefined);
    const release = program.commands.find((command) => command.name() === 'release');
    const train = release?.commands.find((command) => command.name() === 'train');
    const command = (name: string) => train?.commands.find((entry) => entry.name() === name);
    expect(release?.description()).toContain('no command implies publish/deploy authority');
    for (const name of ['plan', 'status', 'inspect']) {
      expect(command(name)?.description()).toContain('Read-only');
    }
    expect(command('epoch-status')?.description()).toContain('Read-only');
    expect(command('reconcile-members')?.description()).toContain('Receipt-bound');
    expect(command('replay-finalization-successor')?.description()).toContain('Typed descendant-target replay');
    expect(command('shadow')?.description()).toContain('Read-only');
    expect(command('select')?.description()).toContain('does not seal');
    expect(command('seal')?.description()).toContain('Explicitly close admission');
    expect(command('drive')?.description()).toContain('one target CAS');
    expect(command('repair')?.description()).toContain('one bounded');
    expect(command('replay-repair')?.description()).toContain('without another model');
    expect(command('retry')?.description()).toContain('Receipt-backed fenced retry');
    expect(command('bootstrap-inspect')?.description()).toContain('Read-only');
    expect(command('bootstrap-status')?.description()).toContain('Read-only');
    expect(command('bootstrap-seal')?.description()).toContain('Explicitly seal');
    expect(command('bootstrap-drive')?.description()).toContain('one expected-old-SHA');
  });

  it('forwards bootstrap inspection, seal, status, and fenced drive', async () => {
    const invoke = vi.fn(async () => undefined);
    const program = new Command().exitOverride();
    configureReleaseTrainCommand(program, invoke);
    await program.parseAsync(['node', 'yy', 'release', 'train', 'bootstrap-inspect', '/bootstrap.json', '--json']);
    expect(invoke).toHaveBeenLastCalledWith('bootstrap-inspect', '/bootstrap.json', ['--json']);
    await program.parseAsync(['node', 'yy', 'release', 'train', 'bootstrap-seal', '/bootstrap.json']);
    expect(invoke).toHaveBeenLastCalledWith('bootstrap-seal', '/bootstrap.json', []);
    await program.parseAsync(['node', 'yy', 'release', 'train', 'bootstrap-status', 'bootstrap-1', '--json']);
    expect(invoke).toHaveBeenLastCalledWith('bootstrap-status', 'bootstrap-1', ['--json']);
    await program.parseAsync(['node', 'yy', 'release', 'train', 'bootstrap-drive', 'bootstrap-1',
      '--bootstrap-token', 'exact-token', '--json']);
    expect(invoke).toHaveBeenLastCalledWith('bootstrap-drive', 'bootstrap-1',
      ['--bootstrap-token', 'exact-token', '--json']);
  });

  it('forwards exact-closure replay without weakening historical or fence identity', async () => {
    const invoke = vi.fn(async () => undefined);
    const program = new Command().exitOverride();
    configureReleaseTrainCommand(program, invoke);
    await program.parseAsync(['node', 'yy', 'release', 'train', 'replay-repair', 'successor',
      '--predecessor-epoch', 'historical', '--receipt', '/recovery.json',
      '--epoch-token', 'exact-token', '--json']);
    expect(invoke).toHaveBeenLastCalledWith('replay-repair', 'successor', [
      '--predecessor-epoch', 'historical', '--receipt', '/recovery.json',
      '--epoch-token', 'exact-token', '--json',
    ]);
  });

  it('forwards receipt-bound member reconciliation with exact target identity', async () => {
    const invoke = vi.fn(async () => undefined);
    const program = new Command().exitOverride();
    configureReleaseTrainCommand(program, invoke);
    await program.parseAsync(['node', 'yy', 'release', 'train', 'reconcile-members', 'epoch-1',
      '--expected-target', 'a'.repeat(40), '--json']);
    expect(invoke).toHaveBeenLastCalledWith('reconcile-members', 'epoch-1',
      ['--expected-target', 'a'.repeat(40), '--json']);
  });

  it('forwards typed stale-finalization successor with both exact target identities', async () => {
    const invoke = vi.fn(async () => undefined);
    const program = new Command().exitOverride();
    configureReleaseTrainCommand(program, invoke);
    await program.parseAsync(['node', 'yy', 'release', 'train', 'replay-finalization-successor', 'epoch-1',
      '--predecessor-target', 'a'.repeat(40), '--expected-target', 'b'.repeat(40), '--json']);
    expect(invoke).toHaveBeenLastCalledWith('replay-finalization-successor', 'epoch-1', [
      '--predecessor-target', 'a'.repeat(40), '--expected-target', 'b'.repeat(40), '--json',
    ]);
  });

  it('forwards fenced aggregate retry without weakening token identity', async () => {
    const invoke = vi.fn(async () => undefined);
    const program = new Command().exitOverride();
    configureReleaseTrainCommand(program, invoke);
    await program.parseAsync(['node', 'yy', 'release', 'train', 'retry', 'epoch-1',
      '--epoch-token', 'exact-token', '--json']);
    expect(invoke).toHaveBeenLastCalledWith('retry', 'epoch-1',
      ['--epoch-token', 'exact-token', '--json']);
  });

  it('forwards optional ejection and read-only shadow projection', async () => {
    const invoke = vi.fn(async () => undefined);
    const program = new Command().exitOverride();
    configureReleaseTrainCommand(program, invoke);
    await program.parseAsync(['node', 'yy', 'release', 'train', 'eject', 'epoch-1', 'TASK01',
      '--reason', 'optional-red', '--epoch-token', 'exact-token']);
    expect(invoke).toHaveBeenLastCalledWith('eject', 'epoch-1',
      ['TASK01', '--reason', 'optional-red', '--epoch-token', 'exact-token']);
    await program.parseAsync(['node', 'yy', 'release', 'train', 'shadow', '/train.json',
      '--baseline', '/baseline.json', '--output', '/decision.json']);
    expect(invoke).toHaveBeenLastCalledWith('shadow', '/train.json',
      ['--baseline', '/baseline.json', '--output', '/decision.json']);
  });
});
