import { spawn } from 'node:child_process';
import path from 'node:path';
import fs from 'fs-extra';
import { Command } from 'commander';
import { routeControlPlane } from '../../utils/control-plane-router.js';
import { checkpointControllerAfterFinalization } from '../../utils/controller-checkpoint.js';

export type MergeQueueOperation = 'status' | 'drive' | 'resume' | 'arbiter-status' | 'arbiter-run' | 'plan' | 'next' | 'resolve' | 'review' | 'reopen' | 'recover-full-suite-failure' | 'recover-repair-predispatch' | 'recover-authority-drift' | 'supersede-lifecycle-journal' | 'reconcile' | 'refresh' | 'withdraw';
export type MergeQueueInvoker = (
  operation: MergeQueueOperation,
  taskId?: string,
  extraArgs?: string[],
) => Promise<void>;
export type MergeQueueCheckpointer = typeof checkpointControllerAfterFinalization;

export function mergeQueueControlOperation(operation: MergeQueueOperation): 'kanban' | 'orchestration' {
  return ['status', 'plan', 'arbiter-status'].includes(operation) ? 'kanban' : 'orchestration';
}

export const MAX_MERGE_RESULT_LINE_CHARS = 1024 * 1024;

/** Retain only one bounded terminal stdout line while all output is streamed. */
export class TerminalMergeResultExtractor {
  private pending = '';
  private pendingOversized = false;
  private terminal: unknown;

  append(text: string): void {
    for (let start = 0; start <= text.length;) {
      const newline = text.indexOf('\n', start);
      const end = newline === -1 ? text.length : newline;
      if (!this.pendingOversized) {
        const remaining = MAX_MERGE_RESULT_LINE_CHARS - this.pending.length;
        const part = text.slice(start, Math.min(end, start + Math.max(remaining, 0)));
        this.pending += part;
        if (end - start > remaining) this.pendingOversized = true;
      }
      if (newline === -1) break;
      this.completeLine();
      start = newline + 1;
    }
  }

  finish(): unknown {
    if (this.pending.length > 0 || this.pendingOversized) this.completeLine();
    return this.terminal;
  }

  private completeLine(): void {
    if (this.pendingOversized) {
      this.terminal = undefined;
    } else if (this.pending.trim()) {
      try {
        this.terminal = JSON.parse(this.pending);
      } catch {
        this.terminal = undefined;
      }
    }
    this.pending = '';
    this.pendingOversized = false;
  }
}

export async function checkpointMergeQueueAfterFinalization(
  operation: MergeQueueOperation,
  controllerRoot: string,
  exitCode: number,
  result: unknown,
  checkpoint: MergeQueueCheckpointer = checkpointControllerAfterFinalization,
): Promise<void> {
  const payload = result && typeof result === 'object'
    ? result as Record<string, unknown>
    : undefined;
  const postIntegration = payload?.post_integration;
  const phases = postIntegration && typeof postIntegration === 'object'
    ? postIntegration as Record<string, unknown>
    : undefined;
  const kanban = phases?.kanban_finalization;
  const kanbanPhase = kanban && typeof kanban === 'object'
    ? kanban as Record<string, unknown>
    : undefined;
  // Do not checkpoint successful intermediate review/admission transitions.
  // MERGED is persisted only after the terminal Kanban mutation and readback.
  if (!['next', 'resolve', 'resume'].includes(operation) || exitCode !== 0
      || payload?.outcome !== 'MERGED' || kanbanPhase?.status !== 'complete') return;
  const checkpointTaskId = typeof payload?.task_id === 'string' ? payload.task_id : undefined;
  if (checkpointTaskId) await checkpoint(controllerRoot, exitCode, checkpointTaskId);
  else await checkpoint(controllerRoot, exitCode);
}

