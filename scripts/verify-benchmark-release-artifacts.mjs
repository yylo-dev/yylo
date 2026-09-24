#!/usr/bin/env node
// Local source verification only: no publication, global install or provider calls.
import assert from 'node:assert/strict';
import { copyFile, chmod, mkdir, mkdtemp, readFile, realpath, rm, stat, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { tmpdir } from 'node:os';
import { dirname, delimiter, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { runBoundedReleaseCommand } from './bounded-release-command.mjs';

const junoCodeRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const repositoryRoot = resolve(junoCodeRoot, '..');
const fixtureRoot = await mkdtemp(join(tmpdir(), 'yylo-benchmark-release-smoke-'));
const sourceRoot = join(fixtureRoot, 'source');
const packDirectory = join(fixtureRoot, 'packs');
const prefix = join(fixtureRoot, 'prefix');
const sha256 = (value) => `sha256:${createHash('sha256').update(value).digest('hex')}`;

function runRaw(command, args, options = {}) {
  const result = runBoundedReleaseCommand(command, args, {
    cwd: options.cwd ?? repositoryRoot, env: options.env ?? process.env,
    input: options.input ?? '', timeout: options.timeout,
  });
  if (result.error) throw new Error(`${command} failed (${result.error.code ?? 'spawn'}): ${result.error.message}`);
  return result;
}
function run(command, args, options = {}) {
  const result = runRaw(command, args, options);
  if (result.status !== 0 || result.signal !== null) {
    throw new Error(`${command} ${args.join(' ')} failed (${result.status ?? result.signal}):\n${result.stderr || result.stdout}`);
  }
  return result;
}
function packedArtifact(projectRoot) {
  const result = run('npm', ['pack', '--silent', '--json', '--pack-destination', packDirectory], { cwd: projectRoot });
  const start = result.stdout.lastIndexOf('\n[');
  const records = JSON.parse(start >= 0 ? result.stdout.slice(start + 1) : result.stdout);
  assert.equal(records.length, 1);
  assert.equal(typeof records[0].filename, 'string');
  return join(packDirectory, records[0].filename);
}
async function stageTrackedSources() {
  const paths = run('git', ['ls-files', '-z']).stdout.split('\0').filter(Boolean);
  assert.ok(paths.length > 0, 'Git returned no tracked release sources');
  for (const relativePath of paths) {
    const source = join(repositoryRoot, relativePath);
    const sourceStat = await stat(source);
    if (sourceStat.isDirectory()) continue; // Independent gitlinks are not npm artifacts.
    const destination = join(sourceRoot, relativePath);
    await mkdir(dirname(destination), { recursive: true });
    await copyFile(source, destination);
    await chmod(destination, sourceStat.mode);
  }
}

try {
  assert.equal(run('git', ['status', '--porcelain']).stdout, '', 'verification requires clean committed source');
  const commit = run('git', ['rev-parse', 'HEAD']).stdout.trim();
  const tree = run('git', ['rev-parse', 'HEAD^{tree}']).stdout.trim();
  await stageTrackedSources();
  await mkdir(packDirectory, { recursive: true });
  await mkdir(prefix, { recursive: true });
  const benchmarkRoot = join(sourceRoot, 'juno-benchmark');
  const cliRoot = join(sourceRoot, 'juno-code');
  for (const cwd of [benchmarkRoot, cliRoot]) run('npm', ['ci', '--ignore-scripts'], { cwd });
  const benchmarkArtifact = packedArtifact(benchmarkRoot);
  const cliArtifact = packedArtifact(cliRoot);
  const benchmarkPackage = JSON.parse(await readFile(join(benchmarkRoot, 'package.json'), 'utf8'));
  const cliPackage = JSON.parse(await readFile(join(cliRoot, 'package.json'), 'utf8'));
  assert.equal(cliPackage.yyloBenchmark.version, benchmarkPackage.version, 'exact Benchmark pin differs from packed source');
  run('npm', ['install', '--offline', '--ignore-scripts', '--no-audit', '--no-fund', '--prefix', prefix, benchmarkArtifact, cliArtifact]);

  const bin = join(prefix, 'node_modules', '.bin');
  const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => !key.startsWith('JUNO_')));
  env.PATH = `${bin}${delimiter}${process.env.PATH ?? ''}`;
  const yy = join(bin, 'yy');
  const benchmark = join(bin, 'yylo-benchmark');
  const options = { cwd: fixtureRoot, env };
  function compare(args, success = true) {
    const standalone = runRaw(benchmark, args, options);
    const delegated = runRaw(yy, ['benchmark', ...args], options);
    for (const key of ['status', 'signal', 'stdout', 'stderr']) assert.equal(delegated[key], standalone[key], `${args.join(' ')} differs: ${key}`);
    if (success) assert.equal(standalone.status, 0, standalone.stderr);
    else assert.notEqual(standalone.status, 0, 'retired command must fail');
    return standalone.stdout;
  }
  assert.match(run(yy, ['--help'], options).stdout, /(^|\s)benchmark(\s|$)/m);
  assert.equal(compare(['--version']).trim().replace(/^yylo-benchmark\s+/, ''), benchmarkPackage.version);
  const help = compare(['--help']);
  const commands = ['case', 'run', 'evaluate', 'report', 'disqualify'];
  for (const command of commands) {
    assert.match(help, new RegExp(`(^|\\s)${command}(\\s|$)`, 'm'));
    compare([command, '--help']);
  }
  for (const action of ['draft', 'create']) compare(['case', action, '--help']);
  for (const retired of ['plan', 'recover', 'doctor', 'regrade', 'rejudge', 'workflow', 'daily-ops']) {
    assert.doesNotMatch(help, new RegExp(`^\\s+${retired}(\\s|$)`, 'm'));
    compare([retired], false);
  }

  // Exercise actual installed lifecycle with a synthetic command, not a model.
  const project = join(fixtureRoot, 'fixture-repo');
  await mkdir(project);
  await writeFile(join(project, 'answer.txt'), 'before\n');
  for (const args of [['init', '--quiet'], ['config', 'user.email', 'fixture@example.test'],
    ['config', 'user.name', 'Fixture'], ['add', '.'], ['commit', '--quiet', '-m', 'fixture']]) {
    run('git', args, { cwd: project, env });
  }
  const base = run('git', ['rev-parse', 'HEAD'], { cwd: project, env }).stdout.trim();
  const prompt = join(fixtureRoot, 'prompt.md');
  await writeFile(prompt, 'Change answer.txt to after.');
  const casePath = join(fixtureRoot, 'case');
  run(benchmark, ['case', 'create', '--source', project, '--base', base, '--prompt', prompt, '--output', casePath, '--reviewed'], options);
  const treatment = join(fixtureRoot, 'treatment.json');
  await writeFile(treatment, JSON.stringify({ name: 'synthetic', model: 'test/no-model', harness: 'command',
    executable: process.execPath, args: ['-e', "require('node:fs').writeFileSync('answer.txt', 'after\\n')"], timeout_ms: 30000 }));
  const experiment = join(fixtureRoot, 'experiment');
  run(yy, ['benchmark', 'run', '--case', casePath, '--treatment', treatment, '--output', experiment], options);
  const attempt = join(experiment, '1-1');
  const evaluator = join(fixtureRoot, 'check.json');
  await writeFile(evaluator, JSON.stringify({ name: 'synthetic-check', kind: 'check', timeout_ms: 30000,
    command: { executable: process.execPath, args: ['-e', "const ok=require('node:fs').readFileSync('answer.txt','utf8')==='after\\n'; console.log(JSON.stringify({verdict:ok?'pass':'fail',findings:[]}))"] } }));
  run(benchmark, ['evaluate', '--attempt', attempt, '--evaluator', evaluator], options);
  const report = JSON.parse(compare(['report', '--root', experiment]));
  assert.ok(report.length > 0);
  assert.ok(report.some((row) => row.verdict === 'pass'), 'retained check verdict missing');
  run(yy, ['benchmark', 'disqualify', '--attempt', attempt, '--reason', 'synthetic exposure'], options);
  assert.ok(JSON.parse(compare(['report', '--root', experiment])).every((row) => row.disqualified));

  // Installed process fidelity: the probe uses the real package version handshake.
  const probeBin = join(fixtureRoot, 'probe-bin'); await mkdir(probeBin);
  const probeRecord = join(fixtureRoot, 'probe-record.json');
  const probe = join(probeBin, 'yylo-benchmark');
  await writeFile(probe, `#!/usr/bin/env node
const {spawnSync}=require('node:child_process');const fs=require('node:fs');
if(process.argv[2]==='--version'){const r=spawnSync(${JSON.stringify(benchmark)},['--version'],{stdio:'inherit'});process.exit(r.status??1);}
const input=fs.readFileSync(0,'utf8');
fs.writeFileSync(process.env.PROBE_RECORD,JSON.stringify({cwd:process.cwd(),marker:process.env.PROBE_MARKER,input}));
if(process.env.PROBE_SIGNAL)process.kill(process.pid,process.env.PROBE_SIGNAL);
process.stdout.write('probe stdout:'+input);process.stderr.write('probe stderr:'+process.env.PROBE_MARKER);process.exit(43);
`, { mode: 0o755 });
  const probeOptions = { ...options, input: 'probe stdin\n', env: { ...env, PATH: `${probeBin}${delimiter}${env.PATH}`, PROBE_RECORD: probeRecord, PROBE_MARKER: 'preserved' } };
  const direct = runRaw(probe, ['probe'], probeOptions);
  const directRecord = JSON.parse(await readFile(probeRecord, 'utf8'));
  const delegated = runRaw(yy, ['benchmark', 'probe'], probeOptions);
  for (const key of ['status', 'signal', 'stdout', 'stderr']) assert.equal(delegated[key], direct[key]);
  assert.equal(direct.status, 43);
  assert.deepEqual(JSON.parse(await readFile(probeRecord, 'utf8')), directRecord);
  assert.deepEqual(directRecord, { cwd: await realpath(fixtureRoot), marker: 'preserved', input: 'probe stdin\n' });
  const signalOptions = { ...probeOptions, env: { ...probeOptions.env, PROBE_SIGNAL: 'SIGTERM' } };
  assert.equal(runRaw(probe, ['probe'], signalOptions).signal, 'SIGTERM');
  assert.equal(runRaw(yy, ['benchmark', 'probe'], signalOptions).signal, 'SIGTERM');

  // Preserve byte-level dist/tarball leak scanning and synthetic detector controls.
  const scanArgs = [join(benchmarkRoot, 'dist'), join(cliRoot, 'dist'), benchmarkArtifact, cliArtifact];
  const leakage = JSON.parse(run(process.execPath, [join(cliRoot, 'scripts/scan-benchmark-release-artifacts.mjs'), ...scanArgs], {
    ...options, env: { ...env, YYLO_BENCHMARK_RELEASE_SOURCE_TREE: tree,
      YYLO_BENCHMARK_RELEASE_COMMAND_HASH: sha256(JSON.stringify(scanArgs)) },
  }).stdout);
  assert.equal(leakage.passed, true);
  assert.ok(leakage.files_scanned > 0 && leakage.canaries_checked > 0);
  assert.equal(run('git', ['rev-parse', 'HEAD']).stdout.trim(), commit, 'source moved during verification');
  assert.equal(run('git', ['status', '--porcelain']).stdout, '', 'source changed during verification');
  const receipt = { schema_version: 'yylo_benchmark_installed_thin_acceptance.v1', commit, tree,
    benchmark_version: benchmarkPackage.version, cli_version: cliPackage.version,
    live_model_calls: 0, candidate_dispatch_count: 1, evaluator_dispatch_count: 1,
    standalone_delegate_equal: true, process_fidelity: true, retired_commands_rejected: true,
    leakage: { files_scanned: leakage.files_scanned, canaries_checked: leakage.canaries_checked },
    artifacts: { benchmark_sha256: sha256(await readFile(benchmarkArtifact)), cli_sha256: sha256(await readFile(cliArtifact)) } };
  process.stdout.write(`${JSON.stringify(receipt)}\nbenchmark ${process.argv.includes('--distribution-only') ? 'installed-pair distribution' : 'release artifact'} smoke passed\n`);
} finally {
  await rm(fixtureRoot, { recursive: true, force: true });
}
