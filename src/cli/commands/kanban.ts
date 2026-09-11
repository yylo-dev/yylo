import path from 'node:path';
import fs from 'fs-extra';
import { Command } from 'commander';
import { routeControlPlane } from '../../utils/control-plane-router.js';
import { markTransparentDelegate } from '../../utils/explicit-command.js';
import { invokeMachineAwareChild, resolveMachineOutput } from '../machine-output.js';

export type KanbanInvoker = (args: string[]) => Promise<void>;

export async function invokeKanban(args: string[]): Promise<void> {
  const route = routeControlPlane(process.cwd(), 'kanban');
  const wrapper = path.join(route.controllerRoot, '.juno_task', 'scripts', 'kanban.sh');
  if (!(await fs.pathExists(wrapper))) {
    throw new Error('Missing canonical controller YYLO Ledger migration wrapper (`kanban.sh`). Run `yy scripts update` from the controller.');
  }
  const machine = resolveMachineOutput(args);
  const { exitCode } = await invokeMachineAwareChild({
    executable: 'bash', args: [wrapper, ...args],
    cwd: route.controllerRoot,
    env: route.env,
    command: `ledger.${args[0] ?? 'help'}`,
    ...(machine ? { machine } : {}),
  });
  if (exitCode !== 0) process.exitCode = exitCode;
}

export function configureKanbanCommand(
  program: Command,
  invoke: KanbanInvoker = invokeKanban,
): void {
  const command = program
    .command('ledger [args...]')
    .alias('kanban')
    .description('Delegate transparently to an independently installed YYLO Ledger CLI')
    .allowUnknownOption(true)
    .allowExcessArguments(true)
    .action((args: string[]) => invoke(args));
  markTransparentDelegate(command);
}