export async function invokeMergeQueueAtController(
  operation: MergeQueueOperation,
  controllerRoot: string,
  env: NodeJS.ProcessEnv,
  taskId?: string,
  checkpoint: MergeQueueCheckpointer = checkpointControllerAfterFinalization,
  extraArgs: string[] = [],
): Promise<void> {
  const script = path.join(controllerRoot, '.juno_task', 'scripts', 'merge_queue.py');
  if (!(await fs.pathExists(script))) {
    throw new Error('Missing managed merge queue runtime. Run `yy scripts update` and retry.');
  }
  const scriptOperation = operation === 'arbiter-status'
    ? ['arbiter', 'status']
    : operation === 'arbiter-run' ? ['arbiter', 'run'] : [operation];
  const args = [script, ...scriptOperation, ...(taskId ? [taskId] : []), ...extraArgs];
  const extractor = new TerminalMergeResultExtractor();
  const exitCode = await new Promise<number>((resolve, reject) => {
    const child = spawn('python3', args, { cwd: controllerRoot, env, stdio: ['inherit', 'pipe', 'inherit'] });
    child.stdout.on('data', (chunk: Buffer | string) => {
      const text = chunk.toString();
      extractor.append(text);
      process.stdout.write(text);
    });
    child.once('error', reject);
    child.once('close', (code, signal) => {
      if (signal) reject(new Error(`Merge queue command terminated by signal ${signal}`));
      else resolve(code ?? 1);
    });
  });
  const result = exitCode === 0 ? extractor.finish() : undefined;
  await checkpointMergeQueueAfterFinalization(operation, controllerRoot, exitCode, result, checkpoint);
  if (exitCode !== 0) process.exitCode = exitCode;
}

export async function invokeMergeQueue(
  operation: MergeQueueOperation,
  taskId?: string,
  extraArgs: string[] = [],
): Promise<void> {
  const route = routeControlPlane(
    process.cwd(),
    mergeQueueControlOperation(operation),
  );
  await invokeMergeQueueAtController(
    operation, route.controllerRoot, route.env, taskId,
    checkpointControllerAfterFinalization, extraArgs,
  );
}

