import path from 'node:path';
import { fileURLToPath } from 'node:url';
import fs from 'fs-extra';
import { Command } from 'commander';
import { routeControlPlane } from '../../utils/control-plane-router.js';
import { addMachineOutputOptions, invokeMachineAwareChild, resolveMachineOutput } from '../machine-output.js';

export type IntegrationOperation =
  | 'status' | 'sync' | 'runtime-doctor' | 'runtime-refresh' | 'runtime-adopt-source'
  | 'register' | 'repair' | 'push';
export type IntegrationOptions = {
  fetch?: boolean;
  owner?: string;
  replace?: boolean;
  dryRun?: boolean;
  apply?: string;
  canonicalOwnerRefresh?: boolean;
  preserveOwners?: string;
  previousSha?: string;
  targetSha?: string;
  installPrefix?: string;
  output?: string;
  isolatedSource?: boolean;
};
export type IntegrationInvoker = (
  operation: IntegrationOperation,
  options: IntegrationOptions,
) => Promise<void>;

export function packagedIntegrationRuntimeCandidates(): string[] {
  const directory = path.dirname(fileURLToPath(import.meta.url));
  return [
    path.resolve(directory, '../templates/scripts/integration_workspace.py'),
    path.resolve(directory, '../../templates/scripts/integration_workspace.py'),
    path.resolve(directory, '../../src/templates/scripts/integration_workspace.py'),
  ];
}

export async function selectIntegrationRuntime(
  controllerRoot: string,
  operation: IntegrationOperation,
  _options: IntegrationOptions,
  packagedCandidates = packagedIntegrationRuntimeCandidates(),
): Promise<string> {
  const canonical = path.join(controllerRoot, '.juno_task', 'scripts', 'integration_workspace.py');
  if (operation === 'runtime-adopt-source') {
    const packaged = packagedCandidates.find((candidate) => fs.existsSync(candidate));
    if (!packaged) {
      throw new Error('Packaged source-runtime adoption engine is missing; refusing partial recovery.');
    }
    const source = await fs.readFile(packaged, 'utf8');
    if (!source.includes('SOURCE_ADOPTION_SCHEMA = "juno_source_runtime_adoption.v1"') ||
        !source.includes('def source_runtime_adopt(') ||
        !source.includes('"runtime-adopt-source"')) {
      throw new Error('Packaged source-runtime adoption engine is incompatible; refusing partial recovery.');
    }
    return packaged;
  }
  const bootstrapRecovery = operation === 'runtime-refresh';
  if (!bootstrapRecovery) {
    if (!(await fs.pathExists(canonical))) {
      throw new Error('Missing managed integration runtime. Run `yy scripts update` and retry.');
    }
    return canonical;
  }
  const packaged = packagedCandidates.find((candidate) => fs.existsSync(candidate));
  if (!packaged) {
    throw new Error('Packaged managed-runtime recovery engine is missing; refusing stale controller fallback.');
  }
  const sibling = path.join(path.dirname(packaged), 'task_workspace.py');
  if (!(await fs.pathExists(sibling))) {
    throw new Error('Packaged managed-runtime recovery engine is incomplete; refusing stale controller fallback.');
  }
  const source = await fs.readFile(packaged, 'utf8');
  const protocol = [
    'MANAGED_REPAIR_SCHEMA = "juno_managed_runtime_repair.v1"',
    'def managed_runtime_repair_plan(',
    'repair_mode.add_argument("--dry-run"',
    'repair_mode.add_argument("--apply"',
  ];
  if (!protocol.every((marker) => source.includes(marker))) {
    throw new Error('Packaged managed-runtime recovery engine is incompatible; refusing stale controller fallback.');
  }
  return packaged;
}

export async function invokeIntegration(
  operation: IntegrationOperation,
  options: IntegrationOptions = {},
): Promise<void> {
  const route = routeControlPlane(process.cwd(), 'orchestration');
  const script = await selectIntegrationRuntime(route.controllerRoot, operation, options);
  const argv = [script, '--controller', route.controllerRoot, operation];
  if (operation === 'status' && options.fetch) argv.push('--fetch');
  if (operation === 'runtime-adopt-source') {
    if (!options.previousSha || !options.targetSha || !options.installPrefix || !options.output) {
      throw new Error('integration runtime-adopt-source requires --previous-sha, --target-sha, --install-prefix, and --output');
    }
    argv.push('--previous-sha', options.previousSha, '--target-sha', options.targetSha,
      '--install-prefix', path.resolve(options.installPrefix), '--output', path.resolve(options.output));
    if (options.isolatedSource) argv.push('--isolated-source');
  }
  if (operation === 'runtime-doctor' || operation === 'runtime-refresh') {
    if (operation === 'runtime-refresh') {
      if (!options.previousSha) throw new Error('integration runtime-refresh requires --previous-sha');
      argv.push('--previous-sha', options.previousSha);
      if (options.dryRun) argv.push('--dry-run');
      else if (options.apply) argv.push('--apply', path.resolve(options.apply));
    }
    if (options.targetSha) argv.push('--target-sha', options.targetSha);
  }
  if (operation === 'register') {
    if (!options.owner) throw new Error('integration register requires an owner path');
    const runtimeExecutable = path.resolve(process.argv[1] ?? '');
    const packagedRuntime = packagedIntegrationRuntimeCandidates().find((candidate) => fs.existsSync(candidate));
    if (!packagedRuntime) throw new Error('integration register package runtime is missing');
    const packagePath = path.resolve(path.dirname(packagedRuntime), '../../..', 'package.json');
    const packageJson = await fs.readJson(packagePath) as { name?: string; version?: string };
    if (packageJson.name !== '@yylo/cli' || typeof packageJson.version !== 'string' ||
        !(await fs.pathExists(runtimeExecutable))) {
      throw new Error('integration register cannot prove the invoking package runtime identity');
    }
    argv.push(path.resolve(options.owner), '--runtime-executable', runtimeExecutable,
      '--runtime-version', packageJson.version);
    if (options.replace) argv.push('--replace');
  }
  if (operation === 'repair' || operation === 'push') {
    if (options.dryRun) argv.push('--dry-run');
    else if (options.apply) argv.push('--apply', path.resolve(options.apply));
    else throw new Error(`integration ${operation} requires --dry-run or --apply <receipt>`);
  }
  if (operation === 'repair') {
    if (options.canonicalOwnerRefresh) argv.push('--canonical-owner-refresh');
    if (options.preserveOwners) argv.push('--preserve-owners', path.resolve(options.preserveOwners));
  }
  const machine = resolveMachineOutput(process.argv.slice(2), { jsonFlag: true });
  const { exitCode } = await invokeMachineAwareChild({
    executable: 'python3', args: argv,
    cwd: route.controllerRoot,
    env: route.env,
    command: `integration.${operation}`,
    ...(machine ? { machine } : {}),
  });
  if (exitCode !== 0) process.exitCode = exitCode;
}

