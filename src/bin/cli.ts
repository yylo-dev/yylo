/**
 * CLI entry point for yylo
 *
 * Comprehensive TypeScript CLI implementation with full functionality parity
 * to the Python budi-cli. Provides all core commands with interactive and
 * headless support, comprehensive error handling, and real-time progress tracking.
 */

// Configure util.inspect to not truncate long strings in JSON output
import { inspect } from 'node:util';
inspect.defaultOptions.maxStringLength = Infinity;
inspect.defaultOptions.breakLength = Infinity;

import { Command, Option } from 'commander';
import chalk from 'chalk';
import { EXIT_CODES, isCLIError } from '../cli/types.js';
import type { SubagentType } from '../types/index.js';
import { resolveAutomaticProjectBootstrap } from '../utils/controller-resolver.js';
import { classifyLeadingCommand, hasManagedWorkspaceMarker } from '../utils/control-plane-router.js';
import {
  InvocationLifecycle,
  joinActiveInvocation,
  runWithInvocationLifecycle,
  startActiveInvocation,
  type InvocationContinuation,
  WRAPPER_LIFECYCLE_ENV,
  WRAPPER_OBSERVATION_ENV,
} from '../core/invocation-lifecycle.js';
import {
  classifyExplicitInvocation,
  formatExplicitInvocationError,
} from '../utils/explicit-command.js';
import { migrateLegacyEnvironment } from '../core/identity-migration.js';

const executableLaunchSurface = (() => {
  const executable = basename(process.argv0);
  return executable === 'yy' || executable === 'ypl' ? executable : 'yylo';
})();

function observeCommanderRequest(program: Command) {
  const argv = process.argv.slice(2);
  const classification = classifyExplicitInvocation(argv, program);
  if (classification.kind === 'unknown-command' || classification.kind === 'unknown-option' ||
    argv.includes('--version') || argv.includes('-V') || argv.includes('--help') || argv.includes('-h')) return {};
  const leading = classifyLeadingCommand(argv);
  const command = leading.command
    ? program.commands.find((candidate) =>
      candidate.name() === leading.command || candidate.aliases().includes(leading.command!))
    : undefined;
  try {
    program.parseOptions(command ? argv.slice(0, leading.index) : argv);
    if (command) command.parseOptions(argv.slice(leading.index + 1));
  } catch {
    // Commander owns the eventual diagnostic. A malformed request must not be
    // guessed into telemetry while preserving its already-recorded attempt.
  }
  const options = { ...program.opts(), ...(command?.opts() ?? {}) } as Record<string, unknown>;
  const cwd = typeof options.cwd === 'string' && options.cwd.length > 0
    ? resolvePath(process.cwd(), options.cwd)
    : process.cwd();
  const aliasService = command && ['claude', 'cursor', 'codex', 'gemini', 'pi'].includes(command.name())
    ? command.name()
    : undefined;
  return {
    workingDirectory: cwd,
    ...(typeof options.subagent === 'string'
      ? { service: options.subagent }
      : aliasService ? { service: aliasService } : {}),
    ...(typeof options.model === 'string' ? { requestedModel: options.model } : {}),
  };
}

// Session ID getter — set when main.js is loaded, used by SIGINT handler
let _getActiveSessionId: (() => string | null) | null = null;

// Import command configurations
import { configureInitCommand } from '../cli/commands/init.js';
import { configureStartCommand } from '../cli/commands/start.js';
import { configureTestCommand } from '../cli/commands/test.js';
import { configureFeedbackCommand } from '../cli/commands/feedback.js';
import { configureSessionCommand } from '../cli/commands/session.js';
import { configureSetupGitCommand } from '../cli/commands/setup-git.js';
import { configureLogsCommand } from '../cli/commands/logs.js';
import { configureViewLogCommand } from '../cli/commands/view-log.js';
import { configureHelpCommand } from '../cli/commands/help.js';
import { createServicesCommand } from '../cli/commands/services.js';
import { createSkillsCommand } from '../cli/commands/skills.js';
import { createAuthCommand } from '../cli/commands/auth.js';
import { configureTaskWorkspaceCommand } from '../cli/commands/task.js';
import { configureIntegrationCommand } from '../cli/commands/integration.js';
import { configureMergeQueueCommand } from '../cli/commands/merge.js';
import { configureWatchCommand } from '../cli/commands/watch.js';
import { configureEvidenceCommand } from '../cli/commands/evidence.js';
import { configureReleaseTrainCommand } from '../cli/commands/release.js';
import { configureKanbanCommand } from '../cli/commands/kanban.js';
import { configureMigrationCommand } from '../cli/commands/migrate.js';
import { configureWorkspaceCommands } from '../cli/commands/workspace.js';
import { configureWikiCommand } from '../cli/commands/wiki.js';
import { configureLoopCommand } from '../cli/commands/loop.js';
import {
  configureBenchmarkCommand,
  forwardBenchmarkSignal,
  runBenchmarkDelegate,
} from '../cli/commands/benchmark.js';
import {
  forwardLedgerSignal,
  runLedgerDelegate,
} from '../cli/commands/ledger.js';
import CompletionCommand from '../cli/commands/completion.js';

// Import version from package.json
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { closeSync, fstatSync, readFileSync, statSync, unlinkSync } from 'node:fs';
import { basename, dirname, join, resolve as resolvePath } from 'node:path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const require = createRequire(import.meta.url);
const packageJson = require(join(__dirname, '../../package.json'));
const VERSION = packageJson.version;

// Continue only a bounded, version-matched state carried by the wrapper's open
// descriptor. Ambient paths and arbitrary regular descriptors are not enough
// to suppress the direct CLI's own start event.
const wrapperLifecycleFd = Number(process.env[WRAPPER_LIFECYCLE_ENV]);
const wrapperContinuation = Number.isInteger(wrapperLifecycleFd) && wrapperLifecycleFd >= 3 && (() => {
  try {
    const descriptorStat = fstatSync(wrapperLifecycleFd);
    if (!descriptorStat.isFile()) return null;
    const raw = readFileSync(wrapperLifecycleFd, 'utf8');
    if (raw.length > 16_384) return null;
    const value = JSON.parse(raw) as InvocationContinuation;
    const identity = value?.identity;
    if (!(typeof value?.stateFile === 'string' && typeof value.workingDirectory === 'string' &&
      typeof value.startedAt === 'string' && Number.isFinite(value.startedMonotonicMs) &&
      identity?.schema_version === 1 && identity.juno_code_version === VERSION &&
      [identity.request_id, identity.trace_id, identity.span_id, identity.launch_surface]
        .every((item) => typeof item === 'string') &&
      [identity.parent_span_id, identity.task_id, identity.workflow_run_id, identity.workflow_step_id]
        .every((item) => item === null || typeof item === 'string'))) return null;
    const pathStat = statSync(value.stateFile);
    if (descriptorStat.dev !== pathStat.dev || descriptorStat.ino !== pathStat.ino) return null;
    unlinkSync(value.stateFile);
    closeSync(wrapperLifecycleFd);
    return value;
  } catch { return null; }
})();
delete process.env[WRAPPER_LIFECYCLE_ENV];
delete process.env[WRAPPER_OBSERVATION_ENV];

/**
 * Normalize verbose flag value to numeric level.
 * Levels: 0=quiet, 1=normal+helping texts (default), 2=debug+hooks
 * -v (no value) → 1, -v 0/false/no → 0, -v 1/true/yes → 1, -v 2 → 2, undefined → 1 (default)
 */
function normalizeVerbose(value: unknown): number {
  if (value === undefined) return 1; // Default: level 1 (normal output)
  if (value === true) return 1; // -v without value
  if (value === false) return 0;
  const str = String(value).toLowerCase().trim();
  if (['false', 'no'].includes(str)) return 0;
  if (['true', 'yes'].includes(str)) return 1;
  const num = Number(str);
  if (!isNaN(num) && num >= 0 && num <= 2) return Math.floor(num);
  return 1; // Default for unrecognized values
}

function extractOptionValueFromArgv(
  argv: readonly string[],
  longOption: string,
  shortOption?: string,
): string | undefined {
  for (let i = 0; i < argv.length; i++) {
    const token = argv[i];
    if (!token) continue;

    if (token === longOption || (shortOption && token === shortOption)) {
      const next = argv[i + 1];
      if (next && !next.startsWith('-')) {
        return next;
      }
      continue;
    }

    if (token.startsWith(`${longOption}=`)) {
      return token.slice(longOption.length + 1);
    }

    if (shortOption && token.startsWith(`${shortOption}=`)) {
      return token.slice(shortOption.length + 1);
    }
  }

  return undefined;
}

/** Determine if an error is a transient connection/pipe error. */
function isConnectionLikeError(err: unknown): boolean {
  const msg = err instanceof Error ? `${err.name}: ${err.message}` : String(err);
  const lower = msg.toLowerCase();
  return [
    'epipe',
    'broken pipe',
    'econnreset',
    'socket hang up',
    'err_socket_closed',
    'connection reset by peer',
  ].some((token) => lower.includes(token));
}

/**
 * Global error handler for CLI operations
 */
function handleCLIError(error: unknown, verbose: number = 0): void {
  if (error instanceof Error && error.name.startsWith('SessionContinuity')) {
    console.error(chalk.red.bold('\n❌ Branch Registry Error'));
    console.error(chalk.red(`   ${error.message}`));
    console.error(chalk.yellow('\n💡 Suggestions:'));
    console.error(chalk.yellow("   • Run ypl 'init' or yylo pi 'init' first to create the main branch"));
    console.error(chalk.yellow('   • Inspect branches with: yylo branches'));
    process.exit(1);
    return;
  }

  if (isCLIError(error)) {
    // Handle known CLI errors
    console.error(chalk.red.bold(`\n❌ ${error.constructor.name}`));
    console.error(chalk.red(`   ${error.message}`));

    if (error.suggestions?.length) {
      console.error(chalk.yellow('\n💡 Suggestions:'));
      error.suggestions.forEach((suggestion) => {
        console.error(chalk.yellow(`   • ${suggestion}`));
      });
    }

    if (error.showHelp) {
      console.error(chalk.gray('\n   Use --help for usage information'));
    }

    // Map CLI error to exit code
    const exitCode = Object.values(EXIT_CODES).includes((error as any).code)
      ? (error as any).code
      : EXIT_CODES.UNEXPECTED_ERROR;

    process.exit(exitCode);
    return;
  }

  // Handle unexpected errors
  console.error(chalk.red.bold('\n❌ Unexpected Error'));
  console.error(chalk.red(`   ${error instanceof Error ? error.message : String(error)}`));

  if (verbose && error instanceof Error) {
    console.error(chalk.gray('\n📍 Stack Trace:'));
    console.error(error.stack);
  }

  process.exit(EXIT_CODES.UNEXPECTED_ERROR);
}

/**
 * Setup global CLI options and behaviors
 */
