import { Command } from 'commander';
import { execFileSync } from 'node:child_process';
import * as os from 'node:os';
import * as path from 'node:path';
import fs from 'fs-extra';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  MAX_MERGE_RESULT_LINE_CHARS,
  checkpointMergeQueueAfterFinalization,
  configureMergeQueueCommand,
  invokeMergeQueueAtController,
} from '../commands/merge.js';

const temporaryRoots: string[] = [];

function git(root: string, ...args: string[]): string {
  return execFileSync('git', ['-C', root, ...args], { encoding: 'utf8' }).trim();
}

async function controllerFixture(): Promise<string> {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'merge-checkpoint-'));
  temporaryRoots.push(root);
  git(root, 'init', '-b', 'controller');
  git(root, 'config', 'user.email', 'fixture@example.invalid');
  git(root, 'config', 'user.name', 'Fixture');
  await fs.ensureDir(path.join(root, '.juno_task', 'scripts'));
  for (const relative of ['tasks/T123.md', 'ledger/events.jsonl', 'state/tasks.json']) {
    await fs.outputFile(path.join(root, '.juno_task', relative), 'initial\n');
  }
  await fs.writeJson(path.join(root, '.juno_task', 'config.json'), {
    gitCheckpoint: { include: ['.juno_task/tasks', '.juno_task/ledger', '.juno_task/state'] },
  });
  const helper = path.resolve(process.cwd(), 'src/templates/scripts/controller_checkpoint.py');
  await fs.writeFile(path.join(root, '.juno_task', 'scripts', 'controller_checkpoint.py'), `#!/usr/bin/env python3
import subprocess, sys
raise SystemExit(subprocess.run([sys.executable, ${JSON.stringify(helper)}, *sys.argv[1:]]).returncode)
`);
  await fs.writeFile(path.join(root, 'product.txt'), 'product\n');
  git(root, 'add', '.');
  git(root, 'commit', '-m', 'initial controller');
  return root;
}

const mergedResult = {
  outcome: 'MERGED',
  post_integration: { kanban_finalization: { status: 'complete' } },
};

async function writeMergeRuntime(root: string, body: string): Promise<void> {
  await fs.writeFile(
    path.join(root, '.juno_task', 'scripts', 'merge_queue.py'),
    `#!/usr/bin/env python3\n${body}\n`,
  );
}

afterEach(async () => {
  vi.restoreAllMocks();
  await Promise.all(temporaryRoots.splice(0).map((root) => fs.remove(root)));
});

