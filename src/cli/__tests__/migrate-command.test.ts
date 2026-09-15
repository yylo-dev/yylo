import { Command } from 'commander';
import { describe, expect, it, vi } from 'vitest';
import { configureMigrationCommand } from '../commands/migrate.js';

describe('migration CLI', () => {
  it('owns finite legacy lifecycle plan/apply/verify and drain outside ordinary task help', async () => {
    const invoke = vi.fn(async () => undefined);
    const legacy = vi.fn(async () => undefined);
    const program = new Command().exitOverride().configureOutput({ writeOut: () => undefined });
    configureMigrationCommand(program, invoke, legacy);
    const migrate = program.commands.find((command) => command.name() === 'migrate');
    const group = migrate?.commands.find((command) => command.name() === 'legacy-lifecycle');
    expect(group?.commands.map((command) => command.name())).toEqual([
      'inventory', 'plan', 'authorize', 'apply', 'verify', 'checkpoint',
    ]);

    await program.parseAsync(['node', 'yy', 'migrate', 'legacy-lifecycle', 'inventory']);
    expect(legacy).toHaveBeenLastCalledWith('doctor', '', []);
    await program.parseAsync(['node', 'yy', 'migrate', 'legacy-lifecycle', 'plan', 'U1',
      '--umbrella-admission', '/r/umbrella.json', '--output', '/r/plan.json']);
    expect(legacy).toHaveBeenLastCalledWith('recovery-plan', 'U1', [], [
      '--umbrella-admission', '/r/umbrella.json', '--output', '/r/plan.json',
    ]);
    await program.parseAsync(['node', 'yy', 'migrate', 'legacy-lifecycle', 'authorize', 'U1',
      '--umbrella-admission', '/r/umbrella.json', '--plan', '/r/plan.json']);
    expect(legacy).toHaveBeenLastCalledWith('recovery-authorize', 'U1', [], [
      '--umbrella-admission', '/r/umbrella.json', '--plan', '/r/plan.json',
    ]);
    for (const operation of ['apply', 'verify'] as const) {
      await program.parseAsync(['node', 'yy', 'migrate', 'legacy-lifecycle', operation, 'U1',
        '--umbrella-admission', '/r/umbrella.json', '--plan', '/r/plan.json',
        '--authorization-receipt', '/r/authorization.json']);
      expect(legacy).toHaveBeenLastCalledWith(`recovery-${operation}`, 'U1', [], [
        '--umbrella-admission', '/r/umbrella.json', '--plan', '/r/plan.json',
        '--authorization-receipt', '/r/authorization.json',
      ]);
    }
    await program.parseAsync(['node', 'yy', 'migrate', 'legacy-lifecycle', 'checkpoint',
      'U1', 'C1', '--lease-token', 'token']);
    expect(legacy).toHaveBeenLastCalledWith('child-checkpoint', 'U1', [], [
      '--child', 'C1', '--lease-token', 'token',
    ]);
    expect(invoke).not.toHaveBeenCalled();
  });

  it('forwards a read-only inventory with an explicit external receipt', async () => {
    const invoke = vi.fn(async () => undefined);
    const program = new Command().exitOverride();
    configureMigrationCommand(program, invoke);
    await program.parseAsync(['node', 'yy', 'migrate', 'inventory', '--project', '/project', '--controller', '/controller', '--product-ref', 'refs/heads/product', '--runtime', '/bin/yy', '--kanban-runtime', '/bin/juno-kanban', '--heavy-threshold-bytes', '42', '--output', '/receipts/inventory.json']);
    expect(invoke).toHaveBeenCalledWith(['inventory', '--project', '/project', '--heavy-threshold-bytes', '42', '--output', '/receipts/inventory.json', '--controller', '/controller', '--product-ref', 'refs/heads/product', '--runtime', '/bin/yy', '--kanban-runtime', '/bin/juno-kanban']);
  });

  it('routes target-runtime provenance plan and explicitly authorized apply', async () => {
    const invoke = vi.fn(async () => undefined);
    const planProgram = new Command().exitOverride();
    configureMigrationCommand(planProgram, invoke);
    await planProgram.parseAsync(['node', 'yy', 'migrate', 'target-runtime-provenance', 'plan',
      '--controller', '/controller', '--output', '/external/plan.json']);
    expect(invoke).toHaveBeenCalledWith(['target-runtime-provenance-plan',
      '--controller', '/controller', '--output', '/external/plan.json']);

    const applyProgram = new Command().exitOverride();
    configureMigrationCommand(applyProgram, invoke);
    await applyProgram.parseAsync(['node', 'yy', 'migrate', 'target-runtime-provenance', 'apply',
      '--plan', '/external/plan.json', '--output', '/external/apply.json',
      '--authorize-target-runtime-provenance']);
    expect(invoke).toHaveBeenCalledWith(['target-runtime-provenance-apply',
      '--plan', '/external/plan.json', '--output', '/external/apply.json',
      '--authorize-target-runtime-provenance']);
  });

  it('exposes explicit controller executable rebind without installing a package', async () => {
    const invoke = vi.fn(async () => undefined);
    const program = new Command().exitOverride();
    configureMigrationCommand(program, invoke);
    await program.parseAsync(['node', 'yy', 'migrate', 'runtime-rebind',
      '--root', '/controller', '--branch', 'refs/heads/controller',
      '--runtime', '/package/dist/bin/cli.mjs', '--runtime-version', '2.1.3',
      '--output', '/tmp/runtime-rebind.json']);
    expect(invoke).toHaveBeenCalledWith(['runtime-rebind',
      '--root', '/controller', '--branch', 'refs/heads/controller',
      '--runtime', '/package/dist/bin/cli.mjs', '--runtime-version', '2.1.3',
      '--output', '/tmp/runtime-rebind.json']);
  });

  it('routes exact-release installation into a fresh non-Git prefix before rebind', async () => {
    const invoke = vi.fn(async () => undefined);
    const program = new Command().exitOverride();
    configureMigrationCommand(program, invoke);
    await program.parseAsync(['node', 'yy', 'migrate', 'runtime-install-rebind',
      '--root', '/controller', '--branch', 'refs/heads/controller',
      '--runtime-version', '2.1.3', '--install-prefix', '/opt/juno/runtimes/2.1.3',
      '--output', '/tmp/runtime-install-rebind.json']);
    expect(invoke).toHaveBeenCalledWith(['runtime-install-rebind',
      '--root', '/controller', '--branch', 'refs/heads/controller',
      '--runtime-version', '2.1.3', '--install-prefix', '/opt/juno/runtimes/2.1.3',
      '--output', '/tmp/runtime-install-rebind.json']);
  });

  it('exposes and forwards an exact local npm artifact without changing registry compatibility', async () => {
    const invoke = vi.fn(async () => undefined);
    const program = new Command().exitOverride();
    configureMigrationCommand(program, invoke);
    const migrate = program.commands.find((command) => command.name() === 'migrate');
    const install = migrate?.commands.find((command) => command.name() === 'runtime-install-rebind');
    expect(install?.helpInformation()).toContain('--artifact <path>');
    await program.parseAsync(['node', 'yy', 'migrate', 'runtime-install-rebind',
      '--root', '/controller', '--branch', 'refs/heads/controller',
      '--runtime-version', '2.1.3-rc.0.33', '--install-prefix', '/runtimes/rc33',
      '--artifact', '/releases/yylo-2.1.3-rc.0.33.tgz', '--output', '/receipts/rebind.json']);
    expect(invoke).toHaveBeenCalledWith(['runtime-install-rebind',
      '--root', '/controller', '--branch', 'refs/heads/controller',
      '--runtime-version', '2.1.3-rc.0.33', '--install-prefix', '/runtimes/rc33',
      '--output', '/receipts/rebind.json', '--artifact', '/releases/yylo-2.1.3-rc.0.33.tgz']);
  });

  it('routes controller config ownership plans and explicitly authorized applies', async () => {
    const invoke = vi.fn().mockResolvedValue(undefined);
    const program = new Command();
    configureMigrationCommand(program, invoke);
    await program.parseAsync(['node', 'yy', 'migrate', 'controller-config', 'plan',
      '--root', '/controller', '--output', '/external/plan.json']);
    expect(invoke).toHaveBeenCalledWith(['agent-config-plan', '--root', '/controller',
      '--output', '/external/plan.json']);
    await program.parseAsync(['node', 'yy', 'migrate', 'controller-config', 'apply',
      '--plan', '/external/plan.json', '--output', '/external/apply.json', '--authorize-config-repair']);
    expect(invoke).toHaveBeenCalledWith(['agent-config-apply', '--plan', '/external/plan.json',
      '--output', '/external/apply.json', '--authorize-config-repair']);
  });

  it('routes receipt-bound metadata-policy plan and explicitly authorized apply', async () => {
    const invoke = vi.fn(async () => undefined);
    const planProgram = new Command().exitOverride(); configureMigrationCommand(planProgram, invoke);
    await planProgram.parseAsync(['node', 'yy', 'migrate', 'metadata-policy', 'plan',
      '--root', '/controller', '--output', '/external/plan.json']);
    expect(invoke).toHaveBeenCalledWith(['metadata-policy-plan', '--root', '/controller',
      '--output', '/external/plan.json']);

    const applyProgram = new Command().exitOverride(); configureMigrationCommand(applyProgram, invoke);
    await applyProgram.parseAsync(['node', 'yy', 'migrate', 'metadata-policy', 'apply',
      '--plan', '/external/plan.json', '--output', '/external/apply.json',
      '--authorize-metadata-policy-migration']);
    expect(invoke).toHaveBeenCalledWith(['metadata-policy-apply', '--plan', '/external/plan.json',
      '--output', '/external/apply.json', '--authorize-metadata-policy-migration']);
  });

  it('routes reviewed agent-surface repair plan, apply, and verify commands', async () => {
    const invoke = vi.fn(async () => undefined);
    const planProgram = new Command().exitOverride(); configureMigrationCommand(planProgram, invoke);
    await planProgram.parseAsync(['node', 'yy', 'migrate', 'agent-surface-repair-plan',
      '--root', '/controller', '--branch', 'refs/heads/controller', '--expected-head', 'a'.repeat(40),
      '--product-ref', 'refs/heads/main', '--expected-product-head', 'b'.repeat(40),
      '--disposition', 'externalize', '--output', '/r/plan.json']);
    expect(invoke).toHaveBeenCalledWith(['agent-surface-repair-plan',
      '--root', '/controller', '--branch', 'refs/heads/controller', '--expected-head', 'a'.repeat(40),
      '--product-ref', 'refs/heads/main', '--expected-product-head', 'b'.repeat(40),
      '--disposition', 'externalize', '--output', '/r/plan.json']);

    const applyProgram = new Command().exitOverride(); configureMigrationCommand(applyProgram, invoke);
    await applyProgram.parseAsync(['node', 'yy', 'migrate', 'agent-surface-repair-apply',
      '--plan', '/r/plan.json', '--output', '/r/apply.json', '--authorize-agent-surface-repair']);
    expect(invoke).toHaveBeenCalledWith(['agent-surface-repair-apply', '--plan', '/r/plan.json',
      '--output', '/r/apply.json', '--authorize-agent-surface-repair']);

    const verifyProgram = new Command().exitOverride(); configureMigrationCommand(verifyProgram, invoke);
    await verifyProgram.parseAsync(['node', 'yy', 'migrate', 'agent-surface-repair-verify',
      '--plan', '/r/plan.json', '--output', '/r/verify.json']);
    expect(invoke).toHaveBeenCalledWith(['agent-surface-repair-verify', '--plan', '/r/plan.json',
      '--output', '/r/verify.json']);
  });

  it('forwards only immutable inputs to policy generation', async () => {
    const invoke = vi.fn(async () => undefined);
    const program = new Command().exitOverride();
    configureMigrationCommand(program, invoke);
    await program.parseAsync(['node', 'yy', 'migrate', 'generate-policy', '--inventory', '/r/i.json', '--answers', '/r/a.json', '--output', '/r/p.json']);
    expect(invoke).toHaveBeenCalledWith(['generate-policy', '--inventory', '/r/i.json', '--answers', '/r/a.json', '--output', '/r/p.json']);
  });

  it('creates an owner template bound to the immutable inventory', async () => {
    const invoke = vi.fn(async () => undefined);
    const program = new Command().exitOverride();
    configureMigrationCommand(program, invoke);
    await program.parseAsync(['node', 'yy', 'migrate', 'owner-template', '--inventory', '/r/i.json', '--output', '/r/a.json']);
    expect(invoke).toHaveBeenCalledWith(['owner-template', '--inventory', '/r/i.json', '--output', '/r/a.json']);
  });

  it('routes metadata evacuation plan, apply, and verify commands', async () => {
    const invoke = vi.fn(async () => undefined);
    const program = new Command().exitOverride(); configureMigrationCommand(program, invoke);
    await program.parseAsync(['node', 'yy', 'migrate', 'evacuation-plan', '--inventory', '/r/i.json', '--policy', '/r/p.json', '--project', '/product', '--output', '/r/plan.json']);
    expect(invoke).toHaveBeenCalledWith(['evacuation-plan', '--inventory', '/r/i.json', '--policy', '/r/p.json', '--project', '/product', '--output', '/r/plan.json']);

    const applyProgram = new Command().exitOverride(); configureMigrationCommand(applyProgram, invoke);
    await applyProgram.parseAsync(['node', 'yy', 'migrate', 'evacuation-apply', '--plan', '/r/plan.json', '--candidate', '/candidate', '--output', '/r/apply.json', '--allow-disposable-mutation']);
    expect(invoke).toHaveBeenCalledWith(['evacuation-apply', '--plan', '/r/plan.json', '--candidate', '/candidate', '--output', '/r/apply.json', '--allow-disposable-mutation']);

    const verifyProgram = new Command().exitOverride(); configureMigrationCommand(verifyProgram, invoke);
    await verifyProgram.parseAsync(['node', 'yy', 'migrate', 'evacuation-verify', '--plan', '/r/plan.json', '--candidate', '/candidate', '--output', '/r/verify.json']);
    expect(invoke).toHaveBeenCalledWith(['evacuation-verify', '--plan', '/r/plan.json', '--candidate', '/candidate', '--output', '/r/verify.json']);
  });

  it('forwards exact identities to a no-mutation registration plan', async () => {
    const invoke = vi.fn(async () => undefined);
    const program = new Command().exitOverride();
    configureMigrationCommand(program, invoke);
    await program.parseAsync(['node', 'yy', 'migrate', 'registration', 'plan',
      '--source-controller', '/old', '--source-ref', 'refs/heads/old', '--expected-source-head', 'a'.repeat(40),
      '--target-controller', '/new', '--target-ref', 'refs/heads/new', '--expected-target-head', 'b'.repeat(40),
      '--product-root', '/integration', '--product-ref', 'refs/heads/main', '--expected-product-head', 'c'.repeat(40),
      '--runtime', '/bin/yy', '--runtime-version', '2.1.1', '--inventory', '/r/inventory.json', '--policy-bundle', '/r/policy.json', '--pending-verification', '/r/pending.json', '--output', '/r/plan.json']);
    expect(invoke).toHaveBeenCalledWith(['registration', 'plan',
      '--source-controller', '/old', '--source-ref', 'refs/heads/old', '--expected-source-head', 'a'.repeat(40),
      '--target-controller', '/new', '--target-ref', 'refs/heads/new', '--expected-target-head', 'b'.repeat(40),
      '--product-root', '/integration', '--product-ref', 'refs/heads/main', '--expected-product-head', 'c'.repeat(40),
      '--runtime', '/bin/yy', '--runtime-version', '2.1.1', '--inventory', '/r/inventory.json', '--policy-bundle', '/r/policy.json', '--pending-verification', '/r/pending.json', '--output', '/r/plan.json']);
  });

  it('keeps apply and rollback behind explicit authorization flags', async () => {
    const invoke = vi.fn(async () => undefined);
    const program = new Command().exitOverride();
    configureMigrationCommand(program, invoke);
    await program.parseAsync(['node', 'yy', 'migrate', 'registration', 'apply', '--plan', '/r/plan.json', '--output', '/r/apply.json', '--authorize-apply']);
    expect(invoke).toHaveBeenLastCalledWith(['registration', 'apply', '--plan', '/r/plan.json', '--output', '/r/apply.json', '--authorize-apply']);
    await program.parseAsync(['node', 'yy', 'migrate', 'registration', 'rollback', '--plan', '/r/plan.json', '--output', '/r/rollback.json', '--authorize-rollback']);
    expect(invoke).toHaveBeenLastCalledWith(['registration', 'rollback', '--plan', '/r/plan.json', '--output', '/r/rollback.json', '--authorize-rollback']);
  });
});
