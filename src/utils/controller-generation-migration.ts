import { execFile as execFileCallback, spawn } from 'node:child_process';
import { AsyncLocalStorage } from 'node:async_hooks';
import { promisify } from 'node:util';
import { fileURLToPath } from 'node:url';
import * as os from 'node:os';
import * as path from 'node:path';
import fs from 'fs-extra';
import { assertSafeManagedWritePath, lstatIfPresent } from './managed-update-transaction.js';

const execFile = promisify(execFileCallback);
export const GENERATION_MIGRATION_ROOT = '.juno_task/runtime/generation-migration';

/** Digest must come from reviewed installation/artifact provenance, never a version lookup. */
export interface InstalledGenerationEvidence {
  root: string;
  artifact: string;
  sha256: string;
}
export interface ControllerGenerationPlan {
  schema_version: 'yylo_controller_generation_transaction.v1';
  id: string;
  attempt: string;
  controller: string;
  candidate: InstalledGenerationEvidence;
  previous: InstalledGenerationEvidence;
  active_pins: Record<string, { state: string; attempt: number; executable: string; generation: InstalledGenerationEvidence }>;
  [key: string]: unknown;
}
export interface ControllerGenerationResult {
  schema_version: 'yylo_controller_generation_transaction.v1';
  id: string;
  outcome: 'completed' | 'rolled_back';
  active_pins: ControllerGenerationPlan['active_pins'];
}

/** Fence check is an exported dispatch contract; it never performs first-use migration. */
export async function assertControllerGenerationReady(projectDir: string): Promise<void> {
  const fence = path.join(projectDir, GENERATION_MIGRATION_ROOT, 'fence.json');
  await assertSafeManagedWritePath(projectDir, fence);
  if (await lstatIfPresent(fence)) {
    throw new Error('generation_transition_incomplete: resume or roll back the exact generation journal; do not refresh');
  }
}

export function packagedGenerationRoot(): string {
  return path.resolve(path.dirname(packagedEngine()), '../../..');
}

export function retainedTaskRuntime(projectDir: string, taskId: string): Promise<{
  pinned: boolean; script?: string; executable?: string; attempt?: number;
}> {
  return maintenance(projectDir, 'task-pin', { task_id: taskId });
}

function packagedEngine(): string {
  const directory = path.dirname(fileURLToPath(import.meta.url));
  const engine = [
    path.resolve(directory, 'templates/maintenance/controller_generation_migration.py'),
    path.resolve(directory, '../templates/maintenance/controller_generation_migration.py'),
  ].find(candidate => fs.existsSync(candidate));
  if (!engine) throw new Error('generation_engine_missing: installed maintenance engine is unavailable');
  return engine;
}

const heldLocks = new AsyncLocalStorage<ReadonlySet<string>>();

/** Hold the same OS lock across the complete installer mutation/rollback window. */
export async function withControllerGenerationMutation<T>(projectDir: string, operation: () => Promise<T>): Promise<T> {
  const root = path.resolve(projectDir);
  const inherited = heldLocks.getStore();
  if (inherited?.has(root) || !(await lstatIfPresent(path.join(root, '.juno_task')))) return operation();
  await assertControllerGenerationReady(root);
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), 'yylo-generation-lock-'));
  try {
    const child = spawn('python3', ['-E', '-B', '-X', `pycache_prefix=${path.join(temporary, 'bytecode')}`,
      packagedEngine(), 'hold-lock', '--controller', root], { cwd: root, stdio: ['pipe', 'pipe', 'pipe'] });
    let output = '';
    let errors = '';
    const closed = new Promise<number | null>((resolve, reject) => {
      child.once('error', reject);
      child.once('close', resolve);
    });
    void closed.catch(() => undefined);
    child.stdin.on('error', () => undefined); // acquisition refusal may close the pipe before release
    child.stderr.on('data', chunk => { errors = (errors + String(chunk)).slice(-4000); });
    try {
      await new Promise<void>((resolve, reject) => {
        let ready = false;
        child.once('error', reject);
        child.stdout.on('data', chunk => {
          if (ready) return;
          output += String(chunk);
          if (!output.includes('\n')) return;
          try {
            const response = JSON.parse(output.slice(0, output.indexOf('\n'))) as { locked?: boolean; code?: string; detail?: string };
            if (!response.locked) { reject(new Error(`${response.code}: ${response.detail}`)); return; }
            ready = true;
            resolve();
          } catch (error) { reject(error); }
        });
        child.once('close', () => { if (!ready) reject(new Error(output || errors || 'generation lock process exited')); });
      });
      await assertControllerGenerationReady(root);
      return await heldLocks.run(new Set([...(inherited ?? []), root]), operation);
    } finally {
      child.stdin.end();
      const code = await closed;
      if (code !== 0 && !output.includes('"outcome": "refused"')) {
        throw new Error(`generation_lock_lost: ${errors || output}`);
      }
    }
  } finally { await fs.remove(temporary); }
}

