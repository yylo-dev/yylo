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

function packagedEngine(): string {
  const directory = path.dirname(fileURLToPath(import.meta.url));
  const engine = [
    path.resolve(directory, 'templates/scripts/controller_generation_migration.py'),
    path.resolve(directory, '../templates/scripts/controller_generation_migration.py'),
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
      const { stdout } = await execFile('python3', args, { cwd: projectDir, maxBuffer: 64 * 1024 * 1024 });
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

export function prepareControllerGeneration(projectDir: string, candidate: InstalledGenerationEvidence,
  previous: InstalledGenerationEvidence): Promise<ControllerGenerationPlan> {
  return maintenance(projectDir, 'plan', { candidate, previous });
}

export function applyControllerGeneration(projectDir: string, plan: ControllerGenerationPlan): Promise<ControllerGenerationResult> {
  return maintenance(projectDir, 'apply', plan);
}

export function recoverControllerGeneration(projectDir: string, transactionId: string,
  rollback = false): Promise<ControllerGenerationResult> {
  return maintenance(projectDir, rollback ? 'rollback' : 'resume', undefined, transactionId);
}