function setupGlobalOptions(program: Command): void {
  // Global options available to all commands
  program
    .option(
      '-v, --verbose [value]',
      'Verbosity level: 0=quiet, 1=normal (default), 2=debug+hooks. Use -v 0, -v 1, -v 2, or -v false/true',
    )
    .option('-q, --quiet', 'Quiet mode: suppress agent messages and hook output (alias: --silent)')
    .option('--execution-envelope', 'Emit the versioned public machine execution envelope as the only stdout payload')
    .option('--silent', 'Alias for --quiet')
    .option('-c, --config <path>', 'Configuration file path (.json, .toml, pyproject.toml)')
    .option('-l, --log-file <path>', 'Log file path (auto-generated if not specified)')
    .option('--no-color', 'Disable colored output')
    .option('--log-level <level>', 'Log level for output (error, warn, info, debug, trace)', 'info')
    .option('-s, --subagent <name>', 'Subagent to use (claude, cursor, codex, gemini, pi)')
    .option('-b, --backend <type>', 'Backend type (default: shell)')
    .option('-m, --model <name>', 'Model to use (subagent-specific)')
    .option(
      '--agents <config>',
      'Agents configuration (forwarded to shell backend)',
    )
    .option(
      '--tools <tools...>',
      'Specify the list of available tools from the built-in set (only works with --print mode). Use "" to disable all tools, "default" to use all tools, or specify tool names (e.g. "Bash,Edit,Read").',
    )
    .option(
      '--allowed-tools <tools...>',
      'Permission-based filtering of specific tool instances (e.g. "Bash(git:*) Edit"). Default when not specified: Task, Bash, Glob, Grep, ExitPlanMode, Read, Edit, Write, NotebookEdit, WebFetch, TodoWrite, WebSearch, BashOutput, KillShell, Skill, SlashCommand, EnterPlanMode.',
    )
    .option(
      '--disallowed-tools <tools...>',
      'Disallowed tools for Claude. By default, no tools are disallowed',
    )
    .option(
      '--append-allowed-tools <tools...>',
      'Append tools to the default allowed-tools list (mutually exclusive with --allowed-tools).',
    )
    .option('--mcp-timeout <number>', 'Operation timeout in milliseconds (default: 43200000)', parseInt)
    .option(
      '--enable-feedback',
      'Enable interactive feedback mode (F+Enter to enter, Q+Enter to submit)',
    )
    .option('-r, --resume <sessionId>', 'Resume a conversation by session ID (shell backend only)')
    .option('--clone [prompt]', 'Pi only: fork a clone from --resume <sessionId> or the current shell continue scope; optional prompt runs in the clone')
    .option('--continue', 'Continue the most recent conversation (shell backend only)')
    .option(
      '--til-completion',
      'Run yylo in a loop until all YYLO Ledger tasks are complete (aliases: --until-completion, --run-until-completion, --till-complete)',
    )
    .option('--until-completion', 'Alias for --til-completion')
    .addOption(new Option('--run-until-completion', 'Alias for --til-completion').hideHelp())
    .addOption(new Option('--till-complete', 'Alias for --til-completion').hideHelp())
    .option(
      '--pre-run-hook <hooks...>',
      'Execute named hooks from .juno_task/config.json before each iteration (only with --til-completion)',
    )
    .option(
      '--stale-threshold <n>',
      'Number of stale iterations before exiting (default: 3). Set to 0 to disable. (only with --til-completion)',
      parseInt,
    )
    .option(
      '--no-stale-check',
      'Disable stale iteration detection (alias for --stale-threshold 0). (only with --til-completion)',
    )
    .option(
      '--force-update',
      'Force update scripts, services, and Python dependencies (bypasses 24-hour cache)',
    )
    .option(
      '--on-hourly-limit <behavior>',
      'Behavior when Claude hourly quota limit is reached: "wait" to sleep until reset, "raise" to exit immediately (default: raise)',
    )
    .option(
      '--no-hooks',
      'Skip execution of all lifecycle hooks (alias: --no-hook)',
    )
    .addOption(new Option('--no-hook', 'Alias for --no-hooks').hideHelp())
    .option(
      '--thinking <level>',
      'Extended thinking level: off, minimal, low, medium, high, xhigh, max (forwarded to service backend)',
    );

  // Global error handling
  program.exitOverride((err) => {
    if (err.code === 'commander.helpDisplayed') {
      process.exit(0);
      return;
    }
    if (err.code === 'commander.version') {
      process.exit(0);
      return;
    }
    handleCLIError(err, 0);
  });

  // Custom help formatting
  program.configureHelp({
    sortSubcommands: true,
    subcommandTerm: (cmd) => cmd.name() + ' ' + cmd.usage(),
    commandUsage: (cmd) => cmd.name() + ' ' + cmd.usage(),
    commandDescription: (cmd) => cmd.description(),
  });
}

const COMPLETION_FLAGS = [
  '--til-completion',
  '--until-completion',
  '--run-until-completion',
  '--till-complete',
] as const;

function isUntilCompletionRequested(options: Record<string, unknown>): boolean {
  return Boolean(
    options.tilCompletion ||
      options.untilCompletion ||
      options.runUntilCompletion ||
      options.tillComplete,
  );
}

function getForwardedUntilCompletionArgs(): string[] {
  const args = process.argv.slice(2);
  const forwardedArgs: string[] = [];

  for (let i = 0; i < args.length; i++) {
    const arg = args[i];
    if (!arg) continue;

    if (COMPLETION_FLAGS.includes(arg as (typeof COMPLETION_FLAGS)[number])) {
      continue;
    }

    if (arg === '--pre-run-hook') {
      // Skip the variadic values for --pre-run-hook until the next option flag
      while (i + 1 < args.length && !args[i + 1]?.startsWith('-')) {
        i += 1;
      }
      continue;
    }

    if (arg.startsWith('--pre-run-hook=')) {
      continue;
    }

    // --cwd is consumed by the outer yylo invocation to locate/run the
    // correct run_until_completion script. Do not forward it to inner loop
    // invocations, otherwise relative paths can be applied twice.
    if (arg === '--cwd' || arg === '-w') {
      if (i + 1 < args.length && args[i + 1] && !args[i + 1]!.startsWith('-')) {
        i += 1;
      }
      continue;
    }

    if (arg.startsWith('--cwd=') || arg.startsWith('-w=')) {
      continue;
    }

    // --max-iterations (and its -i short form) is the OUTER run-until loop
    // bound under until-completion. It is translated into the script's own
    // --max-iterations argument below and must never reach inner agent
    // sessions, where it previously bounded only the inner loop and let a
    // completed one-pass session start a duplicate run.
    if (arg === '--max-iterations' || arg === '-i') {
      if (i + 1 < args.length && args[i + 1] && !args[i + 1]!.startsWith('-')) {
        i += 1;
      }
      continue;
    }

    if (arg.startsWith('--max-iterations=')) {
      continue;
    }

    forwardedArgs.push(arg);
  }

  return forwardedArgs;
}

async function runUntilCompletionScriptIfRequested(
  options: Record<string, unknown>,
): Promise<boolean> {
  if (!isUntilCompletionRequested(options)) {
    return false;
  }

  const { spawn } = await import('node:child_process');
  const path = await import('node:path');
  const fs = await import('fs-extra');

  const optionCwd =
    typeof options.cwd === 'string' && options.cwd.trim().length > 0
      ? options.cwd.trim()
      : extractOptionValueFromArgv(process.argv.slice(2), '--cwd', '-w');
  const invocationCwd =
    typeof optionCwd === 'string' && optionCwd.trim().length > 0
      ? path.resolve(process.cwd(), optionCwd)
      : process.cwd();

  const scriptPath = path.join(invocationCwd, '.juno_task', 'scripts', 'run_until_completion.sh');

  // Until-completion does not enter mainCommandHandler, so resolve its request
  // observation here from Commander's options and the same project config.
  const [{ loadConfig }, { getConfiguredDefaultModelForSubagent }] = await Promise.all([
    import('../core/config.js'),
    import('../core/subagent-models.js'),
  ]);
  const config = await loadConfig({ baseDir: invocationCwd });
  const service = typeof options.subagent === 'string'
    ? options.subagent
    : typeof config.defaultSubagent === 'string' ? config.defaultSubagent : 'claude';
  const explicitModel = typeof options.model === 'string' ? options.model : undefined;
  const configuredModel = getConfiguredDefaultModelForSubagent(config, service as SubagentType);
  if (!await startActiveInvocation({
    service,
    requestedModel: explicitModel || configuredModel || null,
  })) return true;

  // Check if script exists
  if (!(await fs.pathExists(scriptPath))) {
    console.error(chalk.red.bold('\n❌ Error: run_until_completion.sh not found'));
    console.error(chalk.red(`   Expected location: ${scriptPath}`));
    console.error(chalk.yellow('\n💡 Suggestion: Run "yylo init" to initialize the project'));
    process.exit(1);
    return true;
  }

  // Build arguments for run_until_completion.sh
  const scriptArgs: string[] = [];

  // Add --pre-run-hook arguments if provided
  if (Array.isArray(options.preRunHook)) {
    for (const hook of options.preRunHook) {
      scriptArgs.push('--pre-run-hook', String(hook));
    }
  }

  // Forward all yylo arguments except completion flags and pre-run-hook values
  scriptArgs.push(...getForwardedUntilCompletionArgs());

  // The outer iteration bound is passed to the script explicitly and is never
  // forwarded to inner agent sessions. Negative values mean unlimited, which
  // the script expresses as 0.
  const outerBound = typeof options.maxIterations === 'number'
    && Number.isFinite(options.maxIterations)
    ? Math.trunc(options.maxIterations)
    : null;
  if (outerBound !== null && outerBound > 0) {
    scriptArgs.unshift('--max-iterations', String(outerBound));
    console.log(
      chalk.cyan(
        `Until-completion scope: outer loop bounded at ${outerBound} iteration(s); `
        + 'inner agent sessions are not bounded by --max-iterations.',
      ),
    );
  } else if (outerBound !== null) {
    console.log(
      chalk.cyan(
        'Until-completion scope: outer loop unlimited (--max-iterations <= 0); '
        + 'inner agent sessions are not bounded by --max-iterations.',
      ),
    );
  }

  // Execute and join the producer. The outer invocation must not report a
  // terminal event while this child is still running.
  const child = spawn(scriptPath, scriptArgs, {
    stdio: 'inherit',
    cwd: invocationCwd,
  });

  let childExited = false;
  const childResult = new Promise<number>((resolve) => {
    child.once('exit', (code) => {
      childExited = true;
      resolve(code ?? 0);
    });
    child.once('error', (error) => {
      childExited = true;
      console.error(chalk.red.bold('\n❌ Error executing run_until_completion.sh'));
      console.error(chalk.red(`   ${error.message}`));
      resolve(1);
    });
  });
  joinActiveInvocation(childResult);

  const signalHandler = (signal: NodeJS.Signals) => {
    if (!childExited && child.pid) {
      try {
        process.kill(child.pid, signal);
      } catch {
        // Child might have already exited.
      }
    }
  };
  process.on('SIGINT', signalHandler);
  process.on('SIGTERM', signalHandler);

  const exitCode = await childResult;
  process.removeListener('SIGINT', signalHandler);
  process.removeListener('SIGTERM', signalHandler);
  process.exit(exitCode);
  return true;
}

/**
 * Setup continue command.
 * Reuses the most recent persisted session ID + runtime settings, requiring only a new prompt.
 */
function setupContinueCommand(program: Command): void {
  const continueCommand = program
    .command('continue')
    .aliases(['contiue', 'cn', 'cc'])
    .description('Continue the most recent yylo session with saved settings (aliases: contiue, cn, cc)')
    .argument('[prompt_text...]', 'Prompt text (positional, alternative to -p)')
    .option(
      '-p, --prompt [text]',
      "Prompt input (inline text, file path, or heredoc/stdin; supports !'cmd' and !```cmd``` substitutions, prefer single quotes for shell metacharacters)",
    )
    .option('-f, --prompt-file <path>', 'Read prompt from a file (shell-safe for backticks/$())')
    .option('-w, --cwd <path>', 'Working directory')
    .option('-i, --max-iterations <number>', 'Override max iterations for this continue run', parseInt)
    .option('-m, --model <name>', 'Override model for this continue run')
    .option('-s, --subagent <name>', 'Override subagent for this continue run')
    .option('--clone [prompt]', 'Pi only: fork the current shell continue-scope session, persist the cloned session id, and run an optional prompt')
    .option('-I, --interactive', 'Interactive mode for typing prompts')
    .option('--live', 'Run Pi subagent in interactive live TUI mode (pi only)')
    .option('--thinking <level>', 'Override thinking level for this continue run')
    .action(async (promptArgs: string[], options, command) => {
      if (promptArgs.length > 0 && options.prompt === undefined) {
        options.prompt = promptArgs.join(' ');
      }
      if (typeof options.clone === 'string' && options.prompt === undefined) {
        options.prompt = options.clone;
      }

      try {
        const { mainCommandHandler, getActiveSessionId } = await import('../cli/commands/main.js');
        _getActiveSessionId = getActiveSessionId;

        const globalOptions = program.opts();
        const definedGlobalOptions = Object.fromEntries(
          Object.entries(globalOptions).filter(([_, v]) => v !== undefined),
        );
        const allOptions = { ...definedGlobalOptions, ...options, continueFromLatest: true };

        allOptions.verbose = normalizeVerbose(allOptions.verbose);

        if (allOptions.silent) {
          allOptions.quiet = true;
        }

        await mainCommandHandler([], allOptions as any, command);
      } catch (error) {
        handleCLIError(error, normalizeVerbose(options.verbose));
      }
    });

  continueCommand.addHelpText(
    'after',
    `
${chalk.blue.bold('Examples:')}
  yylo continue 'Implement the next step'
  yylo continue --clone 'Explore approach B'

${chalk.blue.bold('Pi clone behavior:')}
  continue --clone forks the current shell continue-scope session with Pi native --fork.
  The cloned session id is persisted to this shell scope, so future continue runs follow the clone.
`,
  );

  continueCommand.allowUnknownOption(true);
}

/**
 * Setup clone command.
 * Alias-style UX for `yylo continue --clone`, using the current shell continue scope.
 */
