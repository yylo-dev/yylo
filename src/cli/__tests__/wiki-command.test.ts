import { Command } from 'commander';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { classifyExplicitInvocation } from '../../utils/explicit-command.js';

const { delegate } = vi.hoisted(() => ({ delegate: vi.fn().mockResolvedValue(undefined) }));
vi.mock('../commands/kanban.js', () => ({ invokeKanban: delegate }));
import { configureWikiCommand } from '../commands/wiki.js';

function program(): Command {
  const root = new Command().exitOverride();
  configureWikiCommand(root);
  return root;
}

beforeEach(() => delegate.mockReset().mockResolvedValue(undefined));

describe('wiki is only a Ledger proxy', () => {
  it.each([
    [], ['--help'], ['get', 'Abc123', '--raw'],
    ['search', '--text', 'a `b` $(c) $d', '--projection', 'summary', '-f', 'json'],
    ['create', '--title', 'a b', '--file', '-'],
    ['update', 'Abc123', '--expected-revision', '2', '--old-file', '/tmp/old.md', '--new-file', '/tmp/new.md'],
    ['--path'], ['show', 'controller/lifecycle'], ['unknown-action'],
  ])('delegates the untouched wiki tail %j including obsolete syntax', async (...tail: string[]) => {
    const root = program();
    expect(classifyExplicitInvocation(['wiki', ...tail], root).kind).toBe('supported-command');
    await root.parseAsync(['wiki', ...tail], { from: 'user' });
    expect(delegate).toHaveBeenCalledOnce();
    expect(delegate).toHaveBeenCalledWith(['wiki', ...tail]);
  });

  it('has no file-backed options or subcommands', () => {
    const wiki = program().commands[0]!;
    expect(wiki.commands).toHaveLength(0);
    expect(wiki.options).toHaveLength(0);
  });

  it('does not substitute local content when Ledger fails', async () => {
    delegate.mockRejectedValueOnce(new Error('Ledger unavailable'));
    await expect(program().parseAsync(['wiki', 'get', 'Abc123'], { from: 'user' }))
      .rejects.toThrow('Ledger unavailable');
    expect(delegate).toHaveBeenCalledOnce();
  });
});
