import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import fs from 'fs-extra';
import os from 'node:os';
import path from 'node:path';
import { execFileSync, spawnSync } from 'node:child_process';
import {
  resolveAutomaticProjectBootstrap,
  resolveController,
} from '../controller-resolver.js';
import {
  classifyLeadingCommand,
  hasManagedWorkspaceMarker,
  routeControlPlane,
} from '../control-plane-router.js';

const resolverTemplate = path.resolve(process.cwd(), 'src/templates/scripts/controller_resolver.py');
const wrapperTemplate = path.resolve(process.cwd(), 'src/templates/scripts/kanban.sh');
const policyTemplate = path.resolve(process.cwd(), 'src/templates/scripts/juno-toolchain-policy.sh');
const controlIdentityNames = [
  'JUNO_CONTROL_INVOCATION_ROOT',
  'JUNO_CONTROL_INVOCATION_ROLE',
  'JUNO_CONTROL_EFFECTIVE_ROOT',
] as const;

type ControlIdentity = Partial<Record<(typeof controlIdentityNames)[number], string>>;

function withControlIdentity<T>(identity: ControlIdentity, operation: () => T): T {
  const previous = Object.fromEntries(controlIdentityNames.map((name) => [name, process.env[name]]));
  try {
    for (const name of controlIdentityNames) delete process.env[name];
    Object.assign(process.env, identity);
    return operation();
  } finally {
    for (const name of controlIdentityNames) {
      const value = previous[name];
      if (value === undefined) delete process.env[name];
      else process.env[name] = value;
    }
  }
}

function run(command: string, args: string[], cwd: string, env: NodeJS.ProcessEnv = {}) {
  const childEnv = { ...process.env };
  for (const name of controlIdentityNames) delete childEnv[name];
  return spawnSync(command, args, {
    cwd,
    encoding: 'utf8',
    env: {
      ...childEnv,
      JUNO_TASK_ROOT: '',
      JUNO_CONTROLLER_BRANCH: '',
      JUNO_WORKSPACE_ROLE: '',
      JUNO_WORKSPACE_ENFORCEMENT: '',
      ...env,
    },
  });
}

function git(cwd: string, ...args: string[]) {
  const result = run('git', args, cwd);
  expect(result.status, result.stderr).toBe(0);
  return result.stdout.trim();
}

describe('control-plane argv classification', () => {
  it('detects a managed marker from nested paths without classifying its parent', async () => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'yylo-managed-marker-'));
    try {
      execFileSync('git', ['-C', root, 'init', '-q']);
      const managed = path.join(root, 'managed');
      const nested = path.join(managed, 'nested');
      await fs.ensureDir(path.join(managed, '.juno_task'));
      await fs.ensureDir(nested);
      expect(hasManagedWorkspaceMarker(nested)).toBe(true);
      expect(hasManagedWorkspaceMarker(root)).toBe(false);
      const independent = path.join(managed, 'child');
      await fs.ensureDir(independent);
      execFileSync('git', ['-C', independent, 'init', '-q']);
      expect(hasManagedWorkspaceMarker(independent)).toBe(false);
    } finally {
      await fs.remove(root);
    }
  });

  it.each([
    { argv: ['--quiet', 'kanban', 'list'], command: 'kanban', index: 1 },
    { argv: ['--config', 'controller.json', '--no-color', 'task', 'status'], command: 'task', index: 3 },
    { argv: ['--verbose=2', 'merge', 'status'], command: 'merge', index: 1 },
    { argv: ['-v', '0', '--', 'doctor', 'workspace'], command: 'doctor', index: 3 },
  ])('finds $command after routing-safe global options', ({ argv, command, index }) => {
    expect(classifyLeadingCommand(argv)).toEqual({ command, index });
  });

  it('does not classify through unknown or variadic options', () => {
    expect(classifyLeadingCommand(['--tools', 'Read', 'kanban', 'list'])).toEqual({ command: '--tools', index: 0 });
  });
});

