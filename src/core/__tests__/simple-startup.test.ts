import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import fs from 'fs-extra';
import os from 'node:os';
import path from 'node:path';
import { execFileSync, spawnSync } from 'node:child_process';
import { loadConfig } from '../config.js';
import { createExecutionEngine, createExecutionRequest, ExecutionStatus } from '../engine.js';
import { checkLedgerReadiness, invokeLedger, LEDGER_VERSION_RANGE } from '../../cli/commands/ledger.js';
import { controllerEnvironment, resolveController } from '../../utils/controller-resolver.js';
import { checkpointControllerAfterFinalization } from '../../utils/controller-checkpoint.js';

const backend = vi.hoisted(() => ({ configure: vi.fn(), execute: vi.fn() }));
vi.mock('../backends/shell-backend.js', () => ({ ShellBackend: vi.fn() }));
const realLedger = process.env.YYLO_TEST_LEDGER_EXECUTABLE;
let root: string, nested: string, bin: string, env: NodeJS.ProcessEnv;
const git = (...args: string[]) => execFileSync('git', ['-C', root, ...args], { encoding: 'utf8' }).trim();
const marker = () => path.join(root, '.juno_task/config.json');
const snapshot = () => ({ head: git('rev-parse', 'HEAD'), refs: git('show-ref'), index: git('ls-files', '--stage'), trees: git('worktree', 'list', '--porcelain') });
const configFile = (extra = {}) => fs.writeJson(marker(), { controllerWorkspace: { mode: 'simple', version: 1 }, ...extra });
async function stubLedger(version = LEDGER_VERSION_RANGE) {
  await fs.writeFile(path.join(bin, 'yylo-ledger'), `#!/bin/sh\nif [ "$1" = --version ]; then echo 'yylo-ledger ${version}'; exit 0; fi\nprintf '%s\\n' "$PWD" "$JUNO_TASK_ROOT" "$@" > "$JUNO_TASK_ROOT/delegate.txt"\ncat > "$JUNO_TASK_ROOT/stdin.txt"\n`, { mode: 0o755 });
}
beforeEach(async () => {
  env = { ...process.env };
  for (const key of Object.keys(process.env)) if (/^(JUNO_|YYLO_|GIT_)/.test(key)) delete process.env[key];
  root = await fs.mkdtemp(path.join(os.tmpdir(), 'yylo-simple-startup-'));
  nested = path.join(root, 'notes/deeper'); bin = path.join(root, 'bin');
  await fs.ensureDir(nested); await fs.ensureDir(bin); await fs.ensureDir(path.dirname(marker()));
  git('init', '-q'); git('config', 'user.email', 'test@example.test'); git('config', 'user.name', 'Test');
  await configFile(); await fs.writeFile(path.join(root, 'notebook.ipynb'), 'original');
  git('add', '.juno_task/config.json', 'notebook.ipynb'); git('commit', '-qm', 'fixture');
  await fs.writeFile(path.join(root, 'notebook.ipynb'), 'dirty notebook');
  process.env.PATH = `${bin}:${process.env.PATH}`;
  await stubLedger();
  vi.spyOn(process, 'on').mockReturnValue(process);
  const { ShellBackend } = await import('../backends/shell-backend.js');
  vi.mocked(ShellBackend).mockImplementation(() => ({
    configure: backend.configure, execute: backend.execute, initialize: async () => {},
    isAvailable: async () => true, cleanup: async () => {}, onProgress: () => () => {},
    name: 'stub', type: 'shell',
  }) as any);
  backend.execute.mockImplementation(async (request) => ({ content: 'local result', status: 'completed', request,
    startTime: new Date(), endTime: new Date(), duration: 1, progressEvents: [] }));
});
afterEach(async () => { process.env = env; vi.restoreAllMocks(); await fs.remove(root); });