describe('merge queue CLI', () => {
  it.each([
    { argv: ['status'], expected: ['status'] },
    { argv: ['status', '--detail', 'T123'], expected: ['status', undefined, ['--detail', 'T123']] },
    { argv: ['status', '--detail'], expected: ['status', undefined, ['--detail']] },
    { argv: ['status', '--full'], expected: ['status', undefined, ['--full']] },
    { argv: ['resume'], expected: ['resume'] },
    { argv: ['next'], expected: ['next'] },
    { argv: ['next', 'T123'], expected: ['next', 'T123'] },
    { argv: ['resolve', 'T123'], expected: ['resolve', 'T123'] },
    { argv: ['review', 'T123'], expected: ['review', 'T123'] },
    { argv: ['reopen', 'T123'], expected: ['reopen', 'T123'] },
    { argv: ['withdraw', 'T123'], expected: ['withdraw', 'T123'] },
  ] as const)('forwards merge $argv', async ({ argv, expected }) => {
    const invoke = vi.fn(async () => undefined);
    const program = new Command().exitOverride().configureOutput({ writeOut: () => undefined });
    configureMergeQueueCommand(program, invoke);
    await program.parseAsync(['node', 'yy', 'merge', ...argv]);
    expect(invoke).toHaveBeenCalledOnce();
    expect(invoke).toHaveBeenCalledWith(...expected);
  });

  it('forwards every exact deterministic full-suite repair identity', async () => {
    const invoke = vi.fn(async () => undefined);
    const program = new Command().exitOverride();
    configureMergeQueueCommand(program, invoke);
    const args = ['--attempt', '231', '--terminal-receipt', '/attempt-231-failed.json',
      '--terminal-receipt-sha256', 'a'.repeat(64), '--expected-revision', 'b'.repeat(64),
      '--run-id', '1788481850518348000-e36d9830ea1a078f', '--scope-sha256', 'c'.repeat(64),
      '--journal-sha256', 'd'.repeat(64)];
    await program.parseAsync(['node', 'yy', 'merge', 'recover-full-suite-failure', 'T123', ...args]);
    expect(invoke).toHaveBeenCalledWith('recover-full-suite-failure', 'T123', args);
  });

  it('forwards every exact semantic-repair pre-dispatch recovery identity', async () => {
    const invoke = vi.fn(async () => undefined);
    const program = new Command().exitOverride();
    configureMergeQueueCommand(program, invoke);
    const args = ['--attempt', '232', '--terminal-receipt', '/attempt-232-failed.json',
      '--terminal-receipt-sha256', 'a'.repeat(64), '--expected-revision', 'b'.repeat(64),
      '--run-id', '1788481850518348000-e36d9830ea1a078f', '--scope-sha256', 'c'.repeat(64),
      '--journal-sha256', 'd'.repeat(64), '--worker-id', 'semantic-repair-0001',
      '--predispatch-receipt', '/controller-predispatch-receipt.json',
      '--predispatch-receipt-sha256', 'e'.repeat(64)];
    await program.parseAsync(['node', 'yy', 'merge', 'recover-repair-predispatch', 'T123', ...args]);
    expect(invoke).toHaveBeenCalledWith('recover-repair-predispatch', 'T123', args);
  });

  it('forwards every exact authority-drift recovery identity', async () => {
    const invoke = vi.fn(async () => undefined);
    const program = new Command().exitOverride();
    configureMergeQueueCommand(program, invoke);
    await program.parseAsync(['node', 'yy', 'merge', 'recover-authority-drift', 'T123',
      '--attempt', '225', '--terminal-receipt', '/attempt-225-failed.json',
      '--terminal-receipt-sha256', 'abc', '--expected-revision', 'def']);
    expect(invoke).toHaveBeenCalledWith('recover-authority-drift', 'T123', [
      '--attempt', '225', '--terminal-receipt', '/attempt-225-failed.json',
      '--terminal-receipt-sha256', 'abc', '--expected-revision', 'def']);
  });

  it('forwards every exact stale-lifecycle supersession identity', async () => {
    const invoke = vi.fn(async () => undefined);
    const program = new Command().exitOverride();
    configureMergeQueueCommand(program, invoke);
    const options = ['--run-id', '1788467754907264000-b5fa7b4da494425e',
      '--expected-journal-revision', '7', '--expected-journal-sha256', 'a'.repeat(64),
      '--scope-sha256', 'b'.repeat(64), '--arbiter-attempt', '227',
      '--terminal-receipt', '/terminal.json', '--terminal-receipt-sha256', 'c'.repeat(64),
      '--recovered-task', 'WxK4xy', '--recovery-receipt', '/recovery.json',
      '--recovery-receipt-sha256', 'd'.repeat(64), '--expected-target-sha', 'e'.repeat(40),
      '--expected-current-fifo-sha256', 'f'.repeat(64)];
    await program.parseAsync(['node', 'yy', 'merge', 'supersede-lifecycle-journal', ...options]);
    expect(invoke).toHaveBeenCalledWith('supersede-lifecycle-journal', undefined, options);
  });

  it('forwards merge drive and resume with an optional frozen FIFO stop boundary', async () => {
    const invoke = vi.fn(async () => undefined);
    const program = new Command().exitOverride();
    configureMergeQueueCommand(program, invoke);
    await program.parseAsync(['node', 'yy', 'merge', 'drive', '--through', 'T123']);
    expect(invoke).toHaveBeenCalledWith('drive', undefined, ['--through', 'T123']);
    invoke.mockClear();
    await program.parseAsync(['node', 'yy', 'merge', 'resume', '--through', 'T123']);
    expect(invoke).toHaveBeenCalledWith('resume', undefined, ['--through', 'T123']);
  });

  it('forwards target arbiter observation and bounded on-demand runs', async () => {
    const invoke = vi.fn(async () => undefined);
    const program = new Command().exitOverride();
    configureMergeQueueCommand(program, invoke);
    await program.parseAsync(['node', 'yy', 'merge', 'arbiter', 'status']);
    expect(invoke).toHaveBeenCalledWith('arbiter-status');
    invoke.mockClear();
    await program.parseAsync(['node', 'yy', 'merge', 'arbiter', 'run', '--through', 'T123']);
    expect(invoke).toHaveBeenCalledWith('arbiter-run', undefined, ['--through', 'T123']);
  });

  it('forwards stable plan projection and ordinary execution options', async () => {
    const invoke = vi.fn(async () => undefined);
    const program = new Command().exitOverride();
    configureMergeQueueCommand(program, invoke);
    await program.parseAsync(['node', 'yy', 'merge', 'plan', 'T123', '--against', 'HEAD', '--json']);
    expect(invoke).toHaveBeenCalledWith('plan', 'T123', ['--against', 'HEAD', '--json']);
    invoke.mockClear();
    await program.parseAsync(['node', 'yy', 'merge', 'resolve', 'T123', '--plan-id', 'abc']);
    expect(invoke).toHaveBeenCalledWith('resolve', 'T123', ['--plan-id', 'abc']);
    invoke.mockClear();
    await program.parseAsync(['node', 'yy', 'merge', 'next']);
    expect(invoke).toHaveBeenCalledWith('next');
    invoke.mockClear();
    await program.parseAsync(['node', 'yy', 'merge', 'reconcile', 'plan', 'T123']);
    expect(invoke).toHaveBeenCalledWith('reconcile', undefined, ['plan', 'T123']);
    invoke.mockClear();
    await program.parseAsync(['node', 'yy', 'merge', 'reconcile', 'apply', 'T123',
      '--receipt', '/reconcile.json', '--receipt-sha256', 'def']);
    expect(invoke).toHaveBeenCalledWith('reconcile', undefined,
      ['apply', 'T123', '--receipt', '/reconcile.json', '--receipt-sha256', 'def']);
    invoke.mockClear();
    await program.parseAsync(['node', 'yy', 'merge', 'refresh', 'plan', 'T123']);
    expect(invoke).toHaveBeenCalledWith('refresh', undefined, ['plan', 'T123']);
    invoke.mockClear();
    await program.parseAsync(['node', 'yy', 'merge', 'refresh', 'apply', 'T123',
      '--receipt', '/receipt.json', '--receipt-sha256', 'abc']);
    expect(invoke).toHaveBeenCalledWith('refresh', undefined,
      ['apply', 'T123', '--receipt', '/receipt.json', '--receipt-sha256', 'abc']);
    invoke.mockClear();
    await program.parseAsync(['node', 'yy', 'merge', 'recover-authority-drift', 'T123',
      '--attempt', '225', '--terminal-receipt', '/attempt-225-failed.json',
      '--terminal-receipt-sha256', 'abc', '--expected-revision', 'def']);
    expect(invoke).toHaveBeenCalledWith('recover-authority-drift', 'T123', [
      '--attempt', '225', '--terminal-receipt', '/attempt-225-failed.json',
      '--terminal-receipt-sha256', 'abc', '--expected-revision', 'def']);
  });

  it('keeps next TASK_ID optional and requires task identity for recovery mutations', () => {
    const program = new Command();
    configureMergeQueueCommand(program, async () => undefined);
    const merge = program.commands.find((command) => command.name() === 'merge');
    expect(merge?.commands.map((command) => command.name())).toEqual(['status', 'drive', 'resume', 'arbiter', 'plan', 'next', 'resolve', 'review', 'reopen', 'recover-full-suite-failure', 'recover-repair-predispatch', 'recover-authority-drift', 'supersede-lifecycle-journal', 'withdraw', 'reconcile', 'refresh']);
    const command = (name: string) => merge?.commands.find((entry) => entry.name() === name);
    expect(command('status')?.registeredArguments).toHaveLength(0);
    expect(command('drive')?.registeredArguments).toHaveLength(0);
    expect(command('resume')?.registeredArguments).toHaveLength(0);
    expect(command('plan')?.registeredArguments[0]?.required).toBe(true);
    expect(command('next')?.registeredArguments[0]?.required).toBe(false);
    for (const name of ['resolve', 'review', 'reopen', 'recover-full-suite-failure',
      'recover-repair-predispatch', 'recover-authority-drift']) {
      expect(command(name)?.registeredArguments[0]?.required).toBe(true);
    }
    expect(command('supersede-lifecycle-journal')?.registeredArguments).toHaveLength(0);
  });

  it('forwards the bounded withdraw operator reason', async () => {
    const reasonInvoke = vi.fn(async () => undefined);
    const reasonProgram = new Command().exitOverride().configureOutput({ writeOut: () => undefined });
    configureMergeQueueCommand(reasonProgram, reasonInvoke);
    await reasonProgram.parseAsync(['node', 'yy', 'merge', 'withdraw', 'T123',
      '--reason', 'orphaned claim recovery']);
    expect(reasonInvoke).toHaveBeenCalledWith('withdraw', 'T123',
      ['--reason', 'orphaned claim recovery']);
    const plainInvoke = vi.fn(async () => undefined);
    const plainProgram = new Command().exitOverride().configureOutput({ writeOut: () => undefined });
    configureMergeQueueCommand(plainProgram, plainInvoke);
    await plainProgram.parseAsync(['node', 'yy', 'merge', 'withdraw', 'T123']);
    expect(plainInvoke).toHaveBeenCalledWith('withdraw', 'T123');
  });

  it('documents observation, fenced ownership, and explicit recovery semantics', () => {
    const program = new Command();
    configureMergeQueueCommand(program, async () => undefined);
    const merge = program.commands.find((command) => command.name() === 'merge');
    const command = (name: string) => merge?.commands.find((entry) => entry.name() === name);
    const arbiter = command('arbiter');
    const arbiterCommand = (name: string) => arbiter?.commands.find((entry) => entry.name() === name);
    const refresh = command('refresh');
    expect(command('status')?.description()).toContain('Read-only');
    expect(command('status')?.description()).toContain('bounded');
    expect(command('status')?.options.map((option) => option.long)).toEqual([
      '--detail', '--full', '--json',
    ]);
    expect(arbiterCommand('status')?.description()).toContain('Read-only');
    expect(arbiterCommand('run')?.description()).toContain('Explicit mutation');
    expect(refresh?.description()).toContain('queued candidate');
    expect(refresh?.commands.map((entry) => entry.name())).toEqual(['plan', 'apply']);
    expect(refresh?.commands[0]?.registeredArguments[0]?.required).toBe(true);
    expect(refresh?.commands[1]?.options.map((option) => option.long)).toEqual([
      '--receipt', '--receipt-sha256',
    ]);
    expect(command('resume')?.description()).toContain('existing fenced target arbiter');
    expect(command('next')?.description()).toContain('Explicit recovery mutation');
    expect(command('next')?.description()).toContain('continue paused evidence');
    expect(command('next')?.registeredArguments[0]?.description).toContain('evidence/review');
    expect(command('resolve')?.description()).toContain('Explicit recovery mutation');
    expect(command('recover-full-suite-failure')?.description()).toContain('receipt-bound');
    expect(command('recover-full-suite-failure')?.description()).toContain('one deterministic');
    expect(command('recover-repair-predispatch')?.description()).toContain('Receipt-bound');
    expect(command('recover-repair-predispatch')?.description()).toContain('exact existing');
    expect(command('recover-authority-drift')?.description()).toContain('receipt-bound recovery');
    expect(command('recover-authority-drift')?.description()).toContain('pre-CAS');
    expect(command('supersede-lifecycle-journal')?.description()).toContain('Terminalize');
  });

  it('checkpoints only after successful terminal merge and Kanban finalization truth', async () => {
    const checkpoint = vi.fn(async () => ({ attempted: true, ok: true }));
    for (const operation of ['next', 'resolve', 'resume'] as const) {
      checkpoint.mockClear();
      await checkpointMergeQueueAfterFinalization(operation, '/controller', 0, mergedResult, checkpoint);
      expect(checkpoint).toHaveBeenCalledWith('/controller', 0);
    }

    for (const [operation, exitCode, result] of [
      ['status', 0, mergedResult],
      ['review', 0, mergedResult],
      ['reopen', 0, mergedResult],
      ['next', 2, mergedResult],
      ['next', 0, { outcome: 'RISK_EVIDENCE_READY' }],
      ['resolve', 0, { outcome: 'AWAITING_RISK' }],
      ['next', 0, { outcome: 'MERGED', post_integration: { kanban_finalization: { status: 'failed' } } }],
    ] as const) {
      checkpoint.mockClear();
      await checkpointMergeQueueAfterFinalization(operation, '/controller', exitCode, result, checkpoint);
      expect(checkpoint).not.toHaveBeenCalled();
    }
  });

  it('extracts noisy terminal JSON at invocation level while streaming stdout', async () => {
    const root = await controllerFixture();
    await writeMergeRuntime(root, [
      'import json',
      'print("managed runtime refresh: completed")',
      `print(json.dumps(${JSON.stringify(mergedResult)}))`,
    ].join('\n'));
    const checkpoint = vi.fn(async () => ({ attempted: true, ok: true }));
    const streamed = vi.spyOn(process.stdout, 'write').mockImplementation(() => true);

    await invokeMergeQueueAtController('next', root, { ...process.env }, undefined, checkpoint);

    expect(checkpoint).toHaveBeenCalledWith(root, 0);
    expect(streamed.mock.calls.flatMap((row) => row).join('')).toContain('managed runtime refresh: completed');
  });

  it.each([
    ['malformed terminal line', `print(${JSON.stringify(JSON.stringify(mergedResult))})\nprint("{not-json")`],
    ['oversized terminal line', `print(${JSON.stringify(JSON.stringify(mergedResult))})\nprint("x" * ${MAX_MERGE_RESULT_LINE_CHARS + 1})`],
    ['no terminal JSON', 'print("progress only")'],
  ])('does not infer terminal merge success from %s', async (_name, body) => {
    const root = await controllerFixture();
    await writeMergeRuntime(root, body);
    const checkpoint = vi.fn(async () => ({ attempted: true, ok: true }));
    vi.spyOn(process.stdout, 'write').mockImplementation(() => true);

    await invokeMergeQueueAtController('next', root, { ...process.env }, undefined, checkpoint);

    expect(checkpoint).not.toHaveBeenCalled();
  });

  it('cleans real-Git Kanban finalization dirt and retries as an idempotent no-op', async () => {
    const root = await controllerFixture();
    for (const relative of ['tasks/T123.md', 'ledger/events.jsonl', 'state/tasks.json']) {
      await fs.writeFile(path.join(root, '.juno_task', relative), 'durable merged truth\n');
    }
    expect(git(root, 'status', '--porcelain')).not.toBe('');
    const previousTaskRoot = process.env.JUNO_TASK_ROOT;
    const previousCheckpointActive = process.env.JUNO_CONTROLLER_CHECKPOINT_ACTIVE;
    process.env.JUNO_TASK_ROOT = '';
    delete process.env.JUNO_CONTROLLER_CHECKPOINT_ACTIVE;
    try {
      await checkpointMergeQueueAfterFinalization('next', root, 0, mergedResult);
      expect(git(root, 'status', '--porcelain')).toBe('');
      expect(git(root, 'show', '--name-only', '--format=', 'HEAD').split('\n').sort()).toEqual([
        '.juno_task/ledger/events.jsonl',
        '.juno_task/state/tasks.json',
        '.juno_task/tasks/T123.md',
      ]);
      expect(git(root, 'show', 'HEAD:product.txt')).toBe('product');
      const checkpointHead = git(root, 'rev-parse', 'HEAD');
      await checkpointMergeQueueAfterFinalization('next', root, 0, mergedResult);
      expect(git(root, 'rev-parse', 'HEAD')).toBe(checkpointHead);
    } finally {
      if (previousTaskRoot === undefined) delete process.env.JUNO_TASK_ROOT;
      else process.env.JUNO_TASK_ROOT = previousTaskRoot;
      if (previousCheckpointActive === undefined) delete process.env.JUNO_CONTROLLER_CHECKPOINT_ACTIVE;
      else process.env.JUNO_CONTROLLER_CHECKPOINT_ACTIVE = previousCheckpointActive;
    }
  }, 30_000);

  it('preserves a merged result and emits recovery when the real checkpointer fails', async () => {
    const root = await controllerFixture();
    await fs.writeFile(path.join(root, '.juno_task', 'tasks', 'T123.md'), 'durable merged truth\n');
    await fs.writeFile(path.join(root, '.juno_task', 'scripts', 'controller_checkpoint.py'), '#!/usr/bin/env python3\nraise SystemExit(2)\n');
    const before = git(root, 'rev-parse', 'HEAD');
    const warning = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const previousTaskRoot = process.env.JUNO_TASK_ROOT;
    const previousCheckpointActive = process.env.JUNO_CONTROLLER_CHECKPOINT_ACTIVE;
    process.env.JUNO_TASK_ROOT = '';
    delete process.env.JUNO_CONTROLLER_CHECKPOINT_ACTIVE;
    try {
      await expect(checkpointMergeQueueAfterFinalization('next', root, 0, mergedResult)).resolves.toBeUndefined();
    } finally {
      if (previousTaskRoot === undefined) delete process.env.JUNO_TASK_ROOT;
      else process.env.JUNO_TASK_ROOT = previousTaskRoot;
      if (previousCheckpointActive === undefined) delete process.env.JUNO_CONTROLLER_CHECKPOINT_ACTIVE;
      else process.env.JUNO_CONTROLLER_CHECKPOINT_ACTIVE = previousCheckpointActive;
    }
    expect(git(root, 'rev-parse', 'HEAD')).toBe(before);
    expect(git(root, 'status', '--porcelain')).toContain('.juno_task/tasks/T123.md');
    expect(warning).toHaveBeenCalledWith(expect.stringContaining('WARNING: Controller checkpoint failed after finalization'));
    expect(warning).toHaveBeenCalledWith(expect.stringContaining('blocker=unknown'));
    expect(warning).toHaveBeenCalledWith(expect.stringContaining('yy doctor workspace'));
    expect(warning).not.toHaveBeenCalledWith(expect.stringContaining('commit manually'));
  }, 30_000);
});
