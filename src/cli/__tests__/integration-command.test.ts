import { Command } from 'commander';
import { describe, expect, it, vi } from 'vitest';
import { configureIntegrationCommand } from '../commands/integration.js';

describe('integration workspace CLI', () => {
  it('forwards offline and fetching status explicitly', async () => {
    const invoke = vi.fn(async () => undefined);
    const program = new Command().exitOverride().configureOutput({ writeOut: () => undefined });
    configureIntegrationCommand(program, invoke);
    await program.parseAsync(['node', 'yy', 'integration', 'status']);
    expect(invoke).toHaveBeenLastCalledWith('status', {});
    await program.parseAsync(['node', 'yy', 'integration', 'status', '--fetch']);
    expect(invoke).toHaveBeenLastCalledWith('status', { fetch: true });
  });

  it('forwards guarded sync without implicit options', async () => {
    const invoke = vi.fn(async () => undefined);
    const program = new Command().exitOverride().configureOutput({ writeOut: () => undefined });
    configureIntegrationCommand(program, invoke);
    await program.parseAsync(['node', 'yy', 'integration', 'sync']);
    expect(invoke).toHaveBeenCalledWith('sync', {});
  });

  it('forwards managed runtime doctor and exact-generation recovery', async () => {
    const invoke = vi.fn(async () => undefined);
    const doctor = new Command().exitOverride().configureOutput({ writeOut: () => undefined });
    configureIntegrationCommand(doctor, invoke);
    await doctor.parseAsync(['node', 'yy', 'integration', 'runtime-doctor', '--target-sha', 'b'.repeat(40)]);
    expect(invoke).toHaveBeenLastCalledWith('runtime-doctor', { targetSha: 'b'.repeat(40) });

    const refresh = new Command().exitOverride().configureOutput({ writeOut: () => undefined });
    configureIntegrationCommand(refresh, invoke);
    await refresh.parseAsync([
      'node', 'yy', 'integration', 'runtime-refresh',
      '--previous-sha', 'a'.repeat(40), '--target-sha', 'b'.repeat(40),
    ]);
    expect(invoke).toHaveBeenLastCalledWith('runtime-refresh', {
      previousSha: 'a'.repeat(40), targetSha: 'b'.repeat(40),
    });

    const plan = new Command().exitOverride().configureOutput({ writeOut: () => undefined });
    configureIntegrationCommand(plan, invoke);
    await plan.parseAsync([
      'node', 'yy', 'integration', 'runtime-refresh', '--previous-sha', 'a'.repeat(40), '--dry-run',
    ]);
    expect(invoke).toHaveBeenLastCalledWith('runtime-refresh', {
      previousSha: 'a'.repeat(40), dryRun: true,
    });

    const apply = new Command().exitOverride().configureOutput({ writeOut: () => undefined });
    configureIntegrationCommand(apply, invoke);
    await apply.parseAsync([
      'node', 'yy', 'integration', 'runtime-refresh', '--previous-sha', 'a'.repeat(40),
      '--apply', '/tmp/approved.json',
    ]);
    expect(invoke).toHaveBeenLastCalledWith('runtime-refresh', {
      previousSha: 'a'.repeat(40), apply: '/tmp/approved.json',
    });
  });

  it('forwards one receipt-bound exact source-runtime adoption transaction', async () => {
    const invoke = vi.fn(async () => undefined);
    const program = new Command().exitOverride().configureOutput({ writeOut: () => undefined });
    configureIntegrationCommand(program, invoke);
    await program.parseAsync([
      'node', 'yy', 'integration', 'runtime-adopt-source',
      '--previous-sha', 'a'.repeat(40), '--target-sha', 'b'.repeat(40),
      '--install-prefix', '/tmp/runtime-b', '--output', '/tmp/adoption.json',
    ]);
    expect(invoke).toHaveBeenCalledWith('runtime-adopt-source', {
      previousSha: 'a'.repeat(40), targetSha: 'b'.repeat(40),
      installPrefix: '/tmp/runtime-b', output: '/tmp/adoption.json',
    });
  });

  it('forwards explicit canonical owner registration', async () => {
    const invoke = vi.fn(async () => undefined);
    const program = new Command().exitOverride().configureOutput({ writeOut: () => undefined });
    configureIntegrationCommand(program, invoke);
    await program.parseAsync(['node', 'yy', 'integration', 'register', '/integration', '--replace']);
    expect(invoke).toHaveBeenCalledWith('register', { owner: '/integration', replace: true });
  });

  it.each(['repair', 'push'] as const)('requires and forwards receipt-bound %s modes', async (operation) => {
    const invoke = vi.fn(async () => undefined);
    const dryRunProgram = new Command().exitOverride().configureOutput({ writeOut: () => undefined });
    configureIntegrationCommand(dryRunProgram, invoke);
    await dryRunProgram.parseAsync(['node', 'yy', 'integration', operation, '--dry-run']);
    expect(invoke).toHaveBeenLastCalledWith(operation, { dryRun: true });
    const applyProgram = new Command().exitOverride().configureOutput({ writeOut: () => undefined });
    configureIntegrationCommand(applyProgram, invoke);
    await applyProgram.parseAsync(['node', 'yy', 'integration', operation, '--apply', '/tmp/plan.json']);
    expect(invoke).toHaveBeenLastCalledWith(operation, { apply: '/tmp/plan.json' });
  });

  it('treats bare integration push as explicit plan-and-publish authority', async () => {
    const invoke = vi.fn(async () => undefined);
    const program = new Command().exitOverride().configureOutput({ writeOut: () => undefined });
    configureIntegrationCommand(program, invoke);
    await program.parseAsync(['node', 'yy', 'integration', 'push']);
    expect(invoke).toHaveBeenLastCalledWith('push', {});
  });

  it('keeps stale-owner migration inside receipt-bound repair with no bypass mode', async () => {
    const invoke = vi.fn(async () => undefined);
    const program = new Command().exitOverride().configureOutput({ writeOut: () => undefined });
    configureIntegrationCommand(program, invoke);
    await expect(program.parseAsync([
      'node', 'yy', 'integration', 'repair', '--migrate-stale-owner',
    ])).rejects.toThrow('unknown option');
    expect(invoke).not.toHaveBeenCalled();
  });

  it('keeps bare repair invalid and rejects combined push modes', async () => {
    const invoke = vi.fn(async () => undefined);
    const repair = new Command().exitOverride().configureOutput({ writeOut: () => undefined });
    configureIntegrationCommand(repair, invoke);
    await expect(repair.parseAsync(['node', 'yy', 'integration', 'repair']))
      .rejects.toThrow('requires exactly one');
    const push = new Command().exitOverride().configureOutput({ writeOut: () => undefined });
    configureIntegrationCommand(push, invoke);
    await expect(push.parseAsync([
      'node', 'yy', 'integration', 'push', '--dry-run', '--apply', '/tmp/plan.json',
    ])).rejects.toThrow('accepts only one');
  });
});