function setupCloneCommand(program: Command): void {
  const cloneCommand = program
    .command('clone')
    .description('Clone/fork a Pi session from the current shell continue-scope session, run a prompt, and continue from the clone')
    .argument('[prompt_text...]', 'Prompt text, or shorthand: <branch> <prompt>')
    .option(
      '-p, --prompt [text]',
      "Prompt input (inline text, file path, or heredoc/stdin; supports !'cmd' and !```cmd``` substitutions, prefer single quotes for shell metacharacters)",
    )
    .option('-f, --prompt-file <path>', 'Read prompt from a file (shell-safe for backticks/$())')
    .option('-w, --cwd <path>', 'Working directory')
    .option('-m, --model <name>', 'Override model for the cloned run')
    .option('-s, --subagent <name>', 'Override subagent for the cloned run')
    .option('--name <branch>', 'Save the cloned session under a named branch (does not switch active branch)')
    .option('--from <branch>', 'Named source branch to clone from (default: main)')
    .option('--live', 'Run Pi subagent in interactive live TUI mode (pi only)')
    .option('--thinking <level>', 'Override thinking level for the cloned run')
    .action(async (promptArgs: string[] | string | undefined, options, command) => {
      const parsedPromptArgs = Array.isArray(promptArgs)
        ? promptArgs
        : typeof promptArgs === 'string' && promptArgs.length > 0
          ? [promptArgs]
          : [];
      const commandArgs = Array.isArray(command.args)
        ? command.args.filter((arg: unknown): arg is string => typeof arg === 'string')
        : [];
      const cloneArgs = parsedPromptArgs.length > 0 ? parsedPromptArgs : commandArgs;
      const hasExplicitPromptInput = options.prompt !== undefined || options.promptFile;
      if (cloneArgs.length >= 2 && options.name === undefined) {
        const [branchName, ...remainingPromptArgs] = cloneArgs;
        options.name = branchName;
        if (options.prompt === undefined) {
          options.prompt = remainingPromptArgs.join(' ');
        }
      } else if (cloneArgs.length === 1 && options.name === undefined && hasExplicitPromptInput) {
        options.name = cloneArgs[0];
      } else if (cloneArgs.length > 0 && options.prompt === undefined) {
        options.prompt = cloneArgs.join(' ');
      }

      try {
        const { mainCommandHandler, getActiveSessionId } = await import('../cli/commands/main.js');
        _getActiveSessionId = getActiveSessionId;

        const globalOptions = program.opts();
        const definedGlobalOptions = Object.fromEntries(
          Object.entries(globalOptions).filter(([_, v]) => v !== undefined),
        );
        const allOptions = {
          ...definedGlobalOptions,
          ...options,
          continueFromLatest: true,
          clone: true,
          cloneBranchName: options.name,
          cloneBranchFrom: options.from,
        };

        allOptions.verbose = normalizeVerbose(allOptions.verbose);

        if (allOptions.silent) {
          allOptions.quiet = true;
        }

        await mainCommandHandler([], allOptions as any, command);
      } catch (error) {
        handleCLIError(error, normalizeVerbose(options.verbose));
      }
    });

  cloneCommand.addHelpText(
    'after',
    `
${chalk.blue.bold('Examples:')}
  yylo clone 'Explore approach A'      # auto-names b1, b2, ...
  yylo clone early_reflect '@@reflect'
  yylo clone --name C 'Explore C'
  yylo clone --from C --name M 'Explore M'
  yylo --resume <session-id> --clone '@@close_loop'   # fork explicit session id

${chalk.blue.bold('Named branch behavior:')}
  clone 'prompt' auto-assigns the first available b-number branch (b1, b2, ...)
  when this shell already has a branch registry. clone C 'prompt' is shorthand
  for --name C 'prompt'. --name C clones from main
  by default, runs the prompt immediately in C, and overwrites C if it already
  exists. Clone does not switch the active branch; use yylo switch C when
  future yylo continue runs should follow C. --from C --name M clones from
  branch C into M. The target name main is reserved.

${chalk.blue.bold('Scope behavior:')}
  Each shell/pane has its own active branch registry. If a new tab says
  "No named session branches found", run ypl 'init' in that tab, use the
  original tab, or set YYLO_CONTINUE_SCOPE=<name> before shared runs.
  Use --resume <id> --clone for explicit session ids; clone C --resume <id>
  is not named-branch syntax.
`,
  );

  cloneCommand.allowUnknownOption(true);
}

/**
 * Setup continue-scope helper command.
 * Exposes the current continue hash + status for script integrations.
 */
function setupNamedBranchCommands(program: Command): void {
  const resolveWorkingDirectory = async (options: Record<string, unknown>) => {
    const workingDirectory =
      typeof options.cwd === 'string' && options.cwd.trim().length > 0
        ? options.cwd.trim()
        : process.cwd();
    const { loadConfig } = await import('../core/config.js');
    return await loadConfig({
      baseDir: workingDirectory,
      cliConfig: {
        verbose: 0,
        quiet: true,
        logLevel: 'info',
        workingDirectory,
      },
    });
  };

  const branchesCommand = program
    .command('branches')
    .description('List named Pi session branches for the current shell branch registry')
    .option('-w, --cwd <path>', 'Working directory')
    .option('--json', 'Output machine-readable JSON')
    .action(async (options) => {
      try {
        const [{ resolveContinueScopeContext }, branchesModule] = await Promise.all([
          import('../core/continue-scope.js'),
          import('../core/session-continuity-state.js'),
        ]);
        const config = await resolveWorkingDirectory(options);
        const scope = resolveContinueScopeContext(process.env, process.ppid, config.workingDirectory);
        const branches = await branchesModule.listSessionBranches({
          workingDirectory: config.workingDirectory,
          scope,
        });

        if (options.json) {
          console.log(JSON.stringify({ scope: scope.scopeHash, branches }, null, 2));
          return;
        }

        if (branches.length === 0) {
          console.log('No named session branches found for this shell scope.');
          console.log("Run ypl 'init' or yylo pi 'init' first.");
          return;
        }

        console.log('ACTIVE  BRANCH  SESSION_ID  PARENT  UPDATED_AT');
        for (const branch of branches) {
          console.log([
            branch.active ? '*' : ' ',
            branch.name,
            branch.sessionId,
            branch.parent ?? '-',
            branch.updatedAt ?? '-',
          ].join('  '));
        }
      } catch (error) {
        handleCLIError(error, normalizeVerbose(options.verbose));
      }
    });

  branchesCommand.addHelpText(
    'after',
    `
${chalk.blue.bold('Examples:')}
  yylo branches
  yylo branches --json

${chalk.blue.bold('Behavior:')}
  Shows named branches for this shell/pane and marks the active branch with *.
  Future yylo continue runs follow the active branch in this shell.
`,
  );

  const switchCommand = program
    .command('switch')
    .description('Switch the active named Pi session branch for this shell branch registry')
    .argument('<branch>', 'Branch name to activate')
    .argument('[prompt_text...]', 'Optional prompt to run immediately after switching')
    .option(
      '-p, --prompt [text]',
      "Prompt input to run after switching (inline text, file path, or heredoc/stdin; supports !'cmd' and !```cmd``` substitutions)",
    )
    .option('-f, --prompt-file <path>', 'Read prompt from a file after switching')
    .option('-w, --cwd <path>', 'Working directory')
    .option('-i, --max-iterations <number>', 'Override max iterations for the switched-branch run', parseInt)
    .option('-m, --model <name>', 'Override model for the switched-branch run')
    .option('-s, --subagent <name>', 'Override subagent for the switched-branch run')
    .option('-I, --interactive', 'Interactive mode for typing a prompt after switching')
    .option('--live', 'Run Pi subagent in interactive live TUI mode (pi only)')
    .option('--thinking <level>', 'Override thinking level for the switched-branch run')
    .action(async (branchName: string, promptArgs: string[], options, command) => {
      if (promptArgs.length > 0 && options.prompt === undefined) {
        options.prompt = promptArgs.join(' ');
      }

      try {
        const { persistActiveSessionBranchSelection } = await import('../core/session-continuity-state.js');
        const config = await resolveWorkingDirectory(options);
        const active = await persistActiveSessionBranchSelection({
          workingDirectory: config.workingDirectory,
          branchName,
        });
        console.log(`Switched to branch ${active.name} (${active.sessionId})`);

        if (options.prompt !== undefined || options.promptFile || options.interactive) {
          const { mainCommandHandler, getActiveSessionId } = await import('../cli/commands/main.js');
          _getActiveSessionId = getActiveSessionId;

          const globalOptions = program.opts();
          const definedGlobalOptions = Object.fromEntries(
            Object.entries(globalOptions).filter(([_, v]) => v !== undefined),
          );
          const allOptions = { ...definedGlobalOptions, ...options, continueFromLatest: true };

          allOptions.verbose = normalizeVerbose(allOptions.verbose);

          if (allOptions.silent) {
            allOptions.quiet = true;
          }

          await mainCommandHandler([], allOptions as any, command);
        }
      } catch (error) {
        handleCLIError(error, normalizeVerbose(options.verbose));
      }
    });

  switchCommand.addHelpText(
    'after',
    `
${chalk.blue.bold('Examples:')}
  yylo switch C
  yy switch C
  yy switch +
  yy switch -
  yy switch C 'Continue C immediately'

${chalk.blue.bold('Behavior:')}
  Makes the branch active only for this shell/pane. Use + or - to cycle to the
  next/previous branch with wraparound. If a prompt is provided, yylo
  switches first and then runs that prompt as a continue on the newly active
  branch. Future yylo continue or yy cc runs in this shell continue that
  branch until you switch again or a reset creates a new main registry.
`,
  );

  switchCommand.allowUnknownOption(true);
}

function setupContinuityCommand(program: Command): void {
  const continuity = program
    .command('continuity')
    .description('Inventory, migrate, retain, and roll back automatic session continuity state');
  const root = (options: { cwd?: string }) => resolvePath(options.cwd?.trim() || process.cwd());

  continuity
    .command('doctor')
    .description('Print a redacted, read-only continuity inventory (never values)')
    .option('-w, --cwd <path>', 'Working directory')
    .option('--json', 'Output machine-readable JSON')
    .action(async (options) => {
      try {
        const { inspectContinuityState } = await import('../core/continuity-maintenance.js');
        const report = await inspectContinuityState({ workingDirectory: root(options) });
        console.log(JSON.stringify(report, null, 2));
      } catch (error) {
        handleCLIError(error, normalizeVerbose(options.verbose));
      }
    });

  continuity
    .command('clean')
    .description('Generate a dry-run reviewed plan, or explicitly apply one')
    .option('-w, --cwd <path>', 'Working directory')
    .option(
      '--plan <path>',
      'Write a redacted reviewed plan without changing env or continuity state',
    )
    .option('--apply <path>', 'Apply an explicitly reviewed plan under the shared lock')
    .action(async (options) => {
      try {
        const maintenance = await import('../core/continuity-maintenance.js');
        if (options.plan && options.apply)
          throw new Error('Choose exactly one of --plan or --apply.');
        if (options.apply) {
          const result = await maintenance.applyContinuityMigrationPlan({
            workingDirectory: root(options),
            planPath: resolvePath(options.apply),
          });
          console.log(JSON.stringify({ applied: true, receiptPath: result.receiptPath }, null, 2));
          return;
        }
        if (options.plan) {
          const plan = await maintenance.createContinuityMigrationPlan({
            workingDirectory: root(options),
            planPath: resolvePath(options.plan),
          });
          console.log(
            JSON.stringify(
              { dryRun: true, planPath: resolvePath(options.plan), projected: plan.projected },
              null,
              2,
            ),
          );
          return;
        }
        const report = await maintenance.inspectContinuityState({
          workingDirectory: root(options),
        });
        console.log(JSON.stringify({ dryRun: true, ...report }, null, 2));
      } catch (error) {
        handleCLIError(error, normalizeVerbose(options.verbose));
      }
    });

  continuity
    .command('rollback')
    .description('Restore exact backup bytes only when all post-apply hashes still match')
    .argument('<receipt>', 'Receipt path emitted by continuity clean --apply')
    .option('-w, --cwd <path>', 'Working directory')
    .action(async (receipt, options) => {
      try {
        const { rollbackContinuityMigration } = await import('../core/continuity-maintenance.js');
        await rollbackContinuityMigration({
          workingDirectory: root(options),
          receiptPath: resolvePath(receipt),
        });
        console.log(
          JSON.stringify({ rolledBack: true, receiptPath: resolvePath(receipt) }, null, 2),
        );
      } catch (error) {
        handleCLIError(error, normalizeVerbose(options.verbose));
      }
    });

  for (const pinned of [true, false]) {
    continuity
      .command(pinned ? 'pin' : 'unpin')
      .description(`${pinned ? 'Protect' : 'Unprotect'} a continuity scope from retention cleanup`)
      .argument('[scope]', 'Full SCOPE_<16 uppercase hex> hash; defaults to the current scope')
      .option('-w, --cwd <path>', 'Working directory')
      .action(async (scopeHash, options) => {
        try {
          const [{ resolveContinueScopeContext }, state] = await Promise.all([
            import('../core/continue-scope.js'),
            import('../core/session-continuity-state.js'),
          ]);
          const workingDirectory = root(options);
          const scope =
            scopeHash ||
            resolveContinueScopeContext(process.env, process.ppid, workingDirectory).scopeHash;
          await state.setContinueScopePinned({ workingDirectory, scope, pinned });
          console.log(JSON.stringify({ scope, pinned }, null, 2));
        } catch (error) {
          handleCLIError(error, normalizeVerbose(options.verbose));
        }
      });
  }
}

