import { spawn } from 'node:child_process';
import path from 'node:path';
import fs from 'fs-extra';
import { Command } from 'commander';
import { routeControlPlane } from '../../utils/control-plane-router.js';

export type ReleaseTrainOperation = 'plan' | 'status' | 'inspect' | 'select' | 'seal' | 'epoch-status' | 'reconcile-members' | 'replay-finalization-successor' | 'drive' | 'eject' | 'repair' | 'replay-repair' | 'retry' | 'shadow' | 'bootstrap-inspect' | 'bootstrap-seal' | 'bootstrap-status' | 'bootstrap-drive';
export type ReleaseTrainInvoker = (
  operation: ReleaseTrainOperation, declaration: string, extraArgs?: string[],
) => Promise<void>;

export async function invokeReleaseTrain(
  operation: ReleaseTrainOperation, declaration: string, extraArgs: string[] = [],
): Promise<void> {
  const route = routeControlPlane(process.cwd(), 'kanban');
  const script = path.join(route.controllerRoot, '.juno_task', 'scripts', 'release_train.py');
  if (!(await fs.pathExists(script))) {
    throw new Error('Missing managed release-train runtime. Run `yy scripts update --force` and retry.');
  }
  const code = await new Promise<number>((resolve, reject) => {
    const child = spawn('python3', [script, operation, declaration, ...extraArgs], {
      cwd: route.controllerRoot, env: route.env, stdio: 'inherit',
    });
    child.once('error', reject);
    child.once('close', (exitCode, signal) => signal
      ? reject(new Error(`Release-train command terminated by signal ${signal}`))
      : resolve(exitCode ?? 1));
  });
  if (code !== 0) process.exitCode = code;
}

