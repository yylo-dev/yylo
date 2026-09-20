import { execFile as execFileCallback } from 'node:child_process';
import { createHash } from 'node:crypto';
import { promisify } from 'node:util';
import * as path from 'node:path';
import fs from 'fs-extra';
import managedAssetManifest from '../templates/managed-assets.json';
import { version as packageVersion } from '../version.js';

// One package-owned maintenance engine; first-use callers consume these APIs.
export {
  prepareControllerGeneration, applyControllerGeneration, recoverControllerGeneration,
  assertControllerGenerationReady,
} from './controller-generation-migration.js';

const execFile = promisify(execFileCallback);
const SHA_PATTERN = /^[0-9a-f]{40,64}$/;
const HASH_PATTERN = /^[0-9a-f]{64}$/;
const TARGET_MANIFEST = 'juno-code/src/templates/managed-assets.json';
const TARGET_PACKAGE = 'juno-code/package.json';
const GENERATION_POLICY = '.juno_task/config/task-workspace.json';

export type ManagedControllerGenerationReceipt = {
  schema_version: 'juno_managed_controller_runtime.v1';
  target_sha: string;
  package_version: string;
  policy_sha256?: string;
  scripts: Record<string, {
    classification: 'exact' | 'preserved_customization';
    source_sha256: string;
    actual_sha256: string;
  }>;
};

export type TargetBoundManagedRecovery = {
  packageVersion: string;
  targetSha: string;
  generationSha256: string;
  generationPolicySha256: string;
  policySha256: string;
  checkpointSha256: string;
  packageIdentitySha256: string;
  assets: ReadonlyMap<string, Buffer>;
};

type ManagedDefinition = {
  source: string;
  destination: string;
  installClass: 'project' | 'script' | 'controller';
  type: string;
  macro?: string;
};

function sha256(value: Buffer | string): string {
  return createHash('sha256').update(value).digest('hex');
}

async function gitBuffer(projectDir: string, args: string[]): Promise<Buffer> {
  const result = await execFile('git', ['-C', projectDir, ...args], {
    encoding: 'buffer',
    maxBuffer: 64 * 1024 * 1024,
  });
  return result.stdout as Buffer;
}

async function gitText(projectDir: string, args: string[]): Promise<string> {
  return (await gitBuffer(projectDir, args)).toString('utf8').trim();
}

async function targetBytes(projectDir: string, targetSha: string, relative: string): Promise<Buffer> {
  if (path.isAbsolute(relative) || relative.split('/').includes('..') || relative.includes('\\')) {
    throw new Error(`Target-bound managed source path is unsafe: ${relative}`);
  }
  try {
    return await gitBuffer(projectDir, ['show', `${targetSha}:${relative}`]);
  } catch (error) {
    throw new Error(`Target-bound managed source is missing: ${relative}`, { cause: error });
  }
}

function controllerDefinitions(manifest: typeof managedAssetManifest): ManagedDefinition[] {
  const definitions = [
    ...(manifest.assets as ManagedDefinition[]).filter((asset) =>
      asset.type !== 'config' && asset.type !== 'wiki'),
    ...manifest.controllerOutputs.filter((asset) => asset.type !== 'wiki').map((asset) => ({
      ...asset,
      installClass: 'controller' as const,
    })),
  ];
  return [...new Map(definitions.map((asset) => [asset.destination, asset])).values()];
}