function setupContinueScopeCommand(program: Command): void {
  const continueScopeCommand = program
    .command('continue-scope')
    .description('Show continue scope hash and status (running, finished, not_found, error)')
    .argument('[hash]', 'Optional hash (5-6 char prefix or full SCOPE_<HASH>)')
    .option('-w, --cwd <path>', 'Working directory')
    .option(
      '--parent-pid <pid>',
      'Resolve the caller scope as if this process had the given parent PID (script integrations)',
    )
    .option('--json', 'Output machine-readable JSON')
    .option('--handoff-session <session-id>', 'Persist a workflow handoff session for the resolved caller scope')
    .option('--handoff-settings <json>', 'Validated bounded execution settings for --handoff-session')
    .action(async (hash: string | undefined, options) => {
      try {
        const workingDirectory =
          typeof options.cwd === 'string' && options.cwd.trim().length > 0
            ? options.cwd.trim()
            : process.cwd();

        const [{ loadConfig }, continueScope] = await Promise.all([
          import('../core/config.js'),
          import('../core/continue-scope.js'),
        ]);

        const config = await loadConfig({
          baseDir: workingDirectory,
          cliConfig: {
            verbose: 0,
            quiet: true,
            logLevel: 'info',
            workingDirectory,
          },
        });

        const parentPid =
          options.parentPid === undefined ? process.ppid : Number(options.parentPid);
        const currentScope = continueScope.resolveContinueScopeContextForParent({
          env: process.env,
          parentPid,
          workingDirectory: config.workingDirectory,
        });
        if (options.handoffSession !== undefined) {
          if (hash !== undefined) throw new Error('Workflow handoff cannot target a hash lookup; resolve the caller scope directly.');
          if (typeof options.handoffSettings !== 'string' || !options.handoffSettings.trim()) {
            throw new Error('--handoff-settings is required with --handoff-session.');
          }
          const state = await import('../core/session-continuity-state.js');
          await state.persistContinueScopeSnapshot({
            workingDirectory: config.workingDirectory,
            context: currentScope,
            sessionId: String(options.handoffSession),
            serializedSettings: options.handoffSettings,
          });
        } else if (options.handoffSettings !== undefined) {
          throw new Error('--handoff-settings requires --handoff-session.');
        }

        const status = await continueScope.resolveContinueScopeStatus({
          workingDirectory: config.workingDirectory,
          ...(hash !== undefined ? { requestedHash: hash } : {}),
          currentScope,
        });

        if (options.json) {
          console.log(JSON.stringify(status, null, 2));
          return;
        }

        const statusColor =
          status.status === 'running'
            ? chalk.yellow
            : status.status === 'finished'
              ? chalk.green
              : status.status === 'not_found'
                ? chalk.gray
                : chalk.red;

        console.log(chalk.blue.bold('Continue Scope'));
        console.log(`  hash: ${chalk.cyan(status.hash)}`);
        console.log(`  full_hash: ${chalk.gray(status.fullHash)}`);
        console.log(`  status: ${statusColor(status.status)}`);
        console.log(`  source: ${chalk.gray(status.scopeSource)}`);
        console.log(`  session_env_key: ${chalk.gray(status.sessionEnvKey)}`);
        console.log(`  settings_env_key: ${chalk.gray(status.settingsEnvKey)}`);
        console.log(`  session_id: ${status.sessionId ? chalk.cyan(status.sessionId) : chalk.gray('-')}`);
        if (status.pid !== null) {
          console.log(`  pid: ${chalk.gray(String(status.pid))}`);
        }
        if (status.reason) {
          console.log(`  reason: ${chalk.yellow(status.reason)}`);
        }
      } catch (error) {
        handleCLIError(error, normalizeVerbose(options.verbose));
      }
    });

  continueScopeCommand.allowUnknownOption(false);
}

/**
 * Setup main execution command (default command)
 */
function setupMainCommand(program: Command): void {
  // Main command for direct execution with subagent
  program
    .argument('[prompt_text...]', 'Prompt text (positional, alternative to -p)')
    .option(
      '-p, --prompt [text]',
      "Prompt input (inline text, file path, or heredoc/stdin; supports !'cmd' and !```cmd``` substitutions, prefer single quotes for shell metacharacters)",
    )
    .option('-f, --prompt-file <path>', 'Read prompt from a file (shell-safe for backticks/$())')
    .option('-w, --cwd <path>', 'Working directory')
    .option('-i, --max-iterations <number>', 'Maximum iterations (-1 for unlimited)', parseInt)
    .option('-I, --interactive', 'Interactive mode for typing prompts')
    .option('--live', 'Run Pi subagent in interactive live TUI mode (pi only)')
    .option('-ip, --interactive-prompt', 'Launch interactive prompt editor')
    .action(async (promptArgs: string[], options, command) => {
      // Merge positional prompt args into options.prompt (if -p not already set)
      if (promptArgs.length > 0 && options.prompt === undefined) {
        options.prompt = promptArgs.join(' ');
      }
      if (typeof program.opts().clone === 'string' && options.prompt === undefined) {
        options.prompt = program.opts().clone;
      }
      try {
        // Get global options from program
        const globalOptions = program.opts();
        // Merge options with command options taking precedence over global options
        // Only merge defined global options to avoid overwriting command options with undefined
        const definedGlobalOptions = Object.fromEntries(
          Object.entries(globalOptions).filter(([_, v]) => v !== undefined),
        );
        const allOptions = { ...definedGlobalOptions, ...options };

        // Normalize verbose: default true, -v false/0/no disables
        allOptions.verbose = normalizeVerbose(allOptions.verbose);

        // Handle --silent as alias for --quiet
        if (allOptions.silent) {
          allOptions.quiet = true;
        }

        if (await runUntilCompletionScriptIfRequested(allOptions)) {
          return;
        }

        // Check if we should auto-detect project configuration
        if (
          !globalOptions.subagent &&
          !options.prompt &&
          !options.interactive &&
          !options.interactivePrompt
        ) {
          const fs = await import('fs-extra');
          const path = await import('node:path');
          const cwd = process.cwd();
          const junoTaskDir = path.join(cwd, '.juno_task');

          // Check if project is initialized
          if (await fs.pathExists(junoTaskDir)) {
            console.log(chalk.blue.bold('🎯 YYLO - Auto-detected Initialized Project\n'));

            // Try to load configuration for auto-detection
            try {
              const { loadConfig } = await import('../core/config.js');
              const config = await loadConfig({
                baseDir: cwd,
                cliConfig: {
                  verbose: allOptions.verbose,
                  quiet: allOptions.quiet || false,
                  logLevel: allOptions.logLevel || 'info',
                  workingDirectory: cwd,
                },
              });

              // Auto-detect subagent from config
              if (!allOptions.subagent && config.defaultSubagent) {
                allOptions.subagent = config.defaultSubagent;
                console.log(
                  chalk.gray(`🤖 Using configured subagent: ${chalk.cyan(config.defaultSubagent)}`),
                );
              }

              // Auto-detect prompt file (.juno_task/prompt.md)
              const promptFile = path.join(junoTaskDir, 'prompt.md');
              if (!allOptions.prompt && (await fs.pathExists(promptFile))) {
                allOptions.prompt = promptFile;
                console.log(
                  chalk.gray(`📄 Using default prompt: ${chalk.cyan('.juno_task/prompt.md')}`),
                );
              }

              // Check if we have enough information to proceed
              if (allOptions.subagent && (allOptions.prompt || (await fs.pathExists(promptFile)))) {
                console.log(chalk.green('✓ Auto-detected project configuration\n'));
                // Import and execute with auto-detected options
                const { mainCommandHandler, getActiveSessionId } = await import('../cli/commands/main.js');
                _getActiveSessionId = getActiveSessionId;
                await mainCommandHandler([], allOptions, command);
                return;
              }
            } catch (configError) {
              console.log(chalk.yellow(`⚠️  Could not load project configuration: ${configError}`));
            }
          }
        }

        // Show help if no arguments provided or auto-detection failed
        if (
          !globalOptions.subagent &&
          !options.prompt &&
          !options.interactive &&
          !options.interactivePrompt
        ) {
          console.log(
            chalk.blue.bold('🎯 YYLO - TypeScript CLI for AI Subagent Orchestration\n'),
          );
          console.log(chalk.white('To get started:'));
          console.log(chalk.gray('  yylo init                    # Initialize new project'));
          console.log(chalk.gray('  yylo start                   # Start execution'));
          console.log(chalk.gray('  yylo test --generate --run   # AI-powered testing'));
          console.log(
            chalk.gray("  yylo -s claude 'prompt'      # Quick execution with Claude"),
          );
          console.log(
            chalk.gray("  yylo -s claude -p 'prompt'   # Same (explicit -p flag)"),
          );
          console.log(chalk.gray("  ypl 'prompt'                     # Shortcut for: yy pi --live 'prompt'"));
          console.log(
            chalk.gray('  shell safety: use single quotes or -f/stdin for prompts with backticks/$()'),
          );
          console.log(chalk.gray('  yylo --help                  # Show all commands'));
          console.log('');
          return;
        }

        // Import and execute main command handler dynamically
        const { mainCommandHandler, getActiveSessionId } = await import('../cli/commands/main.js');
        _getActiveSessionId = getActiveSessionId;
        await mainCommandHandler([], allOptions, command);
      } catch (error) {
        handleCLIError(error, normalizeVerbose(options.verbose));
      }
    });
}

/**
 * Display welcome banner with version and environment info
 */
function displayBanner(verbose: number = 0): void {
  if (verbose >= 1) {
    console.error(chalk.blue.bold(`\n🎯 YYLO v${VERSION} - TypeScript CLI`));
    console.error(chalk.gray(`   Node.js ${process.version} on ${process.platform}`));
    console.error(chalk.gray(`   Working directory: ${process.cwd()}`));
    console.error('');
  }
}

/**
 * Setup enhanced completion support
 */
