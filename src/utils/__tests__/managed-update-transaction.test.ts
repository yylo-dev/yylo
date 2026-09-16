import { afterEach, describe, expect, it, vi } from 'vitest';
import fs from 'fs-extra';
import * as os from 'node:os';
import * as path from 'node:path';
import {
  MANAGED_UPDATE_ROOTS,
  withManagedUpdateRollback,
} from '../managed-update-transaction.js';

describe('managed update transaction', () => {
  let project = '';

  afterEach(async () => {
    vi.restoreAllMocks();
    if (project) await fs.remove(project);
  });

  it('restores exact bytes and modes and removes newly-created destinations after failure', async () => {
    project = await fs.mkdtemp(path.join(os.tmpdir(), 'managed-update-rollback-'));
    const script = path.join(project, '.juno_task/scripts/owner.sh');
    const prompt = path.join(project, '.juno_task/prompts/owner.md');
    await fs.outputFile(script, '#!/bin/sh\necho owner\n');
    await fs.chmod(script, 0o751);
    await fs.outputFile(prompt, 'owner prompt\n');
    await fs.chmod(prompt, 0o640);

    await expect(withManagedUpdateRollback(project, async () => {
      await fs.writeFile(script, 'replacement\n');
      await fs.chmod(script, 0o755);
      await fs.remove(prompt);
      await fs.outputFile(path.join(project, '.venv_juno/bin/python'), 'partial venv\n');
      await fs.outputFile(
        path.join(project, '.juno_task/managed-conflicts/new/file.backup'),
        'partial backup\n',
      );
      throw new Error('injected later-phase failure');
    })).rejects.toThrow('injected later-phase failure');

    expect(await fs.readFile(script, 'utf8')).toBe('#!/bin/sh\necho owner\n');
    expect((await fs.stat(script)).mode & 0o777).toBe(0o751);
    expect(await fs.readFile(prompt, 'utf8')).toBe('owner prompt\n');
    expect((await fs.stat(prompt)).mode & 0o777).toBe(0o640);
    expect(await fs.pathExists(path.join(project, '.venv_juno'))).toBe(false);
    expect(await fs.pathExists(path.join(project, '.juno_task/managed-conflicts'))).toBe(false);
  });

  it('restores every update-owned root while preserving unrelated dirty bytes', async () => {
    project = await fs.mkdtemp(path.join(os.tmpdir(), 'managed-update-all-roots-'));
    const fileRoots = new Set([
      '.juno_task/config.json', '.juno_task/managed-assets.json',
      '.juno_task/.requirements-cache', 'scripts/git-flow.sh', 'AGENTS.md', 'CLAUDE.md',
    ]);
    for (const [index, relative] of MANAGED_UPDATE_ROOTS.entries()) {
      const destination = path.join(project, relative);
      const file = fileRoots.has(relative) ? destination : path.join(destination, 'owner.bin');
      await fs.outputFile(file, Buffer.from([0, index, 255, 10]));
      await fs.chmod(file, 0o600 + (index % 8));
    }
    const unrelated = path.join(project, '.juno_task/ledger/unrelated.bin');
    const unrelatedBytes = Buffer.from([9, 0, 8, 7]);
    await fs.outputFile(unrelated, unrelatedBytes);

    await expect(withManagedUpdateRollback(project, async () => {
      for (const relative of MANAGED_UPDATE_ROOTS) {
        const destination = path.join(project, relative);
        await fs.remove(destination);
        await fs.outputFile(
          fileRoots.has(relative) ? destination : path.join(destination, 'partial.bin'),
          'partial generation\n',
        );
      }
      throw new Error('interrupted complete managed update');
    })).rejects.toThrow('interrupted complete managed update');

    for (const [index, relative] of MANAGED_UPDATE_ROOTS.entries()) {
      const destination = path.join(project, relative);
      const file = fileRoots.has(relative) ? destination : path.join(destination, 'owner.bin');
      expect(await fs.readFile(file), relative).toEqual(Buffer.from([0, index, 255, 10]));
      expect((await fs.stat(file)).mode & 0o777, relative).toBe(0o600 + (index % 8));
      if (!fileRoots.has(relative)) {
        expect(await fs.pathExists(path.join(destination, 'partial.bin')), relative).toBe(false);
      }
    }
    expect(await fs.readFile(unrelated)).toEqual(unrelatedBytes);
  });

  it('snapshots and restores an owned dangling symbolic link exactly', async () => {
    project = await fs.mkdtemp(path.join(os.tmpdir(), 'managed-update-link-'));
    const ownedLink = path.join(project, '.juno_task/prompts');
    await fs.ensureDir(path.dirname(ownedLink));
    await fs.symlink('missing-owner-target', ownedLink);

    await expect(withManagedUpdateRollback(project, async () => {
      await fs.remove(ownedLink);
      await fs.outputFile(path.join(ownedLink, 'settings.json'), 'partial settings\n');
      throw new Error('later phase failed');
    })).rejects.toThrow('later phase failed');

    expect((await fs.lstat(ownedLink)).isSymbolicLink()).toBe(true);
    expect(await fs.readlink(ownedLink)).toBe('missing-owner-target');
  });

  it('never rolls back independent skill writes, active leases, hydration or task bytes', async () => {
    project = await fs.mkdtemp(path.join(os.tmpdir(), 'managed-update-independent-'));
    const independent = [
      '.agents/skills/ralph-loop-yylo/SKILL.md',
      '.claude/skills/ralph-loop-yylo/references/implement.md',
      '.pi/skills/ralph-loop-yylo/SKILL.md',
      '.juno_task/runtime/leases/active.json',
      '.juno_task/runtime/task-hydration/active.json',
      'task/dirty-source.ts',
    ];
    for (const relative of independent) await fs.outputFile(path.join(project, relative), 'before');
    await expect(withManagedUpdateRollback(project, async () => {
      await fs.outputFile(path.join(project, 'AGENTS.md'), 'partial instruction');
      // Model independent owners progressing after the CLI snapshot was taken.
      for (const relative of independent) await fs.writeFile(path.join(project, relative), 'owner progress');
      throw new Error('interrupted CLI refresh');
    })).rejects.toThrow('interrupted CLI refresh');
    for (const relative of independent) {
      expect(await fs.readFile(path.join(project, relative), 'utf8')).toBe('owner progress');
    }
    expect(await fs.pathExists(path.join(project, 'AGENTS.md'))).toBe(false);
  });

  it('reports both the primary update error and a snapshot cleanup error', async () => {
    project = await fs.mkdtemp(path.join(os.tmpdir(), 'managed-update-errors-'));
    await fs.outputFile(path.join(project, '.juno_task/scripts/owner.sh'), 'owner\n');
    const originalRemove = fs.remove.bind(fs);
    let failedCleanup = '';
    vi.spyOn(fs, 'remove').mockImplementation(async (destination: string) => {
      if (destination.includes(`${path.sep}juno-managed-update-`)) {
        failedCleanup = destination;
        throw new Error('injected snapshot cleanup failure');
      }
      await originalRemove(destination);
    });

    let caught: unknown;
    try {
      await withManagedUpdateRollback(project, async () => {
        throw new Error('primary update failure');
      });
    } catch (error) {
      caught = error;
    }
    vi.restoreAllMocks();
    if (failedCleanup) await originalRemove(failedCleanup);

    expect(caught).toBeInstanceOf(AggregateError);
    const aggregate = caught as AggregateError;
    expect(aggregate.errors.map((error) => (error as Error).message)).toEqual([
      'primary update failure',
      'injected snapshot cleanup failure',
    ]);
    expect((aggregate.cause as Error).message).toBe('primary update failure');
  });
});
