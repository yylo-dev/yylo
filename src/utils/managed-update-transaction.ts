import { createHash, randomUUID } from 'node:crypto';
import fs from 'fs-extra';
import * as os from 'node:os';
import * as path from 'node:path';
import type { Stats } from 'node:fs';

/** Every project-local root that `scripts update --force` may mutate. */
export const MANAGED_UPDATE_ROOTS = [
  '.juno_task/scripts',
  '.juno_task/prompts',
  '.juno_task/wiki',
  '.juno_task/workflows',
  '.juno_task/config',
  '.juno_task/config.json',
  '.juno_task/managed-assets.json',
  '.juno_task/managed-conflicts',
  '.juno_task/managed-specializations',
  '.juno_task/.requirements-cache',
  '.juno_task/runtime/managed-controller/policy-backups',
  '.venv_juno',
  'scripts/git-flow.sh',
  // Independent skill acquisition owns .agents/.claude/.pi. A failed CLI
  // refresh must not roll back another owner's concurrent skill writes.
  'AGENTS.md',
  'CLAUDE.md',
] as const;

/** Unlike pathExists/access, lstat observes a dangling symbolic link. */
export async function lstatIfPresent(destination: string): Promise<Stats | undefined> {
  try {
    return await fs.lstat(destination);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return undefined;
    throw error;
  }
}

export async function assertPackageSource(
  source: string,
  packageRoot: string,
  expected: 'file' | 'directory',
): Promise<void> {
  if (!(await lstatIfPresent(source))) {
    throw new Error(`Package ${expected} source is missing: ${source}`);
  }
  let resolved: string;
  try {
    resolved = await fs.realpath(source);
  } catch (error) {
    throw new Error(`Package ${expected} source cannot be resolved: ${source}`, { cause: error });
  }
  const resolvedRoot = await fs.realpath(packageRoot);
  if (resolved !== resolvedRoot && !resolved.startsWith(`${resolvedRoot}${path.sep}`)) {
    throw new Error(`Package source escapes templates root: ${source}`);
  }
  const resolvedEntry = await fs.stat(resolved);
  const valid = expected === 'file' ? resolvedEntry.isFile() : resolvedEntry.isDirectory();
  if (!valid) throw new Error(`Package source is not a ${expected}: ${source}`);
}

export async function assertSafeManagedWritePath(
  projectRoot: string,
  destination: string,
): Promise<void> {
  const root = path.resolve(projectRoot);
  const target = path.resolve(destination);
  if (target !== root && !target.startsWith(`${root}${path.sep}`)) {
    throw new Error(`Managed asset path escapes project root: ${destination}`);
  }
  const relative = path.relative(root, target);
  let cursor = root;
  for (const part of relative.split(path.sep).filter(Boolean)) {
    cursor = path.join(cursor, part);
    const entry = await lstatIfPresent(cursor);
    if (entry?.isSymbolicLink()) {
      throw new Error(`Refusing symbolic-link managed path component: ${path.relative(root, cursor)}`);
    }
  }
}

function combinedFailure(
  primary: unknown,
  secondary: readonly unknown[],
  phase: string,
): unknown {
  if (secondary.length === 0) return primary;
  return new AggregateError(
    [primary, ...secondary],
    `${phase} failed and ${secondary.length} rollback/cleanup failure(s) also occurred`,
    { cause: primary },
  );
}

async function cleanupSnapshot(snapshotRoot: string): Promise<unknown[]> {
  try {
    await fs.remove(snapshotRoot);
    return [];
  } catch (error) {
    return [error];
  }
}

