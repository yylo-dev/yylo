import { Command } from 'commander';
import { TmuxWorkspace, type TmuxWorkspaceStatus } from '../../core/tmux-workspace.js';

function printStatus(status: TmuxWorkspaceStatus, json = false): void {
  if (json) {
    console.log(JSON.stringify(status, null, 2));
    return;
  }
  console.log(`tmux workspace: ${status.session} (${status.attachedClients} attached)`);
  for (const window of status.windows) {
    const flags = `${window.active ? ' active' : ''}${window.unseenGeneration ? ' unread' : ''}`;
    console.log(`  ${window.index}:${window.name}${flags}`);
    for (const pane of window.panes)
      console.log(
        `    ${pane.id} ${pane.index}:${pane.title || pane.currentCommand}${pane.active ? ' active' : ''}`,
      );
  }
}

function printWindow(window: TmuxWorkspaceStatus['windows'][number], json = false): void {
  if (json) console.log(JSON.stringify(window, null, 2));
  else console.log(`created tmux window ${window.index}:${window.name}`);
}

export function configureTmuxCommand(program: Command, factory = () => new TmuxWorkspace()): void {
  const tmux = program
    .command('tmux')
    .description('Create, connect to, and manage a local tmux workspace');

  const session = tmux.command('session').description('Create or ensure a named tmux session');
  for (const operation of ['create', 'ensure'] as const) {
    session
      .command(`${operation} <name>`)
      .description(
        `${operation === 'create' ? 'Create' : 'Idempotently ensure'} a detached tmux workspace`,
      )
      .requiredOption('-d, --directory <path>', 'Existing working directory for the initial window')
      .option('--window <name>', 'Initial window name', 'main')
      .option('--json', 'Output stable JSON')
      .action((name: string, options: { directory: string; window: string; json?: boolean }) => {
        const workspace = factory();
        const status =
          operation === 'create'
            ? workspace.createSession(name, options.directory, options.window)
            : workspace.ensureSession(name, options.directory, options.window);
        printStatus(status, options.json);
      });
  }

  tmux
    .command('connect <name>')
    .description('Attach to a workspace, or switch the current tmux client')
    .action((name: string) => factory().connect(name));

  tmux
    .command('open <name>')
    .description('Ensure a workspace and connect to it')
    .option(
      '-d, --directory <path>',
      'Existing working directory for a new workspace',
      process.cwd(),
    )
    .option('--window <name>', 'Initial window name', 'main')
    .action((name: string, options: { directory: string; window: string }) =>
      factory().open(name, options.directory, options.window),
    );

  tmux
    .command('status')
    .description('Show bounded window and pane inventory without acknowledging completions')
    .requiredOption('--session <name>', 'Exact tmux session name')
    .option('--json', 'Output stable JSON')
    .action((options: { session: string; json?: boolean }) =>
      printStatus(factory().status(options.session), options.json),
    );

  const window = tmux.command('window').description('Manage workspace windows');
  window
    .command('create <name>')
    .description('Create one named window with one initial pane')
    .requiredOption('--session <name>', 'Exact tmux session name')
    .option('-d, --directory <path>', 'Existing working directory', process.cwd())
    .option('--select', 'Select the created window')
    .option('--json', 'Output stable JSON')
    .action(
      (
        name: string,
        options: { session: string; directory: string; select?: boolean; json?: boolean },
      ) =>
        printWindow(
          factory().createWindow(options.session, name, options.directory, options.select),
          options.json,
        ),
    );

  const unread = tmux
    .command('unread')
    .description('Inspect or mutate window-scoped completion markers');
  unread
    .command('list')
    .description('List unread windows without acknowledging them')
    .requiredOption('--session <name>', 'Exact tmux session name')
    .option('--json', 'Output stable JSON')
    .action((options: { session: string; json?: boolean }) => {
      const status = factory().status(options.session);
      const windows = status.windows.filter((item) => item.unseenGeneration);
      if (options.json)
        console.log(
          JSON.stringify(
            { schemaVersion: status.schemaVersion, session: status.session, windows },
            null,
            2,
          ),
        );
      else if (!windows.length) console.log('no unread tmux windows');
      else for (const item of windows) console.log(`${item.index}:${item.name}`);
    });

  unread
    .command('mark')
    .description('Store a fresh completion and unread generation on one window')
    .requiredOption('--session <name>', 'Exact tmux session name')
    .requiredOption('--window <target>', 'Window ID, index, or unique name')
    .option('--json', 'Output stable JSON')
    .action((options: { session: string; window: string; json?: boolean }) => {
      const generation = factory().markUnread(options.session, options.window);
      if (options.json)
        console.log(
          JSON.stringify({ session: options.session, window: options.window, generation }, null, 2),
        );
      else console.log(`marked tmux window ${options.window} unread`);
    });

  unread
    .command('clear')
    .description('Clear only an exact observed unread generation')
    .requiredOption('--session <name>', 'Exact tmux session name')
    .requiredOption('--window <target>', 'Window ID, index, or unique name')
    .requiredOption('--generation <token>', 'Exact observed generation')
    .option('--json', 'Output stable JSON')
    .action((options: { session: string; window: string; generation: string; json?: boolean }) => {
      const cleared = factory().clearUnread(options.session, options.window, options.generation);
      if (options.json)
        console.log(
          JSON.stringify({ session: options.session, window: options.window, cleared }, null, 2),
        );
      else
        console.log(
          cleared
            ? `cleared tmux window ${options.window}`
            : 'unread generation changed; nothing cleared',
        );
    });
}
