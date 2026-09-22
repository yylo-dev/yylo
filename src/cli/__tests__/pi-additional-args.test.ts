import { spawnSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import fs from 'fs-extra';
import { afterEach, describe, expect, it } from 'vitest';

const roots: string[] = [];
afterEach(async () => { await Promise.all(roots.splice(0).map((root) => fs.remove(root))); });

describe('Pi existing additional-args transport through execution-envelope', () => {
  it.each(['success', 'unsupported', 'malformed'])('retains literal argv and session destination: %s', async (mode) => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'yylo-pi-argv-')); roots.push(root);
    const cwd = path.join(root, 'project'); const home = path.join(root, 'home'); const bin = path.join(root, 'bin');
    const sessions = path.join(root, 'private sessions $literal');
    await Promise.all([fs.ensureDir(path.join(cwd, '.juno_task')), fs.ensureDir(bin), fs.ensureDir(home)]);
    expect(spawnSync('git', ['init', '--quiet'], { cwd }).status).toBe(0);
    await fs.writeJson(path.join(cwd, '.juno_task/config.json'), { controllerWorkspace: { mode: 'simple', version: 1 } });
    const ledgerVersion = (await fs.readJson('package.json')).yyloLedger.version;
    await fs.writeFile(path.join(bin, 'yylo-ledger'), `#!/bin/sh\n[ "$1" = "--version" ] && { echo "yylo-ledger ${ledgerVersion}"; exit 0; }\nexit 98\n`, { mode: 0o755 });
    const services = path.join(home, '.yylo/services');
    await fs.copy(path.resolve('src/templates/services'), services);
    await fs.writeFile(path.join(services, '.version'), `${(await fs.readJson('package.json')).version}\n`);
    await fs.writeFile(path.join(bin, 'pi'), `#!/usr/bin/env python3
import argparse, json, pathlib, sys
pathlib.Path(${JSON.stringify(path.join(root, 'child-invoked'))}).write_text('yes')
p=argparse.ArgumentParser()
p.add_argument('--mode'); p.add_argument('--provider'); p.add_argument('--model'); p.add_argument('--thinking')
p.add_argument('-p', action='store_true'); p.add_argument('--session-dir', required=True)
p.add_argument('--no-prompt-templates', action='store_true')
a=p.parse_args()
prompt=sys.stdin.read()
root=pathlib.Path(a.session_dir); root.mkdir(parents=True, exist_ok=True)
(root/'session.json').write_text(json.dumps({'argv':sys.argv[1:],'prompt':prompt,'cwd':str(pathlib.Path.cwd())}))
message={'role':'assistant','content':[{'type':'text','text':'fixture complete'}], 'provider':a.provider,'model':a.model,'stopReason':'stop','usage':{'input':1,'output':1,'cost':{'total':0}}}
print(json.dumps({'type':'session','id':'fixture-session'}), flush=True)
print(json.dumps({'type':'message_end','message':message}), flush=True)
print(json.dumps({'type':'agent_end','messages':[message]}), flush=True)
`, { mode: 0o755 });
    const prompt = 'Literal `echo nope` $(touch SHOULD_NOT_EXIST) {braces} "quotes"';
    const additional = mode === 'malformed' ? '--session-dir "unterminated'
      : `--session-dir '${sessions}' --no-prompt-templates${mode === 'unsupported' ? ' --unsupported-fixture-option' : ''}`;
    const env: NodeJS.ProcessEnv = { ...process.env };
    for (const name of Object.keys(env)) if (/^(JUNO_|YYLO_|PI_|GIT_|PYTHON)/u.test(name)) delete env[name];
    delete env.FORCE_COLOR;
    Object.assign(env, { HOME: home, XDG_STATE_HOME: path.join(root, 'state'),
      PATH: `${bin}:${process.env.PATH}`, NO_COLOR: '1', CI: '1', YYLO_EXECUTION_EVIDENCE_FD: '3' });
    const result = spawnSync(process.execPath, ['--import', path.resolve('node_modules/tsx/dist/loader.mjs'),
      path.resolve('src/bin/cli.ts'), '--execution-envelope', 'pi', '--model', 'vendor/model',
      '--additional-args', additional, '-p', prompt], { cwd, env, encoding: 'utf8', timeout: 45000, stdio: ['pipe', 'pipe', 'pipe', 'pipe'] });
    if (mode === 'success') {
      expect(result.status, result.stderr + result.stdout).toBe(0);
      const envelope = JSON.parse(result.stdout);
      expect(envelope).toMatchObject({ status: 'success', session_id: 'fixture-session', provider: 'vendor', model: 'model' });
      const observed = await fs.readJson(path.join(sessions, 'session.json'));
      expect(observed.prompt).toBe(prompt);
      expect(observed.argv.slice(-3)).toEqual(['--session-dir', sessions, '--no-prompt-templates']);
      expect(observed.cwd).toBe(cwd);
    } else {
      expect(result.status).not.toBe(0);
      expect(await fs.pathExists(path.join(root, 'child-invoked'))).toBe(mode === 'unsupported');
      expect(JSON.parse(result.stdout).status).toBe('failure');
      expect(result.stderr + result.stdout + (result.output[3] ?? '')).toMatch(mode === 'malformed' ? /quotation|quotes|quote/i : /unsupported-fixture-option/);
      expect(await fs.pathExists(path.join(sessions, 'session.json'))).toBe(false);
    }
    expect(await fs.pathExists(path.join(home, '.pi/agent/sessions'))).toBe(false);
    expect(await fs.pathExists(path.join(cwd, 'SHOULD_NOT_EXIST'))).toBe(false);
  }, 60000);
});
