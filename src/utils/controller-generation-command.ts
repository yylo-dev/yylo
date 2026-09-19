import { spawn } from 'node:child_process';
import fs from 'fs-extra';
import path from 'node:path';
import { constants } from 'node:os';
import { Command } from 'commander';
import { withWaitingProgress } from './terminal-progress-writer.js';
import { resolveController } from './controller-resolver.js';
import { ScriptInstaller } from './script-installer.js';
import { applyControllerGeneration, packagedGenerationRoot, recoverControllerGeneration, GENERATION_MIGRATION_ROOT } from './controller-generation-migration.js';
import { assessControllerGeneration, admitControllerCommand, upgradeControllerGeneration, prepareInstalledControllerRepair } from './controller-generation-startup.js';

export const YYLO_CONTROLLER_GENERATION_DISPATCH_V1 = true;
let releaseDispatch: (() => Promise<void>) | undefined;
export async function releaseControllerCommand(): Promise<void> {
  const release = releaseDispatch;
  releaseDispatch = undefined;
  if (release) await release();
}

/** Filesystem ownership proof, not interpretation of ambiguous Git exit codes. */
export async function assertExternalGenerationPlan(file: string): Promise<void> {
  if (!file || !path.isAbsolute(file) || path.resolve(file) !== file
      || await fs.realpath(path.dirname(file)) !== path.dirname(file)) {
    throw new Error('Repair plan must be an absolute non-symlink path outside Git');
  }
  for (const key of ['GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_INDEX_FILE']) {
    if (process.env[key]) throw new Error('Repair plan ownership inspection refuses inherited Git routing');
  }
  for (let cursor = path.dirname(file);;) {
    for (const name of ['.git', 'HEAD']) {
      try {
        await fs.lstat(path.join(cursor, name));
        throw new Error('Repair plan must be outside Git; repository marker or ambiguous bare-repository path');
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
      }
    }
    const parent = path.dirname(cursor);
    if (parent === cursor) break;
    cursor = parent;
  }
}

const AGENT_COMMANDS = ['pi', 'claude', 'cursor', 'codex', 'gemini', 'start', 'continue', 'contiue', 'cn', 'cc', 'clone', 'loop'];

/** Use the registered CLI option grammar, not a scan through prompt/file values. */
export function generationInvocationContext(program: Command, argv: string[], cwd: string): { cwd: string; version: boolean; commandArgs: string[]; quiet?: boolean } {
  const parserFor = (commands: Command[]) => {
    const parser = new Command().allowUnknownOption().exitOverride().configureOutput({ writeErr: () => {} });
    const flags = new Set<string>();
    for (const command of commands) for (const option of command.options) {
      const names = [option.short, option.long].filter((name): name is string => !!name);
      if (names.some(name => flags.has(name))) continue;
      parser.addOption(option);
      names.forEach(name => flags.add(name));
    }
    return parser;
  };
  const isQuiet = (options: Record<string, unknown>): boolean => options.quiet === true || options.silent === true
    || ['0', 'false', 'no'].includes(String(options.verbose ?? process.env.YYLO_VERBOSE ?? '').toLowerCase());
  // Stop at a real registered command, consuming root option values first.
  // In particular, `-s pi -p hello` is the default agent, not command `pi`.
  const root = parserFor([program]).enablePositionalOptions();
  for (const command of program.commands) root.command(command.name()).aliases(command.aliases());
  const parsed = root.parseOptions(argv);
  const selected = program.commands.find(command => command.name() === parsed.operands[0] || command.aliases().includes(parsed.operands[0] ?? ''));
  const commandArgs = selected ? [...parsed.operands, ...parsed.unknown] : [];
  if (selected && selected.name() !== 'scripts' && !AGENT_COMMANDS.includes(commandArgs[0]!)) {
    return { cwd, version: root.opts().version === true, commandArgs, ...(isQuiet(root.opts()) ? { quiet: true } : {}) };
  }
  const chain = [program];
  for (const token of commandArgs) {
    const child = chain[0]!.commands.find(command => command.name() === token || command.aliases().includes(token));
    if (!child) break;
    chain.unshift(child);
  }
  // A separate parser avoids modifying the command later used for execution.
  const parser = parserFor(chain);
  parser.parseOptions(argv);
  const options = parser.opts();
  const quiet = isQuiet(options);
  return { cwd: typeof options.cwd === 'string' ? path.resolve(cwd, options.cwd) : cwd,
    version: options.version === true, commandArgs, ...(quiet ? { quiet: true } : {}) };
}

