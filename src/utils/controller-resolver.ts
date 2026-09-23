import { execFileSync } from 'node:child_process';
import * as path from 'node:path';
import { existsSync as requireExists, readFileSync, realpathSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { buildChildProcessEnvironment } from '../core/child-process-environment.js';

const resolverDirectory = path.dirname(fileURLToPath(import.meta.url));
const PACKAGED_CONTROLLER_RESOLVER = [
  path.resolve(resolverDirectory, '../templates/scripts/controller_resolver.py'), // source utils or bundled CLI
  path.resolve(resolverDirectory, 'templates/scripts/controller_resolver.py'), // bundled library
].find((candidate) => requireExists(candidate))
  ?? path.resolve(resolverDirectory, '../templates/scripts/controller_resolver.py');

export type ControllerOperation = 'diagnostic' | 'kanban' | 'orchestration' | 'session-write' | 'product-edit';
export type WorkspaceRole = 'controller' | 'controller-retired' | 'task' | 'integration-owner' | 'unregistered' | 'simple';

function discoveryDirectory(cwd: string): string {
  try { return realpathSync(cwd); } catch { return path.resolve(cwd); }
}

/** Discovery hint only. The installed resolver validates bytes and Git authority. */
export function hasSimpleWorkspaceHint(cwd: string): boolean {
  let directory = discoveryDirectory(cwd);
  while (true) {
    if (requireExists(path.join(directory, '.yylo-simple-init'))) return true;
    const marker = path.join(directory, '.juno_task/config.json');
    if (requireExists(marker)) {
      try {
        const raw = JSON.parse(readFileSync(marker, 'utf8'));
        const workspace = raw?.controllerWorkspace;
        if (workspace !== undefined && workspace?.mode !== 'metadata-only' && !(workspace?.mode === undefined && workspace?.enabled === true)) return true;
      } catch { return true; }
    }
    if (requireExists(path.join(directory, '.git')) || directory === path.dirname(directory)) return false;
    directory = path.dirname(directory);
  }
}

export interface ControllerResolution {
  path: string;
  current_root: string;
  resolver: 'installed' | 'missing';
  source: 'environment' | 'registration' | 'primary-worktree' | 'non-git-current-root' | 'current-root' | 'workspace-config';
  workspace_mode?: 'simple';
  workspace_version?: 1;
  invocation_cwd?: string;
  capabilities?: readonly string[];
  expected_branch: string | null;
  actual_branch: string | null;
  role: WorkspaceRole;
  enforcement: 'off' | 'warn' | 'strict';
  operation: ControllerOperation;
  valid: boolean;
  diagnostics: string[];
  controller_workspace?: { passed: boolean; checks: Record<string, boolean> } | null;
}

/** Invoke the installed shared resolver so wrappers, runners, and Node use one contract. */
export function resolveController(
  workingDirectory: string,
  operation: ControllerOperation = 'diagnostic',
  options: { ignoreEnvironmentAssertions?: boolean; trustedResolver?: boolean; env?: NodeJS.ProcessEnv } = {},
): ControllerResolution {
  const simpleHint = hasSimpleWorkspaceHint(workingDirectory);
  const trustedResolver = options.trustedResolver || simpleHint;
  let search = discoveryDirectory(workingDirectory);
  let resolver = trustedResolver
    ? PACKAGED_CONTROLLER_RESOLVER
    : path.join(search, '.juno_task', 'scripts', 'controller_resolver.py');
  while (!trustedResolver && !requireExists(resolver) && !requireExists(path.join(search, '.git')) && search !== path.dirname(search)) {
    search = path.dirname(search);
    resolver = path.join(search, '.juno_task', 'scripts', 'controller_resolver.py');
  }
  if (!requireExists(resolver)) {
    if (simpleHint) throw new Error('Installed Simple workspace resolver is missing. Repair the CLI package explicitly; refusing local-script or controller fallback.');
    const currentRoot = path.resolve(workingDirectory);
    return {
      path: currentRoot,
      current_root: currentRoot,
      resolver: 'missing',
      source: 'current-root',
      expected_branch: null,
      actual_branch: null,
      role: 'unregistered',
      enforcement: 'off',
      operation,
      valid: false,
      diagnostics: ['controller resolver is not installed; workspace is unmanaged'],
    };
  }
  const env = buildChildProcessEnvironment(options.env ?? process.env);
  if (options.ignoreEnvironmentAssertions && !simpleHint) {
    delete env.JUNO_TASK_ROOT;
    delete env.JUNO_CONTROLLER_BRANCH;
    delete env.JUNO_WORKSPACE_ROLE;
  }
  const output = execFileSync('python3', [resolver, '--cwd', workingDirectory, '--operation', operation], {
    cwd: workingDirectory,
    env,
    encoding: 'utf8',
    timeout: 15_000,
    maxBuffer: 1024 * 1024,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  return JSON.parse(output) as ControllerResolution;
}

export interface AutomaticProjectBootstrapPolicy {
  allowed: boolean;
  resolution: ControllerResolution;
  reason: 'controller' | 'resolver-missing' | 'non-controller-worktree' | 'sparse-controller-managed';
}

/**
 * Resolve whether implicit CLI startup may rewrite project-owned assets.
 *
 * The installed resolver is the single source of workspace identity. Startup
 * writes are allowed only in the exact resolved controller root. Task,
 * candidate, and integration-owner worktrees stay read-only until a user runs
 * an explicit scripts update command. Resolver failures are intentionally not
 * caught: invalid registration must stop startup before agent dispatch.
 */
export function resolveAutomaticProjectBootstrap(
  workingDirectory: string,
): AutomaticProjectBootstrapPolicy {
  // Persisted registration decides identity; environment is routing/assertion
  // only. A controller-context shell (the standard agent flow) carries
  // JUNO_WORKSPACE_ROLE into task worktrees, and asserting it there would
  // fatally misroute this read-only diagnostic before any command runs.
  const resolution = resolveController(workingDirectory, 'diagnostic', {
    ignoreEnvironmentAssertions: true,
  });
  if (resolution.resolver !== 'installed') {
    return { allowed: false, resolution, reason: 'resolver-missing' };
  }
  const controllerRoot = path.resolve(resolution.path);
  const currentRoot = path.resolve(resolution.current_root);
  if (resolution.role !== 'controller' || controllerRoot !== currentRoot) {
    return { allowed: false, resolution, reason: 'non-controller-worktree' };
  }
  // Sparse controllers are generation-pinned and verified by the resolver.
  // Implicit startup must not create an unexpected tracked/local expansion.
  if (resolution.controller_workspace?.passed) {
    return { allowed: false, resolution, reason: 'sparse-controller-managed' };
  }
  return { allowed: true, resolution, reason: 'controller' };
}

export function controllerEnvironment(
  workingDirectory: string,
  operation: ControllerOperation = 'orchestration',
): NodeJS.ProcessEnv {
  const resolution = resolveController(workingDirectory, operation);
  return buildChildProcessEnvironment(process.env, {
    JUNO_TASK_ROOT: resolution.path,
    JUNO_CONTROLLER_SOURCE: resolution.source,
    JUNO_WORKSPACE_ROLE: resolution.role,
  });
}
