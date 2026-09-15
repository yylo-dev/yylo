import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { AgentStartupError, agentStartupHooks, checkAgentReadiness, errorMessage, resolveAgentWorkspace } from '../agent-startup.js';
import { getDefaultHooks } from '../../templates/default-hooks.js';

const mocks = vi.hoisted(() => ({ ledger: vi.fn(), runtime: vi.fn() }));
vi.mock('../../cli/commands/ledger.js', () => ({ checkLedgerReadiness: mocks.ledger }));
vi.mock('../script-installer.js', () => ({ ScriptInstaller: { assertManagedControllerPackageUpdateAllowed: mocks.runtime } }));
let temp: string;
let saved: NodeJS.ProcessEnv;
const git = (cwd: string, ...args: string[]) => execFileSync('git', ['-C', cwd, ...args], { encoding: 'utf8', stdio: 'pipe' }).trim();
function controller() {
  const root = path.join(temp, 'controller');
  fs.mkdirSync(path.join(root, '.juno_task'), { recursive: true });
  git(root, 'init', '-b', 'controller');
  git(root, 'config', 'user.name', 'Test'); git(root, 'config', 'user.email', 'test@example.invalid');
  fs.writeFileSync(path.join(root, '.juno_task/config.json'), JSON.stringify({ controllerWorkspace: { mode: 'metadata-only', policy: '.juno_task/config/metadata-controller.json' } }));
  git(root, 'add', '.'); git(root, 'commit', '-m', 'controller');
  git(root, 'config', 'extensions.worktreeConfig', 'true');
  git(root, 'config', '--local', 'juno.controller.path', root);
  git(root, 'config', '--local', 'juno.controller.branch', 'refs/heads/controller');
  git(root, 'config', '--worktree', 'juno.workspace.role', 'controller');
  return root;
}
describe('workspace-owned agent startup', () => {
  beforeEach(() => {
    saved = { ...process.env };
    for (const key of Object.keys(process.env)) if (/^(JUNO_|YYLO_|GIT_)/.test(key)) delete process.env[key];
    temp = fs.mkdtempSync(path.join(os.tmpdir(), 'yy-agent-startup-'));
    mocks.ledger.mockResolvedValue('/fixture/yylo-ledger'); mocks.runtime.mockResolvedValue(null);
  });
  afterEach(() => { process.env = saved; fs.rmSync(temp, { recursive: true, force: true }); });
  it('allows a generic folder without hooks, installs, or child-controller discovery', async () => {
    for (const child of ['one_controller', 'two_controller']) fs.mkdirSync(path.join(temp, child, '.juno_task'), { recursive: true });
    const before = fs.readdirSync(temp);
    const authority = resolveAgentWorkspace(temp);
    expect(authority.role).toBe('unregistered');
    expect(authority.path).toBe(temp);
    await checkAgentReadiness(authority);
    expect(mocks.ledger).not.toHaveBeenCalled(); expect(mocks.runtime).not.toHaveBeenCalled();
    expect(fs.readdirSync(temp)).toEqual(before);
    expect(agentStartupHooks(getDefaultHooks(), true)?.START_ITERATION?.commands).toEqual([]);
  });
  it('allows an ordinary Git folder without inventing a controller', () => {
    git(temp, 'init', '-b', 'main');
    expect(resolveAgentWorkspace(temp).role).toBe('unregistered');
    expect(fs.existsSync(path.join(temp, '.juno_task'))).toBe(false);
  });
  it('refuses inherited unrelated controller assertions rather than choosing a child', () => {
    const root = controller();
    process.env.JUNO_TASK_ROOT = root;
    expect(() => resolveAgentWorkspace(temp)).toThrow(/cannot inherit controller authority/);
    expect(() => resolveAgentWorkspace(temp, root)).toThrow(/cannot inherit controller authority/);
  });
  it('checks the registered controller without requiring a worktree-local resolver or installer', async () => {
    const root = controller();
    const task = path.join(temp, 'task');
    git(root, 'worktree', 'add', '-b', 'task', task);
    fs.writeFileSync(path.join(task, '.juno_task/config.json'), '{}');
    git(task, 'add', '.juno_task/config.json'); git(task, 'commit', '-m', 'product configuration');
    for (const [name, value] of Object.entries({ role: 'task', taskId: 'TASK01', manifestIdentity: 'manifest',
      createReceiptSha256: 'receipt', expectedPathsSha256: 'paths' })) git(task, 'config', '--worktree', `juno.workspace.${name}`, value);
    const authority = resolveAgentWorkspace(task);
    expect(authority.role).toBe('task'); expect(authority.path).toBe(root); expect(authority.current_root).toBe(task);
    await checkAgentReadiness(authority);
    expect(mocks.ledger).toHaveBeenCalledWith({ cwd: root });
    expect(mocks.runtime).toHaveBeenCalledWith(root);
    expect(git(task, 'status', '--porcelain')).toBe(''); expect(git(root, 'status', '--porcelain')).toBe('');
    expect(() => resolveAgentWorkspace(task, temp)).toThrow(AgentStartupError);
    expect(resolveAgentWorkspace(task, root).path).toBe(root);
  });
  it('does not swallow incomplete registration or wrong branch failures', () => {
    const root = controller();
    git(root, 'config', '--local', '--unset', 'juno.controller.branch');
    expect(() => resolveAgentWorkspace(root)).toThrow(/registration/);
    git(root, 'config', '--local', 'juno.controller.branch', 'refs/heads/wrong');
    expect(() => resolveAgentWorkspace(root)).toThrow(/branch mismatch/);
  });
  it('retains runtime and Ledger readiness failures without hooks or upgrades', async () => {
    const authority = resolveAgentWorkspace(controller());
    mocks.runtime.mockRejectedValue(new Error('receipt-bound runtime mismatch'));
    await expect(checkAgentReadiness(authority)).rejects.toThrow(/receipt-bound runtime mismatch/);
    expect(mocks.ledger).not.toHaveBeenCalled();
    mocks.runtime.mockResolvedValue(null); mocks.ledger.mockRejectedValue({ message: 'Ledger version incompatible' });
    await expect(checkAgentReadiness(authority)).rejects.toThrow(/Ledger version incompatible/);
  });
  it('normalizes structured errors and preserves custom hooks without mutating configuration', () => {
    expect(errorMessage({ message: 'use the registered controller', type: 'tool_execution' })).toBe('use the registered controller');
    expect(errorMessage({ code: 'BAD_CONTEXT' })).not.toContain('[object Object]');
    const hooks = { START_RUN: { commands: ['./.juno_task/scripts/install_requirements.sh', 'echo custom'] } };
    expect(agentStartupHooks(hooks, true)?.START_RUN?.commands).toEqual(['echo custom']);
    expect(hooks.START_RUN.commands).toHaveLength(2);
  });
});
