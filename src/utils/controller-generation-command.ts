import { spawn } from 'node:child_process';
import fs from 'fs-extra';
import path from 'node:path';
import { constants } from 'node:os';
import { resolveController } from './controller-resolver.js';
import { ScriptInstaller } from './script-installer.js';
import { packagedGenerationRoot, recoverControllerGeneration, acquireControllerGenerationReadLease, GENERATION_MIGRATION_ROOT } from './controller-generation-migration.js';
import { assessControllerGeneration, ensureControllerGeneration } from './controller-generation-startup.js';

export const YYLO_CONTROLLER_GENERATION_DISPATCH_V1 = true;
let releaseDispatch: (() => Promise<void>) | undefined;
export async function releaseControllerCommand(): Promise<void> {
  const release = releaseDispatch;
  releaseDispatch = undefined;
  if (release) await release();
}

export function generationCommandKind(args: string[]): 'read' | 'execute' | 'maintenance' | 'skip' {
  const [command = '', operation = ''] = args;
  if (command === 'scripts' && operation === 'generation') return 'maintenance';
  if (['info', 'where', 'capabilities', 'doctor'].includes(command)) return 'read';
  if ((command === 'scripts' && operation === 'doctor')
      || (command === 'integration' && ['status', 'runtime-doctor'].includes(operation))
      || (command === 'task' && ['status', 'admission', 'preflight', 'doctor', 'lease-status', 'evidence-status'].includes(operation))) return 'read';
  if (['task', 'merge'].includes(command) && operation && !['local', 'runtime-bootstrap'].includes(operation)) return 'execute';
  if (['pi', 'cc', 'start', 'continue', 'loop'].includes(command)) return 'execute';
  return 'skip';
}

/** Runs before any installer or local runtime selection. Discovery never migrates. */
export async function prepareControllerCommand(cwd: string, commandArgs: string[], rawArgs: string[]): Promise<boolean> {
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
    if (operation === 'doctor') {
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
    } else if (operation === 'resume' || operation === 'rollback') {
      const id = commandArgs[3] ?? '';
      if (!/^[a-f0-9]{64}$/.test(id ?? '')) throw new Error('Exact generation transaction ID required');
      console.log(JSON.stringify(await recoverControllerGeneration(controller, id, operation === 'rollback')));
    } else throw new Error('Use yy scripts generation doctor|resume|rollback');
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
  const assessment = await ensureControllerGeneration(controller, packageRoot);
  releaseDispatch = await acquireControllerGenerationReadLease(controller);
  const readback = await assessControllerGeneration(controller, packageRoot);
  if (readback.disposition !== assessment.disposition
      || (assessment.disposition === 'retained' && readback.disposition === 'retained'
          && assessment.executable !== readback.executable)) {
    await releaseControllerCommand();
    throw new Error('generation_changed_before_dispatch: retry the unchanged command');
  }
  if (assessment.disposition !== 'retained') return false;
  if (process.env.YYLO_GENERATION_REDISPATCH) {
    await releaseControllerCommand();
    throw new Error('generation_dispatch_cycle: YYLO_CONTROLLER_GENERATION_DISPATCH_V1 permits only one retained-runtime hop');
  }
  console.error(`Using retained controller runtime: ${assessment.reason}`);
  const exit = await new Promise<number>((resolve, reject) => {
    const child = spawn(process.execPath, [assessment.executable, ...rawArgs], {
      cwd, stdio: 'inherit', env: { ...process.env, YYLO_GENERATION_REDISPATCH: assessment.executable },
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
