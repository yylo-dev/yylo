import { spawnSync } from 'node:child_process';
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