describe('control-plane operation gate', () => {
  it('validates the effective operation before diagnostic-only origin revalidation', () => {
    const controller = path.resolve('/controller');
    const task = path.resolve('/task');
    const calls: Array<{ cwd: string; operation: string; trusted: boolean | undefined }> = [];
    const resolver = ((cwd: string, operation: any, options: any) => {
      calls.push({ cwd: path.resolve(cwd), operation, trusted: options?.trustedResolver });
      const origin = path.resolve(cwd) === task;
      return {
        path: controller, current_root: origin ? task : controller, resolver: 'installed',
        source: 'registration', expected_branch: 'refs/heads/controller', actual_branch: 'controller',
        role: origin ? 'task' : 'controller', enforcement: 'strict', operation,
        valid: true, diagnostics: [],
      };
    }) as typeof resolveController;
    withControlIdentity({
      JUNO_CONTROL_INVOCATION_ROOT: task,
      JUNO_CONTROL_INVOCATION_ROLE: 'task',
      JUNO_CONTROL_EFFECTIVE_ROOT: controller,
    }, () => {
      expect(routeControlPlane(controller, 'orchestration', resolver)).toMatchObject({
        controllerRoot: controller, invocationRoot: task, invocationRole: 'task',
      });
      expect(calls).toEqual([
        { cwd: controller, operation: 'orchestration', trusted: true },
        { cwd: task, operation: 'diagnostic', trusted: true },
      ]);
    });
  });
});

