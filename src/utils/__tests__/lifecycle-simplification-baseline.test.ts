import { spawnSync } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { describe, expect, it } from 'vitest';

const repository = path.resolve(import.meta.dirname, '../../../..');
const driverPath = path.join(
  repository,
  'juno-code/scripts/test-performance/lifecycle-simplification-baseline.mjs',
);
const corpusPath = path.join(
  repository,
  'juno-code/scripts/test-performance/lifecycle-simplification-corpus.v1.json',
);

async function driver(): Promise<Record<string, any>> {
  return import(pathToFileURL(driverPath).href) as Promise<Record<string, any>>;
}

describe('frozen lifecycle simplification baseline', () => {
  it('reproduces source-bound medians, ranges, denominators, and fixed weights', async () => {
    const api = await driver();
    const corpus = JSON.parse(fs.readFileSync(corpusPath, 'utf8')) as Record<string, any>;
    const first = api.aggregateManifest(corpus, { verifySource: true, repository });
    const second = api.aggregateManifest(corpus, { verifySource: true, repository });

    expect(first).toEqual(second);
    expect(first.complete).toBe(true);
    expect(first.reference.commit).toBe('de9e3a8caa9d439654fe25dd06ecd5aae3af0bb4');
    expect(first.corpus_sha256).toMatch(/^[0-9a-f]{64}$/);
    expect(
      fs.readFileSync(
        path.join(repository, 'juno-code/docs/lifecycle-simplification-baseline.md'),
        'utf8',
      ),
    ).toContain(first.corpus_sha256);
    expect(first.primary_target).toEqual(
      expect.objectContaining({
        metric: 'coordination_interventions',
        cohort: 'routine',
        baseline_weighted_median: 10.95,
        reduction_required: 0.7,
      }),
    );
    expect(first.denominator.wall_rule).toMatch(/never summed/);
    expect(first.unique_complete_inputs).toBe(78);
    expect(first.scenarios.find((row: any) => row.id === 'normal').metrics.active_wall_ms).toEqual({
      median: 9257.094,
      min: 8978.759,
      max: 9332.46,
    });
    expect(first.scenarios.find((row: any) => row.id === 'failed').known_failures).toBe(3);
    expect(first.unavailable_measurements.map((row: any) => row.metric)).toContain(
      'paid model tokens and billed cost',
    );
  });

  it('maps every G1-G8 guarantee to named seeded coverage including all boundaries', async () => {
    const api = await driver();
    const corpus = JSON.parse(fs.readFileSync(corpusPath, 'utf8')) as Record<string, any>;
    expect(api.validateManifest(corpus)).toBe(true);
    const mapped = new Set(corpus.scenarios.flatMap((scenario: any) => scenario.guarantees));
    expect([...mapped].sort()).toEqual(['G1', 'G2', 'G3', 'G4', 'G5', 'G6', 'G7', 'G8']);
    expect(corpus.scenarios.map((scenario: any) => scenario.id)).toEqual(
      expect.arrayContaining([
        'docs-inert',
        'docs-inert-warm',
        'docs-active-cold',
        'docs-active-warm',
        'normal',
        'high',
        'two',
        'ten',
        'multi',
        'conflict',
        'failed',
        'stale',
        'crash-validation',
        'crash-cas',
        'crash-finalization',
        'dirty',
      ]),
    );
    expect(corpus.scenarios.every((scenario: any) => scenario.repeats.length >= 3)).toBe(true);
    expect(
      corpus.scenarios.every((scenario: any) => scenario.fixture.review_quality_evidence === false),
    ).toBe(true);
    expect(corpus.k6ozfw_removal_candidates.supported_public_producers).toEqual([]);
  });

  it('keeps a safe but uninstrumented candidate in NEEDS_DECISION', async () => {
    const api = await driver();
    const corpus = JSON.parse(fs.readFileSync(corpusPath, 'utf8')) as Record<string, any>;
    const observation = {
      schema_version: api.ACCEPTANCE_SCHEMA,
      baseline_corpus_sha256: corpus.manifest_sha256,
      candidate_commit: 'b'.repeat(40),
      candidate_tree: 'c'.repeat(40),
      identities: [{ name: 'runtime', sha256: 'a'.repeat(64) }],
      environment: { fixture_kind: 'seeded-disposable' },
      scenarios: corpus.scenarios.map((scenario: any) => ({
        id: scenario.id,
        selector: scenario.fixture.selector,
        repeats: [1, 2, 3].map((fixture_wall_ms) => ({
          outcome: 'passed',
          fixture_wall_ms,
          coordination_interventions: null,
        })),
      })),
      unavailable_measurements: [{ metric: 'coordination_interventions', reason: 'not emitted' }],
    };
    const result = api.aggregateAcceptance(corpus, observation);

    expect(result.safety).toEqual({ passed: true, scenario_count: 16 });
    expect(result.primary_target.candidate_weighted_median).toBeNull();
    expect(result.primary_target.reduction).toBeNull();
    expect(result.primary_target.complete).toBe(false);
    expect(result.readiness).toBe('NEEDS_DECISION');
    expect(result.reason).toBe('target_metric_unknown');
    expect(result.analysis_complete).toBe(true);
    expect(result.next_version_accepted).toBe(false);
    expect(result.percentile_policy).toMatch(/no p99/);
    const report = fs.readFileSync(
      path.join(repository, 'juno-code/docs/lifecycle-simplification-acceptance.md'),
      'utf8',
    );
    expect(report).toContain('"reason": "target_metric_unknown"');
    expect(report).toContain('"next_version_accepted": false');
    expect(report).toContain(corpus.manifest_sha256);
  });

  it('uses the frozen weights when complete candidate intervention counts are supplied', async () => {
    const api = await driver();
    const corpus = JSON.parse(fs.readFileSync(corpusPath, 'utf8')) as Record<string, any>;
    const observation = {
      schema_version: api.ACCEPTANCE_SCHEMA,
      baseline_corpus_sha256: corpus.manifest_sha256,
      candidate_commit: 'b'.repeat(40),
      candidate_tree: 'c'.repeat(40),
      identities: [{ name: 'runtime', sha256: 'a'.repeat(64) }],
      scenarios: corpus.scenarios.map((scenario: any) => ({
        id: scenario.id,
        selector: scenario.fixture.selector,
        repeats: [1, 2, 3].map((fixture_wall_ms) => ({
          outcome: 'passed',
          fixture_wall_ms,
          coordination_interventions: scenario.cohort === 'routine' ? 3 : 0,
        })),
      })),
    };
    const result = api.aggregateAcceptance(corpus, observation);

    expect(result.primary_target.candidate_weighted_median).toBe(3);
    expect(result.primary_target.reduction).toBeCloseTo(1 - 3 / 10.95);
    expect(result.primary_target.passed).toBe(true);
    expect(result.readiness).toBe('ACCEPTED');
  });

  it('binds report digests to the exact integrated candidate', () => {
    const commit = '949f7271bb28d168cf73859c56073a6cb8140968';
    const report = fs.readFileSync(
      path.join(repository, 'juno-code/docs/lifecycle-simplification-acceptance.md'),
      'utf8',
    );
    const tree = spawnSync('git', ['show', '-s', '--format=%T', commit], {
      cwd: repository,
      encoding: 'utf8',
    });
    expect(tree.status, tree.stderr).toBe(0);
    expect(report).toContain(tree.stdout.trim());
    for (const relative of [
      'juno-code/package.json',
      'juno-code/package-lock.json',
      'juno-code/scripts/test-support/task_workspace_fixture.py',
      'juno-code/scripts/test-support/task_workspace_test_runner.py',
      'juno-code/scripts/test-task-workspace.mjs',
      'juno-code/scripts/test-performance/task-workspace-duration-weights.v1.json',
      '.juno_task/scripts/tests/fixtures/lifecycle-evidence-reuse-matrix.v1.json',
      '.juno_task/scripts/task_workspace.py',
      '.juno_task/scripts/merge_queue.py',
      '.juno_task/scripts/risk_policy.py',
      '.juno_task/scripts/operation_snapshot.py',
      '.juno_task/managed-assets.json',
      'juno-code/src/templates/managed-assets.json',
    ]) {
      const source = spawnSync('git', ['show', `${commit}:${relative}`], {
        cwd: repository,
        encoding: null,
        maxBuffer: 16 * 1024 * 1024,
      });
      expect(source.status, String(source.stderr)).toBe(0);
      expect(report).toContain(crypto.createHash('sha256').update(source.stdout).digest('hex'));
    }
  });

  it('treats incomplete output as failure and zero baselines as absolute counts', async () => {
    const api = await driver();
    const corpus = JSON.parse(fs.readFileSync(corpusPath, 'utf8')) as Record<string, any>;
    delete corpus.scenarios[0].repeats[0].command_count;
    corpus.manifest_sha256 = api.manifestDigest(corpus);
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'yylo-lifecycle-baseline-'));
    const incomplete = path.join(directory, 'incomplete.json');
    fs.writeFileSync(incomplete, `${JSON.stringify(corpus)}\n`);
    try {
      const result = spawnSync(
        process.execPath,
        [driverPath, '--manifest', incomplete, '--no-verify-source'],
        {
          cwd: path.join(repository, 'juno-code'),
          encoding: 'utf8',
          timeout: 10_000,
        },
      );
      expect(result.status).not.toBe(0);
      expect(result.stderr).toContain('metric command_count is unknown');
      expect(corpus.acceptance.zero_baseline_rule).toMatch(/never divide by zero/);
    } finally {
      fs.rmSync(directory, { recursive: true, force: true });
    }
  });
});
