import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { execFileSync, spawnSync } from 'node:child_process';
import { Command } from 'commander';
import { planSimpleInit, applySimpleInit, planSimpleConversion, applySimpleConversion, writeSimpleInitPlan } from '../simple-init.js';
import { resolveController } from '../controller-resolver.js';
import { configureInitCommand } from '../../cli/commands/init.js';
import { SIMPLE_FILES } from '../../templates/simple-workspace.js';
import { promptInputOnce, promptMultiline } from '../../cli/utils/multiline.js';
vi.mock('../../cli/utils/multiline.js', () => ({ promptInputOnce: vi.fn(), promptMultiline: vi.fn() }));

let root: string;
let originalEnv: NodeJS.ProcessEnv;
const conversionDestinations: string[] = [];
const git = (...args: string[]) => execFileSync('git', ['-C', root, ...args], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();
const snap = () => ({ head: git('rev-parse', 'HEAD'), refs: git('show-ref'), worktrees: git('worktree', 'list', '--porcelain'), index: git('ls-files', '--stage') });
beforeEach(async () => {
  originalEnv = { ...process.env };
  for (const key of Object.keys(process.env)) if (key.startsWith('JUNO_') || key.startsWith('YYLO_') || key.startsWith('GIT_')) delete process.env[key];
  root = await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(), 'yylo-simple-init-')));
  git('init', '-q'); git('config', 'user.email', 'fixture@example.test'); git('config', 'user.name', 'Fixture');
  await fs.writeFile(path.join(root, 'notebook.ipynb'), 'original');
  git('add', 'notebook.ipynb'); git('commit', '-qm', 'fixture');
  await fs.writeFile(path.join(root, 'notebook.ipynb'), 'dirty notebook');
});
afterEach(async () => {
  vi.restoreAllMocks(); process.env = originalEnv;
  for (const destination of conversionDestinations.splice(0)) await fs.rm(destination, { recursive: true, force: true });
  await fs.rm(root, { recursive: true, force: true });
});

async function advancedFixture(extraProductFile?: { name: string; content: string }): Promise<string> {
  git('branch', '-M', 'main');
  await fs.mkdir(path.join(root, '.juno_task'), { recursive: true });
  await fs.writeFile(path.join(root, '.juno_task/config.json'), '{"defaultSubagent":"pi"}');
  await fs.writeFile(path.join(root, 'AGENTS.md'), 'Use yy task start TASK_ID. Project tests: npm test.');
  await fs.writeFile(path.join(root, '.gitignore'), '*.json\n');
  if (extraProductFile) {
    const file = path.join(root, extraProductFile.name);
    await fs.mkdir(path.dirname(file), { recursive: true });
    await fs.writeFile(file, extraProductFile.content);
    git('add', '-f', extraProductFile.name);
  }
  git('add', '--all'); git('add', '-f', '.juno_task/config.json'); git('commit', '-qm', 'product');
  const product = git('rev-parse', 'HEAD');
  // Disposable fixture only: model the existing split controller/product history.
  git('checkout', '--orphan', 'advanced-controller'); git('rm', '-rf', '.');
  const write = async (name: string, content: unknown) => {
    const file = path.join(root, '.juno_task', name);
    await fs.mkdir(path.dirname(file), { recursive: true });
    await fs.writeFile(file, typeof content === 'string' ? content : JSON.stringify(content));
  };
  await write('config.json', { controllerWorkspace: { mode: 'metadata-only', policy: '.juno_task/config/metadata-controller.json' } });
  await write('config/metadata-controller.json', { schema_version: 'juno_metadata_controller_policy.v1', controller_branch: 'refs/heads/advanced-controller', product_ref: 'refs/heads/main' });
  await write('config/task-workspace.json', { schema_version: 'juno_task_workspace_config.v1', repository: '.', target_ref: 'refs/heads/main' });
  await write('state/tasks.json', { schema_version: 'juno_task_workspace_state.v2', tasks: {}, queues: {} });
  await write('tasks/ab/ABC123.md', '---\nid: ABC123\nstatus: done\n---\nPreserve task history');
  await write('ledger/ab/ABC123/1.json', '{"revision":1,"response":"original"}');
  await write('objects/sha256/ab/payload', '');
  await fs.writeFile(path.join(root, '.juno_task/objects/sha256/ab/payload'), Buffer.from([0, 1, 2, 255]));
  await write('wiki/project.md', '# Project knowledge\n');
  await fs.writeFile(path.join(root, '.gitignore'), '.juno_task/runtime/\n.juno_task/secrets/\n');
  git('add', '--all'); git('commit', '-qm', 'controller');
  git('config', 'extensions.worktreeConfig', 'true');
  git('config', '--local', 'juno.controller.path', root);
  git('config', '--local', 'juno.controller.branch', 'refs/heads/advanced-controller');
  git('config', '--worktree', 'juno.workspace.role', 'controller');
  expect(resolveController(root, 'diagnostic', { trustedResolver: true })).toMatchObject({ valid: true, role: 'controller' });
  return product;
}
function conversionDestination(): string {
  const destination = `${root}-simple-${conversionDestinations.length}`;
  conversionDestinations.push(destination);
  return destination;
}

