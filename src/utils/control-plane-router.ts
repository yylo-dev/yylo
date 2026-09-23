import path from 'node:path';
import { existsSync } from 'node:fs';
import type { ControllerOperation, ControllerResolution, WorkspaceRole } from './controller-resolver.js';
import { resolveController } from './controller-resolver.js';
import { buildChildProcessEnvironment } from '../core/child-process-environment.js';

export type RoutedControlPlane = {
  controllerRoot: string;
  invocationRoot: string;
  invocationRole: WorkspaceRole;
  resolution: ControllerResolution;
  env: NodeJS.ProcessEnv;
};

const FLAG_GLOBAL_OPTIONS = new Set([
  '-q', '--quiet', '--silent', '--no-color', '--enable-feedback', '--no-hooks', '--no-hook',
]);
const VALUE_GLOBAL_OPTIONS = new Set([
  '-c', '--config', '-l', '--log-file', '--log-level', '-s', '--subagent', '-b', '--backend',
  '-m', '--model', '--agents', '--mcp-timeout', '-r', '--resume', '--stale-threshold',
  '--on-hourly-limit', '--thinking',
]);
const OPTIONAL_VERBOSE_VALUES = new Set(['0', '1', '2', 'true', 'false', 'yes', 'no']);

/** Return true when the invocation is nested beneath Juno's managed marker. */
export function hasManagedWorkspaceMarker(workingDirectory: string): boolean {
  let current = path.resolve(workingDirectory);
  while (true) {
    if (existsSync(path.join(current, '.juno_task'))) return true;
    const parent = path.dirname(current);
    if (existsSync(path.join(current, '.git')) || parent === current) return false;
    current = parent;
  }
}

/** Return the first command after routing-safe global options, without mutating argv. */
export function classifyLeadingCommand(argv: readonly string[]): { command?: string; index: number } {
  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];
    if (!token) continue;
    if (token === '--') {
      const command = argv[index + 1];
      return command === undefined ? { index: argv.length } : { command, index: index + 1 };
    }
    if (FLAG_GLOBAL_OPTIONS.has(token)) continue;
    if (VALUE_GLOBAL_OPTIONS.has(token)) {
      if (index + 1 >= argv.length) return { index: argv.length };
      index += 1;
      continue;
    }
    if ([...VALUE_GLOBAL_OPTIONS].some((option) => token.startsWith(`${option}=`))) continue;
    if (token === '-v' || token === '--verbose') {
      if (OPTIONAL_VERBOSE_VALUES.has(argv[index + 1]?.toLowerCase() ?? '')) index += 1;
      continue;
    }
    if (token.startsWith('--verbose=') || token.startsWith('-v=')) continue;
    return { command: token, index };
  }
  return { index: argv.length };
}

/**
 * Resolve an explicitly supported control-plane operation without weakening the
 * resolver's direct-write policy for the invoking product checkout.
 */
export function routeControlPlane(
  workingDirectory: string,
  operation: ControllerOperation,
  resolver: typeof resolveController = resolveController,
  capability?: 'local-task-bookkeeping',
): RoutedControlPlane {
  // Diagnostic resolution validates persisted topology without pretending that
  // the product checkout itself is performing the eventual controller write.
  const resolution = resolver(workingDirectory, operation, {
    ignoreEnvironmentAssertions: true,
    trustedResolver: true,
  });
  const controllerRoot = path.resolve(resolution.path);
  if (resolution.role === 'simple') {
    if (!resolution.valid || resolution.resolver !== 'installed' || capability !== 'local-task-bookkeeping' || operation !== 'kanban') {
      throw new Error('Simple workspace does not support managed task/merge/integration operations. Use `yy task local` or `yy ledger` for bookkeeping; completion is not managed delivery.');
    }
    return {
      controllerRoot,
      invocationRoot: path.resolve(resolution.current_root),
      invocationRole: 'simple',
      resolution,
      env: buildChildProcessEnvironment(process.env, {
        JUNO_TASK_ROOT: controllerRoot,
        JUNO_WORKSPACE_ROLE: 'simple',
        JUNO_WORKSPACE_ENFORCEMENT: 'strict',
      }),
    };
  }
  let invocationRoot = path.resolve(resolution.current_root);
  let invocationRole: WorkspaceRole = resolution.role;
  const forwardedRoot = process.env.JUNO_CONTROL_INVOCATION_ROOT?.trim();
  const forwardedRole = process.env.JUNO_CONTROL_INVOCATION_ROLE?.trim();
  const forwardedEffective = process.env.JUNO_CONTROL_EFFECTIVE_ROOT?.trim();
  if (forwardedRoot || forwardedRole || forwardedEffective) {
    if (!forwardedRoot || !forwardedRole || path.resolve(forwardedEffective ?? '') !== controllerRoot) {
      throw new Error('Incomplete or mismatched forwarded control-plane audit identity.');
    }
    let origin: ControllerResolution;
    try {
      origin = resolver(forwardedRoot, 'diagnostic', {
        ignoreEnvironmentAssertions: true,
        trustedResolver: true,
      });
    } catch {
      throw new Error('Forwarded control-plane audit identity no longer matches registered workspace authority.');
    }
    if (
      !origin.valid || path.resolve(origin.path) !== controllerRoot ||
      origin.role !== forwardedRole || !['controller', 'task', 'integration-owner'].includes(origin.role)
    ) {
      throw new Error('Forwarded control-plane audit identity no longer matches registered workspace authority.');
    }
    invocationRoot = path.resolve(origin.current_root);
    invocationRole = origin.role;
  }
  if (
    resolution.resolver !== 'installed' ||
    !resolution.valid ||
    !['controller', 'task', 'integration-owner'].includes(invocationRole)
  ) {
    throw new Error(
      `Control-plane routing requires a valid registered controller, task, or integration-owner workspace. Run \`yy doctor workspace\` from ${invocationRoot}.`,
    );
  }
  const env = buildChildProcessEnvironment(process.env, {
    JUNO_TASK_ROOT: controllerRoot,
    JUNO_CONTROLLER_SOURCE: resolution.source,
    JUNO_WORKSPACE_ROLE: 'controller',
    JUNO_WORKSPACE_ENFORCEMENT: 'strict',
    JUNO_CONTROL_INVOCATION_ROOT: invocationRoot,
    JUNO_CONTROL_INVOCATION_ROLE: invocationRole,
    JUNO_CONTROL_EFFECTIVE_ROOT: controllerRoot,
    JUNO_CONTROL_OPERATION: operation,
  });
  if (resolution.expected_branch) env.JUNO_CONTROLLER_BRANCH = resolution.expected_branch;
  else delete env.JUNO_CONTROLLER_BRANCH;
  return {
    controllerRoot,
    invocationRoot,
    invocationRole,
    resolution: { ...resolution, operation },
    env,
  };
}
