#!/usr/bin/env node
/** Frozen, source-bound lifecycle baseline aggregator. It never drives a live queue. */
import { execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';

export const SCHEMA = 'juno.lifecycle_simplification.corpus.v1';
export const RESULT_SCHEMA = 'juno.lifecycle_simplification.baseline.v1';
const METRICS = [
  'active_wall_ms',
  'whole_delivery_ms',
  'queue_wait_ms',
  'command_count',
  'model_handoffs',
  'evidence_hits',
  'evidence_misses',
  'duplicate_dispatches',
  'recovery_actions',
  'bytes_preserved',
  'coordination_interventions',
  'state_count',
  'authority_writer_count',
];
const COMPLETE_OUTCOMES = new Set(['passed', 'known_failure']);

function deepCanonical(value) {
  if (Array.isArray(value)) return value.map(deepCanonical);
  if (value && typeof value === 'object')
    return Object.fromEntries(
      Object.keys(value)
        .sort()
        .map((key) => [key, deepCanonical(value[key])]),
    );
  return value;
}

export function manifestDigest(manifest) {
  const copy = structuredClone(manifest);
  delete copy.manifest_sha256;
  return crypto
    .createHash('sha256')
    .update(JSON.stringify(deepCanonical(copy)))
    .digest('hex');
}

function median(values) {
  const ordered = [...values].sort((a, b) => a - b);
  const middle = Math.floor(ordered.length / 2);
  return ordered.length % 2 ? ordered[middle] : (ordered[middle - 1] + ordered[middle]) / 2;
}

function summarize(values) {
  return { median: median(values), min: Math.min(...values), max: Math.max(...values) };
}

function fail(message) {
  throw new Error(`invalid lifecycle baseline corpus: ${message}`);
}

export function validateManifest(manifest, { verifySource = false, repository = null } = {}) {
  if (manifest?.schema_version !== SCHEMA) fail('schema_version');
  if (manifest.manifest_sha256 !== manifestDigest(manifest)) fail('manifest_sha256');
  if (manifest.reference?.commit !== 'de9e3a8caa9d439654fe25dd06ecd5aae3af0bb4')
    fail('reference commit');
  if (!Array.isArray(manifest.scenarios) || !manifest.scenarios.length) fail('scenarios');
  if (!Array.isArray(manifest.guarantees) || manifest.guarantees.length !== 8)
    fail('G1-G8 inventory');
  const guarantees = new Set(manifest.guarantees.map((row) => row.id));
  const scenarioIds = new Set();
  for (const scenario of manifest.scenarios) {
    if (!scenario.id || scenarioIds.has(scenario.id)) fail(`duplicate scenario ${scenario.id}`);
    scenarioIds.add(scenario.id);
    if (
      !Array.isArray(scenario.guarantees) ||
      !scenario.guarantees.length ||
      scenario.guarantees.some((id) => !guarantees.has(id))
    )
      fail(`${scenario.id} guarantee mapping`);
    if (!Array.isArray(scenario.repeats) || scenario.repeats.length < 3)
      fail(`${scenario.id} requires at least three repeats`);
    for (const [index, repeat] of scenario.repeats.entries()) {
      if (!COMPLETE_OUTCOMES.has(repeat.outcome))
        fail(`${scenario.id} repeat ${index + 1} has incomplete outcome`);
      if (
        !Array.isArray(repeat.complete_input_ids) ||
        !repeat.complete_input_ids.length ||
        repeat.complete_input_ids.some(
          (id) => typeof id !== 'string' || !/^[0-9a-f]{64}$/.test(id),
        ) ||
        new Set(repeat.complete_input_ids).size !== repeat.complete_input_ids.length
      ) {
        fail(`${scenario.id} repeat ${index + 1} complete_input_ids`);
      }
      for (const metric of METRICS) {
        if (
          typeof repeat[metric] !== 'number' ||
          !Number.isFinite(repeat[metric]) ||
          repeat[metric] < 0
        ) {
          fail(`${scenario.id} repeat ${index + 1} metric ${metric} is unknown`);
        }
      }
    }
  }
  for (const guarantee of guarantees) {
    if (!manifest.scenarios.some((scenario) => scenario.guarantees.includes(guarantee)))
      fail(`${guarantee} has no seeded scenario`);
  }
  const routine = manifest.scenarios.filter((scenario) => scenario.cohort === 'routine');
  const weight = routine.reduce((sum, scenario) => sum + scenario.weight, 0);
  if (!routine.length || Math.abs(weight - 1) > 1e-9) fail('routine weights must sum to one');
  if (
    routine.some(
      (scenario) =>
        !(scenario.weight > 0) ||
        scenario.repeats.some((repeat) => repeat.coordination_interventions <= 0),
    )
  ) {
    fail('routine cohort requires positive frozen weights and nonzero baselines');
  }
  if (
    !Array.isArray(manifest.unavailable_measurements) ||
    manifest.unavailable_measurements.some((row) => !row.metric || !row.reason)
  )
    fail('unavailable measurements');

  if (verifySource) {
    const root =
      repository ?? path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
    let tree;
    try {
      tree = execFileSync('git', ['show', '-s', '--format=%T', manifest.reference.commit], {
        cwd: root,
        encoding: 'utf8',
        stdio: ['ignore', 'pipe', 'pipe'],
      }).trim();
    } catch {
      fail('reference commit unavailable');
    }
    if (tree !== manifest.reference.tree) fail('reference tree drift');
    for (const input of manifest.reference.source_inputs) {
      let bytes;
      try {
        bytes = execFileSync('git', ['show', `${manifest.reference.commit}:${input.path}`], {
          cwd: root,
          encoding: null,
          stdio: ['ignore', 'pipe', 'pipe'],
          maxBuffer: 16 * 1024 * 1024,
        });
      } catch {
        fail(`reference source unavailable: ${input.path}`);
      }
      const observed = crypto.createHash('sha256').update(bytes).digest('hex');
      if (observed !== input.sha256) fail(`reference source drift: ${input.path}`);
    }
  }
  return true;
}

export function aggregateManifest(manifest, options = {}) {
  validateManifest(manifest, options);
  const scenarios = manifest.scenarios.map((scenario) => ({
    id: scenario.id,
    cohort: scenario.cohort,
    weight: scenario.weight,
    guarantees: scenario.guarantees,
    fixture_kind: scenario.fixture.kind,
    known_failures: scenario.repeats.filter((repeat) => repeat.outcome === 'known_failure').length,
    unique_complete_inputs: new Set(scenario.repeats.flatMap((repeat) => repeat.complete_input_ids))
      .size,
    metrics: Object.fromEntries(
      METRICS.map((metric) => [
        metric,
        summarize(scenario.repeats.map((repeat) => repeat[metric])),
      ]),
    ),
  }));
  const routine = scenarios.filter((scenario) => scenario.cohort === 'routine');
  const weightedCoordinationInterventions = routine.reduce(
    (sum, scenario) => sum + scenario.weight * scenario.metrics.coordination_interventions.median,
    0,
  );
  return {
    schema_version: RESULT_SCHEMA,
    corpus_sha256: manifest.manifest_sha256,
    reference: manifest.reference,
    denominator: manifest.denominator,
    primary_target: {
      metric: 'coordination_interventions',
      cohort: 'routine',
      baseline_weighted_median: weightedCoordinationInterventions,
      reduction_required: manifest.acceptance.primary_reduction,
      stretch_reduction: manifest.acceptance.stretch_reduction,
      zero_baseline_rule: manifest.acceptance.zero_baseline_rule,
    },
    scenarios,
    unique_complete_inputs: new Set(
      manifest.scenarios.flatMap((scenario) =>
        scenario.repeats.flatMap((repeat) => repeat.complete_input_ids),
      ),
    ).size,
    unavailable_measurements: manifest.unavailable_measurements,
    complete: true,
  };
}

function parse(argv) {
  const options = {
    manifest: path.join(
      path.dirname(fileURLToPath(import.meta.url)),
      'lifecycle-simplification-corpus.v1.json',
    ),
    out: null,
    verifySource: true,
  };
  for (let index = 0; index < argv.length; index += 1) {
    if (argv[index] === '--manifest') options.manifest = argv[++index];
    else if (argv[index] === '--out') options.out = argv[++index];
    else if (argv[index] === '--no-verify-source') options.verifySource = false;
    else fail(`unknown argument ${argv[index]}`);
  }
  return options;
}

export function main(argv = process.argv.slice(2)) {
  const options = parse(argv);
  const manifest = JSON.parse(fs.readFileSync(path.resolve(options.manifest), 'utf8'));
  const result = aggregateManifest(manifest, { verifySource: options.verifySource });
  const text = `${JSON.stringify(result, null, 2)}\n`;
  if (options.out) {
    fs.mkdirSync(path.dirname(path.resolve(options.out)), { recursive: true });
    fs.writeFileSync(path.resolve(options.out), text);
  } else process.stdout.write(text);
  return 0;
}

if (
  process.argv[1] &&
  fs.realpathSync(process.argv[1]) === fs.realpathSync(fileURLToPath(import.meta.url))
) {
  try {
    process.exitCode = main();
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  }
}
