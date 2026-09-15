import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import fs from 'fs-extra';
import { Command } from 'commander';
import { routeControlPlane } from '../../utils/control-plane-router.js';
import { checkpointControllerAfterFinalization } from '../../utils/controller-checkpoint.js';
import { addMachineOutputOptions, invokeMachineAwareChild, resolveMachineOutput } from '../machine-output.js';

export type TaskWorkspaceOperation =
  | 'start'
  | 'run'
  | 'resume'
  | 'recover-predispatch'
  | 'recover-wall-budget'
  | 'status'
  | 'admission'
  | 'hydrate'
  | 'preflight'
  | 'finish'
  | 'checkpoint'
  | 'child-checkpoint'
  | 'evidence-run'
  | 'evidence-status'
  | 'evidence-await'
  | 'sync'
  | 'doctor'
  | 'recovery-plan'
  | 'recovery-authorize'
  | 'recovery-apply'
  | 'recovery-verify'
  | 'lease-status'
  | 'lease-heartbeat'
  | 'lease-handoff'
  | 'lease-successor'
  | 'lease-revoke'
  | 'lease-release'
  | 'state-archive-plan'
  | 'state-archive-apply'
  | 'state-archive-verify'
  | 'state-archive-get'
  | 'state-archive-rollback';
export type TaskWorkspaceInvoker = (
  operation: TaskWorkspaceOperation,
  taskId: string,
  requiredPaths?: string[],
  admissionArgs?: string[],
) => Promise<void>;
export type TaskWorkspaceCheckpointer = typeof checkpointControllerAfterFinalization;
export type TaskRuntimeBootstrapOptions = { dryRun?: boolean; apply?: string };
export type TaskRuntimeBootstrapInvoker = (options: TaskRuntimeBootstrapOptions) => Promise<void>;

export function taskWorkspaceControlOperation(operation: TaskWorkspaceOperation | 'local'): 'kanban' | 'orchestration' {
  return ['local', 'status', 'admission', 'preflight', 'recovery-plan', 'recovery-verify', 'evidence-status', 'doctor', 'lease-status',
    'state-archive-plan', 'state-archive-verify', 'state-archive-get'].includes(operation) ? 'kanban' : 'orchestration';
}

export function packagedTaskRuntimeCandidates(): string[] {
  const directory = path.dirname(fileURLToPath(import.meta.url));
  return [
    path.resolve(directory, '../templates/scripts/task_workspace.py'),
    path.resolve(directory, '../../templates/scripts/task_workspace.py'),
    path.resolve(directory, '../../src/templates/scripts/task_workspace.py'),
  ];
}

export async function selectTaskWorkspaceRuntime(
  controllerRoot: string,
  operation: TaskWorkspaceOperation,
  packagedCandidates = packagedTaskRuntimeCandidates(),
): Promise<string> {
  const canonical = path.join(controllerRoot, '.juno_task', 'scripts', 'task_workspace.py');
  if (operation !== 'hydrate') {
    if (!(await fs.pathExists(canonical))) {
      throw new Error('Missing managed task workspace runtime. Run `yy scripts update` and retry.');
    }
    return canonical;
  }
  const packaged = packagedCandidates.find((candidate) => fs.existsSync(candidate));
  if (!packaged) {
    throw new Error('Packaged task-hydrate recovery engine is missing; refusing stale controller fallback.');
  }
  const runner = path.join(path.dirname(packaged), 'workflow_runner.sh');
  if (!(await fs.pathExists(runner))) {
    throw new Error('Packaged task-hydrate recovery engine is incomplete; refusing stale controller fallback.');
  }
  const source = await fs.readFile(packaged, 'utf8');
  const protocol = [
    'TASK_HYDRATE_RECOVERY_SCHEMA = "juno_task_hydrate_recovery.v1"',
    // Stable capability marker: the audited operation list evolves without
    // invalidating hydrate recovery selection.
    'TASK_RUNTIME_CAPABILITY_HYDRATE_V1 = True',
    'def hydrate(controller:',
  ];
  if (!protocol.every((marker) => source.includes(marker))) {
    throw new Error('Packaged task-hydrate recovery engine is incompatible; refusing stale controller fallback.');
  }
  return packaged;
}

