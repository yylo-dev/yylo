import { spawn, spawnSync, type ChildProcess } from 'node:child_process';
import * as os from 'node:os';
import * as path from 'node:path';
import fs from 'fs-extra';
import { afterEach, describe, expect, it } from 'vitest';
import fullConfig, { MANAGED_INSTALL_POOL_MATCH_GLOBS } from '../../../vitest.config.js';
import fastConfig from '../../../vitest.fast.config.js';
import { runBoundedTestProcess } from '../../test-utils/bounded-process.js';
import { contentionBudgetMs } from '../../test-utils/contention-budget.js';
import {
  acquireTestResourceLock,
  DEFAULT_SHARED_RESOURCE_ACQUIRE_TIMEOUT_MS,
  MANAGED_INSTALL_OPERATION_TIMEOUT_MS,
  normalizeTestResourceLockPath,
  processBirthIdentity,
  type TestResourceLockOwner,
} from '../../test-utils/resource-lock.js';

const fixtures: string[] = [];
const children: ChildProcess[] = [];
const pythonModule = path.resolve(import.meta.dirname, '../../templates/scripts/tests/test_task_workspace.py');

async function fixture(): Promise<{ root: string; lockPath: string }> {
  const root = await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(), 'juno-resource-lock-test-')));
  fixtures.push(root);
  return { root, lockPath: path.join(root, 'shared.lock') };
}
async function owner(overrides: Partial<TestResourceLockOwner> = {}): Promise<TestResourceLockOwner> {
  return {
    pid: process.pid, processBirthId: (await processBirthIdentity(process.pid))!,
    token: 'fixture-token', workload: 'fixture', process: 'vitest', cwd: process.cwd(),
    startedAt: new Date().toISOString(), ...overrides,
  };
}
async function waitFor(pathname: string): Promise<void> {
  const deadline = Date.now() + 5_000;
  while (!(await fs.pathExists(pathname))) {
    if (Date.now() > deadline) throw new Error(`timed out waiting for ${pathname}`);
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
}
async function waitForExit(child: ChildProcess): Promise<number | null> {
  if (child.exitCode !== null) return child.exitCode;
  return new Promise((resolve, reject) => {
    child.once('error', reject);
    child.once('exit', resolve);
  });
}
afterEach(async () => {
  for (const child of children.splice(0)) child.kill('SIGKILL');
  for (const root of fixtures.splice(0)) await fs.remove(root);
});

describe('cross-language heavy test resource lock', () => {
  it('routes every managed-install lock sharer through one single-fork file lane', async () => {
    const testDirectory = path.resolve(import.meta.dirname);
    const files = (await fs.readdir(testDirectory)).filter((name) => name.endsWith('.test.ts'));
    const sources = await Promise.all(files.map(async (name) => ({
      name, source: await fs.readFile(path.join(testDirectory, name), 'utf8'),
    })));
    const lockSharers = sources
      .filter(({ source }) => source.includes(['useShared', 'HeavyWorkloadLock('].join('')))
      .map(({ name }) => `src/utils/__tests__/${name}`).sort();
    const scheduled = MANAGED_INSTALL_POOL_MATCH_GLOBS.map(([glob, pool]) => {
      expect(pool).toBe('forks');
      return glob;
    }).sort();
    const indirectLockSharers = [
      'src/cli/__tests__/benchmark-command.test.ts',
      'src/utils/__tests__/task-workspace-fixture-modes.test.ts',
      'src/utils/__tests__/task-workspace.test.ts',
    ];

    expect([...lockSharers, ...indirectLockSharers].sort()).toEqual(scheduled);
    for (const config of [fullConfig, fastConfig]) {
      expect(config.test?.poolMatchGlobs).toEqual(MANAGED_INSTALL_POOL_MATCH_GLOBS);
      expect(config.test?.poolOptions?.forks).toMatchObject({ singleFork: true, isolate: true });
      expect(config.test?.poolOptions?.threads).toMatchObject({ singleThread: false });
    }
  });

  it('keeps loaded-worktree wait policy bounded beyond the legacy five-minute cap', async () => {
    expect(DEFAULT_SHARED_RESOURCE_ACQUIRE_TIMEOUT_MS).toBe(1_200_000);
    expect(DEFAULT_SHARED_RESOURCE_ACQUIRE_TIMEOUT_MS).toBeGreaterThan(300_000);
    expect(MANAGED_INSTALL_OPERATION_TIMEOUT_MS).toBe(900_000);

    const { lockPath } = await fixture();
    const holder = await acquireTestResourceLock('loaded holder', { lockPath });
    const diagnostics: string[] = [];
    const pending = acquireTestResourceLock('concurrent worktree', {
      lockPath,
      // Scaled deterministic contention: wait past the old local cap while the
      // policy budget remains independently bounded. The budget is
      // contention-aware so a starved event loop cannot expire the successor
      // before the deterministic release point.
      timeoutMs: contentionBudgetMs(1_000),
      pollMs: 5,
      diagnosticIntervalMs: 50,
      onDiagnostic: (message) => diagnostics.push(message),
    });
    await new Promise((resolve) => setTimeout(resolve, 180));
    await holder.release();
    const successor = await pending;
    expect(successor.waitedMs).toBeGreaterThanOrEqual(150);
    expect(diagnostics.join('\n')).toMatch(/waiting workload="concurrent worktree".*owner_workload="loaded holder"/);
    await successor.release();
  });

  it('serializes simultaneous acquirers with fully published owner diagnostics', async () => {
    const { lockPath } = await fixture();
    const diagnostics: string[] = [];
    const options = { lockPath, pollMs: 5, diagnosticIntervalMs: 20,
      onDiagnostic: (message: string) => diagnostics.push(message) };
    const promises = ['first', 'second'].map((name) => acquireTestResourceLock(name, options));
    const winner = await Promise.race(promises.map((promise, index) => promise.then((lease) => ({ lease, index }))));
    await new Promise((resolve) => setTimeout(resolve, 80));
    await winner.lease.release();
    const follower = await promises[1 - winner.index]!;
    expect(follower.waitedMs).toBeGreaterThan(40);
    expect(diagnostics.join('\n')).toMatch(/owner_inode=\[[0-9]+,[0-9]+\].*owner_workload="(first|second)"/);
    await follower.release();
  });

  it('makes concurrent stale recoverers and a successor one CAS-serialized ownership chain', async () => {
    const { root, lockPath } = await fixture();
    await fs.writeJson(lockPath, await owner({
      processBirthId: `${(await processBirthIdentity(process.pid))!}-same-second-reused`,
      token: 'stale-token', workload: 'same-second stale owner',
    }));
    const diagnostics: string[] = [];
    const options = { lockPath, pollMs: 5, diagnosticIntervalMs: 20,
      onDiagnostic: (message: string) => diagnostics.push(message) };
    const pending = ['recoverer-a', 'recoverer-b', 'successor'].map((name) =>
      acquireTestResourceLock(name, options));
    const first = await Promise.race(pending.map((promise, index) => promise.then((lease) => ({ lease, index }))));
    const published = await fs.readJson(lockPath);
    expect(published.token).toBe(first.lease.owner.token);
    expect(diagnostics.join('\n')).toContain('same-second stale owner');
    await first.lease.release();
    const remaining = pending.filter((_, index) => index !== first.index);
    const second = await Promise.race(remaining.map((promise, index) => promise.then((lease) => ({ lease, index }))));
    expect((await fs.readJson(lockPath)).token).toBe(second.lease.owner.token);
    await second.lease.release();
    const third = await remaining[1 - second.index]!;
    expect((await fs.readJson(lockPath)).token).toBe(third.owner.token);
    await third.release();
    expect((await fs.readdir(root)).filter((name) => name.includes('stale-') || name.includes('release-'))).toEqual([]);
  });

  it('retries after flock when a regular guard replacement creates a new lock domain', async () => {
    const { root, lockPath } = await fixture();
    const guard = path.join(root, '.shared.lock.protocol');
    const marker = (name: string) => path.join(root, name);
    const probe = (name: string) => {
      const child = spawn('python3', [
        pythonModule, '--resource-lock-guard-probe', lockPath,
        marker(`${name}-opened`), marker(`${name}-entered`), marker(`${name}-release`),
      ], { stdio: ['ignore', 'ignore', 'pipe'] });
      children.push(child);
      return child;
    };

    const oldHolder = probe('old-holder');
    await waitFor(marker('old-holder-entered'));
    const oldWaiter = probe('old-waiter');
    await waitFor(marker('old-waiter-opened'));

    // Replace the regular guard pathname while the waiter is blocked on the
    // old inode, then prove a holder can enter the new pathname domain.
    const replacement = `${guard}.replacement`;
    await fs.writeFile(replacement, '');
    await fs.rename(replacement, guard);
    const newHolder = probe('new-holder');
    await waitFor(marker('new-holder-entered'));

    await fs.writeFile(marker('old-holder-release'), 'release');
    await new Promise((resolve) => setTimeout(resolve, 200));
    // The old waiter acquired the unlinked old inode, detected the post-flock
    // identity change, and retried; it must not overlap the new holder.
    expect(await fs.pathExists(marker('old-waiter-entered'))).toBe(false);

    await fs.writeFile(marker('new-holder-release'), 'release');
    await waitFor(marker('old-waiter-entered'));
    await fs.writeFile(marker('old-waiter-release'), 'release');
    expect(await Promise.all([oldHolder, oldWaiter, newHolder].map(waitForExit))).toEqual([0, 0, 0]);
  });

  it('never removes a successor when an obsolete token/inode releases', async () => {
    const { lockPath } = await fixture();
    const lease = await acquireTestResourceLock('obsolete', { lockPath });
    const successor = await owner({ token: 'successor-token', workload: 'valid successor' });
    const temp = `${lockPath}.successor`;
    await fs.writeJson(temp, successor); await fs.rename(temp, lockPath);
    await lease.release();
    expect((await fs.readJson(lockPath)).token).toBe('successor-token');
  });

  it('fails closed for ownerless publication and leaves no acquisition temporaries', async () => {
    const { root, lockPath } = await fixture();
    await fs.writeFile(lockPath, '');
    await expect(acquireTestResourceLock('ownerless', {
      lockPath, timeoutMs: 40, pollMs: 5, onDiagnostic: () => undefined,
    })).rejects.toThrow(/owner=<invalid-or-unavailable>/);
    expect(await fs.readFile(lockPath, 'utf8')).toBe('');
    expect((await fs.readdir(root)).filter((name) => name.includes('.owner-'))).toEqual([]);
  });

  it('uses sub-second kernel birth identity and detects same-second PID reuse', async () => {
    const identity = await processBirthIdentity(process.pid);
    expect(identity).toMatch(/^(linux-start-ticks:\d+|darwin-start-time:\d+:\d+)$/);
    const { lockPath } = await fixture();
    const prefix = identity!.replace(/(:\d+)$/, '');
    await fs.writeJson(lockPath, await owner({
      processBirthId: `${prefix}:1`, token: 'same-second-old', workload: 'same-second reuse',
    }));
    const lease = await acquireTestResourceLock('new precise birth', { lockPath });
    expect(lease.owner.token).not.toBe('same-second-old');
    await lease.release();
  });

  it('applies one exact Node/Python override matrix', async () => {
    const { root } = await fixture();
    const matrix = [
      '', '   ', 'relative.lock', `${root}/lock`, `${root}/lock/`, `${root}//lock`,
      `${root}/./lock`, `${root}/child/../lock`,
    ];
    const script = `
import importlib.util,json,sys
spec=importlib.util.spec_from_file_location("probe",sys.argv[1]); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
out=[]
for value in json.loads(sys.argv[2]):
 try: out.append([True,str(m._configured_lock_path(value))])
 except RuntimeError as e: out.append([False,str(e)])
print(json.dumps(out))
`;
    const result = spawnSync('python3', ['-c', script, pythonModule, JSON.stringify(matrix)], { encoding: 'utf8' });
    expect(result.status).toBe(0);
    const pythonResults = JSON.parse(result.stdout) as Array<[boolean, string]>;
    const nodeResults = matrix.map((value): [boolean, string] => {
      try { return [true, normalizeTestResourceLockPath(value)]; }
      catch (error) { return [false, String(error)]; }
    });
    expect(nodeResults.map(([accepted]) => accepted)).toEqual(pythonResults.map(([accepted]) => accepted));
    expect(nodeResults.slice(0, 2).every(([accepted]) => accepted)).toBe(true);
    expect(nodeResults[3]).toEqual([true, `${root}/lock`]);
    expect(nodeResults.slice(4).every(([accepted]) => !accepted)).toBe(true);
  });

  it('rejects symlinked protocol/owner paths and components', async () => {
    const { root } = await fixture();
    const real = path.join(root, 'real'); await fs.ensureDir(real);
    const linked = path.join(root, 'linked'); await fs.symlink(real, linked);
    await expect(acquireTestResourceLock('parent attack', { lockPath: path.join(linked, 'lock') }))
      .rejects.toThrow(/symlinked lock path component/);
    const lockPath = path.join(real, 'lock');
    const guard = path.join(real, '.lock.protocol');
    const target = path.join(real, 'target'); await fs.writeFile(target, 'safe'); await fs.symlink(target, guard);
    await expect(acquireTestResourceLock('guard attack', { lockPath })).rejects.toThrow(/symlinked lock path component/);
    expect(await fs.readFile(target, 'utf8')).toBe('safe');
  });

  it('terminates timed-out and cancelled groups before descendants can mutate later', async () => {
    const { root } = await fixture();
    const grandchild = [
      'import signal,time,pathlib,sys',
      'signal.signal(signal.SIGTERM, signal.SIG_IGN)',
      'time.sleep(.5)',
      'pathlib.Path(sys.argv[1]).write_text("escaped")',
    ].join(';');
    const parent = [
      'import subprocess,sys,time',
      `subprocess.Popen([sys.executable,'-c',${JSON.stringify(grandchild)},sys.argv[1]])`,
      'time.sleep(30)',
    ].join(';');
    for (const reason of ['timeout', 'cancellation'] as const) {
      const marker = path.join(root, `escaped-${reason}`);
      const controller = new AbortController();
      const cancellation = reason === 'cancellation'
        ? setTimeout(() => controller.abort(), 100)
        : undefined;
      const result = await runBoundedTestProcess('python3', ['-c', parent, marker], {
        timeoutMs: reason === 'timeout' ? 100 : 2_000,
        terminationGraceMs: 100,
        signal: controller.signal,
      });
      if (cancellation) clearTimeout(cancellation);
      expect(result[reason === 'timeout' ? 'timedOut' : 'cancelled']).toBe(true);
      expect(result.diagnostic).toMatch(new RegExp(`reason=${reason}.*pid=\\d+.*pgid=\\d+`));
      await new Promise((resolve) => setTimeout(resolve, 550));
      expect(await fs.pathExists(marker)).toBe(false);
    }
  });

  it('interoperates with an actual Python owner and Node successor', async () => {
    const { root, lockPath } = await fixture();
    const ready = path.join(root, 'ready');
    const release = path.join(root, 'release');
    const code = `
import importlib.util,pathlib,sys,time
spec=importlib.util.spec_from_file_location("probe",sys.argv[1]); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
lock,ready,release=map(pathlib.Path,sys.argv[2:5])
token,_=m._acquire_resource_lock("python-owner",lock); ready.write_text("ready")
while not release.exists(): time.sleep(.01)
m._release_resource_lock(lock,token)
`;
    const child = spawn('python3', [
      '-c', code, pythonModule, lockPath, ready, release,
    ], { stdio: 'ignore' });
    children.push(child); await waitFor(ready);

    const pythonOwner = await fs.readJson(lockPath) as TestResourceLockOwner;
    expect(pythonOwner.workload).toBe('python-owner');
    expect(pythonOwner.token).toBeTruthy();

    let observedBlocked!: () => void;
    const blocked = new Promise<void>((resolve) => { observedBlocked = resolve; });
    let successorSettled = false;
    const pending = acquireTestResourceLock('node-successor', {
      lockPath, pollMs: 5, diagnosticIntervalMs: 5,
      onDiagnostic: () => observedBlocked(),
    });
    void pending.then(
      () => { successorSettled = true; },
      () => { successorSettled = true; },
    );
    let blockedTimeout: ReturnType<typeof setTimeout> | undefined;
    try {
      await Promise.race([
        blocked,
        new Promise<never>((_, reject) => {
          blockedTimeout = setTimeout(
            () => reject(new Error('Node successor never observed the Python owner')),
            5_000,
          );
        }),
      ]);
    } finally {
      if (blockedTimeout) clearTimeout(blockedTimeout);
    }

    // Ownership publication and the still-pending successor are durable proof
    // of serialization; a transient diagnostic can be superseded during CAS.
    expect((await fs.readJson(lockPath)).token).toBe(pythonOwner.token);
    expect(successorSettled).toBe(false);

    await fs.writeFile(release, 'release');
    const lease = await pending;
    expect(lease.waitedMs).toBeGreaterThan(0);
    expect(lease.owner.workload).toBe('node-successor');
    expect(lease.owner.token).not.toBe(pythonOwner.token);
    expect((await fs.readJson(lockPath)).token).toBe(lease.owner.token);
    expect(await waitForExit(child)).toBe(0);
    await lease.release();
  });
});
