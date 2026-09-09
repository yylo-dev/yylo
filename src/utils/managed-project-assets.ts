import { createHash } from 'node:crypto';
import * as path from 'node:path';
import { fileURLToPath } from 'node:url';
import fs from 'fs-extra';
import managedAssetManifest from '../templates/managed-assets.json';
import { version as packageVersion } from '../version.js';
import type { TargetBoundManagedRecovery } from './managed-controller-recovery.js';
import {
  assertPackageSource,
  assertSafeManagedWritePath,
  lstatIfPresent,
} from './managed-update-transaction.js';

type ManagedAssetDefinition = {
  source: string;
  destination: string;
  installClass: 'project' | 'script' | 'controller';
  type: string;
  macro?: string;
};

type InstructionBundleDeclaration = {
  schemaVersion: 'juno_instruction_bundle_declaration.v1';
  semanticVersion: string;
};

type ManagedControllerOutputDefinition = {
  source: string;
  destination: string;
  type: string;
};

const INSTRUCTION_BUNDLE_DECLARATION =
  managedAssetManifest.instructionBundle as InstructionBundleDeclaration;
const MANAGED_ASSET_DEFINITIONS = managedAssetManifest.assets as ManagedAssetDefinition[];
const MANAGED_CONTROLLER_OUTPUTS = (
  managedAssetManifest.controllerOutputs as ManagedControllerOutputDefinition[]
).map((asset): ManagedAssetDefinition => ({ ...asset, installClass: 'controller' }));

export const MANAGED_ASSETS = MANAGED_ASSET_DEFINITIONS.filter(
  (asset) => asset.installClass !== 'controller',
);
export const MANAGED_CONTROLLER_ASSETS = MANAGED_ASSET_DEFINITIONS.filter(
  (asset) => asset.installClass === 'controller',
);

export const MANAGED_PROJECT_ASSETS = MANAGED_ASSET_DEFINITIONS.filter(
  (asset) => asset.installClass === 'project',
);

function isMetadataOnlyController(projectConfig: Record<string, any>): boolean {
  const controllerWorkspace = projectConfig?.controllerWorkspace;
  return controllerWorkspace?.mode === 'metadata-only' &&
    controllerWorkspace?.policy === '.juno_task/config/metadata-controller.json';
}

function managedAssetsForProject(projectConfig: Record<string, any>): ManagedAssetDefinition[] {
  if (!isMetadataOnlyController(projectConfig)) {
    return MANAGED_ASSET_DEFINITIONS.filter((asset) => asset.installClass !== 'controller');
  }
  // Reviewed controller policy/config is owner-owned. Package updates manage
  // only declared runtime/instruction outputs and bind those exact bytes in the
  // tracked receipt; they never replace controller/product refs or policy.
  const declared = [
    ...MANAGED_ASSET_DEFINITIONS.filter((asset) => asset.type !== 'config'),
    ...MANAGED_CONTROLLER_OUTPUTS,
  ];
  return [...new Map(declared.map((asset) => [asset.destination, asset])).values()];
}

export const MANAGED_PROMPT_MACROS = Object.fromEntries(
  MANAGED_ASSET_DEFINITIONS.filter((asset) => asset.macro).map((asset) => [
    asset.macro as string,
    { path: asset.destination },
  ]),
) as Record<string, { path: string }>;

export interface ManagedAssetRecord {
  type: string;
  templateVersion: string;
  sourceSha256: string;
  installedSha256: string;
}

export interface ManagedInstructionBundleIdentity {
  schemaVersion: 'juno_instruction_bundle.v1';
  semanticVersion: string;
  packageVersion: string;
  assetCount: number;
  assetsSha256: string;
  bundleSha256: string;
}

interface ManagedAssetManifest {
  schemaVersion: 1 | 2;
  packageName: '@yylo/cli' | 'juno-code';
  packageVersion: string;
  instructionBundle?: ManagedInstructionBundleIdentity;
  assets: Record<string, ManagedAssetRecord>;
}

export interface ManagedAssetUpdateResult {
  installed: string[];
  updated: string[];
  unchanged: string[];
  conflicts: Array<{ destination: string; candidate: string }>;
  backups: Array<{ destination: string; backup: string }>;
  macrosAdded: string[];
  macroConflicts: string[];
}

// Version-bound migration inventory for installations created before the Bolt
// task-worktree generation. Every removed byte is copied to managed-conflicts.
const RETIRED_BEFORE_BOLT_2_0_32 = [
  '.juno_task/scripts/task_lifecycle.py',
  '.juno_task/scripts/integration_candidate.py',
  '.juno_task/scripts/integration_owner_preflight.py',
  '.juno_task/scripts/worktree_lifecycle.py',
  '.juno_task/scripts/tests/test_task_lifecycle.py',
  '.juno_task/scripts/tests/test_controller_workspace.py',
  '.juno_task/scripts/tests/test_integration_concurrency.py',
  '.juno_task/config/lifecycle.json',
  '.juno_task/config/controller-workspace.json',
] as const;

const RETIRED_SPECIALIZATION_RECEIPT =
  '.juno_task/managed-specializations/clean-worktree.json';