export function generationCommandKind(args: string[]): 'read' | 'execute' | 'maintenance' | 'skip' {
  const [command = '', operation = ''] = args;
  if (command === 'scripts' && operation === 'generation') return 'maintenance';
  if (['info', 'where', 'capabilities', 'doctor'].includes(command)) return 'read';
  if ((command === 'scripts' && operation === 'doctor')
      || (command === 'integration' && ['status', 'runtime-doctor'].includes(operation))
      || (command === 'task' && ['status', 'admission', 'preflight', 'doctor', 'lease-status', 'evidence-status'].includes(operation))) return 'read';
  if (['task', 'merge'].includes(command) && operation && !['local', 'runtime-bootstrap'].includes(operation)) return 'execute';
  if (!command || AGENT_COMMANDS.includes(command)) return 'execute';
  return 'skip';
}

/** Runs before any installer or local runtime selection. Discovery never migrates. */
export async function prepareControllerCommand(cwd: string, commandArgs: string[], rawArgs: string[], invocationCwd = cwd, progressEnabled = true): Promise<boolean> {
  const kind = generationCommandKind(commandArgs);
  if (kind === 'skip') return false;
  // Presence only; routing/authority comes exclusively from the installed resolver.
  let cursor = path.resolve(cwd);
  while (!(await fs.pathExists(path.join(cursor, '.juno_task')))) {
    const parent = path.dirname(cursor);
    if (parent === cursor) return false;
    cursor = parent;
  }
  const authority = resolveController(cwd, 'diagnostic', { trustedResolver: true });
  if (authority.role === 'simple' || !(await ScriptInstaller.isMetadataOnlyController(authority.path))) return false;
  const registered = authority.source === 'environment'
    ? resolveController(cwd, 'diagnostic', { trustedResolver: true, ignoreEnvironmentAssertions: true }) : authority;
  if (!authority.valid || !registered.valid || registered.source !== 'registration' || registered.path !== authority.path) {
    throw new Error('generation_registration_invalid: exact registered controller required');
  }
  const controller = authority.path;
  const packageRoot = packagedGenerationRoot();
  if (kind === 'maintenance') {
    const operation = commandArgs[2];
    if (operation === 'readiness') {
      const { controllerGenerationReadiness } = await import('./controller-generation-readiness.js');
      const report = await controllerGenerationReadiness(controller, packageRoot);
      console.log(JSON.stringify(report));
      if (report.disposition !== 'ready') process.exitCode = 2;
    } else if (operation === 'doctor') {
      const assessment = await assessControllerGeneration(controller, packageRoot);
      // Plans contain exact preimage bytes (including Git config): diagnostics
      // expose identity and paths, never dump those private payloads to stdout.
      console.log(JSON.stringify(assessment.disposition === 'migration_required' ? {
        disposition: assessment.disposition, controller, transactionId: assessment.plan.id,
        candidate: assessment.plan.candidate, previous: assessment.plan.previous,
        paths: Object.keys(assessment.plan.after as object),
        activeTasks: Object.keys(assessment.plan.active_pins),
      } : assessment));
      if (assessment.disposition === 'refused' || assessment.disposition === 'transition_incomplete') process.exitCode = 2;
    } else if (operation === 'upgrade') {
      console.log(JSON.stringify(await withWaitingProgress('Authenticating explicit controller upgrade…',
        () => upgradeControllerGeneration(controller, packageRoot), progressEnabled)));
    } else if (operation === 'repair-plan' || operation === 'repair-apply') {
      const file = commandArgs[3];
      if (!file) throw new Error('Absolute external repair plan path required');
      await assertExternalGenerationPlan(file);
      if (operation === 'repair-plan') {
        const plan = await prepareInstalledControllerRepair(controller, packageRoot);
        await fs.writeFile(file, JSON.stringify(plan), { flag: 'wx', mode: 0o600 });
        console.log(JSON.stringify({ operation, path: file, id: plan.id,
          reviewRequired: plan.review_required, paths: Object.keys(plan.after as object),
          next: `yy scripts generation repair-apply ${file} ${plan.id}` }));
      } else {
        const stat = await fs.lstat(file);
        if (!stat.isFile() || stat.size > 64 * 1024 * 1024) throw new Error('Unsafe or oversized repair plan');
        const plan = await fs.readJson(file);
        if (plan.repair !== true || !/^[a-f0-9]{64}$/.test(commandArgs[4] ?? '') || plan.id !== commandArgs[4]) {
          throw new Error('Explicit reviewed repair plan ID required');
        }
        console.log(JSON.stringify(await applyControllerGeneration(controller, plan)));
      }
    } else if (operation === 'resume' || operation === 'rollback') {
      const id = commandArgs[3] ?? '';
      if (!/^[a-f0-9]{64}$/.test(id ?? '')) throw new Error('Exact generation transaction ID required');
      console.log(JSON.stringify(await recoverControllerGeneration(controller, id, operation === 'rollback')));
    } else throw new Error('Use yy scripts generation readiness|doctor|upgrade|repair-plan|repair-apply|resume|rollback');
    return true;
  }
  if (kind === 'read') {
    const assessment = await assessControllerGeneration(controller, packageRoot);
    const generationDoctor = (commandArgs[0] === 'integration' && commandArgs[1] === 'runtime-doctor')
      || (commandArgs[0] === 'scripts' && commandArgs[1] === 'doctor');
    if (generationDoctor && await fs.pathExists(path.join(controller, GENERATION_MIGRATION_ROOT, 'current.json'))) {
      // Activated package generations no longer inherit the legacy target-bound
      // receipt format. Do not let that older doctor disagree with admission.
      console.log(JSON.stringify(assessment.disposition === 'migration_required'
        ? { disposition: assessment.disposition, controller, transactionId: assessment.plan.id }
        : assessment));
      if (assessment.disposition !== 'ready') process.exitCode = 2;
      return true;
    }
    if (assessment.disposition !== 'ready') {
      console.error(`Controller generation: ${assessment.disposition}; inspect yy scripts generation doctor`);
      if (assessment.disposition === 'refused' || assessment.disposition === 'transition_incomplete') process.exitCode = 2;
    }
    return false;
  }
  // Authenticate after acquiring the reader lease: no pre-lock assessment to
  // race, and no writer acquisition or upgrade while holding a reader lease.
  const admission = await withWaitingProgress('Authenticating active controller runtime…',
    () => admitControllerCommand(controller, packageRoot), progressEnabled);
  releaseDispatch = admission.release;
  const assessment = admission.assessment;
  if (assessment.disposition !== 'retained') {
    // The hop ends only after this runtime passes admission and the locked
    // readback. Do not leak its loop guard into providers/hooks and their new
    // CLI invocations. This marker grants no runtime or controller authority.
    if (assessment.disposition === 'ready') delete process.env.YYLO_GENERATION_REDISPATCH;
    return false;
  }
  if (process.env.YYLO_GENERATION_REDISPATCH) {
    await releaseControllerCommand();
    throw new Error('generation_dispatch_cycle: YYLO_CONTROLLER_GENERATION_DISPATCH_V1 permits only one retained-runtime hop');
  }
  if (progressEnabled) console.error(`Using retained controller runtime: ${assessment.reason}`);
  const exit = await new Promise<number>((resolve, reject) => {
    const child = spawn(process.execPath, [assessment.executable, ...rawArgs], {
      cwd: invocationCwd, stdio: 'inherit', env: { ...process.env, YYLO_GENERATION_REDISPATCH: assessment.executable },
    });
    const signals = ['SIGINT', 'SIGTERM', 'SIGHUP', 'SIGQUIT'] as const;
    const forwarding = signals.map(signal => {
      const forward = () => { child.kill(signal); };
      process.on(signal, forward);
      return () => process.removeListener(signal, forward);
    });
    const cleanup = () => { for (const remove of forwarding) remove(); };
    child.once('error', error => { cleanup(); reject(error); });
    child.once('exit', (code, signal) => {
      cleanup();
      resolve(signal ? 128 + (constants.signals[signal] ?? 1) : code ?? 1);
    });
  }).finally(releaseControllerCommand);
  process.exitCode = exit;
  return true;
}