export function configureIntegrationCommand(
  program: Command,
  invoke: IntegrationInvoker = invokeIntegration,
): void {
  const integration = addMachineOutputOptions(program
    .command('integration')
    .description('Inspect or synchronize the registered integration owner'));
  integration
    .command('status')
    .description('Show offline integration drift; fetch only when explicitly requested')
    .option('--fetch', 'Refresh the configured remote-tracking ref before reporting')
    .action((options: { fetch?: boolean }) => invoke('status', options));
  integration
    .command('sync')
    .description('Guard, fetch, fast-forward when safe, and refresh exact submodule gitlinks')
    .action(() => invoke('sync', {}));
  integration
    .command('runtime-doctor')
    .description('Verify controller runtime hashes against one exact target generation')
    .option('--target-sha <sha>', 'Exact target generation; defaults to the configured target ref')
    .action((options: { targetSha?: string }) => invoke('runtime-doctor', options));
  integration
    .command('runtime-adopt-source')
    .description('Build and atomically adopt one exact unpublished Juno source generation')
    .requiredOption('--previous-sha <sha>', 'Exact currently admitted target generation')
    .requiredOption('--target-sha <sha>', 'Exact source target generation to adopt')
    .requiredOption('--install-prefix <path>', 'Fresh non-Git package installation prefix')
    .requiredOption('--output <path>', 'New immutable transaction receipt outside Git')
    .option('--isolated-source', 'Build an exact private source checkout without moving the retained owner')
    .action((options: IntegrationOptions) =>
      invoke('runtime-adopt-source', options));
  integration
    .command('runtime-refresh')
    .description('Refresh managed runtime from an exact admitted target transition')
    .requiredOption('--previous-sha <sha>', 'Exact previously admitted target generation')
    .option('--target-sha <sha>', 'Exact target generation; defaults to the configured target ref')
    .option('--dry-run', 'Persist a non-mutating changed-source overlap repair plan')
    .option('--apply <receipt>', 'Apply one exact immutable overlap repair plan')
    .action((options: { previousSha: string; targetSha?: string; dryRun?: boolean; apply?: string }) => {
      if (options.dryRun && options.apply) {
        throw new Error('integration runtime-refresh accepts only one of --dry-run or --apply <receipt>');
      }
      return invoke('runtime-refresh', options);
    });
  integration
    .command('register')
    .description('Bind one verified protected worktree as the canonical integration owner')
    .argument('<owner>', 'Exact linked integration-owner worktree path')
    .option('--replace', 'Replace a different existing canonical registration')
    .action((owner: string, options: { replace?: boolean }) =>
      invoke('register', options.replace ? { owner, replace: true } : { owner }));
  for (const operation of ['repair', 'push'] as const) {
    const command = integration
      .command(operation)
      .description(operation === 'repair'
        ? 'Plan or apply exact, receipt-bound integration topology repair'
        : 'Plan and publish by default, or use explicit plan/apply publication modes')
      .option('--dry-run', 'Persist and print a non-mutating plan receipt')
      .option('--apply <receipt>', 'Apply one exact previously generated plan receipt');
    if (operation === 'repair') {
      command.option('--canonical-owner-refresh', 'Explicit offline non-legacy fast-forward; preserve all historical owners')
        .option('--preserve-owners <approval>', 'Reviewed inventory JSON: approved_by, disposition=preserve-only, inventory_sha256');
    }
    command.action((options: IntegrationOptions) => {
      if (options.dryRun && options.apply) {
        throw new Error(`integration ${operation} accepts only one of --dry-run or --apply <receipt>`);
      }
      if (operation === 'repair' && !options.dryRun && !options.apply) {
        throw new Error('integration repair requires exactly one of --dry-run or --apply <receipt>');
      }
      if (options.preserveOwners && !options.canonicalOwnerRefresh) {
        throw new Error('--preserve-owners requires --canonical-owner-refresh');
      }
      return invoke(operation, {
        ...(options.dryRun ? { dryRun: true } : options.apply ? { apply: options.apply } : {}),
        ...(options.canonicalOwnerRefresh ? { canonicalOwnerRefresh: true } : {}),
        ...(options.preserveOwners ? { preserveOwners: options.preserveOwners } : {}),
      });
    });
  }
}