/** Invocation-local reader guard. A release callback alone is not proof that
 * the lock child still owns its reader lock at the next admission boundary. */
export type ControllerGenerationReadLease = (() => Promise<void>) & {
  assertHeld(): void;
  assessActive(): Promise<ActiveControllerGeneration>;
  recheckActive(): Promise<boolean>;
};

/** Shared lease spans execution, so no updater can replace scripts mid-command.
 * Nested commands take shared leases too; an updater must obtain the exclusive lock.
 */
export async function acquireControllerGenerationReadLease(projectDir: string): Promise<ControllerGenerationReadLease> {
  // -B prevents writes, not reads of poisoned sibling bytecode. Use the same
  // private cache-prefix isolation as all other authenticated engine entries.
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), 'yylo-generation-reader-'));
  const child = spawn('python3', ['-E', '-B', '-X', `pycache_prefix=${path.join(temporary, 'bytecode')}`,
    packagedEngine(), 'hold-read', '--controller', path.resolve(projectDir)],
    { cwd: projectDir, stdio: ['pipe', 'pipe', 'pipe'] });
  let output = ''; let errors = '';
  let held = false;
  let released = false;
  let pending: { resolve(value: any): void; reject(error: Error): void } | undefined;
  const fail = (error: Error) => { const request = pending; pending = undefined; request?.reject(error); };
  child.stdin.on('error', fail);
  child.stderr.on('data', chunk => { errors = (errors + String(chunk)).slice(-4000); });
  child.stdout.on('data', chunk => {
    output += String(chunk);
    if (output.length > 64 * 1024) {
      fail(new Error('generation_reader_response_invalid: response exceeds bounds')); child.stdin.end(); return;
    }
    while (output.includes('\n')) {
      const end = output.indexOf('\n'); const line = output.slice(0, end); output = output.slice(end + 1);
      const request = pending; pending = undefined;
      if (!request) { held = false; child.stdin.end(); return; }
      try {
        const response = JSON.parse(line);
        if (response.error) request.reject(new Error(response.error));
        else request.resolve(response);
      } catch (error) { request.reject(error instanceof Error ? error : new Error(String(error))); }
    }
  });
  const closed = new Promise<void>(resolve => { child.once('close', async () => {
    held = false; fail(new Error(errors || 'generation_read_lock_lost: reader exited'));
    // Only invocation-owned scratch; cleanup never replaces the primary error.
    await fs.remove(temporary).catch(() => undefined);
    resolve();
  }); });
  child.once('error', fail);
  child.once('exit', () => { held = false; });
  const assertHeld = () => {
    if (!held || released || child.exitCode !== null || child.signalCode !== null || child.killed) {
      throw new Error('generation_read_lock_lost: reader guard is no longer held; retry ordinary admission');
    }
  };
  const exitRelease = () => child.stdin.end();
  process.once('exit', exitRelease);
  try {
    const timeout = setTimeout(() => {
      fail(new Error('generation_read_lock_timeout')); child.kill();
    }, 15000);
    try {
      const response = await new Promise<any>((resolve, reject) => { pending = { resolve, reject }; });
      if (response.locked !== true) {
        throw new Error(response.code && response.detail ? `${response.code}: ${response.detail}`
          : 'generation_reader_response_invalid: lock not acquired');
      }
      held = true;
      assertHeld();
    } finally { clearTimeout(timeout); }
  } catch (error) {
    process.removeListener('exit', exitRelease); child.stdin.end(); await closed; throw error;
  }
  const request = async (operation: 'assess-active' | 'recheck-active'): Promise<any> => {
    assertHeld();
    if (pending) throw new Error('generation_reader_busy: concurrent admission requests');
    const response = new Promise<any>((resolve, reject) => { pending = { resolve, reject }; });
    child.stdin.write(`${operation}\n`, error => { if (error) fail(error); });
    const result = await response;
    assertHeld();
    return result;
  };
  const release = async () => {
    released = true;
    held = false;
    process.removeListener('exit', exitRelease);
    child.stdin.end();
    await closed;
  };
  return Object.assign(release, {
    assertHeld,
    assessActive: async (): Promise<ActiveControllerGeneration> => {
      const response = await request('assess-active');
      if (response.assessment?.schema_version !== 'yylo_controller_generation_admission.v1') {
        throw new Error('generation_reader_response_invalid: missing active assessment');
      }
      return response.assessment;
    },
    recheckActive: async (): Promise<boolean> => (await request('recheck-active')).unchanged === true,
  });
}

