import { Command } from 'commander';
import { describe, expect, it } from 'vitest';
import {
  classifyExplicitInvocation,
  formatExplicitInvocationError,
  markTransparentDelegate,
} from '../explicit-command.js';

function surface(): Command {
  const program = new Command('yylo');
  program.helpOption('-h, --help', 'Display help information');
  program.option('-q, --quiet').option('-c, --config <path>');
  program.option('-p, --prompt [text]').option('-f, --prompt-file <path>');
  const integration = program.command('integration');
  integration.command('sync').option('--dry-run');
  integration.command('status');
  integration.command('runtime').command('refresh').requiredOption('--previous-sha <sha>');
  markTransparentDelegate(program.command('benchmark [args...]').allowUnknownOption(true));
  program.command('pi').argument('[prompt...]');
  return program;
}

describe('explicit command preflight', () => {
  it.each([
    [['integration', 'sync'], 'supported-command'],
    [['--quiet', 'integration', 'sync'], 'supported-command'],
    [['--help'], 'help'],
    [['-h'], 'help'],
    [['integration', '--help'], 'help'],
    [['integration', '-h'], 'help'],
    [['integration', 'runtime', 'refresh', '--help'], 'help'],
    [['integration', 'runtime', 'refresh', '-h'], 'help'],
    [['benchmark', '--help'], 'supported-command'],
    [['benchmark', '-h'], 'supported-command'],
    [['benchmark', 'run', '--help'], 'supported-command'],
    [['pi', 'fix', 'tests'], 'supported-command'],
    [['a quoted free-form prompt'], 'prompt'],
    [['fix', 'tests'], 'prompt'],
    [['please', 'list', 'tasks'], 'prompt'],
    [['debug', 'status', 'endpoint'], 'prompt'],
    [['explain', 'sync', 'behavior'], 'prompt'],
    [['future-command', 'status'], 'prompt'],
    [['future-command', '--dry-run'], 'prompt'],
    [['@@close_loop'], 'prompt'],
    [['/skill:ralph-loop-yylo', '##T1'], 'prompt'],
    [['--', 'integration', 'sync'], 'prompt'],
    [['-p', 'integration sync'], 'prompt'],
    [['--prompt-file', 'prompt.md'], 'prompt'],
    [['--prompt-file', '--help'], 'prompt'],
  ] as const)('classifies %j as %s', (argv, kind) => {
    expect(classifyExplicitInvocation(argv, surface()).kind).toBe(kind);
  });

  it.each([
    [['integration', 'mystery'], 'unknown-command', 'mystery'],
    [['integration', '--future-option'], 'unknown-option', '--future-option'],
    [['integration', 'sync', '--future-option'], 'unknown-option', '--future-option'],
    [['--future-option'], 'unknown-option', '--future-option'],
  ] as const)('fails closed for objective explicit input %j', (argv, kind, token) => {
    expect(classifyExplicitInvocation(argv, surface())).toEqual({ kind, token });
  });

  it('reports executable and version identity with prompt recovery syntax', () => {
    expect(formatExplicitInvocationError(
      { kind: 'unknown-command', token: 'integration' },
      '/candidate/dist/bin/cli.mjs',
      '2.1.2',
    )).toContain('effective executable: /candidate/dist/bin/cli.mjs\neffective version: 2.1.2');
  });
});