function setupScriptManagementCommands(program: Command): void {
  const runUpdate = async (options: { force?: boolean; cwd?: string }) => {
    // Commander may consume --cwd as a parent/global option even when it appears
    // after this nested command, so retain argv fallback as the CLI-facing truth.
    const argvCwd = extractOptionValueFromArgv(process.argv.slice(2), '--cwd', '-w');
    const workingDirectory =
      typeof options.cwd === 'string' && options.cwd.trim().length > 0
        ? options.cwd.trim()
        : typeof argvCwd === 'string' && argvCwd.trim().length > 0
          ? argvCwd.trim()
          : process.cwd();
    const [
      { ScriptInstaller },
      { ManagedProjectAssets },
      { SkillInstaller },
      { withManagedUpdateRollback },
    ] = await Promise.all([
      import('../utils/script-installer.js'),
      import('../utils/managed-project-assets.js'),
      import('../utils/skill-installer.js'),
      import('../utils/managed-update-transaction.js'),
    ]);

    // This is deliberately before even an update progress claim. These probes
    // cover every script, dependency, guidance, manifest/backup, and ignored
    // agent-surface destination and perform no writes.
    const recovery = await ScriptInstaller.preflightUpdate(
      workingDirectory, Boolean(options.force),
    );
    await SkillInstaller.preflightInstall(workingDirectory);
    const metadataOnlyController = await ScriptInstaller.isMetadataOnlyController(workingDirectory);

    if (options.force) {
      console.log(chalk.blue(recovery
        ? '🔄 Recovering the exact target-bound metadata-controller bundle...'
        : metadataOnlyController
          ? '🔄 Force updating ignored metadata-controller runtime scripts, agent surface, and Python dependencies...'
          : '🔄 Force updating project scripts, managed prompts/wiki, macros, and Python dependencies...'));
      const outcome = await withManagedUpdateRollback(workingDirectory, async () => {
        if (metadataOnlyController && !recovery) {
          await ScriptInstaller.updateMetadataControllerPolicies(workingDirectory, true, true);
        }
        const assets = await ManagedProjectAssets.update(
          workingDirectory,
          { force: true, silent: true, recovery: recovery ?? undefined },
        );
        const scriptsUpdated = recovery
          ? false
          : await ScriptInstaller.forceUpdateAll(workingDirectory, true);
        const skillsUpdated = recovery
          ? false
          : await SkillInstaller.install(workingDirectory, true, true, true);
        if (metadataOnlyController) {
          await ScriptInstaller.assertMetadataControllerUpdateComplete(
            workingDirectory, recovery ?? undefined,
          );
          if (!recovery && await SkillInstaller.needsUpdate(workingDirectory)) {
            throw new Error('Metadata-controller agent surface remains incomplete after update');
          }
        }
        return { scriptsUpdated, skillsUpdated, assets };
      });
      console.log(chalk.green(recovery
        ? `✓ Recovered exact target-bound controller bundle at ${recovery.targetSha}`
        : '✓ Force updated scripts, requirements, and managed project assets'));
      if (!outcome.scriptsUpdated && !outcome.skillsUpdated &&
          outcome.assets.installed.length + outcome.assets.updated.length === 0) {
        console.log(chalk.yellow('No project assets updated. Is this an initialized yylo project with .juno_task/?'));
      }
      return;
    }

    console.log(chalk.blue(metadataOnlyController
      ? '🔄 Updating ignored metadata-controller runtime scripts and agent surface...'
      : '🔄 Updating project scripts and checksum-managed prompts/wiki/macros...'));
    const update = async () => {
      if (metadataOnlyController) {
        await ScriptInstaller.updateMetadataControllerPolicies(workingDirectory, false, true);
      }
      const assets = await ManagedProjectAssets.update(workingDirectory, { silent: false });
      const scriptsUpdated = await ScriptInstaller.autoUpdate(workingDirectory, false);
      const skillsUpdated = await SkillInstaller.install(workingDirectory, true);
      if (metadataOnlyController) {
        await ScriptInstaller.assertMetadataControllerUpdateComplete(workingDirectory);
        if (await SkillInstaller.needsUpdate(workingDirectory)) {
          throw new Error('Metadata-controller agent surface remains incomplete after update');
        }
      }
      return { scriptsUpdated, skillsUpdated, assets };
    };
    const { scriptsUpdated, skillsUpdated, assets } = metadataOnlyController
      ? await withManagedUpdateRollback(workingDirectory, update)
      : await update();
    if (!scriptsUpdated && !skillsUpdated && assets.installed.length + assets.updated.length === 0 && assets.conflicts.length === 0) {
      console.log(chalk.green(metadataOnlyController
        ? '✓ Metadata-controller runtime scripts and agent surface are already up to date'
        : '✓ Managed project assets are already up to date'));
    }
  };

  const scriptsCommand = program
    .command('scripts')
    .description('Manage project scripts plus checksum-managed prompts/wiki/macros')
    .addHelpText(
      'after',
      `
${chalk.blue.bold('Examples:')}
  yylo scripts update --force
  yy scripts update --force

${chalk.gray('This updates scripts from the currently installed yylo package/templates.')}
`,
    );

  scriptsCommand
    .command('update')
    .description('Refresh scripts plus managed prompts/wiki/macros from the current package')
    .option('-f, --force', 'Force replacement with backups and update Python dependencies')
    .option('-w, --cwd <path>', 'Project directory (default: current working directory)')
    .action(async (options) => {
      await runUpdate(options);
    });

  scriptsCommand
    .command('doctor')
    .description('Check that lifecycle scripts and managed guidance are one coherent generation')
    .option('-w, --cwd <path>', 'Project directory (default: current working directory)')
    .action(async (options: { cwd?: string }) => {
      const argvCwd = extractOptionValueFromArgv(process.argv.slice(2), '--cwd', '-w');
      const workingDirectory = options.cwd?.trim() || argvCwd?.trim() || process.cwd();
      const [{ ManagedProjectAssets }, { ScriptInstaller }, { SkillInstaller }] = await Promise.all([
        import('../utils/managed-project-assets.js'),
        import('../utils/script-installer.js'),
        import('../utils/skill-installer.js'),
      ]);
      if (await ScriptInstaller.isMetadataOnlyController(workingDirectory)) {
        await SkillInstaller.assertInstallAllowed(workingDirectory);
        const recovery = await ScriptInstaller.assertManagedControllerPackageUpdateAllowed(
          workingDirectory,
        );
        await ScriptInstaller.assertMetadataControllerUpdateComplete(
          workingDirectory, recovery ?? undefined,
        );
        const [generation, agentSurfaceStale, bundle] = await Promise.all([
          ScriptInstaller.inspectManagedControllerGeneration(workingDirectory),
          recovery ? false : SkillInstaller.needsUpdate(workingDirectory),
          ManagedProjectAssets.inspectGeneration(workingDirectory, recovery ?? undefined),
        ]);
        if (generation.present) {
          if (generation.healthy && !agentSurfaceStale) {
            console.log(chalk.green(
              `✓ Receipt-bound controller scripts and instruction bundle ` +
              `${bundle.instructionBundle?.bundleSha256} are coherent at ${generation.targetSha} ` +
              `(package ${generation.packageVersion})`,
            ));
            return;
          }
          console.error(chalk.red('✗ Receipt-bound controller script generation is unhealthy'));
          for (const finding of generation.findings) console.error(`  ${finding}`);
          if (agentSurfaceStale) console.error('  incomplete or stale: ignored controller agent surface');
          if (generation.targetSha) console.error(chalk.yellow(
            `Recover explicitly with \`yy integration runtime-refresh --previous-sha ${generation.targetSha} ` +
            `--target-sha ${generation.targetSha}\`.`,
          ));
          process.exitCode = 1;
          return;
        }
        const [missing, outdated] = await Promise.all([
          ScriptInstaller.getMissingScripts(workingDirectory),
          ScriptInstaller.getOutdatedScripts(workingDirectory),
        ]);
        if (missing.length === 0 && outdated.length === 0 && !agentSurfaceStale) {
          console.log(chalk.green(
            `✓ Schema-2 instruction bundle ${bundle.instructionBundle?.bundleSha256} and ` +
            'bootstrap controller scripts are coherent (no integration generation receipt)',
          ));
          return;
        }
        console.error(chalk.red('✗ Bootstrap controller scripts are incomplete or stale'));
        for (const entry of missing) console.error(`  missing: .juno_task/scripts/${entry}`);
        for (const entry of outdated) console.error(`  outdated: .juno_task/scripts/${entry}`);
        if (agentSurfaceStale) console.error('  incomplete or stale: ignored controller agent surface');
        console.error(chalk.yellow('Run `yy scripts update` only for this unbound bootstrap state.'));
        process.exitCode = 1;
        return;
      }
      const report = await ManagedProjectAssets.inspectGeneration(workingDirectory);
      if (report.coherent) {
        console.log(chalk.green('✓ Lifecycle scripts and managed guidance are coherent'));
        return;
      }
      console.error(chalk.red(`✗ Lifecycle asset generation is ${report.status}`));
      for (const entry of report.entries.filter((item) => item.state !== 'current')) {
        console.error(`  ${entry.state}: ${entry.destination}`);
      }
      console.error(chalk.yellow('Run `yy scripts update`; use `--force` only after reviewing backups.'));
      process.exitCode = 1;
    });

  program
    .command('install-scripts')
    .description('Alias for scripts update; refresh scripts plus managed prompts/wiki/macros')
    .option('-f, --force', 'Force reinstall scripts/assets, back up custom managed files, and update Python dependencies')
    .option('-w, --cwd <path>', 'Project directory (default: current working directory)')
    .action(async (options) => {
      await runUpdate(options);
    });

}

function setupTaskLifecycleCommand(program: Command): void {
  const lifecycle = program
    .command('lifecycle')
    .description('Removed command; migrate to the Bolt task and merge interfaces');
  for (const operation of ['run', 'resume', 'status'] as const) {
    lifecycle
      .command(operation)
      .allowUnknownOption(true)
      .option('--task <task-id>', 'Legacy task ID (ignored)')
      .action(() => {
        console.error('The legacy lifecycle executor was removed. Use `yy task start|status|finish` and `yy merge status|next|resolve`.');
        process.exitCode = 2;
      });
  }
}

function setupCompletion(program: Command): void {
  try {
    const completionCommand = new CompletionCommand();
    completionCommand.register(program);
  } catch (error) {
    // Don't fail CLI startup if completion setup fails
    console.warn(chalk.yellow('⚠️  Warning: Could not setup completion commands'));
  }
}

/**
 * Subagent help text definitions.
 * Each entry provides backend-specific documentation shown via `yylo <subagent> --help`.
 */
const SUBAGENT_HELP: Record<
  string,
  { description: string; helpText: string }