export async function invokeTaskRuntimeBootstrap(
  options: TaskRuntimeBootstrapOptions,
  packagedCandidates = packagedTaskRuntimeCandidates(),
): Promise<void> {
  if (Boolean(options.dryRun) === Boolean(options.apply)) {
    throw new Error('task runtime-bootstrap requires exactly one of --dry-run or --apply <receipt>');
  }
  const route = routeControlPlane(process.cwd(), 'orchestration');
  const script = packagedCandidates.find((candidate) => fs.existsSync(candidate));
  if (!script) throw new Error('Packaged task-runtime bootstrap engine is missing.');
  const source = await fs.readFile(script);
  const required = [
    'RUNTIME_BOOTSTRAP_SCHEMA = "juno_target_task_runtime_bootstrap.v1"',
    'def runtime_bootstrap(',
    '"runtime-bootstrap"',
  ];
  if (!required.every((marker) => source.includes(Buffer.from(marker)))) {
    throw new Error('Packaged task-runtime bootstrap engine is incompatible.');
  }
  const packagePath = path.resolve(path.dirname(script), '../../..', 'package.json');
  const packageJson = await fs.readJson(packagePath) as { name?: string; version?: string };
  if (packageJson.name !== '@yylo/cli' || typeof packageJson.version !== 'string') {
    throw new Error('Packaged task-runtime identity is invalid.');
  }
  const hash = createHash('sha256').update(source).digest('hex');
  const argv = [script, 'runtime-bootstrap', '--controller', route.controllerRoot,
    '--package-version', packageJson.version, '--package-runtime-sha256', hash];
  if (options.dryRun) argv.push('--dry-run');
  else argv.push('--apply', path.resolve(options.apply!));
  const exitCode = await new Promise<number>((resolve, reject) => {
    const child = spawn('python3', argv, {
      cwd: route.controllerRoot, env: route.env, stdio: 'inherit',
    });
    child.once('error', reject);
    child.once('exit', (code, signal) => {
      if (signal) reject(new Error(`Task runtime bootstrap terminated by signal ${signal}`));
      else resolve(code ?? 1);
    });
  });
  if (exitCode !== 0) process.exitCode = exitCode;
}

export async function checkpointTaskWorkspaceAfterFinalization(
  operation: TaskWorkspaceOperation,
  controllerRoot: string,
  exitCode: number,
  checkpoint: TaskWorkspaceCheckpointer = checkpointControllerAfterFinalization,
  taskId?: string,
): Promise<void> {
  if (['status', 'admission', 'preflight', 'recovery-plan', 'recovery-verify', 'checkpoint', 'evidence-run', 'evidence-status', 'evidence-await', 'doctor', 'lease-status'].includes(operation)) return;
  if (taskId) await checkpoint(controllerRoot, exitCode, taskId);
  else await checkpoint(controllerRoot, exitCode);
}

export async function invokeTaskWorkspace(
  operation: TaskWorkspaceOperation,
  taskId: string,
  requiredPaths: string[] = [],
  admissionArgs: string[] = [],
): Promise<void> {
  const route = routeControlPlane(process.cwd(), taskWorkspaceControlOperation(operation));
  const controllerRoot = route.controllerRoot;
  const script = await selectTaskWorkspaceRuntime(controllerRoot, operation);
  const taskEnv = route.env;
  const pathArgs = requiredPaths.flatMap((requiredPath) => ['--path', requiredPath]);
  const machine = resolveMachineOutput(process.argv.slice(2), { jsonFlag: true });
  const { exitCode } = await invokeMachineAwareChild({
    executable: 'python3',
    args: [script, operation, ...(taskId ? ['--task', taskId] : []), ...pathArgs, ...admissionArgs],
    cwd: controllerRoot,
    env: taskEnv,
    command: `task.${operation}`,
    ...(machine ? { machine } : {}),
  });
  await checkpointTaskWorkspaceAfterFinalization(operation, controllerRoot, exitCode,
    checkpointControllerAfterFinalization, taskId);
  if (exitCode !== 0) process.exitCode = exitCode;
}

export async function invokeLocalTaskBookkeeping(args: string[]): Promise<void> {
  const route = routeControlPlane(process.cwd(), 'kanban', undefined, 'local-task-bookkeeping');
  if (route.invocationRole !== 'simple') {
    throw new Error('yy task local is only for Simple workspaces; use yy ledger or the managed task lifecycle here.');
  }
  const { runLedgerDelegate } = await import('./ledger.js');
  await runLedgerDelegate(args);
}