async function persistInterruptionEvidence(
  projectRoot: string,
  transactionId: string,
  startedAt: string,
  primaryError: unknown,
  rollbackErrors: readonly unknown[],
): Promise<void> {
  const evidenceRoot = path.join(
    projectRoot, '.juno_task/runtime/managed-controller/update-interruptions',
  );
  await assertSafeManagedWritePath(projectRoot, evidenceRoot);
  await fs.ensureDir(evidenceRoot);
  const evidencePath = path.join(evidenceRoot, `${transactionId}.json`);
  await assertSafeManagedWritePath(projectRoot, evidencePath);
  const failure = primaryError instanceof Error
    ? `${primaryError.name}\0${primaryError.message}`
    : String(primaryError);
  const receipt = {
    schema_version: 'juno_managed_update_interruption.v1',
    transaction_id: transactionId,
    started_at: startedAt,
    recovered_at: new Date().toISOString(),
    owner_pid: process.pid,
    outcome: rollbackErrors.length === 0 ? 'rolled_back' : 'rollback_incomplete',
    rollback_error_count: rollbackErrors.length,
    failure_sha256: createHash('sha256').update(failure).digest('hex'),
    update_roots: [...MANAGED_UPDATE_ROOTS],
  };
  await fs.writeFile(evidencePath, `${JSON.stringify(receipt, null, 2)}\n`, { flag: 'wx' });
}

/**
 * Snapshot all update-owned roots and restore them if any later phase fails.
 * The snapshot lives outside the project, so rollback does not depend on a
 * partially replaced runtime. fs-extra preserves bytes, links, and modes.
 */
export async function withManagedUpdateRollback<T>(
  projectDir: string,
  operation: () => Promise<T>,
): Promise<T> {
  const { withControllerGenerationMutation } = await import('./controller-generation-migration.js');
  return withControllerGenerationMutation(projectDir, () => managedUpdateRollbackUnlocked(projectDir, operation));
}

async function managedUpdateRollbackUnlocked<T>(
  projectDir: string,
  operation: () => Promise<T>,
): Promise<T> {
  const projectRoot = path.resolve(projectDir);
  const transactionId = randomUUID();
  const startedAt = new Date().toISOString();
  const snapshotRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-managed-update-'));
  const present = new Set<string>();

  try {
    for (const relative of MANAGED_UPDATE_ROOTS) {
      const source = path.join(projectRoot, relative);
      if (!(await lstatIfPresent(source))) continue;
      present.add(relative);
      const snapshot = path.join(snapshotRoot, relative);
      await fs.ensureDir(path.dirname(snapshot));
      await fs.copy(source, snapshot, {
        dereference: false,
        preserveTimestamps: true,
        errorOnExist: true,
      });
    }
  } catch (snapshotError) {
    const cleanupErrors = await cleanupSnapshot(snapshotRoot);
    throw combinedFailure(snapshotError, cleanupErrors, 'Managed update snapshot');
  }

  let result: T;
  try {
    result = await operation();
  } catch (primaryError) {
    const rollbackErrors: unknown[] = [];
    for (const relative of [...MANAGED_UPDATE_ROOTS].reverse()) {
      try {
        const destination = path.join(projectRoot, relative);
        await fs.remove(destination);
        if (!present.has(relative)) continue;
        const snapshot = path.join(snapshotRoot, relative);
        await fs.ensureDir(path.dirname(destination));
        await fs.copy(snapshot, destination, {
          dereference: false,
          preserveTimestamps: true,
          errorOnExist: true,
        });
      } catch (rollbackError) {
        rollbackErrors.push(rollbackError);
      }
    }
    rollbackErrors.push(...await cleanupSnapshot(snapshotRoot));
    try {
      await persistInterruptionEvidence(
        projectRoot, transactionId, startedAt, primaryError, rollbackErrors,
      );
    } catch (evidenceError) {
      rollbackErrors.push(evidenceError);
    }
    throw combinedFailure(primaryError, rollbackErrors, 'Managed update');
  }

  const cleanupErrors = await cleanupSnapshot(snapshotRoot);
  if (cleanupErrors.length > 0) {
    throw new AggregateError(cleanupErrors, 'Managed update succeeded but snapshot cleanup failed');
  }
  return result;
}
