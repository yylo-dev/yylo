import { spawn } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import fs from 'fs-extra';
import { Command } from 'commander';
import {
  invokeTaskWorkspace,
  type TaskWorkspaceInvoker,
} from './task.js';

export type MigrationInvocation = (args: string[]) => Promise<void>;

function packagedEngine(name = 'migration_inventory.py'): string {
  const directory = path.dirname(fileURLToPath(import.meta.url));
  const candidates = [
    // Bundled CLI: dist/bin/cli.mjs -> dist/templates/scripts.
    path.resolve(directory, `../templates/scripts/${name}`),
    // Source execution: src/cli/commands -> src/templates/scripts.
    path.resolve(directory, `../../templates/scripts/${name}`),
    // Bundled CLI executed from a source checkout before packaging.
    path.resolve(directory, `../../src/templates/scripts/${name}`),
  ];
  const engine = candidates.find((candidate) => fs.existsSync(candidate));
  if (!engine) throw new Error(`The packaged migration engine is missing: ${name}`);
  return engine;
}

export async function invokeMigration(args: string[]): Promise<void> {
  const evacuation = args[0]?.startsWith('evacuation-');
  const registration = args[0] === 'registration';
  const runtimeRebind = args[0] === 'runtime-rebind' || args[0] === 'runtime-install-rebind';
  const targetRuntimeProvenance = args[0]?.startsWith('target-runtime-provenance-');
  const metadataController = runtimeRebind || args[0]?.startsWith('agent-surface-repair-')
    || args[0]?.startsWith('metadata-policy-') || args[0]?.startsWith('agent-config-');
  let engineName = 'migration_inventory.py';
  if (evacuation) engineName = 'metadata_evacuation.py';
  if (metadataController) engineName = 'metadata_controller.py';
  if (targetRuntimeProvenance) engineName = 'target_runtime_provenance.py';
  if (registration) engineName = 'controller_registration.py';
  const engine = packagedEngine(engineName);
  const engineArgs = registration ? args.slice(1) : args;
  const exitCode = await new Promise<number>((resolve, reject) => {
    const child = spawn('python3', [engine, ...engineArgs], {
      cwd: process.cwd(),
      env: { ...process.env, GIT_OPTIONAL_LOCKS: '0' },
      stdio: 'inherit',
    });
    child.once('error', reject);
    child.once('exit', (code, signal) => {
      if (signal) reject(new Error(`Migration inventory terminated by signal ${signal}`));
      else resolve(code ?? 1);
    });
  });
  if (exitCode !== 0) process.exitCode = exitCode;
}

