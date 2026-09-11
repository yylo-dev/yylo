import { Command } from 'commander';
import * as os from 'node:os';
import * as path from 'node:path';
import fs from 'fs-extra';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  configureMergeCommand,
  invokeMergeAtController,
  mergeControlOperation,
} from '../commands/merge.js';

const temporaryRoots: string[] = [];

afterEach(async () => {
  vi.restoreAllMocks();
  await Promise.all(temporaryRoots.splice(0).map((root) => fs.remove(root)));
});

describe('native Git merge CLI', () => {
  it('exposes only status, one-task land, and separate projection', () => {
    const program = new Command();
    configureMergeCommand(program, async () => undefined);
    const merge = program.commands.find((command) => command.name() === 'merge');
    expect(merge?.commands.map((command) => command.name())).toEqual([
      'status', 'land', 'project',
    ]);
    expect(merge?.description()).toContain('one task');
    expect(merge?.commands.find((command) => command.name() === 'land')?.description())
      .toContain('never runs tests, reviews, or models');
    expect(merge?.commands.find((command) => command.name() === 'project')?.description())
      .toContain('Separately project');
  });

  it('routes observation separately from mutations', () => {
    expect(mergeControlOperation('status')).toBe('kanban');
    expect(mergeControlOperation('land')).toBe('orchestration');
    expect(mergeControlOperation('project')).toBe('orchestration');
  });

  it.each([
    { argv: ['status'], expected: ['status'] },
    { argv: ['status', 'T123'], expected: ['status', 'T123'] },
    { argv: ['land', 'T123'], expected: ['land', 'T123', []] },
    { argv: ['project', 'T123'], expected: ['project', 'T123'] },
  ] as const)('forwards $argv', async ({ argv, expected }) => {
    const invoke = vi.fn(async () => undefined);
    const program = new Command().exitOverride();
    configureMergeCommand(program, invoke);
    await program.parseAsync(['node', 'yy', 'merge', ...argv]);
    expect(invoke).toHaveBeenCalledWith(...expected);
  });

  it('binds a resolved candidate to its exact observed target', async () => {
    const invoke = vi.fn(async () => undefined);
    const program = new Command().exitOverride();
    configureMergeCommand(program, invoke);
    await program.parseAsync(['node', 'yy', 'merge', 'land', 'T123',
      '--candidate', 'a'.repeat(40), '--expected-target', 'b'.repeat(40)]);
    expect(invoke).toHaveBeenCalledWith('land', 'T123', [
      '--candidate', 'a'.repeat(40), '--expected-target', 'b'.repeat(40),
    ]);
  });

  it('refuses a partially specified resolved candidate', async () => {
    const program = new Command().exitOverride();
    configureMergeCommand(program, async () => undefined);
    await expect(program.parseAsync(['node', 'yy', 'merge', 'land', 'T123',
      '--candidate', 'a'.repeat(40)])).rejects.toThrow(
      '--candidate and --expected-target must be supplied together',
    );
  });

  it('invokes the controller adapter without parsing or coupling projection output', async () => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'native-merge-cli-'));
    temporaryRoots.push(root);
    await fs.ensureDir(path.join(root, '.juno_task/scripts'));
    await fs.writeFile(path.join(root, '.juno_task/scripts/merge_queue.py'), [
      '#!/usr/bin/env python3',
      'import json, sys',
      'print(json.dumps({"operation": sys.argv[1], "task": sys.argv[2]}))',
    ].join('\n'));
    const output: string[] = [];
    const write = vi.spyOn(process.stdout, 'write').mockImplementation((chunk) => {
      output.push(String(chunk));
      return true;
    });

    await invokeMergeAtController('land', root, { ...process.env }, 'T123');

    expect(process.exitCode ?? 0).toBe(0);
    expect(write).not.toHaveBeenCalled(); // Child inherits stdout directly.
    expect(output).toEqual([]);
  });

  it('keeps packaged runtime under the frozen 800-line production budget', async () => {
    const repository = path.resolve(import.meta.dirname, '../../../..');
    const runtime = await fs.readFile(
      path.join(repository, 'juno-code/src/templates/scripts/merge_queue.py'), 'utf8',
    );
    const cli = await fs.readFile(
      path.join(repository, 'juno-code/src/cli/commands/merge.ts'), 'utf8',
    );
    const integration = await fs.readFile(
      path.join(repository, 'juno-code/src/templates/scripts/integration_workspace.py'), 'utf8',
    );
    expect(runtime.split('\n').length - 1 + cli.split('\n').length - 1 + 18)
      .toBeLessThanOrEqual(800);
    expect(integration).toContain('def integration_target_lock(');
    expect(integration).not.toContain('import merge_queue');
    expect(runtime).not.toContain('managed_agent_runner');
    expect(runtime).not.toContain('risk_policy');
    expect(runtime).not.toContain('arbiter');
    expect(runtime).not.toContain('FIFO');
    expect(runtime).toContain('update-ref');
  });
});