async function maintenance<T>(projectDir: string, operation: string, request?: unknown, transactionId?: string): Promise<T> {
  const engine = packagedEngine();
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), 'yylo-generation-request-'));
  try {
    const args = ['-E', '-B', '-X', `pycache_prefix=${path.join(temporary, 'bytecode')}`,
      engine, operation, '--controller', path.resolve(projectDir)];
    if (request !== undefined) {
      const input = path.join(temporary, 'request.json');
      await fs.writeFile(input, JSON.stringify(request), { flag: 'wx', mode: 0o600 });
      args.push('--request', input);
    }
    if (transactionId) args.push('--transaction-id', transactionId);
    try {
      const { stdout } = await execFile('python3', args, { cwd: projectDir, maxBuffer: 64 * 1024 * 1024, timeout: operation === 'retain' ? 180_000 : 120_000 });
      return JSON.parse(stdout) as T;
    } catch (error) {
      const output = (error as { stdout?: string }).stdout;
      if (output) {
        const refusal = JSON.parse(output) as { code?: string; detail?: string };
        throw new Error(`${refusal.code}: ${refusal.detail}`, { cause: error });
      }
      throw error;
    }
  } finally {
    await fs.remove(temporary);
  }
}

/** Read-only active-generation observation. Callers retain their reader lease;
 * this value alone is not engine admission or a cross-process authority proof.
 */
export interface ActiveControllerGeneration {
  schema_version: 'yylo_controller_generation_admission.v1';
  controller: string;
  projection: string;
  runtime_sha256: string;
  executable: string;
  /** Exact-byte authenticated alias observed by this assessment, not a caller proof. */
  equivalent_executable?: string;
  package: { name: string; version: string };
}

export interface GenerationDiagnosticContext {
  source: { status: 'pass' | 'action_required'; sha?: string; remote_verified?: false; reason?: string };
  launchers: { status: 'pass' | 'action_required'; commands?: Record<string, string>[]; reason?: string };
}

export function generationDiagnosticContext(projectDir: string, packageRoot: string): Promise<GenerationDiagnosticContext> {
  return maintenance(projectDir, 'diagnostic-context', { package_root: path.resolve(packageRoot) });
}

export function checkActiveControllerGeneration(projectDir: string): Promise<ActiveControllerGeneration> {
  return maintenance(projectDir, 'active-ready');
}

export function retainInstalledGeneration(projectDir: string, evidence: InstalledGenerationEvidence,
  cache: string, state: string): Promise<{ evidence: InstalledGenerationEvidence }> {
  return maintenance(projectDir, 'retain', { evidence, cache, state });
}

export function discoverInstalledGeneration(packageRoot: string, cache: string): Promise<{ evidence: InstalledGenerationEvidence | null }> {
  return maintenance(packageRoot, 'discover', { root: packageRoot, cache });
}

export function prepareControllerGeneration(projectDir: string, candidate: InstalledGenerationEvidence,
  previous: InstalledGenerationEvidence, repair = false): Promise<ControllerGenerationPlan> {
  return maintenance(projectDir, repair ? 'repair-plan' : 'plan', { candidate, previous });
}

export function applyControllerGeneration(projectDir: string, plan: ControllerGenerationPlan): Promise<ControllerGenerationResult> {
  return maintenance(projectDir, 'apply', plan);
}

export function recoverControllerGeneration(projectDir: string, transactionId: string,
  rollback = false): Promise<ControllerGenerationResult> {
  return maintenance(projectDir, rollback ? 'rollback' : 'resume', undefined, transactionId);
}
