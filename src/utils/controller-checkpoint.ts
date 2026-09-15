import fs from 'fs-extra';
import { hasSimpleWorkspaceHint, resolveController } from './controller-resolver.js';
import * as path from 'node:path';
import * as childProcess from 'node:child_process';
import { promisify } from 'node:util';
import { buildChildProcessEnvironment } from '../core/child-process-environment.js';

export type ControllerCheckpointBlocker =
  | 'retryable_lock_or_race'
  | 'deterministic_policy_exclusion'
  | 'unsafe_repository_state'
  | 'unknown';

export interface ControllerCheckpointResult {
  attempted: boolean;
  ok: boolean;
  blocker?: ControllerCheckpointBlocker;
  safeNextAction?: string;
  warning?: string;
}

function checkpointRecovery(error: unknown): {
  blocker: ControllerCheckpointBlocker; safeNextAction: string; detail: string;
} {
  const detail = String(error)
    .replace(/(^|\n)(\s*(?:authorization|token|password|secret)\s*[:=]\s*).*$/gim, '$1$2[REDACTED]')
    .slice(-2000);
  if (/lease busy|lock timeout|branch changed|changed during checkpoint|HEAD\/ref changed/i.test(detail)) {
    return {
      blocker: 'retryable_lock_or_race',
      safeNextAction: 'wait for the owning repository writer to finish, then rerun the owning command',
      detail,
    };
  }
  if (/blocked non-controller|policy refused|include drift|queue attribution refused/i.test(detail)) {
    return {
      blocker: 'deterministic_policy_exclusion',
      safeNextAction: 'preserve the bytes and run `yy doctor workspace`; use the reported exact owner/recovery command',
      detail,
    };
  }
  if (/staged index|conflict|submodule|symlink|nested repository|detached HEAD|unsafe/i.test(detail)) {
    return {
      blocker: 'unsafe_repository_state',
      safeNextAction: 'preserve the repository state and run `yy doctor workspace` before any checkpoint retry',
      detail,
    };
  }
  return {
    blocker: 'unknown',
    safeNextAction: 'preserve the controller state and run `yy doctor workspace` for typed recovery',
    detail,
  };
}

/** Best-effort outer finalizer. It never changes the owning run's exit status. */
export async function checkpointControllerAfterFinalization(
  workingDirectory: string,
  runExitCode: number,
  taskId?: string,
): Promise<ControllerCheckpointResult> {
  if (hasSimpleWorkspaceHint(workingDirectory)) {
    resolveController(workingDirectory, 'diagnostic');
    return { attempted: false, ok: true };
  }
  if (process.env.JUNO_CONTROLLER_CHECKPOINT_ACTIVE === '1') {
    return { attempted: false, ok: true };
  }
  const configured = process.env.JUNO_TASK_ROOT?.trim();
  let root = path.resolve(configured || workingDirectory);
  if (path.basename(root) === '.juno_task') root = path.dirname(root);
  const script = path.join(root, '.juno_task', 'scripts', 'controller_checkpoint.py');
  if (!(await fs.pathExists(script))) return { attempted: false, ok: true };
  const message = runExitCode === 0
    ? 'chore(controller): checkpoint finalized run state'
    : `chore(controller): checkpoint failed run state (exit ${runExitCode})`;
  try {
    const execFile = promisify(childProcess.execFile);
    const scopeArgs = taskId ? ['--task-id', taskId] : [];
    await execFile('python3', [script, '--root', root, ...scopeArgs, 'commit', '--message', message], {
      cwd: root,
      env: buildChildProcessEnvironment(process.env, {
        JUNO_CONTROLLER_CHECKPOINT_ACTIVE: '1',
      }),
      timeout: 30_000,
      maxBuffer: 1024 * 1024,
    });
    return { attempted: true, ok: true };
  } catch (error) {
    const recovery = checkpointRecovery(error);
    const warning = `Controller checkpoint failed after finalization; blocker=${recovery.blocker}; `
      + `safe_next_action=${recovery.safeNextAction}; detail=${recovery.detail}`;
    console.error(`WARNING: ${warning}`);
    return {
      attempted: true, ok: false, blocker: recovery.blocker,
      safeNextAction: recovery.safeNextAction, warning,
    };
  }
}
