import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

const repository = path.resolve(import.meta.dirname, '../../../..');
const fixture = path.join(
  repository,
  'juno-code/scripts/test-performance/native-git-merge-characterization.mjs',
);

describe('native Git merge characterization', () => {
  it('preserves changes and isolates one task conflict from unrelated delivery', () => {
    const run = spawnSync(process.execPath, [fixture], {
      cwd: repository,
      encoding: 'utf8',
      timeout: 30_000,
    });
    expect(run.status, run.stderr).toBe(0);
    const result = JSON.parse(run.stdout) as {
      schema_version: string;
      topology: string;
      scenario_count: number;
      model_calls: number;
      scenarios: Array<Record<string, unknown>>;
    };
    const byName = new Map(result.scenarios.map((scenario) => [scenario.name, scenario]));

    expect(result.schema_version).toBe('yylo.native_git_merge_characterization.v1');
    expect(result.topology).toContain('expected-old');
    expect(result.scenario_count).toBe(11);
    expect(result.model_calls).toBe(0);
    expect(result.scenarios.every((scenario) => scenario.passed === true)).toBe(true);
    expect(byName.get('direct-fast-forward')).toMatchObject({ preserves_both_changes: true });
    expect(byName.get('divergent-clean-merge')).toMatchObject({
      merge_parents: 2,
      preserves_both_changes: true,
    });
    expect(byName.get('same-file-disjoint-hunks')).toMatchObject({ preserves_both_changes: true });
    expect(byName.get('conflict-x-does-not-block-unrelated-y')).toMatchObject({
      x_conflict_preserved: true,
      y_landed: true,
      fifo_wait_due_to_x: 0,
    });
    expect(byName.get('competing-expected-old-updates')).toMatchObject({
      winner_preserved: true,
      stale_writer_rejected: true,
    });
  });

  it('keeps target movement, dirty bytes, and projection failure explicit', () => {
    const run = spawnSync(process.execPath, [fixture], {
      cwd: repository,
      encoding: 'utf8',
      timeout: 30_000,
    });
    expect(run.status, run.stderr).toBe(0);
    const result = JSON.parse(run.stdout) as { scenarios: Array<Record<string, unknown>> };
    const byName = new Map(result.scenarios.map((scenario) => [scenario.name, scenario]));

    expect(byName.get('target-moves-during-validation')).toMatchObject({
      stale_candidate_rejected: true,
      retry_required: true,
    });
    expect(byName.get('dirty-source-bytes-preserved')).toMatchObject({
      dirty_bytes_preserved: 20,
    });
    expect(byName.get('already-contained-source')).toMatchObject({
      contained: true,
      duplicate_integration: false,
    });
    expect(byName.get('crash-before-update')).toMatchObject({
      target_unchanged: true,
      candidate_recoverable: true,
    });
    expect(byName.get('git-success-ledger-failure')).toMatchObject({
      git_success_reportable: true,
      ledger_projection_retryable: true,
      duplicate_integration: false,
    });
    expect(byName.get('submodule-gitlink-change')).toMatchObject({ gitlink_preserved: true });

    const report = fs.readFileSync(
      path.join(repository, 'juno-code/docs/native-git-merge-characterization.md'),
      'utf8',
    );
    expect(report).toContain('`2a46ae2e5ca4ce01c4a9cc4e78097bd2ff58cd57`');
    expect(report).toContain('| **Direct merge-only production total** | **9,041** |');
    expect(report).toContain('at least `(9041-800)/9041 = 91.15%` deletion');
    expect(report).toContain('private detached candidate');
    expect(report).toContain('coordination interventions');
    expect(report).toContain('remain **unknown**');
  });
});