async function assertInstalledPackageIdentity(
  projectDir: string,
  expectedVersion: string,
  invokedPackageScriptsDir: string,
): Promise<string> {
  const identityPath = path.join(projectDir, '.juno_task/runtime/identity.json');
  const identity = await fs.readJson(identityPath).catch((error) => {
    throw new Error(`Managed runtime identity is unavailable: ${String(error)}`);
  }) as Record<string, unknown>;
  if (identity.package !== '@yylo/cli' || identity.version !== expectedVersion ||
      identity.source !== 'installed-release' || identity.tracked !== false ||
      typeof identity.executable !== 'string' ||
      !HASH_PATTERN.test(String(identity.executable_sha256 ?? ''))) {
    throw new Error('Managed runtime identity does not match the target-bound package generation');
  }
  const executable = path.resolve(identity.executable);
  const executableBytes = await fs.readFile(executable).catch((error) => {
    throw new Error(`Managed runtime executable is unavailable: ${String(error)}`);
  });
  if (sha256(executableBytes) !== identity.executable_sha256) {
    throw new Error('Managed runtime executable hash does not match its registered identity');
  }
  const installedRoot = path.resolve(path.dirname(executable), '..', '..');
  const installedPackagePath = path.join(installedRoot, 'package.json');
  const installedManifestPath = path.join(installedRoot, 'dist/templates/managed-assets.json');
  const installedScriptsDir = path.join(installedRoot, 'dist/templates/scripts');
  const [installedPackageBytes, installedManifestBytes, installedScriptsReal, invokedScriptsReal] =
    await Promise.all([
      fs.readFile(installedPackagePath).catch((error) => {
        throw new Error(`Installed package identity is unavailable: ${String(error)}`);
      }),
      fs.readFile(installedManifestPath).catch((error) => {
        throw new Error(`Installed package managed declaration is unavailable: ${String(error)}`);
      }),
      fs.realpath(installedScriptsDir).catch((error) => {
        throw new Error(`Installed package scripts are unavailable: ${String(error)}`);
      }),
      fs.realpath(invokedPackageScriptsDir).catch((error) => {
        throw new Error(`Invoked package scripts are unavailable: ${String(error)}`);
      }),
    ]);
  if (installedScriptsReal !== invokedScriptsReal) {
    throw new Error('Invoked package source is not the registered routed runtime');
  }
  let installedPackage: Record<string, unknown>;
  let installedManifest: unknown;
  try {
    installedPackage = JSON.parse(installedPackageBytes.toString('utf8')) as Record<string, unknown>;
    installedManifest = JSON.parse(installedManifestBytes.toString('utf8')) as unknown;
  } catch (error) {
    throw new Error(`Installed package declaration is invalid: ${String(error)}`);
  }
  if (installedPackage.name !== '@yylo/cli' || installedPackage.version !== expectedVersion ||
      JSON.stringify(installedManifest) !== JSON.stringify(managedAssetManifest)) {
    throw new Error('Installed package declaration is mixed with the invoked package identity');
  }
  return sha256(Buffer.concat([
    Buffer.from(`${sha256(executableBytes)}\0${sha256(installedPackageBytes)}\0`),
    Buffer.from(sha256(installedManifestBytes)),
  ]));
}

/**
 * Resolve the one recovery exception to package-template parity.
 *
 * The ignored runtime generation, registered executable, immutable target ref,
 * target package/declaration, controller policy hash, and every target source
 * hash must all agree. Any absent provenance retains the ordinary package-bound
 * refusal; version equality alone never admits recovery.
 */
