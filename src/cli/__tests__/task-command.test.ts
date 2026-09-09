import { Command } from 'commander';
import { describe, expect, it, vi } from 'vitest';
import fs from 'fs-extra';
import os from 'node:os';
import path from 'node:path';
import {
  checkpointTaskWorkspaceAfterFinalization,
  configureTaskWorkspaceCommand,
  selectTaskWorkspaceRuntime,
  taskWorkspaceControlOperation,
} from '../commands/task.js';

describe('task workspace CLI', () => {
  it('selects the real shipped template for hydrate by stable capability', async () => {
    const controller = await fs.mkdtemp(path.join(os.tmpdir(), 'yylo-hydrate-capability-'));
    const source = path.resolve(process.cwd(), 'src/templates/scripts/task_workspace.py');
    expect(await fs.pathExists(source), source).toBe(true);
    await expect(selectTaskWorkspaceRuntime(controller, 'hydrate', [source]))
      .resolves.toBe(source);
    const runtime = await fs.readFile(source, 'utf8');
    expect(runtime).toContain('TASK_RUNTIME_CAPABILITY_HYDRATE_V1 = True');
    expect(runtime).toContain('TASK_HYDRATE_RECOVERY_SCHEMA = "juno_task_hydrate_recovery.v1"');
    expect(runtime).toContain('def hydrate(controller:');
  });

  it.each([
    { operation: 'run', expected: undefined },
    { operation: 'resume', expected: undefined },
    { operation: 'start', expected: [] },
    { operation: 'status', expected: undefined },
    { operation: 'admission', expected: undefined },
    { operation: 'hydrate', expected: [] },
    { operation: 'preflight', expected: undefined },
    { operation: 'checkpoint', expected: [] },
    { operation: 'finish', expected: [] },
  ] as const)(
    'forwards task $operation and its positional ID to one managed-runtime invoker',
    async ({ operation, expected }) => {
      const invoke = vi.fn(async () => undefined);
      const program = new Command().exitOverride().configureOutput({ writeOut: () => undefined });
      configureTaskWorkspaceCommand(program, invoke);
      await program.parseAsync(['node', 'yy', 'task', operation, 'T123']);
      expect(invoke).toHaveBeenCalledOnce();
      if (expected === undefined) {
        expect(invoke).toHaveBeenCalledWith(operation, 'T123', []);
      } else {
        expect(invoke).toHaveBeenCalledWith(operation, 'T123', [], expected);
      }
    },
  );

  it('exposes preflight, kanban sync, fencing leases, bounded umbrella recovery, and guarded runtime bootstrap below task', () => {
    const program = new Command();
    configureTaskWorkspaceCommand(program, async () => undefined);
    const task = program.commands.find((command) => command.name() === 'task');
    expect(task?.commands.map((command) => command.name())).toEqual([
      'run', 'resume', 'recover-predispatch', 'recover-wall-budget', 'start', 'admission', 'preflight', 'checkpoint',
      'child-checkpoint', 'hydrate', 'status', 'finish', 'doctor', 'sync', 'lease-status',
      'lease-heartbeat', 'lease-handoff', 'lease-successor', 'lease-revoke', 'lease-release',
      'recovery-plan', 'recovery-authorize', 'recovery-apply', 'runtime-bootstrap',
    ]);
    expect(task?.commands.find((command) => command.name() === 'child-checkpoint')
      ?.registeredArguments).toHaveLength(2);
    expect(task?.commands.find((command) => command.name() === 'doctor')
      ?.registeredArguments[0]?.required).toBe(false);
    expect(task?.commands.find((command) => command.name() === 'runtime-bootstrap')
      ?.registeredArguments).toHaveLength(0);
  });

  it.each([
    { argv: ['--dry-run'], expected: { dryRun: true } },
    { argv: ['--apply', '/tmp/plan.json'], expected: { apply: '/tmp/plan.json' } },
  ])('forwards guarded task runtime bootstrap $argv', async ({ argv, expected }) => {
    const invoke = vi.fn(async () => undefined);
    const bootstrap = vi.fn(async () => undefined);
    const program = new Command().exitOverride().configureOutput({ writeOut: () => undefined });
    configureTaskWorkspaceCommand(program, invoke, bootstrap);
    await program.parseAsync(['node', 'yy', 'task', 'runtime-bootstrap', ...argv]);
    expect(invoke).not.toHaveBeenCalled();
    expect(bootstrap).toHaveBeenCalledWith(expected);
  });

  it('documents repeatable exact files while retaining legacy baseline omission', () => {
    const program = new Command();
    configureTaskWorkspaceCommand(program, async () => undefined);
    const task = program.commands.find((command) => command.name() === 'task');
    const start = task?.commands.find((command) => command.name() === 'start');
    const pathOption = start?.options.find((option) => option.long === '--path');

    expect(pathOption?.description).toBe(
      'Exact tracked authored file or additional selectable product root; repeat for exact scope',
    );
    const help = start?.helpInformation();
    expect(help).toContain('--path <path>');
    expect(help).toContain('Exact tracked authored file or');
    expect(help).toContain('repeat for exact scope');
  });

  it('uses baseline paths by default and forwards repeatable additional roots only for task start', async () => {
    const invoke = vi.fn(async () => undefined);
    const program = new Command().exitOverride().configureOutput({ writeOut: () => undefined });
    configureTaskWorkspaceCommand(program, invoke);

    await program.parseAsync(['node', 'yy', 'task', 'start', 'BASE']);
    expect(invoke).toHaveBeenLastCalledWith('start', 'BASE', [], []);

    await program.parseAsync([
      'node', 'yy', 'task', 'start', 'EXTRA', '--path', 'juno_kanban', '--path', 'frontend',
    ]);
    expect(invoke).toHaveBeenLastCalledWith('start', 'EXTRA', ['juno_kanban', 'frontend'], []);
  });

  it('forwards umbrella admission and exact recovery plan/apply arguments', async () => {
    const invoke = vi.fn(async () => undefined);
    const program = new Command().exitOverride().configureOutput({ writeOut: () => undefined });
    configureTaskWorkspaceCommand(program, invoke);
    await program.parseAsync(['node', 'yy', 'task', 'recover-predispatch', 'U1',
      '--run-id', 'run-12345678']);
    expect(invoke).toHaveBeenLastCalledWith('recover-predispatch', 'U1', [],
      ['--run-id', 'run-12345678']);
    await program.parseAsync(['node', 'yy', 'task', 'recover-wall-budget', 'U1',
      '--run-id', 'run-12345678', '--attempt', '1',
      '--predispatch-receipt-sha256', 'a'.repeat(64),
      '--original-deadline-unix-ns', '1787895956343575000']);
    expect(invoke).toHaveBeenLastCalledWith('recover-wall-budget', 'U1', [], [
      '--run-id', 'run-12345678', '--attempt', '1',
      '--predispatch-receipt-sha256', 'a'.repeat(64),
      '--original-deadline-unix-ns', '1787895956343575000',
    ]);
    await program.parseAsync(['node', 'yy', 'task', 'start', 'U1',
      '--umbrella-admission', '/tmp/umbrella.json']);
    expect(invoke).toHaveBeenLastCalledWith('start', 'U1', [],
      ['--umbrella-admission', '/tmp/umbrella.json']);
    await program.parseAsync(['node', 'yy', 'task', 'recovery-plan', 'U1',
      '--umbrella-admission', '/tmp/umbrella.json', '--output', '/tmp/plan.json']);
    expect(invoke).toHaveBeenLastCalledWith('recovery-plan', 'U1', [], [
      '--umbrella-admission', '/tmp/umbrella.json', '--output', '/tmp/plan.json',
    ]);
    await program.parseAsync(['node', 'yy', 'task', 'recovery-authorize', 'U1',
      '--umbrella-admission', '/tmp/umbrella.json', '--plan', '/tmp/plan.json']);
    expect(invoke).toHaveBeenLastCalledWith('recovery-authorize', 'U1', [], [
      '--umbrella-admission', '/tmp/umbrella.json', '--plan', '/tmp/plan.json',
    ]);
    await program.parseAsync(['node', 'yy', 'task', 'recovery-apply', 'U1',
      '--umbrella-admission', '/tmp/umbrella.json', '--plan', '/tmp/plan.json',
      '--authorization-receipt', '/tmp/authorization.json']);
    expect(invoke).toHaveBeenLastCalledWith('recovery-apply', 'U1', [], [
      '--umbrella-admission', '/tmp/umbrella.json', '--plan', '/tmp/plan.json',
      '--authorization-receipt', '/tmp/authorization.json',
    ]);
    await program.parseAsync(['node', 'yy', 'task', 'child-checkpoint', 'U1', 'C1']);
    expect(invoke).toHaveBeenLastCalledWith('child-checkpoint', 'U1', [], ['--child', 'C1']);
  });

  it('routes recovery planning through read-only kanban policy and apply through orchestration', () => {
    expect(taskWorkspaceControlOperation('recovery-plan')).toBe('kanban');
    expect(taskWorkspaceControlOperation('status')).toBe('kanban');
    expect(taskWorkspaceControlOperation('doctor')).toBe('kanban');
    expect(taskWorkspaceControlOperation('lease-status')).toBe('kanban');
    expect(taskWorkspaceControlOperation('lease-heartbeat')).toBe('orchestration');
    expect(taskWorkspaceControlOperation('lease-successor')).toBe('orchestration');
    expect(taskWorkspaceControlOperation('sync')).toBe('orchestration');
    expect(taskWorkspaceControlOperation('child-checkpoint')).toBe('orchestration');
    expect(taskWorkspaceControlOperation('recovery-authorize')).toBe('orchestration');
    expect(taskWorkspaceControlOperation('recovery-apply')).toBe('orchestration');
    expect(taskWorkspaceControlOperation('recover-predispatch')).toBe('orchestration');
    expect(taskWorkspaceControlOperation('recover-wall-budget')).toBe('orchestration');
  });

  it('forwards the fencing lease token and lease command arguments exactly', async () => {
    const invoke = vi.fn(async () => undefined);
    const program = new Command().exitOverride().configureOutput({ writeOut: () => undefined });
    configureTaskWorkspaceCommand(program, invoke);
    await program.parseAsync(['node', 'yy', 'task', 'start', 'T1',
      '--lease-token', 'tok-1']);
    expect(invoke).toHaveBeenLastCalledWith('start', 'T1', [], ['--lease-token', 'tok-1']);
    await program.parseAsync(['node', 'yy', 'task', 'checkpoint', 'T1',
      '--lease-token', 'tok-1']);
    expect(invoke).toHaveBeenLastCalledWith('checkpoint', 'T1', [], ['--lease-token', 'tok-1']);
    await program.parseAsync(['node', 'yy', 'task', 'checkpoint', 'T1',
      '--accept', 'final', '--lease-token', 'tok-1']);
    expect(invoke).toHaveBeenLastCalledWith('checkpoint', 'T1', [], [
      '--accept-checkpoint', 'final', '--lease-token', 'tok-1',
    ]);
    await program.parseAsync(['node', 'yy', 'task', 'finish', 'T1']);
    expect(invoke).toHaveBeenLastCalledWith('finish', 'T1', [], []);
    await program.parseAsync(['node', 'yy', 'task', 'lease-heartbeat', 'T1',
      '--lease-token', 'tok-1']);
    expect(invoke).toHaveBeenLastCalledWith('lease-heartbeat', 'T1', [], ['--lease-token', 'tok-1']);
    await program.parseAsync(['node', 'yy', 'task', 'lease-handoff', 'T1',
      '--lease-token', 'tok-1', '--reason', 'session ending']);
    expect(invoke).toHaveBeenLastCalledWith('lease-handoff', 'T1', [],
      ['--lease-token', 'tok-1', '--reason', 'session ending']);
    await program.parseAsync(['node', 'yy', 'task', 'lease-successor', 'T1',
      '--handoff-receipt', '/tmp/handoff.json']);
    expect(invoke).toHaveBeenLastCalledWith('lease-successor', 'T1', [],
      ['--handoff-receipt', '/tmp/handoff.json']);
    await program.parseAsync(['node', 'yy', 'task', 'lease-revoke', 'T1',
      '--reason', 'lost session']);
    expect(invoke).toHaveBeenLastCalledWith('lease-revoke', 'T1', [], ['--reason', 'lost session']);
    await program.parseAsync(['node', 'yy', 'task', 'lease-status', 'T1']);
    expect(invoke).toHaveBeenLastCalledWith('lease-status', 'T1', []);
  });

  it.each(['run', 'resume', 'start', 'hydrate', 'finish', 'recovery-authorize', 'recovery-apply',
           'lease-heartbeat', 'lease-handoff', 'lease-successor', 'lease-revoke', 'lease-release'] as const)(
    'checkpoints durable controller state after task %s without replacing its outcome',
    async (operation) => {
      const checkpoint = vi.fn(async () => ({ attempted: true, ok: true }));
      await checkpointTaskWorkspaceAfterFinalization(operation, '/controller', 0, checkpoint);
      expect(checkpoint).toHaveBeenCalledOnce();
      expect(checkpoint).toHaveBeenCalledWith('/controller', 0);
    },
  );

  it.each(['status', 'preflight', 'recovery-plan', 'checkpoint', 'evidence-run', 'evidence-status', 'evidence-await', 'doctor', 'lease-status'] as const)(
    'does not checkpoint after read-only task %s',
    async (operation) => {
    const checkpoint = vi.fn(async () => ({ attempted: true, ok: true }));
    await checkpointTaskWorkspaceAfterFinalization(operation, '/controller', 0, checkpoint);
    expect(checkpoint).not.toHaveBeenCalled();
    },
  );

  it('passes the lifecycle task identity to task-scoped checkpoint attribution', async () => {
    const checkpoint = vi.fn(async () => ({ attempted: true, ok: true }));
    await checkpointTaskWorkspaceAfterFinalization('finish', '/controller', 0, checkpoint, 'TaskA');
    expect(checkpoint).toHaveBeenCalledWith('/controller', 0, 'TaskA');
  });

  it('preserves a failed task outcome while the best-effort checkpointer reports recovery', async () => {
    const checkpoint = vi.fn(async () => ({
      attempted: true,
      ok: false,
      warning: 'run controller_checkpoint.py manually',
    }));
    await checkpointTaskWorkspaceAfterFinalization('start', '/controller', 9, checkpoint);
    expect(checkpoint).toHaveBeenCalledWith('/controller', 9);
  });
});
