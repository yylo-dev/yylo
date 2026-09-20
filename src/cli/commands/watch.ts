import { spawn } from 'node:child_process';
import path from 'node:path';
import fs from 'fs-extra';
import { Command } from 'commander';
import { routeControlPlane } from '../../utils/control-plane-router.js';

export type WatchOperation = 'exec' | 'status' | 'await' | 'follow';
export type WatchInvoker = (operation: WatchOperation, args: string[]) => Promise<void>;
export const WATCH_EXEC_RETIRED = 'watch exec is retired: watch only observes existing runs. ' +
  'Run commands or agents explicitly outside watch; use task finish to verify delivery. ' +
  'Historical runs remain available through watch status, await and follow.';

export async function invokeWatch(operation: WatchOperation, args: string[]): Promise<void> {
  if (operation === 'exec') throw new Error(WATCH_EXEC_RETIRED);
  const route = routeControlPlane(process.cwd(), 'kanban');
  const script = path.join(route.controllerRoot, '.juno_task', 'scripts', 'watch_progress.py');
  if (!(await fs.pathExists(script))) {
    throw new Error('Missing managed watch runtime. Run `yy scripts update` and retry.');
  }
  const exitCode = await new Promise<number>((resolve, reject) => {
    const child = spawn('python3', ['-B', script, operation, ...args], {
      cwd: process.cwd(), env: route.env, stdio: 'inherit',
    });
    child.once('error', reject);
    child.once('exit', (code, signal) => {
      if (signal) reject(new Error(`Watch command terminated by signal ${signal}`));
      else resolve(code ?? 1);
    });
  });
  if (exitCode !== 0) process.exitCode = exitCode;
}

export function configureWatchCommand(program: Command, invoke: WatchInvoker = invokeWatch): void {
  const watch = program.command('watch').description('Optionally observe existing runs; never launch, retry or complete tasks');
  watch.command('exec')
    .description('Retired: run commands explicitly outside watch')
    .option('--detach', 'Return a run ID while the owned producer continues')
    .option('--timeout <seconds>', 'Terminate the owned process group after this duration')
    .argument('<command...>', 'Command argv; use -- before options intended for the command')
    .allowUnknownOption(true)
    .action(() => { throw new Error(WATCH_EXEC_RETIRED); });
  watch.command('status').description('Read execution observations, not task completion').argument('<run-id>').action((runId: string) => invoke('status', [runId]));
  watch.command('await').description('Wait read-only for an existing producer footer').argument('<run-id>').action((runId: string) => invoke('await', [runId]));
  watch.command('follow')
    .description('Follow a run log with read-only semantic terminal presentation')
    .argument('<run-id>')
    .action((runId: string) => invoke('follow', [runId]));
}