const BOLT_PROMPT = '.juno_task/prompts/clean_worktree.md';

export type ManagedAssetGenerationState =
  | 'current'
  | 'specialized'
  | 'missing'
  | 'outdated'
  | 'customized';

export interface ManagedAssetGenerationReport {
  status: 'coherent' | 'mixed' | 'incomplete' | 'customized';
  coherent: boolean;
  instructionBundle: ManagedInstructionBundleIdentity | null;
  entries: Array<{
    destination: string;
    installClass: 'project' | 'script' | 'controller';
    state: ManagedAssetGenerationState;
  }>;
}

function sha256(content: Buffer | string): string {
  return createHash('sha256').update(content).digest('hex');
}

export function managedAssetRecordsIdentity(
  assets: Record<string, ManagedAssetRecord>,
): string {
  return sha256(JSON.stringify(Object.entries(assets).sort(([left], [right]) =>
    Buffer.compare(Buffer.from(left, 'utf8'), Buffer.from(right, 'utf8')))
    .map(([destination, record]) => ({
      destination,
      type: record.type,
      sourceSha256: record.sourceSha256,
      installedSha256: record.installedSha256,
    }))));
}

function instructionBundleIdentity(
  assets: Record<string, ManagedAssetRecord>,
  identityVersion = packageVersion,
): ManagedInstructionBundleIdentity {
  const core = {
    schemaVersion: 'juno_instruction_bundle.v1' as const,
    semanticVersion: INSTRUCTION_BUNDLE_DECLARATION.semanticVersion,
    packageVersion: identityVersion,
    assetCount: Object.keys(assets).length,
    assetsSha256: managedAssetRecordsIdentity(assets),
  };
  return { ...core, bundleSha256: sha256(JSON.stringify(core)) };
}

function validateManifest(manifest: unknown, manifestPath: string): ManagedAssetManifest {
  const parsed = manifest as Partial<ManagedAssetManifest> | null;
  const packageNameValid = parsed?.packageName === '@yylo/cli' ||
    (parsed?.schemaVersion === 1 && parsed.packageName === 'juno-code');
  if ((parsed?.schemaVersion !== 1 && parsed?.schemaVersion !== 2) ||
      !packageNameValid || typeof parsed.packageVersion !== 'string' ||
      typeof parsed.assets !== 'object' || parsed.assets === null) {
    throw new Error(`Unsupported managed asset manifest: ${manifestPath}`);
  }
  if (parsed.schemaVersion === 2) {
    const identity = parsed.instructionBundle;
    if (identity?.schemaVersion !== 'juno_instruction_bundle.v1' ||
        typeof identity.semanticVersion !== 'string' ||
        typeof identity.packageVersion !== 'string' ||
        !Number.isInteger(identity.assetCount) ||
        !/^[0-9a-f]{64}$/.test(identity.assetsSha256) ||
        !/^[0-9a-f]{64}$/.test(identity.bundleSha256) ||
        identity.assetCount !== Object.keys(parsed.assets).length ||
        identity.assetsSha256 !== managedAssetRecordsIdentity(parsed.assets) ||
        identity.packageVersion !== parsed.packageVersion ||
        identity.bundleSha256 !== sha256(JSON.stringify({
          schemaVersion: identity.schemaVersion,
          semanticVersion: identity.semanticVersion,
          packageVersion: identity.packageVersion,
          assetCount: identity.assetCount,
          assetsSha256: identity.assetsSha256,
        }))) {
      throw new Error(`Mixed or partial managed instruction bundle: ${manifestPath}`);
    }
  }
  return parsed as ManagedAssetManifest;
}

function safeVersion(version: string): string {
  return version.replace(/[^A-Za-z0-9_.-]/g, '_');
}

async function writeAtomic(
  destination: string,
  content: Buffer | string,
  projectRoot: string,
): Promise<void> {
  await assertSafeManagedWritePath(projectRoot, destination);
  await fs.ensureDir(path.dirname(destination));
  const temporary = `${destination}.tmp-${process.pid}-${Date.now()}`;
  await fs.writeFile(temporary, content);
  await fs.rename(temporary, destination);
}

function emptyManifest(identityVersion = packageVersion): ManagedAssetManifest {
  const assets: Record<string, ManagedAssetRecord> = {};
  return {
    schemaVersion: 2,
    packageName: '@yylo/cli',
    packageVersion: identityVersion,
    instructionBundle: instructionBundleIdentity(assets, identityVersion),
    assets,
  };
}

function targetBoundSource(
  recovery: TargetBoundManagedRecovery | undefined,
  asset: ManagedAssetDefinition,
): Buffer | undefined {
  if (!recovery) return undefined;
  const content = recovery.assets.get(asset.destination);
  if (!content) {
    throw new Error(`Target-bound managed source is incomplete: ${asset.destination}`);
  }
  return content;
}

export class ManagedProjectAssets {
  static getTemplatesDirectory(): string | null {
    const dirname = path.dirname(fileURLToPath(import.meta.url));
    const candidates = [
      path.join(dirname, '..', '..', 'templates'),
      path.join(dirname, '..', 'templates'),
    ];
    return candidates.find((candidate) => fs.existsSync(candidate)) ?? null;
  }

