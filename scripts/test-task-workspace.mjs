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

export function evaluatePerformanceGate(targetMs, testEligible, _comparable, wallMs) {
  return {
    applicable: targetMs !== null,
    target_ms: targetMs,
    eligible: targetMs === null ? null : testEligible && wallMs < targetMs,
    reason: !testEligible ? 'run_failed'
      : targetMs !== null && wallMs >= targetMs ? 'target_exceeded' : null,
  };
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

function parsePosixProcessInventory(output) {
  return output.split('\n').flatMap((line) => {
    const match = line.match(/^\s*(\d+)\s+(\d+)\s+(\d+)\s+(.{24})\s+(.*)$/);
    return match ? [{
      pid: Number(match[1]), parent_pid: Number(match[2]), pgid: Number(match[3]),
      creation_id: match[4].trim(), command: match[5],
    }] : [];
  });
}

async function processInventory() {
  if (process.platform !== 'win32') {
    const inventory = await captureBounded('ps', ['eww', '-axo', 'pid=,ppid=,pgid=,lstart=,command=']);
    return inventory.ok
      ? { rows: parsePosixProcessInventory(inventory.output), error: null }
      : { rows: [], error: inventory.error };
  }
  const inventory = await captureBounded('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command',
    "Get-CimInstance Win32_Process | ForEach-Object { [pscustomobject]@{ ProcessId=$_.ProcessId; ParentProcessId=$_.ParentProcessId; CreationId=$_.CreationDate.ToUniversalTime().Ticks.ToString(); CommandLine=$_.CommandLine } } | ConvertTo-Json -Compress"], 2_000);
  if (!inventory.ok) return { rows: [], error: inventory.error };
  try {
    const decoded = JSON.parse(inventory.output || '[]');
    return {
      rows: (Array.isArray(decoded) ? decoded : [decoded]).map((row) => ({
        pid: Number(row.ProcessId), parent_pid: Number(row.ParentProcessId), pgid: null,
        creation_id: String(row.CreationId ?? ''), command: String(row.CommandLine ?? ''),
      })).filter((row) => Number.isInteger(row.pid) && row.pid > 0 && row.creation_id),
      error: null,
    };
  } catch (error) {
    return { rows: [], error: `cannot parse Windows process inventory: ${error}` };
  }
}

/** Refuse historical bare PIDs: only the same OS process instance remains owned. */
export function selectVerifiedOwnedProcesses(known, rows) {
  const identities = new Map(Array.from(known, (entry) => [Number(entry.pid), String(entry.creation_id)]));
  return rows.filter((row) => identities.get(Number(row.pid)) === String(row.creation_id));
}

function rememberOwnedDescendants(known, rows, ownerToken) {
  const byPid = new Map(rows.map((row) => [row.pid, row]));
  const verified = new Set(selectVerifiedOwnedProcesses(known.values(), rows).map((row) => row.pid));
  for (const entry of known.values()) {
    if (!entry.creation_id) {
      const row = byPid.get(entry.pid);
      if (row) {
        entry.creation_id = row.creation_id;
        verified.add(row.pid);
      }
    }
  }
  const marker = `JUNO_TASK_WORKSPACE_RUN_OWNER=${ownerToken}`;
  for (const row of rows) {
    if (row.command.includes(marker)) verified.add(row.pid);
  }
  let changed = true;
  while (changed) {
    changed = false;
    for (const row of rows) {
      if (verified.has(row.parent_pid) && !verified.has(row.pid)) {
        verified.add(row.pid);
        changed = true;
      }
    }
  }
  for (const pid of verified) {
    if (pid === process.pid) continue;
    const row = byPid.get(pid);
    if (row && !known.has(pid)) known.set(pid, { pid, creation_id: row.creation_id });
  }
  return selectVerifiedOwnedProcesses(known.values(), rows);
}

async function discoverOwnedProcesses(ownerToken, known) {
  const inventory = await processInventory();
  if (inventory.error) return { rows: [], error: inventory.error };
  return { rows: rememberOwnedDescendants(known, inventory.rows, ownerToken), error: null };
}

async function inheritedPipeTokens(child) {
  if (process.platform === 'win32') return [];
  const fds = [child?.stdout?._handle?.fd, child?.stderr?._handle?.fd]
    .filter((fd) => Number.isInteger(fd));
  if (!fds.length) return [];
  if (process.platform === 'linux') {
    return [...new Set(fds.flatMap((fd) => {
      try { return [fs.readlinkSync(`/proc/self/fd/${fd}`)]; } catch { return []; }
    }).filter((token) => token.startsWith('pipe:') || token.startsWith('socket:')))];
  }
  const inventory = await captureBounded('lsof', ['-nP', '-a', '-p', String(process.pid),
    '-d', fds.join(','), '-Fn']);
  if (!inventory.ok) return [];
  return [...new Set(inventory.output.match(/0x[0-9a-f]+/gi) ?? [])];
}

