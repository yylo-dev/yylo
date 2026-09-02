import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const repository = resolve(import.meta.dirname, '../../../..');

describe('Bolt merge queue managed runtime', () => {
  it('ships one deterministic engine with real-Git conflict canaries', () => {
    const runtime = resolve(repository, 'juno-code/src/templates/scripts/merge_queue.py');
    const tests = resolve(repository, 'juno-code/src/templates/scripts/tests/test_merge_queue.py');
    const source = readFileSync(runtime, 'utf8');
    expect(source).toContain('LOCK_EX | fcntl.LOCK_NB');
    expect(source).toContain('"update-ref", target_ref');
    expect(source).toContain('def merge_resolve(');
    expect(source).not.toContain('integration_candidate');
    expect(source).not.toContain('controller-sync');
    // The managed merge engine no longer depends on the agent runner.
    expect(source.match(/managed_agent_runner/g) ?? []).toHaveLength(0);
    expect(source).not.toMatch(/^import managed_agent_runner\b/m);
    execFileSync('python3', [tests], {
      cwd: repository,
      env: { ...process.env, PYTHONPYCACHEPREFIX: '/tmp/juno-merge-queue-test-pycache' },
      stdio: 'pipe',
    });
  // The 109-case real-Git matrix regularly exceeds five minutes on an
  // eight-core host once the fast suite's other process fixtures have run.
  // Keep the wrapper bounded without forcing Vitest to retry a successful
  // synchronous Python child after it returns.
  }, 900_000);
});
