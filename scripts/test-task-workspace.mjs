#!/usr/bin/env node
/** Supported bounded owner/profiler for task-workspace fixture modes. */
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';

const SCHEMA = 'juno.task_workspace.profile.v1';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

function parse(argv) {
  const value = { mode: '', receipt: '', timeoutMs: 600_000, testIds: [], changedPaths: [], command: null, commandArgs: [], shards: null };
  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];
    if (token === '--mode') value.mode = argv[++index];
    else if (token === '--receipt') value.receipt = argv[++index];
    else if (token === '--timeout-ms') value.timeoutMs = Number(argv[++index]);
    else if (token === '--test-id') value.testIds.push(argv[++index]);
    else if (token === '--changed-path') value.changedPaths.push(argv[++index]);
    else if (token === '--shards') value.shards = Number(argv[++index]);
    else if (token === '--command') value.command = argv[++index];
    else if (token === '--command-arg') value.commandArgs.push(argv[++index]);
    else throw new Error(`unknown argument: ${token}`);
  }
  if (!['affected', 'seeded', 'hermetic', 'complete'].includes(value.mode)) throw new Error('--mode is required');
  if (!value.receipt) value.receipt = path.join(root, 'test-results/task-workspace', `${value.mode}.json`);
  if (!Number.isFinite(value.timeoutMs) || value.timeoutMs < 100 || value.timeoutMs > 3_600_000) throw new Error('invalid --timeout-ms');
  value.shards ??= value.testIds.length || value.command || value.mode === 'affected' ? 1 : Math.min(8, os.cpus().length || 1);
  if (!Number.isInteger(value.shards) || value.shards < 1 || value.shards > 16) throw new Error('invalid --shards');
  return value;
}

function quantile(values, percentile) {
  if (!values.length) return null;
  const ordered = [...values].sort((a, b) => a - b);
  return ordered[Math.max(0, Math.ceil(percentile * ordered.length) - 1)];
}

function processGroupAlive(pid) {
  if (process.platform === 'win32') return false;
  try {
    process.kill(-pid, 0);
    return true;
  } catch (error) {
    return error?.code !== 'ESRCH';
  }
}

function signalOwnedGroup(child, signal) {
  try {
    if (process.platform === 'win32') child.kill(signal);
    else process.kill(-child.pid, signal);
  } catch (error) {
    if (error?.code !== 'ESRCH') throw error;
  }
}

function captureBounded(command, args, timeoutMs = 1_000, outputLimit = 16 * 1024 * 1024) {
  return new Promise((resolve) => {
    let output = Buffer.alloc(0);
    let settled = false;
    let child;
    try {
      child = spawn(command, args, { stdio: ['ignore', 'pipe', 'ignore'], windowsHide: true });
    } catch (error) {
      resolve({ ok: false, output: '', error: String(error) });
      return;
    }
    const finish = (value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      child.stdout.destroy();
      resolve(value);
    };
    child.stdout.on('data', (chunk) => {
      output = Buffer.concat([output, Buffer.from(chunk)]);
      if (output.length > outputLimit) {
        child.kill('SIGKILL');
        finish({ ok: false, output: '', error: 'process inventory exceeded bounded output' });
      }
    });
    child.once('error', (error) => finish({ ok: false, output: '', error: String(error) }));
    child.once('exit', (code) => finish({
      ok: code === 0,
      output: output.toString('utf8'),
      error: code === 0 ? null : `process inventory exited ${code}`,
    }));
    const timer = setTimeout(() => {
      child.kill('SIGKILL');
      finish({ ok: false, output: '', error: 'process inventory timed out' });
    }, timeoutMs);
  });
}

