/**
 * Script Installer Utility
 * Handles automatic installation of project-level scripts from templates
 *
 * Unlike ServiceInstaller (which installs to ~/.yylo/services/),
 * this installer manages scripts in the project's .juno_task/scripts/ directory.
 */

import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import fs from 'fs-extra';
import * as path from 'node:path';
import { fileURLToPath } from 'node:url';
import managedAssetManifest from '../templates/managed-assets.json';
import { assertControllerGenerationReady, withControllerGenerationMutation } from './controller-generation-migration.js';
import {
  resolveTargetBoundManagedRecovery,
  type ManagedControllerGenerationReceipt,
  type TargetBoundManagedRecovery,
} from './managed-controller-recovery.js';
import {
  assertPackageSource,
  assertSafeManagedWritePath,
  lstatIfPresent,
} from './managed-update-transaction.js';

const MANAGED_SCRIPT_ROOT = '.juno_task/scripts';
const MANAGED_SCRIPT_NAMES = managedAssetManifest.assets
  .filter((asset) => asset.installClass === 'script')
  .map((asset) => path.relative(MANAGED_SCRIPT_ROOT, asset.destination))
  .map((scriptName) => {
    if (!scriptName || scriptName.startsWith('..') || path.isAbsolute(scriptName)) {
      throw new Error('Managed script destinations must stay under .juno_task/scripts');
    }
    return scriptName;
  });