export function configureTaskWorkspaceCommand(
  program: Command,
  invoke: TaskWorkspaceInvoker = invokeTaskWorkspace,
  invokeBootstrap: TaskRuntimeBootstrapInvoker = invokeTaskRuntimeBootstrap,
  invokeLocal: (args: string[]) => Promise<void> = invokeLocalTaskBookkeeping,
): void {
  const task = addMachineOutputOptions(program
    .command('task')
    .description('Managed feature worktrees, or explicit Simple local bookkeeping'));
  const local = task.command('local').description('Simple Ledger bookkeeping only; no commits, isolation or delivery receipts');
  local.command('list').description('List local Ledger tasks')
    .action(() => invokeLocal(['list']));
  local.command('get <task-id>').description('Read one local Ledger task')
    .action((id: string) => {
      if (!/^[A-Za-z0-9]{6}$/.test(id)) throw new Error('Expected a six-character Ledger task ID.');
      return invokeLocal(['get', id]);
    });
  local.command('mark <status> <task-id>').description('Update Ledger bookkeeping, not managed delivery')
    .requiredOption('--response <text>', 'Reason for the bookkeeping change')
    .action((status: string, id: string, options: { response: string }) => {
      if (!['backlog', 'todo', 'in_progress', 'done'].includes(status)) throw new Error('Unsupported Ledger task status.');
      if (!/^[A-Za-z0-9]{6}$/.test(id)) throw new Error('Expected a six-character Ledger task ID.');
      return invokeLocal(['mark', status, '--id', id, '--response', options.response]);
    });
  task
    .command('run')
    .description('Execute the managed workflow through QUEUED; acquires its own fence without --lease-token')
    .argument('<task-id>', 'Canonical YYLO Ledger task ID')
    .action((taskId: string) => invoke('run', taskId, []));
  task
    .command('resume')
    .description('Resume managed task run with its own fence; existing blockers and budgets still apply')
    .argument('<task-id>', 'Canonical YYLO Ledger task ID')
    .action((taskId: string) => invoke('resume', taskId, []));
  task
    .command('recover-predispatch')
    .description('Release one receipt-proven no-provider task-run attempt without spending model budget')
    .argument('<task-id>', 'Canonical YYLO Ledger task ID')
    .requiredOption('--run-id <run-id>', 'Exact active task-run identity')
    .action((taskId: string, options: { runId: string }) => invoke(
      'recover-predispatch', taskId, [], ['--run-id', options.runId],
    ));
  task
    .command('recover-wall-budget')
    .description('Recover once the wall interval proven by an integrated no-provider receipt')
    .argument('<task-id>', 'Canonical YYLO Ledger task ID')
    .requiredOption('--run-id <run-id>', 'Exact active task-run identity')
    .requiredOption('--attempt <index>', 'Exact worker attempt index')
    .requiredOption('--predispatch-receipt-sha256 <sha256>', 'Exact controller pre-dispatch receipt digest')
    .requiredOption('--original-deadline-unix-ns <unix-ns>', 'Immutable original task-run deadline')
    .action((taskId: string, options: {
      runId: string;
      attempt: string;
      predispatchReceiptSha256: string;
      originalDeadlineUnixNs: string;
    }) => invoke('recover-wall-budget', taskId, [], [
      '--run-id', options.runId,
      '--attempt', options.attempt,
      '--predispatch-receipt-sha256', options.predispatchReceiptSha256,
      '--original-deadline-unix-ns', options.originalDeadlineUnixNs,
    ]));
  task
    .command('start')
    .description('Start a hydrated worktree; retain the returned token for later manual gated commands')
    .argument('<task-id>', 'Canonical YYLO Ledger task ID')
    .option('--path <path>', 'Exact authored file (including one policy-declared new file) or selectable product root; repeat for exact scope', (value, values: string[]) => [...values, value], [])
    .option('--lease-token <token>', 'Current fencing lease token for this gated mutation')
    .action((taskId: string, options: { path: string[]; leaseToken?: string }) => invoke(
      'start', taskId, options.path,
      options.leaseToken ? ['--lease-token', options.leaseToken] : [],
    ));
  task.command('admission')
    .description('Read-only exact authored-path and dirty-path admission check')
    .argument('<task-id>', 'Canonical YYLO Ledger task ID')
    .action((taskId: string) => invoke('admission', taskId, []));
  task.command('preflight')
    .description('Read-only finish/admission check before expensive validation')
    .argument('<task-id>', 'Canonical YYLO Ledger task ID')
    .action((taskId: string) => invoke('preflight', taskId, []));
  task.command('checkpoint')
    .description('Plan validation or accept one ordered checkpoint on the ordinary delivery')
    .argument('<task-id>', 'Canonical YYLO Ledger task ID')
    .option('--accept <checkpoint-id>', 'Run/reuse exact evidence and accept this frozen checkpoint')
    .option('--lease-token <token>', 'Current fencing lease token for this gated mutation')
    .action((taskId: string, options: { accept?: string; leaseToken?: string }) => invoke(
      'checkpoint', taskId, [], [
        ...(options.accept ? ['--accept-checkpoint', options.accept] : []),
        ...(options.leaseToken ? ['--lease-token', options.leaseToken] : []),
      ],
    ));
  task.command('hydrate')
    .description('Rerun the frozen task hydration workflow on a clean task worktree')
    .argument('<task-id>', 'Canonical YYLO Ledger task ID')
    .option('--lease-token <token>', 'Current fencing lease token for this gated mutation')
    .action((taskId: string, options: { leaseToken?: string }) => invoke(
      'hydrate', taskId, [], options.leaseToken ? ['--lease-token', options.leaseToken] : [],
    ));
  task.command('status')
    .description('Read-only state, producer fence, prior terminal evidence, and one eligible action')
    .argument('<task-id>', 'Canonical YYLO Ledger task ID')
    .action((taskId: string) => invoke('status', taskId, []));
  task.command('finish')
    .description('Queue only after live state/fence admission and exact reusable validation')
    .argument('<task-id>', 'Canonical YYLO Ledger task ID')
    .option('--lease-token <token>', 'Current fencing lease token for this gated mutation')
    .action((taskId: string, options: { leaseToken?: string }) => invoke(
      'finish', taskId, [], options.leaseToken ? ['--lease-token', options.leaseToken] : [],
    ));
  task.command('doctor')
    .description('Read-only batched Ledger reconciliation; partial/non-atomic coverage is explicit')
    .argument('[task-id]', 'Optional exact task ID, including cold-archived tasks')
    .option('--limit <count>', 'Maximum lifecycle rows (1-1000; default 1000)')
    .option('--offset <count>', 'Skip sorted lifecycle rows (default 0; not a snapshot cursor)')
    .action((taskId: string | undefined, options: { limit?: string; offset?: string }) => invoke(
      'doctor', taskId ?? '', [], [
        ...(options.limit !== undefined ? ['--limit', options.limit] : []),
        ...(options.offset !== undefined ? ['--offset', options.offset] : []),
      ],
    ));
  task.command('sync')
    .description('Recover one pending lifecycle Kanban projection (exact recovery command)')
    .argument('<task-id>', 'Canonical YYLO Ledger task ID')
    .option('--lease-token <token>', 'Current fencing lease token for this gated mutation')
    .action((taskId: string, options: { leaseToken?: string }) => invoke(
      'sync', taskId, [], options.leaseToken ? ['--lease-token', options.leaseToken] : [],
    ));
  task.command('lease-status')
    .description('Read-only fencing lease observation with actionable reason codes')
    .argument('<task-id>', 'Canonical YYLO Ledger task ID')
    .action((taskId: string) => invoke('lease-status', taskId, []));
  task.command('lease-heartbeat')
    .description('Refresh the active lease heartbeat (holder token required)')
    .argument('<task-id>', 'Canonical YYLO Ledger task ID')
    .requiredOption('--lease-token <token>', 'Current fencing lease token')
    .action((taskId: string, options: { leaseToken: string }) => invoke(
      'lease-heartbeat', taskId, [], ['--lease-token', options.leaseToken],
    ));
  task.command('lease-handoff')
    .description('Release authority to one explicit successor receipt (holder token required)')
    .argument('<task-id>', 'Canonical YYLO Ledger task ID')
    .requiredOption('--lease-token <token>', 'Current fencing lease token')
    .option('--reason <text>', 'Bounded handoff reason')
    .action((taskId: string, options: { leaseToken: string; reason?: string }) => invoke(
      'lease-handoff', taskId, [], [
        '--lease-token', options.leaseToken,
        ...(options.reason ? ['--reason', options.reason] : []),
      ],
    ));
  task.command('lease-successor')
    .description('Issue one successor token; retry manual gated commands with --lease-token <returned-token>')
    .argument('<task-id>', 'Canonical YYLO Ledger task ID')
    .addHelpText('after', '\nThe returned token remains valid after this helper exits until superseded or terminated.\nAt the unchanged clean base: yy task start TASK_ID --lease-token <returned-token>\nUse that token for later manual gated commands, including finish; do not repeat successor.\nFor authorized managed execution instead: yy task run TASK_ID (or resume).\nManaged execution is not read-only recovery; lifecycle blockers and budgets still apply.\nKeep tokens private; never include them in logs or task evidence.\n')
    .option('--handoff-receipt <file>', 'Exact handoff receipt consumed by this successor')
    .action((taskId: string, options: { handoffReceipt?: string }) => invoke(
      'lease-successor', taskId, [],
      options.handoffReceipt ? ['--handoff-receipt', options.handoffReceipt] : [],
    ));
  task.command('lease-revoke')
    .description('Operator-only explicit termination of task mutation authority')
    .argument('<task-id>', 'Canonical YYLO Ledger task ID')
    .requiredOption('--reason <text>', 'Operator decision record')
    .action((taskId: string, options: { reason: string }) => invoke(
      'lease-revoke', taskId, [], ['--reason', options.reason],
    ));
  task.command('lease-release')
    .description('Holder terminal lease release without queueing')
    .argument('<task-id>', 'Canonical YYLO Ledger task ID')
    .requiredOption('--lease-token <token>', 'Current fencing lease token')
    .action((taskId: string, options: { leaseToken: string }) => invoke(
      'lease-release', taskId, [], ['--lease-token', options.leaseToken],
    ));
  task.command('state-archive-plan')
    .description('Create a read-only reviewed plan for terminal lifecycle compaction')
    .requiredOption('--output <file>', 'Fresh external plan path')
    .option('--cold-ref <ref>', 'Dedicated opt-in cold Git ref')
    .action((options: { output: string; coldRef?: string }) => invoke(
      'state-archive-plan', '', [], ['--output', options.output,
        ...(options.coldRef ? ['--cold-ref', options.coldRef] : [])],
    ));
  task.command('state-archive-apply')
    .description('Apply one exact reviewed terminal lifecycle compaction plan')
    .requiredOption('--plan <file>', 'Exact reviewed plan')
    .requiredOption('--output <file>', 'Fresh external receipt path')
    .requiredOption('--authorize-state-compaction', 'Explicit destructive migration authority')
    .action((options: { plan: string; output: string }) => invoke(
      'state-archive-apply', '', [], ['--plan', options.plan, '--output', options.output,
        '--authorize-state-compaction'],
    ));
  task.command('state-archive-verify')
    .description('Verify compact hot state and every archived terminal record')
    .requiredOption('--plan <file>', 'Exact applied plan')
    .action((options: { plan: string }) => invoke(
      'state-archive-verify', '', [], ['--plan', options.plan],
    ));
  task.command('state-archive-get')
    .description('Explicitly retrieve one digest-verified cold terminal lifecycle record')
    .argument('<task-id>', 'Canonical YYLO Ledger task ID')
    .option('--cold-ref <ref>', 'Dedicated opt-in cold Git ref')
    .action((taskId: string, options: { coldRef?: string }) => invoke(
      'state-archive-get', taskId, [], options.coldRef ? ['--cold-ref', options.coldRef] : [],
    ));
  task.command('state-archive-rollback')
    .description('Restore the exact pre-compaction hot state while preserving cold evidence')
    .requiredOption('--plan <file>', 'Exact applied plan')
    .requiredOption('--output <file>', 'Fresh external receipt path')
    .requiredOption('--authorize-state-rollback', 'Explicit rollback authority')
    .action((options: { plan: string; output: string }) => invoke(
      'state-archive-rollback', '', [], ['--plan', options.plan, '--output', options.output,
        '--authorize-state-rollback'],
    ));
  task.command('runtime-bootstrap')
    .description('Plan or apply guarded package-bound target task-runtime recovery')
    .option('--dry-run', 'Persist and print a non-mutating target bootstrap plan')
    .option('--apply <receipt>', 'Apply one exact immutable bootstrap plan')
    .action((options: TaskRuntimeBootstrapOptions) => {
      if (Boolean(options.dryRun) === Boolean(options.apply)) {
        throw new Error('task runtime-bootstrap requires exactly one of --dry-run or --apply <receipt>');
      }
      return invokeBootstrap(options.dryRun ? { dryRun: true } : { apply: options.apply! });
    });
}