export function configureMigrationCommand(
  program: Command,
  invoke: MigrationInvocation = invokeMigration,
  invokeLegacyLifecycle: TaskWorkspaceInvoker = invokeTaskWorkspace,
): void {
  const migrate = program
    .command('migrate')
    .description('Inventory and plan a reviewed Juno architecture migration');
  const controllerConfig = migrate.command('controller-config')
    .description('Reviewed ownership migration for legacy metadata-controller configuration');
  controllerConfig.command('plan')
    .description('Classify legacy fields and freeze a clean controller/config/runtime preimage; no mutation')
    .requiredOption('--root <path>', 'Exact registered metadata-controller root')
    .requiredOption('--output <file>', 'New plan path outside all Git worktrees')
    .action((options: { root: string; output: string }) => invoke([
      'agent-config-plan', '--root', options.root, '--output', options.output,
    ]));
  controllerConfig.command('apply')
    .description('Apply only the reviewed hash-bound plan; preserve original config in the Git parent')
    .requiredOption('--plan <file>', 'Exact reviewed plan')
    .requiredOption('--output <file>', 'New external receipt path')
    .requiredOption('--authorize-config-repair', 'Authorize the exact config-only commit')
    .action((options: { plan: string; output: string }) => invoke([
      'agent-config-apply', '--plan', options.plan, '--output', options.output,
      '--authorize-config-repair',
    ]));
  const legacyLifecycle = migrate
    .command('legacy-lifecycle')
    .description('Finite inventory, conversion, or drain of an existing legacy umbrella attempt');
  legacyLifecycle
    .command('inventory')
    .description('Read-only reconciliation of preserved task states before conversion')
    .argument('[task-id]', 'Optional exact legacy task ID filter')
    .action((taskId?: string) => invokeLegacyLifecycle('doctor', taskId ?? '', []));
  legacyLifecycle
    .command('plan')
    .description('Read-only exact conversion plan for one already-WORKING legacy umbrella')
    .argument('<task-id>', 'Existing canonical legacy umbrella task ID')
    .requiredOption('--umbrella-admission <file>', 'Frozen ordered-child exact-scope input')
    .requiredOption('--output <file>', 'New exclusive recovery plan path')
    .action((taskId: string, options: { umbrellaAdmission: string; output: string }) =>
      invokeLegacyLifecycle('recovery-plan', taskId, [], [
        '--umbrella-admission', options.umbrellaAdmission, '--output', options.output,
      ]));
  legacyLifecycle
    .command('authorize')
    .description('Issue one controller receipt for the exact reviewed conversion plan')
    .argument('<task-id>', 'Existing canonical legacy umbrella task ID')
    .requiredOption('--umbrella-admission <file>', 'Frozen ordered-child exact-scope input')
    .requiredOption('--plan <file>', 'Exact reviewed recovery plan')
    .action((taskId: string, options: { umbrellaAdmission: string; plan: string }) =>
      invokeLegacyLifecycle('recovery-authorize', taskId, [], [
        '--umbrella-admission', options.umbrellaAdmission, '--plan', options.plan,
      ]));
  legacyLifecycle
    .command('apply')
    .description('Apply one authorized conversion; live use requires separate owner authority')
    .argument('<task-id>', 'Existing canonical legacy umbrella task ID')
    .requiredOption('--umbrella-admission <file>', 'Frozen ordered-child exact-scope input')
    .requiredOption('--plan <file>', 'Exact reviewed recovery plan')
    .requiredOption('--authorization-receipt <file>', 'Canonical immutable authorization for the exact plan')
    .action((taskId: string, options: {
      umbrellaAdmission: string; plan: string; authorizationReceipt: string;
    }) => invokeLegacyLifecycle('recovery-apply', taskId, [], [
      '--umbrella-admission', options.umbrellaAdmission, '--plan', options.plan,
      '--authorization-receipt', options.authorizationReceipt,
    ]));
  legacyLifecycle
    .command('verify')
    .description('Read-only verification of the exact applied conversion and preserved evidence')
    .argument('<task-id>', 'Existing canonical legacy umbrella task ID')
    .requiredOption('--umbrella-admission <file>', 'Frozen ordered-child exact-scope input')
    .requiredOption('--plan <file>', 'Exact reviewed recovery plan')
    .requiredOption('--authorization-receipt <file>', 'Canonical immutable authorization for the exact plan')
    .action((taskId: string, options: {
      umbrellaAdmission: string; plan: string; authorizationReceipt: string;
    }) => invokeLegacyLifecycle('recovery-verify', taskId, [], [
      '--umbrella-admission', options.umbrellaAdmission, '--plan', options.plan,
      '--authorization-receipt', options.authorizationReceipt,
    ]));
  legacyLifecycle
    .command('checkpoint')
    .description('Drain one existing unconverted legacy attempt; never starts child authority')
    .argument('<task-id>', 'Existing canonical legacy umbrella task ID')
    .argument('<child-id>', 'Existing admitted ordered reporting child ID')
    .option('--lease-token <token>', 'Current fencing lease token for this gated mutation')
    .action((taskId: string, childId: string, options: { leaseToken?: string }) =>
      invokeLegacyLifecycle('child-checkpoint', taskId, [], [
        '--child', childId,
        ...(options.leaseToken ? ['--lease-token', options.leaseToken] : []),
      ]));
  migrate
    .command('inventory')
    .description('Freeze Git plus exact config/plan/prompt identities and redacted environment sources')
    .option('--project <path>', 'Project worktree to inspect', process.cwd())
    .option('--controller <path>', 'Explicit controller candidate')
    .option('--product-ref <ref>', 'Explicit full product target ref')
    .option('--runtime <path>', 'Exact Juno runtime executable')
    .option('--kanban-runtime <path>', 'Exact Juno Kanban executable')
    .option('--heavy-threshold-bytes <bytes>', 'Heavy file threshold', String(10 * 1024 * 1024))
    .requiredOption('--output <path>', 'New receipt path outside the inspected project')
    .action((options) => {
      const args = ['inventory', '--project', options.project, '--heavy-threshold-bytes', options.heavyThresholdBytes, '--output', options.output];
      if (options.controller) args.push('--controller', options.controller);
      if (options.productRef) args.push('--product-ref', options.productRef);
      if (options.runtime) args.push('--runtime', options.runtime);
      if (options.kanbanRuntime) args.push('--kanban-runtime', options.kanbanRuntime);
      return invoke(args);
    });
  const targetRuntimeProvenance = migrate
    .command('target-runtime-provenance')
    .description('Plan or apply exact installed-package provenance for a legacy consumer target runtime');
  targetRuntimeProvenance
    .command('plan')
    .description('Create a non-mutating receipt binding the consumer, controller, target, package, and runtime bytes')
    .requiredOption('--controller <path>', 'Exact registered metadata-controller worktree')
    .requiredOption('--output <path>', 'New plan receipt outside every worktree and Git administration directory')
    .action((options) => invoke([
      'target-runtime-provenance-plan', '--controller', options.controller,
      '--output', options.output,
    ]));
  targetRuntimeProvenance
    .command('apply')
    .description('Commit only reviewed missing/legacy target runtime provenance under locks and a ref lease')
    .requiredOption('--plan <path>', 'Reviewed immutable provenance plan')
    .requiredOption('--output <path>', 'New immutable apply receipt outside every worktree')
    .requiredOption('--authorize-target-runtime-provenance', 'Authorize only the receipt-bound target provenance commit')
    .action((options) => invoke([
      'target-runtime-provenance-apply', '--plan', options.plan, '--output', options.output,
      '--authorize-target-runtime-provenance',
    ]));
  migrate
    .command('runtime-rebind')
    .description('Explicitly rebind a clean metadata controller to one installed runtime executable')
    .requiredOption('--root <path>', 'Exact metadata-controller worktree')
    .requiredOption('--branch <ref>', 'Exact controller branch ref')
    .requiredOption('--runtime <path>', 'Installed cli.mjs executable outside every Git ancestor (does not install a package)')
    .requiredOption('--runtime-version <version>', 'Version printed by the runtime executable')
    .requiredOption('--output <path>', 'New local receipt outside the controller worktree')
    .action((options) => invoke([
      'runtime-rebind', '--root', options.root, '--branch', options.branch,
      '--runtime', options.runtime, '--runtime-version', options.runtimeVersion,
      '--output', options.output,
    ]));
  migrate
    .command('runtime-install-rebind')
    .description('Install one exact release into a fresh non-Git prefix and rebind a clean metadata controller')
    .requiredOption('--root <path>', 'Exact metadata-controller worktree')
    .requiredOption('--branch <ref>', 'Exact controller branch ref')
    .requiredOption('--runtime-version <version>', 'Exact released yylo version to install')
    .requiredOption('--install-prefix <path>', 'Fresh absent prefix outside every Git worktree/ancestor')
    .option('--artifact <path>', 'Exact local npm pack .tgz outside every Git worktree (bypasses registry lookup)')
    .requiredOption('--output <path>', 'New local receipt outside the controller worktree')
    .action((options) => {
      const args = [
        'runtime-install-rebind', '--root', options.root, '--branch', options.branch,
        '--runtime-version', options.runtimeVersion, '--install-prefix', options.installPrefix,
        '--output', options.output,
      ];
      if (options.artifact) args.push('--artifact', options.artifact);
      return invoke(args);
    });
  const metadataPolicy = migrate
    .command('metadata-policy')
    .description('Plan or apply the narrow legacy integration-workspace policy classification');
  metadataPolicy
    .command('plan')
    .description('Create a mutation-free, hash-bound legacy metadata-policy migration plan')
    .requiredOption('--root <path>', 'Exact registered metadata-controller worktree')
    .requiredOption('--output <path>', 'New plan outside every worktree and Git administration directory')
    .action((options) => invoke([
      'metadata-policy-plan', '--root', options.root, '--output', options.output,
    ]));
  metadataPolicy
    .command('apply')
    .description('Create one bounded controller commit from an exact reviewed migration plan')
    .requiredOption('--plan <path>', 'Reviewed immutable migration plan')
    .requiredOption('--output <path>', 'New immutable apply receipt outside every worktree')
    .requiredOption('--authorize-metadata-policy-migration', 'Authorize only the receipt-bound structural migration')
    .action((options) => invoke([
      'metadata-policy-apply', '--plan', options.plan, '--output', options.output,
      '--authorize-metadata-policy-migration',
    ]));
  migrate
    .command('agent-surface-repair-plan')
    .description('Plan evacuation of committed metadata-controller instructions and skills')
    .requiredOption('--root <path>', 'Exact metadata-controller worktree')
    .requiredOption('--branch <ref>', 'Exact controller branch ref')
    .requiredOption('--expected-head <sha>', 'Frozen controller head')
    .requiredOption('--product-ref <ref>', 'Protected product ref')
    .requiredOption('--expected-product-head <sha>', 'Frozen product head')
    .requiredOption('--disposition <value>', 'Reviewed retire or externalize disposition')
    .requiredOption('--output <path>', 'New plan receipt outside all worktrees')
    .action((options) => invoke([
      'agent-surface-repair-plan', '--root', options.root, '--branch', options.branch,
      '--expected-head', options.expectedHead, '--product-ref', options.productRef,
      '--expected-product-head', options.expectedProductHead, '--disposition', options.disposition,
      '--output', options.output,
    ]));
  migrate
    .command('agent-surface-repair-apply')
    .description('Apply one reviewed, hash-bound agent-surface evacuation')
    .requiredOption('--plan <path>')
    .requiredOption('--output <path>')
    .requiredOption('--authorize-agent-surface-repair', 'Authorize only the reviewed local repair')
    .action((options) => invoke([
      'agent-surface-repair-apply', '--plan', options.plan, '--output', options.output,
      '--authorize-agent-surface-repair',
    ]));
  migrate
    .command('agent-surface-repair-verify')
    .description('Verify the exact repaired controller and preserved parent evidence')
    .requiredOption('--plan <path>')
    .requiredOption('--output <path>')
    .action((options) => invoke([
      'agent-surface-repair-verify', '--plan', options.plan, '--output', options.output,
    ]));
  migrate
    .command('owner-template')
    .description('Create an unresolved owner-answer template bound to an inventory')
    .requiredOption('--inventory <path>', 'Immutable inventory receipt')
    .requiredOption('--output <path>', 'New owner-answer template outside the project')
    .action((options) => invoke(['owner-template', '--inventory', options.inventory, '--output', options.output]));
  migrate
    .command('generate-policy')
    .description('Generate policies only after every path and legacy config field has a reviewed disposition')
    .requiredOption('--inventory <path>', 'Immutable inventory receipt')
    .requiredOption('--answers <path>', 'Completed owner answers JSON')
    .requiredOption('--output <path>', 'New policy bundle receipt')
    .action((options) => invoke(['generate-policy', '--inventory', options.inventory, '--answers', options.answers, '--output', options.output]));
  migrate
    .command('evacuation-plan')
    .description('Create a byte-stable controller-metadata evacuation plan')
    .requiredOption('--inventory <path>', 'Reviewed immutable inventory receipt')
    .requiredOption('--policy <path>', 'Reviewed generated migration policy bundle')
    .requiredOption('--project <path>', 'Exact product source worktree')
    .requiredOption('--output <path>', 'New plan receipt outside all repositories')
    .action((options) => invoke(['evacuation-plan', '--inventory', options.inventory, '--policy', options.policy, '--project', options.project, '--output', options.output]));
  migrate
    .command('evacuation-apply')
    .description('Apply an evacuation plan only to a clean disposable linked worktree')
    .requiredOption('--plan <path>', 'Reviewed evacuation plan')
    .requiredOption('--candidate <path>', 'Disposable linked candidate worktree')
    .requiredOption('--output <path>', 'New apply receipt outside all repositories')
    .requiredOption('--allow-disposable-mutation', 'Acknowledge mutation of the disposable candidate')
    .action((options) => invoke(['evacuation-apply', '--plan', options.plan, '--candidate', options.candidate, '--output', options.output, '--allow-disposable-mutation']));
  migrate
    .command('evacuation-verify')
    .description('Verify an applied candidate has only the planned metadata changes')
    .requiredOption('--plan <path>', 'Reviewed evacuation plan')
    .requiredOption('--candidate <path>', 'Candidate worktree to verify')
    .requiredOption('--output <path>', 'New verification receipt outside all repositories')
    .action((options) => invoke(['evacuation-verify', '--plan', options.plan, '--candidate', options.candidate, '--output', options.output]));

  const registration = migrate
    .command('registration')
    .description('Plan, apply, verify, or roll back protected controller registration');
  registration
    .command('plan')
    .description('Freeze an exact no-mutation controller registration plan')
    .requiredOption('--source-controller <path>')
    .requiredOption('--source-ref <ref>')
    .requiredOption('--expected-source-head <sha>')
    .requiredOption('--target-controller <path>')
    .requiredOption('--target-ref <ref>')
    .requiredOption('--expected-target-head <sha>')
    .requiredOption('--product-root <path>')
    .requiredOption('--product-ref <ref>')
    .requiredOption('--expected-product-head <sha>')
    .requiredOption('--runtime <path>')
    .requiredOption('--runtime-version <version>')
    .requiredOption('--inventory <path>')
    .requiredOption('--policy-bundle <path>')
    .requiredOption('--pending-verification <path>')
    .requiredOption('--output <path>')
    .action((options) => invoke([
      'registration', 'plan',
      '--source-controller', options.sourceController, '--source-ref', options.sourceRef,
      '--expected-source-head', options.expectedSourceHead,
      '--target-controller', options.targetController, '--target-ref', options.targetRef,
      '--expected-target-head', options.expectedTargetHead,
      '--product-root', options.productRoot, '--product-ref', options.productRef,
      '--expected-product-head', options.expectedProductHead,
      '--runtime', options.runtime, '--runtime-version', options.runtimeVersion,
      '--inventory', options.inventory, '--policy-bundle', options.policyBundle,
      '--pending-verification', options.pendingVerification,
      '--output', options.output,
    ]));
  registration.command('apply')
    .description('Apply an exact plan under an explicit registration authorization')
    .requiredOption('--plan <path>').requiredOption('--output <path>')
    .requiredOption('--authorize-apply', 'Authorize only this local controller registration')
    .action((options) => invoke(['registration', 'apply', '--plan', options.plan, '--output', options.output, '--authorize-apply']));
  registration.command('verify')
    .description('Read back registration truth without mutation')
    .requiredOption('--plan <path>').requiredOption('--output <path>')
    .action((options) => invoke(['registration', 'verify', '--plan', options.plan, '--output', options.output]));
  registration.command('rollback')
    .description('Restore the exact prior registration under explicit authorization')
    .requiredOption('--plan <path>').requiredOption('--output <path>')
    .requiredOption('--authorize-rollback', 'Authorize only this local registration rollback')
    .action((options) => invoke(['registration', 'rollback', '--plan', options.plan, '--output', options.output, '--authorize-rollback']));
}