describe('canonical controller resolver', () => {
  let sandbox: string;
  let controller: string;
  let task: string;

  beforeEach(async () => {
    sandbox = await fs.mkdtemp(path.join(os.tmpdir(), 'juno controller resolver '));
    controller = path.join(sandbox, 'controller checkout');
    task = path.join(sandbox, 'task checkout');
    await fs.ensureDir(path.join(controller, '.juno_task', 'scripts'));
    controller = await fs.realpath(controller);
    await fs.copy(resolverTemplate, path.join(controller, '.juno_task', 'scripts', 'controller_resolver.py'));
    await fs.copy(wrapperTemplate, path.join(controller, '.juno_task', 'scripts', 'kanban.sh'));
    await fs.copy(policyTemplate, path.join(controller, '.juno_task', 'scripts', 'juno-toolchain-policy.sh'));
    git(controller, 'init', '-b', 'controller-branch');
    git(controller, 'config', 'user.email', 'test@example.invalid');
    git(controller, 'config', 'user.name', 'Test');
    git(controller, 'add', '.juno_task');
    git(controller, 'commit', '-m', 'fixture');
    git(controller, 'worktree', 'add', '-b', 'feature-task', task);
    task = await fs.realpath(task);
    git(task, 'config', '--local', 'juno.controller.path', controller);
    git(task, 'config', '--local', 'juno.controller.branch', 'controller-branch');
    git(task, 'config', '--local', 'extensions.worktreeConfig', 'true');
    git(task, 'config', '--worktree', 'juno.workspace.role', 'task');
    git(task, 'config', '--worktree', 'juno.workspace.roleBase', git(task, 'rev-parse', 'HEAD'));
    git(task, 'config', '--worktree', 'juno.workspace.taskId', 'fixture-task');
    git(task, 'config', '--worktree', 'juno.workspace.manifestIdentity', 'a'.repeat(64));
    git(task, 'config', '--worktree', 'juno.workspace.createReceiptSha256', 'b'.repeat(64));
    git(task, 'config', '--worktree', 'juno.workspace.expectedPathsSha256', 'd'.repeat(64));
  });

  afterEach(async () => fs.remove(sandbox));

  it('fails closed for a linked worktree before persisted role registration', () => {
    git(task, 'config', '--worktree', '--unset-all', 'juno.workspace.role');
    git(task, 'config', '--worktree', '--unset-all', 'juno.workspace.taskId');
    git(task, 'config', '--worktree', '--unset-all', 'juno.workspace.manifestIdentity');
    const before = git(task, 'rev-parse', 'HEAD');
    const result = run('python3', [path.join(task, '.juno_task/scripts/controller_resolver.py'), '--cwd', task], task);
    expect(result.status).toBe(2);
    expect(result.stderr).toContain('linked worktree has no persisted workspace role registration');
    expect(git(task, 'rev-parse', 'HEAD')).toBe(before);
  });

  it('resolves a registered controller across a real linked worktree with spaces', () => {
    const result = run('python3', [path.join(task, '.juno_task/scripts/controller_resolver.py'), '--cwd', task], task);
    expect(result.status, result.stderr).toBe(0);
    expect(JSON.parse(result.stdout)).toMatchObject({ path: controller, current_root: task, resolver: 'installed', source: 'registration', actual_branch: 'controller-branch', role: 'task', valid: true });
  });

  it.each(['absent', 'empty'])('fails closed for %s controller registration on a persisted product role', (mode) => {
    git(task, 'config', '--local', '--unset-all', 'juno.controller.path');
    git(task, 'config', '--local', '--unset-all', 'juno.controller.branch');
    if (mode === 'empty') {
      git(task, 'config', '--local', 'juno.controller.path', '');
      git(task, 'config', '--local', 'juno.controller.branch', '');
    }
    const result = run(
      'python3',
      [path.join(task, '.juno_task/scripts/controller_resolver.py'), '--cwd', task],
      task,
    );
    expect(result.status).toBe(2);
    expect(JSON.parse(result.stdout)).toMatchObject({
      current_root: task,
      role: 'task',
      valid: false,
      diagnostics: [
        'linked product workspace requires exactly one non-empty controller path and branch registration',
      ],
    });
    expect(result.stderr.trim()).toBe(
      'controller-resolver: linked product workspace requires exactly one non-empty controller path and branch registration',
    );
  });

  it.each(['juno.controller.path', 'juno.controller.branch'])(
    'rejects duplicate %s registration values',
    (key) => {
      const value = key.endsWith('.path') ? controller : 'controller-branch';
      git(task, 'config', '--local', '--add', key, value);
      const result = run(
        'python3',
        [path.join(task, '.juno_task/scripts/controller_resolver.py'), '--cwd', task],
        task,
      );
      expect(result.status).toBe(2);
      expect(result.stderr).toContain(`controller registration is ambiguous: ${key} has multiple values`);
    },
  );

  it('accepts normalized full/short controller refs and routed audit identity', () => {
    git(task, 'config', '--local', 'juno.controller.branch', 'refs/heads/controller-branch');
    const result = run('python3', [path.join(task, '.juno_task/scripts/controller_resolver.py'), '--cwd', task], task, {
      JUNO_CONTROLLER_BRANCH: 'controller-branch',
    });
    expect(result.status, result.stderr).toBe(0);
    expect(JSON.parse(result.stdout)).toMatchObject({
      path: controller,
      expected_branch: 'refs/heads/controller-branch',
      actual_branch: 'controller-branch',
      valid: true,
    });
    const untrustedMarker = path.join(sandbox, 'untrusted-resolver-ran');
    fs.writeFileSync(
      path.join(task, '.juno_task/scripts/controller_resolver.py'),
      `#!/usr/bin/env python3\nfrom pathlib import Path\nPath(${JSON.stringify(untrustedMarker)}).write_text('ran')\nraise SystemExit(97)\n`,
    );
    // A typed parent may carry a different complete forwarding identity. This
    // fixture deliberately starts a fresh route and must not borrow that parent.
    const routed = withControlIdentity({
      JUNO_CONTROL_INVOCATION_ROOT: path.join(sandbox, 'ambient origin'),
      JUNO_CONTROL_INVOCATION_ROLE: 'integration-owner',
      JUNO_CONTROL_EFFECTIVE_ROOT: path.join(sandbox, 'ambient controller'),
    }, () => withControlIdentity({}, () => routeControlPlane(task, 'orchestration')));
    expect(routed).toMatchObject({
      controllerRoot: controller,
      invocationRoot: task,
      invocationRole: 'task',
    });
    expect(routed.env).toMatchObject({
      JUNO_TASK_ROOT: controller,
      JUNO_WORKSPACE_ROLE: 'controller',
      JUNO_WORKSPACE_ENFORCEMENT: 'strict',
      JUNO_CONTROLLER_BRANCH: 'refs/heads/controller-branch',
      JUNO_CONTROL_INVOCATION_ROOT: task,
      JUNO_CONTROL_INVOCATION_ROLE: 'task',
      JUNO_CONTROL_EFFECTIVE_ROOT: controller,
    });
    expect(fs.pathExistsSync(untrustedMarker)).toBe(false);
  });

  it('re-resolves forwarded routing identity and refuses incomplete, spoofed, or stale origins', async () => {
    withControlIdentity({
      JUNO_CONTROL_INVOCATION_ROOT: task,
      JUNO_CONTROL_INVOCATION_ROLE: 'task',
      JUNO_CONTROL_EFFECTIVE_ROOT: controller,
    }, () => {
      expect(routeControlPlane(controller, 'kanban')).toMatchObject({
        controllerRoot: controller, invocationRoot: task, invocationRole: 'task',
      });

      delete process.env.JUNO_CONTROL_INVOCATION_ROLE;
      expect(() => routeControlPlane(controller, 'kanban')).toThrow('Incomplete or mismatched');

      process.env.JUNO_CONTROL_INVOCATION_ROLE = 'integration-owner';
      expect(() => routeControlPlane(controller, 'kanban')).toThrow('no longer matches');

      process.env.JUNO_CONTROL_INVOCATION_ROLE = 'task';
      process.env.JUNO_CONTROL_INVOCATION_ROOT = path.join(sandbox, 'removed-origin');
      expect(() => routeControlPlane(controller, 'kanban')).toThrow('no longer matches');
    });
  });

  it('initializes controller audit authority once and exposes no public role assignment', async () => {
    const resolver = path.join(task, '.juno_task/scripts/controller_resolver.py');
    const initial = git(controller, 'rev-parse', 'HEAD');
    const registration = run('python3', [resolver, '--cwd', task, '--register', controller, '--branch', 'controller-branch'], task);
    expect(registration.status, registration.stderr).toBe(0);
    expect(git(controller, 'config', '--worktree', '--get', 'juno.workspace.roleBase')).toBe(initial);
    expect(run('git', ['config', '--worktree', '--get', 'juno.workspace.role'], controller).status).toBe(1);

    await fs.writeFile(path.join(controller, 'product.txt'), 'unaudited\n');
    git(controller, 'add', 'product.txt');
    git(controller, 'commit', '--no-verify', '-m', 'unaudited product commit');
    const unauthorized = git(controller, 'rev-parse', 'HEAD');
    expect(unauthorized).not.toBe(initial);

    const repeated = run('python3', [resolver, '--cwd', task, '--register', controller, '--branch', 'controller-branch'], task);
    expect(repeated.status, repeated.stderr).toBe(0);
    expect(git(controller, 'config', '--worktree', '--get', 'juno.workspace.roleBase')).toBe(initial);

    const obsolete = run('python3', [resolver, '--cwd', controller, '--register-workspace-role', 'controller'], controller);
    expect(obsolete.status).not.toBe(0);
    expect(obsolete.stderr).toContain('unrecognized arguments: --register-workspace-role controller');
    expect(git(controller, 'config', '--worktree', '--get', 'juno.workspace.roleBase')).toBe(initial);
    const help = run('python3', [resolver, '--help'], task);
    expect(help.status, help.stderr).toBe(0);
    expect(help.stdout).not.toContain('register-workspace-role');
  });

  it('refuses direct replacement of an existing controller registration', async () => {
    const replacement = path.join(sandbox, 'replacement controller');
    git(controller, 'worktree', 'add', '-b', 'replacement-controller', replacement);
    await fs.ensureDir(path.join(replacement, '.juno_task'));
    const result = run('python3', [path.join(task, '.juno_task/scripts/controller_resolver.py'),
      '--cwd', task, '--register', replacement, '--branch', 'replacement-controller'], task);
    expect(result.status).not.toBe(0);
    expect(result.stderr).toContain('yy migrate registration plan');
    expect(git(task, 'config', '--local', '--get', 'juno.controller.path')).toBe(controller);
  });

  it('allows implicit bootstrap only in the resolved controller and preserves resolver failure truth', () => {
    const names = ['JUNO_TASK_ROOT', 'JUNO_CONTROLLER_BRANCH', 'JUNO_WORKSPACE_ROLE', 'JUNO_WORKSPACE_ENFORCEMENT'] as const;
    const previous = Object.fromEntries(names.map((name) => [name, process.env[name]]));
    try {
      for (const name of names) process.env[name] = '';
      expect(resolveAutomaticProjectBootstrap(controller)).toMatchObject({
        allowed: true,
        reason: 'controller',
        resolution: { current_root: controller, role: 'controller' },
      });

      // Environment role is assertion-only: a controller-context shell inside a
      // task worktree must not fatally misroute the read-only diagnostic; the
      // persisted task topology still refuses implicit bootstrap.
      process.env.JUNO_WORKSPACE_ROLE = 'controller';
      expect(resolveAutomaticProjectBootstrap(task)).toMatchObject({
        allowed: false,
        reason: 'non-controller-worktree',
        resolution: { role: 'task', current_root: task },
      });
      process.env.JUNO_WORKSPACE_ROLE = '';

      git(task, 'config', '--local', 'juno.controller.path', path.join(sandbox, 'missing-controller'));
      expect(() => resolveAutomaticProjectBootstrap(task)).toThrow();
    } finally {
      for (const name of names) {
        const value = previous[name];
        if (value === undefined) delete process.env[name];
        else process.env[name] = value;
      }
    }
  });

  it('reports sole controller cleanliness drift in diagnostic mode but refuses orchestration', async () => {
    await fs.writeJson(path.join(controller, '.juno_task', 'config.json'), {
      controllerWorkspace: {
        enabled: true,
        policy: '.juno_task/config/controller-workspace.json',
      },
    });
    await fs.ensureDir(path.join(controller, '.juno_task', 'config'));
    await fs.writeJson(path.join(controller, '.juno_task/config/controller-workspace.json'), {});
    await fs.writeFile(path.join(controller, '.juno_task/scripts/controller_workspace.py'), `
def load_policy(_path): return {}
def inspect(root, _policy):
    return {'root': str(root), 'passed': False,
            'policy_identity': {'fixture': 'identity'},
            'checks': {'root_exact': True, 'clean': False}}
`);
    git(controller, 'add', '.juno_task');
    git(controller, 'commit', '-m', 'canonical sparse fixture');
    await fs.ensureDir(path.join(controller, '.juno_task/state'));
    const queueState = path.join(controller, '.juno_task/state/tasks.json');
    await fs.writeFile(queueState, '{}\n');
    git(controller, 'add', '.juno_task/state/tasks.json');
    git(controller, 'commit', '-m', 'queue state fixture');
    await fs.writeFile(queueState, '{"state":"reviewing"}\n');
    const resolver = path.join(controller, '.juno_task/scripts/controller_resolver.py');
    const diagnostic = run('python3', [resolver, '--cwd', controller, '--operation', 'diagnostic'], controller);
    expect(diagnostic.status, diagnostic.stderr).toBe(0);
    expect(JSON.parse(diagnostic.stdout)).toMatchObject({
      valid: true,
      diagnostics: ['canonical sparse controller policy refused: clean'],
      controller_workspace: { passed: false },
    });
    const orchestration = run('python3', [resolver, '--cwd', controller, '--operation', 'orchestration'], controller);
    expect(orchestration.status).toBe(2);
    expect(orchestration.stderr).toContain('canonical sparse controller policy refused: clean');
    const retiredBinding = {
      schema_version: 'juno_managed_controller_binding.v1',
      root: controller,
      queue_state: [{ path: '.juno_task/state/tasks.json' }],
    };
    const bound = run('python3', [resolver, '--cwd', controller, '--operation', 'orchestration'], controller, {
      JUNO_MANAGED_CONTROLLER_BINDING_JSON: JSON.stringify(retiredBinding),
    });
    expect(bound.status).toBe(2);
    expect(bound.stderr).toContain('canonical sparse controller policy refused: clean');
  });

  it('keeps missing-resolver fallback unmanaged and unavailable to automatic bootstrap', async () => {
    const project = path.join(sandbox, 'unmanaged git checkout');
    await fs.ensureDir(project);
    git(project, 'init');

    expect(resolveController(project)).toMatchObject({
      path: project,
      current_root: project,
      resolver: 'missing',
      role: 'unregistered',
      valid: false,
    });
    expect(resolveAutomaticProjectBootstrap(project)).toMatchObject({
      allowed: false,
      reason: 'resolver-missing',
      resolution: { role: 'unregistered', valid: false },
    });
  });

  it('keeps an initialized non-Git project local despite an inherited controller route', async () => {
    const project = path.join(sandbox, 'initialized non-git project');
    await fs.ensureDir(path.join(project, '.juno_task', 'scripts'));
    await fs.copy(resolverTemplate, path.join(project, '.juno_task', 'scripts', 'controller_resolver.py'));

    const result = run(
      'python3',
      [path.join(project, '.juno_task/scripts/controller_resolver.py'), '--cwd', project],
      project,
      { JUNO_TASK_ROOT: controller, JUNO_WORKSPACE_ROLE: 'controller' },
    );

    expect(result.status, result.stderr).toBe(0);
    expect(JSON.parse(result.stdout)).toMatchObject({
      path: await fs.realpath(project),
      current_root: await fs.realpath(project),
      source: 'non-git-current-root',
      role: 'controller',
      role_source: 'non-git-current-root',
      valid: true,
    });
  });

  it('treats explicit roots as assertions and never falls back from invalid or unrelated roots', async () => {
    const explicit = run('python3', [path.join(task, '.juno_task/scripts/controller_resolver.py'), '--cwd', task], task, { JUNO_TASK_ROOT: controller });
    expect(JSON.parse(explicit.stdout).source).toBe('registration');
    const invalid = run('python3', [path.join(task, '.juno_task/scripts/controller_resolver.py'), '--cwd', task], task, { JUNO_TASK_ROOT: path.join(sandbox, 'missing') });
    expect(invalid.status).toBe(2);
    expect(invalid.stderr).toContain('assertion mismatch');

    const unrelated = path.join(sandbox, 'unrelated controller');
    await fs.ensureDir(path.join(unrelated, '.juno_task'));
    git(unrelated, 'init', '-b', 'controller-branch');
    const wrongRepository = run(
      'python3',
      [path.join(task, '.juno_task/scripts/controller_resolver.py'), '--cwd', task],
      task,
      { JUNO_TASK_ROOT: unrelated, JUNO_CONTROLLER_BRANCH: 'controller-branch' },
    );
    expect(wrongRepository.status).toBe(2);
    expect(wrongRepository.stderr).toContain('assertion mismatch');
  });

  it('rejects stale and wrong-branch registrations without changing either HEAD', () => {
    git(task, 'config', '--local', 'juno.controller.branch', 'wrong-branch');
    const beforeTask = git(task, 'rev-parse', 'HEAD');
    const beforeController = git(controller, 'rev-parse', 'HEAD');
    const wrong = run('python3', [path.join(task, '.juno_task/scripts/controller_resolver.py'), '--cwd', task], task);
    expect(wrong.status).toBe(2);
    expect(wrong.stderr).toContain('branch mismatch');
    git(task, 'config', '--local', 'juno.controller.path', path.join(sandbox, 'gone'));
    const missing = run('python3', [path.join(task, '.juno_task/scripts/controller_resolver.py'), '--cwd', task], task);
    expect(missing.status).toBe(2);
    expect(git(task, 'rev-parse', 'HEAD')).toBe(beforeTask);
    expect(git(controller, 'rev-parse', 'HEAD')).toBe(beforeController);
  });

  it('supports warn and strict integration-owner enforcement', () => {
    git(task, 'config', '--worktree', 'juno.workspace.role', 'integration-owner');
    git(task, 'config', '--worktree', 'juno.workspace.roleAuthority', 'protected-integration.v1');
    for (const key of ['taskId', 'manifestIdentity', 'createReceiptSha256', 'expectedPathsSha256']) {
      run('git', ['config', '--worktree', '--unset-all', `juno.workspace.${key}`], task);
    }
    const warn = run('python3', [path.join(task, '.juno_task/scripts/controller_resolver.py'), '--cwd', task, '--operation', 'orchestration'], task, { JUNO_WORKSPACE_ROLE: 'integration-owner', JUNO_WORKSPACE_ENFORCEMENT: 'warn' });
    expect(warn.status).toBe(0);
    expect(warn.stderr).toContain('warning');
    const strict = run('python3', [path.join(task, '.juno_task/scripts/controller_resolver.py'), '--cwd', task, '--operation', 'session-write'], task, { JUNO_WORKSPACE_ROLE: 'integration-owner', JUNO_WORKSPACE_ENFORCEMENT: 'strict' });
    expect(strict.status).toBe(2);
    expect(strict.stderr).toContain('refuses session-write');
  });

  it('routes the task wrapper mutation process to controller storage', async () => {
    const bin = path.join(controller, '.venv_juno', 'bin');
    const policy = await fs.readFile(policyTemplate, 'utf8');
    const ledgerVersion = policy.match(/^YYLO_LEDGER_COMPAT_RANGE='([^']+)'/m)?.[1];
    expect(ledgerVersion).toBeDefined();
    await fs.ensureDir(bin);
    await fs.writeFile(path.join(bin, 'activate'), `export VIRTUAL_ENV=${JSON.stringify(path.join(controller, '.venv_juno'))}\nexport PATH=${JSON.stringify(bin)}:$PATH\n`);
    await fs.writeFile(path.join(bin, 'yylo-ledger'), `#!/usr/bin/env python3\nimport os,sys\nif sys.argv[1:] == ["--version"]: print("yylo-ledger ${ledgerVersion}")\nelse: print(os.environ["JUNO_TASK_ROOT"] + "|" + " ".join(sys.argv[1:]))\n`);
    await fs.chmod(path.join(bin, 'yylo-ledger'), 0o755);
    const result = run(path.join(task, '.juno_task/scripts/kanban.sh'), ['mark', 'done', '--id', 'ABC123'], task);
    expect(result.status, result.stderr).toBe(0);
    expect(result.stdout.trim()).toBe(
      `${controller}|--config ${path.join(controller, '.juno_task', 'config.json')} mark done --id ABC123`,
    );
    expect(await fs.pathExists(path.join(task, '.venv_juno'))).toBe(false);
  });
});
