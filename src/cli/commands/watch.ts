import { spawn } from 'node:child_process';
import path from 'node:path';
import fs from 'fs-extra';
import { Command } from 'commander';
import { routeControlPlane } from '../../utils/control-plane-router.js';

export type WatchOperation = 'exec' | 'status' | 'await' | 'follow';
export type WatchInvoker = (operation: WatchOperation, args: string[]) => Promise<void>;

export async function invokeWatch(operation: WatchOperation, args: string[]): Promise<void> {
  const route = routeControlPlane(process.cwd(), operation === 'status' ? 'kanban' : 'orchestration');
  const script = path.join(route.controllerRoot, '.juno_task', 'scripts', 'watch_progress.py');
  if (!(await fs.pathExists(script))) {
    throw new Error('Missing managed watch runtime. Run `yy scripts update` and retry.');
  }
  const exitCode = await new Promise<number>((resolve, reject) => {
    const child = spawn('python3', [script, operation, ...args], {
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
  const watch = program.command('watch').description('Run and await observable local commands');
  watch.command('exec')
    .description('Execute a command with bounded logs and terminal machine truth')
    .option('--detach', 'Return a run ID while the owned producer continues')
    .option('--timeout <seconds>', 'Terminate the owned process group after this duration')
    .argument('<command...>', 'Command argv; use -- before options intended for the command')
    .allowUnknownOption(true)
    .action((command: string[], options: { detach?: boolean; timeout?: string }) => invoke('exec', [
      ...(options.detach ? ['--detach'] : []),
      ...(options.timeout ? ['--timeout', options.timeout] : []),
      '--', ...command,
    ]));
  watch.command('status').argument('<run-id>').action((runId: string) => invoke('status', [runId]));
  watch.command('await').argument('<run-id>').action((runId: string) => invoke('await', [runId]));
  watch.command('follow')
    .description('Follow a run log with read-only semantic terminal presentation')
    .argument('<run-id>')
    .action((runId: string) => invoke('follow', [runId]));
}
