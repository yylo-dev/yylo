import { Command } from 'commander';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { configureTmuxCommand } from '../commands/tmux.js';
import {
  TmuxWorkspace,
  type TmuxRunOptions,
  type TmuxRunResult,
} from '../../core/tmux-workspace.js';

const inventory =
  ['safe', '@1', '0', 'main', '%1', '0', 'shell', '1', 'bash', '1', '0', '', '', ''].join('\t') +
  '\n';

afterEach(() => vi.restoreAllMocks());

describe('tmux command', () => {
  it('registers a distinct command tree and emits stable status JSON', async () => {
    const calls: string[][] = [];
    const runner = (args: readonly string[], _options: TmuxRunOptions): TmuxRunResult => {
      calls.push([...args]);
      return { status: 0, stdout: args[0] === 'list-panes' ? inventory : '' };
    };
    const output: string[] = [];
    vi.spyOn(console, 'log').mockImplementation((value) => output.push(String(value)));
    const program = new Command();
    program.exitOverride();
    configureTmuxCommand(program, () => new TmuxWorkspace(runner));
    await program.parseAsync(['node', 'yy', 'tmux', 'status', '--session', 'safe', '--json']);
    expect(JSON.parse(output.join('\n'))).toMatchObject({
      schemaVersion: 'yylo.tmux-workspace.v1',
      session: 'safe',
    });
    expect(calls[0]).toEqual(['list-panes', '-s', '-t', 'safe', '-F', expect.any(String)]);
    expect(program.commands.find((item) => item.name() === 'tmux')).toBeDefined();
    expect(program.commands.find((item) => item.name() === 'workspace')).toBeUndefined();
  });

  it('keeps unread list observational', async () => {
    const calls: string[][] = [];
    const legacy =
      [
        'safe',
        '@1',
        '0',
        'main',
        '%1',
        '0',
        'shell',
        '1',
        'bash',
        '1',
        '0',
        '',
        '',
        'abcdefghijklmnop',
      ].join('\t') + '\n';
    const runner = (args: readonly string[], _options: TmuxRunOptions): TmuxRunResult => {
      calls.push([...args]);
      return { status: 0, stdout: legacy };
    };
    const output: string[] = [];
    vi.spyOn(console, 'log').mockImplementation((value) => output.push(String(value)));
    const program = new Command();
    program.exitOverride();
    configureTmuxCommand(program, () => new TmuxWorkspace(runner));
    await program.parseAsync(['node', 'yy', 'tmux', 'unread', 'list', '--session', 'safe']);
    expect(output).toEqual(['0:main']);
    expect(calls.every((call) => call[0] !== 'set-option')).toBe(true);
  });
});