describe('Advanced-to-Simple fresh-workspace conversion', () => {
  it.each(['v1', 'v2'])('converts settled %s under an unrelated ancestor without source changes', async (version) => {
    await advancedFixture();
    const state = { schema_version: `juno_task_workspace_state.${version}`, tasks: {
      ABC123: version === 'v2' ? { state: 'MERGED', task_id: 'ABC123', schema_version: 'juno_task_terminal_tombstone.v1' } : { state: 'MERGED' },
      DEF456: version === 'v2' ? { state: 'WITHDRAWN', task_id: 'DEF456', schema_version: 'juno_task_terminal_tombstone.v1' } : { state: 'WITHDRAWN' },
    }, queues: {} };
    await fs.writeFile(path.join(root, '.juno_task/state/tasks.json'), JSON.stringify(state));
    git('add', '.'); git('commit', '-qm', 'settled state');
    const parent = conversionDestination(); await fs.mkdir(parent);
    execFileSync('git', ['-C', parent, 'init', '-q']);
    await applySimpleInit(await planSimpleInit(parent));
    const parentConfig = await fs.readFile(path.join(parent, '.juno_task/config.json'));
    const destination = path.join(parent, 'child');
    const before = snap(); const stateBytes = await fs.readFile(path.join(root, '.juno_task/state/tasks.json'));
    await applySimpleConversion(await planSimpleConversion(root, destination));
    expect(resolveController(destination)).toMatchObject({ role: 'simple', path: destination });
    expect(snap()).toEqual(before);
    expect(await fs.readFile(path.join(root, '.juno_task/state/tasks.json'))).toEqual(stateBytes);
    expect(await fs.readFile(path.join(parent, '.juno_task/config.json'))).toEqual(parentConfig);
  });

  it('accepts pre-queue v1 without rewriting source state', async () => {
    await advancedFixture();
    await fs.writeFile(path.join(root, '.juno_task/state/tasks.json'), JSON.stringify({ schema_version: 'juno_task_workspace_state.v1', tasks: {} }));
    git('add', '.'); git('commit', '-qm', 'pre-queue v1');
    const before = snap();
    await planSimpleConversion(root, conversionDestination());
    expect(snap()).toEqual(before);
  });

  it.each([
    { schema_version: 'future.v3', tasks: {}, queues: {} },
    { schema_version: 'juno_task_workspace_state.v1', tasks: [], queues: {} },
    { schema_version: 'juno_task_workspace_state.v1', tasks: {}, queues: [] },
    { schema_version: 'juno_task_workspace_state.v2', tasks: {} },
    { schema_version: 'juno_task_workspace_state.v2', tasks: { ABC123: { state: 'MERGED' } }, queues: {} },
    { schema_version: 'juno_task_workspace_state.v1', tasks: { ABC123: null }, queues: {} },
    { schema_version: 'juno_task_workspace_state.v1', tasks: { ABC123: { state: 'QUEUED' } }, queues: {} },
  ])('refuses malformed, unsupported or unfinished state %j before writes', async (state) => {
    await advancedFixture(); const destination = conversionDestination();
    await fs.writeFile(path.join(root, '.juno_task/state/tasks.json'), JSON.stringify(state));
    git('add', '.'); git('commit', '-qm', 'state fixture');
    const before = snap();
    await expect(planSimpleConversion(root, destination)).rejects.toThrow(/lifecycle/);
    expect(snap()).toEqual(before);
    await expect(fs.stat(destination)).rejects.toThrow();
  });
  it('previews and applies product history plus Ledger, leaving source/registration unchanged', async () => {
    const product = await advancedFixture();
    const before = snap(); const config = git('config', '--local', '--list');
    process.env.JUNO_TASK_ROOT = root;
    process.env.JUNO_WORKSPACE_ROLE = 'controller';
    const destination = conversionDestination();
    const plan = await planSimpleConversion(root, destination);
    expect(plan.source.targetSha).toBe(product);
    await expect(fs.stat(destination)).rejects.toThrow();
    expect(plan.retain).toEqual(['.juno_task', 'AGENTS.md', '.gitignore']);
    await expect(writeSimpleInitPlan(path.join(root, 'plan.json'), plan)).rejects.toThrow(/outside/);
    expect(await applySimpleConversion(plan)).toBe('converted');
    expect(snap()).toEqual(before); expect(git('config', '--local', '--list')).toBe(config);
    const outputGit = (...args: string[]) => execFileSync('git', ['-C', destination, ...args], { encoding: 'utf8' }).trim();
    expect(outputGit('rev-parse', 'HEAD')).toBe(product);
    expect(outputGit('branch', '--show-current')).toBe('main');
    expect(outputGit('remote')).toBe('');
    expect(outputGit('branch', '--list', '*advanced-controller*')).toBe('');
    expect(outputGit('diff', '--cached')).toBe('');
    expect(await fs.readFile(path.join(destination, 'notebook.ipynb'), 'utf8')).toBe('dirty notebook');
    for (const file of plan.copy) expect(await fs.readFile(path.join(destination, file.path))).toEqual(await fs.readFile(path.join(root, file.path)));
    expect(await fs.readFile(path.join(destination, '.juno_task/advanced-backup/AGENTS.md'), 'utf8')).toContain('Project tests: npm test');
    expect(await fs.readFile(path.join(destination, 'AGENTS.md'), 'utf8')).toContain('inactive under');
    expect(JSON.parse(await fs.readFile(path.join(destination, '.juno_task/config.json'), 'utf8')).controllerWorkspace.mode).toBe('simple');
    expect(process.env.JUNO_TASK_ROOT).toBe(root); // Conversion never mutates parent routing.
    delete process.env.JUNO_TASK_ROOT; delete process.env.JUNO_WORKSPACE_ROLE;
    expect(resolveController(destination)).toMatchObject({ valid: true, role: 'simple' });
    await expect(fs.stat(path.join(destination, '.juno_task/state'))).rejects.toThrow();
    await expect(applySimpleConversion(plan)).rejects.toThrow(/must not exist/);
  });

  it.each([
    { name: '.env.yylo', content: 'fixture, not a credential', error: /tracks secrets/ },
    { name: 'pkg/.claude/settings.json', content: '{}', error: /Nested agent/ },
    { name: 'pkg/AGENTS.md', content: 'Use yy task start TASK_ID', error: /Nested agent/ },
    { name: '.juno_task/tasks/ab/OTHER.md', content: 'other board', error: /also contains durable/ },
  ])('refuses unsupported product content $name before mutation', async ({ name, content, error }) => {
    await advancedFixture({ name, content });
    const destination = conversionDestination(); const before = snap();
    await expect(planSimpleConversion(root, destination)).rejects.toThrow(error);
    expect(snap()).toEqual(before);
    await expect(fs.stat(destination)).rejects.toThrow();
  });

  it.each(['WORKING', 'QUEUED', 'CONFLICTED', 'unknown'])('refuses unsettled lifecycle %s without destination writes', async (state) => {
    await advancedFixture(); const destination = conversionDestination();
    await fs.writeFile(path.join(root, '.juno_task/state/tasks.json'), JSON.stringify({ schema_version: 'juno_task_workspace_state.v2', tasks: { ABC123: { state } }, queues: {} }));
    git('add', '.'); git('commit', '-qm', 'state');
    await expect(planSimpleConversion(root, destination)).rejects.toThrow(/Unfinished or unknown/);
    await expect(fs.stat(destination)).rejects.toThrow();
  });

  it('refuses dirty, ignored durable, stale, tampered and colliding sources/destinations', async () => {
    await advancedFixture(); const destination = conversionDestination();
    const plan = await planSimpleConversion(root, destination);
    await fs.writeFile(path.join(root, 'untracked.txt'), 'preserve');
    await expect(applySimpleConversion(plan)).rejects.toThrow(/Dirty source/);
    await fs.unlink(path.join(root, 'untracked.txt'));
    const bad = structuredClone(plan); bad.copy[0]!.path = '../escape';
    await expect(applySimpleConversion(bad)).rejects.toThrow(/Stale or modified/);
    git('config', 'user.name', 'Changed');
    await expect(applySimpleConversion(plan)).rejects.toThrow(/Stale or modified/);
    await expect(fs.stat(destination)).rejects.toThrow();
    await fs.mkdir(destination);
    await expect(planSimpleConversion(root, destination)).rejects.toThrow(/must not exist/);
    await fs.appendFile(path.join(root, '.gitignore'), '.juno_task/tasks/ignored.md\n');
    git('add', '.gitignore'); git('commit', '-qm', 'ignore');
    await fs.writeFile(path.join(root, '.juno_task/tasks/ignored.md'), 'must not omit');
    await expect(planSimpleConversion(root, conversionDestination())).rejects.toThrow(/Ignored durable/);
  });

  it('refuses index flags hiding dirty durable bytes', async () => {
    await advancedFixture(); const destination = conversionDestination();
    const file = '.juno_task/tasks/ab/ABC123.md';
    git('update-index', '--assume-unchanged', file);
    await fs.writeFile(path.join(root, file), 'hidden dirty task');
    await expect(planSimpleConversion(root, destination)).rejects.toThrow(/assume-unchanged/);
    expect(await fs.readFile(path.join(root, file), 'utf8')).toBe('hidden dirty task');
    await expect(fs.stat(destination)).rejects.toThrow();
  });

  it('keeps an interrupted destination unready and the source unchanged', async () => {
    await advancedFixture(); const destination = conversionDestination();
    const before = snap(); const plan = await planSimpleConversion(root, destination);
    const write = fs.writeFile.bind(fs);
    vi.spyOn(fs, 'writeFile').mockImplementation(async (...args: Parameters<typeof fs.writeFile>) => {
      if (String(args[0]) === path.join(plan.root, '.juno_task/config.json')) throw new Error('injected conversion interruption');
      return write(...args);
    });
    await expect(applySimpleConversion(plan)).rejects.toThrow(/injected/);
    vi.restoreAllMocks();
    expect(snap()).toEqual(before);
    expect(() => resolveController(destination)).toThrow(/incomplete or active Simple/);
    expect(await fs.readFile(path.join(destination, 'notebook.ipynb'), 'utf8')).toBe('dirty notebook');
    await expect(applySimpleConversion(plan)).rejects.toThrow(/must not exist/);
  });

  it('refuses symlinks, nested destinations and Simple sources', async () => {
    await applySimpleInit(await planSimpleInit(root));
    await expect(planSimpleConversion(root, conversionDestination())).rejects.toThrow(/registered Advanced/);
    await fs.rm(path.join(root, '.juno_task'), { recursive: true });
    await advancedFixture();
    await expect(planSimpleConversion(root, path.join(root, 'nested'))).rejects.toThrow(/separate/);
    await fs.symlink('tasks/ab/ABC123.md', path.join(root, '.juno_task/linked'));
    git('add', '.'); git('commit', '-qm', 'symlink');
    await expect(planSimpleConversion(root, conversionDestination())).rejects.toThrow(/symlinks and submodules/);
  });

  it('refuses reverse conversion even with force and preserves Simple configuration', async () => {
    await applySimpleInit(await planSimpleInit(root));
    const before = snap(); const configPath = path.join(root, '.juno_task/config.json');
    const config = await fs.readFile(configPath, 'utf8');
    const program = new Command(); configureInitCommand(program);
    vi.spyOn(console, 'log').mockImplementation(() => undefined);
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    vi.spyOn(process, 'exit').mockImplementation(() => { throw new Error('exit intercepted'); });
    await expect(program.parseAsync(['init', 'Do not convert', '--mode', 'advanced', '--force', '--directory', root], { from: 'user' })).rejects.toThrow('exit intercepted');
    expect(await fs.readFile(configPath, 'utf8')).toBe(config);
    expect(snap()).toEqual(before);
  });

  it('checks every source worktree and refuses stale product movement', async () => {
    const product = await advancedFixture(); const destination = conversionDestination();
    const owner = `${root}-owner`; conversionDestinations.push(owner);
    git('worktree', 'add', '--detach', owner, product);
    const plan = await planSimpleConversion(root, destination);
    await expect(planSimpleConversion(root, path.join(owner, 'nested'))).rejects.toThrow(/separate from all source/);
    await fs.writeFile(path.join(owner, 'untracked.txt'), 'preserve owner dirt');
    await expect(applySimpleConversion(plan)).rejects.toThrow(/Dirty source worktree/);
    await fs.unlink(path.join(owner, 'untracked.txt'));
    const child = execFileSync('git', ['-C', root, 'commit-tree', `${product}^{tree}`, '-p', product, '-m', 'new product'], { encoding: 'utf8' }).trim();
    git('update-ref', 'refs/heads/main', child, product);
    await expect(applySimpleConversion(plan)).rejects.toThrow(/Stale or modified/);
    await expect(fs.stat(destination)).rejects.toThrow();
  });

  it('exposes conversion through the existing init plan/apply CLI only', async () => {
    await advancedFixture(); const destination = conversionDestination();
    const external = `${root}-conversion.json`; conversionDestinations.push(external);
    const preview = new Command(); configureInitCommand(preview);
    vi.spyOn(console, 'log').mockImplementation(() => undefined);
    await preview.parseAsync(['init', '--mode', 'simple', '--from-advanced', root, '--directory', destination], { from: 'user' });
    await expect(fs.stat(destination)).rejects.toThrow();
    const program = new Command(); configureInitCommand(program);
    await program.parseAsync(['init', '--mode', 'simple', '--from-advanced', root, '--directory', destination, '--plan-file', external], { from: 'user' });
    await expect(fs.stat(destination)).rejects.toThrow();
    // Fresh Commander instances avoid retained options across multiple parses.
    const apply = new Command(); configureInitCommand(apply);
    await apply.parseAsync(['init', '--mode', 'simple', '--apply-plan', external], { from: 'user' });
    expect(resolveController(destination).role).toBe('simple');
    const reverse = new Command(); configureInitCommand(reverse);
    await expect(reverse.parseAsync(['init', '--mode', 'advanced', '--from-advanced', destination], { from: 'user' })).rejects.toThrow(/require --mode simple/);
  });
});

