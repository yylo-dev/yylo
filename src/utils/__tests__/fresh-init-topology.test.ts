import { execFileSync } from 'node:child_process';
import fs from 'fs-extra';
import os from 'node:os';
import path from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import {
  establishFreshInitTopology,
  probeFreshInitTopology,
} from '../fresh-init-topology.js';

const temporary: string[] = [];

function git(root: string, ...args: string[]): string {
  return execFileSync('git', ['-C', root, ...args], { encoding: 'utf8' }).trim();
}

async function repository(): Promise<string> {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'yylo-fresh-init-probe-'));
  temporary.push(root);
  git(root, 'init', '-b', 'main');
  return root;
}

afterEach(async () => {
  await Promise.all(temporary.splice(0).map((root) => fs.remove(root)));
});

describe('fresh init topology admission', () => {
  it('admits only an exact empty unborn Git worktree', async () => {
    const root = await repository();
    expect(probeFreshInitTopology(root)).toMatchObject({
      eligible: true,
      repositoryRoot: root,
      targetRef: 'refs/heads/main',
    });
    await fs.ensureDir(path.join(root, 'nested'));
    expect(probeFreshInitTopology(path.join(root, 'nested')).eligible).toBe(false);
  });

  it('preserves unrelated dirty bytes and existing repository identity', async () => {
    const dirty = await repository();
    const unrelated = path.join(dirty, 'owner-notes.txt');
    await fs.writeFile(unrelated, 'keep me\n');
    const dirtyProbe = probeFreshInitTopology(dirty);
    expect(dirtyProbe.eligible).toBe(false);
    await expect(establishFreshInitTopology(dirtyProbe, '1.2.3')).resolves.toEqual({
      configured: false,
      integrationOwner: null,
    });
    expect(await fs.readFile(unrelated, 'utf8')).toBe('keep me\n');
    expect(git(dirty, 'status', '--porcelain')).toContain('owner-notes.txt');

    const committed = await repository();
    await fs.writeFile(path.join(committed, 'README.md'), 'existing\n');
    git(committed, 'add', 'README.md');
    execFileSync('git', ['-C', committed, '-c', 'user.name=Test', '-c',
      'user.email=test@example.invalid', 'commit', '-m', 'existing'], { stdio: 'ignore' });
    expect(probeFreshInitTopology(committed).eligible).toBe(false);
    expect(git(committed, 'branch', '--show-current')).toBe('main');
    expect(git(committed, 'worktree', 'list', '--porcelain').match(/^worktree /g)).toHaveLength(1);
  });
});
