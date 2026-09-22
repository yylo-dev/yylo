import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { prepareControllerCommand } from '../controller-generation-command.js';

// Exercise real workspace discovery; no generation may be admitted for these roots.
const mocks = vi.hoisted(() => ({ admit: vi.fn(), metadata: vi.fn() }));
vi.mock('../controller-generation-startup.js', () => ({
  admitControllerCommand: mocks.admit, assessControllerGeneration: vi.fn(),
  prepareInstalledControllerRepair: vi.fn(), ensureControllerGeneration: vi.fn(),
  reuseControllerCommandAdmission: vi.fn(),
}));
vi.mock('../script-installer.js', () => ({ ScriptInstaller: { isMetadataOnlyController: mocks.metadata } }));
let temp: string;
let saved: NodeJS.ProcessEnv;
const git = (cwd: string, ...args: string[]) => execFileSync('git', ['-C', cwd, ...args], { stdio: 'pipe' });
const invoke = (cwd: string) => prepareControllerCommand(cwd, ['pi'], ['pi', '--no-hooks']);

describe('generation discovery respects the invocation workspace', () => {
  beforeEach(() => {
    saved = { ...process.env };
    for (const key of Object.keys(process.env)) if (/^(JUNO_|YYLO_|GIT_)/.test(key)) delete process.env[key];
    temp = fs.mkdtempSync(path.join(os.tmpdir(), 'yy-generation-discovery-'));
    fs.mkdirSync(path.join(temp, '.juno_task'));
    fs.mkdirSync(path.join(temp, 'agent-root'));
  });
  afterEach(() => { process.env = saved; fs.rmSync(temp, { recursive: true, force: true }); });

  it.each([false, true])('does not inherit an ancestor marker (Git root: %s)', async initializedGit => {
    const cwd = path.join(temp, 'agent-root');
    if (initializedGit) git(cwd, 'init', '-b', 'main');
    const before = fs.readdirSync(cwd);
    expect(await invoke(cwd)).toBe(false);
    expect(mocks.admit).not.toHaveBeenCalled();
    expect(mocks.metadata).not.toHaveBeenCalled();
    expect(fs.readdirSync(cwd)).toEqual(before);
    expect(fs.existsSync(path.join(temp, '.juno_task'))).toBe(true);
  });

  it('still refuses inherited controller assertions for a neutral directory', async () => {
    process.env.JUNO_TASK_ROOT = temp;
    await expect(invoke(path.join(temp, 'agent-root'))).rejects.toThrow(/cannot inherit controller authority/);
    expect(mocks.admit).not.toHaveBeenCalled();
  });

  it('still refuses incomplete persisted registration', async () => {
    const cwd = path.join(temp, 'agent-root');
    git(cwd, 'init', '-b', 'main');
    git(cwd, 'config', 'juno.controller.path', temp);
    await expect(invoke(cwd)).rejects.toThrow(/registration/);
    expect(mocks.admit).not.toHaveBeenCalled();
  });
});