  /** Validate every source, config, manifest, and possible write path without writing. */
  static async preflight(
    projectDir: string,
    options: { force?: boolean; recovery?: TargetBoundManagedRecovery | undefined } = {},
  ): Promise<void> {
    const junoTaskDir = path.join(projectDir, '.juno_task');
    const junoTaskEntry = await lstatIfPresent(junoTaskDir);
    if (!junoTaskEntry) return;
    await assertSafeManagedWritePath(projectDir, junoTaskDir);
    if (!junoTaskEntry.isDirectory()) {
      throw new Error(`Managed project root is not a directory: ${junoTaskDir}`);
    }

    const projectConfigPath = path.join(junoTaskDir, 'config.json');
    let projectConfig: Record<string, any> = {};
    if (await fs.pathExists(projectConfigPath)) {
      projectConfig = await fs.readJson(projectConfigPath);
      const controllerWorkspace = projectConfig?.controllerWorkspace;
      const metadataOnlyController =
        controllerWorkspace?.mode === 'metadata-only' &&
        controllerWorkspace?.policy === '.juno_task/config/metadata-controller.json';
      if (projectConfig?.lifecycle !== undefined ||
          (controllerWorkspace !== undefined && !metadataOnlyController)) {
        throw new Error(
          'Legacy Juno 2.0 lifecycle/controllerWorkspace config requires the reviewed 2.1 ' +
            'migration flow. Run `yy migrate inventory`, generate the owner-reviewed policy, ' +
            'then apply and verify `yy migrate evacuation-*` in a disposable worktree before ' +
            'updating managed assets.',
        );
      }
    }

    const templatesDir = this.getTemplatesDirectory();
    if (!templatesDir && !options.recovery) {
      throw new Error('YYLO managed prompt/wiki templates are missing from this package');
    }
    const manifestPath = path.join(junoTaskDir, 'managed-assets.json');
    let manifest = emptyManifest(options.recovery?.packageVersion);
    if (await fs.pathExists(manifestPath)) {
      manifest = validateManifest(await fs.readJson(manifestPath), manifestPath);
    }

    if (templatesDir && !options.recovery) {
      await assertPackageSource(templatesDir, templatesDir, 'directory');
    }
    const possiblePaths = [
      projectConfigPath,
      manifestPath,
      path.join(projectDir, RETIRED_SPECIALIZATION_RECEIPT),
      path.join(
        projectDir, '.juno_task', 'managed-conflicts', safeVersion(packageVersion),
        '.juno_task/config.json.candidate',
      ),
    ];
    for (const asset of managedAssetsForProject(projectConfig)) {
      const recovered = targetBoundSource(options.recovery, asset);
      if (!recovered) {
        const sourcePath = path.join(templatesDir as string, asset.source);
        await assertPackageSource(sourcePath, templatesDir as string, 'file');
      }
      possiblePaths.push(
        path.join(projectDir, asset.destination),
        path.join(
          projectDir, '.juno_task', 'managed-conflicts', safeVersion(packageVersion),
          `${asset.destination}.candidate`,
        ),
        path.join(
          projectDir, '.juno_task', 'managed-conflicts', `bolt-${safeVersion(packageVersion)}`,
          `${asset.destination}.backup`,
        ),
      );
    }
    for (const destination of possiblePaths) {
      await assertSafeManagedWritePath(projectDir, destination);
    }
    // Force backups may select a content-addressed collision name. Reject any
    // pre-existing link in that package-owned archive tree now rather than
    // discovering it after another destination was replaced.
    const backupTree = path.join(
      projectDir,
      '.juno_task',
      'managed-conflicts',
      `bolt-${safeVersion(packageVersion)}`,
    );
    if (await fs.pathExists(backupTree)) {
      const pending = [backupTree];
      while (pending.length > 0) {
        const current = pending.pop() as string;
        for (const entry of await fs.readdir(current, { withFileTypes: true })) {
          const child = path.join(current, entry.name);
          if (entry.isSymbolicLink()) {
            throw new Error(
              `Refusing symbolic-link managed backup: ${path.relative(projectDir, child)}`,
            );
          }
          if (entry.isDirectory()) pending.push(child);
        }
      }
    }
    await this.assertRetiredGenerationSafe(projectDir, manifest, Boolean(options.force));
  }

