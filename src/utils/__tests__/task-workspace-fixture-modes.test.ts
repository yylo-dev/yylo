import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { afterEach, describe, expect, it } from 'vitest';

const repository = path.resolve(import.meta.dirname, '../../../..');
const packageJson = path.join(repository, 'juno-code/package.json');
const runner = path.join(repository, 'juno-code/scripts/test-task-workspace.mjs');
const scratch: string[] = [];

function temporaryDirectory(): string {
  const value = fs.mkdtempSync(path.join(os.tmpdir(), 'yylo-task-workspace-mode-test-'));
  scratch.push(value);
  return value;
}

afterEach(() => {
  while (scratch.length) fs.rmSync(scratch.pop()!, { recursive: true, force: true });
});

describe('task-workspace supported profiler and runner', () => {
  it('test_task_workspace_profile_requires_supported_entrypoint', () => {
    const scripts = (JSON.parse(fs.readFileSync(packageJson, 'utf8')) as { scripts: Record<string, string> }).scripts;
    expect(scripts['test:task-workspace:affected']).toContain('test-task-workspace.mjs');
    expect(scripts['test:task-workspace:seeded']).toContain('test-task-workspace.mjs');
    expect(scripts['test:task-workspace:hermetic']).toContain('test-task-workspace.mjs');
    expect(scripts['test:task-workspace:complete']).toContain('test-task-workspace.mjs');
    expect(fs.existsSync(runner)).toBe(true);
  });

  it('keeps the complete profile as one explicit gate instead of duplicating it in the umbrella suite', () => {
    const umbrellaTest = fs.readFileSync(path.join(
      repository, 'juno-code/src/utils/__tests__/task-workspace.test.ts',
    ), 'utf8');
    const scripts = (JSON.parse(fs.readFileSync(packageJson, 'utf8')) as {
      scripts: Record<string, string>;
    }).scripts;
    const performanceGuide = fs.readFileSync(path.join(
      repository, 'juno-code/docs/test-performance.md',
    ), 'utf8');

    expect(scripts['test:task-workspace:complete']).toBe(
      'node scripts/test-task-workspace.mjs --mode complete',
    );
    expect(umbrellaTest).not.toContain("'--mode', 'complete'");
    expect(umbrellaTest).not.toContain('complete profile prerequisite drain');
    expect(umbrellaTest).not.toContain('acquireTestResourceLock');
    expect(performanceGuide).toMatch(
      /the single task-owned 239-case performance\s+gate/,
    );
  });

  it('test_task_workspace_profile_reports_per_test_fixture_and_git_process_timing', () => {
    const root = temporaryDirectory();
    const receipt = path.join(root, 'receipt.json');
    const result = spawnSync(process.execPath, [runner, '--mode', 'seeded', '--receipt', receipt,
      '--test-id', 'SemVerValidationTests.test_rejects_malformed_versions'], {
      cwd: path.join(repository, 'juno-code'), encoding: 'utf8', timeout: 10_000,
    });
    expect(result.status, result.stderr).toBe(0);
    const value = JSON.parse(fs.readFileSync(receipt, 'utf8')) as Record<string, any>;
    expect(value.schema_version).toBe('juno.task_workspace.profile.v1');
    expect(value.mode).toBe('seeded');
    expect(value.eligible).toBe(true);
    expect(value.tests).toEqual([
      expect.objectContaining({
        id: 'SemVerValidationTests.test_rejects_malformed_versions',
        tier: 'pure',
        fixture_ms: expect.any(Number),
        execution_ms: expect.any(Number),
        git_processes: 0,
        subprocess_processes: expect.any(Number),
        process_argv_identity: expect.stringMatching(/^[0-9a-f]{64}$/),
        output_identity: expect.stringMatching(/^[0-9a-f]{64}$/),
      }),
    ]);
    expect(value.summary.wall).toEqual(expect.objectContaining({ p50_ms: expect.any(Number), p95_ms: expect.any(Number) }));
    expect(value.processes).toEqual(expect.objectContaining({ settled: true }));
  });

  it('rejects an ineligible applicable performance gate with a truthful nonzero result', () => {
    const root = temporaryDirectory();
    const receipt = path.join(root, 'performance.json');
    const result = spawnSync(process.execPath, [runner, '--mode', 'affected', '--receipt', receipt,
      '--command', process.execPath, '--command-arg', '-e', '--command-arg',
      'setTimeout(() => {}, 5100)'], {
      cwd: path.join(repository, 'juno-code'), encoding: 'utf8', timeout: 10_000,
    });
    expect(result.status).not.toBe(0);
    const value = JSON.parse(fs.readFileSync(receipt, 'utf8')) as Record<string, any>;
    expect(value.exit_code).toBe(result.status);
    expect(value.shards[0].exit_code).toBe(0);
    expect(value.eligible).toBe(false);
    expect(value.performance_gate).toEqual(expect.objectContaining({
      applicable: true,
      eligible: false,
    }));
    expect(['target_exceeded', 'incomparable_environment']).toContain(
      value.performance_gate.reason,
    );
  }, 15_000);

  it('replays an explicitly requested seeded test hermetically and rejects zero selection', () => {
    const root = temporaryDirectory();
    const seededId = 'TaskWorkspaceTests.test_finish_queues_clean_committed_tip_without_merging_or_cleanup';
    const replayReceipt = path.join(root, 'replay.json');
    const replay = spawnSync(process.execPath, [runner, '--mode', 'hermetic', '--receipt', replayReceipt,
      '--test-id', seededId], {
      cwd: path.join(repository, 'juno-code'), encoding: 'utf8', timeout: 30_000,
    });
    expect(replay.status, replay.stderr).toBe(0);
    const replayValue = JSON.parse(fs.readFileSync(replayReceipt, 'utf8')) as Record<string, any>;
    expect(replayValue.selected).toEqual([seededId]);
    expect(replayValue.counts.selected).toBe(1);
    expect(replayValue.eligible).toBe(true);

    const missingReceipt = path.join(root, 'missing.json');
    const missing = spawnSync(process.execPath, [runner, '--mode', 'hermetic', '--receipt', missingReceipt,
      '--test-id', 'TaskWorkspaceTests.test_not_in_inventory'], {
      cwd: path.join(repository, 'juno-code'), encoding: 'utf8', timeout: 10_000,
    });
    expect(missing.status).not.toBe(0);
    const missingValue = JSON.parse(fs.readFileSync(missingReceipt, 'utf8')) as Record<string, any>;
    expect(missingValue.selected).toEqual([]);
    expect(missingValue.counts.selected).toBe(0);
    expect(missingValue.eligible).toBe(false);
  }, 45_000);

  it('reconciles descendants after a normal leader exit before claiming settlement', () => {
    const root = temporaryDirectory();
    const receipt = path.join(root, 'normal-exit.json');
    const probe = path.join(root, 'normal-exit.py');
    const childPid = path.join(root, 'normal-child.pid');
    fs.writeFileSync(probe, [
      'import pathlib, subprocess, sys',
      `p=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(2)'])`,
      `pathlib.Path(${JSON.stringify(childPid)}).write_text(str(p.pid))`,
    ].join('\n'));
    const started = performance.now();
    const result = spawnSync(process.execPath, [runner, '--mode', 'seeded', '--receipt', receipt,
      '--command', 'python3', '--command-arg', probe], {
      cwd: path.join(repository, 'juno-code'), encoding: 'utf8', timeout: 5_000,
    });
    const elapsed = performance.now() - started;
    expect(result.status, result.stderr).toBe(0);
    expect(elapsed).toBeLessThan(1_000);
    const value = JSON.parse(fs.readFileSync(receipt, 'utf8')) as Record<string, any>;
    expect(value.processes).toEqual({ settled: true, surviving: [] });
    const pid = Number(fs.readFileSync(childPid, 'utf8'));
    expect(() => process.kill(pid, 0)).toThrow();
  }, 10_000);

  it.runIf(process.platform !== 'win32')('reconciles an env-scrubbing descendant that escapes into a new session', () => {
    const root = temporaryDirectory();
    const receipt = path.join(root, 'escaped-session.json');
    const probe = path.join(root, 'escaped-session.py');
    const childPid = path.join(root, 'escaped-child.pid');
    fs.writeFileSync(probe, [
      'import pathlib, subprocess, sys',
      `p=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True, env={})`,
      `pathlib.Path(${JSON.stringify(childPid)}).write_text(str(p.pid))`,
    ].join('\n'));
    let pid: number | undefined;
    try {
      const result = spawnSync(process.execPath, [runner, '--mode', 'seeded', '--receipt', receipt,
        '--command', 'python3', '--command-arg', probe], {
        cwd: path.join(repository, 'juno-code'), encoding: 'utf8', timeout: 5_000,
      });
      expect(result.status, result.stderr).toBe(0);
      const value = JSON.parse(fs.readFileSync(receipt, 'utf8')) as Record<string, any>;
      expect(value.exit_code).toBe(result.status);
      expect(value.processes).toEqual({ settled: true, surviving: [] });
      pid = Number(fs.readFileSync(childPid, 'utf8'));
      expect(() => process.kill(pid!, 0)).toThrow();
    } finally {
      if (pid) {
        try { process.kill(pid, 'SIGKILL'); } catch { /* already reconciled */ }
      }
    }
  }, 10_000);

  it('finalizes asynchronous launch failures promptly with an exact terminal receipt', () => {
    const root = temporaryDirectory();
    const receipt = path.join(root, 'async-spawn-failure.json');
    const started = performance.now();
    const result = spawnSync(process.execPath, [runner, '--mode', 'seeded', '--receipt', receipt,
      '--timeout-ms', '100', '--command', path.join(root, 'does-not-exist')], {
      cwd: path.join(repository, 'juno-code'), encoding: 'utf8', timeout: 5_000,
    });
    expect(performance.now() - started).toBeLessThan(1_000);
    expect(result.status).not.toBe(0);
    expect(fs.existsSync(receipt)).toBe(true);
    const value = JSON.parse(fs.readFileSync(receipt, 'utf8')) as Record<string, any>;
    expect(value.exit_code).toBe(result.status);
    expect(value.eligible).toBe(false);
    expect(value.timeout).toBe(false);
    expect(value.processes).toEqual({ settled: true, surviving: [] });
    expect(value.output.truncated_tail).toMatch(/ENOENT|not found/i);
  });

  it('finalizes synchronous spawn exceptions through the same terminal receipt path', async () => {
    const root = temporaryDirectory();
    const receipt = path.join(root, 'sync-spawn-failure.json');
    const module = await import(pathToFileURL(runner).href) as Record<string, any>;
    const code = await module.main([
      '--mode', 'seeded', '--receipt', receipt, '--timeout-ms', '100',
      '--command', 'synthetic-command',
    ], {
      spawnImpl: () => { throw Object.assign(new Error('synthetic synchronous spawn failure'), { code: 'EINVAL' }); },
    });
    expect(code).not.toBe(0);
    const value = JSON.parse(fs.readFileSync(receipt, 'utf8')) as Record<string, any>;
    expect(value.exit_code).toBe(code);
    expect(value.eligible).toBe(false);
    expect(value.timeout).toBe(false);
    expect(value.processes).toEqual({ settled: true, surviving: [] });
    expect(value.output.truncated_tail).toContain('synthetic synchronous spawn failure');
  });

  it('refuses a reused Windows PID whose creation identity no longer matches', async () => {
    const module = await import(pathToFileURL(runner).href) as Record<string, any>;
    const known = [{ pid: 4100, creation_id: '20260902220000.000000-000' }];
    const replacement = [{
      pid: 4100, parent_pid: 99, creation_id: '20260902220100.000000-000', command: 'foreign.exe',
    }];
    expect(module.selectVerifiedOwnedProcesses(known, replacement)).toEqual([]);
    expect(module.selectVerifiedOwnedProcesses(known, [{
      ...replacement[0], creation_id: known[0].creation_id,
    }])).toEqual([{ ...replacement[0], creation_id: known[0].creation_id }]);
  });

  it('returns nonzero and reports survivors when settlement cannot be verified', () => {
    const root = temporaryDirectory();
    const receipt = path.join(root, 'unverified.json');
    const result = spawnSync(process.execPath, [runner, '--mode', 'seeded', '--receipt', receipt,
      '--command', process.execPath, '--command-arg', '-e', '--command-arg', 'process.exit(0)'], {
      cwd: path.join(repository, 'juno-code'), encoding: 'utf8', timeout: 5_000,
      env: { ...process.env, PATH: root },
    });
    expect(result.status).not.toBe(0);
    const value = JSON.parse(fs.readFileSync(receipt, 'utf8')) as Record<string, any>;
    expect(value.exit_code).toBe(result.status);
    expect(value.processes.settled).toBe(false);
    expect(value.processes.surviving).toEqual([
      expect.stringMatching(/^verification:/),
    ]);
  });

  it('task-workspace-wrapper-enforces-child-timeout-and-process-settlement', () => {
    const root = temporaryDirectory();
    const receipt = path.join(root, 'timeout.json');
    const probe = path.join(root, 'probe.py');
    const childPid = path.join(root, 'child.pid');
    fs.writeFileSync(probe, [
      'import pathlib, subprocess, sys, time',
      `p=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])`,
      `pathlib.Path(${JSON.stringify(childPid)}).write_text(str(p.pid))`,
      'time.sleep(60)',
    ].join('\n'));
    const result = spawnSync(process.execPath, [runner, '--mode', 'affected', '--receipt', receipt,
      '--timeout-ms', '250', '--command', 'python3', '--command-arg', probe], {
      cwd: path.join(repository, 'juno-code'), encoding: 'utf8', timeout: 10_000,
    });
    expect(result.status).not.toBe(0);
    const value = JSON.parse(fs.readFileSync(receipt, 'utf8')) as Record<string, any>;
    expect(value.timeout).toBe(true);
    expect(value.exit_code).toBe(result.status);
    expect(value.eligible).toBe(false);
    expect(value.processes.settled).toBe(true);
    const pid = Number(fs.readFileSync(childPid, 'utf8'));
    expect(() => process.kill(pid, 0)).toThrow();
  });
});
