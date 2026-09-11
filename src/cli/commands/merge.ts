import { spawn } from 'node:child_process';
import path from 'node:path';
import fs from 'fs-extra';
import { Command } from 'commander';
import { routeControlPlane } from '../../utils/control-plane-router.js';
import { addMachineOutputOptions, invokeMachineAwareChild, resolveMachineOutput } from '../machine-output.js';

export type MergeOperation = 'status' | 'land' | 'project';
export type MergeInvoker = (
  operation: MergeOperation,
  taskId?: string,
  extraArgs?: string[],
) => Promise<void>;

export function mergeControlOperation(operation: MergeOperation): 'kanban' | 'orchestration' {
  return operation === 'status' ? 'kanban' : 'orchestration';
}
export async function invokeMergeAtController(
  operation: MergeOperation,
  controllerRoot: string,
  env: NodeJS.ProcessEnv,
  taskId?: string,
  extraArgs: string[] = [],
): Promise<void> {
  const script = path.join(controllerRoot, '.juno_task', 'scripts', 'merge_queue.py');
  if (!(await fs.pathExists(script))) {
    throw new Error('Missing managed native Git delivery adapter. Run `yy scripts update` and retry.');
  }
  const args = [script, operation, ...(taskId ? [taskId] : []), ...extraArgs];
  const machine = resolveMachineOutput(process.argv.slice(2), { jsonFlag: true });
  if (machine) {
    const { exitCode } = await invokeMachineAwareChild({
      executable: 'python3', args, cwd: controllerRoot, env,
      command: `merge.${operation}`, machine,
    });
    if (exitCode !== 0) process.exitCode = exitCode;
    return;
  }
  const exitCode = await new Promise<number>((resolve, reject) => {
    const child = spawn('python3', args, {
      cwd: controllerRoot,
      env,
      stdio: ['inherit', 'inherit', 'inherit'],
    });
    child.once('error', reject);
    child.once('close', (code, signal) => {
      if (signal) reject(new Error(`Native Git delivery terminated by signal ${signal}`));
      else resolve(code ?? 1);
    });
  });
  if (exitCode !== 0) process.exitCode = exitCode;
}

export async function invokeMerge(
  operation: MergeOperation,
  taskId?: string,
  extraArgs: string[] = [],
): Promise<void> {
  const route = routeControlPlane(process.cwd(), mergeControlOperation(operation));
  await invokeMergeAtController(operation, route.controllerRoot, route.env, taskId, extraArgs);
}

export function configureMergeCommand(
  program: Command,
  invoke: MergeInvoker = invokeMerge,
): void {
  const merge = addMachineOutputOptions(program.command('merge')
    .description('Land one task with native Git, then project its result to Ledger'));

  merge.command('status')
    .description('Read-only status for independently landable tasks')
    .argument('[task-id]', 'Optional task to inspect')
    .action((taskId?: string) => taskId ? invoke('status', taskId) : invoke('status'));

  merge.command('land')
    .description('Compose and atomically land exactly one task; never runs tests, reviews, or models')
    .argument('<task-id>', 'Queued task with one immutable source commit')
    .option('--candidate <sha>', 'Explicit manually resolved candidate commit')
    .option('--expected-target <sha>', 'Target observed when the explicit candidate was composed')
    .action((taskId: string, options: { candidate?: string; expectedTarget?: string }) => {
      if ((options.candidate === undefined) !== (options.expectedTarget === undefined)) {
        throw new Error('--candidate and --expected-target must be supplied together');
      }
      const args = options.candidate
        ? ['--candidate', options.candidate, '--expected-target', options.expectedTarget!]
        : [];
      return invoke('land', taskId, args);
    });

  merge.command('project')
    .description('Separately project an already integrated Git result to Ledger')
    .argument('<task-id>', 'Task whose source ancestry is already in the target')
    .action((taskId: string) => invoke('project', taskId));
}
