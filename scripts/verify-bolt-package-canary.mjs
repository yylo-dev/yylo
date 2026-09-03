#!/usr/bin/env node
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { execFileSync, spawnSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { mkdtemp, mkdir, rm, symlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import process from 'node:process';
import { performance } from 'node:perf_hooks';

const started = performance.now();
const temporary = await mkdtemp(path.join(tmpdir(), 'juno-bolt-package-canary-'));
const packDirectory = path.join(temporary, 'pack');
await mkdir(packDirectory);
const outputIndex = process.argv.indexOf('--output');
const output = outputIndex >= 0 ? path.resolve(process.argv[outputIndex + 1] ?? '') : null;

const run = (command, args, cwd, extraEnv = {}) =>
  execFileSync(command, args, {
    cwd,
    encoding: 'utf8',
    env: { ...process.env, PYTHONPYCACHEPREFIX: '/tmp/juno-bolt-canary-pycache', ...extraEnv },
    stdio: 'pipe',
  });

try {
  const packed = JSON.parse(
    run('npm', ['pack', '--json', '--ignore-scripts', '--pack-destination', packDirectory], process.cwd()),
  )[0];
  assert.ok(packed?.filename, 'npm pack did not return a tarball');
  const archive = path.join(packDirectory, packed.filename);
  run('tar', ['-xzf', archive, '-C', packDirectory], process.cwd());
  const installed = path.join(packDirectory, 'package');
  const scripts = path.join(installed, 'dist/templates/scripts');
  const inventory = new Set(packed.files.map((entry) => entry.path));
  const packageJson = JSON.parse(readFileSync(path.join(installed, 'package.json'), 'utf8'));
  const executable = path.join(installed, 'dist/bin/cli.mjs');
  const runtimeDirectory = path.join(installed, '.juno_task/runtime');
  await mkdir(runtimeDirectory, { recursive: true });
  await writeFile(path.join(installed, '.juno_task/managed-assets.json'), JSON.stringify({
    schemaVersion: 1, packageName: '@yylo/cli', packageVersion: packageJson.version, assets: {},
  }));
  await writeFile(path.join(runtimeDirectory, 'identity.json'), JSON.stringify({
    package: '@yylo/cli', version: packageJson.version, executable,
    executable_sha256: createHash('sha256').update(readFileSync(executable)).digest('hex'),
    source: 'installed-release', tracked: false,
  }));

  for (const retired of [
    'dist/templates/scripts/task_lifecycle.py',
    'dist/templates/scripts/integration_candidate.py',
    'dist/templates/scripts/integration_owner_preflight.py',
    'dist/templates/scripts/worktree_lifecycle.py',
    'dist/templates/config/lifecycle.json',
    'dist/templates/config/controller-workspace.json',
  ]) {
    assert.equal(inventory.has(retired), false, `packed retired asset: ${retired}`);
  }

  const instructionFiles = [
    'dist/templates/prompts/new_task_workflow.md',
    'dist/templates/prompts/clean_worktree.md',
    'dist/templates/prompts/run_workflow.md',
  ];
  for (const relative of instructionFiles) {
    const instruction = readFileSync(path.join(installed, relative), 'utf8');
    assert.match(instruction, /yy task preflight TASK_ID/u, `missing task preflight: ${relative}`);
    assert.match(instruction, /\.\.\/wiki\/controller\/task_dependency_hydration\.md/u,
      `stale controller wiki link: ${relative}`);
    assert.doesNotMatch(instruction, /\.\.\/wiki\/task_dependency_hydration\.md/u,
      `legacy controller wiki link: ${relative}`);
  }
  const migrationInstruction = readFileSync(
    path.join(installed, 'dist/templates/prompts/migrate_juno_code_v1_to_v2.md'),
    'utf8',
  );
  assert.match(migrationInstruction, /yy task preflight CANARY_X/u);
  assert.match(migrationInstruction, /yy task preflight CANARY_Y/u);
  assert.match(readFileSync(path.join(installed, 'README.md'), 'utf8'),
    /yy task preflight ID -> yy task finish ID/u);

  const selections = {
    task_workspace: [
      'SemVerValidationTests.test_accepts_stable_prerelease_build_and_combined_versions',
      'SemVerValidationTests.test_rejects_malformed_versions',
      'SemVerValidationTests.test_validation_is_exact_string_only_without_trimming_or_coercion',
      'TaskWorkspaceTests.test_concurrent_tasks_share_frozen_base_without_controller_data',
      'TaskWorkspaceTests.test_finish_refuses_failed_focused_validation_without_state_advance',
    ],
    merge_queue: [
      'MergeQueueTests.test_parallel_x_y_then_moved_target_uses_one_two_parent_composition',
      'MergeQueueTests.test_real_a_b_text_conflict_is_preserved_then_resolved_without_feature_recreation',
      'MergeQueueTests.test_reviewer_a_pass_b_transport_failure_retries_only_b_in_fresh_namespace',
      'MergeQueueTests.test_nonblocking_target_lock_refuses_duplicate_worker_without_state_or_ref_change',
      'MergeQueueTests.test_failed_validation_and_target_movement_do_zero_queue_cas',
      'MergeQueueTests.test_cleanup_refuses_dirty_reachable_checkout_and_target_readback_is_exact',
      'MergeQueueTests.test_cleanup_refuses_unreachable_candidate',
    ],
    metadata_controller: [
      'MetadataControllerTest.test_prepare_creates_unrelated_metadata_only_controller_and_preserves_product',
      'MetadataControllerTest.test_runtime_rebind_is_local_and_rollback_is_plan_only',
    ],
  };
  for (const [suite, tests] of Object.entries(selections)) {
    run('python3', [path.join(scripts, `tests/test_${suite}.py`), ...tests], installed,
      { JUNO_TASK_ROOT: installed });
  }

  const profilerReceipt = path.join(temporary, 'task-workspace-profile.json');
  run(process.execPath, [
    path.join(installed, 'scripts/test-task-workspace.mjs'),
    '--mode', 'seeded',
    '--test-id', 'SemVerValidationTests.test_rejects_malformed_versions',
    '--receipt', profilerReceipt,
  ], installed);
  const profiler = JSON.parse(readFileSync(profilerReceipt, 'utf8'));
  assert.equal(profiler.eligible, true, 'packed task-workspace profiler must be eligible');
  assert.equal(profiler.counts.selected, 1, 'packed task-workspace profiler selected count');

  const dependencies = path.resolve('node_modules');
  assert.ok(existsSync(dependencies), 'build dependencies are required for the packed CLI canary');
  await symlink(dependencies, path.join(installed, 'node_modules'));
  const legacy = spawnSync(
    process.execPath,
    [path.join(installed, 'dist/bin/cli.mjs'), 'lifecycle', 'status', '--task', 'OLD'],
    { cwd: temporary, encoding: 'utf8', env: { ...process.env, CI: '1', NO_COLOR: '1' } },
  );
  assert.equal(legacy.status, 2);
  assert.match(legacy.stderr, /legacy lifecycle executor was removed/i);

  const sync = spawnSync('python3', [path.join(scripts, 'git_flow.py'), 'controller-sync'], {
    cwd: temporary,
    encoding: 'utf8',
  });
  assert.equal(sync.status, 2);
  assert.match(sync.stderr, /controller synchronization is removed/i);

  const result = {
    schema_version: 'juno_bolt_package_canary.v1',
    package: `@yylo/cli@${JSON.parse(readFileSync(path.join(installed, 'package.json'))).version}`,
    package_files: packed.entryCount,
    selected_tests: Object.values(selections).flat().length,
    scenarios: {
      concurrent_feature_worktrees: 4,
      moved_target_compositions: 1,
      preserved_conflicts_resolved: 1,
      expected_refusals: 7,
      metadata_prepare_verify_cutover_rollback: true,
      retired_entrypoints_refused: 2,
      packed_task_workspace_profiler: true,
    },
    orchestration: {
      model_calls: 0,
      agent_tool_calls: 0,
      failed_agent_calls: 0,
      reviewer_sessions: 0,
      controller_checkpoints: 0,
      uncached_input_tokens: 0,
      output_tokens: 0,
      cache_read_tokens: 0,
    },
    elapsed_ms: Math.round(performance.now() - started),
  };
  const serialized = `${JSON.stringify(result, null, 2)}\n`;
  if (output) await writeFile(output, serialized, { flag: 'wx' });
  process.stdout.write(serialized);
} finally {
  await rm(temporary, { recursive: true, force: true });
}
