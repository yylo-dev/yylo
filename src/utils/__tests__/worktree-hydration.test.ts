import fs from 'fs-extra';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

const helper = path.resolve(process.cwd(), 'src/templates/scripts/worktree_hydration.py');

describe('worktree hydration helper', () => {
  let root: string;

  beforeEach(async () => {
    root = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-worktree-hydration-'));
    spawnSync('git', ['init', '-q', root], { encoding: 'utf8' });
    await fs.writeFile(path.join(root, '.gitignore'), '.env\n');
    spawnSync('git', ['-C', root, 'add', '.gitignore'], { encoding: 'utf8' });
    spawnSync('git', ['-C', root, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'base'], { encoding: 'utf8' });
  });

  afterEach(async () => fs.remove(root));

  it('copies only an explicit absolute env source without echo and enforces mode 0600', async () => {
    const source = path.join(root, '..', `${path.basename(root)}.approved-env`);
    await fs.writeFile(source, 'SECRET_MARKER=do-not-print\n');
    const result = spawnSync('python3', [helper, '--project-root', root, 'copy-env', '--source', source, '--destination', '.env'], { encoding: 'utf8' });
    expect(result.status, result.stderr).toBe(0);
    expect(result.stdout + result.stderr).not.toContain('SECRET_MARKER');
    expect((await fs.stat(path.join(root, '.env'))).mode & 0o777).toBe(0o600);
    expect(await fs.readFile(path.join(root, '.env'), 'utf8')).toBe('SECRET_MARKER=do-not-print\n');
    await fs.remove(source);
  });

  it('binds installed Node dependencies to the exact package-lock hash', async () => {
    const pkg = path.join(root, 'pkg');
    await fs.ensureDir(pkg);
    await fs.writeJson(path.join(pkg, 'package.json'), { name: 'fixture', version: '1.0.0' });
    expect(spawnSync('npm', ['install', '--package-lock-only', '--ignore-scripts'], { cwd: pkg }).status).toBe(0);
    const hydrate = spawnSync('python3', [helper, '--project-root', root, 'hydrate-node', '--cwd', 'pkg'], { encoding: 'utf8' });
    expect(hydrate.status, hydrate.stderr).toBe(0);
    const verify = () => spawnSync('python3', [helper, '--project-root', root, 'verify-node-lock', '--cwd', 'pkg'], { encoding: 'utf8' });
    expect(verify().status).toBe(0);
    await fs.writeFile(path.join(pkg, 'node_modules/.yylo-package-lock.sha256'), 'stale\n');
    expect(verify().status).toBe(2);
    expect(verify().stderr).toContain('missing or stale');
  });

  it('refuses borrowed dependency directories before installation or probing', async () => {
    const pkg = path.join(root, 'pkg');
    const borrowed = path.join(root, 'other-checkout-dependencies');
    await fs.ensureDir(pkg);
    await fs.ensureDir(borrowed);
    await fs.writeJson(path.join(pkg, 'package-lock.json'), { lockfileVersion: 3, packages: {} });
    await fs.symlink(borrowed, path.join(pkg, 'node_modules'));
    for (const action of ['hydrate-node', 'verify-node-lock']) {
      const result = spawnSync('python3', [helper, '--project-root', root, action, '--cwd', 'pkg'], { encoding: 'utf8' });
      expect(result.status, result.stderr).toBe(2);
      expect(result.stderr).toContain('symbolic');
      expect((await fs.lstat(path.join(pkg, 'node_modules'))).isSymbolicLink()).toBe(true);
    }
  });

  it('selects admitted roots and finish prerequisites with frozen fail-closed evidence', () => {
    const result = spawnSync('python3', [
      path.resolve(process.cwd(), 'src/templates/scripts/tests/test_scoped_hydration.py'),
    ], { encoding: 'utf8', timeout: 120_000 });
    expect(result.status, result.stdout + result.stderr).toBe(0);
    expect(result.stdout).toContain('"steps_avoided": 1');
    expect(result.stderr).toContain('Ran 12 tests');
  }, 130_000);

  it('rejects tracked hydration drift', async () => {
    await fs.writeFile(path.join(root, '.gitignore'), '.env\nchanged\n');
    const result = spawnSync('python3', [helper, '--project-root', root, 'verify-clean'], { encoding: 'utf8' });
    expect(result.status).toBe(2);
    expect(result.stderr).toContain('tracked or unignored worktree drift');
  });
});