async function rememberInheritedPipeHolders(known, tokens) {
  if (process.platform === 'win32' || !tokens.length) return;
  const holderPids = new Set();
  if (process.platform === 'linux') {
    for (const name of fs.readdirSync('/proc')) {
      if (!/^\d+$/.test(name)) continue;
      try {
        const descriptors = fs.readdirSync(`/proc/${name}/fd`);
        if (descriptors.some((fd) => {
          try { return tokens.includes(fs.readlinkSync(`/proc/${name}/fd/${fd}`)); } catch { return false; }
        })) holderPids.add(Number(name));
      } catch { /* process exited or is not inspectable */ }
    }
  } else {
    const inventory = await captureBounded('lsof', ['-nP', '-U'], 2_000);
    if (!inventory.ok) return;
    for (const line of inventory.output.split('\n')) {
      if (!tokens.some((token) => line.includes(token))) continue;
      const pid = Number(line.trim().split(/\s+/)[1]);
      if (Number.isInteger(pid)) holderPids.add(pid);
    }
  }
  const processes = await processInventory();
  if (processes.error) return;
  for (const row of processes.rows) {
    if (holderPids.has(row.pid) && row.pid !== process.pid) {
      known.set(row.pid, { pid: row.pid, creation_id: row.creation_id });
    }
  }
}

function signalOwnedGroup(child, signal) {
  if (!Number.isInteger(child?.pid) || child.pid <= 0) return;
  try {
    if (process.platform === 'win32') child.kill(signal);
    else process.kill(-child.pid, signal);
  } catch (error) {
    if (error?.code !== 'ESRCH') throw error;
  }
}

const WINDOWS_TERMINATE_SOURCE = String.raw`
using System;
using System.Runtime.InteropServices;
public static class JunoStableProcess {
  [StructLayout(LayoutKind.Sequential)] public struct FILETIME { public uint Low; public uint High; }
  [DllImport("kernel32.dll", SetLastError=true)] static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
  [DllImport("kernel32.dll", SetLastError=true)] static extern bool GetProcessTimes(IntPtr handle, out FILETIME created, out FILETIME exited, out FILETIME kernel, out FILETIME user);
  [DllImport("kernel32.dll", SetLastError=true)] static extern bool TerminateProcess(IntPtr handle, uint exitCode);
  [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
  public static string Terminate(uint pid, long expectedTicks, uint exitCode) {
    IntPtr handle = OpenProcess(0x0400 | 0x0001, false, pid);
    if (handle == IntPtr.Zero) return "gone";
    try {
      FILETIME created, exited, kernel, user;
      if (!GetProcessTimes(handle, out created, out exited, out kernel, out user)) return "unverified";
      long fileTime = ((long)created.High << 32) | created.Low;
      if (DateTime.FromFileTimeUtc(fileTime).Ticks != expectedTicks) return "identity_mismatch";
      return TerminateProcess(handle, exitCode) ? "terminated" : "terminate_failed";
    } finally { CloseHandle(handle); }
  }
}`;

async function atomicTerminateWindowsProcess(entry, signal) {
  const exitCode = signal === 'SIGKILL' ? 137 : 143;
  const script = `Add-Type -TypeDefinition $args[0]; [JunoStableProcess]::Terminate([uint32]$args[1], [long]$args[2], [uint32]$args[3])`;
  const result = await captureBounded('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command',
    script, WINDOWS_TERMINATE_SOURCE, String(entry.pid), String(entry.creation_id), String(exitCode)], 2_000);
  if (!result.ok) return { status: 'error', pid: entry.pid, error: result.error };
  return { status: result.output.trim(), pid: entry.pid };
}

/** Open an instance handle, verify its creation identity, then signal that same handle. */
export async function terminateVerifiedWindowsProcess(entry, signal, dependencies = {}) {
  if (dependencies.atomicTerminate) return dependencies.atomicTerminate(entry, signal);
  if (!dependencies.openProcess) return atomicTerminateWindowsProcess(entry, signal);
  let handle;
  try {
    handle = await dependencies.openProcess(entry.pid);
    if (!handle) return { status: 'gone', pid: entry.pid };
    const creationId = await dependencies.creationIdForHandle(handle);
    if (String(creationId) !== String(entry.creation_id)) {
      return { status: 'identity_mismatch', pid: entry.pid };
    }
    await dependencies.terminateHandle(handle, signal);
    return { status: 'terminated', pid: entry.pid };
  } finally {
    if (handle) await dependencies.closeHandle(handle);
  }
}