export function configureReleaseTrainCommand(
  program: Command, invoke: ReleaseTrainInvoker = invokeReleaseTrain,
): void {
  const train = program.command('release').description('Observe or explicitly mutate sealed release epochs; no command implies publish/deploy authority')
    .command('train').description('Observe a train or explicitly seal/drive it with separate authority');
  for (const operation of ['plan', 'status', 'inspect'] as const) {
    train.command(operation)
      .description('Read-only release-epoch observation; never seals or mutates the target')
      .argument('<declaration>', 'Versioned release-train declaration JSON')
      .option('--json', 'Emit stable versioned JSON')
      .option('--output <path>', 'Write the exact JSON projection')
      .action((declaration: string, options: { json?: boolean; output?: string }) => invoke(
        operation, declaration,
        [...(options.json ? ['--json'] : []), ...(options.output ? ['--output', options.output] : [])],
      ));
  }
  train.command('select')
    .description('Explicitly route the exact wave to epoch delivery; does not seal, test, review, or mutate the target')
    .argument('<declaration>', 'Versioned release-train declaration JSON')
    .option('--json', 'Emit stable versioned JSON')
    .action((declaration: string, options: { json?: boolean }) => invoke(
      'select', declaration, options.json ? ['--json'] : [],
    ));
  train.command('seal')
    .description('Explicitly close admission and create one immutable fenced epoch')
    .argument('<declaration>', 'Versioned release-train declaration JSON')
    .option('--json', 'Emit stable versioned JSON')
    .action((declaration: string, options: { json?: boolean }) => invoke(
      'seal', declaration, options.json ? ['--json'] : [],
    ));
  train.command('epoch-status')
    .description('Read-only sealed-epoch state, reason code, and next action')
    .argument('<epoch-id>', 'Sealed epoch identity')
    .option('--json', 'Emit stable versioned JSON')
    .action((epochId: string, options: { json?: boolean }) => invoke(
      'epoch-status', epochId, options.json ? ['--json'] : [],
    ));
  train.command('reconcile-members')
    .description('Receipt-bound terminal Ledger recovery for every exact integrated epoch member')
    .argument('<epoch-id>', 'Receipt-proven integrated epoch identity')
    .requiredOption('--expected-target <sha>', 'Exact current protected-target readback')
    .option('--json', 'Emit stable versioned JSON')
    .action((epochId: string, options: { expectedTarget: string; json?: boolean }) => invoke(
      'reconcile-members', epochId,
      ['--expected-target', options.expectedTarget, ...(options.json ? ['--json'] : [])],
    ));
  train.command('replay-finalization-successor')
    .description('Typed descendant-target replay of one untouched incomplete terminal-finalization journal')
    .argument('<epoch-id>', 'Receipt-proven integrated epoch identity')
    .requiredOption('--predecessor-target <sha>', 'Exact target bound by the untouched predecessor journal')
    .requiredOption('--expected-target <sha>', 'Exact current protected-target readback')
    .option('--json', 'Emit stable versioned JSON')
    .action((epochId: string, options: {
      predecessorTarget: string; expectedTarget: string; json?: boolean;
    }) => invoke('replay-finalization-successor', epochId, [
      '--predecessor-target', options.predecessorTarget,
      '--expected-target', options.expectedTarget, ...(options.json ? ['--json'] : []),
    ]));
  train.command('drive')
    .description('Fenced epoch mutation through composition, aggregate gates, terminal member projection, and one target CAS')
    .argument('<epoch-id>', 'Sealed epoch identity')
    .requiredOption('--epoch-token <token>', 'Exact fencing token emitted once by seal')
    .option('--json', 'Emit stable versioned JSON')
    .action((epochId: string, options: { epochToken: string; json?: boolean }) => invoke(
      'drive', epochId, ['--epoch-token', options.epochToken, ...(options.json ? ['--json'] : [])],
    ));
  train.command('eject')
    .description('Explicitly record an optional failure and eject its dependent subtree')
    .argument('<epoch-id>', 'Sealed epoch identity')
    .argument('<task-id>', 'Optional failed member')
    .requiredOption('--reason <reason>', 'Receipt-bound failure reason')
    .requiredOption('--epoch-token <token>', 'Exact epoch fencing token')
    .action((epochId: string, taskId: string, options: { reason: string; epochToken: string }) => invoke(
      'eject', epochId, [taskId, '--reason', options.reason, '--epoch-token', options.epochToken],
    ));
  train.command('repair')
    .description('Consume one bounded managed conflict-repair receipt')
    .argument('<epoch-id>', 'Recovering epoch identity')
    .requiredOption('--receipt <path>', 'Successful canonical managed-worker receipt')
    .requiredOption('--epoch-token <token>', 'Exact epoch fencing token')
    .action((epochId: string, options: { receipt: string; epochToken: string }) => invoke(
      'repair', epochId, ['--receipt', options.receipt, '--epoch-token', options.epochToken],
    ));
  train.command('replay-repair')
    .description('Replay one exact-closure historical repair without another model invocation')
    .argument('<epoch-id>', 'Recovering successor epoch identity')
    .requiredOption('--predecessor-epoch <epoch>', 'Historical validated epoch identity')
    .requiredOption('--receipt <path>', 'Consumed no-model recovered-worker receipt')
    .requiredOption('--epoch-token <token>', 'Exact successor epoch fencing token')
    .option('--json', 'Emit stable versioned JSON')
    .action((epochId: string, options: {
      predecessorEpoch: string; receipt: string; epochToken: string; json?: boolean;
    }) => invoke('replay-repair', epochId, [
      '--predecessor-epoch', options.predecessorEpoch, '--receipt', options.receipt,
      '--epoch-token', options.epochToken, ...(options.json ? ['--json'] : []),
    ]));
  train.command('retry')
    .description('Receipt-backed fenced retry of one failed aggregate gate on the exact train tip')
    .argument('<epoch-id>', 'Recovering epoch identity')
    .requiredOption('--epoch-token <token>', 'Exact epoch fencing token')
    .option('--json', 'Emit stable versioned JSON')
    .action((epochId: string, options: { epochToken: string; json?: boolean }) => invoke(
      'retry', epochId, ['--epoch-token', options.epochToken, ...(options.json ? ['--json'] : [])],
    ));
  train.command('bootstrap-inspect')
    .description('Read-only validation of one causally bound bootstrap-repair declaration')
    .argument('<declaration>', 'Versioned bootstrap-repair declaration JSON')
    .option('--json', 'Emit stable versioned JSON')
    .action((declaration: string, options: { json?: boolean }) => invoke(
      'bootstrap-inspect', declaration, options.json ? ['--json'] : [],
    ));
  train.command('bootstrap-seal')
    .description('Explicitly seal one immutable bootstrap-repair transaction')
    .argument('<declaration>', 'Versioned bootstrap-repair declaration JSON')
    .option('--json', 'Emit stable versioned JSON')
    .action((declaration: string, options: { json?: boolean }) => invoke(
      'bootstrap-seal', declaration, options.json ? ['--json'] : [],
    ));
  train.command('bootstrap-status')
    .description('Read-only bootstrap-repair state and receipt projection')
    .argument('<operation-id>', 'Sealed bootstrap-repair operation identity')
    .option('--json', 'Emit stable versioned JSON')
    .action((operationId: string, options: { json?: boolean }) => invoke(
      'bootstrap-status', operationId, options.json ? ['--json'] : [],
    ));
  train.command('bootstrap-drive')
    .description('Fenced bootstrap-only composition and one expected-old-SHA target CAS')
    .argument('<operation-id>', 'Sealed bootstrap-repair operation identity')
    .requiredOption('--bootstrap-token <token>', 'Exact fencing token emitted once by bootstrap-seal')
    .option('--json', 'Emit stable versioned JSON')
    .action((operationId: string, options: { bootstrapToken: string; json?: boolean }) => invoke(
      'bootstrap-drive', operationId,
      ['--bootstrap-token', options.bootstrapToken, ...(options.json ? ['--json'] : [])],
    ));
  train.command('shadow')
    .description('Read-only production-shaped replay; never merges, releases, or mutates production')
    .argument('<source>', 'Versioned release-train declaration or immutable sealed-epoch state JSON')
    .option('--baseline <path>', 'Required frozen telemetry baseline or aggregate scorecard JSON')
    .option('--json', 'Emit stable versioned JSON')
    .option('--output <path>', 'Write the canary decision receipt')
    .action((declaration: string, options: { baseline?: string; json?: boolean; output?: string }) => invoke(
      'shadow', declaration, [...(options.baseline ? ['--baseline', options.baseline] : []),
        ...(options.json ? ['--json'] : []), ...(options.output ? ['--output', options.output] : [])],
    ));
}
