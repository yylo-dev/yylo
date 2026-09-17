#!/usr/bin/env node
/** Offline packed-artifact acceptance. No publication, target-owner or live-controller operations. */
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const args = process.argv.slice(2);
let artifact, report;
for (let i = 0; i < args.length; i += 2) {
  if (!['--artifact', '--report'].includes(args[i]) || !args[i + 1]) throw new Error('Use --artifact FILE [--report NEW_FILE]');
  if (args[i] === '--artifact' && artifact === undefined) artifact = path.resolve(args[i + 1]);
  else if (args[i] === '--report' && report === undefined) report = path.resolve(args[i + 1]);
  else throw new Error('Duplicate acceptance option');
}
const sha256 = bytes => createHash('sha256').update(bytes).digest('hex');
const run = (exe, argv, timeout = 900_000) => execFileSync(exe, argv, {
  cwd: root, encoding: 'utf8', timeout, maxBuffer: 8 * 1024 * 1024,
});
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'yylo-upgrade-package-'));
try {
  if (!artifact) {
    run('npm', ['run', 'build'], 180_000);
    const packed = JSON.parse(run('npm', ['pack', '--ignore-scripts', '--json', '--pack-destination', temporary], 120_000));
    artifact = path.join(temporary, packed[0].filename);
  }
  const info = fs.lstatSync(artifact);
  if (!info.isFile() || info.nlink !== 1 || fs.realpathSync(artifact) !== artifact) throw new Error('Exact regular non-symlink artifact required');
  const before = fs.readFileSync(artifact);
  const output = run('python3', ['-E', '-B', 'src/templates/maintenance/tests/controller_generation_package_acceptance.py', '--artifact', artifact]);
  const result = JSON.parse(output.trim().split('\n').at(-1));
  if (result.schema_version !== 'yylo_controller_upgrade_acceptance.v1' || result.outcome !== 'passed'
      || result.scenarios?.length !== 2 || result.scenarios.some(row => row.candidate_sha256 !== sha256(before)
        || row.native_merge !== 'GIT_INTEGRATED' || row.hydration !== 'passed' || row.candidate_package !== 'unchanged')
      || sha256(fs.readFileSync(artifact)) !== sha256(before)) throw new Error('Contradictory packed acceptance result');
  const sourceSha = run('git', ['rev-parse', 'HEAD'], 10_000).trim();
  const sourceDirty = Boolean(run('git', ['status', '--porcelain'], 10_000).trim());
  const evidence = { ...result, artifact: { sha256: sha256(before), bytes: before.length },
    source: { sha: sourceSha, dirty: sourceDirty },
    gate_sha256: sha256(fs.readFileSync(fileURLToPath(import.meta.url))) };
  const bytes = JSON.stringify(evidence, null, 2) + '\n';
  if (report) fs.writeFileSync(report, bytes, { flag: 'wx', mode: 0o600 });
  process.stdout.write(bytes);
} finally {
  // Only this test-owned scratch directory, never controller/worktree cleanup.
  fs.rmSync(temporary, { recursive: true, force: true });
}