async function signalVerifiedOwned(ownerToken, known, signal) {
  const discovered = await discoverOwnedProcesses(ownerToken, known);
  if (discovered.error) return discovered;
  for (const row of discovered.rows) {
    if (row.pid === process.pid) continue;
    if (process.platform === 'win32') {
      const outcome = await terminateVerifiedWindowsProcess(row, signal);
      if (['error', 'unverified', 'terminate_failed'].includes(outcome.status)) {
        return { rows: discovered.rows, error: `Windows stable-handle termination failed for ${row.pid}: ${outcome.error ?? outcome.status}` };
      }
      continue;
    }
    try { process.kill(row.pid, signal); } catch (error) {
      if (error?.code !== 'ESRCH') throw error;
    }
  }
  return discovered;
}

async function reconcileOwnedProcesses(child, ownerToken, known, pipeTokens = []) {
  const destroyPipes = () => {
    child?.stdout?.destroy();
    child?.stderr?.destroy();
  };
  if (!Number.isInteger(child?.pid) || child.pid <= 0) {
    destroyPipes();
    return { settled: true, surviving: [] };
  }
  await rememberInheritedPipeHolders(known, pipeTokens);
  const verify = async () => {
    const discovered = await discoverOwnedProcesses(ownerToken, known);
    const groupIdentityVerified = discovered.rows.some((row) => row.pgid === child.pid);
    return { ...discovered, group: groupIdentityVerified && processGroupAlive(child.pid) };
  };
  let state = await verify();
  if (state.error) {
    destroyPipes();
    return { settled: false, surviving: [`verification:${state.error}`] };
  }
  if (state.group) signalOwnedGroup(child, 'SIGTERM');
  await signalVerifiedOwned(ownerToken, known, 'SIGTERM');
  for (const [waitMs, signal] of [[300, 'SIGKILL'], [1_000, null]]) {
    const deadline = performance.now() + waitMs;
    do {
      await new Promise((resolve) => setTimeout(resolve, 20));
      state = await verify();
      if (state.error || (!state.group && state.rows.length === 0)) break;
    } while (performance.now() < deadline);
    if (state.error || (!state.group && state.rows.length === 0) || signal === null) break;
    if (state.group) signalOwnedGroup(child, signal);
    await signalVerifiedOwned(ownerToken, known, signal);
  }
  destroyPipes();
  if (state.error) return { settled: false, surviving: [`verification:${state.error}`] };
  const surviving = [
    ...(state.group ? [`process_group:${child.pid}`] : []),
    ...state.rows.map((row) => `pid:${row.pid}@${row.creation_id}`),
  ];
  return { settled: surviving.length === 0, surviving };
}

function commandExists(command) {
  if (path.isAbsolute(command) || command.includes(path.sep)) return fs.existsSync(command);
  return (process.env.PATH ?? '').split(path.delimiter)
    .some((directory) => fs.existsSync(path.join(directory, command)));
}

