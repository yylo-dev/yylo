import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const cli = path.resolve('dist/bin/cli.mjs');
let base: string;
let root: string;
let env: NodeJS.ProcessEnv;
function git(cwd: string, ...args: string[]) {
  const result = spawnSync('git', args, { cwd, env, encoding: 'utf8' });
  expect(result.status, result.stderr).toBe(0);
  return result.stdout;
}
function yy(args: string[], cwd = root) {
  return spawnSync(process.execPath, [cli, 'init', ...args], { cwd, env, input: '', encoding: 'utf8', timeout: 30000 });
}
function ok(args: string[], cwd = root) {
  const result = yy(args, cwd);
  expect(result.status, result.stdout + result.stderr).toBe(0);
  return result.stdout;
}
function snapshot(directory = root): Record<string, string> {
  const result: Record<string, string> = {};
  function walk(dir: string) {
    for (const name of fs.readdirSync(dir).sort()) {
      const file = path.join(dir, name);
      const stat = fs.lstatSync(file);
      const key = path.relative(directory, file);
      if (stat.isSymbolicLink()) result[key] = `link:${fs.readlinkSync(file)}`;
      else if (stat.isDirectory()) { result[key] = 'directory'; walk(file); }
      else result[key] = fs.readFileSync(file).toString('base64');
    }
  }
  walk(directory);
  return result;
}
beforeEach(() => {
  base = fs.mkdtempSync(path.join(os.tmpdir(), 'simple-init-cli-'));
  root = path.join(base, 'project'); fs.mkdirSync(root);
  env = { ...process.env, CI: '1', XDG_STATE_HOME: path.join(base, 'state') };
  for (const key of Object.keys(env)) if (/^(JUNO_|YYLO_|GIT_|FORCE_INTERACTIVE)/u.test(key)) delete env[key];
  git(root, 'init', '-q');
  fs.writeFileSync(path.join(root, 'AGENTS.md'), 'Preserve my instructions.');
  fs.writeFileSync(path.join(root, 'notebook.ipynb'), 'dirty notebook');
});
afterEach(() => fs.rmSync(base, { recursive: true, force: true }));

