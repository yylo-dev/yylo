import { Command } from 'commander';
import {
  inspectWorkspaceTopology,
  workspaceLocation,
  type WorkspaceTopology,
} from '../../utils/workspace-topology.js';

function humanInfo(report: WorkspaceTopology): string {
  const line = (label: string, value: unknown) => `${label.padEnd(20)} ${value ?? 'missing'}`;
  const findingLines = report.findings.length
    ? report.findings.map(
        (item) =>
          `  ${item.severity.toUpperCase()} ${item.code}: ${item.message}${item.nextCommand ? ` Next: ${item.nextCommand}` : ''}`,
      )
    : ['  OK: no workspace topology findings'];
  return [
    `Juno workspace (${report.schemaVersion})`,
    line('Repository', report.repository.root),
    line('Invocation', report.invocation.cwd),
    line(
      'Invocation role',
      `${report.invocation.role}${report.invocation.managed ? '' : ' (unmanaged)'}`,
    ),
    line('Role authority', report.invocation.roleAuthority),
    line('Resolver', report.resolver.status),
    line('Controller', report.controller.path),
    line('Controller ref', report.controller.configuredRef),
    line('Controller HEAD', report.controller.head),
    line('Target', report.target.ref),
    line('Target SHA', report.target.sha),
    line('Target owners', report.target.owners.length ? report.target.owners.join(', ') : 'none'),
    line('Integration', report.integration.status),
    line('Integration owner', report.integration.owner?.path),
    line('Integration HEAD', report.integration.owner?.head),
    line('Integration clean', report.integration.owner?.clean),
    line('Tasks', report.tasks.length),
    line('Submodules', report.submodules.length),
    line('Invoker CLI', report.runtime.cliVersion),
    line('Controller executable', report.runtime.controllerVersion),
    line('Managed scripts package', report.runtime.managedGeneration.packageVersion ??
      'not applicable (unbound consumer)'),
    line('Managed scripts target', report.runtime.managedGeneration.targetSha ??
      'not applicable (unbound consumer)'),
    line('Managed scripts healthy', report.runtime.managedGeneration.healthy ??
      'not applicable (unbound consumer)'),
    'Findings',
    ...findingLines,
  ].join('\n');
}

function report(cwd: string | undefined, version: string): WorkspaceTopology {
  return inspectWorkspaceTopology(cwd?.trim() || process.cwd(), version);
}

export function configureWorkspaceCommands(program: Command, version: string): void {
  program
    .command('info')
    .description('Show normalized, offline workspace topology')
    .option('--json', 'Output the stable machine-readable topology')
    .option('-w, --cwd <path>', 'Invocation directory (default: current directory)')
    .action((options: { json?: boolean; cwd?: string }) => {
      const value = report(options.cwd, version);
      console.log(options.json ? JSON.stringify(value, null, 2) : humanInfo(value));
    });

  program
    .command('where')
    .description('Print one script-safe workspace path')
    .argument('<kind>', 'controller, integration, target, or task')
    .argument('[task-id]', 'Required when kind is task')
    .option('-w, --cwd <path>', 'Invocation directory (default: current directory)')
    .action((kind: string, taskId: string | undefined, options: { cwd?: string }) => {
      if (!['controller', 'integration', 'target', 'task'].includes(kind))
        throw new Error(`Unknown workspace kind: ${kind}`);
      if (kind === 'task' && !taskId) throw new Error('where task requires TASK_ID.');
      if (kind !== 'task' && taskId) throw new Error(`${kind} does not accept TASK_ID.`);
      console.log(
        workspaceLocation(
          report(options.cwd, version),
          kind as 'controller' | 'integration' | 'target' | 'task',
          taskId,
        ),
      );
    });

  const doctor = program.command('doctor').description('Run bounded read-only diagnostics');
  doctor
    .command('workspace')
    .description('Diagnose registered workspace topology without fetching or changing state')
    .option('--json', 'Output the stable machine-readable topology')
    .option('-w, --cwd <path>', 'Invocation directory (default: current directory)')
    .action((options: { json?: boolean; cwd?: string }) => {
      const value = report(options.cwd, version);
      console.log(options.json ? JSON.stringify(value, null, 2) : humanInfo(value));
      if (!value.healthy) process.exitCode = 1;
    });
}