describe('fresh Simple initialization', () => {
  it.each(['simple', 'metadata-only'])('allows independent child Git roots beneath %s metadata', async (mode) => {
    await fs.mkdir(path.join(root, '.juno_task'), { recursive: true });
    const bytes = JSON.stringify({ controllerWorkspace: mode === 'simple' ? { mode, version: 1 } : { mode, policy: '.juno_task/config/metadata-controller.json' } });
    await fs.writeFile(path.join(root, '.juno_task/config.json'), bytes);
    const child = path.join(root, 'child'); await fs.mkdir(child);
    execFileSync('git', ['-C', child, 'init', '-q']);
    const before = snap();
    await applySimpleInit(await planSimpleInit(child));
    expect(resolveController(child)).toMatchObject({ role: 'simple', path: child });
    expect(await applySimpleInit(await planSimpleInit(child))).toBe('already-initialized');
    expect(snap()).toEqual(before);
    expect(await fs.readFile(path.join(root, '.juno_task/config.json'), 'utf8')).toBe(bytes);
  });
  it.each([undefined, 'simple'])('initializes guided Simple with final choice or explicit mode %s', async (mode) => {
    const before = snap();
    vi.spyOn(console, 'log').mockImplementation(() => undefined);
    vi.mocked(promptMultiline).mockResolvedValue('Explore the notebook');
    vi.mocked(promptInputOnce).mockImplementation(async (label) => {
      if (label === 'Subagent choice') return '5';
      if (label === 'Git setup') return 'n';
      if (label.startsWith('Workspace mode')) {
        await expect(fs.stat(path.join(root, '.juno_task'))).rejects.toThrow();
        return '';
      }
      throw new Error(`Unexpected prompt: ${label}`);
    });
    const program = new Command(); configureInitCommand(program);
    await program.parseAsync(['init', '--interactive', '--directory', root, ...(mode ? ['--mode', mode] : [])], { from: 'user' });
    if (!mode) expect(promptInputOnce).toHaveBeenLastCalledWith(expect.stringContaining('Workspace mode'), '1');
    else expect(vi.mocked(promptInputOnce).mock.calls.some(([label]) => label.startsWith('Workspace mode'))).toBe(false);
    const config = JSON.parse(await fs.readFile(path.join(root, '.juno_task/config.json'), 'utf8'));
    expect(config).toMatchObject({ controllerWorkspace: { mode: 'simple' }, defaultSubagent: 'pi' });
    expect(await fs.readFile(path.join(root, '.juno_task/simple-agent-guidance.md'), 'utf8')).toContain('Explore the notebook');
    expect(snap()).toEqual(before);
    expect(await fs.readFile(path.join(root, 'notebook.ipynb'), 'utf8')).toBe('dirty notebook');
  });

  it.each([
    ['--mode', 'unknown'], ['--mode', 'advanced', '--plan-file', '/tmp/unused-plan'],
    ['--mode', 'simple', '--force'], ['--mode', 'simple', '--plan-file', '/tmp/p', '--apply-plan', '/tmp/p'],
  ])('refuses contradictory options before writes: %j', async (...args) => {
    const before = snap(); const program = new Command(); configureInitCommand(program);
    await expect(program.parseAsync(['init', '--directory', root, ...args], { from: 'user' })).rejects.toThrow();
    expect(snap()).toEqual(before);
    await expect(fs.stat(path.join(root, '.juno_task'))).rejects.toThrow();
  });

  it('revalidates customized plan output and preserves repeat initialization', async () => {
    const settings = { task: 'Read notebooks', subagent: 'pi' };
    const plan = await planSimpleInit(root, settings);
    const bad = structuredClone(plan); bad.settings!.subagent = 'codex';
    await expect(applySimpleInit(bad)).rejects.toThrow(/Stale or modified/);
    await applySimpleInit(plan);
    expect(await applySimpleInit(await planSimpleInit(root, settings))).toBe('already-initialized');
  });

  it('previews without writes and applies only named local assets, preserving Git and user bytes', async () => {
    await fs.writeFile(path.join(root, 'AGENTS.md'), 'Use project tests.');
    await fs.writeFile(path.join(root, 'CLAUDE.md'), 'Read the notebooks.');
    await fs.writeFile(path.join(root, '.gitignore'), '*.csv\n');
    const before = snap(); const entries = await fs.readdir(root);
    const plan = await planSimpleInit(root);
    expect(plan.outcome).toBe('ready'); expect(await fs.readdir(root)).toEqual(entries);
    expect(await applySimpleInit(plan)).toBe('initialized');
    expect(snap()).toEqual(before);
    expect(await fs.readFile(path.join(root, 'notebook.ipynb'), 'utf8')).toBe('dirty notebook');
    expect(await fs.readFile(path.join(root, 'AGENTS.md'), 'utf8')).toBe('Use project tests.');
    expect(await fs.readFile(path.join(root, 'CLAUDE.md'), 'utf8')).toBe('Read the notebooks.');
    expect(await fs.readFile(path.join(root, '.gitignore'), 'utf8')).toBe('*.csv\n');
    expect((await fs.readdir(path.join(root, '.juno_task'))).sort()).toEqual(['.gitignore', 'config.json', 'simple-agent-guidance.md', 'simple-init.json']);
    for (const [name, bytes] of Object.entries(SIMPLE_FILES)) expect(await fs.readFile(path.join(root, '.juno_task', name), 'utf8')).toBe(bytes);
    const guidance = await fs.readFile(path.join(root, '.juno_task/simple-agent-guidance.md'), 'utf8');
    expect(guidance).toContain('Done is not managed delivery');
    expect(guidance).toContain('without\nfile isolation');
    expect(guidance).toContain('No-Git execution');
    expect(guidance).toContain('Never convert an existing managed installation');
    expect(resolveController(root)).toMatchObject({ role: 'simple', path: root });
    expect(await applySimpleInit(await planSimpleInit(root))).toBe('already-initialized');
    expect(snap()).toEqual(before);
  });

  it('keeps durable Ledger paths Git-eligible and ignores only disposable/secret paths', async () => {
    await applySimpleInit(await planSimpleInit(root));
    for (const name of ['tasks/aa/ABC123.md', 'ledger/aa/event.json', 'documents/aa/r.json', 'artifacts/aa/r.json', 'artifact-ledger/aa/r.json', 'objects/sha256/aa/hash', 'archive/pack']) {
      expect(spawnSync('git', ['-C', root, 'check-ignore', '--no-index', `.juno_task/${name}`]).status, name).toBe(1);
    }
    for (const name of ['cache/kanban.sqlite3', 'locks/aa/ABC123.lock', 'logs/run.log', 'runtime/run.json', 'secrets/.env.yylo', 'sessions/secret.json']) {
      expect(spawnSync('git', ['-C', root, 'check-ignore', '--no-index', `.juno_task/${name}`]).status, name).toBe(0);
    }
  });

  it('requires explicit prior Git initialization and supports an unborn Git repository', async () => {
    const folder = await fs.mkdtemp(path.join(os.tmpdir(), 'yylo-simple-empty-'));
    try {
      await expect(planSimpleInit(folder)).rejects.toThrow(/Run git init explicitly/);
      expect(await fs.readdir(folder)).toEqual([]);
      execFileSync('git', ['init', '-q', folder]);
      const plan = await planSimpleInit(folder); expect(plan.identity.head).toBeNull();
      await applySimpleInit(plan);
      expect(spawnSync('git', ['-C', folder, 'rev-parse', '--verify', 'HEAD']).status).not.toBe(0);
    } finally { await fs.rm(folder, { recursive: true, force: true }); }
  });

  it.each(['AGENTS.md', 'CLAUDE.md'])('refuses managed %s instructions without rewriting them', async (name) => {
    await fs.writeFile(path.join(root, name), 'Start with yy task start TASK_ID in another worktree.');
    await expect(planSimpleInit(root)).rejects.toThrow(/managed-lifecycle guidance/);
    expect(await fs.readFile(path.join(root, name), 'utf8')).toContain('yy task start');
    await expect(fs.stat(path.join(root, '.juno_task'))).rejects.toThrow();
  });

  it('refuses ignore rules hiding durable metadata rather than editing them', async () => {
    await fs.writeFile(path.join(root, '.gitignore'), '.juno_task/\n');
    await expect(planSimpleInit(root)).rejects.toThrow(/hide durable Simple data/);
    expect(await fs.readFile(path.join(root, '.gitignore'), 'utf8')).toBe('.juno_task/\n');
  });

  it('refuses existing metadata and symlinked root instructions', async () => {
    await fs.mkdir(path.join(root, '.juno_task'));
    await fs.writeFile(path.join(root, '.juno_task/untracked.txt'), 'preserve');
    await expect(planSimpleInit(root)).rejects.toThrow(/conflicts with fresh initialization/);
    expect(await fs.readFile(path.join(root, '.juno_task/untracked.txt'), 'utf8')).toBe('preserve');
    await fs.symlink('notebook.ipynb', path.join(root, 'AGENTS.md'));
    await expect(planSimpleInit(root)).rejects.toThrow(/non-symlink/);
  });

  it('refuses registered managed repos, nested directories and inherited unrelated authority', async () => {
    git('config', 'juno.controller.path', root);
    await expect(planSimpleInit(root)).rejects.toThrow(/Managed registration conflicts/);
    git('config', '--unset', 'juno.controller.path');
    const nested = path.join(root, 'notes'); await fs.mkdir(nested);
    await expect(planSimpleInit(nested)).rejects.toThrow(/Git top-level/);
    process.env.JUNO_TASK_ROOT = '/unrelated';
    await expect(planSimpleInit(root)).rejects.toThrow(/assertion mismatch/);
  });

  it('refuses linked worktrees and symlinked metadata without writing through them', async () => {
    const linked = `${root}-linked`;
    git('worktree', 'add', '--detach', linked);
    try { await expect(planSimpleInit(linked)).rejects.toThrow(/linked worktrees/); }
    finally { git('worktree', 'remove', linked); }
    const outside = await fs.mkdtemp(path.join(os.tmpdir(), 'yylo-simple-outside-'));
    try {
      await fs.symlink(outside, path.join(root, '.juno_task'));
      await expect(planSimpleInit(root)).rejects.toThrow(/regular local directory/);
      expect(await fs.readdir(outside)).toEqual([]);
    } finally { await fs.rm(outside, { recursive: true, force: true }); }
  });

  it('admits only one concurrent initializer and never overwrites the winning bytes', async () => {
    const plan = await planSimpleInit(root);
    const results = await Promise.allSettled([applySimpleInit(plan), applySimpleInit(plan)]);
    expect(results.filter((result) => result.status === 'fulfilled')).toHaveLength(1);
    expect((await planSimpleInit(root)).outcome).toBe('already-initialized');
    expect(await fs.readFile(path.join(root, 'notebook.ipynb'), 'utf8')).toBe('dirty notebook');
  });

  it('refuses stale plans after HEAD, config or preserved file changes', async () => {
    const plan = await planSimpleInit(root);
    await fs.writeFile(path.join(root, 'AGENTS.md'), 'new instruction');
    await expect(applySimpleInit(plan)).rejects.toThrow(/Stale or modified/);
    const next = await planSimpleInit(root);
    git('config', 'user.name', 'Changed');
    await expect(applySimpleInit(next)).rejects.toThrow(/Stale or modified/);
    const headPlan = await planSimpleInit(root);
    git('commit', '--allow-empty', '-qm', 'move head');
    await expect(applySimpleInit(headPlan)).rejects.toThrow(/Stale or modified/);
  });

  it('rejects tampered output paths and concurrent destination creation', async () => {
    const plan = await planSimpleInit(root);
    const bad = structuredClone(plan); bad.files['../notebook.ipynb'] = 'overwrite';
    await expect(applySimpleInit(bad)).rejects.toThrow(/Stale or modified/);
    await fs.mkdir(path.join(root, '.juno_task'));
    await expect(applySimpleInit(plan)).rejects.toThrow(/conflicts with fresh initialization/);
    expect(await fs.readFile(path.join(root, 'notebook.ipynb'), 'utf8')).toBe('dirty notebook');
  });

  it('preserves interrupted writes and blocks resolution until explicit recovery', async () => {
    const plan = await planSimpleInit(root);
    const write = fs.writeFile.bind(fs);
    vi.spyOn(fs, 'writeFile').mockImplementation(async (...args: Parameters<typeof fs.writeFile>) => {
      if (String(args[0]).endsWith('/.juno_task/config.json')) throw new Error('injected interrupted write');
      return write(...args);
    });
    await expect(applySimpleInit(plan)).rejects.toThrow(/injected/);
    vi.restoreAllMocks();
    expect(await fs.readFile(path.join(root, '.juno_task/simple-agent-guidance.md'), 'utf8')).toContain('Simple workspace');
    await expect(planSimpleInit(root)).rejects.toThrow(/Interrupted or active/);
    expect(() => resolveController(root)).toThrow(/incomplete or active Simple/);
    expect(await fs.readFile(path.join(root, 'notebook.ipynb'), 'utf8')).toBe('dirty notebook');
  });

  it('writes plans exclusively outside the project and dry-run preserves explicit apply', async () => {
    const plan = await planSimpleInit(root);
    await expect(writeSimpleInitPlan(path.join(root, 'plan.json'), plan)).rejects.toThrow(/outside/);
    const external = await fs.mkdtemp(path.join(os.tmpdir(), 'yylo-init-plan-'));
    try {
      const file = path.join(external, 'plan.json'); await writeSimpleInitPlan(file, plan);
      await expect(writeSimpleInitPlan(file, plan)).rejects.toThrow();
      const program = new Command(); configureInitCommand(program);
      const log = vi.spyOn(console, 'log').mockImplementation(() => undefined);
      await program.parseAsync(['init', '--mode', 'simple', '--dry-run', '--directory', root], { from: 'user' });
      expect(String(log.mock.calls[0][0])).toContain('Preview only; not initialized');
      await expect(fs.stat(path.join(root, '.juno_task'))).rejects.toThrow();
      const apply = new Command(); configureInitCommand(apply);
      await apply.parseAsync(['init', '--mode', 'simple', '--apply-plan', file], { from: 'user' });
      expect(log).toHaveBeenCalledWith(`Initialized Simple workspace: ${root}`);
    } finally { await fs.rm(external, { recursive: true, force: true }); }
  });
});