describe('built Simple init with closed stdin', () => {
  it.each([false, true])('initializes immediately, preserves user/Git bytes, repeats as no-op (directory=%s)', (directory) => {
    const before = snapshot();
    const args = ['--mode', 'simple', ...(directory ? ['--directory', root] : [])];
    expect(ok(args, directory ? base : root)).toContain('Initialized Simple workspace: ' + root);
    const after = snapshot();
    for (const [file, bytes] of Object.entries(before)) expect(after[file]).toBe(bytes);
    expect(Object.keys(after).filter((file) => !Object.hasOwn(before, file)).sort()).toEqual([
      '.juno_task', '.juno_task/.gitignore', '.juno_task/config.json', '.juno_task/simple-agent-guidance.md', '.juno_task/simple-init.json',
    ]);
    expect(ok(args, directory ? base : root)).toContain('Already initialized');
    expect(snapshot()).toEqual(after);
  });
  it('dry-run leaves the entire filesystem and Git unchanged', () => {
    const before = snapshot(base);
    const output = ok(['--mode', 'simple', '--dry-run']);
    expect(output).toContain('Preview only; not initialized: ' + root);
    expect(output).toContain('config.json');
    expect(snapshot(base)).toEqual(before);
  });
  it.each(['json', 'ndjson'])('frames %s output without human contamination', (format) => {
    const preview = JSON.parse(ok(['--mode', 'simple', '--dry-run', '--format', format, '--raw']));
    expect(preview).toMatchObject({ schema_version: 'yylo.machine-response.v1', status: 'success', data: { root, outcome: 'ready' } });
    const result = JSON.parse(ok(['--mode', 'simple', '--format', format, '--raw']));
    expect(result.data).toEqual({ outcome: 'initialized', root });
  });
  it('supports saved plans and refuses stale/tampered plans without writes', () => {
    const plan = path.join(base, 'plan.json');
    ok(['--mode', 'simple', '--plan-file', plan]);
    expect(fs.existsSync(path.join(root, '.juno_task'))).toBe(false);
    const original = fs.readFileSync(plan, 'utf8');
    const tampered = JSON.parse(original); tampered.files['config.json'] = 'tampered';
    fs.writeFileSync(plan, JSON.stringify(tampered));
    const before = snapshot();
    expect(yy(['--mode', 'simple', '--apply-plan', plan]).status).not.toBe(0);
    expect(snapshot()).toEqual(before);
    fs.writeFileSync(plan, original);
    fs.appendFileSync(path.join(root, 'AGENTS.md'), ' changed');
    expect(yy(['--mode', 'simple', '--apply-plan', plan]).status).not.toBe(0);
    expect(fs.existsSync(path.join(root, '.juno_task'))).toBe(false);
    const fresh = path.join(base, 'fresh.json');
    ok(['--mode', 'simple', '--plan-file', fresh]);
    ok(['--mode', 'simple', '--apply-plan', fresh]);
  });
  it.each([
    ['--apply-plan', '/unused'], ['--plan-file', '/unused'], ['--from-advanced', '/unused'], ['--interactive'],
  ])('rejects dry-run conflicts before writes: %j', (flags) => {
    const before = snapshot(base);
    expect(yy(['--mode', 'simple', '--dry-run', ...flags]).status).not.toBe(0);
    expect(snapshot(base)).toEqual(before);
  });
  it.each(['advanced', 'omitted'])('rejects dry-run outside explicit Simple (%s)', (mode) => {
    const before = snapshot(base);
    expect(yy(['--dry-run', ...(mode === 'advanced' ? ['--mode', mode] : [])]).status).not.toBe(0);
    expect(snapshot(base)).toEqual(before);
  });
  it('preserves interrupted reservations and customized configuration', () => {
    const reservation = path.join(root, '.yylo-simple-init'); fs.mkdirSync(reservation);
    const before = snapshot();
    expect(yy(['--mode', 'simple']).status).not.toBe(0);
    expect(snapshot()).toEqual(before);
    fs.rmdirSync(reservation); // Test-owned fixture only.
    ok(['--mode', 'simple']);
    fs.appendFileSync(path.join(root, '.juno_task/config.json'), '\n');
    const customized = snapshot();
    expect(yy(['--mode', 'simple']).status).not.toBe(0);
    expect(snapshot()).toEqual(customized);
  });
  it('refuses missing Git and non-root directories without writes', () => {
    const before = snapshot(base);
    expect(yy(['--mode', 'simple'], base).status).not.toBe(0);
    expect(snapshot(base)).toEqual(before);
    const nested = path.join(root, 'notes'); fs.mkdirSync(nested);
    const withNested = snapshot();
    expect(yy(['--mode', 'simple'], nested).status).not.toBe(0);
    expect(snapshot()).toEqual(withNested);
  });
  it.each(['symlink', 'ignore', 'collision', 'registration', 'linked'])('refuses %s without changing bytes', (kind) => {
    let cwd = root;
    if (kind === 'symlink') fs.symlinkSync(path.join(root, 'notebook.ipynb'), path.join(root, 'CLAUDE.md'));
    if (kind === 'ignore') fs.writeFileSync(path.join(root, '.gitignore'), '.juno_task/\n');
    if (kind === 'collision') { fs.mkdirSync(path.join(root, '.juno_task')); fs.writeFileSync(path.join(root, '.juno_task/custom'), 'preserve'); }
    if (kind === 'registration') git(root, 'config', 'juno.controller.root', base);
    if (kind === 'linked') {
      git(root, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test', 'commit', '--allow-empty', '-qm', 'fixture');
      cwd = path.join(base, 'linked'); git(root, 'worktree', 'add', '--detach', cwd);
    }
    const before = snapshot(base);
    expect(yy(['--mode', 'simple'], cwd).status).not.toBe(0);
    expect(snapshot(base)).toEqual(before);
  });
  it('documents immediate writes and optional preview', () => {
    const help = ok(['--help']);
    expect(help).toContain('Plain fresh Simple init now writes files');
    expect(help).toContain('--dry-run');
    expect(help).toContain('Conversion remains preview-only');
  });
});