export async function resolveTargetBoundManagedRecovery(
  projectDir: string,
  generation: ManagedControllerGenerationReceipt,
  invokedPackageScriptsDir: string,
): Promise<TargetBoundManagedRecovery> {
  if (generation.schema_version !== 'juno_managed_controller_runtime.v1' ||
      !SHA_PATTERN.test(generation.target_sha) ||
      typeof generation.package_version !== 'string' ||
      !generation.scripts || typeof generation.scripts !== 'object') {
    throw new Error('Managed controller generation receipt is invalid');
  }
  if (generation.package_version !== packageVersion) {
    throw new Error('Invoked package version does not match the target-bound generation');
  }
  const generationPath = path.join(
    projectDir, '.juno_task/runtime/managed-controller/generation.json',
  );
  const generationBytes = await fs.readFile(generationPath).catch((error) => {
    throw new Error(`Managed controller generation receipt is unavailable: ${String(error)}`);
  });
  let persistedGeneration: unknown;
  try {
    persistedGeneration = JSON.parse(generationBytes.toString('utf8'));
  } catch (error) {
    throw new Error(`Managed controller generation receipt is invalid: ${String(error)}`);
  }
  if (JSON.stringify(persistedGeneration) !== JSON.stringify(generation)) {
    throw new Error('Managed controller generation receipt changed during recovery admission');
  }
  const generationSha256 = sha256(generationBytes);
  const packageIdentitySha256 = await assertInstalledPackageIdentity(
    projectDir, generation.package_version, invokedPackageScriptsDir,
  );

  const generationPolicyPath = path.join(projectDir, GENERATION_POLICY);
  const generationPolicyBytes = await fs.readFile(generationPolicyPath).catch((error) => {
    throw new Error(`Managed generation task policy is unavailable: ${String(error)}`);
  });
  const generationPolicySha256 = sha256(generationPolicyBytes);
  const committedGenerationPolicy = await targetBytes(projectDir, 'HEAD', GENERATION_POLICY);
  if (generation.policy_sha256 !== generationPolicySha256 ||
      !committedGenerationPolicy.equals(generationPolicyBytes)) {
    throw new Error('Managed generation task policy has mixed recovery provenance');
  }

  const policyPath = path.join(projectDir, '.juno_task/config/metadata-controller.json');
  const policyBytes = await fs.readFile(policyPath);
  const policy = JSON.parse(policyBytes.toString('utf8')) as Record<string, unknown>;
  const generated = policy.generated_metadata;
  const tracked = policy.tracked_exact;
  const policySha256 = sha256(policyBytes);
  if (policy.schema_version !== 'juno_metadata_controller_policy.v1' ||
      typeof policy.controller_branch !== 'string' || typeof policy.product_ref !== 'string' ||
      !Array.isArray(generated) || !Array.isArray(tracked) ||
      !generated.every((entry) => typeof entry === 'string') ||
      !tracked.every((entry) => typeof entry === 'string')) {
    throw new Error('Controller policy does not match the target-bound generation receipt');
  }
  // The generation may predate the tracked schema-2 bundle classification
  // expansion. Current policy is admissible only as exact committed controller
  // state; an uncommitted edit can never turn an old receipt into recovery proof.
  const policyRelative = '.juno_task/config/metadata-controller.json';
  const committedPolicy = await targetBytes(projectDir, 'HEAD', policyRelative);
  if (!committedPolicy.equals(policyBytes)) {
    throw new Error('Controller policy has uncommitted mixed recovery provenance');
  }
  const [branch, targetSha] = await Promise.all([
    gitText(projectDir, ['symbolic-ref', '-q', 'HEAD']),
    gitText(projectDir, ['rev-parse', '--verify', `${policy.product_ref}^{commit}`]),
  ]);
  if (branch !== policy.controller_branch || targetSha !== generation.target_sha) {
    throw new Error('Controller or target ref moved outside the target-bound generation');
  }

  const [targetPackageBytes, targetManifestBytes] = await Promise.all([
    targetBytes(projectDir, generation.target_sha, TARGET_PACKAGE),
    targetBytes(projectDir, generation.target_sha, TARGET_MANIFEST),
  ]);
  const targetPackage = JSON.parse(targetPackageBytes.toString('utf8')) as Record<string, unknown>;
  const targetManifest = JSON.parse(targetManifestBytes.toString('utf8')) as typeof managedAssetManifest;
  if (targetPackage.name !== '@yylo/cli' ||
      targetPackage.version !== generation.package_version ||
      JSON.stringify(targetManifest) !== JSON.stringify(managedAssetManifest)) {
    throw new Error('Target source declaration is mixed with the installed package identity');
  }

  const definitions = controllerDefinitions(targetManifest);
  const scriptDefinitions = definitions.filter((asset) => asset.installClass === 'script');
  const expectedScripts = new Set(scriptDefinitions.map((asset) => asset.destination));
  if (expectedScripts.size !== Object.keys(generation.scripts).length ||
      Object.keys(generation.scripts).some((destination) => !expectedScripts.has(destination))) {
    throw new Error('Target script inventory does not match the managed generation receipt');
  }
  const requiredTracked = [
    '.juno_task/managed-assets.json',
    ...definitions.map((asset) => asset.destination)
      .filter((destination) => /^\.juno_task\/(prompts|wiki|workflows)\//.test(destination)),
  ];
  if (requiredTracked.some((destination) =>
    !generated.includes(destination) || !tracked.includes(destination))) {
    throw new Error('Controller policy does not classify the complete managed bundle');
  }
  const checkpointRelative = '.juno_task/config.json';
  const checkpointPath = path.join(projectDir, checkpointRelative);
  const checkpointBytes = await fs.readFile(checkpointPath);
  const committedCheckpoint = await targetBytes(projectDir, 'HEAD', checkpointRelative);
  if (!committedCheckpoint.equals(checkpointBytes)) {
    throw new Error('Controller checkpoint policy has uncommitted mixed recovery provenance');
  }
  const checkpoint = JSON.parse(checkpointBytes.toString('utf8')) as Record<string, any>;
  const checkpointInclude = checkpoint?.gitCheckpoint?.include;
  if (!Array.isArray(checkpointInclude) ||
      !checkpointInclude.every((entry) => typeof entry === 'string') ||
      requiredTracked.some((destination) =>
        !checkpointInclude.some((root: string) =>
          destination === root || destination.startsWith(`${root.replace(/\/$/, '')}/`)))) {
    throw new Error('Controller checkpoint policy does not classify the complete managed bundle');
  }
  const checkpointSha256 = sha256(checkpointBytes);

  const assets = new Map<string, Buffer>();
  await Promise.all(definitions.map(async (asset) => {
    const bytes = await targetBytes(
      projectDir,
      generation.target_sha,
      `juno-code/src/templates/${asset.source}`,
    );
    assets.set(asset.destination, bytes);
    if (asset.installClass !== 'script') return;
    const binding = generation.scripts[asset.destination];
    const sourceHash = sha256(bytes);
    const installedPath = path.join(projectDir, asset.destination);
    const installedBytes = await fs.readFile(installedPath).catch((error) => {
      throw new Error(`Receipt-bound managed script is unavailable: ${asset.destination}`, {
        cause: error,
      });
    });
    if (binding?.classification !== 'exact' || binding.source_sha256 !== sourceHash ||
        binding.actual_sha256 !== sourceHash || sha256(installedBytes) !== sourceHash) {
      throw new Error(`Managed script has mixed target provenance: ${asset.destination}`);
    }
  }));

  return {
    packageVersion: generation.package_version,
    targetSha: generation.target_sha,
    generationSha256,
    generationPolicySha256,
    policySha256,
    checkpointSha256,
    packageIdentitySha256,
    assets,
  };
}