async function discoverOwnedProcesses(ownerToken, knownPids) {
  if (process.platform !== 'win32') {
    const inventory = await captureBounded('ps', ['eww', '-axo', 'pid=,command=']);
    if (!inventory.ok) return { pids: [], error: inventory.error };
    const marker = `JUNO_TASK_WORKSPACE_RUN_OWNER=${ownerToken}`;
    const pids = inventory.output.split('\n').flatMap((line) => {
      if (!line.includes(marker)) return [];
      const match = line.match(/^\s*(\d+)\s/);
      return match ? [Number(match[1])] : [];
    }).filter((pid) => pid !== process.pid);
    return { pids: [...new Set(pids)], error: null };
  }

  const inventory = await captureBounded('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command',
    'Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId | ConvertTo-Json -Compress'], 2_000);
  if (!inventory.ok) return { pids: [], error: inventory.error };
  try {
    const decoded = JSON.parse(inventory.output || '[]');
    const rows = Array.isArray(decoded) ? decoded : [decoded];
    const owned = new Set(knownPids);
    let changed = true;
    while (changed) {
      changed = false;
      for (const row of rows) {
        const pid = Number(row.ProcessId);
        if (owned.has(Number(row.ParentProcessId)) && !owned.has(pid)) {
          owned.add(pid);
          changed = true;
        }
      }
    }
    return { pids: [...owned].filter((pid) => rows.some((row) => Number(row.ProcessId) === pid)), error: null };
  } catch (error) {
    return { pids: [], error: `cannot parse Windows process inventory: ${error}` };
  }
}

function signalOwnedPids(pids, signal) {
  for (const pid of pids) {
    if (pid === process.pid) continue;
    try { process.kill(pid, signal); } catch (error) {
      if (error?.code !== 'ESRCH') throw error;
    }
  }
}

async function reconcileOwnedProcesses(child, ownerToken, knownPids) {
  const verify = async () => {
    const discovered = await discoverOwnedProcesses(ownerToken, knownPids);
    for (const pid of discovered.pids) knownPids.add(pid);
    const group = processGroupAlive(child.pid);
    return { ...discovered, group };
  };
  let state = await verify();
  if (state.error) {
    child.stdout.destroy();
    child.stderr.destroy();
    return { settled: false, surviving: [`verification:${state.error}`] };
  }
  if (state.group) signalOwnedGroup(child, 'SIGTERM');
  signalOwnedPids(state.pids, 'SIGTERM');
  for (const [waitMs, signal] of [[300, 'SIGKILL'], [1_000, null]]) {
    const deadline = performance.now() + waitMs;
    do {
      await new Promise((resolve) => setTimeout(resolve, 20));
      state = await verify();
      if (state.error || (!state.group && state.pids.length === 0)) break;
    } while (performance.now() < deadline);
    if (state.error || (!state.group && state.pids.length === 0) || signal === null) break;
    if (state.group) signalOwnedGroup(child, signal);
    signalOwnedPids(state.pids, signal);
  }
  child.stdout.destroy();
  child.stderr.destroy();
  if (state.error) return { settled: false, surviving: [`verification:${state.error}`] };
  const surviving = [
    ...(state.group ? [`process_group:${child.pid}`] : []),
    ...state.pids.map((pid) => `pid:${pid}`),
  ];
  return { settled: surviving.length === 0, surviving };
}

