import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import * as path from 'node:path';
import { fileURLToPath } from 'node:url';
import fs from 'fs-extra';
import managedAssetManifest from '../templates/managed-assets.json';
import { version as packageVersion } from '../version.js';
import {
  assertInstructionVersion, instructionDeclarationCompatible, INSTRUCTION_IDENTITY_SCHEMA,
} from './instruction-bundle-compatibility.js';
import type { TargetBoundManagedRecovery } from './managed-controller-recovery.js';
import { assertControllerGenerationReady, withControllerGenerationMutation } from './controller-generation-migration.js';
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

if (!instructionDeclarationCompatible(managedAssetManifest.schemaVersion, managedAssetManifest.instructionBundle)) {
  throw new Error('instruction_bundle_incompatible: package declaration requires a compatible CLI');
}
const INSTRUCTION_BUNDLE_DECLARATION =
  managedAssetManifest.instructionBundle as InstructionBundleDeclaration;
// Wiki rows are retained solely as legacy migration mappings, not file outputs.
const MANAGED_ASSET_DEFINITIONS = (managedAssetManifest.assets as ManagedAssetDefinition[])
  .filter((asset) => asset.type !== 'wiki');
const MANAGED_CONTROLLER_OUTPUTS = (
  managedAssetManifest.controllerOutputs as ManagedControllerOutputDefinition[]
).filter((asset) => asset.type !== 'wiki')
  .map((asset): ManagedAssetDefinition => ({ ...asset, installClass: 'controller' }));

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

const LOCALIZED_CONFIGS = new Set([
  '.juno_task/config/task-workspace.json',
  '.juno_task/config/metadata-controller.json',
  '.juno_task/config/worktree-hydration.yaml',
]);

export interface ManagedProjectLocalization {
  targetBranch?: string;
  gitRemoteUrl?: string;
}

function gitValue(projectDir: string, args: string[]): string | undefined {
  const result = spawnSync('git', ['-C', projectDir, ...args], { encoding: 'utf8' });
  return result.status === 0 && result.stdout.trim() ? result.stdout.trim() : undefined;
}