  static async update(
    projectDir: string,
    options: {
      force?: boolean;
      silent?: boolean;
      recovery?: TargetBoundManagedRecovery | undefined;
    } = {},
  ): Promise<ManagedAssetUpdateResult> {
    await this.preflight(projectDir, {
      force: Boolean(options.force), recovery: options.recovery,
    });
    const result: ManagedAssetUpdateResult = {
      installed: [],
      updated: [],
      unchanged: [],
      conflicts: [],
      backups: [],
      macrosAdded: [],
      macroConflicts: [],
    };
    const junoTaskDir = path.join(projectDir, '.juno_task');
    if (!(await fs.pathExists(junoTaskDir))) {
      return result;
    }
    const projectConfigPath = path.join(junoTaskDir, 'config.json');
    let projectConfig: Record<string, any> = {};
    if (await fs.pathExists(projectConfigPath)) {
      projectConfig = await fs.readJson(projectConfigPath);
      const controllerWorkspace = projectConfig?.controllerWorkspace;
      const metadataOnlyController =
        controllerWorkspace?.mode === 'metadata-only' &&
        controllerWorkspace?.policy === '.juno_task/config/metadata-controller.json';
      if (projectConfig?.lifecycle !== undefined ||
          (controllerWorkspace !== undefined && !metadataOnlyController)) {
        throw new Error(
          'Legacy Juno 2.0 lifecycle/controllerWorkspace config requires the reviewed 2.1 ' +
            'migration flow. Run `yy migrate inventory`, generate the owner-reviewed policy, ' +
            'then apply and verify `yy migrate evacuation-*` in a disposable worktree before ' +
            'updating managed assets.',
        );
      }
    }
    const templatesDir = this.getTemplatesDirectory();
    if (!templatesDir && !options.recovery) {
      throw new Error('YYLO managed prompt/wiki templates are missing from this package');
    }

    const manifestPath = path.join(junoTaskDir, 'managed-assets.json');
    let manifest = emptyManifest(options.recovery?.packageVersion);
    if (await fs.pathExists(manifestPath)) {
      manifest = validateManifest(await fs.readJson(manifestPath), manifestPath);
    }

    // Validate all possible install/candidate/backup parents before the first
    // generation write. A missing leaf below a symlinked directory is just as
    // unsafe as a symlinked leaf.
    await assertSafeManagedWritePath(projectDir, projectConfigPath);
    await assertSafeManagedWritePath(projectDir, manifestPath);
    await assertSafeManagedWritePath(
      projectDir,
      path.join(projectDir, RETIRED_SPECIALIZATION_RECEIPT),
    );
    const applicableAssets = managedAssetsForProject(projectConfig);
    for (const asset of applicableAssets) {
      await assertSafeManagedWritePath(projectDir, path.join(projectDir, asset.destination));
      await assertSafeManagedWritePath(
        projectDir,
        path.join(
          projectDir, '.juno_task', 'managed-conflicts', safeVersion(packageVersion),
          `${asset.destination}.candidate`,
        ),
      );
      await assertSafeManagedWritePath(
        projectDir,
        path.join(
          projectDir, '.juno_task', 'managed-conflicts', `bolt-${safeVersion(packageVersion)}`,
          `${asset.destination}.backup`,
        ),
      );
    }

    await this.assertRetiredGenerationSafe(projectDir, manifest, Boolean(options.force));

    // Discover every ordinary managed conflict before changing the installed
    // generation. Candidate files are review aids; installed bytes and the
    // manifest stay untouched until the whole generation is admissible.
    if (!options.force) {
      for (const asset of applicableAssets) {
        const recovered = targetBoundSource(options.recovery, asset);
        const sourcePath = recovered ? null : path.join(templatesDir as string, asset.source);
        if (!recovered && !(await fs.pathExists(sourcePath as string))) {
          throw new Error(`Missing managed package asset: ${sourcePath}`);
        }
        const sourceContent = recovered ?? await fs.readFile(sourcePath as string);
        const destinationPath = path.join(projectDir, asset.destination);
        const record = manifest.assets[asset.destination];
        if (await fs.pathExists(destinationPath)) {
          const currentHash = sha256(await fs.readFile(destinationPath));
          const sourceHash = sha256(sourceContent);
          const generatedSpecialization =
            asset.destination === BOLT_PROMPT &&
            await fs.pathExists(path.join(projectDir, RETIRED_SPECIALIZATION_RECEIPT));
          if (!generatedSpecialization &&
              currentHash !== sourceHash && currentHash !== record?.installedSha256) {
            const candidateRelative = path.join(
              '.juno_task', 'managed-conflicts', safeVersion(packageVersion),
              `${asset.destination}.candidate`,
            );
            await writeAtomic(path.join(projectDir, candidateRelative), sourceContent, projectDir);
            result.conflicts.push({ destination: asset.destination, candidate: candidateRelative });
          }
        }
      }
      const config = projectConfig;
      const global = config?.promptMacros?.global;
      if (global && typeof global === 'object' && !Array.isArray(global)) {
        const mappings = global as Record<string, unknown>;
        for (const [name, mapping] of Object.entries(MANAGED_PROMPT_MACROS)) {
          if (mappings[name] !== undefined && JSON.stringify(mappings[name]) !== JSON.stringify(mapping)) {
            result.macroConflicts.push(name);
          }
        }
      }
      if (result.macroConflicts.length > 0) {
        const candidateConfig = structuredClone(config);
        candidateConfig.promptMacros = candidateConfig.promptMacros ?? {};
        candidateConfig.promptMacros.global = candidateConfig.promptMacros.global ?? {};
        for (const name of result.macroConflicts) {
          candidateConfig.promptMacros.global[name] = MANAGED_PROMPT_MACROS[name];
        }
        const candidateRelative = path.join(
          '.juno_task', 'managed-conflicts', safeVersion(packageVersion),
          '.juno_task/config.json.candidate',
        );
        await writeAtomic(
          path.join(projectDir, candidateRelative),
          `${JSON.stringify(candidateConfig, null, 2)}\n`,
          projectDir,
        );
        result.conflicts.push({
          destination: '.juno_task/config.json',
          candidate: candidateRelative,
        });
      }
      if (result.conflicts.length > 0) return result;
    }

    await this.migrateRetiredGeneration(projectDir, manifest, result, Boolean(options.force));

    // Scripts and project guidance are one migration generation.  Handling only
    // prompts/config here and letting ScriptInstaller overwrite scripts later
    // would bypass checksum conflict detection for customized runtime bytes.
    for (const asset of applicableAssets) {
      const recovered = targetBoundSource(options.recovery, asset);
      const sourcePath = recovered ? null : path.join(templatesDir as string, asset.source);
      if (!recovered && !(await fs.pathExists(sourcePath as string))) {
        throw new Error(`Missing managed package asset: ${sourcePath}`);
      }
      const sourceContent = recovered ?? await fs.readFile(sourcePath as string);
      const sourceHash = sha256(sourceContent);
      const destinationPath = path.join(projectDir, asset.destination);
      const record = manifest.assets[asset.destination];

      if (!(await fs.pathExists(destinationPath))) {
        const specializationReceipt = path.join(
          projectDir,
          '.juno_task',
          'managed-specializations',
          'clean-worktree.json',
        );
        const missingSpecializedPolicy =
          asset.destination === '.juno_task/prompts/clean_worktree.md' &&
          (await fs.pathExists(specializationReceipt));
        if (missingSpecializedPolicy && !options.force) {
          const candidateRelative = path.join(
            '.juno_task',
            'managed-conflicts',
            safeVersion(packageVersion),
            `${asset.destination}.candidate`,
          );
          await writeAtomic(path.join(projectDir, candidateRelative), sourceContent, projectDir);
          result.conflicts.push({ destination: asset.destination, candidate: candidateRelative });
          continue;
        }
        await writeAtomic(destinationPath, sourceContent, projectDir);
        if (asset.installClass === 'script' || asset.type === 'script') {
          await fs.chmod(destinationPath, 0o755);
        }
        result.installed.push(asset.destination);
      } else {
        const currentContent = await fs.readFile(destinationPath);
        const currentHash = sha256(currentContent);
        const safelyManaged = currentHash === sourceHash || currentHash === record?.installedSha256;

        if (currentHash === sourceHash) {
          result.unchanged.push(asset.destination);
        } else if (safelyManaged || options.force) {
          if (options.force && !safelyManaged) {
            await this.archiveRetired(projectDir, asset.destination, currentContent, result);
          }
          await writeAtomic(destinationPath, sourceContent, projectDir);
          if (asset.installClass === 'script' || asset.type === 'script') {
            await fs.chmod(destinationPath, 0o755);
          }
          result.updated.push(asset.destination);
        } else {
          const candidateRelative = path.join(
            '.juno_task',
            'managed-conflicts',
            safeVersion(packageVersion),
            `${asset.destination}.candidate`,
          );
          await writeAtomic(path.join(projectDir, candidateRelative), sourceContent, projectDir);
          result.conflicts.push({ destination: asset.destination, candidate: candidateRelative });
          continue;
        }
      }

      manifest.assets[asset.destination] = {
        type: asset.type,
        templateVersion: options.recovery?.packageVersion ?? packageVersion,
        sourceSha256: sourceHash,
        installedSha256: sourceHash,
      };
    }

    if (!isMetadataOnlyController(projectConfig)) {
      await this.registerPromptMacros(projectDir, result, Boolean(options.force));
    }
    const applicableDestinations = new Set(applicableAssets.map((asset) => asset.destination));
    manifest.assets = Object.fromEntries(
      Object.entries(manifest.assets).filter(([destination]) =>
        applicableDestinations.has(destination)),
    );
    manifest.schemaVersion = 2;
    manifest.packageName = '@yylo/cli';
    manifest.packageVersion = options.recovery?.packageVersion ?? packageVersion;
    manifest.instructionBundle = instructionBundleIdentity(
      manifest.assets, manifest.packageVersion,
    );
    await writeAtomic(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`, projectDir);

    if (!options.silent) {
      console.log(
        `Managed assets: ${result.installed.length} installed, ${result.updated.length} updated, ` +
          `${result.unchanged.length} unchanged, ${result.conflicts.length} conflict(s)`,
      );
      for (const conflict of result.conflicts) {
        console.log(`⚠ Preserved ${conflict.destination}; review ${conflict.candidate}`);
      }
    }
    return result;
  }

  private static async archiveRetired(
    projectDir: string,
    destination: string,
    content: Buffer | string,
    result: ManagedAssetUpdateResult,
  ): Promise<void> {
    const bytes = Buffer.isBuffer(content) ? content : Buffer.from(content);
    const backupRoot = path.join(
      '.juno_task',
      'managed-conflicts',
      `bolt-${safeVersion(packageVersion)}`,
    );
    const baseRelative = path.join(backupRoot, `${destination}.backup`);
    const contentRelative = path.join(
      backupRoot,
      `${destination}.${sha256(bytes).slice(0, 16)}.backup`,
    );

    for (let attempt = 0; ; attempt += 1) {
      const backupRelative =
        attempt === 0
          ? baseRelative
          : attempt === 1
            ? contentRelative
            : `${contentRelative}.${attempt - 1}`;
      const backupPath = path.join(projectDir, backupRelative);
      await assertSafeManagedWritePath(projectDir, backupPath);
      if (await fs.pathExists(backupPath)) {
        if ((await fs.lstat(backupPath)).isSymbolicLink()) {
          throw new Error(`Refusing symbolic-link managed backup: ${backupRelative}`);
        }
        if ((await fs.readFile(backupPath)).equals(bytes)) {
          result.backups.push({ destination, backup: backupRelative });
          return;
        }
        continue;
      }
      await fs.ensureDir(path.dirname(backupPath));
      try {
        await fs.writeFile(backupPath, bytes, { flag: 'wx' });
        result.backups.push({ destination, backup: backupRelative });
        return;
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error;
      }
    }
  }

  /**
   * Install missing controller-class assets on a metadata-only controller.
   *
   * The full generation update is all-or-nothing: one customized tracked
   * asset suspends every install until the owner reviews its candidate.
   * Lifecycle workflow templates and prompts have no other delivery path —
   * a metadata controller cannot run its managed lifecycle until they exist
   * — so absent seeds install directly from the exact package source.
   * Existing bytes are never touched (customization stays authoritative)
   * and the manifest is not rewritten: installed bytes equal the package
   * source and therefore read as `current` to inspectGeneration.
   */
  static async installControllerSeeds(
    projectDir: string,
    options: { silent?: boolean } = {},
  ): Promise<string[]> {
    // Scoped safety only: safe write paths, real package sources, and the
    // metadata-only config shape. Retired-generation and manifest concerns
    // belong to the full generation update; a controller with pre-Bolt
    // history (for example a tracked retired controller-workspace.json)
    // must still receive its missing lifecycle seeds.
    const junoTaskDir = path.join(projectDir, '.juno_task');
    const junoTaskEntry = await lstatIfPresent(junoTaskDir);
    if (!junoTaskEntry) return [];
    await assertSafeManagedWritePath(projectDir, junoTaskDir);
    if (!junoTaskEntry.isDirectory()) {
      throw new Error(`Managed project root is not a directory: ${junoTaskDir}`);
    }
    const projectConfigPath = path.join(junoTaskDir, 'config.json');
    if (!(await fs.pathExists(projectConfigPath))) return [];
    const projectConfig = await fs.readJson(projectConfigPath);
    const controllerWorkspace = projectConfig?.controllerWorkspace;
    const metadataOnlyController =
      controllerWorkspace?.mode === 'metadata-only' &&
      controllerWorkspace?.policy === '.juno_task/config/metadata-controller.json';
    if (projectConfig?.lifecycle !== undefined ||
        (controllerWorkspace !== undefined && !metadataOnlyController)) {
      throw new Error(
        'Legacy Juno 2.0 lifecycle/controllerWorkspace config requires the reviewed 2.1 ' +
          'migration flow. Run `yy migrate inventory`, generate the owner-reviewed policy, ' +
          'then apply and verify `yy migrate evacuation-*` in a disposable worktree before ' +
          'updating managed assets.',
      );
    }
    if (!metadataOnlyController) {
      throw new Error('Controller lifecycle seeds install only on a metadata-only controller');
    }
    const templatesDir = this.getTemplatesDirectory();
    if (!templatesDir) {
      throw new Error('YYLO managed prompt/wiki templates are missing from this package');
    }
    const installed: string[] = [];
    for (const asset of MANAGED_CONTROLLER_ASSETS) {
      const destinationPath = path.join(projectDir, asset.destination);
      if (await lstatIfPresent(destinationPath)) continue;
      const sourcePath = path.join(templatesDir, asset.source);
      await assertPackageSource(sourcePath, templatesDir, 'file');
      await assertSafeManagedWritePath(projectDir, destinationPath);
      if (!options.silent) {
        console.log(`Installing managed controller lifecycle seed: ${asset.destination}`);
      }
      await writeAtomic(destinationPath, await fs.readFile(sourcePath), projectDir);
      installed.push(asset.destination);
    }
    return installed;
  }

  private static async migrateRetiredGeneration(
    projectDir: string,
    manifest: ManagedAssetManifest,
    result: ManagedAssetUpdateResult,
    force: boolean,
  ): Promise<void> {
    await this.assertRetiredGenerationSafe(projectDir, manifest, force);
    for (const destination of RETIRED_BEFORE_BOLT_2_0_32) {
      const destinationPath = path.join(projectDir, destination);
      if (!(await lstatIfPresent(destinationPath))) continue;
      await assertSafeManagedWritePath(projectDir, destinationPath);
      if ((await fs.lstat(destinationPath)).isSymbolicLink()) {
        throw new Error(`Refusing symbolic-link retired managed asset: ${destination}`);
      }
      const content = await fs.readFile(destinationPath);
      await this.archiveRetired(projectDir, destination, content, result);
      await fs.remove(destinationPath);
      delete manifest.assets[destination];
    }

    const receiptPath = path.join(projectDir, RETIRED_SPECIALIZATION_RECEIPT);
    if (!(await fs.pathExists(receiptPath))) return;
    const receiptContent = await fs.readFile(receiptPath);
    let receipt: { promptSha256?: unknown } = {};
    try {
      receipt = JSON.parse(receiptContent.toString('utf8')) as { promptSha256?: unknown };
    } catch {
      // Invalid receipt bytes are customized state and require explicit force.
    }
    const promptPath = path.join(projectDir, BOLT_PROMPT);
    const promptContent = (await fs.pathExists(promptPath)) ? await fs.readFile(promptPath) : null;
    const generated =
      promptContent !== null &&
      typeof receipt.promptSha256 === 'string' &&
      receipt.promptSha256 === sha256(promptContent);
    if (!generated && !force) {
      throw new Error(
        `Refusing to migrate customized retired specialization ${RETIRED_SPECIALIZATION_RECEIPT}; ` +
          'run `yy scripts update --force` to archive it and install the Bolt prompt',
      );
    }
    if (promptContent !== null) {
      await this.archiveRetired(projectDir, BOLT_PROMPT, promptContent, result);
      await fs.remove(promptPath);
    }
    await this.archiveRetired(projectDir, RETIRED_SPECIALIZATION_RECEIPT, receiptContent, result);
    await fs.remove(receiptPath);
    delete manifest.assets[BOLT_PROMPT];
  }

  private static async assertRetiredGenerationSafe(
    projectDir: string,
    manifest: ManagedAssetManifest,
    force: boolean,
  ): Promise<void> {
    for (const destination of RETIRED_BEFORE_BOLT_2_0_32) {
      const destinationPath = path.join(projectDir, destination);
      if (!(await lstatIfPresent(destinationPath))) continue;
      await assertSafeManagedWritePath(projectDir, destinationPath);
      if ((await fs.lstat(destinationPath)).isSymbolicLink()) {
        throw new Error(`Refusing symbolic-link retired managed asset: ${destination}`);
      }
      const content = await fs.readFile(destinationPath);
      const managed = manifest.assets[destination]?.installedSha256 === sha256(content);
      if (!managed && !force) {
        throw new Error(
          `Refusing to migrate customized retired asset ${destination}; ` +
            'run `yy scripts update --force` to archive it and complete the Bolt migration',
        );
      }
    }
    const preflightReceiptPath = path.join(projectDir, RETIRED_SPECIALIZATION_RECEIPT);
    if (await fs.pathExists(preflightReceiptPath)) {
      const preflightReceiptContent = await fs.readFile(preflightReceiptPath);
      let preflightReceipt: { promptSha256?: unknown } = {};
      try {
        preflightReceipt = JSON.parse(preflightReceiptContent.toString('utf8')) as {
          promptSha256?: unknown;
        };
      } catch {
        // Invalid receipt bytes are customized state and require explicit force.
      }
      const preflightPromptPath = path.join(projectDir, BOLT_PROMPT);
      const preflightPromptContent = (await fs.pathExists(preflightPromptPath))
        ? await fs.readFile(preflightPromptPath)
        : null;
      const generated =
        preflightPromptContent !== null &&
        typeof preflightReceipt.promptSha256 === 'string' &&
        preflightReceipt.promptSha256 === sha256(preflightPromptContent);
      if (!generated && !force) {
        throw new Error(
          `Refusing to migrate customized retired specialization ${RETIRED_SPECIALIZATION_RECEIPT}; ` +
            'run `yy scripts update --force` to archive it and install the Bolt prompt',
        );
      }
    }
  }

  /** Inspect the installed lifecycle bundle without changing project files. */
  static async inspectGeneration(
    projectDir: string,
    recovery?: TargetBoundManagedRecovery,
  ): Promise<ManagedAssetGenerationReport> {
    const templatesDir = this.getTemplatesDirectory();
    if (!templatesDir && !recovery) {
      throw new Error('YYLO managed prompt/wiki templates are missing from this package');
    }
    const manifestPath = path.join(projectDir, '.juno_task', 'managed-assets.json');
    let manifest = emptyManifest(recovery?.packageVersion);
    if (await fs.pathExists(manifestPath)) {
      manifest = validateManifest(await fs.readJson(manifestPath), manifestPath);
    }

    let projectConfig: Record<string, any> = {};
    const projectConfigPath = path.join(projectDir, '.juno_task', 'config.json');
    if (await fs.pathExists(projectConfigPath)) projectConfig = await fs.readJson(projectConfigPath);
    const specializationReceipt = path.join(
      projectDir,
      '.juno_task',
      'managed-specializations',
      'clean-worktree.json',
    );
    const retiredSpecializationPresent = await fs.pathExists(specializationReceipt);
    const entries: ManagedAssetGenerationReport['entries'] = [];
    for (const asset of managedAssetsForProject(projectConfig)) {
      const sourceContent = targetBoundSource(recovery, asset) ??
        await fs.readFile(path.join(templatesDir as string, asset.source));
      const sourceHash = sha256(sourceContent);
      const destinationPath = path.join(projectDir, asset.destination);
      let state: ManagedAssetGenerationState;
      if (!(await fs.pathExists(destinationPath))) {
        state = 'missing';
      } else {
        const currentHash = sha256(await fs.readFile(destinationPath));
        const record = manifest.assets[asset.destination];
        if (currentHash === sourceHash) {
          state = 'current';
        } else if (
          asset.destination === '.juno_task/prompts/clean_worktree.md' &&
          (await fs.pathExists(specializationReceipt))
        ) {
          state = 'specialized';
        } else if (record?.installedSha256 === currentHash) {
          state = 'outdated';
        } else {
          state = 'customized';
        }
      }
      entries.push({
        destination: asset.destination,
        installClass: asset.installClass,
        state,
      });
    }

    const scripts = entries.filter((entry) => entry.installClass === 'script');
    const guidance = entries.filter(
      (entry) =>
        ['project', 'controller'].includes(entry.installClass) &&
        entry.destination !== '.juno_task/prompts/clean_worktree.md',
    );
    const cleanPolicy = entries.find(
      (entry) => entry.destination === '.juno_task/prompts/clean_worktree.md',
    );
    const scriptsCurrent = scripts.every((entry) => entry.state === 'current');
    const guidanceCurrent = guidance.every((entry) => entry.state === 'current');
    // A retired specialization receipt can never certify a coherent Bolt generation.
    const cleanCurrent = cleanPolicy?.state === 'current' && !retiredSpecializationPresent;
    const coherent = scriptsCurrent && guidanceCurrent && cleanCurrent;
    const anyMissing = entries.some((entry) => entry.state === 'missing');
    const someScriptsCurrent = scripts.some((entry) => entry.state === 'current');
    const someGuidanceCurrent = guidance.some((entry) => entry.state === 'current');
    const mixed =
      (someScriptsCurrent && !guidanceCurrent) || (someGuidanceCurrent && !scriptsCurrent);
    return {
      status: coherent ? 'coherent' : mixed ? 'mixed' : anyMissing ? 'incomplete' : 'customized',
      coherent,
      instructionBundle: manifest.schemaVersion === 2 ? manifest.instructionBundle ?? null : null,
      entries,
    };
  }

  private static async registerPromptMacros(
    projectDir: string,
    result: ManagedAssetUpdateResult,
    force: boolean,
  ): Promise<void> {
    const configPath = path.join(projectDir, '.juno_task', 'config.json');
    const config = (await fs.pathExists(configPath)) ? await fs.readJson(configPath) : {};
    const original = `${JSON.stringify(config, null, 2)}\n`;
    const promptMacros =
      config.promptMacros &&
      typeof config.promptMacros === 'object' &&
      !Array.isArray(config.promptMacros)
        ? config.promptMacros
        : {};
    const global =
      promptMacros.global &&
      typeof promptMacros.global === 'object' &&
      !Array.isArray(promptMacros.global)
        ? promptMacros.global
        : {};
    let changed = false;

    for (const [name, mapping] of Object.entries(MANAGED_PROMPT_MACROS)) {
      const existing = global[name];
      if (existing === undefined) {
        global[name] = mapping;
        result.macrosAdded.push(name);
        changed = true;
      } else if (JSON.stringify(existing) !== JSON.stringify(mapping)) {
        result.macroConflicts.push(name);
        if (force) {
          global[name] = mapping;
          changed = true;
        }
      }
    }

    if (!force && result.macroConflicts.length > 0) {
      const candidateConfig = structuredClone(config);
      candidateConfig.promptMacros = candidateConfig.promptMacros ?? {};
      candidateConfig.promptMacros.global = candidateConfig.promptMacros.global ?? {};
      for (const name of result.macroConflicts) {
        candidateConfig.promptMacros.global[name] =
          MANAGED_PROMPT_MACROS[name as keyof typeof MANAGED_PROMPT_MACROS];
      }
      const candidateRelative = path.join(
        '.juno_task',
        'managed-conflicts',
        safeVersion(packageVersion),
        '.juno_task/config.json.candidate',
      );
      await writeAtomic(
        path.join(projectDir, candidateRelative),
        `${JSON.stringify(candidateConfig, null, 2)}\n`,
        projectDir,
      );
      result.conflicts.push({
        destination: '.juno_task/config.json',
        candidate: candidateRelative,
      });
    }

    if (!changed) return;
    promptMacros.global = global;
    config.promptMacros = promptMacros;

    if (force && result.macroConflicts.length > 0) {
      await this.archiveRetired(
        projectDir,
        '.juno_task/config.json',
        original,
        result,
      );
    }
    await writeAtomic(configPath, `${JSON.stringify(config, null, 2)}\n`, projectDir);
  }
}
