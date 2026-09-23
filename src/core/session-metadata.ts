import { createHash, randomUUID } from 'node:crypto';
import * as childProcess from 'node:child_process';
import * as nodeFs from 'node:fs/promises';
import * as os from 'node:os';
import * as path from 'node:path';
import fs from 'fs-extra';

export const SESSION_METADATA_DIRECTORY_ENV = 'YYLO_SESSION_METADATA_DIRECTORY';
/** One lock shared by continuity state, runtime liveness markers, migration, and retention. */
export const SESSION_CONTINUITY_SHARED_LOCK_NAME = 'session_continuity.v2.json';
const PROJECT_STATE_KIND_PATTERN = /^[a-z][a-z0-9_]*$/;
const LOCK_RETRY_MS = 25;
const LOCK_TIMEOUT_MS = 5_000;

function canonicalPath(candidate: string): string {
  const absolute = path.resolve(candidate.trim() || process.cwd());
  try {
    return fs.realpathSync(absolute);
  } catch {
    return absolute;
  }
}

function gitCommonDirectory(workingDirectory: string): string | null {
  try {
    const output = childProcess.execFileSync(
      'git', ['-C', workingDirectory, 'rev-parse', '--path-format=absolute', '--git-common-dir'],
      { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'], timeout: 1_000 },
    ).trim();
    return output ? canonicalPath(output) : null;
  } catch {
    return null;
  }
}

export interface JunoProjectStateLocation {
  directory: string;
  durabilityAnchor: string;
}

/** Resolve a volatile project-state kind using Git-common identity or a stable non-Git path hash. */
export function resolveJunoProjectStateLocation(
  workingDirectory: string,
  stateKind: string,
  env: NodeJS.ProcessEnv = process.env,
): JunoProjectStateLocation {
  if (!PROJECT_STATE_KIND_PATTERN.test(stateKind)) {
    throw new Error(`Invalid YYLO project-state kind: ${stateKind}`);
  }

  const canonicalWorkingDirectory = canonicalPath(workingDirectory);
  const commonDirectory = gitCommonDirectory(canonicalWorkingDirectory);
  if (commonDirectory) {
    return {
      directory: path.join(commonDirectory, 'juno', stateKind),
      durabilityAnchor: commonDirectory,
    };
  }

  const identity = createHash('sha256').update(canonicalWorkingDirectory).digest('hex').slice(0, 16);
  const stateHome = path.resolve(env.XDG_STATE_HOME?.trim() || path.join(os.homedir(), '.local', 'state'));
  return {
    directory: path.join(stateHome, '@yylo/cli', stateKind, identity),
    durabilityAnchor: stateHome,
  };
}

export function getJunoProjectStateDirectory(
  workingDirectory: string,
  stateKind: string,
  env: NodeJS.ProcessEnv = process.env,
): string {
  return resolveJunoProjectStateLocation(workingDirectory, stateKind, env).directory;
}

/** Resolve every volatile session producer to one untracked repository-local state root. */
export function getSessionMetadataDirectory(
  workingDirectory: string,
  env: NodeJS.ProcessEnv = process.env,
): string {
  const override = env[SESSION_METADATA_DIRECTORY_ENV]?.trim();
  if (override) {
    return path.resolve(workingDirectory, override);
  }
  return getJunoProjectStateDirectory(workingDirectory, 'session_metadata', env);
}

function processAlive(pid: number): boolean {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return (error as NodeJS.ErrnoException).code === 'EPERM';
  }
}

async function removeStaleLock(lockDirectory: string): Promise<boolean> {
  try {
    const owner = await fs.readJson(path.join(lockDirectory, 'owner.json')) as { pid?: unknown };
    if (typeof owner.pid === 'number' && processAlive(owner.pid)) return false;
  } catch {
    // An incomplete lock older than the retry interval is safe to reclaim.
    try {
      const stat = await fs.stat(lockDirectory);
      if (Date.now() - stat.mtimeMs < 250) return false;
    } catch {
      return true;
    }
  }
  const quarantine = `${lockDirectory}.stale-${process.pid}-${randomUUID()}`;
  try {
    await fs.rename(lockDirectory, quarantine);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return true;
    return false;
  }
  await fs.remove(quarantine);
  return true;
}

async function ensurePrivateMetadataDirectory(metadataDirectory: string): Promise<void> {
  await fs.ensureDir(metadataDirectory, 0o700);
  await fs.chmod(metadataDirectory, 0o700);
}

export async function withSessionMetadataLock<T>(
  metadataDirectory: string,
  name: string,
  operation: () => Promise<T>,
): Promise<T> {
  await ensurePrivateMetadataDirectory(metadataDirectory);
  const lockDirectory = path.join(metadataDirectory, `${name}.lock`);
  const deadline = Date.now() + LOCK_TIMEOUT_MS;
  const token = randomUUID();

  while (true) {
    try {
      await fs.mkdir(lockDirectory, { mode: 0o700 });
      await fs.writeFile(
        path.join(lockDirectory, 'owner.json'),
        `${JSON.stringify({
          pid: process.pid,
          token,
          started_at: new Date().toISOString(),
        }, null, 2)}\n`,
        { encoding: 'utf8', mode: 0o600 },
      );
      break;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error;
      if (await removeStaleLock(lockDirectory)) continue;
      if (Date.now() >= deadline) {
        throw new Error(`Timed out waiting for session metadata lock '${name}' at ${lockDirectory}.`);
      }
      await new Promise((resolve) => setTimeout(resolve, LOCK_RETRY_MS));
    }
  }

  try {
    return await operation();
  } finally {
    try {
      const owner = await fs.readJson(path.join(lockDirectory, 'owner.json')) as { token?: unknown };
      if (owner.token === token) await fs.remove(lockDirectory);
    } catch {
      // Never remove a lock whose ownership cannot be proven.
    }
  }
}

export async function writeSessionMetadataFileAtomic(filePath: string, content: string): Promise<void> {
  await ensurePrivateMetadataDirectory(path.dirname(filePath));
  const temporaryPath = `${filePath}.tmp-${process.pid}-${randomUUID()}`;
  const handle = await nodeFs.open(temporaryPath, 'wx', 0o600);
  try {
    await handle.writeFile(content, 'utf8');
    await handle.sync();
  } finally {
    await handle.close();
  }
  await fs.chmod(temporaryPath, 0o600);
  await fs.rename(temporaryPath, filePath);
  // A rename over legacy permissive metadata must not preserve its old mode.
  await fs.chmod(filePath, 0o600);
}