export function configureMergeQueueCommand(
  program: Command,
  invoke: MergeQueueInvoker = invokeMergeQueue,
): void {
  const merge = program.command('merge').description('Observe delivery or explicitly run one fenced target owner');
  merge.command('status')
    .description('Read-only bounded queue state, producer fence, prior evidence, and one eligible action')
    .option('--detail [task-id]', 'Bounded detail for TASK_ID, or the active FIFO attempt')
    .option('--full', 'Legacy exhaustive diagnostic representation')
    .option('--json', 'Force structured JSON when stdout is interactive')
    .action((options: { detail?: string | boolean; full?: boolean; json?: boolean }) => {
      if (options.full && options.detail !== undefined) {
        throw new Error('merge status accepts only one of --detail or --full');
      }
      const args = [
        ...(options.full ? ['--full'] : []),
        ...(options.detail === true ? ['--detail']
          : typeof options.detail === 'string' ? ['--detail', options.detail] : []),
        ...(!options.json && process.stdout.isTTY && !options.full ? ['--human'] : []),
      ];
      return args.length ? invoke('status', undefined, args) : invoke('status');
    });
  merge
    .command('drive')
    .description('Explicit mutation: run the controller-owned typed workflow for a frozen FIFO scope')
    .option('--through <task-id>', 'Stop after this FIFO-authorized task')
    .action((options: { through?: string }) => options.through
      ? invoke('drive', undefined, ['--through', options.through])
      : invoke('drive'));
  merge
    .command('resume')
    .description('Resume through the existing fenced target arbiter from the earliest verified stage')
    .option('--through <task-id>', 'Stop after this FIFO-authorized task')
    .action((options: { through?: string }) => options.through
      ? invoke('resume', undefined, ['--through', options.through])
      : invoke('resume'));
  const arbiter = merge.command('arbiter')
    .description('Observe or explicitly run the one on-demand fenced owner for this protected target');
  arbiter.command('status')
    .description('Read-only arbiter ownership, eligible work, reason code, and next action')
    .action(() => invoke('arbiter-status'));
  arbiter.command('run')
    .description('Explicit mutation: start only with authority, drain deterministically, then exit')
    .option('--through <task-id>', 'Stop after this FIFO-authorized task')
    .action((options: { through?: string }) => invoke(
      'arbiter-run', undefined, [...(options.through ? ['--through', options.through] : [])],
    ));
  merge
    .command('plan')
    .description('Compute an offline, non-mutating candidate feasibility report')
    .argument('<task-id>', 'Canonical YYLO Ledger task ID')
    .option('--against <ref>', 'Plan against an exact alternate Git ref')
    .option('--json', 'Emit the stable versioned JSON projection')
    .action((taskId: string, options: { against?: string; json?: boolean }) => {
      const args = [
        ...(options.against ? ['--against', options.against] : []),
        ...(options.json ? ['--json'] : []),
      ];
      return invoke('plan', taskId, args);
    });
  merge
    .command('next')
    .description('Explicit recovery mutation: advance once or continue paused evidence for TASK_ID')
    .argument('[task-id]', 'Paused task whose evidence/review processing should continue')
    .option('--plan-id <sha256>', 'Require this exact current feasibility identity')
    .action((taskId: string | undefined, options: { planId?: string; trainPlan?: string }) => {
      const args = [...(options.planId ? ['--plan-id', options.planId] : []),
        ...(options.trainPlan ? ['--train-plan', options.trainPlan] : [])];
      return args.length ? invoke('next', taskId, args)
        : taskId === undefined ? invoke('next') : invoke('next', taskId);
    });
  merge.command('resolve').description('Explicit recovery mutation for one preserved conflict').argument('<task-id>', 'Canonical YYLO Ledger task ID')
    .option('--plan-id <sha256>', 'Require this exact current feasibility identity')
    .action((taskId: string, options: { planId?: string; trainPlan?: string }) => {
      const args = [...(options.planId ? ['--plan-id', options.planId] : []),
        ...(options.trainPlan ? ['--train-plan', options.trainPlan] : [])];
      return args.length ? invoke('resolve', taskId, args) : invoke('resolve', taskId);
    });
  merge.command('review').argument('<task-id>', 'Canonical YYLO Ledger task ID').action((taskId: string) => invoke('review', taskId));
  merge.command('reopen').argument('<task-id>', 'Task with review findings and a new committed tip')
    .option('--plan-id <sha256>', 'Require this exact current feasibility identity')
    .action((taskId: string, options: { planId?: string }) => options.planId
      ? invoke('reopen', taskId, ['--plan-id', options.planId])
      : invoke('reopen', taskId));
  merge.command('recover-full-suite-failure')
    .description('Explicit receipt-bound authorization for one deterministic failed-suite repair')
    .argument('<task-id>', 'AWAITING_RISK task bound by unchanged deterministic failure evidence')
    .requiredOption('--attempt <number>', 'Exact terminal target-arbiter attempt')
    .requiredOption('--terminal-receipt <path>', 'Canonical terminal failed-arbiter receipt')
    .requiredOption('--terminal-receipt-sha256 <sha256>', 'Exact terminal receipt byte identity')
    .requiredOption('--expected-revision <sha256>', 'Exact current lifecycle record revision')
    .requiredOption('--run-id <id>', 'Exact managed merge-drive run identity')
    .requiredOption('--scope-sha256 <sha256>', 'Exact frozen FIFO scope identity')
    .requiredOption('--journal-sha256 <sha256>', 'Exact nonterminal lifecycle journal bytes')
    .action((taskId: string, options: {
      attempt: string; terminalReceipt: string; terminalReceiptSha256: string;
      expectedRevision: string; runId: string; scopeSha256: string; journalSha256: string;
    }) => invoke('recover-full-suite-failure', taskId, [
      '--attempt', options.attempt,
      '--terminal-receipt', options.terminalReceipt,
      '--terminal-receipt-sha256', options.terminalReceiptSha256,
      '--expected-revision', options.expectedRevision,
      '--run-id', options.runId,
      '--scope-sha256', options.scopeSha256,
      '--journal-sha256', options.journalSha256,
    ]));
  merge.command('recover-repair-predispatch')
    .description('Receipt-bound zero-cost recovery for the exact existing semantic repair worker')
    .argument('<task-id>', 'REVIEW_FINDINGS task with one refused semantic-repair worker')
    .requiredOption('--attempt <number>', 'Exact terminal target-arbiter attempt')
    .requiredOption('--terminal-receipt <path>', 'Canonical terminal failed-arbiter receipt')
    .requiredOption('--terminal-receipt-sha256 <sha256>', 'Exact terminal receipt bytes')
    .requiredOption('--expected-revision <sha256>', 'Exact lifecycle record revision')
    .requiredOption('--run-id <id>', 'Exact managed merge-drive run identity')
    .requiredOption('--scope-sha256 <sha256>', 'Exact frozen FIFO scope identity')
    .requiredOption('--journal-sha256 <sha256>', 'Exact nonterminal journal bytes')
    .requiredOption('--worker-id <id>', 'Exact existing semantic-repair worker ID')
    .requiredOption('--predispatch-receipt <path>', 'Canonical no-provider receipt')
    .requiredOption('--predispatch-receipt-sha256 <sha256>', 'Exact no-provider receipt bytes')
    .action((taskId: string, options: {
      attempt: string; terminalReceipt: string; terminalReceiptSha256: string;
      expectedRevision: string; runId: string; scopeSha256: string; journalSha256: string;
      workerId: string; predispatchReceipt: string; predispatchReceiptSha256: string;
    }) => invoke('recover-repair-predispatch', taskId, [
      '--attempt', options.attempt,
      '--terminal-receipt', options.terminalReceipt,
      '--terminal-receipt-sha256', options.terminalReceiptSha256,
      '--expected-revision', options.expectedRevision,
      '--run-id', options.runId,
      '--scope-sha256', options.scopeSha256,
      '--journal-sha256', options.journalSha256,
      '--worker-id', options.workerId,
      '--predispatch-receipt', options.predispatchReceipt,
      '--predispatch-receipt-sha256', options.predispatchReceiptSha256,
    ]));
  merge.command('recover-authority-drift')
    .description('Explicit receipt-bound recovery from terminal pre-CAS authority drift to fenced editable WORKING')
    .argument('<task-id>', 'MERGING task bound by the failed arbiter receipt')
    .requiredOption('--attempt <number>', 'Exact terminal target-arbiter attempt')
    .requiredOption('--terminal-receipt <path>', 'Canonical terminal failed-arbiter receipt')
    .requiredOption('--terminal-receipt-sha256 <sha256>', 'Exact terminal receipt byte identity')
    .requiredOption('--expected-revision <sha256>', 'Exact current lifecycle record revision')
    .action((taskId: string, options: {
      attempt: string; terminalReceipt: string; terminalReceiptSha256: string; expectedRevision: string;
    }) => invoke('recover-authority-drift', taskId, [
      '--attempt', options.attempt,
      '--terminal-receipt', options.terminalReceipt,
      '--terminal-receipt-sha256', options.terminalReceiptSha256,
      '--expected-revision', options.expectedRevision,
    ]));
  merge.command('supersede-lifecycle-journal')
    .description('Terminalize one receipt-recovered pre-CAS stale merge lifecycle journal')
    .requiredOption('--run-id <id>', 'Exact managed merge-drive run identity')
    .requiredOption('--expected-journal-revision <number>', 'Exact nonterminal journal revision')
    .requiredOption('--expected-journal-sha256 <sha256>', 'Exact nonterminal journal bytes')
    .requiredOption('--scope-sha256 <sha256>', 'Exact frozen FIFO scope identity')
    .requiredOption('--arbiter-attempt <number>', 'Exact terminal failed arbiter attempt')
    .requiredOption('--terminal-receipt <path>', 'Canonical terminal failed-arbiter receipt')
    .requiredOption('--terminal-receipt-sha256 <sha256>', 'Exact failed-arbiter receipt bytes')
    .requiredOption('--recovered-task <task-id>', 'Receipt-recovered and requeued frozen task')
    .requiredOption('--recovery-receipt <path>', 'Canonical pre-CAS task recovery receipt')
    .requiredOption('--recovery-receipt-sha256 <sha256>', 'Exact task recovery receipt bytes')
    .requiredOption('--expected-target-sha <sha>', 'Exact unchanged protected target')
    .requiredOption('--expected-current-fifo-sha256 <sha256>', 'Exact current actionable FIFO identity')
    .action((options: {
      runId: string; expectedJournalRevision: string; expectedJournalSha256: string;
      scopeSha256: string; arbiterAttempt: string; terminalReceipt: string;
      terminalReceiptSha256: string; recoveredTask: string; recoveryReceipt: string;
      recoveryReceiptSha256: string; expectedTargetSha: string;
      expectedCurrentFifoSha256: string;
    }) => invoke('supersede-lifecycle-journal', undefined, [
      '--run-id', options.runId,
      '--expected-journal-revision', options.expectedJournalRevision,
      '--expected-journal-sha256', options.expectedJournalSha256,
      '--scope-sha256', options.scopeSha256,
      '--arbiter-attempt', options.arbiterAttempt,
      '--terminal-receipt', options.terminalReceipt,
      '--terminal-receipt-sha256', options.terminalReceiptSha256,
      '--recovered-task', options.recoveredTask,
      '--recovery-receipt', options.recoveryReceipt,
      '--recovery-receipt-sha256', options.recoveryReceiptSha256,
      '--expected-target-sha', options.expectedTargetSha,
      '--expected-current-fifo-sha256', options.expectedCurrentFifoSha256,
    ]));
  merge
    .command('withdraw')
    .description('Withdraw one queued task after proving no live producer owns its claims')
    .argument('<task-id>', 'Canonical YYLO Ledger task ID')
    .option('--reason <text>', 'Bounded operator reason recorded in the withdraw receipt')
    .action((taskId: string, options: { reason?: string }) => options.reason
      ? invoke('withdraw', taskId, ['--reason', options.reason])
      : invoke('withdraw', taskId));
  const reconcile = merge.command('reconcile')
    .description('Reconcile terminal findings whose exact tip is already in the protected target');
  reconcile.command('plan').argument('<task-id>', 'Terminal findings task to reconcile')
    .action((taskId: string) => invoke('reconcile', undefined, ['plan', taskId]));
  reconcile.command('apply').argument('<task-id>', 'Task bound by the reconciliation receipt')
    .requiredOption('--receipt <path>', 'Canonical immutable reconciliation receipt')
    .requiredOption('--receipt-sha256 <sha256>', 'Exact receipt byte identity')
    .action((taskId: string, options: { receipt: string; receiptSha256: string }) =>
      invoke('reconcile', undefined, ['apply', taskId, '--receipt', options.receipt,
        '--receipt-sha256', options.receiptSha256]));
  const refresh = merge.command('refresh')
    .description('Safely admit exact protected-target bytes into a queued candidate');
  refresh.command('plan').argument('<task-id>', 'Queued or reopen candidate')
    .action((taskId: string) => invoke('refresh', undefined, ['plan', taskId]));
  refresh.command('apply').argument('<task-id>', 'Candidate bound by the refresh receipt')
    .requiredOption('--receipt <path>', 'Canonical immutable refresh receipt')
    .requiredOption('--receipt-sha256 <sha256>', 'Exact receipt byte identity')
    .action((taskId: string, options: { receipt: string; receiptSha256: string }) =>
      invoke('refresh', undefined, ['apply', taskId, '--receipt', options.receipt,
        '--receipt-sha256', options.receiptSha256]));
}
