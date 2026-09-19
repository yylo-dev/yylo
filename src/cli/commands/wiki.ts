import type { Command } from 'commander';
import { markTransparentDelegate } from '../../utils/explicit-command.js';
import { invokeKanban } from './kanban.js';

/** Wiki identity, storage, help and errors belong exclusively to Ledger. */
export function configureWikiCommand(program: Command): void {
  const command = program.command('wiki [args...]')
    .description('Proxy for yy ledger wiki; use Ledger wiki Records')
    .allowUnknownOption(true)
    .allowExcessArguments(true)
    .action(async (args: string[]) => invokeKanban(['wiki', ...args]));
  markTransparentDelegate(command);
}