async function runOwned(command, args, timeoutMs, fixtureMode, outputLimit = 262_144) {
  const started = performance.now();
  const ownerToken = `${process.pid}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const knownPids = new Set();
  const child = spawn(command, args, {
    cwd: root,
    detached: process.platform !== 'win32',
    stdio: ['ignore', 'pipe', 'pipe'],
    env: {
      ...process.env,
      JUNO_TASK_WORKSPACE_FIXTURE_MODE: fixtureMode,
      JUNO_TASK_WORKSPACE_RUN_OWNER: ownerToken,
      PYTHONPYCACHEPREFIX: process.env.PYTHONPYCACHEPREFIX ?? path.join(os.tmpdir(), 'juno-task-workspace-runner-pycache'),
    },
  });
  let output = Buffer.alloc(0);
  const append = (chunk) => {
    output = Buffer.concat([output, Buffer.from(chunk)]);
    if (output.length > outputLimit) output = output.subarray(output.length - outputLimit);
  };
  child.stdout.on('data', append);
  child.stderr.on('data', append);
  knownPids.add(child.pid);
  let timedOut = false;
  let timer;
  let escalationTimer;
  let inventoryInFlight = null;
  const inventoryTimer = process.platform === 'win32' ? setInterval(() => {
    if (inventoryInFlight) return;
    inventoryInFlight = discoverOwnedProcesses(ownerToken, knownPids).then((inventory) => {
      for (const pid of inventory.pids) knownPids.add(pid);
    }).finally(() => { inventoryInFlight = null; });
  }, 100) : null;
  const status = await new Promise((resolve, reject) => {
    child.once('error', reject);
    child.once('exit', (code, signal) => resolve({ code, signal }));
    timer = setTimeout(() => {
      timedOut = true;
      signalOwnedGroup(child, 'SIGTERM');
      escalationTimer = setTimeout(() => signalOwnedGroup(child, 'SIGKILL'), 200);
      escalationTimer.unref();
    }, timeoutMs);
  });
  clearTimeout(timer);
  if (inventoryTimer) clearInterval(inventoryTimer);
  if (inventoryInFlight) await inventoryInFlight;
  const reconciliation = await reconcileOwnedProcesses(child, ownerToken, knownPids);
  if (escalationTimer) clearTimeout(escalationTimer);
  return {
    ...status,
    timedOut,
    wallMs: performance.now() - started,
    output: output.toString('utf8'),
    ...reconciliation,
  };
}

function atomicWrite(target, value) {
  fs.mkdirSync(path.dirname(target), { recursive: true });
  const temporary = `${target}.${process.pid}.tmp`;
  fs.writeFileSync(temporary, `${JSON.stringify(value, null, 2)}\n`);
  fs.renameSync(temporary, target);
}

async function main() {
  const options = parse(process.argv.slice(2));
  const startedAt = new Date().toISOString();
  const cpuCount = os.cpus().length || 1;
  const initialLoad = os.loadavg();
  const comparable = initialLoad[0] <= cpuCount * 2;
  const profileRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'yylo-task-workspace-profile-'));
  const python = process.env.PYTHON ?? 'python3';
  const templateRoot = fs.existsSync(path.join(root, 'src/templates'))
    ? path.join(root, 'src/templates') : path.join(root, 'dist/templates');
  const runner = path.join(root, 'scripts/test-support/task_workspace_test_runner.py');
  const testModule = path.join(templateRoot, 'scripts/tests/test_task_workspace.py');
  const durationWeights = path.join(root, 'scripts/test-performance/task-workspace-duration-weights.v1.json');
  const plans = [];
  if (options.command) {
    plans.push({ command: options.command, args: options.commandArgs, profilePath: null, shard: 0 });
  } else {
    for (let shard = 0; shard < options.shards; shard += 1) {
      const profilePath = path.join(profileRoot, `python-${shard}.json`);
      const args = [runner, '--tests', testModule, '--out', profilePath, '--mode', options.mode,
        '--shard-index', String(shard), '--shard-count', String(options.shards)];
      for (const id of options.testIds) args.push('--test-id', id);
      for (const changedPath of options.changedPaths) args.push('--changed-path', changedPath);
      if (fs.existsSync(durationWeights)) args.push('--duration-weights', durationWeights);
      plans.push({ command: python, args, profilePath, shard });
    }
  }
  const runs = await Promise.all(plans.map(async (plan) => ({
    ...plan,
    run: await runOwned(plan.command, plan.args, options.timeoutMs, options.mode),
  })));
  const profiles = runs.map(({ profilePath }) => {
    try { return profilePath ? JSON.parse(fs.readFileSync(profilePath, 'utf8')) : null; } catch { return null; }
  });
  const profile = profiles.some(Boolean) ? {
    success: profiles.every((value) => value?.success === true),
    inventory: profiles.find(Boolean)?.inventory ?? [],
    selected: profiles.flatMap((value) => value?.selected ?? []).sort(),
    tests: profiles.flatMap((value) => value?.tests ?? []).sort((a, b) => a.id.localeCompare(b.id)),
    counts: profiles.reduce((total, value) => {
      for (const key of ['selected', 'failures', 'errors', 'skipped', 'git_processes']) total[key] += value?.counts?.[key] ?? 0;
      total.inventory = value?.counts?.inventory ?? total.inventory;
      return total;
    }, { inventory: 0, selected: 0, failures: 0, errors: 0, skipped: 0, git_processes: 0 }),
  } : null;
  const run = {
    code: runs.every((value) => value.run.code === 0) ? 0 : (runs.find((value) => value.run.code !== 0)?.run.code ?? 1),
    timedOut: runs.some((value) => value.run.timedOut),
    wallMs: Math.max(...runs.map((value) => value.run.wallMs)),
    output: runs.map((value) => value.run.output).join('\n'),
    settled: runs.every((value) => value.run.settled),
    surviving: runs.flatMap((value) => value.run.surviving),
  };
  const command = plans[0].command;
  const args = plans[0].args;
  const tests = profile?.tests ?? [];
  const walls = tests.map((test) => test.wall_ms);
  const receipt = {
    schema_version: SCHEMA,
    mode: options.mode,
    created_at: new Date().toISOString(),
    started_at: startedAt,
    command: [command, ...args],
    shards: runs.map(({ shard, run: shardRun }) => ({
      index: shard, exit_code: shardRun.code, timeout: shardRun.timedOut,
      wall_ms: shardRun.wallMs,
      predicted_ms: profiles[shard]?.shard?.predicted_ms ?? null,
      weights_identity: profiles[shard]?.shard?.weights_identity ?? null,
    })),
    timeout_ms: options.timeoutMs,
    timeout: run.timedOut,
    exit_code: null,
    eligible: !run.timedOut && run.code === 0 && run.settled && profile?.success !== false && tests.every((test) => test.outcome === 'passed'),
    environment: { platform: os.platform(), release: os.release(), arch: os.arch(), node: process.version, cpu_count: cpuCount, initial_loadavg: initialLoad },
    comparability: { comparable, reason: comparable ? null : 'reference_load_exceeded', max_load_1m: cpuCount * 2 },
    inventory: profile?.inventory ?? [],
    selected: profile?.selected ?? options.testIds,
    tests,
    counts: profile?.counts ?? { selected: 0, failures: run.code === 0 ? 0 : 1, errors: 0, skipped: 0, git_processes: 0 },
    summary: {
      wall: { count: walls.length || 1, p50_ms: quantile(walls.length ? walls : [run.wallMs], 0.50), p95_ms: quantile(walls.length ? walls : [run.wallMs], 0.95) },
      total_wall_ms: Math.round(run.wallMs * 1000) / 1000,
    },
    processes: { settled: run.settled, surviving: run.surviving },
    failure_guidance: options.mode === 'seeded' && run.code !== 0
      ? { replay_mode: 'hermetic', command: `npm run test:task-workspace:hermetic -- --test-id ${tests.find((test) => test.outcome !== 'passed')?.id ?? '<test-id>'}` }
      : null,
    cold_fallback: { disable_cache_env: 'YYLO_TEST_DISABLE_FIXTURE_BASE_CACHE=1', mode: 'complete' },
    output: { truncated_tail: run.output },
  };
  const targetMs = options.mode === 'affected' ? 5_000 : options.mode === 'complete' ? 90_000 : null;
  const testEligible = receipt.eligible;
  receipt.performance_gate = {
    applicable: targetMs !== null,
    target_ms: targetMs,
    eligible: targetMs === null ? null : testEligible && comparable && run.wallMs <= targetMs,
    reason: !testEligible ? 'run_failed' : !comparable ? 'incomparable_environment' : run.wallMs > targetMs ? 'target_exceeded' : null,
  };
  receipt.eligible = testEligible && (targetMs === null || receipt.performance_gate.eligible === true);
  receipt.exit_code = receipt.eligible ? 0
    : run.timedOut ? 124
      : run.code && run.code !== 0 ? run.code : 1;
  atomicWrite(path.resolve(options.receipt), receipt);
  if (!receipt.eligible) process.stderr.write(run.output);
  process.stdout.write(`${JSON.stringify({ receipt: path.resolve(options.receipt), eligible: receipt.eligible, mode: options.mode, selected: receipt.selected.length })}\n`);
  return receipt.exit_code;
}

main().then((code) => { process.exitCode = code; }).catch((error) => {
  process.stderr.write(`${error.stack ?? error}\n`);
  process.exitCode = 2;
});
