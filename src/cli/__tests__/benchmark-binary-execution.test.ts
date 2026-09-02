import { spawn } from 'node:child_process';
import { chmod, mkdir, mkdtemp, readFile, realpath, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { execa } from 'execa';
import { afterEach, beforeAll, describe, expect, it } from 'vitest';

const projectRoot = path.resolve(__dirname, '../../..');
const wrapper = path.join(projectRoot, 'dist/bin/yylo.sh');
const fixtures: string[] = [];

async function makeFixture(version = 'yylo-benchmark 0.1.0-rc.9'): Promise<{
  root: string; env: NodeJS.ProcessEnv; record: string;
}> {
  const root = await mkdtemp(path.join(os.tmpdir(), 'yylo-benchmark-built-'));
  fixtures.push(root);
  const bin = path.join(root, 'bin');
  const record = path.join(root, 'record.json');
  await mkdir(bin);
  const executable = path.join(bin, 'yylo-benchmark');
  await writeFile(executable, `#!/usr/bin/env node
const fs = require('node:fs');
if (process.argv[2] === '--version') { console.log(process.env.FAKE_VERSION); process.exit(0); }
fs.writeFileSync(process.env.FAKE_RECORD, JSON.stringify({argv:process.argv.slice(2),cwd:process.cwd(),marker:process.env.FIDELITY_MARKER}));
if (process.env.FAKE_SIGNAL) process.kill(process.pid, process.env.FAKE_SIGNAL);
if (process.env.FAKE_WAIT) setInterval(() => {}, 1000);
else {
  process.stdout.write('canonical stdout\\n'); process.stderr.write('canonical stderr\\n');
  process.exit(Number(process.env.FAKE_EXIT || 0));
}
`);
  await chmod(executable, 0o755);
  return {
    root,
    record,
    env: {
      ...process.env,
      PATH: `${bin}${path.delimiter}${process.env.PATH ?? ''}`,
      FAKE_VERSION: version,
      FAKE_RECORD: record,
      FIDELITY_MARKER: 'pass-through',
      CI: '',
    },
  };
}

beforeAll(async () => {
  await chmod(wrapper, 0o755);
});

afterEach(async () => {
  await Promise.all(fixtures.splice(0).map((directory) => rm(directory, { recursive: true, force: true })));
});

describe('built yy/yylo benchmark delegate', () => {
  it.each(['yy', 'yylo'])('preserves the %s launch surface and standalone help tail', async (surface) => {
    const fixture = await makeFixture();
    const launcher = path.join(fixture.root, surface);
    await import('node:fs/promises').then(async ({ symlink }) => {
      await symlink(wrapper, path.join(fixture.root, 'yy'));
      await symlink(wrapper, path.join(fixture.root, 'yylo'));
    });
    const result = await execa(launcher, ['benchmark', '--help'], {
      cwd: fixture.root,
      env: { ...fixture.env, PATH: `${fixture.root}${path.delimiter}${fixture.env.PATH ?? ''}` },
      reject: false,
    });
    expect(result.exitCode).toBe(0);
    expect(JSON.parse(await readFile(fixture.record, 'utf8')).argv).toEqual(['--help']);
  });

  it('preserves args including dry-run, stdio, cwd, environment, and success status', async () => {
    const fixture = await makeFixture();
    const args = ['benchmark', 'plan', '--task', 'T1', '--models', ':mini,:sol', '--dry-run'];
    const result = await execa(wrapper, args, { cwd: fixture.root, env: fixture.env, reject: false });
    const observed = JSON.parse(await readFile(fixture.record, 'utf8'));
    expect(result.exitCode).toBe(0);
    expect(result.stdout).toBe('canonical stdout');
    expect(result.stderr).toBe('canonical stderr');
    expect(observed).toEqual({ argv: args.slice(1), cwd: await realpath(fixture.root), marker: 'pass-through' });
  });

  it('forwards canonical help and option delimiters instead of consuming them', async () => {
    const helpFixture = await makeFixture();
    const help = await execa(wrapper, ['benchmark', '--help'], {
      cwd: helpFixture.root, env: helpFixture.env, reject: false,
    });
    expect(help.exitCode).toBe(0);
    expect(JSON.parse(await readFile(helpFixture.record, 'utf8')).argv).toEqual(['--help']);

    const nestedHelpFixture = await makeFixture();
    const nestedHelp = await execa(wrapper, ['benchmark', 'run', '--help'], {
      cwd: nestedHelpFixture.root, env: nestedHelpFixture.env, reject: false,
    });
    expect(nestedHelp.exitCode).toBe(0);
    expect(JSON.parse(await readFile(nestedHelpFixture.record, 'utf8')).argv).toEqual(['run', '--help']);

    const delimiterFixture = await makeFixture();
    const delimiter = await execa(wrapper, ['benchmark', '--', '--help'], {
      cwd: delimiterFixture.root, env: delimiterFixture.env, reject: false,
    });
    expect(delimiter.exitCode).toBe(0);
    expect(JSON.parse(await readFile(delimiterFixture.record, 'utf8')).argv).toEqual(['--', '--help']);
  });

  it('preserves a nonzero canonical exit status', async () => {
    const fixture = await makeFixture();
    const result = await execa(wrapper, ['benchmark', 'run'], {
      cwd: fixture.root, env: { ...fixture.env, FAKE_EXIT: '42' }, reject: false,
    });
    expect(result.exitCode).toBe(42);
  });

  it('fails closed when the executable is missing or incompatible', async () => {
    const missingRoot = await mkdtemp(path.join(os.tmpdir(), 'yylo-benchmark-missing-'));
    fixtures.push(missingRoot);
    const missing = await execa(process.execPath, [path.join(projectRoot, 'dist/bin/cli.mjs'), 'benchmark', 'plan'], {
      cwd: missingRoot, env: { ...process.env, PATH: missingRoot }, reject: false,
    });
    expect(missing.exitCode).toBe(127);
    expect(missing.stderr).toContain('independently installed');

    const incompatible = await makeFixture('yylo-benchmark 0.1.1');
    const rejected = await execa(wrapper, ['benchmark', 'plan'], {
      cwd: incompatible.root, env: incompatible.env, reject: false,
    });
    expect(rejected.exitCode).toBe(69);
    expect(rejected.stderr).toContain('incompatible YYLO Benchmark version');
  });

  it('mirrors canonical signal termination', async () => {
    const fixture = await makeFixture();
    const outcome = await new Promise<{ code: number | null; signal: NodeJS.Signals | null }>((resolve, reject) => {
      const child = spawn(wrapper, ['benchmark', 'run'], {
        cwd: fixture.root,
        env: { ...fixture.env, FAKE_SIGNAL: 'SIGTERM' },
        stdio: 'ignore',
      });
      child.once('error', reject);
      child.once('exit', (code, signal) => resolve({ code, signal }));
    });
    expect(outcome).toEqual({ code: null, signal: 'SIGTERM' });
  });

  it('forwards a caller signal to the canonical process and mirrors termination', async () => {
    const fixture = await makeFixture();
    const child = spawn(wrapper, ['benchmark', 'run'], {
      cwd: fixture.root,
      env: { ...fixture.env, FAKE_WAIT: '1' },
      stdio: 'ignore',
    });
    const outcome = new Promise<{ code: number | null; signal: NodeJS.Signals | null }>((resolve, reject) => {
      child.once('error', reject);
      child.once('exit', (code, signal) => resolve({ code, signal }));
    });
    try {
      for (let attempt = 0; attempt < 100; attempt += 1) {
        try {
          await readFile(fixture.record, 'utf8');
          break;
        } catch {
          await new Promise((resolve) => setTimeout(resolve, 20));
        }
      }
      await readFile(fixture.record, 'utf8');
      child.kill('SIGTERM');
      await expect(outcome).resolves.toEqual({ code: null, signal: 'SIGTERM' });
    } finally {
      if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL');
    }
  });
});
