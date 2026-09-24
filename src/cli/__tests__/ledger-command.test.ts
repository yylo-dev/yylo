import { chmod, mkdir, mkdtemp, readFile, realpath, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import {
  LEDGER_VERSION_RANGE,
  LedgerDelegateError,
  discoverLedgerExecutable,
  invokeLedger,
} from '../commands/ledger.js';

const fixtures: string[] = [];
afterEach(async () => Promise.all(fixtures.splice(0).map((item) => rm(item, { recursive: true, force: true }))));

async function fixture(version = `yylo-ledger ${LEDGER_VERSION_RANGE}`) {
  const root = await mkdtemp(path.join(os.tmpdir(), 'yylo-ledger-delegate-'));
  fixtures.push(root);
  const bin = path.join(root, 'bin');
  const record = path.join(root, 'record.json');
  await mkdir(bin);
  const executable = path.join(bin, 'yylo-ledger');
  await writeFile(executable, `#!/usr/bin/env node
const fs=require('node:fs');
if(process.argv[2]==='--version'){console.log(process.env.FAKE_VERSION);process.exit(Number(process.env.FAKE_VERSION_EXIT||0));}
fs.writeFileSync(process.env.FAKE_RECORD,JSON.stringify({argv:process.argv.slice(2),cwd:process.cwd(),stdin:process.env.STDIN_MARKER}));
process.stdout.write('ledger stdout\\n');process.stderr.write('ledger stderr\\n');process.exit(Number(process.env.FAKE_EXIT||0));
`);
  await chmod(executable, 0o755);
  return { root, record, env: { ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH ?? ''}`, FAKE_RECORD: record, FAKE_VERSION: version, STDIN_MARKER: 'preserved' } };
}

describe('ledger delegate', () => {
  it('binds this YYLO release to the supported stable Ledger', () => {
    expect(LEDGER_VERSION_RANGE).toBe('0.4.0');
  });

  it('keeps CLI package, lock and shipped Ledger policy coherent', async () => {
    const metadata = JSON.parse(await readFile(path.resolve('package.json'), 'utf8'));
    const lock = JSON.parse(await readFile(path.resolve('package-lock.json'), 'utf8'));
    const policy = await readFile(path.resolve('src/templates/scripts/juno-toolchain-policy.sh'), 'utf8');
    expect(metadata.version).toBe('0.2.10');
    expect(lock.version).toBe(metadata.version);
    expect(lock.packages[''].version).toBe(metadata.version);
    expect(metadata.yyloLedger.version).toBe(LEDGER_VERSION_RANGE);
    expect(policy).toContain(`YYLO_LEDGER_COMPAT_RANGE='${LEDGER_VERSION_RANGE}'`);
  });

  it.each(['yylo-ledger 0.3.3', 'yylo-ledger 0.4.0rc1', 'yylo-ledger 0.4.1', 'yylo-ledger 0.5.0'])(
    'refuses %s before dispatch without installing a substitute', async (version) => {
      const item = await fixture(version);
      await expect(invokeLedger(['get', 'doc_Ab1Cd2'], { cwd: item.root, env: item.env }))
        .rejects.toMatchObject({ exitCode: 69 });
      await expect(readFile(item.record)).rejects.toMatchObject({ code: 'ENOENT' });
    },
  );

  it('discovers only yylo-ledger and preserves argv, cwd, environment, and exit', async () => {
    const item = await fixture();
    const result = await invokeLedger(['list', '--format', 'json'], { cwd: item.root, env: { ...item.env, FAKE_EXIT: '47' } });
    expect(result).toEqual({ code: 47, signal: null });
    expect(JSON.parse(await readFile(item.record, 'utf8'))).toEqual({ argv: ['list', '--format', 'json'], cwd: await realpath(item.root), stdin: 'preserved' });
  });

  it('fails actionably for missing or incompatible/mixed legacy installations', async () => {
    expect(() => discoverLedgerExecutable({ PATH: '' })).toThrowError(LedgerDelegateError);
    try { discoverLedgerExecutable({ PATH: '' }); } catch (error) {
      expect(error).toMatchObject({ exitCode: 127 });
      expect(String(error)).toContain('Install a compatible yylo-ledger');
      expect(String(error)).not.toContain('juno-kanban');
    }
    const item = await fixture('juno-kanban 2.0.7');
    await expect(invokeLedger(['list'], { cwd: item.root, env: item.env })).rejects.toMatchObject({ exitCode: 69 });
    await expect(readFile(item.record)).rejects.toMatchObject({ code: 'ENOENT' });
  });
});
