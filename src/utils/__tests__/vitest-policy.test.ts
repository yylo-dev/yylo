import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import { quarantineRetryCount } from '../../../vitest.config';

const REPOSITORY = path.resolve(import.meta.dirname, '../../../..');
const CONFIG_SOURCE = fs.readFileSync(path.join(REPOSITORY, 'juno-code', 'vitest.config.ts'), 'utf8');

/**
 * Wave 1 (7djT8N) runner policy contracts: Node is the default environment,
 * browser dependence is an explicit per-file opt-in, ordinary failures run
 * exactly once, and retries exist only as a reported quarantine that can never
 * feed lifecycle admission receipts.
 */
describe('Vitest runner policy', () => {
  it('defaults to the Node environment and opts into happy-dom per file', () => {
    expect(CONFIG_SOURCE).toContain("environment: 'node'");
    expect(CONFIG_SOURCE).not.toContain("environment: 'happy-dom'");
    // The opt-in mechanism is documented in the config for future DOM tests.
    expect(CONFIG_SOURCE).toContain('@vitest-environment happy-dom');
  });

  it('runs Node files without a DOM while the happy-dom docblock still yields one', () => {
    // Opt-in proof: the probe file declares the docblock and receives a DOM.
    const probe = path.join(
      REPOSITORY,
      'juno-code/src/utils/__tests__/fixtures/happy-dom-probe.test.ts',
    );
    expect(fs.readFileSync(probe, 'utf8')).toContain('@vitest-environment happy-dom');
    const spawnedDom = execFileSync(
      'npx',
      ['vitest', 'run', 'src/utils/__tests__/fixtures/happy-dom-probe.test.ts'],
      { cwd: path.join(REPOSITORY, 'juno-code'), encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] },
    );
    expect(spawnedDom).toContain('1 passed');

    // Node-default proof: an isolated run containing only Node-environment
    // files executes with no browser globals. The probe is generated per run
    // so a co-resident happy-dom file in the outer suite cannot leak worker
    // globals into this assertion.
    const generated = path.join(
      REPOSITORY,
      `juno-code/src/utils/__tests__/fixtures/.node-env-probe-${process.pid}.test.ts`,
    );
    try {
      fs.writeFileSync(
        generated,
        [
          "import { describe, expect, it } from 'vitest';",
          "describe('node environment probe', () => {",
          "  it('executes without browser globals', () => {",
          '    expect(typeof globalThis.document).toBe("undefined");',
          '    expect(typeof globalThis.window).toBe("undefined");',
          '  });',
          '});',
          '',
        ].join('\n'),
      );
      const spawnedNode = execFileSync(
        'npx',
        ['vitest', 'run', `src/utils/__tests__/fixtures/.node-env-probe-${process.pid}.test.ts`],
        { cwd: path.join(REPOSITORY, 'juno-code'), encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] },
      );
      expect(spawnedNode).toContain('1 passed');
    } finally {
      fs.rmSync(generated, { force: true });
    }
  }, 180_000);

  it('makes ordinary failures execute exactly once', () => {
    expect(CONFIG_SOURCE).toContain('retry: quarantineRetryCount()');
    expect(CONFIG_SOURCE).not.toMatch(/retry:\s*\d+/);
  });

  it('treats retries as an explicit reported quarantine only', () => {
    expect(CONFIG_SOURCE).toContain('YYLO_TEST_QUARANTINE_RETRIES');
    expect(CONFIG_SOURCE).toContain('advisory-not-first-pass');
    expect(quarantineRetryCount({})).toBe(0);
    expect(quarantineRetryCount({ YYLO_TEST_QUARANTINE_RETRIES: '' })).toBe(0);
    expect(quarantineRetryCount({ YYLO_TEST_QUARANTINE_RETRIES: '0' })).toBe(0);
    expect(quarantineRetryCount({ YYLO_TEST_QUARANTINE_RETRIES: '2' })).toBe(2);
    expect(() => quarantineRetryCount({ YYLO_TEST_QUARANTINE_RETRIES: '-1' })).toThrow(/integer in \[0, 5\]/);
    expect(() => quarantineRetryCount({ YYLO_TEST_QUARANTINE_RETRIES: '6' })).toThrow(/integer in \[0, 5\]/);
    expect(() => quarantineRetryCount({ YYLO_TEST_QUARANTINE_RETRIES: 'yes' })).toThrow(/integer in \[0, 5\]/);
  });

  it('keeps lifecycle admission argv free of quarantine environment', () => {
    for (const policyPath of [
      path.join(REPOSITORY, '.juno_task/config/task-workspace.json'),
      path.join(REPOSITORY, 'juno-code/src/templates/config/task-workspace.json'),
    ]) {
      const policy = JSON.parse(fs.readFileSync(policyPath, 'utf8')) as {
        focused_validation: Array<{ argv: string[]; env?: Record<string, string> }>;
        full_suite_validation: { argv: string[]; env?: Record<string, string> };
      };
      const commands = [
        ...policy.focused_validation.map((row) => row.argv),
        policy.full_suite_validation.argv,
      ];
      for (const argv of commands) {
        expect(argv.join(' ')).not.toContain('YYLO_TEST_QUARANTINE_RETRIES');
      }
    }
  });

  it('preserves the managed-install serialization lanes', () => {
    expect(CONFIG_SOURCE).toContain('managed-project-assets.test.ts');
    expect(CONFIG_SOURCE).toContain('script-installer.test.ts');
    expect(CONFIG_SOURCE).toContain('poolMatchGlobs');
  });
});
