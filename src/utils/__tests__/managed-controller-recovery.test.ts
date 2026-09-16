import { createHash } from 'node:crypto';
import * as os from 'node:os';
import * as path from 'node:path';
import fs from 'fs-extra';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { version as packageVersion } from '../../version.js';
import {
  createTargetBoundMetadataController,
  REAL_METADATA_CONTROLLER_TARGET_REF,
  REAL_STALE_CONTROLLER_SCRIPTS,
  realStaleControllerScriptBytes,
} from '../../cli/__tests__/helpers/managed-controller-recovery-fixture.js';
import { ManagedProjectAssets } from '../managed-project-assets.js';
import { ScriptInstaller } from '../script-installer.js';
import { withManagedUpdateRollback } from '../managed-update-transaction.js';
import {
  MANAGED_INSTALL_OPERATION_TIMEOUT_MS,
  useSharedHeavyWorkloadLock,
} from '../../test-utils/resource-lock.js';

const sha256 = (value: Buffer | string) => createHash('sha256').update(value).digest('hex');

describe('target-bound managed controller recovery', {
  timeout: MANAGED_INSTALL_OPERATION_TIMEOUT_MS,
  retry: 0,
}, () => {
  useSharedHeavyWorkloadLock('target-bound metadata-controller recovery fixtures');
  let root = '';

  afterEach(async () => {
    vi.restoreAllMocks();
    if (root) await fs.remove(root);
  });

  it('rolls back an interruption, preserves unrelated dirt, and converges exactly on retry', async () => {
    root = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-target-bound-recovery-'));
    const { targetSha, changedScripts, packageScriptsDir } =
      await createTargetBoundMetadataController(root);
    vi.spyOn(ScriptInstaller as any, 'getPackageScriptsDir').mockReturnValue(packageScriptsDir);
    const manifestPath = path.join(root, '.juno_task/managed-assets.json');
    const oldManifest = await fs.readFile(manifestPath);
    const agentPath = path.join(root, 'AGENTS.md');
    await fs.writeFile(agentPath, 'owner-customized ignored controller instructions\n');
    const oldAgent = await fs.readFile(agentPath);
    const oldWorkflow = await fs.readFile(path.join(root, '.juno_task/workflows/yy-task-run.yaml'));
    const ledgerPath = path.join(root, '.juno_task/ledger/owner.txt');
    await fs.appendFile(ledgerPath, 'unrelated dirty byte\n');
    const dirtyLedger = await fs.readFile(ledgerPath);

    const recovery = await ScriptInstaller.preflightUpdate(root, true);
    expect(recovery).toMatchObject({ packageVersion, targetSha });
    expect(changedScripts).toEqual([
      'managed_agent_runner.py',
      'merge_queue.py',
      'task_workspace.py',
      'tests/test_managed_agent_runner.py',
      'tests/test_merge_queue.py',
      'tests/test_task_workspace.py',
    ]);
    for (const name of changedScripts) {
      expect(recovery?.assets.get(`.juno_task/scripts/${name}`)?.toString()).toContain(
        `exact target-only recovery fixture: ${name}`,
      );
    }

    await expect(withManagedUpdateRollback(root, async () => {
      await ManagedProjectAssets.update(root, {
        force: true, silent: true, recovery: recovery ?? undefined,
      });
      throw new Error('injected interruption after target receipt persistence');
    })).rejects.toThrow('injected interruption');
    expect(await fs.readFile(manifestPath)).toEqual(oldManifest);
    expect(await fs.readFile(agentPath)).toEqual(oldAgent);
    expect(await fs.readFile(path.join(root, '.juno_task/workflows/yy-task-run.yaml')))
      .toEqual(oldWorkflow);
    expect(await fs.readFile(ledgerPath)).toEqual(dirtyLedger);
    const interruptionRoot = path.join(
      root, '.juno_task/runtime/managed-controller/update-interruptions',
    );
    const interruptionFiles = await fs.readdir(interruptionRoot);
    expect(interruptionFiles).toHaveLength(1);
    expect(await fs.readJson(path.join(interruptionRoot, interruptionFiles[0]))).toMatchObject({
      schema_version: 'juno_managed_update_interruption.v1',
      outcome: 'rolled_back',
      rollback_error_count: 0,
    });

    const retry = await ScriptInstaller.preflightUpdate(root, true);
    const recovered = await withManagedUpdateRollback(root, () => ManagedProjectAssets.update(root, {
      force: true, silent: true, recovery: retry ?? undefined,
    }));
    expect(recovered.backups).toContainEqual(expect.objectContaining({ destination: 'AGENTS.md' }));
    const agentBackup = recovered.backups.find((entry) => entry.destination === 'AGENTS.md');
    expect(await fs.readFile(path.join(root, agentBackup?.backup as string))).toEqual(oldAgent);
    await ScriptInstaller.assertMetadataControllerUpdateComplete(root, retry ?? undefined);
    const manifest = await fs.readJson(manifestPath);
    expect(manifest).toMatchObject({
      schemaVersion: 2,
      packageName: '@yylo/cli',
      packageVersion,
      instructionBundle: {
        schemaVersion: 'juno_instruction_bundle.v1',
        packageVersion,
        assetCount: retry?.assets.size,
      },
    });
    for (const [destination, expected] of retry?.assets ?? []) {
      const actual = await fs.readFile(path.join(root, destination));
      expect(actual, destination).toEqual(expected);
      expect(manifest.assets[destination].sourceSha256, destination).toBe(sha256(expected));
      expect(manifest.assets[destination].installedSha256, destination).toBe(sha256(actual));
    }
    expect(await fs.readFile(ledgerPath)).toEqual(dirtyLedger);

    const exactReceipt = await fs.readFile(manifestPath);
    const exactRetry = await ScriptInstaller.preflightUpdate(root, true);
    await withManagedUpdateRollback(root, () => ManagedProjectAssets.update(root, {
      force: true, silent: true, recovery: exactRetry ?? undefined,
    }));
    expect(await fs.readFile(manifestPath)).toEqual(exactReceipt);
    expect(await fs.readdir(interruptionRoot)).toEqual(interruptionFiles);
    await ScriptInstaller.assertMetadataControllerUpdateComplete(root, exactRetry ?? undefined);
  });

  it('preserves historical protected-target evidence rather than mixing its declaration with the new instruction bundle', async () => {
    root = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-real-controller-recovery-'));
    const { changedScripts, packageScriptsDir } =
      await createTargetBoundMetadataController(
        root,
        packageVersion,
        { exactTargetRef: REAL_METADATA_CONTROLLER_TARGET_REF },
      );
    vi.spyOn(ScriptInstaller as any, 'getPackageScriptsDir').mockReturnValue(packageScriptsDir);
    for (const [name, identity] of Object.entries(REAL_STALE_CONTROLLER_SCRIPTS)) {
      const staleBytes = realStaleControllerScriptBytes(
        name as keyof typeof REAL_STALE_CONTROLLER_SCRIPTS,
      );
      const targetBytes = await fs.readFile(path.join(root, '.juno_task/scripts', name));
      expect(sha256(staleBytes), `${name} stale distribution`).toBe(identity.staleSha256);
      expect(sha256(targetBytes), `${name} protected target`).toBe(identity.targetSha256);
      expect(staleBytes, name).not.toEqual(targetBytes);
      await fs.outputFile(path.join(packageScriptsDir, name), staleBytes);
    }

    const manifestPath = path.join(root, '.juno_task/managed-assets.json');
    const before = await fs.readFile(manifestPath);
    // The archived package predates controller wiki-alias ownership and bundle
    // revision 1.1.0. Equal version strings do not authorize this newer adapter
    // to recover it using a different declaration; retain the historical bytes.
    await expect(ScriptInstaller.preflightUpdate(root, true)).rejects.toThrow(
      'Installed package declaration is mixed with the invoked package identity',
    );
    expect(changedScripts).toEqual(Object.keys(REAL_STALE_CONTROLLER_SCRIPTS));
    expect(await fs.readFile(manifestPath)).toEqual(before);
    for (const [name, identity] of Object.entries(REAL_STALE_CONTROLLER_SCRIPTS)) {
      expect(sha256(await fs.readFile(path.join(root, '.juno_task/scripts', name)))).toBe(identity.targetSha256);
    }
  });

  it('fails closed on a mixed installed package identity without changing controller bytes', async () => {
    root = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-mixed-package-recovery-'));
    const { packageScriptsDir } = await createTargetBoundMetadataController(root);
    vi.spyOn(ScriptInstaller as any, 'getPackageScriptsDir').mockReturnValue(packageScriptsDir);
    const identity = await fs.readJson(path.join(root, '.juno_task/runtime/identity.json'));
    const packageRoot = path.resolve(path.dirname(identity.executable), '..', '..');
    await fs.writeJson(path.join(packageRoot, 'package.json'), {
      name: '@yylo/cli', version: '9.9.9',
    });
    const manifestBefore = await fs.readFile(path.join(root, '.juno_task/managed-assets.json'));
    const scriptBefore = await fs.readFile(path.join(root, '.juno_task/scripts/merge_queue.py'));

    await expect(ScriptInstaller.preflightUpdate(root, true)).rejects.toThrow(
      /No exact target-bound recovery provenance.*mixed with the invoked package identity/s,
    );
    expect(await fs.readFile(path.join(root, '.juno_task/managed-assets.json')))
      .toEqual(manifestBefore);
    expect(await fs.readFile(path.join(root, '.juno_task/scripts/merge_queue.py')))
      .toEqual(scriptBefore);
  });

  it('fails closed when invoked package source is not the registered runtime', async () => {
    root = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-mixed-invocation-recovery-'));
    await createTargetBoundMetadataController(root);
    const manifestBefore = await fs.readFile(path.join(root, '.juno_task/managed-assets.json'));
    const scriptBefore = await fs.readFile(path.join(root, '.juno_task/scripts/merge_queue.py'));

    await expect(ScriptInstaller.preflightUpdate(root, true)).rejects.toThrow(
      /No exact target-bound recovery provenance.*not the registered routed runtime/s,
    );
    expect(await fs.readFile(path.join(root, '.juno_task/managed-assets.json')))
      .toEqual(manifestBefore);
    expect(await fs.readFile(path.join(root, '.juno_task/scripts/merge_queue.py')))
      .toEqual(scriptBefore);
  });

  it('fails closed on a mixed target generation without changing controller bytes', async () => {
    root = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-mixed-recovery-'));
    const { packageScriptsDir } = await createTargetBoundMetadataController(root);
    vi.spyOn(ScriptInstaller as any, 'getPackageScriptsDir').mockReturnValue(packageScriptsDir);
    const generationPath = path.join(
      root, '.juno_task/runtime/managed-controller/generation.json',
    );
    const generation = await fs.readJson(generationPath);
    generation.scripts['.juno_task/scripts/merge_queue.py'].source_sha256 = '0'.repeat(64);
    await fs.writeJson(generationPath, generation);
    const manifestBefore = await fs.readFile(path.join(root, '.juno_task/managed-assets.json'));
    const scriptBefore = await fs.readFile(path.join(root, '.juno_task/scripts/merge_queue.py'));

    await expect(ScriptInstaller.preflightUpdate(root, true)).rejects.toThrow(
      /No exact target-bound recovery provenance.*mixed target provenance/s,
    );
    expect(await fs.readFile(path.join(root, '.juno_task/managed-assets.json')))
      .toEqual(manifestBefore);
    expect(await fs.readFile(path.join(root, '.juno_task/scripts/merge_queue.py')))
      .toEqual(scriptBefore);
  });

  it('fails closed when the generation task-policy binding is mixed', async () => {
    root = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-mixed-policy-recovery-'));
    const { packageScriptsDir } = await createTargetBoundMetadataController(root);
    vi.spyOn(ScriptInstaller as any, 'getPackageScriptsDir').mockReturnValue(packageScriptsDir);
    const generationPath = path.join(
      root, '.juno_task/runtime/managed-controller/generation.json',
    );
    const generation = await fs.readJson(generationPath);
    generation.policy_sha256 = '0'.repeat(64);
    await fs.writeJson(generationPath, generation);
    const manifestBefore = await fs.readFile(path.join(root, '.juno_task/managed-assets.json'));
    const scriptBefore = await fs.readFile(path.join(root, '.juno_task/scripts/task_workspace.py'));

    await expect(ScriptInstaller.preflightUpdate(root, true)).rejects.toThrow(
      /No exact target-bound recovery provenance.*task policy has mixed recovery provenance/s,
    );
    expect(await fs.readFile(path.join(root, '.juno_task/managed-assets.json')))
      .toEqual(manifestBefore);
    expect(await fs.readFile(path.join(root, '.juno_task/scripts/task_workspace.py')))
      .toEqual(scriptBefore);
  });
});