async function runOwned(command, args, timeoutMs, fixtureMode, outputLimit = 262_144, dependencies = {}) {
  const started = performance.now();
  const ownerToken = `${process.pid}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const known = new Map();
  let child;
  let output = Buffer.alloc(0);
  let timedOut = false;
  let timer = null;
  let escalationTimer = null;
  let inventoryTimer = null;
  const inventoriesInFlight = new Set();
  const append = (chunk) => {
    output = Buffer.concat([output, Buffer.from(chunk)]);
    if (output.length > outputLimit) output = output.subarray(output.length - outputLimit);
  };
  const sampleInventory = () => {
    if (!child?.pid || inventoriesInFlight.size >= 4) return;
    const sample = discoverOwnedProcesses(ownerToken, known)
      .finally(() => { inventoriesInFlight.delete(sample); });
    inventoriesInFlight.add(sample);
  };
  try {
    const warmOwnershipMonitor = !dependencies.spawnImpl && process.platform !== 'win32' && commandExists(command);
    const launchCommand = warmOwnershipMonitor ? '/bin/sh' : command;
    const launchArgs = warmOwnershipMonitor
      ? ['-c', 'sleep 0.075; exec "$@"', 'task-workspace-owner', command, ...args]
      : args;
    child = (dependencies.spawnImpl ?? spawn)(launchCommand, launchArgs, {
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
  } catch (error) {
    append(error?.stack ?? String(error));
    return {
      code: 2, signal: null, timedOut: false, wallMs: performance.now() - started,
      output: output.toString('utf8'), settled: true, surviving: [],
    };
  }
  child.stdout?.on('data', append);
  child.stderr?.on('data', append);
  const pipeTokensPromise = inheritedPipeTokens(child);
  if (Number.isInteger(child.pid) && child.pid > 0) {
    known.set(child.pid, { pid: child.pid, creation_id: '' });
    sampleInventory();
    // Sampling is cleanup assistance only. It is never settlement proof for an
    // arbitrary command because a detached child can close every inherited token.
    // Keep that diagnostic monitor responsive; avoid imposing it on the closed,
    // managed profile contract where process groups and final verification apply.
    if (dependencies.arbitraryCommand === true) {
      inventoryTimer = setInterval(sampleInventory, 20);
    } else {
      const burstStarted = performance.now();
      inventoryTimer = setInterval(() => {
        sampleInventory();
        if (performance.now() - burstStarted >= 100) {
          clearInterval(inventoryTimer);
          inventoryTimer = setInterval(sampleInventory, 10_000);
        }
      }, 3);
    }
  }
  let status;
  try {
    status = await new Promise((resolve) => {
      let terminal = false;
      const finish = (value) => {
        if (terminal) return;
        terminal = true;
        resolve(value);
      };
      child.once('error', (error) => {
        append(error?.stack ?? String(error));
        finish({ code: 2, signal: null });
      });
      child.once('exit', (code, signal) => finish({ code: code ?? (signal ? 1 : 2), signal }));
      timer = setTimeout(() => {
        timedOut = true;
        signalOwnedGroup(child, 'SIGTERM');
        escalationTimer = setTimeout(() => signalOwnedGroup(child, 'SIGKILL'), 200);
        escalationTimer.unref();
      }, timeoutMs);
    });
  } finally {
    if (timer) clearTimeout(timer);
    if (inventoryTimer) clearInterval(inventoryTimer);
    if (inventoriesInFlight.size) await Promise.allSettled([...inventoriesInFlight]);
  }
  const reconciliation = await reconcileOwnedProcesses(child, ownerToken, known, await pipeTokensPromise);
  if (escalationTimer) clearTimeout(escalationTimer);
  const arbitraryCommandUncontained = dependencies.arbitraryCommand === true
    && Number.isInteger(child?.pid) && child.pid > 0;
  if (arbitraryCommandUncontained) {
    reconciliation.settled = false;
    reconciliation.surviving = [...reconciliation.surviving,
      'containment:unavailable_for_arbitrary_command'];
  }
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

export async function main(argv = process.argv.slice(2), dependencies = {}) {
  const options = parse(argv);
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
    run: await runOwned(plan.command, plan.args, options.timeoutMs, options.mode, 262_144, {
      ...dependencies,
      arbitraryCommand: options.command !== null,
    }),
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
  const targetMs = options.mode === 'affected' ? 5_000 : options.mode === 'complete' ? 150_000 : null;
  const completeInventoryValid = options.mode !== 'complete' || (
    options.testIds.length === 0
    && receipt.inventory.length > 0
    && receipt.selected.length === receipt.inventory.length
    && new Set(receipt.selected).size === receipt.inventory.length
    && receipt.inventory.every((id) => receipt.selected.includes(id))
  );
  if (!completeInventoryValid) {
    receipt.selection_error = {
      reason: 'complete_requires_full_inventory',
      inventory: receipt.inventory.length,
      selected: receipt.selected.length,
      requested: options.testIds,
    };
    receipt.eligible = false;
  }
  const testEligible = receipt.eligible;
  receipt.performance_gate = evaluatePerformanceGate(targetMs, testEligible, comparable, run.wallMs);
  receipt.eligible = testEligible && (targetMs === null || receipt.performance_gate.eligible === true);
  receipt.exit_code = receipt.eligible ? 0
    : run.timedOut ? 124
      : run.code && run.code !== 0 ? run.code : 1;
  atomicWrite(path.resolve(options.receipt), receipt);
  if (!receipt.eligible) process.stderr.write(run.output);
  process.stdout.write(`${JSON.stringify({ receipt: path.resolve(options.receipt), eligible: receipt.eligible, mode: options.mode, selected: receipt.selected.length })}\n`);
  return receipt.exit_code;
}

if (process.argv[1]
    && fs.realpathSync(process.argv[1]) === fs.realpathSync(fileURLToPath(import.meta.url))) {
  main().then((code) => { process.exitCode = code; }).catch((error) => {
    process.stderr.write(`${error.stack ?? error}\n`);
    process.exitCode = 2;
  });
}