> = {
  claude: {
    description: 'Execute with Anthropic Claude subagent',
    helpText: `
${chalk.blue.bold('Claude Backend')} — Anthropic Claude Code CLI wrapper

${chalk.blue('Model Shorthands:')}
  :sonnet              claude-sonnet-4-6       ${chalk.gray('(default)')}
  :opus                claude-opus-4-6
  :haiku               claude-haiku-4-5-20251001
  :claude-sonnet-4-5   claude-sonnet-4-5-20250929
  :claude-sonnet-4-6   claude-sonnet-4-6
  :claude-opus-4       claude-opus-4-20250514
  :claude-opus-4-5     claude-opus-4-5-20251101
  :claude-opus-4-6     claude-opus-4-6
  :claude-haiku-4-5    claude-haiku-4-5-20251001

${chalk.blue('Service-Specific Options:')}
  These are forwarded to claude.py and the Claude CLI:
  --permission-mode <mode>  Permission mode: acceptEdits, bypassPermissions, default, plan, skip
  --auto-instruction <txt>  Text prepended to the prompt automatically
  --agents <config>         Agents configuration (forwarded to Claude CLI)
  --additional-args <args>  Extra CLI arguments as a space-separated string

${chalk.blue('Tool Configuration:')}
  --tools <tools...>              Built-in tool set (use "" to disable all)
  --allowed-tools <tools...>      Replace default allowed tools
  --append-allowed-tools <t...>   Add to default allowed tools
  --disallowed-tools <tools...>   Disable specific tools

  Default tools: Task, Bash, Glob, Grep, ExitPlanMode, Read, Edit, Write,
    NotebookEdit, WebFetch, TodoWrite, WebSearch, BashOutput, KillShell,
    Skill, SlashCommand, EnterPlanMode

${chalk.blue('Environment Variables:')}
  CLAUDE_MODEL                    Model override (default: claude-sonnet-4-6)
  CLAUDE_PROJECT_PATH             Project directory
  CLAUDE_AUTO_INSTRUCTION         Auto-instruction text
  CLAUDE_PERMISSION_MODE          Permission mode (default: default)
  CLAUDE_PRETTY                   Pretty-print JSON output (true/false)
  CLAUDE_VERBOSE                  Verbose output (true/false)

${chalk.blue('Examples:')}
  ${chalk.gray('# Basic execution')}
  yylo claude 'Analyze this codebase'

  ${chalk.gray('# Choose a model')}
  yylo claude -m :opus 'Complex refactoring task'
  yylo claude -m :haiku 'Quick analysis'

  ${chalk.gray('# File-based prompt')}
  yylo claude -f prompt.md

  ${chalk.gray('# Resume a session')}
  yylo claude --resume <session-id> 'Continue the work'
  yylo claude --continue 'Next step'

  ${chalk.gray('# Tool configuration')}
  yylo claude --disallowed-tools Bash 'Read-only analysis'
  yylo claude --allowed-tools Read Grep 'Search only'

  ${chalk.gray('# Pipe prompt via stdin')}
  echo 'Explain this code' | yylo claude
  cat prompt.md | yylo claude

  ${chalk.gray('# Shell safety')}
  ${chalk.gray('Use single quotes (or -f/stdin) when prompts contain backticks or $()')}

  ${chalk.gray('# Prompt-time substitutions (refreshed each iteration)')}
  yylo claude -p "Status: !'git status --short'"
  yylo claude -i 3 -p "Recent commits:\n!\`\`\`git log -n 5 --oneline\`\`\`"
`,
  },

  pi: {
    description: 'Execute with Pi multi-provider coding agent',
    helpText: `
${chalk.blue.bold('Pi Backend')} — Multi-provider coding agent (Anthropic, OpenAI, Google, Groq, xAI)

${chalk.blue('Model Shorthands:')}
  ${chalk.gray('# Anthropic')}
  :sonnet              anthropic/claude-sonnet-4-6
  :opus                anthropic/claude-opus-4-6
  :haiku               anthropic/claude-haiku-4-5-20251001
  ${chalk.gray('# OpenAI / OpenAI Codex')}
  :luna                openai-codex/gpt-5.6-luna
  :sol                 openai-codex/gpt-5.6-sol
  :gpt                 :sol ${chalk.gray('(default)')}
  :gpt5.5              openai-codex/gpt-5.5
  :mini                openai-codex/gpt-5.6-terra
  :gpt-5               openai/gpt-5
  :gpt-4o              openai/gpt-4o
  :o3                  openai/o3
  :codex               openai-codex/gpt-5.3-codex
  :api-codex           openai/gpt-5.3-codex
  :codex-spark         openai-codex/gpt-5.3-codex-spark
  :api-codex-spark     openai/gpt-5.3-codex-spark
  ${chalk.gray('# Google')}
  :gemini-pro          google/gemini-2.5-pro
  :gemini-flash        google/gemini-2.5-flash
  ${chalk.gray('# Others')}
  :groq                groq/llama-4-scout-17b-16e-instruct
  :grok                xai/grok-3
  :pi, :default        :gpt ${chalk.gray('(default aliases)')}

${chalk.blue('Service-Specific Options:')}
  These are forwarded to pi.py and the Pi CLI:
  --provider <name>         LLM provider override (anthropic, openai, google, groq, xai)
  --thinking <level>        Extended thinking: off, minimal, low, medium, high, xhigh, max
  --tools <list>            Pi tools (comma-separated): read, bash, edit, write, grep, find, ls
  --no-tools                Disable all built-in Pi tools
  --system-prompt <text>    Replace Pi's default system prompt
  --append-system-prompt <text>  Append to Pi's default system prompt
  --no-extensions           Disable Pi TypeScript extensions
  --no-skills               Disable Pi skills
  --no-session              Ephemeral mode — no session persistence
  --auto-instruction <txt>  Text prepended to the prompt automatically
  --additional-args <args>  Extra Pi CLI arguments as a space-separated string
  --live                    Run Pi in interactive TUI mode (auto-exits on non-aborted completion)

${chalk.blue('Environment Variables:')}
  PI_MODEL                  Model override (default: :gpt → openai-codex/gpt-5.6-sol)
  PI_PROVIDER               Provider override
  PI_PROJECT_PATH           Project directory
  PI_THINKING               Thinking level
  PI_TOOLS                  Comma-separated tool list
  PI_SYSTEM_PROMPT          Custom system prompt
  PI_APPEND_SYSTEM_PROMPT   Text appended to system prompt
  PI_AUTO_INSTRUCTION       Auto-instruction text
  PI_NO_SESSION             Disable sessions (true/false)
  PI_PRETTY                 Pretty-print JSON output (true/false)
  HEADLESS_UI_TURN_COST_DISPLAY_THRESHOLD_USD
                            Show authoritative per-turn cost above this threshold (default: 0.5)
  PI_VERBOSE                Verbose mode (true/false)

${chalk.blue('Examples:')}
  ${chalk.gray('# Basic execution')}
  yylo pi 'Build a REST API endpoint'

  ${chalk.gray('# Use a specific provider and model')}
  yylo pi -m :gpt-5 'Refactor this module'
  yylo pi -m openai/gpt-4o --provider openai 'Task'
  yylo pi -m :gemini-pro 'Analyze performance'

  ${chalk.gray('# Extended thinking')}
  yylo pi --thinking high 'Complex architecture redesign'

  ${chalk.gray('# Tool and session control')}
  yylo pi --no-tools 'Read-only analysis'
  yylo pi --no-session 'One-off question'
  yylo pi --resume <session-id> 'Continue work'
  ypl --resume <session-id> '@@close_loop'  ${chalk.gray('# live resume; do not prefix with "clone C"')}

  ${chalk.gray('# Interactive live TUI mode')}
  yylo pi --live -p '/skill:ralph-loop' -i 1
  ypl '/skill:ralph-loop' -i 1     ${chalk.gray('# shortcut for: yy pi --live ...')}

  ${chalk.gray('# Named Pi session branches (per shell/pane continue scope)')}
  ypl 'init'                       ${chalk.gray('# creates/resets the main branch from a root Pi run')}
  yy clone 'Explore A'             ${chalk.gray('# auto-names b1, b2, ...; runs prompt, does not switch')}
  yy clone C 'Explore C'           ${chalk.gray('# shorthand for --name C; runs prompt, does not switch')}
  yy clone --name C 'Explore C'    ${chalk.gray('# forks main into C, runs prompt, does not switch')}
  yy clone --from C --name M 'Explore M'
  yy --resume <session-id> --clone '@@close_loop' ${chalk.gray('# fork explicit session id (not named)')}
  yy branches                      ${chalk.gray('# list branches; * marks active')}
  yy switch C                      ${chalk.gray('# future yy cc / yylo continue follows C')}
  yy switch +                      ${chalk.gray('# cycle to next branch with wraparound')}
  yy switch -                      ${chalk.gray('# cycle to previous branch with wraparound')}
  yy switch C 'Continue C now'     ${chalk.gray('# switch first, then run prompt on C')}
  yy cc 'Continue C'               ${chalk.gray('# updates only the active branch')}

  ${chalk.gray('# Branch rules')}
  ${chalk.gray('Default branch is main; --name main is reserved; named clones require this')}
  ${chalk.gray('shell/pane to have a branch registry. New tabs may need: ypl \'init\'.')}
  ${chalk.gray('Use yy --resume <id> --clone for explicit ids; yy clone C --resume <id> is')}
  ${chalk.gray('not named-branch syntax. ypl clone C ... sends "clone C" as prompt text.')}
  ${chalk.gray('New root Pi runs and explicit --resume without --clone reset registry to main.')}

  ${chalk.gray('# File-based prompt')}
  yylo pi -f instructions.md
`,
  },

  codex: {
    description: 'Execute with OpenAI Codex subagent',
    helpText: `
${chalk.blue.bold('Codex Backend')} — OpenAI Codex CLI wrapper

${chalk.blue('Model Shorthands:')}
  :codex               gpt-5.3-codex        ${chalk.gray('(default)')}
  :codex-mini          gpt-5.1-codex-mini
  :gpt-5               gpt-5
  :mini                gpt-5-codex-mini

${chalk.blue('Service-Specific Options:')}
  These are forwarded to codex.py and the Codex CLI:
  -c, --config <args>       Additional codex config arguments (repeatable)
  --auto-instruction <txt>  Text prepended to the prompt automatically

  Default configs applied automatically:
    include_apply_patch_tool=true
    use_experimental_streamable_shell_tool=true
    sandbox_mode=danger-full-access

${chalk.blue('Environment Variables:')}
  CODEX_MODEL                     Model override (default: gpt-5.3-codex)
  CODEX_HIDE_STREAM_TYPES         Stream types to suppress (comma-separated)
  YYLO_HIDE_STREAM_TYPES     Alternative stream type filter

${chalk.blue('Examples:')}
  ${chalk.gray('# Basic execution')}
  yylo codex 'Fix the failing tests'

  ${chalk.gray('# Use a different model')}
  yylo codex -m :gpt-5 'Implement feature X'
  yylo codex -m :codex-mini 'Quick fix'

  ${chalk.gray('# File-based prompt')}
  yylo codex -f prompt.md
`,
  },

  gemini: {
    description: 'Execute with Google Gemini subagent',
    helpText: `
${chalk.blue.bold('Gemini Backend')} — Google Gemini CLI wrapper

${chalk.blue('Model Shorthands:')}
  :pro                 gemini-2.5-pro       ${chalk.gray('(default)')}
  :flash               gemini-2.5-flash
  :pro-2.5             gemini-2.5-pro
  :flash-2.5           gemini-2.5-flash
  :pro-3               gemini-3.0-pro
  :flash-3             gemini-3.0-flash

${chalk.blue('Service-Specific Options:')}
  These are forwarded to gemini.py and the Gemini CLI:
  --output-format <fmt>         Output format: stream-json, json, text (default: stream-json)
  --include-directories <dirs>  Comma-separated directories to include for context
  --approval-mode <mode>        Approval mode (e.g., auto_edit). Defaults to --yolo
  --yolo                        Auto-approve all actions (default in headless mode)
  --debug                       Enable Gemini CLI debug output

${chalk.blue('Environment Variables:')}
  GEMINI_API_KEY                Required API key for authentication
  GEMINI_MODEL                  Model override (default: gemini-2.5-pro)
  GEMINI_PROJECT_PATH           Project directory
  GEMINI_OUTPUT_FORMAT          Output format override

${chalk.blue('Examples:')}
  ${chalk.gray('# Basic execution')}
  yylo gemini 'Analyze this codebase'

  ${chalk.gray('# Choose a model')}
  yylo gemini -m :flash 'Quick analysis'
  yylo gemini -m :pro-3 'Complex task'

  ${chalk.gray('# Include specific directories')}
  yylo gemini --include-directories 'src,tests' 'Review code quality'

  ${chalk.gray('# File-based prompt')}
  yylo gemini -f prompt.md
`,
  },

  cursor: {
    description: 'Execute with Cursor AI subagent',
    helpText: `
${chalk.blue.bold('Cursor Backend')} — Cursor AI editor agent

${chalk.blue('Note:')}
  Cursor uses the Claude service backend (claude.py).
  See ${chalk.cyan('yylo claude --help')} for model shorthands and service options.

${chalk.blue('Examples:')}
  ${chalk.gray('# Basic execution')}
  yylo cursor 'Refactor this component'

  ${chalk.gray('# With model selection')}
  yylo cursor -m :sonnet 'Analyze code'

  ${chalk.gray('# File-based prompt')}
  yylo cursor -f prompt.md
`,
  },
};

/**
 * Create command aliases for each subagent with dedicated help text.
 * Each subagent command shows backend-specific options, model shorthands, and examples.
 */
function setupAliases(program: Command): void {
  const subagents = ['claude', 'cursor', 'codex', 'gemini', 'pi'];

  for (const subagent of subagents) {
    const help = SUBAGENT_HELP[subagent];
    if (!help) continue;
    const cmd = program
      .command(subagent)
      .description(help.description)
      .argument('[prompt...]', 'Prompt text or file path')
      .option(
        '-p, --prompt [text]',
        "Prompt input (inline text, or heredoc/stdin; supports !'cmd' and !```cmd``` substitutions, prefer single quotes for shell metacharacters)",
      )
      .option('-f, --prompt-file <path>', 'Read prompt from a file (shell-safe for backticks/$())')
      .option('-w, --cwd <path>', 'Working directory')
      .option('-i, --max-iterations <number>', 'Maximum iterations (-1 for unlimited)', parseInt)
      .option('-m, --model <name>', 'Model to use (see model shorthands below)')
      .option('-r, --resume <sessionId>', 'Resume a conversation by session ID')
      .option('--continue', 'Continue the most recent conversation')
      .option('-I, --interactive', 'Interactive mode for typing prompts')
      .option('--live', 'Run Pi subagent in interactive live TUI mode (pi only)')
      .addHelpText('after', help.helpText)
      .action(async (promptArgs: string[], options, command) => {
        // Merge positional prompt args into options.prompt (if -p not already set)
        if (promptArgs.length > 0 && options.prompt === undefined) {
          options.prompt = promptArgs.join(' ');
        }

        try {
          const { mainCommandHandler, getActiveSessionId } = await import('../cli/commands/main.js');
          _getActiveSessionId = getActiveSessionId;

          // Get global options from program
          const globalOptions = program.opts();
          // Merge options with command options taking precedence over global options
          // Only merge defined global options to avoid overwriting command options with undefined
          const definedGlobalOptions = Object.fromEntries(
            Object.entries(globalOptions).filter(([_, v]) => v !== undefined),
          );
          const allOptions = { ...definedGlobalOptions, ...options, subagent };

          // Normalize verbose: default true, -v false/0/no disables
          allOptions.verbose = normalizeVerbose(allOptions.verbose);

          // Handle --silent as alias for --quiet
          if (allOptions.silent) {
            allOptions.quiet = true;
          }

          if (await runUntilCompletionScriptIfRequested(allOptions)) {
            return;
          }

          await mainCommandHandler([], allOptions, command);
        } catch (error) {
          handleCLIError(error, normalizeVerbose(options.verbose));
        }
      });

    cmd
      .command('set-default-model <model>')
      .description(`Set default model for the ${subagent} subagent in .juno_task/config.json`)
      .option('-w, --cwd <path>', 'Working directory')
      .action(async (model: string, commandOptions: Record<string, unknown>, command: Command) => {
        const mergedOptions = (() => {
          const commandWithGlobals = command as Command & {
            optsWithGlobals?: () => Record<string, unknown>;
          };

          if (typeof commandWithGlobals.optsWithGlobals === 'function') {
            return {
              ...commandWithGlobals.optsWithGlobals(),
              ...commandOptions,
            };
          }

          const parentOptions = command.parent?.opts ? command.parent.opts() : {};
          return {
            ...parentOptions,
            ...commandOptions,
          };
        })();

        try {
          const optionCwd =
            typeof mergedOptions.cwd === 'string' && mergedOptions.cwd.trim().length > 0
              ? mergedOptions.cwd.trim()
              : extractOptionValueFromArgv(process.argv.slice(2), '--cwd', '-w');
          const workingDirectory =
            typeof optionCwd === 'string' && optionCwd.trim().length > 0
              ? optionCwd.trim()
              : process.cwd();

          const [{ loadConfig }, subagentModels, fsExtra, nodePath] = await Promise.all([
            import('../core/config.js'),
            import('../core/subagent-models.js'),
            import('fs-extra'),
            import('node:path'),
          ]);

          const typedSubagent = subagent as SubagentType;
          const normalizedModel = String(model).trim();
          if (!normalizedModel) {
            throw new Error('Model cannot be empty. Example: yylo pi set-default-model :api-codex');
          }

          // Ensure project config exists and is valid before we mutate it.
          const config = await loadConfig({
            baseDir: workingDirectory,
            cliConfig: {
              verbose: 0,
              quiet: true,
              logLevel: 'info',
              workingDirectory,
            },
          });

          if (!subagentModels.isModelCompatibleWithSubagent(normalizedModel, typedSubagent, config)) {
            throw new Error(
              `Model ${normalizedModel} is not compatible with subagent ${subagent}. ` +
                `Use ${subagent} model shorthands or a full provider model id.`,
            );
          }

          const configPath = nodePath.join(workingDirectory, '.juno_task', 'config.json');
          const configObject = (await fsExtra.default.readJson(configPath)) as Record<string, unknown>;

          const existingMap =
            configObject.defaultModels &&
            typeof configObject.defaultModels === 'object' &&
            !Array.isArray(configObject.defaultModels)
              ? { ...(configObject.defaultModels as Record<string, unknown>) }
              : {};

          existingMap[subagent] = normalizedModel;
          configObject.defaultModels = existingMap;

          if (configObject.defaultSubagent === subagent) {
            configObject.defaultModel = normalizedModel;
          }

          await fsExtra.default.writeJson(configPath, configObject, { spaces: 2 });

          console.log(
            chalk.green(
              `✓ Default model for ${subagent} set to ${normalizedModel} in ${nodePath.relative(process.cwd(), configPath) || configPath}`,
            ),
          );
        } catch (error) {
          handleCLIError(error, normalizeVerbose(mergedOptions.verbose));
        }
      });

    // Pass through unknown options to the backend service
    cmd.allowUnknownOption(true);
  }
}

