import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { resolvePromptMacros } from '../prompt-macro-resolver.js';

describe('prompt-macro-resolver', () => {
  it('preserves a lifecycle caller payload byte-for-byte exactly once', () => {
    const payload = '## T1\n@@no_code\n"quotes" `ticks` $ARGUMENTS $1 $2 $@ $(echo no)';
    const lifecycle = readFileSync(
      resolve(process.cwd(), 'src/templates/prompts/life_cycle.md'),
      'utf8',
    );
    const result = resolvePromptMacros(`@@life_cycle ${payload}`, {
      dictionary: { life_cycle: lifecycle, no_code: 'MUST NOT EXPAND' },
      maxDepth: 8,
    });
    expect(result.resolvedPrompt).toBe(`${lifecycle} ${payload}`);
    expect(result.resolvedPrompt).toContain('Keep checks outside merge');
    expect(result.resolvedPrompt).toContain('Merge launches');
    expect(result.resolvedPrompt).toContain('zero models');
    expect(result.resolvedPrompt).toContain('yy merge land TASK_ID');
    expect(result.resolvedPrompt).toContain('yy merge project TASK_ID');
    expect(result.resolvedPrompt).not.toContain('sole lifecycle-semantic review owner');
    expect(result.resolvedPrompt).not.toContain('REVIEW_FINDINGS_EXHAUSTED');
    expect(result.resolvedPrompt.indexOf(payload)).toBe(result.resolvedPrompt.lastIndexOf(payload));
    expect(result.warnings).toEqual([]);
  });
  it('expands exact case-sensitive keys with local/global dictionary values', () => {
    const result = resolvePromptMacros('Do @@ship now', {
      dictionary: {
        git: 'commit changes',
        ship: 'run tests then @@git',
      },
      maxDepth: 10,
    });

    expect(result.resolvedPrompt).toBe('Do run tests then commit changes now');
    expect(result.warnings).toEqual([]);
  });

  it('keeps unresolved tokens unchanged and warns', () => {
    const result = resolvePromptMacros('Use @@miss token', {
      dictionary: {},
      maxDepth: 10,
    });

    expect(result.resolvedPrompt).toBe('Use @@miss token');
    expect(result.warnings).toEqual([
      expect.objectContaining({ code: 'unresolved', key: 'miss', token: '@@miss' }),
    ]);
  });

  it('does not partially match tokens or invalid boundaries', () => {
    const result = resolvePromptMacros('@@gitignore x@@git @@git, @@git', {
      dictionary: { git: 'commit', gitignore: 'ignore file' },
      maxDepth: 10,
    });

    expect(result.resolvedPrompt).toBe('ignore file x@@git @@git, commit');
    expect(result.warnings).toEqual([]);
  });

  it('supports escaped tokens via \\@@key literal form', () => {
    const result = resolvePromptMacros('Print \\@@ship and @@ship', {
      dictionary: { ship: 'deploy' },
      maxDepth: 10,
    });

    expect(result.resolvedPrompt).toBe('Print @@ship and deploy');
    expect(result.warnings).toEqual([]);
  });

  it('detects circular references and leaves cycle token unchanged', () => {
    const result = resolvePromptMacros('Run @@a', {
      dictionary: { a: '@@b', b: '@@a' },
      maxDepth: 10,
    });

    expect(result.resolvedPrompt).toBe('Run @@a');
    expect(result.warnings).toEqual([
      expect.objectContaining({ code: 'cycle', key: 'a', token: '@@a' }),
    ]);
  });

  it('stops recursive expansion at maxDepth and warns', () => {
    const result = resolvePromptMacros('Start @@one', {
      dictionary: {
        one: '@@two',
        two: '@@three',
        three: 'done',
      },
      maxDepth: 2,
    });

    expect(result.resolvedPrompt).toBe('Start @@three');
    expect(result.warnings).toEqual([
      expect.objectContaining({ code: 'max-depth', key: 'three', token: '@@three' }),
    ]);
  });
});
