import { execFileSync } from 'node:child_process';
import { existsSync, realpathSync } from 'node:fs';
import path from 'node:path';
import { hasSimpleWorkspaceHint, resolveController, type ControllerResolution } from './controller-resolver.js';
import { assessControllerGeneration } from './controller-generation-startup.js';
import { packagedGenerationRoot } from './controller-generation-migration.js';
import { checkLedgerReadiness } from '../cli/commands/ledger.js';
import { getDefaultHooks } from '../templates/default-hooks.js';
import type { Hooks, HookType } from '../types/index.js';

/** Startup refusals have one user-facing boundary; they are not retryable provider errors. */
export class AgentStartupError extends Error {
  constructor(message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = 'AgentStartupError';
  }
}

export function errorMessage(error: unknown): string {
  if (typeof error === 'string') return error;
  if (error && typeof error === 'object' && 'message' in error && typeof error.message === 'string') return error.message;
  try { return JSON.stringify(error) ?? String(error); } catch { return 'Unknown error (unserializable detail)'; }
}

function git(cwd: string, ...args: string[]): string {
  try {
    return execFileSync('git', ['-C', cwd, ...args], { encoding: 'utf8', timeout: 5000,
      stdio: ['ignore', 'pipe', 'pipe'] }).trim();
  } catch (error) {
    // Exit 1 is normal for an unset Git config key; non-Git rev-parse is also expected.
    const status = (error as { status?: number }).status;
    if ((args[0] === 'config' && status === 1) || (args[0] === 'rev-parse' && status === 128)) return '';
    throw error;
  }
}

/** Discover presence only, never infer controller ownership from directory/child names. */
export function resolveAgentWorkspace(cwd: string, delegatedController?: string,
  operation: 'diagnostic' | 'orchestration' = 'orchestration'): ControllerResolution {
  try {
    const physicalCwd = realpathSync(cwd);
    const repository = git(physicalCwd, 'rev-parse', '--show-toplevel');
    const root = repository || physicalCwd;
    const registration = repository && (
      git(root, 'config', '--get-all', 'juno.controller.path') ||
      git(root, 'config', '--get-all', 'juno.controller.branch') ||
      git(root, 'config', '--worktree', '--get', 'juno.workspace.role')
    );
    const simple = hasSimpleWorkspaceHint(physicalCwd);
    const initialized = existsSync(path.join(root, '.juno_task')) || simple;
    if (!registration && !initialized) {
      if (delegatedController || process.env.JUNO_TASK_ROOT || process.env.JUNO_CONTROLLER_BRANCH || process.env.JUNO_WORKSPACE_ROLE) {
        throw new Error(`Unregistered agent directory ${JSON.stringify(physicalCwd)} cannot inherit controller authority. `
          + 'Run from the registered controller/task worktree, or use a shell without unrelated controller assertions for generic agent use.');
      }
      return { path: root, current_root: root, resolver: 'missing', source: 'current-root', role: 'unregistered',
        expected_branch: null, actual_branch: null, enforcement: 'off', operation: 'diagnostic', valid: true,
        diagnostics: ['Generic agent directory: no controller-owned hooks or dependency preflight.'] };
    }
    const authority = resolveController(physicalCwd, simple ? 'product-edit' : delegatedController ? 'diagnostic' : operation, {
      trustedResolver: true, ignoreEnvironmentAssertions: Boolean(delegatedController),
    });
    if (authority.resolver !== 'installed' || !authority.valid) {
      throw new Error('Installed workspace resolver is unavailable; repair the bound CLI package explicitly.');
    }
    if (authority.source === 'environment') throw new Error('Environment-only controller authority is not admitted for agent startup.');
    if (delegatedController) {
      const delegated = resolveController(delegatedController, simple ? 'product-edit' : 'orchestration', {
        trustedResolver: true, ignoreEnvironmentAssertions: true,
      });
      if (!delegated.valid || delegated.path !== authority.path || realpathSync(delegatedController) !== authority.path) {
        throw new Error('Delegated agent controller differs from the invocation workspace registration.');
      }
    }
    return authority;
  } catch (error) {
    if (error instanceof AgentStartupError) throw error;
    throw new AgentStartupError(`Agent workspace preflight: ${errorMessage(error)}`, { cause: error });
  }
}

/** Bounded readiness only: never invoke install_requirements.sh or its upgrade/repair path. */
export async function checkAgentReadiness(authority: ControllerResolution): Promise<void> {
  if (authority.role === 'unregistered') return;
  try {
    if (authority.role !== 'simple') {
      const assessment = await assessControllerGeneration(authority.path, packagedGenerationRoot());
      if (assessment.disposition !== 'ready') {
        const detail = assessment.disposition === 'refused'
          ? `${assessment.detail}; ${assessment.safeNextAction}`
          : 'run through the public CLI first-use boundary or inspect yy scripts generation doctor.';
        throw new Error(`Controller generation ${assessment.disposition}; ${detail}`);
      }
    }
    await checkLedgerReadiness({ cwd: authority.path });
  } catch (error) {
    throw new AgentStartupError(`Dependency preflight at ${JSON.stringify(authority.path)}: ${errorMessage(error)}`, { cause: error });
  }
}

export const LEGACY_DEPENDENCY_HOOK = './.juno_task/scripts/install_requirements.sh';

/** Preserve custom hooks, but never execute the obsolete default installer. */
export function agentStartupHooks(hooks: Hooks | undefined, generic: boolean): Hooks | undefined {
  if (!hooks) return undefined;
  const defaults = getDefaultHooks();
  return Object.fromEntries(Object.entries(hooks).map(([name, hook]) => [name, {
    ...hook,
    commands: hook.commands.filter((command) => command.trim() !== LEGACY_DEPENDENCY_HOOK
      && !(generic && defaults[name as HookType]?.commands.includes(command))),
  }]));
}