const COHERENCE_BLOCKING_MANAGED_ASSETS = new Set(
  managedAssetManifest.assets
    .map((asset) => asset.destination),
);
const MANAGED_CONTROLLER_GENERATION = path.join(
  '.juno_task', 'runtime', 'managed-controller', 'generation.json',
);
const CONTROLLER_POLICY_PATHS = [
  '.juno_task/config/metadata-controller.json',
  '.juno_task/config/task-workspace.json',
  '.juno_task/config/integration-workspace.json',
  '.juno_task/config/risk-policy.json',
] as const;
const CONTROLLER_BUNDLE_TRACKED_PATHS = [
  '.juno_task/managed-assets.json',
  ...[...managedAssetManifest.assets, ...managedAssetManifest.controllerOutputs]
    .map((asset) => asset.destination)
    .filter((destination) => /^\.juno_task\/(prompts|wiki|workflows)\//.test(destination)),
].sort();

type ManagedControllerGeneration = ManagedControllerGenerationReceipt;

export class ScriptInstaller {
  static async isMetadataOnlyController(projectDir: string): Promise<boolean> {
    try {
      const config = await fs.readJson(path.join(projectDir, '.juno_task/config.json'));
      return config?.controllerWorkspace?.mode === 'metadata-only'
        && config.controllerWorkspace.policy === '.juno_task/config/metadata-controller.json';
    } catch {
      return false;
    }
  }

  static async inspectManagedControllerGeneration(projectDir: string): Promise<{
    present: boolean; healthy: boolean; packageVersion: string | null; targetSha: string | null;
    findings: string[];
  }> {
    const generationPath = path.join(projectDir, MANAGED_CONTROLLER_GENERATION);
    if (!(await fs.pathExists(generationPath))) {
      return { present: false, healthy: false, packageVersion: null, targetSha: null,
        findings: ['generation receipt is missing'] };
    }
    try {
      const generation = await fs.readJson(generationPath) as ManagedControllerGeneration;
      const findings: string[] = [];
      if (generation.schema_version !== 'juno_managed_controller_runtime.v1' ||
          !/^[0-9a-f]{40,64}$/.test(generation.target_sha) ||
          typeof generation.package_version !== 'string' ||
          !generation.scripts || typeof generation.scripts !== 'object') {
        findings.push('generation receipt identity is invalid');
      } else {
        for (const [relative, binding] of Object.entries(generation.scripts)) {
          const destination = path.resolve(projectDir, relative);
          const scriptRoot = path.resolve(projectDir, MANAGED_SCRIPT_ROOT);
          if (!destination.startsWith(`${scriptRoot}${path.sep}`) ||
              !/^[0-9a-f]{64}$/.test(binding?.source_sha256 ?? '') ||
              !/^[0-9a-f]{64}$/.test(binding?.actual_sha256 ?? '') ||
              !['exact', 'preserved_customization'].includes(binding?.classification) ||
              (binding.classification === 'exact' && binding.actual_sha256 !== binding.source_sha256) ||
              (binding.classification === 'preserved_customization' && binding.actual_sha256 === binding.source_sha256)) {
            findings.push(`${relative}: invalid binding`);
            continue;
          }
          if (!(await fs.pathExists(destination))) {
            findings.push(`${relative}: missing`);
            continue;
          }
          const actual = createHash('sha256').update(await fs.readFile(destination)).digest('hex');
          if (actual !== binding.actual_sha256) findings.push(`${relative}: drift`);
        }
      }
      return { present: true, healthy: findings.length === 0,
        packageVersion: typeof generation.package_version === 'string' ? generation.package_version : null,
        targetSha: typeof generation.target_sha === 'string' ? generation.target_sha : null,
        findings };
    } catch (error) {
      return { present: true, healthy: false, packageVersion: null, targetSha: null,
        findings: [`generation receipt is invalid: ${String(error)}`] };
    }
  }

  /**
   * A post-integration generation is Git/receipt-owned, not package-owned.
   * Generic package installation may repair it only when package source is the
   * exact bound source and no owner customization is present. In particular an
   * older globally installed yy must not silently roll ignored scripts back.
   */
  static async assertManagedControllerPackageUpdateAllowed(
    projectDir: string,
  ): Promise<TargetBoundManagedRecovery | null> {
    await assertControllerGenerationReady(projectDir);
    if (!(await this.isMetadataOnlyController(projectDir))) return null;
    const generationPath = path.join(projectDir, MANAGED_CONTROLLER_GENERATION);
    if (!(await fs.pathExists(generationPath))) return null;

    let generation: ManagedControllerGeneration;
    try {
      generation = await fs.readJson(generationPath) as ManagedControllerGeneration;
    } catch (error) {
      throw new Error(`Managed controller generation receipt is invalid: ${String(error)}`);
    }
    if (
      generation.schema_version !== 'juno_managed_controller_runtime.v1' ||
      !/^[0-9a-f]{40,64}$/.test(generation.target_sha) ||
      typeof generation.package_version !== 'string' ||
      !generation.scripts || typeof generation.scripts !== 'object'
    ) {
      throw new Error(`Managed controller generation receipt is invalid: ${generationPath}`);
    }

    const packageScriptsDir = this.getPackageScriptsDir();
    if (!packageScriptsDir) throw new Error('YYLO package scripts are missing');
    const mismatches: string[] = [];
    for (const [relative, binding] of Object.entries(generation.scripts)) {
      const scriptName = path.relative(MANAGED_SCRIPT_ROOT, relative);
      const sourcePath = path.join(packageScriptsDir, scriptName);
      if (
        scriptName.startsWith('..') || path.isAbsolute(scriptName) ||
        binding?.classification !== 'exact' ||
        !(await fs.pathExists(sourcePath))
      ) {
        mismatches.push(relative);
        continue;
      }
      const packageHash = createHash('sha256').update(await fs.readFile(sourcePath)).digest('hex');
      if (packageHash !== binding.source_sha256) mismatches.push(relative);
    }
    if (mismatches.length === 0) return null;
    const sample = mismatches.slice(0, 3).join(', ');
    try {
      return await resolveTargetBoundManagedRecovery(
        projectDir, generation, packageScriptsDir,
      );
    } catch (recoveryError) {
      throw new Error(
        `Refusing package script update: controller runtime is receipt-bound to target ${generation.target_sha} ` +
        `(package ${generation.package_version}), but the invoked package source is not that exact generation` +
        `${sample ? `; non-package bindings: ${sample}` : ''}. ` +
        `No exact target-bound recovery provenance was admitted: ${String(recoveryError)}. ` +
        'Preserved the exact target generation. Inspect `yy scripts generation doctor` for the shared ' +
        'read-only assessment; only authenticated generation migration or a separately admitted maintenance plan may change it.',
        { cause: recoveryError },
      );
    }
  }

  /**
   * Materialize the project-class policy subset owned by a sparse metadata
   * controller. Existing reviewed policy bytes are never replaced with package
   * defaults. The one structural migration needed by pre-2.1.2 controllers is
   * explicit-force only and archives the original bytes in ignored runtime state.
   */
  static async updateMetadataControllerPolicies(projectDir: string, force = false, apply = false):
    Promise<{ installed: string[]; updated: string[]; backups: string[] }> {
    return withControllerGenerationMutation(projectDir, () => this.updateMetadataControllerPoliciesUnlocked(projectDir, force, apply));
  }

  private static async updateMetadataControllerPoliciesUnlocked(
    projectDir: string,
    force = false,
    apply = false,
  ): Promise<{ installed: string[]; updated: string[]; backups: string[] }> {
    if (!(await this.isMetadataOnlyController(projectDir))) {
      return { installed: [], updated: [], backups: [] };
    }
    const packageScriptsDir = this.getPackageScriptsDir();
    if (!packageScriptsDir) throw new Error('YYLO package scripts are missing');
    const packageConfigDir = path.join(path.dirname(packageScriptsDir), 'config');
    await assertPackageSource(packageConfigDir, path.dirname(packageScriptsDir), 'directory');

    const metadataRelative = CONTROLLER_POLICY_PATHS[0];
    const metadataPath = path.join(projectDir, metadataRelative);
    if (!(await fs.pathExists(metadataPath))) {
      throw new Error(
        'Metadata-controller identity policy is missing; a package default cannot reconstruct reviewed controller and product refs',
      );
    }
    await assertSafeManagedWritePath(projectDir, metadataPath);
    const originalMetadata = await fs.readFile(metadataPath);
    let metadataPolicy: Record<string, unknown>;
    try {
      metadataPolicy = JSON.parse(originalMetadata.toString('utf8')) as Record<string, unknown>;
    } catch (error) {
      throw new Error(`Metadata-controller identity policy is invalid: ${String(error)}`);
    }
    if (metadataPolicy.schema_version !== 'juno_metadata_controller_policy.v1' ||
        !Array.isArray(metadataPolicy.generated_metadata) ||
        !metadataPolicy.generated_metadata.every((entry) => typeof entry === 'string') ||
        !Array.isArray(metadataPolicy.tracked_exact) ||
        !metadataPolicy.tracked_exact.every((entry) => typeof entry === 'string')) {
      throw new Error('Metadata-controller identity policy is invalid or predates the supported migration shape');
    }
    const generatedMetadata = metadataPolicy.generated_metadata as string[];
    const trackedExact = metadataPolicy.tracked_exact as string[];
    const missingClassifications = CONTROLLER_POLICY_PATHS.filter((relative) =>
      !generatedMetadata.includes(relative) || !trackedExact.includes(relative),
    );
    void force;
    if (missingClassifications.length > 0) {
      const exactLegacy = missingClassifications.length === 1
        && missingClassifications[0] === '.juno_task/config/integration-workspace.json';
      if (exactLegacy) {
        throw new Error(
          'Metadata-controller policy requires the receipt-bound legacy migration; scripts update is mutation-free for tracked policy. ' +
          'Run `yy migrate metadata-policy plan --root "$PWD" --output /external/metadata-policy-plan.json`, ' +
          'review it, then run `yy migrate metadata-policy apply --plan /external/metadata-policy-plan.json ' +
          '--output /external/metadata-policy-apply.json --authorize-metadata-policy-migration`.',
        );
      }
      throw new Error(
        `Metadata-controller policy has unsupported managed classification gaps: ${missingClassifications.join(', ')}. ` +
        'Preserved tracked policy bytes for owner review.',
      );
    }
    const missingEndpoints = CONTROLLER_POLICY_PATHS.filter((relative) =>
      !fs.existsSync(path.join(projectDir, relative)),
    );
    if (missingEndpoints.length > 0) {
      throw new Error(
        `Metadata-controller tracked policy endpoints are missing: ${missingEndpoints.join(', ')}. ` +
        'Generic scripts update will not recreate tracked controller policy.',
      );
    }

    const missingBundleClassifications = CONTROLLER_BUNDLE_TRACKED_PATHS.filter((relative) =>
      !generatedMetadata.includes(relative) || !trackedExact.includes(relative),
    );
    if (missingBundleClassifications.length === 0 || !apply) {
      return { installed: [], updated: [], backups: [] };
    }

    // This narrowly versioned migration admits only package-declared controller
    // outputs. It does not broaden ownership to arbitrary prompt/wiki trees and
    // does not alter controller/product refs or any other policy field.
    metadataPolicy.generated_metadata = [...new Set([
      ...generatedMetadata, ...missingBundleClassifications,
    ])].sort();
    metadataPolicy.tracked_exact = [...new Set([
      ...trackedExact, ...missingBundleClassifications,
    ])].sort();
    const backupRoot = path.join(
      projectDir, '.juno_task/runtime/managed-controller/policy-backups',
    );
    const originalHash = createHash('sha256').update(originalMetadata).digest('hex');
    const backupPath = path.join(backupRoot, `metadata-controller.${originalHash}.json`);
    await assertSafeManagedWritePath(projectDir, backupPath);
    await fs.ensureDir(backupRoot);
    if (!(await fs.pathExists(backupPath))) {
      await fs.writeFile(backupPath, originalMetadata, { flag: 'wx' });
    } else if (!(await fs.readFile(backupPath)).equals(originalMetadata)) {
      throw new Error(`Metadata-controller policy backup collision: ${backupPath}`);
    }
    const replacement = `${JSON.stringify(metadataPolicy, null, 2)}\n`;
    const temporary = `${metadataPath}.tmp-${process.pid}-${Date.now()}`;
    await fs.writeFile(temporary, replacement);
    await fs.rename(temporary, metadataPath);
    return {
      installed: [],
      updated: [metadataRelative],
      backups: [path.relative(projectDir, backupPath)],
    };
  }

  /** Refuse a success message until sparse policy and routed runtime parity are proven. */
  static async assertMetadataControllerUpdateComplete(
    projectDir: string,
    recovery?: TargetBoundManagedRecovery,
  ): Promise<void> {
    if (!(await this.isMetadataOnlyController(projectDir))) return;
    const metadataPath = path.join(projectDir, CONTROLLER_POLICY_PATHS[0]);
    const metadata = await fs.readJson(metadataPath).catch((error) => {
      throw new Error(`Updated metadata-controller policy is unreadable: ${String(error)}`);
    }) as Record<string, unknown>;
    const generatedMetadata = Array.isArray(metadata.generated_metadata)
      ? metadata.generated_metadata : [];
    const trackedExact = Array.isArray(metadata.tracked_exact) ? metadata.tracked_exact : [];
    for (const relative of CONTROLLER_POLICY_PATHS) {
      if (!generatedMetadata.includes(relative) || !trackedExact.includes(relative)) {
        throw new Error(`Updated metadata-controller policy still omits required asset: ${relative}`);
      }
      const destination = path.join(projectDir, relative);
      if (!(await fs.pathExists(destination))) {
        throw new Error(`Required metadata-controller managed asset is missing: ${relative}`);
      }
      await fs.readJson(destination).catch((error) => {
        throw new Error(`Required metadata-controller managed asset is invalid: ${relative}: ${String(error)}`);
      });
    }
    for (const relative of CONTROLLER_BUNDLE_TRACKED_PATHS) {
      if (!generatedMetadata.includes(relative) || !trackedExact.includes(relative)) {
        throw new Error(
          `Updated metadata-controller policy still omits managed bundle output: ${relative}`,
        );
      }
    }
    const { ManagedProjectAssets } = await import('./managed-project-assets.js');
    const bundle = await ManagedProjectAssets.inspectGeneration(projectDir, recovery);
    if (!bundle.coherent || !bundle.instructionBundle ||
        (recovery && bundle.instructionBundle.packageVersion !== recovery.packageVersion)) {
      throw new Error(
        `Metadata-controller instruction bundle readback is ${bundle.status}; ` +
        'the complete schema-2 receipt was not persisted',
      );
    }
    if (recovery) {
      const generationReceipt = await fs.readJson(
        path.join(projectDir, MANAGED_CONTROLLER_GENERATION),
      ) as ManagedControllerGeneration;
      const packageScriptsDir = this.getPackageScriptsDir();
      if (!packageScriptsDir) throw new Error('YYLO package scripts are missing during readback');
      const persistedRecovery = await resolveTargetBoundManagedRecovery(
        projectDir, generationReceipt, packageScriptsDir,
      );
      if (persistedRecovery.targetSha !== recovery.targetSha ||
          persistedRecovery.packageVersion !== recovery.packageVersion ||
          persistedRecovery.generationSha256 !== recovery.generationSha256 ||
          persistedRecovery.generationPolicySha256 !== recovery.generationPolicySha256 ||
          persistedRecovery.policySha256 !== recovery.policySha256 ||
          persistedRecovery.checkpointSha256 !== recovery.checkpointSha256 ||
          persistedRecovery.packageIdentitySha256 !== recovery.packageIdentitySha256 ||
          persistedRecovery.assets.size !== recovery.assets.size) {
        throw new Error('Recovered target-bound package identity changed before readback');
      }
      const manifest = await fs.readJson(
        path.join(projectDir, '.juno_task/managed-assets.json'),
      ) as Record<string, any>;
      if (manifest.schemaVersion !== 2 || manifest.packageName !== '@yylo/cli' ||
          manifest.packageVersion !== recovery.packageVersion ||
          Object.keys(manifest.assets ?? {}).length !== recovery.assets.size) {
        throw new Error('Recovered target-bound managed inventory is incomplete');
      }
      for (const [relative, expected] of recovery.assets) {
        const persistedExpected = persistedRecovery.assets.get(relative);
        const actual = await fs.readFile(path.join(projectDir, relative)).catch((error) => {
          throw new Error(`Recovered target-bound asset is unreadable: ${relative}`, {
            cause: error,
          });
        });
        const expectedHash = createHash('sha256').update(expected).digest('hex');
        const record = manifest.assets[relative];
        if (!persistedExpected?.equals(expected) || !actual.equals(expected) ||
            record?.templateVersion !== recovery.packageVersion ||
            record?.sourceSha256 !== expectedHash || record?.installedSha256 !== expectedHash) {
          throw new Error(`Recovered target-bound asset has mixed persisted identity: ${relative}`);
        }
      }
    }
    const checkpointInclude = await fs.readJson(
      path.join(projectDir, '.juno_task/config.json'),
    ).then((config) => config?.gitCheckpoint?.include).catch(() => null) as unknown;
    if (recovery && (!Array.isArray(checkpointInclude) ||
        !checkpointInclude.every((entry) => typeof entry === 'string') ||
        CONTROLLER_BUNDLE_TRACKED_PATHS.some((relative) =>
          !(checkpointInclude as string[]).some((root) =>
            relative === root || relative.startsWith(`${root.replace(/\/$/, '')}/`))))) {
      throw new Error('Metadata-controller checkpoint policy omits managed bundle outputs');
    }
    const generation = await this.inspectManagedControllerGeneration(projectDir);
    if (generation.present) {
      if (!generation.healthy) {
        throw new Error(
          `Receipt-bound metadata-controller runtime remains unhealthy: ${generation.findings.join('; ')}`,
        );
      }
    } else {
      const missing = await this.getMissingScripts(projectDir);
      const outdated = await this.getOutdatedScripts(projectDir);
      if (missing.length > 0 || outdated.length > 0) {
        throw new Error(
          `Metadata-controller routed scripts remain incomplete after update` +
          `${missing.length ? `; missing: ${missing.join(', ')}` : ''}` +
          `${outdated.length ? `; outdated: ${outdated.join(', ')}` : ''}`,
        );
      }
    }
    const integrationRuntime = path.join(
      projectDir, '.juno_task', 'scripts', 'integration_workspace.py',
    );
    const runtime = await fs.readFile(integrationRuntime, 'utf8');
    const routedMarkers: Record<string, string> = {
      status: 'add_parser("status"',
      repair: 'for name in ("repair", "push")',
      'runtime-doctor': 'add_parser("runtime-doctor"',
      'runtime-refresh': 'add_parser("runtime-refresh"',
    };
    for (const [operation, marker] of Object.entries(routedMarkers)) {
      if (!runtime.includes(marker)) {
        throw new Error(`Updated integration runtime does not route advertised command: ${operation}`);
      }
    }
  }

  /**
   * Controller-class managed assets (lifecycle workflow templates and
   * lifecycle prompts) exist only on metadata-only controllers. They have no
   * install surface of their own: this installer is the delivery path, so an
   * absent seed is a missing install, not a customization to preserve.
   */
  private static async missingManagedControllerSeeds(projectDir: string): Promise<string[]> {
    const missing: string[] = [];
    for (const asset of managedAssetManifest.assets) {
      if (asset.installClass !== 'controller') continue;
      if (!(await lstatIfPresent(path.join(projectDir, asset.destination)))) {
        missing.push(asset.destination);
      }
    }
    return missing;
  }

  /**
   * Install controller-class managed seeds on a metadata-only controller.
   * Scoped seed installation never forwards force and never rewrites the
   * tracked generation: owner-reviewed assets that diverge from package
   * bytes stay authoritative until the full generation update is admissible.
   */
  private static async installManagedControllerSeeds(
    projectDir: string,
    silent: boolean,
  ): Promise<string[]> {
    const { ManagedProjectAssets } = await import('./managed-project-assets.js');
    return ManagedProjectAssets.installControllerSeeds(projectDir, { silent });
  }

  /**
   * Update lifecycle guidance before installing a new lifecycle script generation.
   * A specialized clean_worktree policy is intentionally exempt; any other
   * customized lifecycle prompt/wiki must be reviewed or force-backed-up first.
   */
  private static async prepareManagedLifecycleBundle(
    projectDir: string,
    silent: boolean,
    force: boolean,
  ): Promise<boolean> {
    const { ManagedProjectAssets } = await import('./managed-project-assets.js');
    const assets = await ManagedProjectAssets.update(projectDir, { force, silent });
    const blocking = assets.conflicts.filter((conflict) =>
      COHERENCE_BLOCKING_MANAGED_ASSETS.has(conflict.destination),
    );
    if (blocking.length === 0) return true;

    if (!silent || process.env.YYLO_DEBUG === '1') {
      console.error(
        '⚠ Lifecycle scripts were not updated because managed guidance has unresolved conflicts:',
      );
      for (const conflict of blocking) {
        console.error(`  ${conflict.destination} (candidate: ${conflict.candidate})`);
      }
      console.error('  Review the candidates, then run: yy scripts update --force');
    }
    return false;
  }

  /**
   * Scripts that should be auto-installed if missing
   * These are critical scripts that users expect to be available
   */
  /**
   * Required scripts include both standalone scripts and their dependencies.
   * kanban.sh depends on install_requirements.sh for Python venv setup.
   * Slack integration scripts allow fetching tasks from Slack and responding.
   * Hook scripts are stored in the hooks/ subdirectory.
   */
  private static readonly REQUIRED_SCRIPTS = [...new Set([
    'run_until_completion.sh',
    'kanban.sh',
    'juno-toolchain-policy.sh', // Kanban >=2,<3 runtime identity SOT used by kanban.sh
    'controller_resolver.py', // Shared canonical controller/workspace-role resolver
    'orchestration_guard.py', // Cron/workflow singleton and controller-role ownership guard
    'install_requirements.sh', // Required by kanban.sh for Python venv creation
    // Shared utilities
    'attachment_downloader.py', // File attachment downloading utility (used by Slack/GitHub)
    // Slack integration scripts
    'slack_state.py', // State management for Slack integration
    'slack_fetch.py', // Core logic for fetching Slack messages
    'slack_fetch.sh', // Wrapper script for Slack fetch
    'slack_respond.py', // Core logic for sending responses to Slack
    'slack_respond.sh', // Wrapper script for Slack respond
    // GitHub integration script (single-file architecture)
    'github.py', // Unified GitHub integration (fetch, respond, sync)
    // Claude Code hooks (stored in hooks/ subdirectory)
    'hooks/session_counter.sh', // Session message counter hook for warning about long sessions
    // Log scanning utility
    'log_scanner.sh', // Scans log files for errors/exceptions and creates kanban bug reports
    // Parallel/workflow execution
    'parallel_runner.sh', // Run yylo tasks in parallel with tmux visualization
    'parallel_runner_wait.sh', // Wait for nonblocking parallel_runner runs to complete
    'workflow_runner.sh', // Run ordered YAML workflows with per-step artifacts
    'workflow_assert.py', // Emit named, machine-readable workflow assertions
    'git-flow.sh', // Configured integration/controller Git-flow entrypoint
    'git_flow.py', // Canonical controller-owned Git-flow engine
    ...MANAGED_SCRIPT_NAMES, // Lifecycle scripts are declared once in managed-assets.json
  ])];

  private static readonly ROOT_DELEGATE_MARKER = '# yylo-managed: root-git-flow.v1';
  private static readonly ROOT_DELEGATE = `#!/usr/bin/env bash
# yylo-managed: root-git-flow.v1
set -euo pipefail
ROOT="$(CDPATH= cd -- "$(dirname -- "\${BASH_SOURCE[0]}")/.." && pwd -P)"
exec "$ROOT/.juno_task/scripts/git-flow.sh" "$@"
`;

  /**
   * Get the templates scripts directory from the package
   */
  private static getPackageScriptsDir(): string | null {
    const __dirname = path.dirname(fileURLToPath(import.meta.url));

    const candidates = [
      path.join(__dirname, '..', '..', 'templates', 'scripts'), // dist (production)
      path.join(__dirname, '..', 'templates', 'scripts'), // src (development)
    ];

    for (const scriptsPath of candidates) {
      if (fs.existsSync(scriptsPath)) {
        return scriptsPath;
      }
    }

    if (process.env.YYLO_DEBUG === '1') {
      console.error('[DEBUG] ScriptInstaller: Could not find templates/scripts directory');
      console.error('[DEBUG] Tried:', candidates);
    }

    return null;
  }

  /**
   * Check if a specific script exists in the project's .juno_task/scripts/ directory
   */
  static async scriptExists(projectDir: string, scriptName: string): Promise<boolean> {
    const scriptPath = path.join(projectDir, '.juno_task', 'scripts', scriptName);
    return fs.pathExists(scriptPath);
  }

  /**
   * Install a specific script to the project's .juno_task/scripts/ directory
   * @param projectDir - The project root directory
   * @param scriptName - Name of the script to install (e.g., 'run_until_completion.sh')
   * @param silent - If true, suppresses console output
   * @returns true if script was installed, false if installation was skipped or failed
   */
  static async installScript(projectDir: string, scriptName: string, silent = false): Promise<boolean> {
    return withControllerGenerationMutation(projectDir, () => this.installScriptUnlocked(projectDir, scriptName, silent));
  }

  private static async installScriptUnlocked(
    projectDir: string,
    scriptName: string,
    silent = false,
  ): Promise<boolean> {
    try {
      if (
        MANAGED_SCRIPT_NAMES.includes(scriptName)
        && !(await this.isMetadataOnlyController(projectDir))
      ) {
        const ready = await this.prepareManagedLifecycleBundle(projectDir, silent, false);
        if (!ready) return false;
      }
      const packageScriptsDir = this.getPackageScriptsDir();
      if (!packageScriptsDir) {
        if (!silent && process.env.YYLO_DEBUG === '1') {
          console.error('[DEBUG] ScriptInstaller: Package scripts directory not found');
        }
        return false;
      }

      const sourcePath = path.join(packageScriptsDir, scriptName);
      if (!(await fs.pathExists(sourcePath))) {
        if (!silent && process.env.YYLO_DEBUG === '1') {
          console.error(`[DEBUG] ScriptInstaller: Source script not found: ${sourcePath}`);
        }
        return false;
      }

      // Ensure .juno_task/scripts directory exists
      const destDir = path.join(projectDir, '.juno_task', 'scripts');
      await fs.ensureDir(destDir);

      const destPath = path.join(destDir, scriptName);

      // Ensure parent directory exists for scripts in subdirectories (e.g., hooks/session_counter.sh)
      const destParentDir = path.dirname(destPath);
      if (destParentDir !== destDir) {
        await fs.ensureDir(destParentDir);
      }

      // Copy the script
      await fs.copy(sourcePath, destPath, { overwrite: true });

      // Make executable
      if (scriptName.endsWith('.sh') || scriptName.endsWith('.py')) {
        await fs.chmod(destPath, 0o755);
      }

      if (!silent) {
        console.log(`✓ Installed script: ${scriptName} to .juno_task/scripts/`);
      }

      if (process.env.YYLO_DEBUG === '1') {
        console.error(`[DEBUG] ScriptInstaller: Installed ${scriptName} to ${destPath}`);
      }

      return true;
    } catch (error) {
      if (!silent && process.env.YYLO_DEBUG === '1') {
        console.error(`[DEBUG] ScriptInstaller: Failed to install ${scriptName}:`, error);
      }
      return false;
    }
  }

  /** Install the root convenience delegate without overwriting unrelated project scripts. */
  static async installRootGitFlowDelegate(projectDir: string, silent = true): Promise<boolean> {
    return withControllerGenerationMutation(projectDir, () => this.installRootGitFlowDelegateUnlocked(projectDir, silent));
  }

  private static async installRootGitFlowDelegateUnlocked(
    projectDir: string,
    silent = true,
  ): Promise<boolean> {
    const destination = path.join(projectDir, 'scripts', 'git-flow.sh');
    if (await fs.pathExists(destination)) {
      const existing = await fs.readFile(destination, 'utf8');
      if (!existing.includes(this.ROOT_DELEGATE_MARKER)) {
        if (!silent) {
          console.error(
            '⚠ Preserved existing scripts/git-flow.sh because it is not Juno-managed.',
          );
        }
        return false;
      }
      if (this.ROOT_DELEGATE === existing) return false;
    }
    await fs.ensureDir(path.dirname(destination));
    await fs.writeFile(destination, this.ROOT_DELEGATE, { mode: 0o755 });
    await fs.chmod(destination, 0o755);
    if (!silent) console.log('✓ Installed managed delegate: scripts/git-flow.sh');
    return true;
  }

  private static async rootDelegateNeedsUpdate(projectDir: string): Promise<boolean> {
    const destination = path.join(projectDir, 'scripts', 'git-flow.sh');
    if (!(await fs.pathExists(destination))) return true;
    const existing = await fs.readFile(destination, 'utf8');
    if (!existing.includes(this.ROOT_DELEGATE_MARKER)) return false;
    return existing !== this.ROOT_DELEGATE;
  }

  /**
   * Check which required scripts are missing from the project
   * @param projectDir - The project root directory
   * @returns Array of missing script names
   */
  static async getMissingScripts(projectDir: string): Promise<string[]> {
    const missing: string[] = [];

    for (const script of this.REQUIRED_SCRIPTS) {
      if (!(await this.scriptExists(projectDir, script))) {
        missing.push(script);
      }
    }

    return missing;
  }

  /**
   * Auto-install any missing required scripts
   * This should be called on CLI startup for initialized projects
   * @param projectDir - The project root directory
   * @param silent - If true, suppresses console output
   * @returns true if any scripts were installed
   */
  static async autoInstallMissing(projectDir: string, silent = true): Promise<boolean> {
    return withControllerGenerationMutation(projectDir, () => this.autoInstallMissingUnlocked(projectDir, silent));
  }

  private static async autoInstallMissingUnlocked(projectDir: string, silent = true): Promise<boolean> {
    try {
      const metadataOnlyController = await this.isMetadataOnlyController(projectDir);
      // First check if .juno_task exists (project is initialized)
      const junoTaskDir = path.join(projectDir, '.juno_task');
      if (!(await fs.pathExists(junoTaskDir))) {
        // Project not initialized, skip
        return false;
      }

      const missing = await this.getMissingScripts(projectDir);

      const delegateNeeded = metadataOnlyController
        ? false
        : await this.rootDelegateNeedsUpdate(projectDir);
      if (missing.length === 0 && !delegateNeeded) {
        return false;
      }

      if (process.env.YYLO_DEBUG === '1') {
        console.error(`[DEBUG] ScriptInstaller: Missing scripts: ${missing.join(', ')}`);
      }

      let scriptsToInstall = missing;
      if (
        !metadataOnlyController
        && missing.some((script) => MANAGED_SCRIPT_NAMES.includes(script))
      ) {
        const lifecycleReady = await this.prepareManagedLifecycleBundle(projectDir, silent, false);
        if (!lifecycleReady) {
          scriptsToInstall = missing.filter((script) => !MANAGED_SCRIPT_NAMES.includes(script));
        }
      }

      let installedAny = delegateNeeded
        ? await this.installRootGitFlowDelegate(projectDir, silent)
        : false;
      for (const script of scriptsToInstall) {
        const installed = await this.installScript(projectDir, script, silent);
        if (installed) {
          installedAny = true;
        }
      }

      if (installedAny && !silent) {
        console.log(`✓ Auto-installed ${scriptsToInstall.length} missing script(s)`);
      }

      return installedAny;
    } catch (error) {
      if (process.env.YYLO_DEBUG === '1') {
        console.error('[DEBUG] ScriptInstaller: autoInstallMissing error:', error);
      }
      return false;
    }
  }

  /**
   * Update a script if the package version is newer (by content comparison)
   * @param projectDir - The project root directory
   * @param scriptName - Name of the script to update
   * @param silent - If true, suppresses console output
   * @returns true if script was updated
   */
  static async updateScriptIfNewer(projectDir: string, scriptName: string, silent = true): Promise<boolean> {
    return withControllerGenerationMutation(projectDir, () => this.updateScriptIfNewerUnlocked(projectDir, scriptName, silent));
  }

  private static async updateScriptIfNewerUnlocked(
    projectDir: string,
    scriptName: string,
    silent = true,
  ): Promise<boolean> {
    try {
      const packageScriptsDir = this.getPackageScriptsDir();
      if (!packageScriptsDir) {
        return false;
      }

      const sourcePath = path.join(packageScriptsDir, scriptName);
      const destPath = path.join(projectDir, '.juno_task', 'scripts', scriptName);

      // If destination doesn't exist, install it
      if (!(await fs.pathExists(destPath))) {
        return this.installScript(projectDir, scriptName, silent);
      }

      // Compare contents
      const [sourceContent, destContent] = await Promise.all([
        fs.readFile(sourcePath, 'utf-8'),
        fs.readFile(destPath, 'utf-8'),
      ]);

      if (sourceContent !== destContent) {
        return this.installScript(projectDir, scriptName, silent);
      }

      return false;
    } catch (error) {
      if (process.env.YYLO_DEBUG === '1') {
        console.error(
          `[DEBUG] ScriptInstaller: updateScriptIfNewer error for ${scriptName}:`,
          error,
        );
      }
      return false;
    }
  }

  /**
   * Get the path to a script in the project's .juno_task/scripts/ directory
   */
  static getScriptPath(projectDir: string, scriptName: string): string {
    return path.join(projectDir, '.juno_task', 'scripts', scriptName);
  }

  /**
   * List all required scripts and their installation status
   */
  static async listRequiredScripts(
    projectDir: string,
  ): Promise<{ name: string; installed: boolean }[]> {
    const results = [];

    for (const script of this.REQUIRED_SCRIPTS) {
      results.push({
        name: script,
        installed: await this.scriptExists(projectDir, script),
      });
    }

    return results;
  }

  /**
   * Get scripts that need updates based on content comparison
   * @param projectDir - The project root directory
   * @returns Array of script names that have different content from package version
   */
  static async getOutdatedScripts(projectDir: string): Promise<string[]> {
    const outdated: string[] = [];

    const packageScriptsDir = this.getPackageScriptsDir();
    if (!packageScriptsDir) {
      return outdated;
    }

    for (const script of this.REQUIRED_SCRIPTS) {
      const sourcePath = path.join(packageScriptsDir, script);
      const destPath = path.join(projectDir, '.juno_task', 'scripts', script);

      // Skip if source doesn't exist
      if (!(await fs.pathExists(sourcePath))) {
        continue;
      }

      // If destination doesn't exist, it's missing not outdated
      if (!(await fs.pathExists(destPath))) {
        continue;
      }

      // Compare contents
      try {
        const [sourceContent, destContent] = await Promise.all([
          fs.readFile(sourcePath, 'utf-8'),
          fs.readFile(destPath, 'utf-8'),
        ]);

        if (sourceContent !== destContent) {
          outdated.push(script);
        }
      } catch {
        // On error, assume it needs update
        outdated.push(script);
      }
    }

    return outdated;
  }

  /**
   * Check if any scripts need installation or update
   * @param projectDir - The project root directory
   * @returns true if any scripts need to be installed or updated
   */
  static async needsUpdate(projectDir: string): Promise<boolean> {
    try {
      // First check if .juno_task exists (project is initialized)
      const junoTaskDir = path.join(projectDir, '.juno_task');
      if (!(await fs.pathExists(junoTaskDir))) {
        return false;
      }

      const missing = await this.getMissingScripts(projectDir);
      if (missing.length > 0) {
        return true;
      }

      const outdated = await this.getOutdatedScripts(projectDir);
      return outdated.length > 0 || (
        !(await this.isMetadataOnlyController(projectDir))
        && await this.rootDelegateNeedsUpdate(projectDir)
      );
    } catch {
      return false;
    }
  }

  /** Validate config, package sources, and every possible script/requirement destination. */
  static async preflightUpdate(
    projectDir: string,
    force = false,
  ): Promise<TargetBoundManagedRecovery | null> {
    const junoTaskDir = path.join(projectDir, '.juno_task');
    if (!(await lstatIfPresent(junoTaskDir))) return null;
    const recovery = await this.assertManagedControllerPackageUpdateAllowed(projectDir);
    if (recovery && !force) {
      throw new Error(
        'Exact target-bound controller recovery requires the explicit `yy scripts update --force` transaction',
      );
    }
    if (await this.isMetadataOnlyController(projectDir)) {
      await this.updateMetadataControllerPolicies(projectDir, force);
      // Root instructions remain CLI-owned ignored runtime outputs even though
      // skills now have independent acquisition. Do not lose the tracked-user
      // evidence guard when the skill installer is no longer called here.
      const tracked = spawnSync('git', [
        '-C', projectDir, '-c', 'core.fsmonitor=false', 'ls-files', '-z', '--', 'AGENTS.md', 'CLAUDE.md',
      ], { encoding: 'utf8', timeout: 30_000, maxBuffer: 1024 * 1024 });
      if (tracked.status !== 0) {
        throw new Error('Metadata-controller instruction ownership inspection failed; preserve all bytes');
      }
      const paths = tracked.stdout.split('\0').filter(Boolean);
      if (paths.length > 0) {
        throw new Error(
          `Metadata-controller agent surface contains tracked user evidence; reviewed evacuation is required: ${paths.join(', ')}`,
        );
      }
    }

    const { ManagedProjectAssets } = await import('./managed-project-assets.js');
    await ManagedProjectAssets.preflight(projectDir, { force, recovery: recovery ?? undefined });

    const packageScriptsDir = this.getPackageScriptsDir();
    if (!packageScriptsDir) {
      throw new Error('YYLO package scripts are missing');
    }
    await assertPackageSource(packageScriptsDir, packageScriptsDir, 'directory');
    for (const scriptName of this.REQUIRED_SCRIPTS) {
      const recovered = recovery?.assets.get(path.join(MANAGED_SCRIPT_ROOT, scriptName));
      if (!recovered) {
        const source = path.join(packageScriptsDir, scriptName);
        await assertPackageSource(source, packageScriptsDir, 'file');
      }
      await assertSafeManagedWritePath(
        projectDir,
        path.join(projectDir, '.juno_task', 'scripts', scriptName),
      );
    }
    await assertSafeManagedWritePath(projectDir, path.join(projectDir, 'scripts', 'git-flow.sh'));
    await assertSafeManagedWritePath(projectDir, path.join(projectDir, '.venv_juno'));
    await assertSafeManagedWritePath(
      projectDir,
      path.join(projectDir, '.juno_task', '.requirements-cache'),
    );
    return recovery;
  }

  /**
   * Automatically update scripts - installs missing AND updates outdated scripts
   * Similar to ServiceInstaller.autoUpdate(), this ensures project scripts
   * are always in sync with the package version.
   *
   * This should be called on every CLI run to ensure scripts are up-to-date.
   * @param projectDir - The project root directory
   * @param silent - If true, suppresses console output
   * @param force - If true, reinstall all scripts regardless of content comparison
   * @returns true if any scripts were installed or updated
   */
  static async autoUpdate(projectDir: string, silent = true, force = false): Promise<boolean> {
    return withControllerGenerationMutation(projectDir, () => this.autoUpdateUnlocked(projectDir, silent, force));
  }

  private static async autoUpdateUnlocked(projectDir: string, silent = true, force = false): Promise<boolean> {
    // Keep this outside the compatibility catch: generation regression is a
    // control-plane refusal, not a best-effort installer miss.
    const recovery = await this.assertManagedControllerPackageUpdateAllowed(projectDir);
    if (recovery) {
      throw new Error(
        'Target-bound controller runtime cannot use package auto-update; run `yy scripts update --force`',
      );
    }
    try {
      const debug = process.env.YYLO_DEBUG === '1';
      const metadataOnlyController = await this.isMetadataOnlyController(projectDir);

      // Metadata-only controllers receive their controller-class managed seeds
      // (lifecycle workflows and prompts) through this installer; an absent
      // seed keeps the update loop alive even when every script is current.
      const controllerSeedsMissing = metadataOnlyController
        ? await this.missingManagedControllerSeeds(projectDir)
        : [];

      // First check if .juno_task exists (project is initialized)
      const junoTaskDir = path.join(projectDir, '.juno_task');
      if (!(await fs.pathExists(junoTaskDir))) {
        return false;
      }

      let scriptsToUpdate: string[];

      if (force) {
        // Force update: reinstall all required scripts
        scriptsToUpdate = [...this.REQUIRED_SCRIPTS];
        if (debug) {
          console.error(
            `[DEBUG] ScriptInstaller: Force update - reinstalling all ${scriptsToUpdate.length} scripts`,
          );
        }
      } else {
        const missing = await this.getMissingScripts(projectDir);
        const outdated = await this.getOutdatedScripts(projectDir);

        if (debug) {
          if (missing.length > 0) {
            console.error(`[DEBUG] ScriptInstaller: Missing scripts: ${missing.join(', ')}`);
          }
          if (outdated.length > 0) {
            console.error(`[DEBUG] ScriptInstaller: Outdated scripts: ${outdated.join(', ')}`);
          }
        }

        const delegateNeeded = await this.rootDelegateNeedsUpdate(projectDir);
        if (missing.length === 0 && outdated.length === 0
            && !delegateNeeded && controllerSeedsMissing.length === 0) {
          return false;
        }

        scriptsToUpdate = [...new Set([...missing, ...outdated])];
      }

      let seedsInstalled = false;
      const lifecycleScriptsPending = scriptsToUpdate.some((script) =>
        MANAGED_SCRIPT_NAMES.includes(script));
      if (lifecycleScriptsPending && !metadataOnlyController) {
        const lifecycleReady = await this.prepareManagedLifecycleBundle(
          projectDir,
          silent,
          force,
        );
        if (!lifecycleReady) {
          scriptsToUpdate = scriptsToUpdate.filter(
            (script) => !MANAGED_SCRIPT_NAMES.includes(script),
          );
        }
      } else if (controllerSeedsMissing.length > 0) {
        const installed = await this.installManagedControllerSeeds(projectDir, silent);
        seedsInstalled = installed.length > 0;
        if (seedsInstalled && (debug || !silent)) {
          console.log('Installed managed controller lifecycle seeds:');
          for (const destination of installed) console.log(`  ${destination}`);
        }
      }

      let updatedAny = seedsInstalled || (metadataOnlyController
        ? false
        : await this.installRootGitFlowDelegate(projectDir, silent));
      if (scriptsToUpdate.length === 0) return updatedAny;

      for (const script of scriptsToUpdate) {
        const installed = await this.installScript(projectDir, script, silent);
        if (installed) {
          updatedAny = true;
        }
      }

      if (updatedAny) {
        if (debug) {
          console.error(`[DEBUG] ScriptInstaller: Updated ${scriptsToUpdate.length} script(s)`);
        }
        if (!silent) {
          console.log(`✓ Updated ${scriptsToUpdate.length} script(s) in .juno_task/scripts/`);
        }
      }

      return updatedAny;
    } catch (error) {
      if (process.env.YYLO_DEBUG === '1') {
        console.error('[DEBUG] ScriptInstaller: autoUpdate error:', error);
      }
      return false;
    }
  }

  private static async assertForcedScriptsInstalled(projectDir: string): Promise<void> {
    const packageScriptsDir = this.getPackageScriptsDir();
    if (!packageScriptsDir) throw new Error('YYLO package scripts are missing');
    for (const scriptName of this.REQUIRED_SCRIPTS) {
      const source = path.join(packageScriptsDir, scriptName);
      const destination = path.join(projectDir, '.juno_task', 'scripts', scriptName);
      if (!(await fs.pathExists(destination)) ||
          !(await fs.readFile(source)).equals(await fs.readFile(destination))) {
        throw new Error(`Force update did not install exact package bytes: ${scriptName}`);
      }
      if (scriptName.endsWith('.sh') || scriptName.endsWith('.py')) {
        const mode = (await fs.stat(destination)).mode & 0o777;
        if (mode !== 0o755) {
          throw new Error(`Force update installed an unsafe mode for ${scriptName}: ${mode.toString(8)}`);
        }
      }
    }
  }

  /**
   * Force update all scripts and run install_requirements.sh with --force-update
   * This bypasses the 24-hour cache and reinstalls all Python dependencies
   * @param projectDir - The project root directory
   * @param silent - If true, suppresses console output
   * @returns true if update was successful
   */
  static async forceUpdateAll(projectDir: string, silent = false): Promise<boolean> {
    return withControllerGenerationMutation(projectDir, () => this.forceUpdateAllUnlocked(projectDir, silent));
  }

  private static async forceUpdateAllUnlocked(projectDir: string, silent = false): Promise<boolean> {
    const debug = process.env.YYLO_DEBUG === '1';

    try {
      await this.preflightUpdate(projectDir, true);
      if (!(await fs.pathExists(path.join(projectDir, '.juno_task')))) return false;
      // First, force update all scripts.
      const scriptsUpdated = await this.autoUpdate(projectDir, silent, true);
      await this.assertForcedScriptsInstalled(projectDir);

      // Retire the old worktree-local version-check cache. The installed script
      // now uses Git-common-dir (or XDG outside Git), keeping sparse controllers clean.
      await fs.remove(path.join(projectDir, '.juno_task', '.requirements-cache'));

      // Then run install_requirements.sh with --force-update
      const scriptsDir = path.join(projectDir, '.juno_task', 'scripts');
      const installScript = path.join(scriptsDir, 'install_requirements.sh');

      if (await fs.pathExists(installScript)) {
        if (debug || !silent) {
          console.log('Running install_requirements.sh --force-update...');
        }

        try {
          const { spawnSync } = await import('child_process');
          // Array-form spawn: no shell interpolation of paths into a command string.
          const localVenv = path.join(projectDir, '.venv_juno');
          const inheritedPath = process.env.PATH ?? '';
          const scan = spawnSync('bash', [installScript, '--force-update'], {
            cwd: projectDir,
            encoding: 'utf8',
            stdio: 'pipe',
            env: {
              ...process.env,
              VIRTUAL_ENV: await fs.pathExists(localVenv) ? localVenv : '',
              CONDA_DEFAULT_ENV: '',
              PATH: await fs.pathExists(path.join(localVenv, 'bin'))
                ? `${path.join(localVenv, 'bin')}${path.delimiter}${inheritedPath}`
                : inheritedPath,
            },
          });
          const output = scan.stdout ?? '';
          if (scan.status !== 0) {
            const failure: NodeJS.ErrnoException & { stdout?: string; stderr?: string } =
              new Error(`install_requirements.sh --force-update failed with exit code ${scan.status}`);
            failure.stdout = scan.stdout ?? '';
            failure.stderr = scan.stderr ?? '';
            throw failure;
          }

          if (output && output.trim() && (debug || !silent)) {
            console.log(output);
          }

          if (!silent) {
            console.log('✓ Python dependencies force updated (cache bypassed)');
          }
        } catch (error: any) {
          if (error.stdout && error.stdout.trim() && (debug || !silent)) {
            console.log(error.stdout);
          }
          throw error instanceof Error ? error : new Error(String(error));
        }
      }

      if (debug || !silent) {
        console.log('✓ Force updated scripts and Python dependencies');
      }
      return scriptsUpdated;
    } catch (error) {
      if (debug) {
        console.error('[DEBUG] ScriptInstaller: forceUpdateAll error:', error);
      }
      throw error;
    }
  }
}
