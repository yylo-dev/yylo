import { spawnSync } from 'node:child_process';
import fs from 'fs-extra';
import os from 'node:os';
import path from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';

const engine = path.resolve(process.cwd(), 'src/templates/scripts/controller_workspace.py');
const git = (cwd: string, ...args: string[]) => spawnSync('git', args, { cwd, encoding: 'utf8' });
const run = (cwd: string, ...args: string[]) => spawnSync('python3', [engine, ...args], {
  cwd, encoding: 'utf8', env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' },
});

const rootIgnores = ['/AGENTS.md', '/CLAUDE.md', '/.agents/', '/.claude/', '/.pi/'];

describe('metadata-controller sparse policy', () => {
  let temporary = '';

  afterEach(async () => {
    if (temporary) await fs.remove(temporary);
  });

  it('classifies native Records and canonical task scopes as recursive controller metadata', async () => {
    const policy = await fs.readJson(path.resolve(process.cwd(), 'src/templates/config/metadata-controller.json'));
    const nativeRecordRoots = [
      '.juno_task/archive', '.juno_task/archive-receipts',
      '.juno_task/artifact-ledger', '.juno_task/artifacts',
      '.juno_task/document-ledger', '.juno_task/documents',
      '.juno_task/objects',
    ];
    for (const recordRoot of nativeRecordRoots) {
      expect(policy.copied_metadata).toContain(recordRoot);
      expect(policy.product_forbidden).toContain(recordRoot);
      expect(policy.tracked_recursive).toContain(recordRoot);
      expect(policy.tracked_exact).not.toContain(recordRoot);
    }
    expect(policy.copied_metadata).toContain('.juno_task/task-scopes');
    expect(policy.product_forbidden).toContain('.juno_task/task-scopes');
    expect(policy.tracked_recursive).toContain('.juno_task/task-scopes');
    expect(policy.tracked_exact).not.toContain('.juno_task/task-scopes');
    expect(policy.copied_metadata).toContain('.juno_task/config/umbrella-admissions');
    expect(policy.product_forbidden).toContain('.juno_task/config/umbrella-admissions');
    expect(policy.tracked_recursive).toContain('.juno_task/config/umbrella-admissions');
  });

  it('classifies, selects, requires, and materializes the generated .gitignore', async () => {
    temporary = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-controller-sparse-policy-'));
    const repository = path.join(temporary, 'repository');
    await fs.ensureDir(repository);
    expect(git(repository, 'init', '-q', '-b', 'controller').status).toBe(0);
    git(repository, 'config', 'user.name', 'Test');
    git(repository, 'config', 'user.email', 'test@example.invalid');
    await fs.writeFile(path.join(repository, '.gitignore'), `${rootIgnores.join('\n')}\n`);
    await fs.outputFile(path.join(repository, '.juno_task/config.json'), '{}\n');
    await fs.writeFile(path.join(repository, 'product.txt'), 'product must stay sparse\n');
    git(repository, 'add', '.'); git(repository, 'commit', '-qm', 'controller tree');
    const head = git(repository, 'rev-parse', 'HEAD').stdout.trim();
    const policy = {
      schema_version: 'juno_controller_workspace.v1',
      controller_branch: 'refs/heads/controller',
      ownership: {
        schema_version: 'juno_workspace_ownership.v1',
        controller_canonical: ['.gitignore', '.juno_task'],
        shared_managed_distribution: ['shared'],
        product_canonical: ['product.txt'],
        local_ignored: ['.agents', '.claude', '.pi', 'AGENTS.md', 'CLAUDE.md'],
      },
      sparse_policy: {
        style: 'non-cone', index_sparse: false,
        selected_paths: ['.gitignore', '.juno_task'],
        required_paths: ['.gitignore', '.juno_task/config.json'],
      },
      generation: { package_name: '@yylo/cli', package_version: '2.1.2', managed_assets_schema: 1 },
    };
    const policyPath = path.join(temporary, 'policy.json');
    await fs.writeJson(policyPath, policy);
    const controller = path.join(temporary, 'sparse-controller');
    const receipt = path.join(temporary, 'create.json');
    const created = run(repository, '--policy', policyPath, 'create', '--repository', repository,
      '--path', controller, '--controller-ref', 'refs/heads/controller', '--expected-head', head,
      '--registration-source', 'test', '--rollback-controller', repository, '--output', receipt);
    expect(created.status, created.stderr).toBe(0);
    const evidence = (await fs.readJson(receipt)).evidence;
    expect(evidence.checks.gitignore_materialized).toBe(true);
    expect(evidence.checks.root_agent_ignores).toBe(true);
    expect(evidence.checks.agent_surface_untracked).toBe(true);
    expect(await fs.pathExists(path.join(controller, '.gitignore'))).toBe(true);
    expect(await fs.pathExists(path.join(controller, 'product.txt'))).toBe(false);

    policy.ownership.controller_canonical = ['.juno_task'];
    policy.ownership.product_canonical = ['.gitignore', 'product.txt'];
    const invalidPath = path.join(temporary, 'invalid-policy.json');
    await fs.writeJson(invalidPath, policy);
    const invalid = run(repository, '--policy', invalidPath, 'classify', '.gitignore');
    expect(invalid.status).toBe(2);
    expect(invalid.stderr).toContain('.gitignore must be controller_canonical');
  });

  it('admits legacy task-scope records without admitting unknown controller paths', async () => {
    temporary = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-controller-task-scopes-'));
    const repository = path.join(temporary, 'repository');
    await fs.ensureDir(repository);
    expect(git(repository, 'init', '-q', '-b', 'controller').status).toBe(0);
    git(repository, 'config', 'user.name', 'Test');
    git(repository, 'config', 'user.email', 'test@example.invalid');
    await fs.writeFile(path.join(repository, '.gitignore'), `${rootIgnores.join('\n')}\n`);
    await fs.outputFile(path.join(repository, '.juno_task/config.json'), '{"revision":1}\n');
    const scope = '.juno_task/task-scopes/ab/abc123.json';
    await fs.outputFile(path.join(repository, scope), '{"task_id":"abc123"}\n');
    git(repository, 'add', '.'); git(repository, 'commit', '-qm', 'legacy controller tree');
    const head = git(repository, 'rev-parse', 'HEAD').stdout.trim();
    // Leave the controller branch available for the sparse linked worktree.
    expect(git(repository, 'switch', '-qc', 'product').status).toBe(0);
    const policy = {
      schema_version: 'juno_controller_workspace.v1',
      controller_branch: 'refs/heads/controller',
      ownership: {
        schema_version: 'juno_workspace_ownership.v1',
        controller_canonical: ['.gitignore', '.juno_task/config.json'],
        shared_managed_distribution: ['shared'],
        product_canonical: ['product'],
        local_ignored: ['.agents', '.claude', '.pi', 'AGENTS.md', 'CLAUDE.md'],
      },
      sparse_policy: {
        style: 'non-cone', index_sparse: false,
        selected_paths: ['.gitignore', '.juno_task/config.json'],
        required_paths: ['.gitignore', '.juno_task/config.json'],
      },
      generation: { package_name: '@yylo/cli', package_version: '2.1.3', managed_assets_schema: 1 },
    };
    const policyPath = path.join(temporary, 'policy.json');
    await fs.writeJson(policyPath, policy);
    const controller = path.join(temporary, 'sparse-controller');
    const created = run(repository, '--policy', policyPath, 'create', '--repository', repository,
      '--path', controller, '--controller-ref', 'refs/heads/controller', '--expected-head', head,
      '--registration-source', 'test', '--rollback-controller', repository,
      '--output', path.join(temporary, 'create.json'));
    expect(created.status, created.stderr).toBe(0);
    expect(git(controller, 'switch', 'controller').status).toBe(0);

    // Reproduce a previously tracked scope becoming materialized while a new
    // task-state mutation is committed by the controller checkpoint.
    await fs.outputFile(path.join(controller, scope), '{"task_id":"abc123"}\n');
    await fs.writeFile(path.join(controller, '.juno_task/config.json'), '{"revision":2}\n');
    expect(git(controller, 'add', '--sparse', scope, '.juno_task/config.json').status).toBe(0);
    expect(git(controller, 'commit', '-qm', 'checkpoint task state').status).toBe(0);
    const verified = run(controller, '--policy', policyPath, 'verify', '--root', controller,
      '--output', path.join(temporary, 'verify.json'));
    const verification = await fs.readJson(path.join(temporary, 'verify.json'));
    expect(verified.status, `${verified.stderr}\n${JSON.stringify(verification, null, 2)}`).toBe(0);
    const classified = run(controller, '--policy', policyPath, 'classify', scope);
    expect(classified.status, classified.stderr).toBe(0);
    expect(JSON.parse(classified.stdout)[scope]).toBe('controller_canonical');
    const unknown = run(controller, '--policy', policyPath, 'classify', '.juno_task/unknown/value.json');
    expect(unknown.status).toBe(2);
    expect(unknown.stderr).toContain('exactly one ownership class');
  });
});
