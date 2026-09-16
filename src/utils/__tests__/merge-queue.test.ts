import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const repository = resolve(import.meta.dirname, '../../../..');

describe('native Git delivery managed runtime', () => {
  it('ships one small task-selected adapter with real-Git conflict canaries', () => {
    const runtime = resolve(repository, 'juno-code/src/templates/scripts/merge_queue.py');
    const installed = resolve(repository, '.juno_task/scripts/merge_queue.py');
    const tests = resolve(repository, 'juno-code/src/templates/scripts/tests/test_merge_queue.py');
    const source = readFileSync(runtime, 'utf8');
    expect(readFileSync(installed)).toEqual(readFileSync(runtime));
    expect(source.split('\n').length - 1).toBeLessThan(800);
    expect(source).toContain('"update-ref", target_ref');
    expect(source).toContain('def land(');
    expect(source).toContain('def project(');
    expect(source).not.toContain('managed_agent_runner');
    expect(source).not.toContain('risk_policy');
    expect(source).not.toContain('arbiter');
    expect(source).not.toContain('review_candidate');
    execFileSync('python3', [tests], {
      cwd: repository,
      env: { ...process.env, PYTHONPYCACHEPREFIX: '/tmp/juno-merge-queue-test-pycache' },
      stdio: 'pipe',
    });
  }, 120_000);
});