/**
 * Configure environment variable integration
 * Supports YYLO_* environment variables
 */
function configureEnvironment(): void {
  migrateLegacyEnvironment();
  // Canonical YYLO_* environment variables
  const newEnvVars = [
    'YYLO_SUBAGENT',
    'YYLO_PROMPT',
    'YYLO_CWD',
    'YYLO_MAX_ITERATIONS',
    'YYLO_MODEL',
    'YYLO_LOG_FILE',
    'YYLO_VERBOSE',
    'YYLO_QUIET',
    'YYLO_CONFIG',
    'YYLO_MCP_TIMEOUT',
    'YYLO_NO_COLOR',
    'YYLO_ENABLE_FEEDBACK',
  ];

  const optionAliases: Record<string, string[]> = {
    subagent: ['-s'],
    prompt: ['-p'],
    cwd: ['-w'],
    'max-iterations': ['-i'],
    model: ['-m'],
    'log-file': ['-l'],
    verbose: ['-v'],
    quiet: ['-q'],
    config: ['-c'],
  };

  const hasExplicitOption = (option: string): boolean => {
    const delimiter = process.argv.indexOf('--');
    const args = process.argv.slice(2, delimiter === -1 ? undefined : delimiter);
    const names = [`--${option}`, ...(optionAliases[option] ?? [])];
    return args.some((arg) => names.some((name) =>
      arg === name || arg.startsWith(`${name}=`),
    ));
  };

  // Helper function to process environment variables
  const processEnvVar = (envVar: string, prefix: string) => {
    const value = process.env[envVar];
    const option = envVar.toLowerCase().replace(prefix, '').replace(/_/g, '-');
    if (value && !hasExplicitOption(option)) {
      // Environment variable is set but not overridden by a long, short, or
      // --name=value CLI argument.
      switch (option) {
        case 'verbose':
          // YYLO_VERBOSE supports true/false/0/1/no/yes — pass value through for normalization
          if (value.toLowerCase() === 'false' || value === '0' || value.toLowerCase() === 'no') {
            process.argv.push('--verbose', 'false');
          } else if (value.toLowerCase() === 'true' || value === '1' || value.toLowerCase() === 'yes') {
            process.argv.push('--verbose');
          }
          break;
        case 'quiet':
        case 'no-color':
        case 'enable-feedback':
          if (value.toLowerCase() === 'true' || value === '1') {
            process.argv.push(`--${option}`);
          }
          break;
        default:
          process.argv.push(`--${option}`, value);
      }
      return true; // Indicates value was processed
    }
    return false;
  };

  // Process YYLO_* variables
  for (const envVar of newEnvVars) {
    processEnvVar(envVar, 'yylo_');
  }

  // Handle JUNO_INTERACTIVE_FEEDBACK_MODE environment variable (user-requested alternative)
  // This is an alias for --enable-feedback, provides user-friendly environment variable name
  if (!process.argv.includes('--enable-feedback')) {
    const feedbackMode = process.env.JUNO_INTERACTIVE_FEEDBACK_MODE;
    if (feedbackMode && (feedbackMode.toLowerCase() === 'true' || feedbackMode === '1')) {
      process.argv.push('--enable-feedback');
    }
  }

  // Handle NO_COLOR standard
  if (process.env.NO_COLOR && !process.argv.includes('--no-color')) {
    process.argv.push('--no-color');
  }

  // Handle CI environment
  if (process.env.CI && !process.argv.includes('--quiet')) {
    process.argv.push('--quiet');
  }
}

function configureCommandSurface(program: Command): void {
  program
    .name('yylo')
    .description('TypeScript implementation of yylo CLI tool for AI subagent orchestration')
    .version(VERSION, '-V, --version', 'Display version information')
    .helpOption('-h, --help', 'Display help information');
  setupGlobalOptions(program);
  configureInitCommand(program);
  configureStartCommand(program);
  configureTestCommand(program);
  configureFeedbackCommand(program);
  configureSessionCommand(program);
  configureSetupGitCommand(program);
  configureLogsCommand(program);
  configureViewLogCommand(program);
  configureHelpCommand(program);
  program.addCommand(createServicesCommand());
  program.addCommand(createSkillsCommand());
  program.addCommand(createAuthCommand());
  setupScriptManagementCommands(program);
  setupTaskLifecycleCommand(program);
  configureKanbanCommand(program);
  configureTaskWorkspaceCommand(program);
  configureIntegrationCommand(program);
  configureMergeQueueCommand(program);
  configureWatchCommand(program);
  configureEvidenceCommand(program);
  configureReleaseTrainCommand(program);
  configureMigrationCommand(program);
  configureWorkspaceCommands(program, VERSION);
  configureWikiCommand(program);
  configureLoopCommand(program);
  configureBenchmarkCommand(program);
  setupCompletion(program);
  setupAliases(program);
  setupContinueCommand(program);
  setupCloneCommand(program);
  setupNamedBranchCommands(program);
  setupContinuityCommand(program);
  setupContinueScopeCommand(program);
  setupMainCommand(program);
}

/**
 * Main CLI function
 */
async function main(): Promise<void> {
  const program = new Command();
  configureCommandSurface(program);

  // This check deliberately precedes environment/config/bootstrap installers.
  // The wrapper also invokes it in preflight-only mode before project bootstrap.
  const explicitInvocation = classifyExplicitInvocation(process.argv.slice(2), program);
  if (process.env.YYLO_HELP_PREFLIGHT_ONLY === '1') {
    // Private wrapper protocol: 80 means a semantic help option (not an option
    // value or post-`--` payload); zero means normal execution should continue.
    process.exitCode = explicitInvocation.kind === 'help' ? 80 : 0;
    return;
  }
  if (explicitInvocation.kind === 'unknown-command' || explicitInvocation.kind === 'unknown-option') {
    console.error(formatExplicitInvocationError(explicitInvocation, process.argv[1] ?? __filename, VERSION));
    process.exitCode = 2;
    return;
  }
  // Help is terminal discovery. Resolve its exact command from the configured
  // tree and print it before environment, controller, lifecycle, or installer
  // bootstrap can observe or mutate the workspace.
  if (explicitInvocation.kind === 'help') {
    explicitInvocation.command.outputHelp();
    return;
  }
  if (process.env.YYLO_PREFLIGHT_ONLY === '1') return;

  // Benchmark is an independent product. Delegate its untouched argument tail
  // before Commander can consume --help or the `--` delimiter and before YYLO
  // loads configuration, installs assets, or touches session state.
  const initialArgs = process.argv.slice(2);
  const initialLeading = classifyLeadingCommand(initialArgs);
  if (initialArgs[initialLeading.index] === 'benchmark') {
    await runBenchmarkDelegate(initialArgs.slice(initialLeading.index + 1));
    return;
  }
  if (
    initialArgs[initialLeading.index] === 'ledger' &&
    !hasManagedWorkspaceMarker(process.cwd())
  ) {
    await runLedgerDelegate(initialArgs.slice(initialLeading.index + 1));
    return;
  }

  // Configure environment only after explicit input has passed the no-mutation preflight.
  configureEnvironment();

  // Identity discovery must not refresh scripts, skills, services, or project state.
  const cliArgs = process.argv.slice(2);
  const leading = classifyLeadingCommand(cliArgs);
  const commandArgs = cliArgs.slice(leading.index);
  const isReadOnlyVersionRequest = cliArgs.some((arg) => arg === '--version' || arg === '-V');
  const isLifecycleCommand = commandArgs[0] === 'lifecycle';
  const isTaskWorkspaceCommand = commandArgs[0] === 'task';
  const isIntegrationCommand = commandArgs[0] === 'integration';
  const isMigrationCommand = commandArgs[0] === 'migrate';
  const isReadOnlyLifecycleStatus = isLifecycleCommand && commandArgs[1] === 'status';
  const isReadOnlyTaskStatus = isTaskWorkspaceCommand && commandArgs[1] === 'status';
  const isScriptsDoctor = commandArgs[0] === 'scripts' && commandArgs[1] === 'doctor';
  const isWorkspaceDiscovery = commandArgs[0] === 'info' || commandArgs[0] === 'where' || (commandArgs[0] === 'doctor' && commandArgs[1] === 'workspace');
  const isControlPlaneCommand = ['ledger', 'kanban', 'task', 'merge', 'integration'].includes(commandArgs[0] ?? '');
  const isReadOnlyIdentityRequest = isReadOnlyVersionRequest || isReadOnlyLifecycleStatus || isReadOnlyTaskStatus || isMigrationCommand || isScriptsDoctor || isWorkspaceDiscovery || isControlPlaneCommand;
  const isForceUpdate = process.argv.includes('--force-update');
  const isExplicitProjectAssetUpdate =
    isForceUpdate ||
    commandArgs[0] === 'install-scripts' ||
    (commandArgs[0] === 'scripts' && commandArgs[1] === 'update');
  const { ScriptInstaller: StartupScriptInstaller } = await import('../utils/script-installer.js');
  const isMetadataOnlyController =
    await StartupScriptInstaller.isMetadataOnlyController(process.cwd());
  // Implicit startup writes require resolver-confirmed controller identity. Do
  // this once, before any project installer, and let invalid registration fail
  // closed before command parsing or agent dispatch. Explicit update commands
  // retain their documented authority in every initialized workspace.
  const mayAutoUpdateProjectAssets =
    !isReadOnlyIdentityRequest && !isLifecycleCommand && !isTaskWorkspaceCommand && !isIntegrationCommand &&
    (isForceUpdate || (
      !isExplicitProjectAssetUpdate && resolveAutomaticProjectBootstrap(process.cwd()).allowed
    ));
  // Config/env bootstrap consumes the same decision; do not let a later config
  // load reintroduce project writes after installers were correctly skipped.
  process.env.YYLO_PROJECT_BOOTSTRAP_WRITES =
    mayAutoUpdateProjectAssets && !isMetadataOnlyController ? '1' : '0';

  // Auto-update service scripts if package version changed (silent operation)
  // This ensures users always have the latest service scripts after npm upgrade
  try {
    if (!isReadOnlyIdentityRequest && !isExplicitProjectAssetUpdate) {
      const { ServiceInstaller } = await import('../utils/service-installer.js');

    if (isForceUpdate) {
      console.log(
        chalk.blue('🔄 Force updating service scripts (codex.py, claude.py, gemini.py)...'),
      );
    }

    const updated = await ServiceInstaller.autoUpdate(isForceUpdate);

    // Show update message in force update mode or YYLO_DEBUG
    if (
      updated &&
      (isForceUpdate ||
        process.env.YYLO_DEBUG === '1')
    ) {
      if (isForceUpdate) {
        console.log(chalk.green('✓ Service scripts reinstalled'));
      } else {
        console.error('[DEBUG] Service scripts auto-updated to latest version');
      }
    }
    }
  } catch (error) {
    // Log error in debug mode, but don't break CLI
    if (process.env.YYLO_DEBUG === '1') {
      console.error(
        '[DEBUG] Service auto-update failed:',
        error instanceof Error ? error.message : String(error),
      );
    }
  }

  // Auto-update project scripts (e.g., run_until_completion.sh) in .juno_task/scripts/
  // This ensures scripts are always in sync with the package version (installs missing + updates outdated)
  // If --force-update flag is present, force reinstall all scripts and bypass Python dependency cache

  try {
    if (mayAutoUpdateProjectAssets) {
      const { ScriptInstaller } = await import('../utils/script-installer.js');

    if (isForceUpdate) {
      // Force update all scripts and Python dependencies
      console.log(chalk.blue('🔄 Force updating scripts and Python dependencies...'));
      await ScriptInstaller.forceUpdateAll(process.cwd(), false);
      console.log(chalk.green('✓ Force update completed'));
    } else {
      // Normal auto-update (only missing/outdated scripts)
      const updated = await ScriptInstaller.autoUpdate(process.cwd(), true);

      // Show update message in YYLO_DEBUG mode
      if (updated && process.env.YYLO_DEBUG === '1') {
        console.error('[DEBUG] Project scripts auto-updated in .juno_task/scripts/');
      }
    }
    }
  } catch (error) {
    // Log error in debug mode, but don't break CLI
    if (process.env.YYLO_DEBUG === '1') {
      console.error(
        '[DEBUG] Script auto-update failed:',
        error instanceof Error ? error.message : String(error),
      );
    }
  }

  // Auto-update agent skill files in .agents/skills/ and .claude/skills/
  // Skills are installed for ALL agents regardless of which subagent is selected
  try {
    if (mayAutoUpdateProjectAssets) {
      const { SkillInstaller } = await import('../utils/skill-installer.js');

    if (isForceUpdate) {
      console.log(chalk.blue('🔄 Force updating agent skill files...'));
      await SkillInstaller.autoUpdate(process.cwd(), true);
      console.log(chalk.green('✓ Agent skill files updated'));
    } else {
      const updated = await SkillInstaller.autoUpdate(process.cwd());

      if (updated && process.env.YYLO_DEBUG === '1') {
        console.error('[DEBUG] Agent skill files auto-updated');
      }
    }
    }
  } catch (error) {
    if (process.env.YYLO_DEBUG === '1') {
      console.error(
        '[DEBUG] Skill auto-update failed:',
        error instanceof Error ? error.message : String(error),
      );
    }
  }

  // Determine verbose level from argv (before Commander parses)
  const isQuiet = process.argv.includes('--quiet') || process.argv.includes('-q') || process.argv.includes('--silent');
  const isVerbose: number = isQuiet ? 0 : normalizeVerbose(
    process.argv.includes('--verbose') || process.argv.includes('-v')
      ? (() => {
          // Find the value after -v/--verbose (if any)
          const idx = process.argv.indexOf('--verbose') !== -1
            ? process.argv.indexOf('--verbose')
            : process.argv.indexOf('-v');
          const next = process.argv[idx + 1];
          // If next arg exists and doesn't start with '-', it's the value
          if (next && !next.startsWith('-')) return next;
          return true; // -v without value → level 1
        })()
      : undefined, // no -v flag at all → normalizeVerbose returns 1 (default)
  );
  displayBanner(isWorkspaceDiscovery ? 0 : isVerbose);

  // Add comprehensive help (scoped to root command only — 'before'/'after' not 'beforeAll'/'afterAll')
  program.addHelpText(
    'before',
    `
${chalk.blue.bold('🎯 YYLO')} - TypeScript CLI for AI Subagent Orchestration

`,
  );

  program.addHelpText(
    'after',
    `
${chalk.blue.bold('Examples:')}
  ${chalk.gray('# Initialize new project (interactive mode)')}
  yylo init

  ${chalk.gray('# Initialize with inline mode (automation-friendly)')}
  yylo init 'Build a REST API' --subagent claude --git-repo https://github.com/user/repo

  ${chalk.gray('# Start execution using .juno_task/init.md')}
  yylo start

  ${chalk.gray('# AI-powered testing')}
  yylo test --generate --run
  yylo test src/utils.ts --subagent claude
  yylo test --analyze --coverage

  ${chalk.gray('# Quick execution with Claude')}
  yylo claude 'Analyze this codebase and suggest improvements'

  ${chalk.gray('# Short aliases')}
  yy pi --live 'hello'
  ypl 'hello'     ${chalk.gray('# same as: yy pi --live hello')}

  ${chalk.gray('# Pipe prompt via stdin (heredoc, pipe, redirect)')}
  echo 'Analyze this codebase' | yylo -s claude
  yylo -s claude << 'EOF'
  Analyze this codebase and suggest improvements
  EOF

  ${chalk.gray('# Shell safety')}
  ${chalk.gray('Use single quotes or -f/stdin when prompts include backticks or $()')}

  ${chalk.gray('# Prompt-time command substitution (per iteration)')}
  yylo claude -p "Status: !'git status --short'"
  yylo claude -i 3 -p "Recent commits:\n!\`\`\`git log -n 5 --oneline\`\`\`"

  ${chalk.gray('# Interactive project setup')}
  yylo init --interactive

  ${chalk.gray('# Continue the last session without retyping settings')}
  yylo continue 'Implement the next step'
  yylo continue -p 'Continue from here'

  ${chalk.gray('# Clone Pi sessions for independent branches')}
  yylo clone 'Explore approach A'
  yylo clone --name C 'Explore C'
  yylo clone --from C --name M 'Explore M'
  yylo --resume <session-id> --clone '@@close_loop'  ${chalk.gray('# explicit id fork')}
  yylo branches
  yylo switch C
  yylo continue --clone 'Explore approach B'
  yylo --resume <session-id> --clone 'Explore approach C'

  ${chalk.gray('# Force-refresh installed project scripts from the current package')}
  yylo scripts update --force
  yylo install-scripts --force

  ${chalk.gray('# Query continue scope hash/status for scripts')}
  yylo continue-scope --json
  yylo continue-scope A1B2C3 --json

  ${chalk.gray('# Manage sessions')}
  yylo session list
  yylo session info abc123

  ${chalk.gray('# Enable feedback collection globally')}
  yylo --enable-feedback start

  ${chalk.gray('# Collect feedback')}
  yylo feedback --interactive

  ${chalk.gray('# Import Codex auth into Pi auth.json')}
  yylo auth import-codex

  ${chalk.gray('# Set subagent-specific default models')}
  yylo pi set-default-model :api-codex
  yylo claude set-default-model :opus

  ${chalk.gray('# Setup Git repository')}
  yylo setup-git https://github.com/yylo-dev/yylo

  ${chalk.gray('# Verbose is ON by default. Disable with:')}
  yylo -v false -s claude 'prompt'
  yylo -v 0 -s claude 'prompt'
  yylo -v no -s claude 'prompt'

  ${chalk.gray('# Quiet mode (suppress agent output and hooks):')}
  yylo --quiet -s claude 'prompt'
  yylo --silent -s claude 'prompt'

${chalk.blue.bold('Environment Variables:')}
  YYLO_SUBAGENT              Default subagent (claude, cursor, codex, gemini, pi)
  YYLO_CONFIG                Configuration file path
  YYLO_VERBOSE               Verbose output (true/false/0/1/no/yes, default: true)
  YYLO_ENABLE_FEEDBACK       Enable concurrent feedback collection (true/false)
  YYLO_MCP_TIMEOUT           Operation timeout in milliseconds
  YYLO_ON_HOURLY_LIMIT       Behavior when quota limit reached (wait/raise)
  JUNO_INTERACTIVE_FEEDBACK_MODE  Enable interactive feedback mode (true/false)
  NO_COLOR                        Disable colored output (standard)

${chalk.blue.bold('Env File Bootstrap:')}
  Auto-creates ${chalk.cyan('.env.yylo')} in project root and loads it on startup.
  (Python virtual environment path is ${chalk.cyan('.venv_juno')}; this is separate from env files.)
  Configure custom env file in ${chalk.cyan('.juno_task/config.json')}:
    ${chalk.gray('"envFilePath": ".env.local", "envFileCopied": true')}

${chalk.blue.bold('Prompt Macros (@@key):')}
  Define reusable prompt macros in ${chalk.cyan('.juno_task/config.json')} under ${chalk.cyan('promptMacros')}.
  Example: ${chalk.gray('"promptMacros": {"global": {"git": "commit changes", "spec": {"path": "prompts/spec.md"}}, "local": {"ship": "run tests then @@git"}, "maxDepth": 10}')}
  Values can be strings or {path}/{text}; relative paths resolve from the project working directory.
  Local keys override global keys. Default order is before command substitution.

${chalk.blue.bold('Configuration:')}
  Configuration can be specified via:
  1. Command line arguments (highest priority)
  2. Environment variables
  3. Configuration files (.json, .toml, pyproject.toml)
  4. Built-in defaults (lowest priority)

${chalk.blue.bold('Support:')}
  Documentation: https://github.com/yylo-dev/yylo#readme
  Issues: https://github.com/yylo-dev/yylo/issues
  Website: https://yylo.dev
  License: MIT

`,
  );

  // Parse and execute
  try {
    await program.parseAsync(process.argv);
  } catch (error) {
    handleCLIError(error, isVerbose);
  }
}

