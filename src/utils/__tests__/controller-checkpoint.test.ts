import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import fs from 'fs-extra';
import * as os from 'node:os';
import * as path from 'node:path';
import { createHash } from 'node:crypto';
import { spawn, spawnSync } from 'node:child_process';

const helper = path.resolve(process.cwd(), 'src/templates/scripts/controller_checkpoint.py');

function run(repo: string, ...args: string[]) {
  return runWithEnv(repo, {}, ...args);
}

function runWithEnv(repo: string, env: NodeJS.ProcessEnv, ...args: string[]) {
  return spawnSync('python3', [helper, '--root', repo, ...args], {
    encoding: 'utf8',
    env: { ...process.env, JUNO_TASK_ROOT: '', JUNO_CONTROLLER_BRANCH: '', JUNO_WORKSPACE_ROLE: '', ...env },
  });
}

function git(repo: string, ...args: string[]) {
  const result = spawnSync('git', ['-C', repo, ...args], { encoding: 'utf8' });
  expect(result.status, result.stderr).toBe(0);
  return result.stdout.trim();
}

describe('controller_checkpoint.py template script', () => {
  let testDir: string;
  let repo: string;

  beforeEach(async () => {
    testDir = await fs.mkdtemp(path.join(os.tmpdir(), 'controller-checkpoint-'));
    repo = path.join(testDir, 'repo');
    git(testDir, 'init', '-b', 'task', repo);
    git(repo, 'config', 'user.email', 'fixture@example.invalid');
    git(repo, 'config', 'user.name', 'Fixture');
    await fs.ensureDir(path.join(repo, '.juno_task', 'tasks'));
    await fs.writeFile(path.join(repo, '.juno_task', 'config.json'), '{}\n');
    await fs.writeFile(path.join(repo, '.juno_task', 'tasks', 'one.md'), 'initial\n');
    await fs.writeFile(path.join(repo, 'product.txt'), 'initial\n');
    git(repo, 'add', '.juno_task/config.json', '.juno_task/tasks/one.md', 'product.txt');
    git(repo, 'commit', '-m', 'initial');
  });

  afterEach(async () => fs.remove(testDir));

  it('commits only explicit allowlisted controller files and supports a clean no-op', async () => {
    await fs.writeFile(path.join(repo, '.juno_task', 'tasks', 'one.md'), 'durable\n');
    const result = run(repo, 'commit', '--message', 'chore(controller): fixture');
    expect(result.status, result.stderr).toBe(0);
    expect(git(repo, 'show', '--name-only', '--format=', 'HEAD')).toBe('.juno_task/tasks/one.md');
    expect(git(repo, 'status', '--porcelain')).toBe('');
    const noOp = run(repo, 'commit', '--message', 'chore(controller): no-op', '--json');
    expect(noOp.status, noOp.stderr).toBe(0);
    expect(JSON.parse(noOp.stdout).outcome).toBe('noop');
  });

  it('selects workflows and the managed-assets manifest under legacy fallback defaults', async () => {
    await fs.ensureDir(path.join(repo, '.juno_task', 'workflows'));
    await fs.writeFile(path.join(repo, '.juno_task', 'workflows', 'run.json'), '{}\n');
    await fs.writeFile(path.join(repo, '.juno_task', 'managed-assets.json'), '{}\n');
    const result = run(repo, 'plan', '--json');
    expect(result.status, result.stderr).toBe(0);
    const expected = [
      '.juno_task/managed-assets.json',
      '.juno_task/workflows/run.json',
    ];
    expect(JSON.parse(result.stdout).selected).toEqual(expected);

    await fs.writeJson(path.join(repo, '.juno_task', 'config.json'), {
      gitCheckpoint: {
        include: [
          '.juno_task/tasks',
          '.juno_task/ledger',
          '.juno_task/wiki',
          '.juno_task/specs',
          '.juno_task/workflows',
          '.juno_task/plan.md',
          '.juno_task/tasks.md',
          '.juno_task/managed-assets.json',
        ],
      },
    });
    git(repo, 'add', '.juno_task/config.json');
    git(repo, 'commit', '-m', 'explicit migrated config');
    const explicit = run(repo, 'plan', '--json');
    expect(explicit.status, explicit.stderr).toBe(0);
    expect(JSON.parse(explicit.stdout).selected).toEqual(expected);
  });

  async function configureMetadataController(): Promise<void> {
    const policy = await fs.readJson(path.resolve(process.cwd(), 'src/templates/config/metadata-controller.json'));
    policy.controller_branch = 'refs/heads/task';
    policy.product_ref = 'refs/heads/product';
    await fs.ensureDir(path.join(repo, '.juno_task', 'config'));
    await fs.writeJson(path.join(repo, '.juno_task', 'config', 'metadata-controller.json'), policy);
    await fs.writeJson(path.join(repo, '.juno_task', 'config.json'), {
      controllerWorkspace: { mode: 'metadata-only', policy: '.juno_task/config/metadata-controller.json' },
    });
    git(repo, 'add', '.juno_task/config.json', '.juno_task/config/metadata-controller.json');
    git(repo, 'commit', '-m', 'configure metadata boundary');
  }

  it('derives metadata checkpoint paths from policy and commits canonical queue state', async () => {
    await configureMetadataController();
    await fs.outputJson(path.join(repo, '.juno_task', 'state', 'tasks.json'), {
      schema_version: 1, tasks: { A: { state: 'QUEUED' } },
    });
    git(repo, 'add', '.juno_task/state/tasks.json');
    git(repo, 'commit', '-m', 'canonical queue state');
    await fs.outputJson(path.join(repo, '.juno_task', 'state', 'tasks.json'), {
      schema_version: 1, tasks: { A: { state: 'MERGED' } },
    });
    const result = run(repo, 'commit', '--message', 'checkpoint queue state');
    expect(result.status, result.stderr).toBe(0);
    expect(git(repo, 'show', '--name-only', '--format=', 'HEAD')).toBe('.juno_task/state/tasks.json');
  });

  it('preserves an admitted legacy broad include list and replays a successful checkpoint as a no-op', async () => {
    await configureMetadataController();
    const legacyInclude = [
      '.gitignore', '.agents', '.claude', '.juno_task/USER_FEEDBACK.md', '.juno_task/config',
      '.juno_task/config.json', '.juno_task/ledger', '.juno_task/managed-assets.json',
      '.juno_task/prompts', '.juno_task/receipts', '.juno_task/specs', '.juno_task/state',
      '.juno_task/tasks', '.juno_task/tasks.md', '.juno_task/wiki', '.juno_task/workflows',
      '.pi', 'AGENTS.md', 'CLAUDE.md',
    ];
    const config = await fs.readJson(path.join(repo, '.juno_task', 'config.json'));
    config.gitCheckpoint = { include: legacyInclude };
    await fs.writeJson(path.join(repo, '.juno_task', 'config.json'), config);
    await fs.outputJson(path.join(repo, '.juno_task', 'state', 'tasks.json'), {
      schema_version: 1, tasks: { A: { state: 'QUEUED' } },
    });
    await fs.outputFile(path.join(repo, '.juno_task', 'prompts', 'managed.md'), 'initial\n');
    await fs.outputFile(path.join(repo, '.juno_task', 'workflows', 'managed.json'), '{}\n');
    await fs.outputFile(path.join(repo, '.juno_task', 'wiki', 'legacy.md'), 'initial\n');
    await fs.outputJson(path.join(repo, '.juno_task', 'managed-assets.json'), { schema_version: 1 });
    await fs.outputJson(path.join(repo, '.juno_task', 'config', 'controller-workspace.json'), {
      schema_version: 'retired-policy-that-must-not-be-reloaded',
    });
    await fs.outputFile(path.join(repo, '.agents', 'managed.md'), 'initial\n');
    git(repo, 'add', '-f', '.');
    git(repo, 'commit', '-m', 'legacy broad metadata controller');

    await fs.outputJson(path.join(repo, '.juno_task', 'state', 'tasks.json'), {
      schema_version: 1, tasks: { A: { state: 'MERGED' } },
    });
    const lifecycle = run(repo, '--task-id', 'A', 'commit', '--message', 'checkpoint task lifecycle', '--json');
    expect(lifecycle.status, lifecycle.stderr).toBe(0);
    expect(JSON.parse(lifecycle.stdout).selected).toEqual(['.juno_task/state/tasks.json']);
    const replay = run(repo, '--task-id', 'A', 'commit', '--message', 'checkpoint replay', '--json');
    expect(replay.status, replay.stderr).toBe(0);
    expect(JSON.parse(replay.stdout).outcome).toBe('noop');

    await fs.writeFile(path.join(repo, '.juno_task', 'prompts', 'managed.md'), 'updated\n');
    await fs.writeFile(path.join(repo, '.juno_task', 'wiki', 'legacy.md'), 'updated\n');
    await fs.writeFile(path.join(repo, '.agents', 'managed.md'), 'updated\n');
    const result = run(repo, 'commit', '--message', 'checkpoint legacy surfaces', '--json');
    expect(result.status, result.stderr).toBe(0);
    expect(JSON.parse(result.stdout).selected).toEqual([
      '.agents/managed.md', '.juno_task/prompts/managed.md', '.juno_task/wiki/legacy.md',
    ]);
  });

  it('reports deterministic read-only recovery for a missing canonical include', async () => {
    await configureMetadataController();
    const configPath = path.join(repo, '.juno_task', 'config.json');
    const config = await fs.readJson(configPath);
    config.gitCheckpoint = { include: ['.juno_task/tasks', '.juno_task/ledger'] };
    await fs.writeJson(configPath, config);
    git(repo, 'add', '.juno_task/config.json');
    git(repo, 'commit', '-m', 'legacy incomplete checkpoint selection');
    const before = await fs.readFile(configPath);
    const head = git(repo, 'rev-parse', 'HEAD');

    const result = run(repo, 'plan');
    expect(result.status).toBe(2);
    expect(result.stderr).toContain("missing required canonical roots: ['.juno_task/state/tasks.json']");
    expect(result.stderr).toContain('safe_next_action=add ".juno_task/state/tasks.json"');
    expect(await fs.readFile(configPath)).toEqual(before);
    expect(git(repo, 'rev-parse', 'HEAD')).toBe(head);
    expect(git(repo, 'diff', '--cached', '--name-only')).toBe('');
  });

  it('rejects configured product, unknown, and symlink roots before selection with policy diagnostics', async () => {
    await configureMetadataController();
    const configPath = path.join(repo, '.juno_task', 'config.json');
    const required = ['.juno_task/tasks', '.juno_task/ledger', '.juno_task/state/tasks.json'];
    for (const invalid of ['product.txt', '.juno_task/unknown']) {
      const config = await fs.readJson(configPath);
      config.gitCheckpoint = { include: [...required, invalid] };
      await fs.writeJson(configPath, config);
      const result = run(repo, 'plan');
      expect(result.status).toBe(2);
      expect(result.stderr).toContain(`${invalid} (reason=outside_controller_boundary, rule=metadata_controller:tracked_path_classes)`);
      expect(git(repo, 'diff', '--cached', '--name-only')).toBe('');
    }
    await fs.ensureDir(path.join(repo, '.juno_task', 'links'));
    await fs.symlink(testDir, path.join(repo, '.juno_task', 'links', 'escape'));
    const config = await fs.readJson(configPath);
    config.gitCheckpoint = { include: [...required, '.juno_task/links/escape'] };
    await fs.writeJson(configPath, config);
    const symlink = run(repo, 'plan');
    expect(symlink.status).toBe(2);
    expect(symlink.stderr).toContain('unsafe symlink path: .juno_task/links/escape');
    expect(git(repo, 'diff', '--cached', '--name-only')).toBe('');
  });

  it('task-scoped queue attribution excludes task B and refuses shared dirt', async () => {
    await configureMetadataController();
    const queue = path.join(repo, '.juno_task', 'state', 'tasks.json');
    await fs.outputJson(queue, { schema_version: 1, tasks: {
      A: { state: 'QUEUED' }, B: { state: 'QUEUED' },
    } });
    git(repo, 'add', '.juno_task/state/tasks.json');
    git(repo, 'commit', '-m', 'canonical queue state');
    await fs.outputJson(queue, { schema_version: 1, tasks: {
      A: { state: 'MERGED' }, B: { state: 'QUEUED' },
    } });
    let result = run(repo, '--task-id', 'A', 'commit', '--message', 'checkpoint task A');
    expect(result.status, result.stderr).toBe(0);

    await fs.outputJson(queue, { schema_version: 1, tasks: {
      A: { state: 'MERGED' }, B: { state: 'MERGED' },
    } });
    await fs.outputFile(path.join(repo, '.juno_task', 'specs', 'unrelated.md'), 'shared dirt\n');
    result = run(repo, '--task-id', 'A', 'plan');
    expect(result.status).toBe(2);
    expect(result.stderr).toMatch(/queue attribution refused|blocked non-controller paths/);
    expect(git(repo, 'diff', '--cached', '--name-only')).toBe('');
  });

  it('queue-attributed receipt admits queue-owned shared fields and multi-task mutations', async () => {
    await configureMetadataController();
    await fs.writeFile(path.join(repo, '.gitignore'), '/.juno_task/runtime/\n');
    git(repo, 'add', '.gitignore');
    git(repo, 'commit', '-m', 'ignore controller runtime');
    const queue = path.join(repo, '.juno_task', 'state', 'tasks.json');
    const canonical = { schema_version: 1, tasks: {
      A: { state: 'QUEUED' }, B: { state: 'QUEUED' },
    }, queues: { task_workspace_fifo: {
      schema_version: 'juno_task_workspace_fifo.v1', next: 4,
    } } };
    await fs.outputJson(queue, canonical);
    git(repo, 'add', '.juno_task/state/tasks.json');
    git(repo, 'commit', '-m', 'canonical queue state');
    await fs.outputJson(queue, { schema_version: 1, tasks: {
      A: { state: 'MERGED', queue_attempt: { candidate_sha: 'c' }, last_queue_outcome: 'MERGED' },
      B: { state: 'MERGED', last_queue_outcome: 'MERGED' },
    }, queues: { task_workspace_fifo: {
      schema_version: 'juno_task_workspace_fifo.v1', next: 5,
    } } });
    const digest = createHash('sha256').update(await fs.readFile(queue)).digest('hex');
    const receiptPath = path.join(repo, '.juno_task', 'runtime', 'controller-checkpoint', 'queue-attribution.json');
    await fs.outputJson(receiptPath, {
      schema_version: 'juno_checkpoint_queue_attribution.v1',
      producer: 'task_workspace.write_state',
      task_ids: ['A', 'B'],
      shared_fields: ['queues.task_workspace_fifo.next'],
      queue_document_sha256: digest,
    });
    await fs.outputFile(path.join(repo, '.juno_task', 'ledger', 'b', 'B', '000001.ndjson'), '{}\n');
    const result = run(repo, '--task-id', 'A', 'commit', '--message', 'checkpoint merged projection', '--json');
    expect(result.status, result.stderr).toBe(0);
    const payload = JSON.parse(result.stdout);
    expect(payload.outcome).toBe('committed');
    expect(payload.selected).toEqual([
      '.juno_task/ledger/b/B/000001.ndjson',
      '.juno_task/state/tasks.json',
    ]);
    expect(git(repo, 'show', '--name-only', '--format=', 'HEAD').split('\n').filter(Boolean))
      .toEqual(['.juno_task/ledger/b/B/000001.ndjson', '.juno_task/state/tasks.json']);
    // The attribution chain resets with the durable queue commit.
    expect(await fs.pathExists(receiptPath)).toBe(false);
    expect(git(repo, 'status', '--porcelain')).toBe('');
  });

  it('queue-attributed receipt admits path-keyed doctor shared fields', async () => {
    await configureMetadataController();
    await fs.writeFile(path.join(repo, '.gitignore'), '/.juno_task/runtime/\n');
    git(repo, 'add', '.gitignore');
    git(repo, 'commit', '-m', 'ignore controller runtime');
    const queue = path.join(repo, '.juno_task', 'state', 'tasks.json');
    // Real queue documents key the managed-runtime doctor report by exact
    // identity: script leaves carry absolute paths (empty leading dotted
    // segment plus slashes) and toolchain leaves carry colon-bearing keys.
    const doctorKey = '.juno_task/scripts/controller_checkpoint.py';
    const toolchainKey = 'python:3.13';
    const queueKey = '15ecaf9a9ce6b646';
    const doctorSection = (value: string) => ({
      last_attempt: { managed_runtime_refresh: { doctor: {
        scripts: { [doctorKey]: { actual_sha256: value } },
        toolchains: { [toolchainKey]: { actual_sha256: value } },
      } } },
    });
    const doctorLeaf = (section: string) =>
      `queues.${queueKey}.last_attempt.managed_runtime_refresh.doctor.${section}`;
    await fs.outputJson(queue, { schema_version: 1, tasks: {
      A: { state: 'QUEUED' },
    }, queues: { [queueKey]: doctorSection('0'.repeat(64)) } });
    git(repo, 'add', '.juno_task/state/tasks.json');
    git(repo, 'commit', '-m', 'canonical queue state');
    await fs.outputJson(queue, { schema_version: 1, tasks: {
      A: { state: 'MERGED', last_queue_outcome: 'MERGED' },
    }, queues: { [queueKey]: doctorSection('1'.repeat(64)) } });
    const digest = createHash('sha256').update(await fs.readFile(queue)).digest('hex');
    const receiptPath = path.join(repo, '.juno_task', 'runtime', 'controller-checkpoint', 'queue-attribution.json');
    await fs.outputJson(receiptPath, {
      schema_version: 'juno_checkpoint_queue_attribution.v1',
      producer: 'task_workspace.write_state',
      task_ids: ['A'],
      shared_fields: [
        `${doctorLeaf('scripts')}.${doctorKey}.actual_sha256`,
        `${doctorLeaf('toolchains')}.${toolchainKey}.actual_sha256`,
      ],
      queue_document_sha256: digest,
    });
    const result = run(repo, '--task-id', 'A', 'commit', '--message',
      'checkpoint drained queue projection', '--json');
    expect(result.status, result.stderr).toBe(0);
    const payload = JSON.parse(result.stdout);
    expect(payload.outcome).toBe('committed');
    expect(payload.selected).toEqual(['.juno_task/state/tasks.json']);
    expect(await fs.pathExists(receiptPath)).toBe(false);
    expect(git(repo, 'status', '--porcelain')).toBe('');

    // Section-boundary truth survives: tasks-rooted, unknown-root, and
    // whitespace/control-bearing shared fields keep the receipt invalid.
    for (const invalid of ['tasks.A.state', 'schema_version', 'unknown.root',
      'queues.doctor report', 'queues\u0000fifo', 'queues\n']) {
      await fs.outputJson(queue, { schema_version: 1, tasks: {
        A: { state: 'MERGED', last_queue_outcome: 'MERGED', queue_attempt: { candidate_sha: 'c' } },
      }, queues: { [queueKey]: doctorSection('2'.repeat(64)) } });
      const stale = createHash('sha256').update(await fs.readFile(queue)).digest('hex');
      await fs.outputJson(receiptPath, {
        schema_version: 'juno_checkpoint_queue_attribution.v1',
        producer: 'task_workspace.write_state',
        task_ids: ['A'],
        shared_fields: [invalid],
        queue_document_sha256: stale,
      });
      const refused = run(repo, '--task-id', 'A', 'plan');
      expect(refused.status).toBe(2);
      expect(refused.stderr).toMatch(/task-scoped queue attribution refused/);
      expect(refused.stderr).toContain('no queue attribution receipt');
      expect(git(repo, 'diff', '--cached', '--name-only')).toBe('');
      spawnSync('git', ['-C', repo, 'checkout', '--', '.juno_task/state/tasks.json']);
      await fs.remove(receiptPath);
    }
  }, 30_000);

  it('refuses queue-attributed mutations that escape the receipt binding', async () => {
    await configureMetadataController();
    await fs.writeFile(path.join(repo, '.gitignore'), '/.juno_task/runtime/\n');
    git(repo, 'add', '.gitignore');
    git(repo, 'commit', '-m', 'ignore controller runtime');
    const queue = path.join(repo, '.juno_task', 'state', 'tasks.json');
    const receiptPath = path.join(repo, '.juno_task', 'runtime', 'controller-checkpoint', 'queue-attribution.json');
    const seed = async () => {
      spawnSync('git', ['-C', repo, 'checkout', '--', '.juno_task/state/tasks.json']);
      await fs.outputJson(queue, { schema_version: 1, tasks: {
        A: { state: 'QUEUED' }, B: { state: 'QUEUED' },
      }, queues: { task_workspace_fifo: {
        schema_version: 'juno_task_workspace_fifo.v1', next: 4,
      } } });
      if (git(repo, 'status', '--porcelain=v1', '--', '.juno_task/state/tasks.json')) {
        git(repo, 'add', '.juno_task/state/tasks.json');
        git(repo, 'commit', '-m', 'canonical queue state');
      }
    };
    const bind = async (mutation: Record<string, unknown>) => {
      await fs.outputJson(queue, mutation);
      const digest = createHash('sha256').update(await fs.readFile(queue)).digest('hex');
      return digest;
    };

    // Digest binding: a receipt naming older bytes cannot admit current dirt.
    await seed();
    const stale = await bind({ schema_version: 1, tasks: {
      A: { state: 'MERGED' }, B: { state: 'QUEUED' },
    }, queues: { task_workspace_fifo: {
      schema_version: 'juno_task_workspace_fifo.v1', next: 5,
    } } });
    await fs.outputJson(queue, { schema_version: 1, tasks: {
      A: { state: 'MERGED' }, B: { state: 'QUEUED' },
    }, queues: { task_workspace_fifo: {
      schema_version: 'juno_task_workspace_fifo.v1', next: 6,
    } } });
    await fs.outputJson(receiptPath, {
      schema_version: 'juno_checkpoint_queue_attribution.v1',
      producer: 'task_workspace.write_state',
      task_ids: ['A'],
      shared_fields: ['queues.task_workspace_fifo.next'],
      queue_document_sha256: stale,
    });
    let result = run(repo, '--task-id', 'A', 'plan');
    expect(result.status).toBe(2);
    expect(result.stderr).toContain('receipt digest does not bind the current queue document bytes');
    expect(git(repo, 'diff', '--cached', '--name-only')).toBe('');

    // Transition-field confinement: another task may only move queue fields.
    await seed();
    const escaped = await bind({ schema_version: 1, tasks: {
      A: { state: 'MERGED', last_queue_outcome: 'MERGED' },
      B: { state: 'MERGED', worktree: '/elsewhere' },
    }, queues: { task_workspace_fifo: {
      schema_version: 'juno_task_workspace_fifo.v1', next: 5,
    } } });
    await fs.outputJson(receiptPath, {
      schema_version: 'juno_checkpoint_queue_attribution.v1',
      producer: 'task_workspace.write_state',
      task_ids: ['A', 'B'],
      shared_fields: ['queues.task_workspace_fifo.next'],
      queue_document_sha256: escaped,
    });
    result = run(repo, '--task-id', 'A', 'plan');
    expect(result.status).toBe(2);
    expect(result.stderr).toContain('task B record delta escapes queue transition fields');
    expect(git(repo, 'diff', '--cached', '--name-only')).toBe('');

    // Exact set truth: a receipt may not attribute tasks that did not change.
    await seed();
    await bind({ schema_version: 1, tasks: {
      A: { state: 'MERGED', last_queue_outcome: 'MERGED' }, B: { state: 'QUEUED' },
    }, queues: { task_workspace_fifo: {
      schema_version: 'juno_task_workspace_fifo.v1', next: 5,
    } } });
    const digest = createHash('sha256').update(await fs.readFile(queue)).digest('hex');
    await fs.outputJson(receiptPath, {
      schema_version: 'juno_checkpoint_queue_attribution.v1',
      producer: 'task_workspace.write_state',
      task_ids: ['A', 'B'],
      shared_fields: ['queues.task_workspace_fifo.next'],
      queue_document_sha256: digest,
    });
    result = run(repo, '--task-id', 'A', 'plan');
    expect(result.status).toBe(2);
    expect(result.stderr).toContain("receipt task set ['A', 'B'] does not match the observed change");
    expect(git(repo, 'diff', '--cached', '--name-only')).toBe('');
  });

  it('task-scoped selection commits umbrella-declared child scope files', async () => {
    await configureMetadataController();
    await fs.outputJson(path.join(repo, '.juno_task', 'task-scopes', 'u1', 'U1.json'), {
      schema_version: 'juno_task_canonical_scope.v1', task_id: 'U1',
      umbrella_relations: { owner: null, children: ['C1', 'C2'] },
      scope: { baseline: true, selectable_paths: [], required_paths: [], generated_paths: [] },
    });
    await fs.outputJson(path.join(repo, '.juno_task', 'task-scopes', 'c1', 'C1.json'), {
      schema_version: 'juno_task_canonical_scope.v1', task_id: 'C1',
    });
    await fs.outputJson(path.join(repo, '.juno_task', 'task-scopes', 'c2', 'C2.json'), {
      schema_version: 'juno_task_canonical_scope.v1', task_id: 'C2',
    });
    // An undeclared sibling scope file stays unadmitted controller residue.
    await fs.outputJson(path.join(repo, '.juno_task', 'task-scopes', 'z9', 'Z9.json'), {
      schema_version: 'juno_task_canonical_scope.v1', task_id: 'Z9',
    });
    const result = run(repo, '--task-id', 'U1', 'commit', '--message', 'checkpoint umbrella scopes', '--json');
    expect(result.status, result.stderr).toBe(0);
    const payload = JSON.parse(result.stdout);
    expect(payload.outcome).toBe('committed');
    expect(payload.selected).toEqual([
      '.juno_task/task-scopes/c1/C1.json',
      '.juno_task/task-scopes/c2/C2.json',
      '.juno_task/task-scopes/u1/U1.json',
    ]);
    expect(git(repo, 'show', '--name-only', '--format=', 'HEAD').split('\n').filter(Boolean))
      .toEqual(['.juno_task/task-scopes/c1/C1.json', '.juno_task/task-scopes/c2/C2.json',
                '.juno_task/task-scopes/u1/U1.json']);
    expect(git(repo, 'status', '--porcelain')).toContain('.juno_task/task-scopes/z9/');
  });

  it('task-scoped checkpoints exclude another task namespace residue', async () => {
    await fs.ensureDir(path.join(repo, '.juno_task', 'tasks', 't1'));
    await fs.ensureDir(path.join(repo, '.juno_task', 'ledger', 't1', 'T1'));
    await fs.writeFile(path.join(repo, '.juno_task', 'tasks', 't1', 'T1.md'), 'one\n');
    await fs.writeFile(path.join(repo, '.juno_task', 'tasks', 't2', 'T2.md'), 'two\n').catch(async () => {
      await fs.ensureDir(path.join(repo, '.juno_task', 'tasks', 't2'));
      await fs.writeFile(path.join(repo, '.juno_task', 'tasks', 't2', 'T2.md'), 'two\n');
    });
    const scoped = run(repo, '--task-id', 'T1', 'plan', '--json');
    expect(scoped.status, scoped.stderr).toBe(0);
    expect(JSON.parse(scoped.stdout).selected).toEqual(['.juno_task/tasks/t1/T1.md']);
    expect(git(repo, 'status', '--porcelain')).toContain('.juno_task/tasks/t2/');
  });

  it('uses metadata-controller direct-child rules and reports every refused nested path', async () => {
    await configureMetadataController();
    for (const name of ['one.json', 'two.md']) {
      await fs.outputFile(path.join(repo, '.juno_task', 'specs', 'backend', 'artifacts', name), 'evidence\n');
    }
    const before = git(repo, 'rev-parse', 'HEAD');
    const result = run(repo, 'commit', '--message', 'must refuse nested evidence');
    expect(result.status).toBe(2);
    for (const name of ['one.json', 'two.md']) {
      expect(result.stderr).toContain(`.juno_task/specs/backend/artifacts/${name}`);
    }
    expect(result.stderr).toContain('reason=unattributed_nested_path');
    expect(result.stderr).toContain('rule=tracked_top_level_files:.juno_task/specs:direct_children_only');
    expect(git(repo, 'rev-parse', 'HEAD')).toBe(before);
    expect(git(repo, 'diff', '--cached', '--name-only')).toBe('');
  });

  it('preserves unrelated untracked output while committing only eligible metadata', async () => {
    await configureMetadataController();
    const bytes = 'bash: s1034-battery.sh: No such file or directory\n';
    await fs.writeFile(path.join(repo, 's1034-out.txt'), bytes);
    await fs.writeFile(path.join(repo, '.juno_task', 'tasks', 'one.md'), 'controller\n');
    const result = run(repo, 'commit', '--message', 'checkpoint selected metadata', '--json');
    expect(result.status, result.stderr).toBe(0);
    const payload = JSON.parse(result.stdout);
    expect(payload.excluded).toEqual([expect.objectContaining({
      path: 's1034-out.txt', reason: 'untracked_outside_checkpoint_selection',
    })]);
    expect(git(repo, 'show', '--name-only', '--format=', 'HEAD')).toBe('.juno_task/tasks/one.md');
    expect(await fs.readFile(path.join(repo, 's1034-out.txt'), 'utf8')).toBe(bytes);
    expect(git(repo, 'status', '--porcelain')).toBe('?? s1034-out.txt');
    expect(git(repo, 'diff', '--cached', '--name-only')).toBe('');
    expect(run(repo, 'require-clean').status).toBe(2);
    expect(JSON.parse(run(repo, 'commit', '--json').stdout).outcome).toBe('noop');
  });

  it('never stages unselected untracked symlinks or follows their targets', async () => {
    await fs.symlink('/absent/external', path.join(repo, 'unrelated-link'));
    await fs.writeFile(path.join(repo, '.juno_task/tasks/one.md'), 'changed\n');
    const result = run(repo, 'commit', '--json');
    expect(result.status, result.stderr).toBe(0);
    expect(await fs.readlink(path.join(repo, 'unrelated-link'))).toBe('/absent/external');
    expect(git(repo, 'ls-files', 'unrelated-link')).toBe('');
  });

  it('blocks dirty tracked product paths and leaves both worktree and index untouched', async () => {
    await fs.writeFile(path.join(repo, '.juno_task', 'tasks', 'one.md'), 'controller\n');
    await fs.writeFile(path.join(repo, 'product.txt'), 'product\n');
    const before = git(repo, 'rev-parse', 'HEAD');
    const result = run(repo, 'commit', '--message', 'must not commit');
    expect(result.status).toBe(2);
    expect(result.stderr).toContain('blocked non-controller paths');
    expect(git(repo, 'rev-parse', 'HEAD')).toBe(before);
    expect(git(repo, 'diff', '--cached', '--name-only')).toBe('');
  });

  it('rejects staged or alternate-index work, detached HEAD, conflicts, and unsafe allowlist entries', async () => {
    await fs.writeFile(path.join(repo, '.juno_task', 'tasks', 'one.md'), 'staged\n');
    git(repo, 'add', '.juno_task/tasks/one.md');
    expect(run(repo, 'plan').stderr).toContain('pre-existing staged');
    git(repo, 'restore', '--staged', '.juno_task/tasks/one.md');
    expect(runWithEnv(repo, { GIT_INDEX_FILE: path.join(testDir, 'alternate-index') }, 'plan').stderr).toContain('alternate GIT_INDEX_FILE');
    git(repo, 'restore', '.juno_task/tasks/one.md');
    const attachedHead = git(repo, 'rev-parse', 'HEAD');
    await fs.writeFile(path.join(repo, '.git', 'HEAD'), `${attachedHead}\n`);
    expect(run(repo, 'plan').stderr).toContain('named branch');
    await fs.writeFile(path.join(repo, '.git', 'HEAD'), 'ref: refs/heads/task\n');
    await fs.writeJson(path.join(repo, '.juno_task', 'config.json'), { gitCheckpoint: { include: ['../escape'] } });
    expect(run(repo, 'plan').stderr).toContain('unsafe allowlist');
  });

  it('parses porcelain-v2 renames and commits only the renamed controller path', async () => {
    git(repo, 'mv', '.juno_task/tasks/one.md', '.juno_task/tasks/renamed.md');
    git(repo, 'restore', '--staged', '.juno_task/tasks/one.md', '.juno_task/tasks/renamed.md');
    const result = run(repo, 'commit', '--message', 'chore(controller): rename fixture');
    expect(result.status, result.stderr).toBe(0);
    expect(git(repo, 'show', '--name-status', '--format=', 'HEAD')).toMatch(/R\d+\s+\.juno_task\/tasks\/one\.md\s+\.juno_task\/tasks\/renamed\.md/);
  });

  it('rejects symlink escapes and nested repositories', async () => {
    await fs.mkdir(path.join(testDir, 'outside'));
    await fs.symlink(path.join(testDir, 'outside'), path.join(repo, '.juno_task', 'tasks', 'link'));
    await fs.writeFile(path.join(testDir, 'outside', 'bad.md'), 'bad\n');
    expect(run(repo, 'plan').stderr).toContain('symlink');
    await fs.remove(path.join(repo, '.juno_task', 'tasks', 'link'));
    await fs.ensureDir(path.join(repo, '.juno_task', 'tasks', 'nested'));
    git(path.join(repo, '.juno_task', 'tasks'), 'init', path.join(repo, '.juno_task', 'tasks', 'nested'));
    await fs.writeFile(path.join(repo, '.juno_task', 'tasks', 'nested', 'x'), 'x');
    expect(run(repo, 'plan').stderr).toContain('nested repository');
  });

  it('quarantines a high-confidence stale empty index lock before inspection', async () => {
    const lock = path.join(repo, '.git', 'index.lock');
    await fs.writeFile(lock, '');
    const old = new Date(Date.now() - 10 * 60 * 1000);
    await fs.utimes(lock, old, old);
    const plan = run(repo, 'plan', '--json');
    expect(plan.status).toBe(2);
    expect(await fs.pathExists(lock)).toBe(true);
    const result = run(repo, 'commit', '--message', 'chore(controller): stale lock fixture', '--json');
    expect(result.status, result.stderr).toBe(0);
    expect(await fs.pathExists(lock)).toBe(false);
    const quarantines = (await fs.readdir(path.join(repo, '.git'))).filter((name) =>
      name.startsWith('index.lock.stale.'),
    );
    expect(quarantines).toHaveLength(1);
  }, 30_000);

  it('rejects submodule dirt and an existing index lock', async () => {
    await fs.writeFile(path.join(repo, '.git', 'index.lock'), 'busy');
    expect(run(repo, 'plan').stderr).toContain('index.lock');
    await fs.remove(path.join(repo, '.git', 'index.lock'));
    const child = path.join(testDir, 'child');
    git(testDir, 'init', '-b', 'main', child);
    git(child, 'config', 'user.email', 'fixture@example.invalid');
    git(child, 'config', 'user.name', 'Fixture');
    await fs.writeFile(path.join(child, 'tracked'), 'initial\n');
    git(child, 'add', 'tracked');
    git(child, 'commit', '-m', 'child initial');
    git(repo, '-c', 'protocol.file.allow=always', 'submodule', 'add', child, 'juno_kanban');
    git(repo, 'commit', '-m', 'add child submodule');
    await fs.writeFile(path.join(repo, 'juno_kanban', 'tracked'), 'dirty\n');
    expect(run(repo, 'plan').stderr).toContain('dirty submodule state');
  });

  it('removes only checkpoint-owned staging when commit fails', async () => {
    await fs.writeFile(path.join(repo, '.juno_task', 'tasks', 'one.md'), 'must remain unstaged\n');
    const bin = path.join(testDir, 'bin');
    await fs.ensureDir(bin);
    const wrapper = path.join(bin, 'git');
    await fs.writeFile(wrapper, `#!/bin/sh\nfor arg in "$@"; do test "$arg" = commit && exit 73; done\nexec ${JSON.stringify(spawnSync('which', ['git'], { encoding: 'utf8' }).stdout.trim())} "$@"\n`);
    await fs.chmod(wrapper, 0o755);
    const result = runWithEnv(repo, { PATH: `${bin}:${process.env.PATH}` }, 'commit', '--message', 'must fail');
    expect(result.status).toBe(2);
    expect(git(repo, 'diff', '--cached', '--name-only')).toBe('');
    expect(git(repo, 'status', '--porcelain')).toContain('.juno_task/tasks/one.md');
  });

  it('fails promptly when the repository lease is held', async () => {
    const lock = path.join(repo, '.git', 'juno-repository-writer.lock');
    const holder = spawn('python3', ['-c', 'import fcntl,sys,time; f=open(sys.argv[1],"a+"); fcntl.flock(f,fcntl.LOCK_EX); print("ready",flush=True); time.sleep(10)', lock], { stdio: ['ignore', 'pipe', 'ignore'] });
    await new Promise<void>((resolve) => holder.stdout!.once('data', () => resolve()));
    const result = run(repo, 'plan');
    holder.kill();
    expect(result.status).toBe(2);
    expect(result.stderr).toContain('lease busy');
  });

  const agentEnv = (command: string) => ({
    ...process.env,
    JUNO_TASK_ROOT: '',
    JUNO_CONTROLLER_BRANCH: '',
    JUNO_CONTROLLER_CHECKPOINT_ACTIVE: '',
    JUNO_WORKSPACE_ROLE: '',
    JUNO_CHECKPOINT_AGENT_COMMAND: command,
  });

  it('accepts valid agent grouping while deterministic code owns commits', async () => {
    await fs.writeFile(path.join(repo, '.juno_task', 'tasks', 'one.md'), 'agent grouped\n');
    const command = path.join(testDir, 'agent-ok.py');
    await fs.writeFile(command, `#!/usr/bin/env python3\nimport json\nprint(json.dumps({"schema_version":"juno_controller_checkpoint_agent.v1","groups":[{"paths":[".juno_task/tasks/one.md"],"message":"chore(controller): grouped fixture"}]}))\n`);
    await fs.chmod(command, 0o755);
    const result = spawnSync('python3', [helper, '--root', repo, 'commit', '--agent', '--json'], { encoding: 'utf8', env: agentEnv(command) });
    expect(result.status, result.stderr).toBe(0);
    expect(JSON.parse(result.stdout).outcome).toBe('committed');
    expect(git(repo, 'log', '-1', '--format=%s')).toBe('chore(controller): grouped fixture');
  });

  it('rejects an agent that mutates the frozen repository state', async () => {
    await fs.writeFile(path.join(repo, '.juno_task', 'tasks', 'one.md'), 'before agent\n');
    const command = path.join(testDir, 'agent-mutate.py');
    await fs.writeFile(command, `#!/usr/bin/env python3\nimport json, pathlib\npathlib.Path('.juno_task/tasks/one.md').write_text('mutated by agent\\n')\nprint(json.dumps({"schema_version":"juno_controller_checkpoint_agent.v1","groups":[{"paths":[".juno_task/tasks/one.md"],"message":"bad"}]}))\n`);
    await fs.chmod(command, 0o755);
    const result = spawnSync('python3', [helper, '--root', repo, 'commit', '--agent'], { encoding: 'utf8', env: agentEnv(command) });
    expect(result.status).toBe(2);
    expect(result.stderr).toContain('content changed');
    expect(git(repo, 'diff', '--cached', '--name-only')).toBe('');
  });

  it('rejects invalid, incomplete, timed-out, or mutating agent proposals', async () => {
    await fs.writeFile(path.join(repo, '.juno_task', 'tasks', 'one.md'), 'agent\n');
    const command = path.join(testDir, 'agent.py');
    await fs.writeFile(command, '#!/usr/bin/env python3\nprint("not json")\n');
    await fs.chmod(command, 0o755);
    let result = spawnSync('python3', [helper, '--root', repo, 'commit', '--agent'], { encoding: 'utf8', env: agentEnv(command) });
    expect(result.status).toBe(2);
    expect(result.stderr).toContain('agent proposal');
    await fs.writeFile(command, `#!/usr/bin/env python3\nimport json\nprint(json.dumps({"schema_version":"juno_controller_checkpoint_agent.v1","groups":[]}))\n`);
    result = spawnSync('python3', [helper, '--root', repo, 'commit', '--agent'], { encoding: 'utf8', env: agentEnv(command) });
    expect(result.status).toBe(2);
    expect(result.stderr).toContain('exactly once');
    await fs.writeFile(command, '#!/usr/bin/env python3\nimport time\ntime.sleep(2)\n');
    await fs.writeJson(path.join(repo, '.juno_task', 'config.json'), { gitCheckpoint: { agent: { timeoutSeconds: 1 } } });
    git(repo, 'add', '.juno_task/config.json');
    git(repo, 'commit', '-m', 'configure timeout', '--', '.juno_task/config.json');
    result = spawnSync('python3', [helper, '--root', repo, 'commit', '--agent'], { encoding: 'utf8', env: agentEnv(command) });
    expect(result.status).toBe(2);
    expect(result.stderr).toContain('timed out');
  });

  it('defers only sparse clean-state verification while committing and performs strict readback', async () => {
    const checkpoint = await fs.readFile(helper, 'utf8');
    expect(checkpoint).toContain('allow_pending_changes: bool = False');
    expect(checkpoint).toContain('allow_pending_changes and key == "clean"');
    expect(checkpoint).toContain('payload["sparse_controller_readback"] = require_sparse_controller(root, allow_pending_changes=True)');
    expect(checkpoint).toContain('role_source": "registered-sparse-checkpoint"');
    expect(checkpoint).toContain('sparse checkpoint root is not the exact registered controller');
    expect(checkpoint).not.toContain('if not evidence["passed"]:\n        failed = sorted');
  });

  it('keeps checkpoints at outer finalizers and out of target-ref integration', async () => {
    const main = await fs.readFile(path.resolve(process.cwd(), 'src/cli/commands/main.ts'), 'utf8');
    expect(main.indexOf('clearContinueScopeRunning')).toBeLessThan(main.lastIndexOf('checkpointControllerAfterFinalization'));
    const workflow = await fs.readFile(path.resolve(process.cwd(), 'src/templates/scripts/workflow_runner.sh'), 'utf8');
    expect(workflow).toContain('checkpoint_after_finalization(exit_code, "workflow")');
    expect(workflow).toContain('completed.stderr or completed.stdout');
    expect(workflow).toContain('[REDACTED]');
    expect(workflow).toContain('detail[-2000:]');
    expect(workflow).toContain('legacy local_integration execution is read-only');
    expect(workflow).toContain('yy task start TASK_ID');
    expect(workflow).not.toContain('env["JUNO_WORKFLOW_DIRECT_OWNER"]');
    expect(workflow).not.toContain('integration_command_text.index("--checkpoint-controller")');
    const parallel = await fs.readFile(path.resolve(process.cwd(), 'src/templates/scripts/parallel_runner.sh'), 'utf8');
    expect(parallel).toContain('finally:\n        # main returns only after task/session summaries');
    const checkpoint = await fs.readFile(path.resolve(process.cwd(), 'src/templates/scripts/controller_checkpoint.py'), 'utf8');
    expect(checkpoint).toContain('juno-integration-channels');
    expect(checkpoint).toContain('target channel lock timeout');
  });
});
