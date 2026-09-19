import { spawnSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import fs from 'fs-extra';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';

const project = path.resolve(__dirname, '../../..');
let root: string;

beforeAll(async () => {
  root = await fs.mkdtemp(path.join(os.tmpdir(), 'yylo-ledger-machine-'));
  const scripts = path.join(root, '.juno_task/scripts');
  const bin = path.join(root, '.venv_juno/bin');
  await fs.ensureDir(scripts);
  await fs.ensureDir(bin);
  for (const file of ['kanban.sh', 'controller_resolver.py', 'juno-toolchain-policy.sh']) {
    await fs.copy(path.join(project, 'src/templates/scripts', file), path.join(scripts, file));
  }
  await fs.writeJson(path.join(root, '.juno_task/config.json'), {});
  for (const args of [
    ['init', '-b', 'controller'],
    ['config', 'juno.controller.path', root],
    ['config', 'juno.controller.branch', 'controller'],
    ['config', 'juno.workspace.role', 'controller'],
    ['config', 'juno.controller.runtimeExecutable', path.join(project, 'dist/bin/cli.mjs')],
  ]) {
    const result = spawnSync('git', args, { cwd: root, encoding: 'utf8' });
    expect(result.status, result.stderr).toBe(0);
  }
  await fs.writeFile(path.join(bin, 'activate'), `export VIRTUAL_ENV='${path.dirname(bin)}'\nexport PATH='${bin}':$PATH\n`);
  // Model Ledger 0.3.1's separate root format and native record_format
  // destinations, including its JSON page object and NDJSON page trailer.
  await fs.writeFile(path.join(bin, 'yylo-ledger'), `#!/usr/bin/env python3
import argparse, json, os, sys
if sys.argv[1:] == ['--version']:
    print('yylo-ledger 0.3.2'); sys.exit(0)
with open(os.path.join(os.getcwd(), 'argv.json'), 'w') as f: json.dump(sys.argv[1:], f)
p = argparse.ArgumentParser()
p.add_argument('--config'); p.add_argument('--format', '-f'); p.add_argument('--raw', action='store_true')
g = p.add_subparsers(dest='group', required=True)
for name in ['record', 'task', 'wiki', 'workflow', 'artifact']:
    a = g.add_parser(name).add_subparsers(dest='action', required=True).add_parser('search')
    a.add_argument('--format', '-f', dest='record_format', default='ndjson')
    a.add_argument('--text', default=''); a.add_argument('--cursor'); a.add_argument('--limit', type=int, default=2)
a = p.parse_args()
if a.text == 'error':
    print('native refusal diagnostic', file=sys.stderr); sys.exit(2)
if a.text == 'malformed':
    print('not JSON'); sys.exit(0)
count = int(a.text or '3'); start = int(a.cursor or '0'); end = min(count, start + a.limit)
records = [{'id': str(i)} for i in range(start, end)]
page = {'next_cursor': str(end) if end < count else None}
if a.record_format == 'json': print(json.dumps({'records': records, **page}))
else:
    for item in records: print(json.dumps(item))
    print(json.dumps({'type': 'page', **page}))
`);
  await fs.chmod(path.join(bin, 'yylo-ledger'), 0o755);
});
afterAll(async () => fs.remove(root));

function run(args: string[], entry = 'compiled') {
  const executable = entry === 'compiled' ? process.execPath : path.join(project, 'dist/bin/yylo.sh');
  return spawnSync(executable, [...(entry === 'compiled' ? [path.join(project, 'dist/bin/cli.mjs')] : []), 'ledger', ...args], {
    cwd: root, encoding: 'utf8',
    env: { ...process.env, NO_COLOR: '1', CI: '1', YYLO_SESSION_METADATA_DIRECTORY: path.join(root, 'metadata') },
  });
}

describe('managed Ledger native format boundary', () => {
  it.each(['compiled', 'wrapper'])('%s preserves zero, one, multiple records and every cursor page', (entry) => {
    for (const format of ['json', 'ndjson']) {
      for (const count of [0, 1, 5]) {
        const ids: string[] = [];
        let cursor: string | null = null;
        do {
          const result = run(['record', 'search', '--text', String(count), '--limit', '2', '--format', format,
            ...(cursor ? ['--cursor', cursor] : [])], entry);
          expect(result.status, result.stderr).toBe(0);
          const envelope = JSON.parse(result.stdout);
          expect(envelope).toMatchObject({ schema_version: 'yylo.machine-response.v1', status: 'success', error: null });
          if (format === 'json') {
            ids.push(...envelope.data.records.map((item: { id: string }) => item.id));
            cursor = envelope.data.next_cursor;
          } else {
            expect(result.stdout.trim().split('\n')).toHaveLength(1);
            // A page-only NDJSON response is one document under the existing v1 contract.
            const stream = Array.isArray(envelope.data) ? envelope.data : [envelope.data];
            ids.push(...stream.filter((item: { type?: string }) => item.type !== 'page').map((item: { id: string }) => item.id));
            cursor = stream.at(-1).next_cursor;
          }
        } while (cursor);
        expect(ids).toEqual(Array.from({ length: count }, (_, index) => String(index)));
      }
    }
  }, 60_000);

  it.each(['record', 'task', 'wiki', 'workflow', 'artifact'])('keeps %s format spellings at native scope', async (group) => {
    for (const formatArgs of [['--format=json'], ['--format', 'json']]) {
      const result = run([group, 'search', '--text', '3', ...formatArgs]);
      expect(result.status, result.stderr).toBe(0);
      expect(JSON.parse(result.stdout).data.records).toHaveLength(2);
      const args = await fs.readJson(path.join(root, 'argv.json'));
      expect(args.indexOf(group)).toBeLessThan(args.findIndex((arg: string) => arg === '--format' || arg === '--format=json'));
    }
  });

  it.each(['json', 'ndjson'])('preserves native failures and rejects corrupt %s bytes', (format) => {
    const refusal = run(['record', 'search', '--text', 'error', '--format', format]);
    expect(refusal.status).toBe(2);
    expect(refusal.stderr).toContain('native refusal diagnostic');
    expect(JSON.parse(refusal.stdout)).toMatchObject({ status: 'refusal', error: { exit_code: 2, message: 'native refusal diagnostic' } });
    const corrupt = run(['record', 'search', '--text', 'malformed', '--format', format]);
    expect(corrupt.status).toBe(70);
    expect(JSON.parse(corrupt.stdout)).toMatchObject({ status: 'error', error: { code: 'INVALID_CHILD_PAYLOAD' } });
  });
});