/**
 * Global error handlers
 */
process.on('unhandledRejection', async (reason, promise) => {
  try {
    if (isConnectionLikeError(reason)) {
      // Log and continue (don’t exit) for transient pipe/socket issues
      const { getMCPLogger } = await import('../utils/logger.js');
      const logger = getMCPLogger();
      await logger.error(`[Process][unhandledRejection][connection] ${String(reason)}`, false);
      const verbose = process.argv.includes('--verbose') || process.argv.includes('-v');
      if (verbose) {
        console.error(chalk.yellow('\n⚠️  Transient connection issue (continuing):'));
        console.error(chalk.gray('   Reason:'), reason);
      }
      return; // do not exit
    }
  } catch {
    // fall through to default handler
  }
  console.error(chalk.red.bold('\n💥 Unhandled Promise Rejection'));
  console.error(chalk.red('   This is likely a bug. Please report it.'));
  console.error(chalk.gray('   Promise:'), promise);
  console.error(chalk.gray('   Reason:'), reason);
  process.exit(EXIT_CODES.UNEXPECTED_ERROR);
});

process.on('uncaughtException', async (error) => {
  try {
    if (isConnectionLikeError(error)) {
      const { getMCPLogger } = await import('../utils/logger.js');
      const logger = getMCPLogger();
      await logger.error(`[Process][uncaughtException][connection] ${error.message}`, false);
      const verbose = process.argv.includes('--verbose') || process.argv.includes('-v');
      if (verbose) {
        console.error(chalk.yellow('\n⚠️  Transient connection exception (continuing):'));
        console.error(chalk.gray('   Error:'), error.message);
      }
      return; // do not exit
    }
  } catch {
    // ignore and fall through
  }
  console.error(chalk.red.bold('\n💥 Uncaught Exception'));
  console.error(chalk.red('   This is likely a bug. Please report it.'));
  console.error(chalk.gray('   Error:'), error.message);
  console.error(chalk.gray('   Stack:'), error.stack);
  process.exit(EXIT_CODES.UNEXPECTED_ERROR);
});

// Handle Ctrl+C gracefully — show session ID so user can resume later
process.on('SIGINT', () => {
  if (forwardBenchmarkSignal('SIGINT') || forwardLedgerSignal('SIGINT')) return;
  const sessionId = _getActiveSessionId?.();
  if (sessionId) {
    console.log(chalk.cyan(`\n\n🔑 Session ID: ${sessionId}`));
  } else {
    console.log(''); // blank line before cancellation message
  }
  console.log(chalk.yellow('⚠️  Execution cancelled by user'));
  // ShellBackend's prepended owner forwards cancellation to its complete Pi/tool
  // process group and escalates before this bounded wrapper exit.
  setTimeout(() => process.exit(130), 400);
});

// Handle SIGTERM gracefully
process.on('SIGTERM', () => {
  if (forwardBenchmarkSignal('SIGTERM') || forwardLedgerSignal('SIGTERM')) return;
  console.log(chalk.yellow('\n\n⚠️  Execution terminated'));
  setTimeout(() => process.exit(143), 400);
});

// Parent terminal/pipe owners commonly use SIGHUP for cancellation.
process.on('SIGHUP', () => {
  if (forwardBenchmarkSignal('SIGHUP') || forwardLedgerSignal('SIGHUP')) return;
  console.log(chalk.yellow('\n\n⚠️  Execution parent disconnected'));
  setTimeout(() => process.exit(129), 400);
});

process.on('SIGQUIT', () => {
  if (forwardBenchmarkSignal('SIGQUIT') || forwardLedgerSignal('SIGQUIT')) return;
  process.removeAllListeners('SIGQUIT');
  process.kill(process.pid, 'SIGQUIT');
});

// Export for testing
export { main, handleCLIError };

// Always run main() when this file is executed as a CLI. Wrapper preflight is
// deliberately excluded: it is an internal read-only classifier preceding the
// one user-visible invocation, not a second command lifecycle.
const reportFatalError = (error: unknown) => {
  console.error(chalk.red.bold('\n💥 Fatal Error'));
  console.error(chalk.red(`   ${error instanceof Error ? error.message : String(error)}`));
  process.exit(EXIT_CODES.UNEXPECTED_ERROR);
};

const versionOnly = process.argv.length === 3 && ['--version', '-V'].includes(process.argv[2]!);
if (versionOnly) {
  // Public execution adapters require a bounded, machine-parseable handshake.
  // Keep the exact version probe inside the normal durable invocation boundary,
  // but bypass Commander and all of its human-oriented output.
  const invocationLifecycle = new InvocationLifecycle({
    workingDirectory: process.cwd(),
    junoCodeVersion: VERSION,
    launchSurface: executableLaunchSurface ?? 'yylo',
    ...(wrapperContinuation ? { continuation: wrapperContinuation } : {}),
  });
  void runWithInvocationLifecycle(
    invocationLifecycle,
    async () => { process.stdout.write(`${VERSION}\n`); },
  ).catch(reportFatalError);
} else if (process.env.YYLO_PREFLIGHT_ONLY === '1' ||
  process.env.YYLO_HELP_PREFLIGHT_ONLY === '1' ||
  process.env.YYLO_RUNTIME_PROBE === '1') {
  void main().catch(reportFatalError);
} else {
  const observationProgram = new Command();
  configureCommandSurface(observationProgram);
  const initialInvocation = classifyExplicitInvocation(process.argv.slice(2), observationProgram);
  if (initialInvocation.kind === 'help') {
    // Do not create even the durable invocation-attempt record for help.
    void main().catch(reportFatalError);
  } else {
    const requestObservation = observeCommanderRequest(observationProgram);
    const invocationLifecycle = new InvocationLifecycle({
      workingDirectory: process.cwd(),
      junoCodeVersion: VERSION,
      launchSurface: executableLaunchSurface ?? 'yylo',
      ...(wrapperContinuation ? { continuation: wrapperContinuation } : {}),
    });
    void runWithInvocationLifecycle(invocationLifecycle, main, requestObservation).catch(reportFatalError);
  }
}
