import { spawn } from 'node:child_process';
import { chmod, mkdir, mkdtemp, readFile, realpath, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { execa } from 'execa';
import { afterEach, beforeAll, describe, expect, it } from 'vitest';

const projectRoot = path.resolve(__dirname, '../../..');
const wrapper = path.join(projectRoot, 'dist/bin/yylo.sh');
const fixtures: string[] = [];

async function fixture() {
  const root = await mkdtemp(path.join(os.tmpdir(), 'yylo-ledger-built-'));
  fixtures.push(root);
  const bin = path.join(root, 'bin'); const record = path.join(root, 'record.json');
  await mkdir(bin);
  const executable = path.join(bin, 'yylo-ledger');
  await writeFile(executable, `#!/usr/bin/env node
const fs=require('node:fs');
if(process.argv[2]==='--version'){console.log('yylo-ledger 0.3.1');process.exit(0);}
const input=fs.readFileSync(0,'utf8');fs.writeFileSync(process.env.FAKE_RECORD,JSON.stringify({argv:process.argv.slice(2),cwd:process.cwd(),input}));
if(process.env.FAKE_SIGNAL)process.kill(process.pid,process.env.FAKE_SIGNAL);
process.stdout.write('standalone stdout\\n');process.stderr.write('standalone stderr\\n');process.exit(Number(process.env.FAKE_EXIT||0));
`);
  await chmod(executable, 0o755);
  return { root, record, env: { ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH ?? ''}`, FAKE_RECORD: record, CI: '' } };
}

beforeAll(async () => chmod(wrapper, 0o755));
afterEach(async () => Promise.all(fixtures.splice(0).map((item) => rm(item, { recursive: true, force: true }))));

describe('built yy/yylo ledger delegation', () => {
  it.each(['yy', 'yylo'])('preserves %s args, help, stdio, cwd, environment, and exit status', async (surface) => {
    const item = await fixture();
    await import('node:fs/promises').then(async ({ symlink }) => {
      await symlink(wrapper, path.join(item.root, 'yylo'));
      await symlink(wrapper, path.join(item.root, 'yy'));
    });
    const launcher = path.join(item.root, surface);
    const result = await execa(launcher, ['ledger', 'list', '--help'], {
      cwd: item.root,
      env: { ...item.env, PATH: `${item.root}${path.delimiter}${item.env.PATH}`, FAKE_EXIT: '43' },
      input: 'request body',
      reject: false,
    });
    expect(result.exitCode, result.stderr).toBe(43);
    expect(result.stdout).toBe('standalone stdout');
    expect(result.stderr).toBe('standalone stderr');
    expect(JSON.parse(await readFile(item.record, 'utf8'))).toEqual({ argv: ['list', '--help'], cwd: await realpath(item.root), input: 'request body' });
  });

  it('mirrors standalone signal termination', async () => {
    const item = await fixture();
    const outcome = await new Promise<{ code: number | null; signal: NodeJS.Signals | null }>((resolve, reject) => {
      const child = spawn(wrapper, ['ledger', 'list'], { cwd: item.root, env: { ...item.env, FAKE_SIGNAL: 'SIGTERM' }, stdio: 'ignore' });
      child.once('error', reject); child.once('exit', (code, signal) => resolve({ code, signal }));
    });
    expect(outcome).toEqual({ code: null, signal: 'SIGTERM' });
  });
});