describe('validated Simple local startup', () => {
  it('loads root preferences with nested cwd and no implicit hooks, assets, commits or notebook changes', async () => {
    await configFile({ defaultSubagent: 'pi' });
    const before = snapshot(); const bytes = await fs.readFile(marker(), 'utf8');
    const config = await loadConfig({ baseDir: nested });
    expect(config).toMatchObject({ workingDirectory: nested, sessionDirectory: path.join(root, '.juno_task'), defaultSubagent: 'pi', hooks: {}, autoDependencyUpdate: false });
    expect(config.gitFlow).toBeUndefined();
    const engine = createExecutionEngine(config);
    try {
      const result = await engine.execute(createExecutionRequest({ instruction: 'local stub', workingDirectory: nested, subagent: 'pi', maxIterations: 1 }));
      expect(result.status, JSON.stringify(result.error)).toBe(ExecutionStatus.COMPLETED);
      expect(backend.execute).toHaveBeenCalledOnce();
      expect(backend.configure).toHaveBeenCalledWith(expect.objectContaining({ workingDirectory: nested,
        environment: expect.objectContaining({ JUNO_TASK_ROOT: root, JUNO_WORKSPACE_ROLE: 'simple' }) }));
    } finally { await engine.shutdown(); }
    expect(await checkpointControllerAfterFinalization(nested, 0)).toEqual({ attempted: false, ok: true });
    expect(snapshot()).toEqual(before);
    expect(await fs.readFile(marker(), 'utf8')).toBe(bytes);
    expect(await fs.readFile(path.join(root, 'notebook.ipynb'), 'utf8')).toBe('dirty notebook');
    expect(await fs.pathExists(path.join(root, '.juno_task/scripts'))).toBe(false);
    expect(await fs.pathExists(path.join(root, '.env.yylo'))).toBe(false);
  });

  it('preserves explicitly configured local hooks and keeps cwd overrides canonical', async () => {
    await configFile({ hooks: { START_RUN: { commands: ['echo local'] } }, workingDirectory: 'notes/deeper' });
    const config = await loadConfig({ baseDir: root });
    expect(config.workingDirectory).toBe(nested);
    expect((await loadConfig({ baseDir: nested })).workingDirectory).toBe(nested);
    expect(config.hooks?.START_RUN?.commands).toEqual(['echo local']);
    await expect(loadConfig({ baseDir: nested, cliConfig: { workingDirectory: os.tmpdir() } })).rejects.toThrow(/same project root/);
    await fs.symlink(os.tmpdir(), path.join(root, 'escape'));
    await expect(loadConfig({ baseDir: root, cliConfig: { workingDirectory: 'escape' } })).rejects.toThrow(/same project root/);
    const child = path.join(root, 'child'); await fs.ensureDir(child); execFileSync('git', ['-C', child, 'init', '-q']);
    await expect(loadConfig({ baseDir: root, cliConfig: { workingDirectory: child } })).rejects.toThrow(/nested Simple/);
    await expect(loadConfig({ baseDir: root, cliConfig: { controllerWorkspace: undefined } })).rejects.toThrow(/contradicts/);
  });

  it('carries same-root authority to nested agents and refuses unrelated inherited routing', async () => {
    Object.assign(process.env, controllerEnvironment(root, 'product-edit'));
    expect(resolveController(nested).path).toBe(root);
    expect((await loadConfig({ baseDir: nested })).workingDirectory).toBe(nested);
    process.env.JUNO_TASK_ROOT = '/unrelated';
    await expect(loadConfig({ baseDir: nested })).rejects.toThrow(/assertion mismatch/);
    await expect(invokeLedger(['list'], { cwd: nested })).rejects.toThrow(/assertion mismatch/);
    expect(backend.execute).not.toHaveBeenCalled();
  });

  it('blocks missing/incompatible Ledger before stub dispatch without installing', async () => {
    await stubLedger('9.9.9');
    await expect(checkLedgerReadiness({ cwd: nested })).rejects.toThrow(/incompatible/);
    // Probe absence by an explicit missing name, but keep the incompatible
    // fixture ahead of any developer-installed Ledger for the engine check.
    await expect(checkLedgerReadiness({ cwd: nested, executableName: 'missing-ledger-test' })).rejects.toThrow(/Install a compatible/);
    const engine = createExecutionEngine(await loadConfig({ baseDir: nested }));
    try {
      await expect(engine.execute(createExecutionRequest({ instruction: 'local', workingDirectory: nested, maxIterations: 1 })))
        .rejects.toMatchObject({ message: expect.stringContaining('Install a compatible') });
      expect(backend.configure).not.toHaveBeenCalled();
      expect(backend.execute).not.toHaveBeenCalled();
    } finally { await engine.shutdown(); }
    expect(await fs.pathExists(path.join(root, '.venv_juno'))).toBe(false);
  });

  it.each(['--config=/outside', '-c/outside', '--conf=/outside'])('rejects Ledger config escape %s before dispatch', async (arg) => {
    await expect(invokeLedger([arg, 'list'], { cwd: nested })).rejects.toThrow(/root-bound/);
    expect(await fs.pathExists(path.join(root, 'delegate.txt'))).toBe(false);
  });

  it('bounds the readiness probe without dependency installation', async () => {
    await fs.writeFile(path.join(bin, 'yylo-ledger'), '#!/bin/sh\nexec sleep 30\n', { mode: 0o755 });
    await expect(checkLedgerReadiness({ cwd: root, versionTimeoutMs: 30 })).rejects.toThrow(/timed out/);
  });

  it('packaged Ledger wrapper keeps cwd/root and stdin, bypassing local provisioning', async () => {
    const wrapper = path.resolve('src/templates/scripts/kanban.sh');
    const before = snapshot();
    const result = spawnSync('bash', [wrapper, 'create', 'local'], { cwd: nested, input: 'body from stdin', encoding: 'utf8', env: process.env });
    expect(result.status, result.stderr).toBe(0);
    expect(await fs.readFile(path.join(root, 'delegate.txt'), 'utf8')).toBe(`${nested}\n${root}\n--config\n${marker()}\ncreate\nlocal\n`);
    expect(await fs.readFile(path.join(root, 'stdin.txt'), 'utf8')).toBe('body from stdin');
    expect(snapshot()).toEqual(before);
    expect(await fs.pathExists(path.join(root, '.venv_juno'))).toBe(false);
  });

  it.skipIf(!realLedger)('uses normal installed Ledger for a same-root round trip from nested cwd', async () => {
    await fs.remove(path.join(bin, 'yylo-ledger'));
    await fs.symlink(realLedger!, path.join(bin, 'yylo-ledger'));
    const before = snapshot();
    const created = spawnSync(realLedger!, ['--config', marker(), '-f', 'json', 'create', 'Simple round trip'], { cwd: nested, encoding: 'utf8', env: controllerEnvironment(nested, 'kanban') });
    expect(created.status, created.stderr).toBe(0);
    const [task] = JSON.parse(created.stdout);
    expect(task).toEqual(expect.objectContaining({ id: expect.stringMatching(/^[A-Za-z0-9]{6}$/) }));
    expect((await invokeLedger(['get', task.id], { cwd: nested })).code).toBe(0);
    expect((await invokeLedger(['mark', 'done', '--id', task.id, '--response', 'local bookkeeping'], { cwd: root })).code).toBe(0);
    const read = spawnSync(realLedger!, ['--config', marker(), '-f', 'json', 'get', task.id], { cwd: nested, encoding: 'utf8', env: controllerEnvironment(nested, 'kanban') });
    expect(JSON.parse(read.stdout)[0].status).toBe('done');
    expect((await invokeLedger(['doctor'], { cwd: nested })).code).toBe(0);
    expect((await invokeLedger(['history', task.id], { cwd: root })).code).toBe(0);
    expect(await fs.pathExists(path.join(nested, '.juno_task/tasks'))).toBe(false);
    expect(snapshot()).toEqual(before);
  });
});