function normalizedBranch(value: string | undefined): string {
  const branch = (value || '').trim().replace(/^refs\/heads\//, '');
  if (!branch || branch.startsWith('-') || branch.includes('..') || /[~^:?*[\\\s]/.test(branch)) {
    return 'main';
  }
  return branch;
}

async function isYyloMonorepo(projectDir: string): Promise<boolean> {
  try {
    const manifest = await fs.readJson(path.join(projectDir, 'juno-code/package.json'));
    return manifest?.name === '@yylo/cli';
  } catch {
    return false;
  }
}

function discoverLocalization(
  projectDir: string,
  overrides: ManagedProjectLocalization = {},
): Required<ManagedProjectLocalization> {
  const remoteHead = gitValue(projectDir, ['symbolic-ref', '--quiet', '--short', 'refs/remotes/origin/HEAD'])
    ?.replace(/^origin\//, '');
  const currentBranch = gitValue(projectDir, ['branch', '--show-current']);
  return {
    targetBranch: normalizedBranch(
      overrides.targetBranch || process.env.YYLO_TARGET_BRANCH || remoteHead || currentBranch,
    ),
    gitRemoteUrl: (
      overrides.gitRemoteUrl || process.env.JUNO_TASK_GIT_URL ||
      gitValue(projectDir, ['remote', 'get-url', 'origin']) || ''
    ).trim(),
  };
}

async function localizedManagedSource(
  projectDir: string,
  asset: ManagedAssetDefinition,
  sourceContent: Buffer,
  localization: ManagedProjectLocalization = {},
): Promise<Buffer> {
  if (!LOCALIZED_CONFIGS.has(asset.destination) || await isYyloMonorepo(projectDir)) {
    return sourceContent;
  }
  const local = discoverLocalization(projectDir, localization);
  if (asset.destination === '.juno_task/config/worktree-hydration.yaml') {
    return Buffer.from(
      'schema_version: v1\n' +
      'workflow_id: worktree-hydration\n' +
      'workflow_class: task_hydration\n' +
      'steps:\n' +
      '  - id: clean_tree\n' +
      '    name: Prove the consumer worktree is clean\n' +
      '    probe: ["python3", ".juno_task/scripts/worktree_hydration.py", "--project-root", ".", "verify-clean"]\n' +
      '    command: ["python3", ".juno_task/scripts/worktree_hydration.py", "--project-root", ".", "verify-clean"]\n' +
      '    timeout_seconds: 30\n' +
      '    fail_workflow: true\n' +
      '    non_interactive: true\n' +
      '    network: false\n' +
      '    sensitive: false\n' +
      '    outputs: []\n',
    );
  }
  const value = JSON.parse(sourceContent.toString('utf8')) as Record<string, any>;
  if (asset.destination === '.juno_task/config/metadata-controller.json') {
    value.controller_branch = 'refs/heads/juno/controller-metadata';
    value.product_ref = `refs/heads/${local.targetBranch}`;
  } else {
    value.target_ref = `refs/heads/${local.targetBranch}`;
    value.workspace_root = '@state/yylo/task-worktrees';
    value.allowed_paths = [
      '.gitignore', '.juno_task/config', '.juno_task/managed-assets.json',
      '.juno_task/prompts', '.juno_task/wiki', 'AGENTS.md', 'CLAUDE.md',
      'README.md', 'docs', 'src', 'tests',
    ];
    value.selectable_paths = [];
    value.focused_validation = [{
      id: 'consumer-policy-canary', cwd: '.juno_task', timeout_seconds: 30,
      max_output_bytes: 16384,
      argv: ['python3', '-c', "import json; json.load(open('config/task-workspace.json'))"],
    }];
    value.full_suite_validation = {
      id: 'consumer-full-suite-canary', cwd: '.juno_task', timeout_seconds: 30,
      max_output_bytes: 16384,
      argv: ['python3', '-c', "import json; json.load(open('config/task-workspace.json'))"],
    };
    delete value.validation_profiles;
    value.documentation_validation = {
      ...value.documentation_validation,
      inert_exact_files: ['AGENTS.md', 'CLAUDE.md'],
      inert_roots: ['.juno_task/wiki'],
      active_exact_files: ['README.md'],
      active_roots: ['docs'],
      public_identities: local.gitRemoteUrl ? [local.gitRemoteUrl] : [],
    };
  }
  return Buffer.from(`${JSON.stringify(value, null, 2)}\n`);
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
  assertInstructionVersion(INSTRUCTION_BUNDLE_DECLARATION.semanticVersion);
  const core = {
    schemaVersion: 'juno_instruction_bundle.v1' as const,
    semanticVersion: INSTRUCTION_BUNDLE_DECLARATION.semanticVersion,
    packageVersion: identityVersion,
    assetCount: Object.keys(assets).length,
    assetsSha256: managedAssetRecordsIdentity(assets),
  };
  return { ...core, bundleSha256: sha256(JSON.stringify(core)) };
}

export function validateManifest(manifest: unknown, manifestPath: string): ManagedAssetManifest {
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
    assertInstructionVersion(identity?.semanticVersion);
    if (identity?.schemaVersion !== INSTRUCTION_IDENTITY_SCHEMA ||
        Object.keys(identity).sort().join(',') !== 'assetCount,assetsSha256,bundleSha256,packageVersion,schemaVersion,semanticVersion' ||
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
  /** Explicit native-publication boundary, separately exercised by real-Git/ Ledger tests. */
  static stagePackageWiki(projectDir: string, templatesDir: string): string {
    const helper = path.join(templatesDir, 'maintenance/package_wiki_publication.py');
    const publication = spawnSync('python3', ['-I', '-B', helper, '--bootstrap', projectDir,
      '--package-root', path.resolve(templatesDir, '../..')], {
      encoding: 'utf8', timeout: 90_000, maxBuffer: 1024 * 1024,
    });
    if (publication.error || publication.status !== 0) {
      throw new Error('package_wiki_bootstrap_refused: preserve controller bytes; use an authenticated ' +
        'release and compatible Ledger, or the generation migration for an existing binding');
    }
    return `${JSON.stringify(JSON.parse(publication.stdout), null, 2)}\n`;
  }

  static verifyPackageWiki(projectDir: string, templatesDir: string): boolean {
    const result = spawnSync('python3', ['-I', '-B', path.join(templatesDir, 'maintenance/package_wiki_publication.py'),
      '--verify-binding', projectDir, '--package-root', path.resolve(templatesDir, '../..')], {
      encoding: 'utf8', timeout: 90_000, maxBuffer: 1024 * 1024,
    });
    return !result.error && result.status === 0;
  }

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
    await assertControllerGenerationReady(projectDir);
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

  static async update(projectDir: string,
    options: Parameters<typeof ManagedProjectAssets.updateUnlocked>[1] = {},
  ): Promise<ManagedAssetUpdateResult> {
    return withControllerGenerationMutation(projectDir, () => this.updateUnlocked(projectDir, options));
  }

  private static async updateUnlocked(
    projectDir: string,
    options: {
      force?: boolean;
      silent?: boolean;
      recovery?: TargetBoundManagedRecovery | undefined;
      localization?: ManagedProjectLocalization | undefined;
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
        const packageContent = recovered ?? await fs.readFile(sourcePath as string);
        const sourceContent = recovered
          ? packageContent
          : await localizedManagedSource(projectDir, asset, packageContent, options.localization);
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

    let packageWikiBinding: string | undefined;
    if (isMetadataOnlyController(projectConfig)) {
      await assertSafeManagedWritePath(projectDir, path.join(projectDir, '.juno_task/config/package-wiki.json'));
      packageWikiBinding = this.stagePackageWiki(projectDir, templatesDir as string);
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
      const packageContent = recovered ?? await fs.readFile(sourcePath as string);
      const sourceContent = recovered
        ? packageContent
        : await localizedManagedSource(projectDir, asset, packageContent, options.localization);
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
    if (packageWikiBinding !== undefined) {
      const bindingRelative = '.juno_task/config/package-wiki.json';
      const bindingPath = path.join(projectDir, bindingRelative);
      await writeAtomic(bindingPath, packageWikiBinding, projectDir);
      const identity = sha256(packageWikiBinding);
      manifest.assets[bindingRelative] = { type: 'config', templateVersion: packageVersion,
        sourceSha256: identity, installedSha256: identity };
    }
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
  static async installControllerSeeds(projectDir: string, options: { silent?: boolean } = {}): Promise<string[]> {
    return withControllerGenerationMutation(projectDir, () => this.installControllerSeedsUnlocked(projectDir, options));
  }

  private static async installControllerSeedsUnlocked(
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
      const recovered = targetBoundSource(recovery, asset);
      const packageContent = recovered ??
        await fs.readFile(path.join(templatesDir as string, asset.source));
      const sourceContent = recovered
        ? packageContent
        : await localizedManagedSource(projectDir, asset, packageContent);
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

    if (isMetadataOnlyController(projectConfig)) {
      const destination = '.juno_task/config/package-wiki.json';
      const bindingPath = path.join(projectDir, destination);
      const present = await fs.pathExists(bindingPath);
      const record = manifest.assets[destination];
      const current = present && record?.installedSha256 === sha256(await fs.readFile(bindingPath)) &&
        this.verifyPackageWiki(projectDir, templatesDir as string);
      entries.push({ destination, installClass: 'controller', state: current ? 'current' : present ? 'customized' : 'missing' });
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
