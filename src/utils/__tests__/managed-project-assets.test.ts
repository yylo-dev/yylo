import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import * as os from 'node:os';
import * as path from 'node:path';
import fs from 'fs-extra';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ConfigLoader, getPromptMacroDictionary } from '../../core/config.js';
import { ScriptInstaller } from '../script-installer.js';
import {
  MANAGED_ASSETS,
  MANAGED_CONTROLLER_ASSETS,
  MANAGED_PROMPT_MACROS,
  ManagedProjectAssets,
  managedAssetRecordsIdentity,
} from '../managed-project-assets.js';
import { runBoundedTestProcess } from '../../test-utils/bounded-process.js';
import { withManagedUpdateRollback } from '../managed-update-transaction.js';
import {
  MANAGED_INSTALL_OPERATION_TIMEOUT_MS,
  useSharedHeavyWorkloadLock,
} from '../../test-utils/resource-lock.js';
import { contentionBudgetMs } from '../../test-utils/contention-budget.js';

const sha256 = (value: string) => createHash('sha256').update(value).digest('hex');

describe('ManagedProjectAssets', {
  timeout: MANAGED_INSTALL_OPERATION_TIMEOUT_MS,
  retry: 0,
}, () => {
  useSharedHeavyWorkloadLock(
    'Vitest ManagedProjectAssets installation and installed-runtime suite',
  );
  let projectDir: string;

  beforeEach(async () => {
    // Transport is covered with authenticated artifacts and a real Ledger by
    // maintenance/tests/test_package_wiki_generation.py; isolate installer writes here.
    vi.spyOn(ManagedProjectAssets, 'stagePackageWiki').mockReturnValue(
      JSON.stringify({ schema_version: 'yylo_package_wiki_binding.v1', records: [] }) + '\n');
    vi.spyOn(ManagedProjectAssets, 'verifyPackageWiki').mockReturnValue(true);
    projectDir = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-managed-assets-'));
    await fs.ensureDir(path.join(projectDir, '.juno_task'));
    await fs.writeJson(path.join(projectDir, '.juno_task', 'config.json'), {
      promptMacros: {
        global: { existing: 'keep me' },
        local: { clean_worktree: 'local override' },
      },
    });
  });

  afterEach(async () => {
    vi.restoreAllMocks();
    await fs.remove(projectDir);
  });

  it('renders schema-valid consumer policies and keeps monorepo dogfood package-bound', async () => {
    const remote = 'https://github.com/example/consumer.git';
    await ManagedProjectAssets.update(projectDir, {
      silent: true,
      localization: { targetBranch: 'trunk', gitRemoteUrl: remote },
    });

    const taskPath = path.join(projectDir, '.juno_task/config/task-workspace.json');
    const metadataPath = path.join(projectDir, '.juno_task/config/metadata-controller.json');
    const hydrationPath = path.join(projectDir, '.juno_task/config/worktree-hydration.yaml');
    const task = await fs.readJson(taskPath);
    const metadata = await fs.readJson(metadataPath);
    const hydration = await fs.readFile(hydrationPath, 'utf8');
    expect(task.target_ref).toBe('refs/heads/trunk');
    expect(task.workspace_root).toBe('@state/yylo/task-worktrees');
    expect(task.full_suite_validation.id).toBe('consumer-full-suite-canary');
    expect(task.validation_profiles).toBeUndefined();
    expect(task.documentation_validation.public_identities).toEqual([remote]);
    expect(metadata.controller_branch).toBe('refs/heads/juno/controller-metadata');
    expect(metadata.product_ref).toBe('refs/heads/trunk');
    expect(hydration).toContain('verify-clean');
    expect(hydration).not.toContain('juno-code');
    expect(hydration).not.toContain('juno-benchmark');

    const validation = spawnSync('python3', ['-c', [
      'import importlib.util, pathlib, sys',
      `root=pathlib.Path(${JSON.stringify(projectDir)})`,
      "p=root/'.juno_task/scripts/task_workspace.py'",
      'sys.path.insert(0, str(p.parent))',
      "s=importlib.util.spec_from_file_location('consumer_task_workspace', p)",
      'm=importlib.util.module_from_spec(s); s.loader.exec_module(m)',
      'm.load_config(root)',
    ].join(';')], { encoding: 'utf8' });
    expect(validation.status, validation.stderr).toBe(0);

    await fs.writeFile(taskPath, `${JSON.stringify({ ...task, owner_value: true }, null, 2)}\n`);
    const preserved = await ManagedProjectAssets.update(projectDir, {
      silent: true,
      localization: { targetBranch: 'main', gitRemoteUrl: 'https://example.test/new' },
    });
    expect(preserved.conflicts.map((entry) => entry.destination)).toContain(
      '.juno_task/config/task-workspace.json',
    );
    expect((await fs.readJson(taskPath)).owner_value).toBe(true);

    await ManagedProjectAssets.update(projectDir, {
      force: true,
      silent: true,
      localization: { targetBranch: 'main', gitRemoteUrl: 'https://example.test/new' },
    });
    const forced = await fs.readJson(taskPath);
    expect(forced.owner_value).toBeUndefined();
    expect(forced.documentation_validation.public_identities).toEqual(['https://example.test/new']);
    expect(JSON.stringify(forced)).not.toContain('askbudi/juno-mono');

    const monorepo = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-managed-monorepo-'));
    try {
      await fs.ensureDir(path.join(monorepo, '.juno_task'));
      await fs.writeJson(path.join(monorepo, '.juno_task/config.json'), {});
      await fs.outputJson(path.join(monorepo, 'juno-code/package.json'), {
        name: '@yylo/cli', version: '0.0.0-test',
      });
      await ManagedProjectAssets.update(monorepo, {
        silent: true,
        localization: { targetBranch: 'consumer-branch', gitRemoteUrl: remote },
      });
      expect(await fs.readFile(
        path.join(monorepo, '.juno_task/config/task-workspace.json'), 'utf8',
      )).toBe(await fs.readFile(
        path.join(process.cwd(), 'src/templates/config/task-workspace.json'), 'utf8',
      ));
    } finally {
      await fs.remove(monorepo);
    }
  });

  it('keeps every checked-in managed destination bound to its inventory hash', async () => {
    const productRoot = path.resolve(process.cwd(), '..');
    const inventoryPath = path.join(productRoot, '.juno_task/managed-assets.json');
    if (!(await fs.pathExists(inventoryPath))) return;

    const inventory = await fs.readJson(inventoryPath);
    expect(inventory.packageName).toBe('@yylo/cli');
    const assetsSha256 = managedAssetRecordsIdentity(inventory.assets);
    expect(inventory.instructionBundle.assetsSha256).toBe(assetsSha256);
    const identityCore = {
      schemaVersion: inventory.instructionBundle.schemaVersion,
      semanticVersion: inventory.instructionBundle.semanticVersion,
      packageVersion: inventory.instructionBundle.packageVersion,
      assetCount: inventory.instructionBundle.assetCount,
      assetsSha256,
    };
    expect(inventory.instructionBundle.bundleSha256).toBe(
      sha256(JSON.stringify(identityCore)),
    );
    for (const [destination, identity] of Object.entries(
      inventory.assets as Record<string, { sourceSha256: string; installedSha256: string }>,
    )) {
      const destinationPath = path.join(productRoot, destination);
      expect(await fs.pathExists(destinationPath), destination).toBe(true);
      const actual = sha256(await fs.readFile(destinationPath, 'utf8'));
      expect(identity.installedSha256, destination).toBe(actual);
      expect(identity.sourceSha256, destination).toBe(actual);
    }
  });

  it('keeps every active lifecycle instruction surface on the one-task native contract', async () => {
    const sourceRoot = path.join(process.cwd(), 'src/templates');
    const files = [
      'controller-agent/AGENTS.md', 'controller-agent/CLAUDE.md',
      'prompts/life_cycle.md', 'prompts/new_task_workflow.md',
      'prompts/clean_worktree.md', 'prompts/run_workflow.md',
      'skills/canonical/ralph-loop/references/implement.md',
      'wiki/controller/git_worktree_lifecycle.md',
      'wiki/controller/metadata_controller_boundary.md',
    ];
    const surfaces = (await Promise.all(files.map((file) => fs.readFile(
      path.join(sourceRoot, file), 'utf8',
    )))).join('\n');
    for (const obsolete of [
      'The merge owner uses `yy merge status|next|resolve`',
      'Advance queued work with `yy merge next`',
      'Serialized delivery: `yy merge status|next|resolve`',
      'Use `yy merge status` and `yy merge next`',
    ]) expect(surfaces).not.toContain(obsolete);
    for (const obsolete of [
      'yy release', 'release train', 'immutable epoch', 'private train',
      'history-preserving train',
    ]) expect(surfaces.toLowerCase()).not.toContain(obsolete);
    for (const required of [
      'yy merge status', 'yy merge land TASK_ID', 'yy merge project TASK_ID',
      'expected-old', 'zero models',
    ]) expect(surfaces).toContain(required);
    for (const retired of [
      'yy merge arbiter', 'yy merge drive', 'yy merge next', 'yy merge resolve',
      'REVIEW_FINDINGS_EXHAUSTED',
    ]) expect(surfaces).not.toContain(retired);
  });

  it('admits canonical controller runtime twins for coherent template changes', async () => {
    const declaration = await fs.readJson(
      path.join(process.cwd(), 'src/templates/managed-assets.json'),
    );
    expect(declaration.admissionOutputs).toEqual(expect.arrayContaining([
      {
        source: 'scripts/juno-toolchain-policy.sh',
        destination: '.juno_task/scripts/juno-toolchain-policy.sh',
      },
      {
        source: 'scripts/kanban.sh',
        destination: '.juno_task/scripts/kanban.sh',
      },
    ]));
  });

  it('installs and receipts the complete metadata-controller bundle while preserving customization', async () => {
    const configPath = path.join(projectDir, '.juno_task', 'config.json');
    const config = await fs.readJson(configPath);
    config.controllerWorkspace = {
      mode: 'metadata-only', policy: '.juno_task/config/metadata-controller.json',
    };
    await fs.writeJson(configPath, config);
    const installed = await ManagedProjectAssets.update(projectDir, { silent: true });
    for (const asset of MANAGED_CONTROLLER_ASSETS) {
      expect(installed.installed).toContain(asset.destination);
      expect(await fs.pathExists(path.join(projectDir, asset.destination))).toBe(true);
    }
    const manifestPath = path.join(projectDir, '.juno_task/managed-assets.json');
    const manifest = await fs.readJson(manifestPath);
    for (const destination of [
      'AGENTS.md',
      'CLAUDE.md',
      '.juno_task/prompts/lifecycle/task-implementation.md',
      '.juno_task/workflows/yy-task-run.yaml',
    ]) {
      expect(manifest.assets[destination], destination).toBeDefined();
      expect(manifest.assets[destination].installedSha256).toBe(
        sha256(await fs.readFile(path.join(projectDir, destination), 'utf8')),
      );
    }
    expect(Object.keys(manifest.assets).some((entry) => entry.includes('/skills/'))).toBe(false);
    expect(Object.keys(manifest.assets).some((entry) => entry.startsWith('.juno_task/wiki/'))).toBe(false);
    expect(manifest.assets['.juno_task/config/package-wiki.json']).toBeDefined();
    expect(await fs.pathExists(path.join(projectDir, '.juno_task/wiki'))).toBe(false);
    expect(manifest.instructionBundle.assetCount).toBe(Object.keys(manifest.assets).length);
    expect((await ManagedProjectAssets.inspectGeneration(projectDir)).coherent).toBe(true);

    const legacy = structuredClone(manifest);
    legacy.schemaVersion = 1;
    legacy.packageName = 'juno-code';
    legacy.packageVersion = '2.1.1';
    delete legacy.instructionBundle;
    await fs.writeJson(manifestPath, legacy);
    await ManagedProjectAssets.update(projectDir, { silent: true });
    const upgraded = await fs.readJson(manifestPath);
    expect(upgraded.schemaVersion).toBe(2);
    expect(upgraded.packageName).toBe('@yylo/cli');
    expect(upgraded.instructionBundle.assetCount).toBe(Object.keys(upgraded.assets).length);

    const workflow = path.join(projectDir, '.juno_task/workflows/yy-task-run.yaml');
    await fs.writeFile(workflow, '{"owner":"customized"}\n');
    const preserved = await ManagedProjectAssets.update(projectDir, { silent: true });
    expect(preserved.conflicts).toContainEqual(expect.objectContaining({
      destination: '.juno_task/workflows/yy-task-run.yaml',
    }));
    expect(await fs.readFile(workflow, 'utf8')).toBe('{"owner":"customized"}\n');
  });

  it('refuses incompatible Ledger bootstrap before changing installed controller bytes', async () => {
    const configPath = path.join(projectDir, '.juno_task/config.json');
    const config = await fs.readJson(configPath);
    config.controllerWorkspace = { mode: 'metadata-only', policy: '.juno_task/config/metadata-controller.json' };
    await fs.writeJson(configPath, config);
    vi.mocked(ManagedProjectAssets.stagePackageWiki).mockImplementation(() => {
      throw new Error('package_wiki_bootstrap_refused');
    });
    await expect(ManagedProjectAssets.update(projectDir, { silent: true })).rejects.toThrow('package_wiki_bootstrap_refused');
    expect(await fs.pathExists(path.join(projectDir, 'AGENTS.md'))).toBe(false);
    expect(await fs.pathExists(path.join(projectDir, '.juno_task/managed-assets.json'))).toBe(false);
  });

  it('rolls back an interrupted metadata-controller bundle and converges on retry', async () => {
    const configPath = path.join(projectDir, '.juno_task/config.json');
    const config = await fs.readJson(configPath);
    config.controllerWorkspace = {
      mode: 'metadata-only', policy: '.juno_task/config/metadata-controller.json',
    };
    await fs.writeJson(configPath, config);
    const manifestPath = path.join(projectDir, '.juno_task/managed-assets.json');
    const legacy = `${JSON.stringify({
      schemaVersion: 1,
      packageName: 'juno-code',
      packageVersion: '2.1.1',
      assets: {},
    }, null, 2)}\n`;
    await fs.writeFile(manifestPath, legacy);

    await expect(withManagedUpdateRollback(projectDir, async () => {
      await ManagedProjectAssets.update(projectDir, { force: true, silent: true });
      throw new Error('injected interruption after receipt persistence');
    })).rejects.toThrow('injected interruption');
    expect(await fs.readFile(manifestPath, 'utf8')).toBe(legacy);
    expect(await fs.pathExists(path.join(projectDir, 'AGENTS.md'))).toBe(false);
    expect(await fs.pathExists(
      path.join(projectDir, '.juno_task/workflows/yy-task-run.yaml'),
    )).toBe(false);

    await withManagedUpdateRollback(projectDir, () =>
      ManagedProjectAssets.update(projectDir, { force: true, silent: true }));
    const report = await ManagedProjectAssets.inspectGeneration(projectDir);
    expect(report.coherent).toBe(true);
    expect(report.instructionBundle?.schemaVersion).toBe('juno_instruction_bundle.v1');
  });

  it('detects and atomically refreshes receipted nested guidance without touching task state', async () => {
    await fs.writeJson(path.join(projectDir, '.juno_task/config.json'), {
      controllerWorkspace: { mode: 'metadata-only', policy: '.juno_task/config/metadata-controller.json' },
    });
    const independentSkill = path.join(projectDir, '.pi/skills/ralph-loop-yylo/references/implement.md');
    await fs.outputFile(independentSkill, 'independent skill owner bytes');
    await ManagedProjectAssets.update(projectDir, { silent: true });
    const destinations = [
      'AGENTS.md',
      'CLAUDE.md',
      '.juno_task/prompts/lifecycle/task-implementation.md',
    ];
    const receiptPath = path.join(projectDir, '.juno_task/managed-assets.json');
    const receipt = await fs.readJson(receiptPath);
    // Reproduce a same-version old receipt: source-generation/version agreement
    // must not conceal content drift in root instructions or a nested prompt.
    receipt.schemaVersion = 1;
    delete receipt.instructionBundle;
    for (const destination of destinations) {
      await fs.writeFile(path.join(projectDir, destination), 'yy merge arbiter run\n');
      receipt.assets[destination].installedSha256 = sha256('yy merge arbiter run\n');
    }
    await fs.writeJson(receiptPath, receipt);
    const statePath = path.join(projectDir, '.juno_task/runtime/task-hydration/active.json');
    await fs.outputFile(statePath, 'preserve active lease/hydration bytes');
    const before = await fs.readFile(receiptPath);
    const report = await ManagedProjectAssets.inspectGeneration(projectDir);
    expect(report.coherent).toBe(false);
    for (const destination of destinations) {
      expect(report.entries).toContainEqual({ destination, installClass: 'controller', state: 'outdated' });
    }
    expect(await fs.readFile(receiptPath)).toEqual(before);
    await expect(withManagedUpdateRollback(projectDir, async () => {
      await ManagedProjectAssets.update(projectDir, { silent: true });
      throw new Error('injected nested guidance interruption');
    })).rejects.toThrow('injected nested guidance interruption');
    expect(await fs.readFile(receiptPath)).toEqual(before);
    for (const destination of destinations) {
      expect(await fs.readFile(path.join(projectDir, destination), 'utf8')).toBe('yy merge arbiter run\n');
    }
    await withManagedUpdateRollback(projectDir, () => ManagedProjectAssets.update(projectDir, { silent: true }));
    expect((await ManagedProjectAssets.inspectGeneration(projectDir)).coherent).toBe(true);
    expect(await fs.readFile(statePath, 'utf8')).toBe('preserve active lease/hydration bytes');
    expect(await fs.readFile(independentSkill, 'utf8')).toBe('independent skill owner bytes');
    expect(Object.keys((await fs.readJson(receiptPath)).assets).some((entry) => entry.includes('/skills/'))).toBe(false);
  });

  it('preserves unowned legacy wiki bytes without adopting them as runtime guidance', async () => {
    await fs.writeJson(path.join(projectDir, '.juno_task/config.json'), {
      controllerWorkspace: { mode: 'metadata-only', policy: '.juno_task/config/metadata-controller.json' },
    });
    const destination = '.juno_task/wiki/controller/git_worktree_lifecycle.md';
    await fs.outputFile(path.join(projectDir, destination), 'user-owned old guidance');
    const result = await ManagedProjectAssets.update(projectDir, { silent: true });
    expect(result.conflicts).toEqual([]);
    expect(await fs.readFile(path.join(projectDir, destination), 'utf8')).toBe('user-owned old guidance');
    expect(await fs.pathExists(path.join(projectDir, 'AGENTS.md'))).toBe(true);
    const receipt = await fs.readJson(path.join(projectDir, '.juno_task/managed-assets.json'));
    expect(receipt.assets[destination]).toBeUndefined();
    expect((await ManagedProjectAssets.inspectGeneration(projectDir)).coherent).toBe(true);
    vi.mocked(ManagedProjectAssets.verifyPackageWiki).mockReturnValue(false);
    expect((await ManagedProjectAssets.inspectGeneration(projectDir)).coherent).toBe(false);
  });

  it('uses canonical UTF-8 ordering and encoding for managed record identity', () => {
    const keys = ['😀', 'a', '.dot', 'é', 'A', '_under'];
    const assets = Object.fromEntries(keys.map((destination, index) => [destination, {
      type: 'script',
      templateVersion: '1.2.3',
      sourceSha256: String(index + 1).repeat(64),
      installedSha256: String(index + 1).repeat(64),
    }]));
    expect(managedAssetRecordsIdentity(assets)).toBe(
      'd965b07f2505b7a1c7c7c5dfb8151409191fc8dfa37b81b4bc9ec9a2db4c6f82',
    );
  });

  it('writes one complete semantic instruction-bundle identity on fresh install', async () => {
    await ManagedProjectAssets.update(projectDir, { silent: true });
    const manifest = await fs.readJson(path.join(projectDir, '.juno_task/managed-assets.json'));
    expect(manifest.schemaVersion).toBe(2);
    expect(manifest.instructionBundle).toEqual(expect.objectContaining({
      schemaVersion: 'juno_instruction_bundle.v1',
      semanticVersion: '1.2.0',
      packageVersion: manifest.packageVersion,
      assetCount: Object.keys(manifest.assets).length,
      assetsSha256: expect.stringMatching(/^[0-9a-f]{64}$/),
      bundleSha256: expect.stringMatching(/^[0-9a-f]{64}$/),
    }));
    const report = await ManagedProjectAssets.inspectGeneration(projectDir);
    expect(report.coherent).toBe(true);
    expect(report.instructionBundle).toEqual(manifest.instructionBundle);
  });

  it('upgrades a coherent v1 receipt and rejects tampered v2 identity before mutation', async () => {
    await ManagedProjectAssets.update(projectDir, { silent: true });
    const manifestPath = path.join(projectDir, '.juno_task/managed-assets.json');
    const legacy = await fs.readJson(manifestPath);
    legacy.schemaVersion = 1;
    legacy.packageName = 'juno-code';
    legacy.packageVersion = '2.1.1';
    delete legacy.instructionBundle;
    await fs.writeJson(manifestPath, legacy);
    await ManagedProjectAssets.update(projectDir, { silent: true });
    const upgraded = await fs.readJson(manifestPath);
    expect(upgraded.schemaVersion).toBe(2);
    expect(upgraded.packageName).toBe('@yylo/cli');
    expect(upgraded.instructionBundle.schemaVersion).toBe('juno_instruction_bundle.v1');

    upgraded.instructionBundle.assetsSha256 = '0'.repeat(64);
    await fs.writeJson(manifestPath, upgraded);
    const before = await fs.readFile(manifestPath);
    await expect(ManagedProjectAssets.update(projectDir, { silent: true })).rejects.toThrow(
      'Mixed or partial managed instruction bundle',
    );
    expect(await fs.readFile(manifestPath)).toEqual(before);
  });

  it('installs every managed asset and registers resolvable file-backed macros', async () => {
    const result = await ManagedProjectAssets.update(projectDir, { silent: true });
    expect(result.installed).toHaveLength(MANAGED_ASSETS.length);

    const configJson = await fs.readJson(path.join(projectDir, '.juno_task', 'config.json'));
    expect(configJson.promptMacros.global.existing).toBe('keep me');
    expect(configJson.promptMacros.local.clean_worktree).toBe('local override');
    for (const [name, mapping] of Object.entries(MANAGED_PROMPT_MACROS)) {
      expect(configJson.promptMacros.global[name]).toEqual(mapping);
      expect(await fs.pathExists(path.join(projectDir, mapping.path))).toBe(true);
    }

    const overrideLoader = new ConfigLoader(projectDir);
    await overrideLoader.fromProjectConfig();
    expect(getPromptMacroDictionary(overrideLoader.merge()).clean_worktree).toBe('local override');

    configJson.promptMacros.local = {};
    await fs.writeJson(path.join(projectDir, '.juno_task', 'config.json'), configJson);
    const freshLoader = new ConfigLoader(projectDir);
    await freshLoader.fromProjectConfig();
    const dictionary = getPromptMacroDictionary(freshLoader.merge());
    for (const name of ['life_cycle', 'clean_worktree', 'new_task_workflow', 'run_workflow']) {
      expect(dictionary[name], name).toContain('Optional read-only `yy task preflight TASK_ID`');
      expect(dictionary[name], name).toContain('not a prerequisite');
      expect(dictionary[name], name).toContain('yy task finish TASK_ID');
      expect(dictionary[name], name).not.toMatch(/preflighted (?:tip|closure)|^yy task preflight TASK_ID$/m);
    }
    expect(dictionary.life_cycle).toContain('revision: 10');
    expect(dictionary.life_cycle).toContain('juno.life_cycle.v1');
    expect(dictionary.life_cycle).not.toContain('yy watch exec');
    expect(dictionary.life_cycle).toContain('yy watch status|await|follow RUN_ID');
    expect(dictionary.life_cycle).toContain('watch never launches, retries, cancels, or completes work');
    expect(dictionary.life_cycle).toContain('yy task preflight TASK_ID');
    expect(dictionary.life_cycle).toContain('Merge launches\n   zero models');
    expect(dictionary.life_cycle).not.toContain('launch a fresh read-only independent `yy pi` review');
    expect(dictionary.life_cycle).toContain('native-Git expected-old delivery');
    expect(dictionary.life_cycle).toContain('Package preparation uses the repository maintainer');
    expect(dictionary.life_cycle).toContain('RC/tag creation');
    expect(dictionary.life_cycle).not.toContain('immutable epoch');
    expect(dictionary.clean_worktree).toContain('# Clean Bolt task workspaces');
    expect(dictionary.clean_worktree).toContain('yy task start TASK_ID');
    expect(dictionary.clean_worktree).toContain('yy task preflight TASK_ID');
    expect(dictionary.clean_worktree).toContain('yy merge land TASK_ID');
    expect(dictionary.clean_worktree).toContain('yy merge project TASK_ID');
    expect(dictionary.clean_worktree).toContain('launches zero models');
    expect(dictionary.reflect).toContain('# End-of-session reflection');
    expect(dictionary.reflect).toContain('REFLECTION_TABLE');
    expect(dictionary.reflect).toContain('complete reflection table');
    expect(dictionary.new_task_workflow).toContain('# Start a feature task');
    expect(dictionary.new_task_workflow).toContain('task-workspace policy');
    expect(dictionary.new_task_workflow).toContain('one task branch and one product worktree');
    expect(dictionary.new_task_workflow).toContain('exact frozen base');
    expect(dictionary.new_task_workflow).toContain('yy task start TASK_ID');
    expect(dictionary.new_task_workflow).toContain('yy task preflight TASK_ID');
    expect(dictionary.new_task_workflow).toContain('yy task finish TASK_ID');
    expect(dictionary.new_task_workflow).toContain('yy merge land TASK_ID');
    expect(dictionary.new_task_workflow).toContain('yy merge project TASK_ID');
    expect(dictionary.new_task_workflow).toContain('launches zero models');
    expect(dictionary.new_task_workflow).toContain('maintainer-only');
    expect(dictionary.new_task_workflow).not.toContain('release train');
    expect(dictionary.new_task_workflow).not.toContain('immutable epoch');
    expect(dictionary.run_workflow).toContain('# Run a workflow or Bolt task');
    expect(dictionary.run_workflow).toContain('yy task preflight TASK_ID');
    expect(dictionary.run_workflow).toContain('read-only doctor support');
    expect(dictionary.run_workflow).toContain('yy merge land TASK_ID');
    expect(dictionary.run_workflow).toContain('yy merge project TASK_ID');
    expect(dictionary.run_workflow).toContain('launches zero models');
    expect(dictionary.migrate_juno_code_v1_to_v2).toContain('# Migrate a YYLO v1 project');
    expect(dictionary.migrate_juno_kanban_v1_to_v2).toContain('# Migrate juno-kanban v1 storage');
    expect(dictionary.migrate_juno_kanban_v1_to_v2).toContain('resolve its latest reviewed commit');
    expect(dictionary.migrate_juno_kanban_v1_to_v2).toContain(
      'a merely compatible but older installed v2 is stale',
    );
    for (const prompt of [
      dictionary.migrate_juno_code_v1_to_v2,
      dictionary.migrate_juno_kanban_v1_to_v2,
    ]) {
      expect(prompt).toContain('548d1e6763bb6c5b3f2b27a63398faf225ebbb1c');
      expect(prompt).toContain('2.0.5');
      expect(prompt).toContain('ruamel.yaml');
      expect(prompt).toContain('--no-deps');
      expect(prompt).toContain('before any canonical board access');
    }
    expect(dictionary.migrate_juno_code_v1_to_v2).toContain('absolute integration-owner worktree');
    expect(dictionary.migrate_juno_code_v1_to_v2).toContain('yy task preflight CANARY_X');
    expect(dictionary.migrate_juno_code_v1_to_v2).toContain('yy task preflight CANARY_Y');
    expect(dictionary.migrate_juno_code_v1_to_v2).toContain('screenshots');
    expect(dictionary.migrate_juno_code_v1_to_v2).toContain('separate yes/no authorities');
    expect(dictionary.migrate_juno_code_v2_to_v2_1).toContain(
      '548d1e6763bb6c5b3f2b27a63398faf225ebbb1c',
    );
    expect(dictionary.migrate_juno_code_v2_to_v2_1).toContain('juno_kanban-2.0.6');
    expect(dictionary.migrate_juno_code_v2_to_v2_1).toContain('ruamel.yaml');
    expect(dictionary.migrate_juno_code_v2_to_v2_1).toContain('--no-deps');
    expect(dictionary.migrate_juno_code_v2_to_v2_1).toContain('before any canonical board access');
    const reviewPrompt = await fs.readFile(
      path.join(projectDir, '.juno_task/prompts/review_commit_parallel_runner.md'),
      'utf8',
    );
    const metadataPolicy = await fs.readJson(
      path.join(projectDir, '.juno_task/config/metadata-controller.json'),
    );
    const riskPolicy = await fs.readJson(
      path.join(projectDir, '.juno_task/config/risk-policy.json'),
    );
    expect(metadataPolicy.schema_version).toBe('juno_metadata_controller_policy.v1');
    expect(metadataPolicy.product_forbidden).toContain('.juno_task/tasks');
    expect(metadataPolicy.runtime.ignored_roots).toContain('.juno_task/scripts');
    expect(metadataPolicy.runtime.ignored_roots).toContain('.juno_task/cache');
    expect(metadataPolicy.runtime.ignored_roots).toContain('.juno_task/locks');
    expect(metadataPolicy.runtime.ignored_roots).toContain('AGENTS.md');
    expect(metadataPolicy.runtime.ignored_roots).toContain('.agents');
    expect(metadataPolicy.tracked_exact).not.toContain('.juno_task/state/queue.json');
    expect(metadataPolicy.tracked_exact).toContain('.juno_task/state/tasks.json');
    expect(metadataPolicy.tracked_exact).toContain('.juno_task/config/task-workspace.json');
    expect(metadataPolicy.tracked_exact).toContain('.juno_task/config/risk-policy.json');
    expect(metadataPolicy.generated_metadata).toContain('.juno_task/config/task-workspace.json');
    expect(metadataPolicy.generated_metadata).toContain('.juno_task/config/risk-policy.json');
    expect(metadataPolicy.tracked_top_level_files).toContain('.juno_task/receipts');
    expect(riskPolicy.schema_version).toBe('juno_bolt_risk_policy.v1');
    expect(riskPolicy.review_policy).toEqual({
      low: { sequence: [], min: 0, max: 0 },
      normal: { sequence: ['reviewer'], min: 0, max: 1 },
      high: { sequence: ['reviewer_a', 'reviewer_b'], min: 2, max: 2 },
      release: { sequence: [], min: 0, max: 0 },
    });
    expect(
      await fs.readFile(
        path.join(process.cwd(), 'src/templates/wiki/controller/metadata_controller_boundary.md'),
        'utf8',
      ),
    ).toContain('Controller commits never merge or synchronize into a product target');
    expect(reviewPrompt).toContain('Never use bare `pi`');
    expect(reviewPrompt).toContain('Review only');
    expect(reviewPrompt).toContain(
      'do not edit, commit, update Kanban, create advisory tasks, launch another reviewer',
    );
    expect(reviewPrompt).toContain('Return PASS only after reviewing the complete frozen candidate');
    expect(reviewPrompt).toContain('Return every independently actionable admitted defect');
    expect(reviewPrompt).toContain('Do not downgrade an out-of-scope idea');
    expect(reviewPrompt).toContain('structured `truncated=true` signal');
    expect(reviewPrompt).not.toContain('then resolve it');
    expect(
      await fs.readFile(
        path.join(process.cwd(), 'src/templates/wiki/controller/parallel_runner_and_spec_review.md'),
        'utf8',
      ),
    ).toContain('Reviewer launcher identity');
    expect(
      await fs.readFile(path.join(process.cwd(), 'src/templates/wiki/controller/git_worktree_lifecycle.md'), 'utf8'),
    ).toContain('yy task finish TASK_ID');
    for (const relative of ['AGENTS.md', 'CLAUDE.md']) {
      const controllerInstruction = await fs.readFile(
        path.join(process.cwd(), 'src/templates/controller-agent', relative),
        'utf8',
      );
      expect(controllerInstruction, relative).toContain('Optional read-only `yy task preflight TASK_ID`');
      expect(controllerInstruction, relative).toContain('not a prerequisite');
      expect(controllerInstruction, relative).toContain('independently enforces admission and validation');
      expect(controllerInstruction, relative).toContain('yy merge land TASK_ID');
      expect(controllerInstruction, relative).toContain('yy merge project TASK_ID');
      expect(controllerInstruction, relative).toContain('launches no models');
    }
    const installedWatcher = await fs.readFile(
      path.join(projectDir, '.juno_task/scripts/watch_progress.py'),
    );
    expect(await fs.pathExists(path.join(projectDir, '.juno_task/wiki/watching_progress.md'))).toBe(false);
    const installedWatchingWiki = await fs.readFile(
      path.join(process.cwd(), 'src/templates/wiki/controller/yy_pi_progress.md'),
    );
    expect(installedWatcher).toEqual(
      await fs.readFile(path.join(process.cwd(), 'src/templates/scripts/watch_progress.py')),
    );
    expect(installedWatchingWiki).toEqual(
      await fs.readFile(path.join(process.cwd(), 'src/templates/wiki/controller/yy_pi_progress.md')),
    );
    expect(installedWatchingWiki.toString()).toContain('juno.watch-footer.v1');
    expect(installedWatchingWiki.toString()).toContain('juno.watch-run.v1');
    expect(installedWatchingWiki.toString()).toContain('`watch exec` is retired');
    expect(installedWatchingWiki.toString()).toContain('yy task finish TASK_ID');

    const canonicalImplementationReference = await fs.readFile(
      path.join(process.cwd(), 'src/templates/skills/canonical/ralph-loop/references/implement.md'),
      'utf8',
    );
    expect(canonicalImplementationReference).toContain('GENERATED DESTINATIONS');
    for (const agent of ['claude', 'codex', 'pi']) {
      const implementationReference = await fs.readFile(
        path.join(
          process.cwd(),
          'src/templates/skills',
          agent,
          'ralph-loop/references/implement.md',
        ),
        'utf8',
      );
      expect(implementationReference).toBe(canonicalImplementationReference);
      expect(implementationReference).toContain('# Bolt implementation worker contract');
      expect(implementationReference).toContain('yy task start TASK_ID');
      expect(implementationReference).toContain('yy task finish TASK_ID');
      expect(implementationReference).toContain('Tests and semantic reviews remain explicit');
      expect(implementationReference).toContain('yy merge land TASK_ID');
      expect(implementationReference).toContain('yy merge project TASK_ID');
      expect(implementationReference).toContain('merge launches zero models');
      expect(implementationReference).toContain('controller checkpoint');
    }

    const distributedSkills = [
      ['claude', '.claude/skills/ralph-loop/references/implement.md'],
      ['codex', '.agents/skills/ralph-loop/references/implement.md'],
      ['pi', '.pi/skills/ralph-loop/references/implement.md'],
    ] as const;
    for (const [agent, relativePath] of distributedSkills) {
      const canonical = await fs.readFile(
        path.join(
          process.cwd(),
          'src/templates/skills',
          agent,
          'ralph-loop/references/implement.md',
        ),
        'utf8',
      );
      expect(await fs.readFile(path.resolve(process.cwd(), '..', relativePath), 'utf8')).toBe(
        canonical,
      );
      expect(await fs.readFile(path.resolve(process.cwd(), relativePath), 'utf8')).toBe(canonical);
    }

    const unchanged = await ManagedProjectAssets.update(projectDir, { silent: true });
    expect(unchanged.unchanged).toHaveLength(MANAGED_ASSETS.length);
  });

  it('distributes the canonical pre-implementation dependency hydration contract', async () => {
    await ManagedProjectAssets.update(projectDir, { silent: true });
    expect(await fs.pathExists(path.join(projectDir, '.juno_task/wiki/task_dependency_hydration.md'))).toBe(false);
    const installedWiki = await fs.readFile(
      path.join(process.cwd(), 'src/templates/wiki/controller/task_dependency_hydration.md'),
      'utf8',
    );
    const sourceWiki = await fs.readFile(
      path.join(process.cwd(), 'src/templates/wiki/controller/task_dependency_hydration.md'),
      'utf8',
    );
    expect(installedWiki).toBe(sourceWiki);

    for (const required of [
      'hydration_selection: admitted_scope_v1',
      'dependency_root: juno-code',
      'Selection uses **admitted paths**',
      '(`cwd` and declared `input_paths`)',
      'historical dependency checks',
      'Broad',
      'task-local installers',
      'Node 22',
      'node_modules/.yylo-package-lock.sha256',
      'hashed selection',
      'Unselected lock changes and unrelated task metadata',
      'prior selection is stale until explicit',
      'git status --short',
      'Before editing',
      'yy task hydrate TASK_ID',
    ]) {
      expect(installedWiki).toContain(required);
    }

    const taskStartPrompt = await fs.readFile(
      path.join(projectDir, '.juno_task/prompts/new_task_workflow.md'),
      'utf8',
    );
    expect(taskStartPrompt).toContain(
      'Immediately after start and before editing or testing, follow the exact-lock, validation-cwd-aware hydration contract',
    );
    expect(taskStartPrompt).toContain('Stop before implementation if provisioning');

    for (const relative of [
      'src/templates/prompts/clean_worktree.md',
      'src/templates/prompts/run_workflow.md',
      'src/templates/prompts/migrate_juno_code_v1_to_v2.md',
      'src/templates/controller-agent/AGENTS.md',
      'src/templates/controller-agent/CLAUDE.md',
      'src/templates/wiki/controller/git_worktree_lifecycle.md',
    ]) {
      const instruction = await fs.readFile(path.join(process.cwd(), relative), 'utf8');
      expect(instruction, relative).toContain('task_dependency_hydration.md');
      expect(instruction, relative).toMatch(/before\s+(?:editing|any edit|edits)/i);
      expect(instruction, relative).toMatch(/stop before implementation|stops on provisioning/i);
    }
    for (const relative of [
      'src/templates/prompts/clean_worktree.md',
      'src/templates/prompts/run_workflow.md',
      'src/templates/prompts/migrate_juno_code_v1_to_v2.md',
    ]) {
      const instruction = await fs.readFile(path.join(process.cwd(), relative), 'utf8');
      expect(instruction, relative).toContain('../wiki/controller/task_dependency_hydration.md');
      expect(instruction, relative).not.toContain('../wiki/task_dependency_hydration.md');
    }
  });

  it('runs the documented hydration helper with workflow probes for fresh, lock-changed, and failed installs', async () => {
    const wiki = await fs.readFile(
      path.join(process.cwd(), 'src/templates/wiki/controller/task_dependency_hydration.md'),
      'utf8',
    );
    expect(wiki).toContain('hydrate-node --cwd ROOT');
    expect(wiki).toContain('verify-node-lock --cwd ROOT');
    const helper = path.resolve(process.cwd(), 'src/templates/scripts/worktree_hydration.py');
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-hydration-command-'));
    try {
      const bin = path.join(root, 'bin');
      const count = path.join(root, 'npm-count');
      const probeCount = path.join(root, 'npm-probe-count');
      await fs.ensureDir(bin);
      await fs.writeFile(path.join(root, 'package-lock.json'), '{"lockfileVersion":3}\n');
      await fs.writeFile(
        path.join(root, '.gitignore'),
        'node_modules/\nbin/\nnpm-count\nnpm-probe-count\n',
      );
      await fs.writeFile(path.join(bin, 'node'), '#!/bin/sh\necho 22.22.3\n');
      await fs.writeFile(
        path.join(bin, 'npm'),
        '#!/bin/sh\ncase "$1" in\n  ci) echo run >> "$FAKE_NPM_COUNT"; mkdir -p node_modules; echo fake npm ci; exit "${FAKE_NPM_FAIL:-0}" ;;\n  ls) echo probe >> "$FAKE_NPM_PROBE_COUNT"; echo "{}"; exit "${FAKE_NPM_PROBE_FAIL:-0}" ;;\n  *) exit 2 ;;\nesac\n',
      );
      await fs.chmod(path.join(bin, 'node'), 0o755);
      await fs.chmod(path.join(bin, 'npm'), 0o755);
      spawnSync('git', ['init', '-q'], { cwd: root });
      spawnSync('git', ['add', '.gitignore', 'package-lock.json'], { cwd: root });
      spawnSync(
        'git',
        [
          '-c',
          'user.name=Test',
          '-c',
          'user.email=test@example.invalid',
          'commit',
          '-qm',
          'fixture',
        ],
        { cwd: root },
      );
      const runHydration = (extra: Record<string, string> = {}) => {
        const run = (action: string) => spawnSync('python3', [helper, '--project-root', root, action, '--cwd', '.'], {
          cwd: root,
          encoding: 'utf8',
          env: {
            ...process.env,
            PATH: `${bin}:${process.env.PATH}`,
            FAKE_NPM_COUNT: count,
            FAKE_NPM_PROBE_COUNT: probeCount,
            ...extra,
          },
        });
        const probe = run('verify-node-lock');
        return probe.status === 0 ? probe : run('hydrate-node');
      };

      const fresh = runHydration();
      expect(fresh.status).toBe(0);
      expect(fresh.stderr).toBe('');
      expect((await fs.readFile(count, 'utf8')).trim().split('\n')).toHaveLength(1);
      expect((await fs.readFile(probeCount, 'utf8')).trim().split('\n')).toHaveLength(1);
      expect(spawnSync('git', ['status', '--short'], { cwd: root, encoding: 'utf8' }).stdout).toBe(
        '',
      );

      const exact = runHydration();
      expect(exact.status).toBe(0);
      expect(exact.stderr).toBe('');
      expect((await fs.readFile(count, 'utf8')).trim().split('\n')).toHaveLength(1);

      await fs.writeFile(
        path.join(root, 'package-lock.json'),
        '{"lockfileVersion":3,"changed":true}\n',
      );
      const changed = runHydration();
      expect(changed.status).toBe(0);
      expect((await fs.readFile(count, 'utf8')).trim().split('\n')).toHaveLength(2);

      const failed = runHydration({ FAKE_NPM_FAIL: '17', FAKE_NPM_PROBE_FAIL: '1' });
      expect(failed.status).toBe(2);
      expect(failed.stderr).toContain('npm ci failed');
      expect(await fs.pathExists(path.join(root, 'node_modules/.yylo-package-lock.sha256'))).toBe(false);

      const failedProbe = runHydration({ FAKE_NPM_PROBE_FAIL: '1' });
      expect(failedProbe.status).toBe(2);
      expect(failedProbe.stderr).toContain('dependency tree does not satisfy the exact lock');
      expect(await fs.pathExists(path.join(root, 'node_modules/.yylo-package-lock.sha256'))).toBe(false);
    } finally {
      await fs.remove(root);
    }
  });

  it('ships closed wiki release inputs without installing a second runtime wiki store', async () => {
    const templatesDir = ManagedProjectAssets.getTemplatesDirectory();
    expect(templatesDir).not.toBeNull();
    const definitions = (await fs.readJson(path.join(templatesDir!, 'managed-assets.json')))
      .assets as Array<{
      source: string;
      destination: string;
      installClass: 'project' | 'script';
      type: string;
    }>;

    // This fixture exercises the package's own dogfood policy and Python suite.
    await fs.outputJson(path.join(projectDir, 'juno-code/package.json'), {
      name: '@yylo/cli', version: '0.0.0-test',
    });
    await ManagedProjectAssets.update(projectDir, { silent: true });
    await ScriptInstaller.autoUpdate(projectDir, true);

    const taskWorkflowHelper = await fs.readFile(
      path.join(projectDir, '.juno_task/scripts/task_workflow_helper.py'),
      'utf8',
    );
    expect(taskWorkflowHelper).toContain(
      'role review must not declare edit_capable true or edit_admission',
    );
    expect(taskWorkflowHelper).toContain(
      '$(yy wiki --path)/controller/parallel_runner_and_spec_review.md',
    );
    expect(taskWorkflowHelper).not.toContain(
      '"review": ["AGENTS.md", ".juno_task/wiki/parallel_runner_and_spec_review.md"]',
    );
    expect(taskWorkflowHelper).not.toContain('review_fix');
    const reflectPrompt = await fs.readFile(
      path.join(projectDir, '.juno_task/prompts/reflect.md'),
      'utf8',
    );
    expect(reflectPrompt).toContain('resolve the canonical root with `yy wiki --path`');
    expect(reflectPrompt).toContain('`controller/wiki_maintenance.md` otherwise');
    for (const name of [
      'parallel_runner_and_spec_review.md',
      'runtime_migration_and_replacement_contract.md',
    ]) {
      const lifecycleWiki = await fs.readFile(
        path.join(templatesDir!, 'wiki/controller', name),
        'utf8',
      );
      expect(lifecycleWiki).toContain('wiki_root=$(yy wiki --path 2>/dev/null || true)');
      expect(lifecycleWiki).toContain(`|| wiki_file=.juno_task/wiki/${name}`);
    }

    const managedWikis = definitions.filter((asset) => asset.type === 'wiki');
    const relativeLink = /\[[^\]]+\]\((?![a-z]+:|#)([^)#]+)(?:#[^)]*)?\)/gi;
    for (const wiki of managedWikis) {
      const wikiPath = path.join(templatesDir!, wiki.source);
      expect(await fs.pathExists(path.join(projectDir, wiki.destination))).toBe(false);
      const content = await fs.readFile(wikiPath, 'utf8');
      for (const match of content.matchAll(relativeLink)) {
        expect(
          await fs.pathExists(path.resolve(path.dirname(wikiPath), match[1])),
          `${wiki.source} has an unresolved release-input link: ${match[1]}`,
        ).toBe(true);
      }
    }

    for (const requiredPath of [
      '.juno_task/scripts/wiki_lint.sh',
      '.juno_task/scripts/wiki_lint.py',
      '.juno_task/scripts/managed_agent_runner.py',
      '.juno_task/scripts/tests/test_managed_agent_runner.py',
      '.juno_task/scripts/metadata_controller.py',
      '.juno_task/scripts/tests/test_metadata_controller.py',
      '.juno_task/scripts/controller_registration.py',
      '.juno_task/scripts/tests/test_controller_registration.py',
      '.juno_task/scripts/risk_policy.py',
      '.juno_task/scripts/tests/test_risk_policy.py',
      '.juno_task/scripts/release_gate.py',
      '.juno_task/scripts/tests/test_release_gate.py',
      '.juno_task/config/metadata-controller.json',
      '.juno_task/config/risk-policy.json',
    ]) {
      expect(await fs.pathExists(path.join(projectDir, requiredPath)), requiredPath).toBe(true);
    }

    const subprocessEnv = { ...process.env };
    for (const key of Object.keys(subprocessEnv)) {
      if (key.startsWith('GIT_')) delete subprocessEnv[key];
    }

    for (const command of [
      `./.juno_task/scripts/wiki_lint.sh --file '${path.join(templatesDir!, 'wiki/controller/parallel_runner_and_spec_review.md')}'`,
      `./.juno_task/scripts/wiki_lint.sh --file '${path.join(templatesDir!, 'wiki/controller/runtime_migration_and_replacement_contract.md')}'`,
      // Keep the fast suite bounded: this proves the installed lifecycle modules load;
      // the exact installed concurrency gate is exercised by the package acceptance loop.
      'python3 -m py_compile .juno_task/scripts/task_workspace.py .juno_task/scripts/merge_queue.py .juno_task/scripts/risk_policy.py',
      'python3 .juno_task/scripts/tests/test_metadata_controller.py',
      'python3 .juno_task/scripts/tests/test_risk_policy.py',
      'python3 .juno_task/scripts/tests/test_release_gate.py',
    ]) {
      const result = await runBoundedTestProcess('/bin/bash', ['-c', command], {
        cwd: projectDir,
        env: {
          ...subprocessEnv,
          PYTHONDONTWRITEBYTECODE: '1',
          PYTHONPYCACHEPREFIX: '/tmp/juno-managed-assets-pycache',
        },
        // The metadata-controller fixture is CPU-heavy on a loaded host. This
        // bounded operation budget starts after the cross-worktree lease wait
        // and scales with ambient contention so admission stays deterministic.
        timeoutMs: contentionBudgetMs(300_000),
        terminationGraceMs: 1_000,
      });
      expect(
        result.status,
        `${command}\n${result.diagnostic}\nstdout:\n${result.stdout}\nstderr:\n${result.stderr}`,
      ).toBe(0);
    }
  });

  it('detects a mixed lifecycle generation without changing project files', async () => {
    await ManagedProjectAssets.update(projectDir, { silent: true });
    await ScriptInstaller.autoUpdate(projectDir, true);
    expect((await ManagedProjectAssets.inspectGeneration(projectDir)).status).toBe('coherent');

    const guidancePath = path.join(projectDir, '.juno_task/prompts/reflect.md');
    await fs.writeFile(guidancePath, '# stale lifecycle guidance\n');
    const report = await ManagedProjectAssets.inspectGeneration(projectDir);

    expect(report.status).toBe('mixed');
    expect(report.coherent).toBe(false);
    expect(
      report.entries.find(
        (entry) => entry.destination === '.juno_task/prompts/reflect.md',
      )?.state,
    ).toBe('customized');
    expect(await fs.readFile(guidancePath, 'utf8')).toBe('# stale lifecycle guidance\n');
  });

  it('updates a stale unmodified managed file and reinstalls a missing file', async () => {
    await ManagedProjectAssets.update(projectDir, { silent: true });
    const destination = '.juno_task/prompts/run_workflow.md';
    const destinationPath = path.join(projectDir, destination);
    const oldManagedContent = 'old managed bytes\n';
    await fs.writeFile(destinationPath, oldManagedContent);
    const manifestPath = path.join(projectDir, '.juno_task', 'managed-assets.json');
    const manifest = await fs.readJson(manifestPath);
    manifest.schemaVersion = 1;
    delete manifest.instructionBundle;
    manifest.assets[destination].installedSha256 = sha256(oldManagedContent);
    await fs.writeJson(manifestPath, manifest);
    await fs.remove(path.join(projectDir, '.juno_task/prompts/new_task_workflow.md'));

    const result = await ManagedProjectAssets.update(projectDir, { silent: true });
    expect(result.updated).toContain(destination);
    expect(result.installed).toContain('.juno_task/prompts/new_task_workflow.md');
    expect(await fs.readFile(destinationPath, 'utf8')).toContain('# Run a workflow or Bolt task');
  });

  it('preserves customized prompts and writes a package-version candidate', async () => {
    await ManagedProjectAssets.update(projectDir, { silent: true });
    const destination = '.juno_task/prompts/clean_worktree.md';
    const destinationPath = path.join(projectDir, destination);
    await fs.writeFile(destinationPath, 'exact integration target: refs/heads/customer-release\n');

    const result = await ManagedProjectAssets.update(projectDir, { silent: true });
    const conflict = result.conflicts.find((entry) => entry.destination === destination);
    expect(conflict).toBeDefined();
    expect(await fs.readFile(destinationPath, 'utf8')).toContain('refs/heads/customer-release');
    expect(await fs.readFile(path.join(projectDir, conflict!.candidate), 'utf8')).toContain(
      '# Clean Bolt task workspaces',
    );
  });

  it('backs up a customized prompt before explicit force replacement', async () => {
    await ManagedProjectAssets.update(projectDir, { silent: true });
    const destination = '.juno_task/prompts/clean_worktree.md';
    const destinationPath = path.join(projectDir, destination);
    const customized = 'refs/heads/owner-specific\n';
    await fs.writeFile(destinationPath, customized);

    const result = await ManagedProjectAssets.update(projectDir, { force: true, silent: true });
    const backup = result.backups.find((entry) => entry.destination === destination);
    expect(backup).toBeDefined();
    expect(await fs.readFile(path.join(projectDir, backup!.backup), 'utf8')).toBe(customized);
    expect(await fs.readFile(destinationPath, 'utf8')).toContain('# Clean Bolt task workspaces');
  });

  it('preserves a customized managed runtime until force creates a byte-exact archive', async () => {
    await ManagedProjectAssets.update(projectDir, { silent: true });
    const destination = '.juno_task/scripts/metadata_controller.py';
    const destinationPath = path.join(projectDir, destination);
    const customized = Buffer.from('# owner-customized runtime\n', 'utf8');
    await fs.writeFile(destinationPath, customized);
    const laterManaged = path.join(projectDir, '.juno_task/scripts/migration_inventory.py');
    await fs.remove(laterManaged);

    const ordinary = await ManagedProjectAssets.update(projectDir, { silent: true });
    expect(ordinary.conflicts).toContainEqual(expect.objectContaining({ destination }));
    expect(await fs.readFile(destinationPath)).toEqual(customized);
    expect(await fs.pathExists(laterManaged)).toBe(false);
    expect(await ScriptInstaller.installScript(projectDir, 'metadata_controller.py', true)).toBe(
      false,
    );
    expect(
      await ScriptInstaller.updateScriptIfNewer(projectDir, 'metadata_controller.py', true),
    ).toBe(false);
    expect(await fs.readFile(destinationPath)).toEqual(customized);
    expect(await fs.pathExists(laterManaged)).toBe(false);

    const forced = await ManagedProjectAssets.update(projectDir, { force: true, silent: true });
    const backup = forced.backups.find((entry) => entry.destination === destination);
    expect(backup).toBeDefined();
    expect(await fs.readFile(path.join(projectDir, backup!.backup))).toEqual(customized);
    expect((await fs.stat(destinationPath)).mode & 0o111).not.toBe(0);
    expect(await fs.pathExists(laterManaged)).toBe(true);
  });

  it('rejects legacy 2.0 config shapes without changing project bytes', async () => {
    const configPath = path.join(projectDir, '.juno_task/config.json');
    const legacy = `${JSON.stringify(
      {
        lifecycle: { enabled: true },
        controllerWorkspace: { enabled: true },
        promptMacros: { global: { owner: 'preserve' } },
      },
      null,
      2,
    )}\n`;
    await fs.writeFile(configPath, legacy);

    await expect(ManagedProjectAssets.update(projectDir, { silent: true })).rejects.toThrow(
      'yy migrate evacuation-*',
    );
    expect(await fs.readFile(configPath, 'utf8')).toBe(legacy);
    expect(await fs.pathExists(path.join(projectDir, '.juno_task/managed-assets.json'))).toBe(
      false,
    );
    expect(await fs.pathExists(path.join(projectDir, '.juno_task/scripts'))).toBe(false);
  });

  it('rejects a missing managed leaf beneath a symlinked scripts directory', async () => {
    const outside = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-managed-outside-'));
    try {
      await fs.symlink(outside, path.join(projectDir, '.juno_task/scripts'), 'dir');
      await expect(ManagedProjectAssets.update(projectDir, { silent: true })).rejects.toThrow(
        'symbolic-link managed path component',
      );
      expect(await fs.readdir(outside)).toEqual([]);
      expect(await fs.pathExists(path.join(projectDir, '.juno_task/managed-assets.json'))).toBe(
        false,
      );
    } finally {
      await fs.remove(outside);
    }
  });

  it('rejects a dangling managed path component before any mutation', async () => {
    const dangling = path.join(projectDir, '.juno_task/scripts');
    await fs.symlink('missing-runtime-directory', dangling);

    await expect(ManagedProjectAssets.preflight(projectDir)).rejects.toThrow(
      'symbolic-link managed path component: .juno_task/scripts',
    );
    expect((await fs.lstat(dangling)).isSymbolicLink()).toBe(true);
    expect(await fs.readlink(dangling)).toBe('missing-runtime-directory');
    expect(await fs.pathExists(path.join(projectDir, '.juno_task/managed-assets.json'))).toBe(
      false,
    );
    expect(await fs.pathExists(path.join(projectDir, '.juno_task/prompts'))).toBe(false);
  });

  it('rejects a symlinked nested backup parent before force replacement', async () => {
    await ManagedProjectAssets.update(projectDir, { silent: true });
    const destination = '.juno_task/scripts/metadata_controller.py';
    const customized = '# customized before unsafe backup\n';
    await fs.writeFile(path.join(projectDir, destination), customized);
    const installedManifest = await fs.readJson(
      path.join(projectDir, '.juno_task/managed-assets.json'),
    );
    const outside = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-backup-outside-'));
    const unsafeParent = path.join(
      projectDir,
      `.juno_task/managed-conflicts/bolt-${installedManifest.packageVersion}/.juno_task/scripts`,
    );
    try {
      await fs.ensureDir(path.dirname(unsafeParent));
      await fs.symlink(outside, unsafeParent, 'dir');
      await expect(
        ManagedProjectAssets.update(projectDir, { force: true, silent: true }),
      ).rejects.toThrow('symbolic-link managed path component');
      expect(await fs.readdir(outside)).toEqual([]);
      expect(await fs.readFile(path.join(projectDir, destination), 'utf8')).toBe(customized);
    } finally {
      await fs.remove(outside);
    }
  });

  it('rejects a symlinked managed-specializations parent before receipt access', async () => {
    const outside = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-specialization-outside-'));
    try {
      await fs.symlink(outside, path.join(projectDir, '.juno_task/managed-specializations'), 'dir');
      await expect(ManagedProjectAssets.update(projectDir, { silent: true })).rejects.toThrow(
        'symbolic-link managed path component',
      );
      expect(await fs.readdir(outside)).toEqual([]);
      expect(await fs.pathExists(path.join(projectDir, '.juno_task/managed-assets.json'))).toBe(
        false,
      );
    } finally {
      await fs.remove(outside);
    }
  });

  it('archives retired managed and customized assets during the Bolt upgrade', async () => {
    const retiredManaged = '.juno_task/scripts/task_lifecycle.py';
    const retiredCustomized = '.juno_task/config/lifecycle.json';
    const managedBytes = '# old managed executor\n';
    const customizedBytes = '{"owner":"custom"}\n';
    await fs.ensureDir(path.join(projectDir, '.juno_task/scripts'));
    await fs.ensureDir(path.join(projectDir, '.juno_task/config'));
    await fs.writeFile(path.join(projectDir, retiredManaged), managedBytes);
    await fs.writeFile(path.join(projectDir, retiredCustomized), customizedBytes);
    await fs.writeJson(path.join(projectDir, '.juno_task/managed-assets.json'), {
      schemaVersion: 1,
      packageName: '@yylo/cli',
      packageVersion: '2.0.31',
      assets: {
        [retiredManaged]: {
          type: 'script',
          templateVersion: '2.0.31',
          sourceSha256: sha256(managedBytes),
          installedSha256: sha256(managedBytes),
        },
      },
    });

    await expect(ManagedProjectAssets.update(projectDir, { silent: true })).rejects.toThrow(
      'customized retired asset',
    );
    expect(await fs.pathExists(path.join(projectDir, retiredCustomized))).toBe(true);

    const forced = await ManagedProjectAssets.update(projectDir, { force: true, silent: true });
    expect(await fs.pathExists(path.join(projectDir, retiredManaged))).toBe(false);
    expect(await fs.pathExists(path.join(projectDir, retiredCustomized))).toBe(false);
    for (const retired of [retiredManaged, retiredCustomized]) {
      const backup = forced.backups.find((entry) => entry.destination === retired);
      expect(backup).toBeDefined();
      expect(await fs.pathExists(path.join(projectDir, backup!.backup))).toBe(true);
    }
  });

  it('reuses identical retired archives and never overwrites collisions on reappearance', async () => {
    const destination = '.juno_task/scripts/task_lifecycle.py';
    const destinationPath = path.join(projectDir, destination);
    const firstBytes = '# retired generation A\n';
    const secondBytes = '# retired generation B\n';
    await fs.ensureDir(path.dirname(destinationPath));
    await fs.writeFile(destinationPath, firstBytes);
    await fs.writeJson(path.join(projectDir, '.juno_task/managed-assets.json'), {
      schemaVersion: 1,
      packageName: '@yylo/cli',
      packageVersion: '2.0.31',
      assets: {
        [destination]: {
          type: 'script',
          templateVersion: '2.0.31',
          sourceSha256: sha256(firstBytes),
          installedSha256: sha256(firstBytes),
        },
      },
    });

    const initial = await ManagedProjectAssets.update(projectDir, { silent: true });
    const initialBackup = initial.backups.find((entry) => entry.destination === destination)!;
    expect(await fs.readFile(path.join(projectDir, initialBackup.backup), 'utf8')).toBe(firstBytes);

    await fs.writeFile(destinationPath, firstBytes);
    const identicalRetry = await ManagedProjectAssets.update(projectDir, {
      force: true,
      silent: true,
    });
    expect(identicalRetry.backups.find((entry) => entry.destination === destination)?.backup).toBe(
      initialBackup.backup,
    );
    expect(await fs.readFile(path.join(projectDir, initialBackup.backup), 'utf8')).toBe(firstBytes);

    const manifest = await fs.readJson(path.join(projectDir, '.juno_task/managed-assets.json'));
    const collisionRelative = path.join(
      '.juno_task',
      'managed-conflicts',
      `bolt-${manifest.packageVersion}`,
      `${destination}.${sha256(secondBytes).slice(0, 16)}.backup`,
    );
    await fs.ensureDir(path.dirname(path.join(projectDir, collisionRelative)));
    await fs.writeFile(path.join(projectDir, collisionRelative), 'preserve collision bytes\n');
    await fs.writeFile(destinationPath, secondBytes);

    const differingRetry = await ManagedProjectAssets.update(projectDir, {
      force: true,
      silent: true,
    });
    const differingBackup = differingRetry.backups.find(
      (entry) => entry.destination === destination,
    )!;
    expect(differingBackup.backup).not.toBe(initialBackup.backup);
    expect(differingBackup.backup).not.toBe(collisionRelative);
    expect(await fs.readFile(path.join(projectDir, initialBackup.backup), 'utf8')).toBe(firstBytes);
    expect(await fs.readFile(path.join(projectDir, collisionRelative), 'utf8')).toBe(
      'preserve collision bytes\n',
    );
    expect(await fs.readFile(path.join(projectDir, differingBackup.backup), 'utf8')).toBe(
      secondBytes,
    );
    expect(await fs.pathExists(destinationPath)).toBe(false);
  });

  it('archives a generated specialization receipt and installs the Bolt prompt', async () => {
    const prompt = 'generated exact-target specialization\n';
    const promptPath = path.join(projectDir, '.juno_task/prompts/clean_worktree.md');
    const receiptPath = path.join(
      projectDir,
      '.juno_task/managed-specializations/clean-worktree.json',
    );
    await fs.ensureDir(path.dirname(promptPath));
    await fs.ensureDir(path.dirname(receiptPath));
    await fs.writeFile(promptPath, prompt);
    await fs.writeJson(receiptPath, {
      schemaVersion: 2,
      promptPath: '.juno_task/prompts/clean_worktree.md',
      promptSha256: sha256(prompt),
    });

    const result = await ManagedProjectAssets.update(projectDir, { silent: true });
    expect(await fs.pathExists(receiptPath)).toBe(false);
    expect(await fs.readFile(promptPath, 'utf8')).toContain('# Clean Bolt task workspaces');
    expect(result.backups.map((entry) => entry.destination)).toEqual(
      expect.arrayContaining([
        '.juno_task/prompts/clean_worktree.md',
        '.juno_task/managed-specializations/clean-worktree.json',
      ]),
    );
  });

  it('preserves a conflicting macro unless force is explicit', async () => {
    const configPath = path.join(projectDir, '.juno_task', 'config.json');
    const config = await fs.readJson(configPath);
    config.promptMacros.global.run_workflow = { path: 'private/run.md' };
    await fs.writeJson(configPath, config);

    const ordinary = await ManagedProjectAssets.update(projectDir, { silent: true });
    expect(ordinary.macroConflicts).toContain('run_workflow');
    expect((await fs.readJson(configPath)).promptMacros.global.run_workflow.path).toBe(
      'private/run.md',
    );

    const forced = await ManagedProjectAssets.update(projectDir, { force: true, silent: true });
    expect((await fs.readJson(configPath)).promptMacros.global.run_workflow).toEqual(
      MANAGED_PROMPT_MACROS.run_workflow,
    );
    expect(forced.backups.some((entry) => entry.destination === '.juno_task/config.json')).toBe(
      true,
    );
  });
});
