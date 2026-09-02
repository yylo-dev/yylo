import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
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

  it('test_task_workspace_profile_reports_per_test_fixture_and_git_process_timing', () => {
    const root = temporaryDirectory();
    const receipt = path.join(root, 'receipt.json');
    const result = spawnSync(process.execPath, [runner, '--mode', 'affected', '--receipt', receipt,
      '--test-id', 'SemVerValidationTests.test_rejects_malformed_versions'], {
      cwd: path.join(repository, 'juno-code'), encoding: 'utf8', timeout: 10_000,
    });
    expect(result.status, result.stderr).toBe(0);
    const value = JSON.parse(fs.readFileSync(receipt, 'utf8')) as Record<string, any>;
    expect(value.schema_version).toBe('juno.task_workspace.profile.v1');
    expect(value.mode).toBe('affected');
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
    expect(value.eligible).toBe(false);
    expect(value.processes.settled).toBe(true);
    const pid = Number(fs.readFileSync(childPid, 'utf8'));
    expect(() => process.kill(pid, 0)).toThrow();
  });
});
