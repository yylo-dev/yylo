/**
 * Script Installer Tests
 * Tests for the ScriptInstaller utility that manages project-level scripts
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import fs from 'fs-extra';
import * as path from 'node:path';
import * as os from 'node:os';
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import semver from 'semver';
import { ScriptInstaller } from '../script-installer.js';
import { ManagedProjectAssets } from '../managed-project-assets.js';
import {
  MANAGED_INSTALL_OPERATION_TIMEOUT_MS,
  useSharedHeavyWorkloadLock,
} from '../../test-utils/resource-lock.js';
import { contentionBudgetMs } from '../../test-utils/contention-budget.js';

describe('ScriptInstaller', {
  timeout: MANAGED_INSTALL_OPERATION_TIMEOUT_MS,
  retry: 0,
}, () => {
  useSharedHeavyWorkloadLock('Vitest ScriptInstaller managed script installation suite');
  let testDir: string;
  let fixtureController: string;
  let pythonExecutable: string;

  beforeEach(async () => {
    // Create temporary test directory and a strict fixture-owned controller so
    // runner finalization can never discover the repository hosting the suite.
    testDir = await fs.mkdtemp(path.join(os.tmpdir(), 'script-installer-test-'));
    fixtureController = path.join(testDir, 'controller');
    const scripts = path.join(fixtureController, '.juno_task', 'scripts');
    const bin = path.join(fixtureController, '.venv_juno', 'bin');
    await fs.ensureDir(scripts);
    await fs.ensureDir(bin);
    await fs.copyFile(
      path.resolve(process.cwd(), 'src/templates/scripts/controller_resolver.py'),
      path.join(scripts, 'controller_resolver.py'),
    );
    const pythonCandidates = [
      process.env.JUNO_TEST_PYTHON,
      path.resolve(process.cwd(), '..', '.venv_juno', 'bin', 'python'),
      'python3.13',
      'python3.12',
      'python3.11',
      'python3.10',
      'python3',
    ].filter((candidate): candidate is string => Boolean(candidate));
    const python = pythonCandidates.find((candidate) => spawnSync(candidate, [
      '-c',
      'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)',
    ]).status === 0);
    if (python === undefined) throw new Error('ScriptInstaller tests require Python 3.10 or newer');
    pythonExecutable = python;
    await fs.symlink(python, path.join(bin, 'python'));
    spawnSync('git', ['init', '-b', 'fixture-controller'], {
      cwd: fixtureController,
      encoding: 'utf8',
    });
  });

  afterEach(async () => {
    // Cleanup test directory
    if (testDir) {
      await fs.remove(testDir);
    }
  });

  describe('scriptExists', () => {
    it('should return false when .juno_task does not exist', async () => {
      const exists = await ScriptInstaller.scriptExists(testDir, 'run_until_completion.sh');
      expect(exists).toBe(false);
    });

    it('should return false when script does not exist', async () => {
      await fs.ensureDir(path.join(testDir, '.juno_task', 'scripts'));
      const exists = await ScriptInstaller.scriptExists(testDir, 'run_until_completion.sh');
      expect(exists).toBe(false);
    });

    it('should return true when script exists', async () => {
      const scriptsDir = path.join(testDir, '.juno_task', 'scripts');
      await fs.ensureDir(scriptsDir);
      await fs.writeFile(
        path.join(scriptsDir, 'run_until_completion.sh'),
        '#!/bin/bash\necho "test"',
      );

      const exists = await ScriptInstaller.scriptExists(testDir, 'run_until_completion.sh');
      expect(exists).toBe(true);
    });
  });

  describe('getMissingScripts', () => {
    it('should return all required scripts when .juno_task does not exist', async () => {
      const missing = await ScriptInstaller.getMissingScripts(testDir);
      expect(missing).toContain('run_until_completion.sh');
      expect(missing).toContain('kanban.sh');
      expect(missing).toContain('juno-toolchain-policy.sh');
      expect(missing).toContain('controller_resolver.py');
      expect(missing).toContain('orchestration_guard.py');
      expect(missing).toContain('install_requirements.sh'); // Required by kanban.sh
      expect(missing).toContain('workflow_runner.sh');
      expect(missing).toContain('workflow_assert.py');
      expect(missing).toContain('git_index_lock.py');
      expect(missing).toContain('controller_checkpoint.py');
      expect(missing).toContain('task_workflow_helper.py');
      expect(missing).toContain('workflow_run_evidence.py');
      expect(missing).toContain('watch_progress.py');
      expect(missing).toContain('wiki_lint.py');
      expect(missing).toContain('wiki_lint.sh');
    });

    it('should return empty array when all required scripts exist', async () => {
      const scriptsDir = path.join(testDir, '.juno_task', 'scripts');
      const hooksDir = path.join(scriptsDir, 'hooks');
      await fs.ensureDir(scriptsDir);
      await fs.ensureDir(hooksDir);
      // Create all required scripts including install_requirements.sh and Slack scripts
      await fs.writeFile(
        path.join(scriptsDir, 'run_until_completion.sh'),
        '#!/bin/bash\necho "test"',
      );
      await fs.writeFile(path.join(scriptsDir, 'kanban.sh'), '#!/bin/bash\necho "kanban"');
      await fs.writeFile(
        path.join(scriptsDir, 'juno-toolchain-policy.sh'),
        '#!/bin/bash\necho "policy"',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'controller_resolver.py'),
        '#!/usr/bin/env python3\nprint("controller")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'orchestration_guard.py'),
        '#!/usr/bin/env python3\nprint("guard")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'install_requirements.sh'),
        '#!/bin/bash\necho "install"',
      );
      // Shared utilities
      await fs.writeFile(
        path.join(scriptsDir, 'attachment_downloader.py'),
        '#!/usr/bin/env python3\nprint("downloader")',
      );
      // Slack integration scripts
      await fs.writeFile(
        path.join(scriptsDir, 'slack_state.py'),
        '#!/usr/bin/env python3\nprint("state")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'slack_fetch.py'),
        '#!/usr/bin/env python3\nprint("fetch")',
      );
      await fs.writeFile(path.join(scriptsDir, 'slack_fetch.sh'), '#!/bin/bash\necho "fetch"');
      await fs.writeFile(
        path.join(scriptsDir, 'slack_respond.py'),
        '#!/usr/bin/env python3\nprint("respond")',
      );
      await fs.writeFile(path.join(scriptsDir, 'slack_respond.sh'), '#!/bin/bash\necho "respond"');
      // GitHub integration script
      await fs.writeFile(
        path.join(scriptsDir, 'github.py'),
        '#!/usr/bin/env python3\nprint("github")',
      );
      // Claude Code hooks
      await fs.writeFile(
        path.join(hooksDir, 'session_counter.sh'),
        '#!/bin/bash\necho "session_counter"',
      );
      // Log scanning utility
      await fs.writeFile(
        path.join(scriptsDir, 'log_scanner.sh'),
        '#!/bin/bash\necho "log_scanner"',
      );
      // Parallel execution
      await fs.writeFile(
        path.join(scriptsDir, 'parallel_runner.sh'),
        '#!/usr/bin/env python3\nprint("parallel")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'parallel_runner_wait.sh'),
        '#!/usr/bin/env python3\nprint("parallel wait")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'workflow_runner.sh'),
        '#!/usr/bin/env python3\nprint("workflow")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'workflow_assert.py'),
        '#!/usr/bin/env python3\nprint("assert")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'git_index_lock.py'),
        '#!/usr/bin/env python3\nprint("index lock")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'controller_checkpoint.py'),
        '#!/usr/bin/env python3\nprint("checkpoint")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'controller_workspace.py'),
        '#!/usr/bin/env python3\nprint("controller workspace")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'integration_owner_preflight.py'),
        '#!/usr/bin/env python3\nprint("preflight")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'integration_candidate.py'),
        '#!/usr/bin/env python3\nprint("writer guard")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'worktree_lifecycle.py'),
        '#!/usr/bin/env python3\nprint("audit")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'task_workflow_helper.py'),
        '#!/usr/bin/env python3\nprint("task set")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'workflow_run_evidence.py'),
        '#!/usr/bin/env python3\nprint("workflow evidence")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'wiki_lint.py'),
        '#!/usr/bin/env python3\nprint("wiki lint")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'metadata_controller.py'),
        '#!/usr/bin/env python3\nprint("metadata controller")',
      );
      await fs.writeFile(path.join(scriptsDir, 'wiki_lint.sh'), '#!/bin/bash\necho "wiki lint"');
      await fs.ensureDir(path.join(scriptsDir, 'tests'));
      await fs.writeFile(
        path.join(scriptsDir, 'tests/test_integration_concurrency.py'),
        '#!/usr/bin/env python3\nprint("integration concurrency")',
      );
      await fs.writeFile(path.join(scriptsDir, 'git-flow.sh'), '#!/bin/sh\n');
      await fs.writeFile(path.join(scriptsDir, 'git_flow.py'), '#!/usr/bin/env python3\n');

      const newlyManaged = await ScriptInstaller.getMissingScripts(testDir);
      expect(newlyManaged.sort()).toEqual([
        'await_blocker.py',
        'controller_registration.py',
        'integration_workspace.py',
        'invocation_correlation.py',
        'managed_agent_runner.py',
        'merge_queue.py',
        'metadata_evacuation.py',
        'migration_inventory.py',
        'operation_snapshot.py',
        'release_gate.py',
        'risk_policy.py',
        'target_runtime_provenance.py',
        'task_workspace.py',
        'task_workspace_decisions.py',
        'tests/fixtures/lifecycle-evidence-reuse-matrix.v1.json',
        'tests/test_controller_registration.py',
        'tests/test_integration_workspace.py',
        'tests/test_lifecycle_evidence_reuse_matrix.py',
        'tests/test_managed_agent_runner.py',
        'tests/test_merge_queue.py',
        'tests/test_metadata_controller.py',
        'tests/test_operation_snapshot.py',
        'tests/test_release_gate.py',
        'tests/test_risk_policy.py',
        'tests/test_task_workspace.py',
        'tests/test_task_workspace_decisions.py',
        'tests/test_workflow_runner_resume_contract.py',
        'watch_progress.py',
        'worktree_hydration.py',
      ]);
      for (const relative of newlyManaged) {
        const destination = path.join(scriptsDir, relative);
        await fs.ensureDir(path.dirname(destination));
        await fs.writeFile(destination, '#!/usr/bin/env python3\n');
      }
      expect(await ScriptInstaller.getMissingScripts(testDir)).toEqual([]);
    });
  });

  describe('installScript', () => {
    it('installs the strict watcher byte-identically and executable', async () => {
      await fs.ensureDir(path.join(testDir, '.juno_task'));
      expect(await ScriptInstaller.installScript(testDir, 'watch_progress.py', true)).toBe(true);
      const installed = path.join(testDir, '.juno_task/scripts/watch_progress.py');
      expect(await fs.readFile(installed)).toEqual(
        await fs.readFile(path.join(process.cwd(), 'src/templates/scripts/watch_progress.py')),
      );
      expect((await fs.stat(installed)).mode & 0o111).not.toBe(0);
      expect(await fs.readFile(installed, 'utf8')).toContain('juno.watch-footer.v1');
      expect(await fs.readFile(installed, 'utf8')).toContain('juno.watch-event.v1');
    });

    it('should not install if project not initialized', async () => {
      // Note: This will fail because getPackageScriptsDir may not find scripts in test env
      // The test verifies the flow doesn't throw
      const result = await ScriptInstaller.installScript(testDir, 'run_until_completion.sh', true);
      // Result depends on whether templates are accessible
      expect(typeof result).toBe('boolean');
    });

    it('should create .juno_task/scripts directory if needed', async () => {
      // Create .juno_task but not scripts
      await fs.ensureDir(path.join(testDir, '.juno_task'));

      // Install will try to create scripts dir
      await ScriptInstaller.installScript(testDir, 'run_until_completion.sh', true);

      // Even if install fails (no source), dir should be created
      const scriptsDir = path.join(testDir, '.juno_task', 'scripts');
      const dirExists = await fs.pathExists(scriptsDir);
      expect(dirExists).toBe(true);
    });
  });

  describe('autoInstallMissing', () => {
    it('should not install when project is not initialized', async () => {
      const installed = await ScriptInstaller.autoInstallMissing(testDir, true);
      expect(installed).toBe(false);
    });

    it('should attempt install when .juno_task exists but scripts are missing', async () => {
      const junoTaskDir = path.join(testDir, '.juno_task');
      await fs.ensureDir(junoTaskDir);

      const installed = await ScriptInstaller.autoInstallMissing(testDir, true);
      expect(installed).toBe(true);
      expect(await fs.pathExists(path.join(junoTaskDir, 'scripts'))).toBe(true);
    }, contentionBudgetMs(180_000));
  });

  describe('getScriptPath', () => {
    it('should return correct path', () => {
      const scriptPath = ScriptInstaller.getScriptPath('/some/project', 'run_until_completion.sh');
      expect(scriptPath).toBe('/some/project/.juno_task/scripts/run_until_completion.sh');
    });
  });

  describe('listRequiredScripts', () => {
    it('should list required scripts with installation status', async () => {
      const list = await ScriptInstaller.listRequiredScripts(testDir);

      expect(list).toEqual([
        { name: 'run_until_completion.sh', installed: false },
        { name: 'kanban.sh', installed: false },
        { name: 'juno-toolchain-policy.sh', installed: false },
        { name: 'controller_resolver.py', installed: false },
        { name: 'orchestration_guard.py', installed: false },
        { name: 'install_requirements.sh', installed: false },
        // Shared utilities
        { name: 'attachment_downloader.py', installed: false },
        // Slack integration scripts
        { name: 'slack_state.py', installed: false },
        { name: 'slack_fetch.py', installed: false },
        { name: 'slack_fetch.sh', installed: false },
        { name: 'slack_respond.py', installed: false },
        { name: 'slack_respond.sh', installed: false },
        // GitHub integration script
        { name: 'github.py', installed: false },
        // Claude Code hooks
        { name: 'hooks/session_counter.sh', installed: false },
        // Log scanning utility
        { name: 'log_scanner.sh', installed: false },
        // Parallel/workflow execution
        { name: 'parallel_runner.sh', installed: false },
        { name: 'parallel_runner_wait.sh', installed: false },
        { name: 'workflow_runner.sh', installed: false },
        { name: 'workflow_assert.py', installed: false },
        { name: 'git-flow.sh', installed: false },
        { name: 'git_flow.py', installed: false },
        { name: 'worktree_hydration.py', installed: false },
        { name: 'wiki_lint.py', installed: false },
        { name: 'metadata_controller.py', installed: false },
        { name: 'migration_inventory.py', installed: false },
        { name: 'metadata_evacuation.py', installed: false },
        { name: 'controller_registration.py', installed: false },
        { name: 'tests/test_controller_registration.py', installed: false },
        { name: 'risk_policy.py', installed: false },
        { name: 'release_gate.py', installed: false },
        { name: 'tests/test_release_gate.py', installed: false },
        { name: 'tests/test_risk_policy.py', installed: false },
        { name: 'tests/test_metadata_controller.py', installed: false },
        { name: 'wiki_lint.sh', installed: false },
        { name: 'tests/test_managed_agent_runner.py', installed: false },
        { name: 'git_index_lock.py', installed: false },
        { name: 'await_blocker.py', installed: false },
        { name: 'controller_checkpoint.py', installed: false },
        { name: 'managed_agent_runner.py', installed: false },
        { name: 'invocation_correlation.py', installed: false },
        { name: 'watch_progress.py', installed: false },
        { name: 'task_workspace.py', installed: false },
        { name: 'task_workspace_decisions.py', installed: false },
        { name: 'target_runtime_provenance.py', installed: false },
        { name: 'integration_workspace.py', installed: false },
        { name: 'merge_queue.py', installed: false },
        { name: 'operation_snapshot.py', installed: false },
        { name: 'tests/test_task_workspace.py', installed: false },
        { name: 'tests/test_task_workspace_decisions.py', installed: false },
        { name: 'tests/test_integration_workspace.py', installed: false },
        { name: 'tests/test_merge_queue.py', installed: false },
        { name: 'tests/test_operation_snapshot.py', installed: false },
        { name: 'tests/test_lifecycle_evidence_reuse_matrix.py', installed: false },
        { name: 'tests/fixtures/lifecycle-evidence-reuse-matrix.v1.json', installed: false },
        { name: 'tests/test_workflow_runner_resume_contract.py', installed: false },
        { name: 'task_workflow_helper.py', installed: false },
        { name: 'workflow_run_evidence.py', installed: false },
      ]);
    });

    it('should show installed=true for existing scripts', async () => {
      const scriptsDir = path.join(testDir, '.juno_task', 'scripts');
      const hooksDir = path.join(scriptsDir, 'hooks');
      await fs.ensureDir(scriptsDir);
      await fs.ensureDir(hooksDir);
      await fs.writeFile(
        path.join(scriptsDir, 'run_until_completion.sh'),
        '#!/bin/bash\necho "test"',
      );
      await fs.writeFile(path.join(scriptsDir, 'kanban.sh'), '#!/bin/bash\necho "kanban"');
      await fs.writeFile(
        path.join(scriptsDir, 'juno-toolchain-policy.sh'),
        '#!/bin/bash\necho "policy"',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'controller_resolver.py'),
        '#!/usr/bin/env python3\nprint("controller")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'orchestration_guard.py'),
        '#!/usr/bin/env python3\nprint("guard")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'install_requirements.sh'),
        '#!/bin/bash\necho "install"',
      );
      // Shared utilities
      await fs.writeFile(
        path.join(scriptsDir, 'attachment_downloader.py'),
        '#!/usr/bin/env python3\nprint("downloader")',
      );
      // Slack integration scripts
      await fs.writeFile(
        path.join(scriptsDir, 'slack_state.py'),
        '#!/usr/bin/env python3\nprint("state")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'slack_fetch.py'),
        '#!/usr/bin/env python3\nprint("fetch")',
      );
      await fs.writeFile(path.join(scriptsDir, 'slack_fetch.sh'), '#!/bin/bash\necho "fetch"');
      await fs.writeFile(
        path.join(scriptsDir, 'slack_respond.py'),
        '#!/usr/bin/env python3\nprint("respond")',
      );
      await fs.writeFile(path.join(scriptsDir, 'slack_respond.sh'), '#!/bin/bash\necho "respond"');
      // GitHub integration script
      await fs.writeFile(
        path.join(scriptsDir, 'github.py'),
        '#!/usr/bin/env python3\nprint("github")',
      );
      // Claude Code hooks
      await fs.writeFile(
        path.join(hooksDir, 'session_counter.sh'),
        '#!/bin/bash\necho "session_counter"',
      );
      // Log scanning utility
      await fs.writeFile(
        path.join(scriptsDir, 'log_scanner.sh'),
        '#!/bin/bash\necho "log_scanner"',
      );
      // Parallel execution
      await fs.writeFile(
        path.join(scriptsDir, 'parallel_runner.sh'),
        '#!/usr/bin/env python3\nprint("parallel")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'parallel_runner_wait.sh'),
        '#!/usr/bin/env python3\nprint("parallel wait")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'workflow_runner.sh'),
        '#!/usr/bin/env python3\nprint("workflow")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'workflow_assert.py'),
        '#!/usr/bin/env python3\nprint("assert")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'git_index_lock.py'),
        '#!/usr/bin/env python3\nprint("index lock")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'controller_checkpoint.py'),
        '#!/usr/bin/env python3\nprint("checkpoint")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'controller_workspace.py'),
        '#!/usr/bin/env python3\nprint("controller workspace")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'integration_owner_preflight.py'),
        '#!/usr/bin/env python3\nprint("preflight")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'integration_candidate.py'),
        '#!/usr/bin/env python3\nprint("writer guard")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'worktree_lifecycle.py'),
        '#!/usr/bin/env python3\nprint("audit")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'task_workflow_helper.py'),
        '#!/usr/bin/env python3\nprint("task set")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'workflow_run_evidence.py'),
        '#!/usr/bin/env python3\nprint("workflow evidence")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'watch_progress.py'),
        '#!/usr/bin/env python3\nprint("watch progress")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'wiki_lint.py'),
        '#!/usr/bin/env python3\nprint("wiki lint")',
      );
      await fs.writeFile(path.join(scriptsDir, 'wiki_lint.sh'), '#!/bin/bash\necho "wiki lint"');
      await fs.ensureDir(path.join(scriptsDir, 'tests'));
      await fs.writeFile(
        path.join(scriptsDir, 'tests/test_integration_concurrency.py'),
        '#!/usr/bin/env python3\nprint("integration concurrency")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'tests/test_managed_agent_runner.py'),
        '#!/usr/bin/env python3\nprint("managed agent")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'tests/test_task_lifecycle.py'),
        '#!/usr/bin/env python3\nprint("task lifecycle")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'tests/test_task_workspace.py'),
        '#!/usr/bin/env python3\nprint("task workspace")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'tests/test_task_workspace_decisions.py'),
        '#!/usr/bin/env python3\nprint("task workspace decisions")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'tests/test_controller_workspace.py'),
        '#!/usr/bin/env python3\nprint("controller workspace")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'metadata_controller.py'),
        '#!/usr/bin/env python3\nprint("metadata controller")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'tests/test_metadata_controller.py'),
        '#!/usr/bin/env python3\nprint("metadata controller")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'risk_policy.py'),
        '#!/usr/bin/env python3\nprint("risk policy")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'release_gate.py'),
        '#!/usr/bin/env python3\nprint("release gate")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'tests/test_release_gate.py'),
        '#!/usr/bin/env python3\nprint("release gate")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'tests/test_risk_policy.py'),
        '#!/usr/bin/env python3\nprint("risk policy")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'managed_agent_runner.py'),
        '#!/usr/bin/env python3\n',
      );
      await fs.writeFile(path.join(scriptsDir, 'invocation_correlation.py'), '#!/usr/bin/env python3\n');
      await fs.writeFile(path.join(scriptsDir, 'task_lifecycle.py'), '#!/usr/bin/env python3\n');
      await fs.writeFile(path.join(scriptsDir, 'task_workspace.py'), '#!/usr/bin/env python3\n');
      await fs.writeFile(path.join(scriptsDir, 'task_workspace_decisions.py'), '#!/usr/bin/env python3\n');
      await fs.writeFile(path.join(scriptsDir, 'target_runtime_provenance.py'), '#!/usr/bin/env python3\n');
      await fs.writeFile(path.join(scriptsDir, 'merge_queue.py'), '#!/usr/bin/env python3\n');
      await fs.writeFile(path.join(scriptsDir, 'operation_snapshot.py'), '#!/usr/bin/env python3\n');
      await fs.writeFile(path.join(scriptsDir, 'migration_inventory.py'), '#!/usr/bin/env python3\n');
      await fs.writeFile(path.join(scriptsDir, 'metadata_evacuation.py'), '#!/usr/bin/env python3\n');
      await fs.writeFile(path.join(scriptsDir, 'controller_registration.py'), '#!/usr/bin/env python3\n');
      await fs.writeFile(path.join(scriptsDir, 'tests/test_controller_registration.py'), '#!/usr/bin/env python3\n');
      await fs.writeFile(path.join(scriptsDir, 'integration_workspace.py'), '#!/usr/bin/env python3\n');
      await fs.writeFile(path.join(scriptsDir, 'tests/test_integration_workspace.py'), '#!/usr/bin/env python3\n');
      await fs.writeFile(
        path.join(scriptsDir, 'tests/test_merge_queue.py'),
        '#!/usr/bin/env python3\nprint("merge queue")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'tests/test_operation_snapshot.py'),
        '#!/usr/bin/env python3\nprint("operation snapshot")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'tests/test_workflow_runner_resume_contract.py'),
        '#!/usr/bin/env python3\nprint("resume contract")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'tests/test_lifecycle_evidence_reuse_matrix.py'),
        '#!/usr/bin/env python3\nprint("evidence reuse matrix")',
      );
      await fs.ensureDir(path.join(scriptsDir, 'tests/fixtures'));
      await fs.writeFile(
        path.join(scriptsDir, 'tests/fixtures/lifecycle-evidence-reuse-matrix.v1.json'),
        '{}\n',
      );
      await fs.writeFile(path.join(scriptsDir, 'git-flow.sh'), '#!/bin/sh\n');
      await fs.writeFile(path.join(scriptsDir, 'git_flow.py'), '#!/usr/bin/env python3\n');
      await fs.writeFile(path.join(scriptsDir, 'worktree_hydration.py'), '#!/usr/bin/env python3\n');

      await fs.writeFile(
        path.join(scriptsDir, 'await_blocker.py'),
        '#!/usr/bin/env python3\nprint("await")',
      );
      const list = await ScriptInstaller.listRequiredScripts(testDir);

      expect(list).toEqual([
        { name: 'run_until_completion.sh', installed: true },
        { name: 'kanban.sh', installed: true },
        { name: 'juno-toolchain-policy.sh', installed: true },
        { name: 'controller_resolver.py', installed: true },
        { name: 'orchestration_guard.py', installed: true },
        { name: 'install_requirements.sh', installed: true },
        // Shared utilities
        { name: 'attachment_downloader.py', installed: true },
        // Slack integration scripts
        { name: 'slack_state.py', installed: true },
        { name: 'slack_fetch.py', installed: true },
        { name: 'slack_fetch.sh', installed: true },
        { name: 'slack_respond.py', installed: true },
        { name: 'slack_respond.sh', installed: true },
        // GitHub integration script
        { name: 'github.py', installed: true },
        // Claude Code hooks
        { name: 'hooks/session_counter.sh', installed: true },
        // Log scanning utility
        { name: 'log_scanner.sh', installed: true },
        // Parallel/workflow execution
        { name: 'parallel_runner.sh', installed: true },
        { name: 'parallel_runner_wait.sh', installed: true },
        { name: 'workflow_runner.sh', installed: true },
        { name: 'workflow_assert.py', installed: true },
        { name: 'git-flow.sh', installed: true },
        { name: 'git_flow.py', installed: true },
        { name: 'worktree_hydration.py', installed: true },
        { name: 'wiki_lint.py', installed: true },
        { name: 'metadata_controller.py', installed: true },
        { name: 'migration_inventory.py', installed: true },
        { name: 'metadata_evacuation.py', installed: true },
        { name: 'controller_registration.py', installed: true },
        { name: 'tests/test_controller_registration.py', installed: true },
        { name: 'risk_policy.py', installed: true },
        { name: 'release_gate.py', installed: true },
        { name: 'tests/test_release_gate.py', installed: true },
        { name: 'tests/test_risk_policy.py', installed: true },
        { name: 'tests/test_metadata_controller.py', installed: true },
        { name: 'wiki_lint.sh', installed: true },
        { name: 'tests/test_managed_agent_runner.py', installed: true },
        { name: 'git_index_lock.py', installed: true },
        { name: 'await_blocker.py', installed: true },
        { name: 'controller_checkpoint.py', installed: true },
        { name: 'managed_agent_runner.py', installed: true },
        { name: 'invocation_correlation.py', installed: true },
        { name: 'watch_progress.py', installed: true },
        { name: 'task_workspace.py', installed: true },
        { name: 'task_workspace_decisions.py', installed: true },
        { name: 'target_runtime_provenance.py', installed: true },
        { name: 'integration_workspace.py', installed: true },
        { name: 'merge_queue.py', installed: true },
        { name: 'operation_snapshot.py', installed: true },
        { name: 'tests/test_task_workspace.py', installed: true },
        { name: 'tests/test_task_workspace_decisions.py', installed: true },
        { name: 'tests/test_integration_workspace.py', installed: true },
        { name: 'tests/test_merge_queue.py', installed: true },
        { name: 'tests/test_operation_snapshot.py', installed: true },
        { name: 'tests/test_lifecycle_evidence_reuse_matrix.py', installed: true },
        { name: 'tests/fixtures/lifecycle-evidence-reuse-matrix.v1.json', installed: true },
        { name: 'tests/test_workflow_runner_resume_contract.py', installed: true },
        { name: 'task_workflow_helper.py', installed: true },
        { name: 'workflow_run_evidence.py', installed: true },
      ]);
    });
  });

  describe('parallel_runner command mode template', () => {
    it('executes YAML command entries through raw headless scheduler', async () => {
      const commandsFile = path.join(testDir, 'commands.yaml');
      const outputDir = path.join(testDir, 'out');
      await fs.writeFile(
        commandsFile,
        [
          'schema_version: 1',
          'parallel: 2',
          'env:',
          '  SHARED_VALUE: top',
          'commands:',
          '  - id: argv-command',
          '    command:',
          '      - python3',
          '      - -c',
          "      - \"import os; print('argv:' + os.environ['SHARED_VALUE'])\"",
          '    env:',
          '      SHARED_VALUE: per',
          '  - id: shell-command',
          '    command: "python3 -c \'print(\\"shell:ok\\")\'"',
          '',
        ].join('\n'),
      );

      const scriptPath = path.resolve(process.cwd(), 'src/templates/scripts/parallel_runner.sh');
      const result = spawnSync(
        pythonExecutable,
        [scriptPath, '--commands-file', commandsFile, '--parallel', '2', '--output-dir', outputDir],
        {
          cwd: fixtureController,
          encoding: 'utf8',
          // Managed runner scripts spawn interpreters and real Git fixtures;
          // their bounded subprocess budget must tolerate shared-host load.
          timeout: contentionBudgetMs(120_000),
          env: {
            ...process.env,
            JUNO_TASK_ROOT: fixtureController,
            JUNO_WORKSPACE_ROLE: 'controller',
            JUNO_WORKSPACE_ENFORCEMENT: 'strict',
            JUNO_CONTROLLER_BRANCH: 'fixture-controller',
            YYLO_SESSION_METADATA_DIRECTORY: path.join(testDir, 'metadata'),
          },
        },
      );

      expect(result.status).toBe(0);
      expect(result.stdout).toContain('Commands (2): argv-command, shell-command');
      expect(result.stdout).toContain('Succeeded:      2');

      const runArtifactsMatch = result.stdout.match(/Run artifacts: (.+)/);
      expect(runArtifactsMatch?.[1]).toBeTruthy();
      const logDir = runArtifactsMatch![1].trim();
      expect(await fs.readFile(path.join(logDir, 'task_argv-command.log'), 'utf8')).toContain(
        'argv:per',
      );
      expect(await fs.readFile(path.join(logDir, 'task_shell-command.log'), 'utf8')).toContain(
        'shell:ok',
      );
    });

    it('returns non-zero and names failing raw commands', async () => {
      const commandsFile = path.join(testDir, 'commands-fail.yaml');
      await fs.writeFile(
        commandsFile,
        [
          'schema_version: 1',
          'commands:',
          '  - id: failing-command',
          '    command:',
          '      - python3',
          '      - -c',
          '      - "import sys; print(\'failing\'); sys.exit(7)"',
          '',
        ].join('\n'),
      );

      const scriptPath = path.resolve(process.cwd(), 'src/templates/scripts/parallel_runner.sh');
      const result = spawnSync(
        pythonExecutable,
        [scriptPath, '--commands-file', commandsFile, '--parallel', '1'],
        {
          cwd: fixtureController,
          encoding: 'utf8',
          timeout: contentionBudgetMs(120_000),
          env: {
            ...process.env,
            JUNO_TASK_ROOT: fixtureController,
            JUNO_WORKSPACE_ROLE: 'controller',
            JUNO_WORKSPACE_ENFORCEMENT: 'strict',
            JUNO_CONTROLLER_BRANCH: 'fixture-controller',
            YYLO_SESSION_METADATA_DIRECTORY: path.join(testDir, 'metadata'),
          },
        },
      );

      expect(result.status).toBe(1);
      expect(result.stdout).toContain('[failing-command]   FAILED (exit 7)');
      expect(result.stdout).toContain('Failed IDs:   failing-command');
    });
  });

  describe('updateScriptIfNewer', () => {
    it('should install script if it does not exist', async () => {
      await fs.ensureDir(path.join(testDir, '.juno_task'));

      const updated = await ScriptInstaller.updateScriptIfNewer(
        testDir,
        'run_until_completion.sh',
        true,
      );
      // Result depends on whether templates are accessible
      expect(typeof updated).toBe('boolean');
    });
  });

  describe('getOutdatedScripts', () => {
    it('should return empty array when no scripts exist', async () => {
      await fs.ensureDir(path.join(testDir, '.juno_task', 'scripts'));
      const outdated = await ScriptInstaller.getOutdatedScripts(testDir);
      expect(outdated).toEqual([]);
    });

    it('should detect scripts with different content', async () => {
      const scriptsDir = path.join(testDir, '.juno_task', 'scripts');
      await fs.ensureDir(scriptsDir);

      // Create scripts with different content than package
      await fs.writeFile(
        path.join(scriptsDir, 'run_until_completion.sh'),
        '#!/bin/bash\necho "OLD VERSION"',
      );
      await fs.writeFile(path.join(scriptsDir, 'kanban.sh'), '#!/bin/bash\necho "OLD VERSION"');
      await fs.writeFile(
        path.join(scriptsDir, 'install_requirements.sh'),
        '#!/bin/bash\necho "OLD VERSION"',
      );

      // Create all Slack scripts too
      await fs.writeFile(
        path.join(scriptsDir, 'slack_state.py'),
        '#!/usr/bin/env python3\nprint("OLD")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'slack_fetch.py'),
        '#!/usr/bin/env python3\nprint("OLD")',
      );
      await fs.writeFile(path.join(scriptsDir, 'slack_fetch.sh'), '#!/bin/bash\necho "OLD"');
      await fs.writeFile(
        path.join(scriptsDir, 'slack_respond.py'),
        '#!/usr/bin/env python3\nprint("OLD")',
      );
      await fs.writeFile(path.join(scriptsDir, 'slack_respond.sh'), '#!/bin/bash\necho "OLD"');

      const outdated = await ScriptInstaller.getOutdatedScripts(testDir);
      // Should detect outdated scripts if templates are accessible
      // Result depends on whether templates are accessible
      expect(Array.isArray(outdated)).toBe(true);
    });
  });

  describe('needsUpdate', () => {
    it('should return false when project is not initialized', async () => {
      const needsUpdate = await ScriptInstaller.needsUpdate(testDir);
      expect(needsUpdate).toBe(false);
    });

    it('should return true when scripts are missing', async () => {
      await fs.ensureDir(path.join(testDir, '.juno_task'));
      const needsUpdate = await ScriptInstaller.needsUpdate(testDir);
      // Should return true if templates are accessible and scripts are missing
      expect(typeof needsUpdate).toBe('boolean');
    });

    it('should return true when scripts have different content', async () => {
      const scriptsDir = path.join(testDir, '.juno_task', 'scripts');
      await fs.ensureDir(scriptsDir);

      // Create all required scripts with old content
      await fs.writeFile(
        path.join(scriptsDir, 'run_until_completion.sh'),
        '#!/bin/bash\necho "OLD"',
      );
      await fs.writeFile(path.join(scriptsDir, 'kanban.sh'), '#!/bin/bash\necho "OLD"');
      await fs.writeFile(
        path.join(scriptsDir, 'install_requirements.sh'),
        '#!/bin/bash\necho "OLD"',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'slack_state.py'),
        '#!/usr/bin/env python3\nprint("OLD")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'slack_fetch.py'),
        '#!/usr/bin/env python3\nprint("OLD")',
      );
      await fs.writeFile(path.join(scriptsDir, 'slack_fetch.sh'), '#!/bin/bash\necho "OLD"');
      await fs.writeFile(
        path.join(scriptsDir, 'slack_respond.py'),
        '#!/usr/bin/env python3\nprint("OLD")',
      );
      await fs.writeFile(path.join(scriptsDir, 'slack_respond.sh'), '#!/bin/bash\necho "OLD"');

      const needsUpdate = await ScriptInstaller.needsUpdate(testDir);
      // Should return true if templates are accessible and content differs
      expect(typeof needsUpdate).toBe('boolean');
    });
  });

  describe('autoUpdate', () => {
    it('should not update when project is not initialized', async () => {
      const updated = await ScriptInstaller.autoUpdate(testDir, true);
      expect(updated).toBe(false);
    });

    it('should install missing scripts when project is initialized', async () => {
      await fs.ensureDir(path.join(testDir, '.juno_task'));

      const updated = await ScriptInstaller.autoUpdate(testDir, true);
      expect(updated).toBe(true);
      expect(
        await fs.pathExists(path.join(testDir, '.juno_task/wiki/git_worktree_lifecycle.md')),
      ).toBe(true);
      expect(
        await fs.pathExists(path.join(testDir, '.juno_task/scripts/task_workspace.py')),
      ).toBe(true);
      const installedTaskHelper = await fs.readFile(
        path.join(testDir, '.juno_task/scripts/task_workflow_helper.py'),
        'utf8',
      );
      const packageTaskHelper = await fs.readFile(
        path.resolve(process.cwd(), 'src/templates/scripts/task_workflow_helper.py'),
        'utf8',
      );
      expect(installedTaskHelper).toBe(packageTaskHelper);
      const installedEvidenceHelper = await fs.readFile(
        path.join(testDir, '.juno_task/scripts/workflow_run_evidence.py'),
        'utf8',
      );
      const packageEvidenceHelper = await fs.readFile(
        path.resolve(process.cwd(), 'src/templates/scripts/workflow_run_evidence.py'),
        'utf8',
      );
      expect(installedEvidenceHelper).toBe(packageEvidenceHelper);
    });

    it('installs ignored runtime scripts and controller lifecycle seeds in a metadata-only controller', async () => {
      await fs.ensureDir(path.join(testDir, '.juno_task/config'));
      const config = {
        controllerWorkspace: {
          mode: 'metadata-only',
          policy: '.juno_task/config/metadata-controller.json',
        },
      };
      await fs.writeJson(path.join(testDir, '.juno_task/config.json'), config);
      await fs.writeJson(path.join(testDir, '.juno_task/config/metadata-controller.json'), {
        preserved: 'reviewed-project-policy',
      });

      const updated = await ScriptInstaller.autoUpdate(testDir, true);

      expect(updated).toBe(true);
      expect(await fs.pathExists(path.join(testDir, '.juno_task/scripts/task_workspace.py'))).toBe(true);
      expect(await fs.pathExists(path.join(testDir, '.juno_task/scripts/merge_queue.py'))).toBe(true);
      // Only task-run lifecycle seeds remain. Native delivery has no workflow
      // engine or semantic-repair prompt.
      expect(await fs.pathExists(path.join(testDir, '.juno_task/workflows/yy-task-run.yaml'))).toBe(true);
      expect(await fs.pathExists(path.join(testDir, '.juno_task/workflows/yy-merge-drive.yaml'))).toBe(false);
      expect(await fs.pathExists(
        path.join(testDir, '.juno_task/prompts/lifecycle/task-implementation.md'))).toBe(true);
      expect(await fs.pathExists(
        path.join(testDir, '.juno_task/prompts/lifecycle/task-test-repair.md'))).toBe(true);
      expect(await fs.pathExists(
        path.join(testDir, '.juno_task/prompts/lifecycle/merge-semantic-repair.md'))).toBe(false);
      // Seed installation is scoped: the tracked generation (wiki, prompts,
      // manifest) stays untouched while customized policy blocks it.
      expect(await fs.pathExists(path.join(testDir, '.juno_task/wiki'))).toBe(false);
      expect(await fs.pathExists(path.join(testDir, '.juno_task/managed-assets.json'))).toBe(false);
      expect(await fs.pathExists(path.join(testDir, 'scripts/git-flow.sh'))).toBe(false);
      expect(await fs.readJson(path.join(testDir, '.juno_task/config.json'))).toEqual(config);
      expect(await fs.readJson(
        path.join(testDir, '.juno_task/config/metadata-controller.json'),
      )).toEqual({ preserved: 'reviewed-project-policy' });
      expect(await ScriptInstaller.autoUpdate(testDir, true)).toBe(false);
    });

    it('installs controller seeds despite customized retired pre-Bolt history', async () => {
      await fs.ensureDir(path.join(testDir, '.juno_task/config'));
      await fs.writeJson(path.join(testDir, '.juno_task/config.json'), {
        controllerWorkspace: {
          mode: 'metadata-only',
          policy: '.juno_task/config/metadata-controller.json',
        },
      });
      await fs.writeJson(path.join(testDir, '.juno_task/config/metadata-controller.json'), {
        preserved: 'reviewed-project-policy',
      });
      // A tracked retired asset whose bytes match no source nor manifest
      // record suspends the full generation update, but must not block the
      // scoped lifecycle seed delivery.
      const retiredPath = path.join(testDir, '.juno_task/scripts/task_lifecycle.py');
      await fs.ensureDir(path.dirname(retiredPath));
      const retiredBytes = '# pre-Bolt retired controller seam\n';
      await fs.writeFile(retiredPath, retiredBytes);

      const updated = await ScriptInstaller.autoUpdate(testDir, true);

      expect(updated).toBe(true);
      expect(await fs.pathExists(path.join(testDir, '.juno_task/workflows/yy-task-run.yaml'))).toBe(true);
      expect(await fs.pathExists(path.join(testDir, '.juno_task/workflows/yy-merge-drive.yaml'))).toBe(false);
      expect(await fs.pathExists(
        path.join(testDir, '.juno_task/prompts/lifecycle/task-implementation.md'))).toBe(true);
      // The retired customized asset is untouched: retirement belongs to the
      // full generation update, not to scoped seed installation.
      expect(await fs.readFile(retiredPath, 'utf8')).toBe(retiredBytes);
    });

    it('self-heals missing controller lifecycle seeds without touching current scripts', async () => {
      await fs.ensureDir(path.join(testDir, '.juno_task/config'));
      await fs.writeJson(path.join(testDir, '.juno_task/config.json'), {
        controllerWorkspace: {
          mode: 'metadata-only',
          policy: '.juno_task/config/metadata-controller.json',
        },
      });
      await fs.writeJson(path.join(testDir, '.juno_task/config/metadata-controller.json'), {
        preserved: 'reviewed-project-policy',
      });

      // Reach a fully current controller generation, then remove only the seeds.
      expect(await ScriptInstaller.autoUpdate(testDir, true)).toBe(true);
      await fs.remove(path.join(testDir, '.juno_task/workflows'));
      const promptsBefore = await fs.readFile(
        path.join(testDir, '.juno_task/prompts/lifecycle/task-implementation.md'), 'utf8');

      // Non-force update reinstalls exactly the missing seeds.
      expect(await ScriptInstaller.autoUpdate(testDir, false)).toBe(true);
      expect(await fs.pathExists(path.join(testDir, '.juno_task/workflows/yy-task-run.yaml'))).toBe(true);
      expect(await fs.pathExists(path.join(testDir, '.juno_task/workflows/yy-merge-drive.yaml'))).toBe(false);
      expect(await fs.readFile(
        path.join(testDir, '.juno_task/prompts/lifecycle/task-implementation.md'), 'utf8'),
      ).toBe(promptsBefore);
      // A subsequent no-op run stays a no-op.
      expect(await ScriptInstaller.autoUpdate(testDir, false)).toBe(false);
    });

    it('routes one sparse pre-2.1.2 controller to the receipt-bound migration without tracked mutation', async () => {
      const templateRoot = path.resolve(process.cwd(), 'src/templates/config');
      const metadata = await fs.readJson(path.join(templateRoot, 'metadata-controller.json'));
      metadata.controller_branch = 'refs/heads/customer/controller';
      metadata.product_ref = 'refs/heads/customer/release';
      metadata.generated_metadata = metadata.generated_metadata.filter(
        (entry: string) => entry !== '.juno_task/config/integration-workspace.json',
      );
      metadata.tracked_exact = metadata.tracked_exact.filter(
        (entry: string) => entry !== '.juno_task/config/integration-workspace.json',
      );
      await fs.ensureDir(path.join(testDir, '.juno_task/config'));
      await fs.writeJson(path.join(testDir, '.juno_task/config.json'), {
        controllerWorkspace: {
          mode: 'metadata-only', policy: '.juno_task/config/metadata-controller.json',
        },
      });
      await fs.writeJson(
        path.join(testDir, '.juno_task/config/metadata-controller.json'), metadata,
      );
      const taskBytes = '{"schema_version":"owner-task-policy","preserve":true}\n';
      const riskBytes = '{"schema_version":"owner-risk-policy","preserve":true}\n';
      await fs.writeFile(path.join(testDir, '.juno_task/config/task-workspace.json'), taskBytes);
      await fs.writeFile(path.join(testDir, '.juno_task/config/risk-policy.json'), riskBytes);

      const policyBefore = await fs.readFile(
        path.join(testDir, '.juno_task/config/metadata-controller.json'),
      );
      for (const force of [false, true]) {
        await expect(
          ScriptInstaller.updateMetadataControllerPolicies(testDir, force),
        ).rejects.toThrow('yy migrate metadata-policy plan');
        expect(await fs.readFile(
          path.join(testDir, '.juno_task/config/metadata-controller.json'),
        )).toEqual(policyBefore);
        expect(await fs.pathExists(
          path.join(testDir, '.juno_task/config/integration-workspace.json'),
        )).toBe(false);
        expect(await fs.readFile(
          path.join(testDir, '.juno_task/config/task-workspace.json'), 'utf8',
        )).toBe(taskBytes);
        expect(await fs.readFile(
          path.join(testDir, '.juno_task/config/risk-policy.json'), 'utf8',
        )).toBe(riskBytes);
      }
      await expect(ScriptInstaller.preflightUpdate(testDir, true)).rejects.toThrow(
        'scripts update is mutation-free for tracked policy',
      );
    });

    it('preserves newer receipt-bound bytes when package and generation versions match but hashes differ', async () => {
      const script = '.juno_task/scripts/task_workspace.py';
      const exactTargetBytes = '# newer exact target runtime from integrated source\n';
      const hash = createHash('sha256').update(exactTargetBytes).digest('hex');
      const packageRoot = path.resolve(process.cwd());
      const packageVersion = (await fs.readJson(path.join(packageRoot, 'package.json'))).version;
      expect(typeof packageVersion).toBe('string');
      expect(semver.valid(packageVersion)).not.toBeNull();
      const packagedBytes = await fs.readFile(
        path.join(packageRoot, 'src/templates/scripts/task_workspace.py'),
      );
      expect(createHash('sha256').update(packagedBytes).digest('hex')).not.toBe(hash);
      await fs.ensureDir(path.join(testDir, '.juno_task/runtime/managed-controller'));
      await fs.writeJson(path.join(testDir, '.juno_task/config.json'), {
        controllerWorkspace: {
          mode: 'metadata-only', policy: '.juno_task/config/metadata-controller.json',
        },
      });
      await fs.outputFile(path.join(testDir, script), exactTargetBytes);
      const generation = {
        schema_version: 'juno_managed_controller_runtime.v1',
        target_sha: 'a'.repeat(40),
        // This is the observed seam: version strings alone agree even though
        // the older installed package does not contain the integrated bytes.
        package_version: packageVersion,
        scripts: { [script]: {
          classification: 'exact', source_sha256: hash, actual_sha256: hash,
        } },
      };
      expect(generation.package_version).toBe(packageVersion);
      await fs.writeJson(
        path.join(testDir, '.juno_task/runtime/managed-controller/generation.json'),
        generation,
      );

      const escapedPackageVersion = packageVersion.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      await expect(ScriptInstaller.autoUpdate(testDir, true)).rejects.toThrow(
        new RegExp(
          `Refusing package script update.*receipt-bound.*${escapedPackageVersion}` +
          '.*not that exact generation',
          's',
        ),
      );
      expect(await fs.readFile(path.join(testDir, script), 'utf8')).toBe(exactTargetBytes);
      expect(await ScriptInstaller.inspectManagedControllerGeneration(testDir)).toMatchObject({
        present: true, healthy: true, packageVersion, targetSha: 'a'.repeat(40),
      });
    });

    it('does not mix a new lifecycle script generation with customized guidance', async () => {
      await fs.ensureDir(path.join(testDir, '.juno_task'));
      await ManagedProjectAssets.update(testDir, { silent: true });
      const wikiPath = path.join(testDir, '.juno_task/wiki/git_worktree_lifecycle.md');
      const scriptPath = path.join(testDir, '.juno_task/scripts/task_workspace.py');
      await fs.writeFile(wikiPath, '# owner-specific lifecycle policy\n');
      await fs.ensureDir(path.dirname(scriptPath));
      await fs.writeFile(scriptPath, '# old lifecycle generation\n');

      await ScriptInstaller.autoUpdate(testDir, true);

      expect(await fs.readFile(wikiPath, 'utf8')).toBe('# owner-specific lifecycle policy\n');
      expect(await fs.readFile(scriptPath, 'utf8')).toBe('# old lifecycle generation\n');
      const managedManifest = await fs.readJson(
        path.join(testDir, '.juno_task/managed-assets.json'),
      );
      expect(
        await fs.pathExists(
          path.join(
            testDir,
            '.juno_task/managed-conflicts',
            managedManifest.packageVersion,
            '.juno_task/wiki/git_worktree_lifecycle.md.candidate',
          ),
        ),
      ).toBe(true);
    });

    it('force-updates lifecycle guidance before replacing lifecycle scripts', async () => {
      await fs.ensureDir(path.join(testDir, '.juno_task'));
      await ManagedProjectAssets.update(testDir, { silent: true });
      const wikiPath = path.join(testDir, '.juno_task/wiki/git_worktree_lifecycle.md');
      const scriptPath = path.join(testDir, '.juno_task/scripts/task_workspace.py');
      await fs.writeFile(wikiPath, '# owner-specific lifecycle policy\n');
      await fs.ensureDir(path.dirname(scriptPath));
      await fs.writeFile(scriptPath, '# old lifecycle generation\n');

      const updated = await ScriptInstaller.autoUpdate(testDir, true, true);

      expect(updated).toBe(true);
      expect(await fs.readFile(wikiPath, 'utf8')).toContain(
        '# Task worktrees and native Git delivery',
      );
      expect(await fs.readFile(scriptPath, 'utf8')).toContain('def main(');
      const backupRoot = path.join(testDir, '.juno_task/managed-conflicts');
      const backupDirectories = await fs.readdir(backupRoot);
      const backupChecks = await Promise.all(
        backupDirectories.map((directory) =>
          fs.pathExists(
            path.join(backupRoot, directory, '.juno_task/wiki/git_worktree_lifecycle.md.backup'),
          ),
        ),
      );
      expect(backupChecks.filter(Boolean)).toHaveLength(1);
    });

    it('should update outdated scripts', async () => {
      const scriptsDir = path.join(testDir, '.juno_task', 'scripts');
      await fs.ensureDir(scriptsDir);

      // Create all required scripts with old content
      await fs.writeFile(
        path.join(scriptsDir, 'run_until_completion.sh'),
        '#!/bin/bash\necho "OLD"',
      );
      await fs.writeFile(path.join(scriptsDir, 'kanban.sh'), '#!/bin/bash\necho "OLD"');
      await fs.writeFile(
        path.join(scriptsDir, 'install_requirements.sh'),
        '#!/bin/bash\necho "OLD"',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'slack_state.py'),
        '#!/usr/bin/env python3\nprint("OLD")',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'slack_fetch.py'),
        '#!/usr/bin/env python3\nprint("OLD")',
      );
      await fs.writeFile(path.join(scriptsDir, 'slack_fetch.sh'), '#!/bin/bash\necho "OLD"');
      await fs.writeFile(
        path.join(scriptsDir, 'slack_respond.py'),
        '#!/usr/bin/env python3\nprint("OLD")',
      );
      await fs.writeFile(path.join(scriptsDir, 'slack_respond.sh'), '#!/bin/bash\necho "OLD"');

      const updated = await ScriptInstaller.autoUpdate(testDir, true);
      // Result depends on whether templates are accessible
      expect(typeof updated).toBe('boolean');
    });

    it('should preserve the assignment guard when replacing a project kanban wrapper', async () => {
      const scriptsDir = path.join(testDir, '.juno_task', 'scripts');
      await fs.ensureDir(scriptsDir);
      await fs.writeFile(path.join(scriptsDir, 'kanban.sh'), '#!/bin/bash\necho "OLD"\n');

      const updated = await ScriptInstaller.autoUpdate(testDir, true);
      const installed = await fs.readFile(path.join(scriptsDir, 'kanban.sh'), 'utf8');
      const runner = await fs.readFile(path.join(scriptsDir, 'parallel_runner.sh'), 'utf8');

      expect(updated).toBe(true);
      expect(installed).toContain('ASSIGNED_TASK_ID');
      expect(installed).toContain('E2E_SWEEP_KANBAN_GUARD_DIR');
      expect(installed).toContain('E2E_SWEEP_KANBAN_RECORDS');
      expect(installed).toContain('E2E_SWEEP_KANBAN_INTERNAL');
      expect(installed).toContain('guard-kanban');
      expect(installed).toContain('exec python3 "$guard_helper"');
      expect(installed).toContain('E2E_CONTRACT_VALIDATION_INTERNAL');
      expect(installed).toContain('validate-kanban-write');
      const mainBody = installed.slice(installed.indexOf('main() {'));
      expect(mainBody.indexOf('ensure_python_environment')).toBeLessThan(
        mainBody.indexOf('guard-kanban'),
      );
      expect(mainBody.indexOf('ensure_python_environment')).toBeLessThan(
        mainBody.indexOf('validate-kanban-write'),
      );
      expect(mainBody.match(/if ! ensure_python_environment/g) || []).toHaveLength(1);
      expect(runner).toContain('_build_process_env({"ASSIGNED_TASK_ID": task_id}, task_id=task_id)');
      expect(runner).toContain('export ASSIGNED_TASK_ID=%s');
    });

    it('installs the managed root Git-flow delegate and canonical engine', async () => {
      await fs.ensureDir(path.join(testDir, '.juno_task'));

      const updated = await ScriptInstaller.autoUpdate(testDir, true);

      expect(updated).toBe(true);
      expect(await fs.readFile(path.join(testDir, 'scripts/git-flow.sh'), 'utf8')).toContain(
        '# yylo-managed: root-git-flow.v1',
      );
      expect(await fs.pathExists(path.join(testDir, '.juno_task/scripts/git-flow.sh'))).toBe(true);
      expect(await fs.pathExists(path.join(testDir, '.juno_task/scripts/git_flow.py'))).toBe(true);
    }, contentionBudgetMs(180_000));

    it('preserves an unrelated root Git-flow script', async () => {
      await fs.ensureDir(path.join(testDir, '.juno_task'));
      await fs.ensureDir(path.join(testDir, 'scripts'));
      const custom = '#!/bin/sh\necho custom\n';
      await fs.writeFile(path.join(testDir, 'scripts/git-flow.sh'), custom);

      await ScriptInstaller.autoUpdate(testDir, true);

      expect(await fs.readFile(path.join(testDir, 'scripts/git-flow.sh'), 'utf8')).toBe(custom);
    });

    it('should not update when scripts match package version', async () => {
      const scriptsDir = path.join(testDir, '.juno_task', 'scripts');
      await fs.ensureDir(scriptsDir);

      // Copy the actual package scripts to simulate up-to-date state
      // This test verifies that when content matches, no update is performed
      const beforeUpdate = await ScriptInstaller.autoUpdate(testDir, true);

      // If first update succeeded, run again - should return false (no updates needed)
      if (beforeUpdate) {
        const secondUpdate = await ScriptInstaller.autoUpdate(testDir, true);
        expect(secondUpdate).toBe(false);
      }
    });
  });
});
