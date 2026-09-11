import { spawnSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import fs from 'fs-extra';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';

const project = path.resolve(__dirname, '../../..');
const cli = path.join(project, 'dist/bin/cli.mjs');
const wrapper = path.join(project, 'dist/bin/yylo.sh');
let root: string;

beforeAll(async () => {
  root = await fs.mkdtemp(path.join(os.tmpdir(), 'yylo-machine-binary-'));
});

afterAll(async () => {
  await fs.remove(root);
});

function run(executable: string, args: string[]) {
  return spawnSync(executable, args, {
    cwd: root,
    encoding: 'utf8',
    env: { ...process.env, NO_COLOR: '1', CI: '1', YYLO_SESSION_METADATA_DIRECTORY: path.join(root, 'metadata') },
  });
}

describe('package machine channel', () => {
  it.each([
    ['compiled entrypoint', process.execPath, [cli, 'capabilities', '--format', 'json', '--raw']],
    ['package wrapper', wrapper, ['capabilities', '--format', 'json', '--raw']],
  ])('%s emits one directly parseable payload', (_name, executable, args) => {
    const result = run(executable, args);
    expect(result.status, result.stderr).toBe(0);
    expect(JSON.parse(result.stdout)).toMatchObject({
      schema_version: 'yylo.machine-response.v1',
      command: { name: 'capabilities', version: 1 },
      data: { schema_version: 'yylo.machine-capabilities.v1' },
    });
  });

  it('emits a typed envelope for a fatal command-surface refusal', () => {
    const result = run(process.execPath, [cli, 'task', 'not-a-command', '--format', 'json', '--raw']);
    expect(result.status).not.toBe(0);
    expect(result.stderr).toContain('unknown explicit command');
    expect(JSON.parse(result.stdout)).toMatchObject({
      schema_version: 'yylo.machine-response.v1', status: 'refusal',
      error: { exit_code: 2 },
    });
  });

  it('keeps fatal bootstrap diagnostics out of stdout and emits one typed payload', async () => {
    const fixture = path.join(root, 'bootstrap-failure');
    const scripts = path.join(fixture, '.juno_task', 'scripts');
    await fs.ensureDir(scripts);
    await fs.writeFile(path.join(scripts, 'bootstrap.sh'), [
      '#!/usr/bin/env bash',
      'echo "bootstrap path with spaces"',
      'return 7',
      '',
    ].join('\n'));
    const result = spawnSync(wrapper, ['--execution-envelope', '-s', 'pi', '-p', 'not dispatched'], {
      cwd: fixture, encoding: 'utf8',
      env: { ...process.env, NO_COLOR: '1', CI: '1', YYLO_SESSION_METADATA_DIRECTORY: path.join(fixture, 'metadata') },
    });
    expect(result.status).toBe(7);
    expect(result.stderr).toContain('bootstrap path with spaces');
    expect(JSON.parse(result.stdout)).toMatchObject({
      schema_version: 'juno_execution_envelope.v1',
      command: { name: 'managed.run', version: 1 },
      status: 'failure',
      error: { code: 'BOOTSTRAP_FAILED', exit_code: 7 },
    });
  });
});
