#!/usr/bin/env node
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { execFileSync, spawnSync } from 'node:child_process';
import { cp, mkdir, mkdtemp, readFile, rename, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

const root = process.cwd();
const temporary = await mkdtemp(path.join(os.tmpdir(), 'juno-installed-test-fixture-'));
const packDirectory = path.join(temporary, 'pack');
await mkdir(packDirectory);

try {
  const packed = JSON.parse(execFileSync(
    'npm', ['pack', '--json', '--ignore-scripts', '--pack-destination', packDirectory],
    { cwd: root, encoding: 'utf8' },
  ))[0];
  const archive = path.join(packDirectory, packed.filename);
  execFileSync('tar', ['-xzf', archive, '-C', packDirectory]);
  const packageRoot = path.join(packDirectory, 'package');
  const templates = path.join(packageRoot, 'dist/templates');
  const installed = path.join(temporary, 'consumer');
  const installedScripts = path.join(installed, '.juno_task/scripts');
  await mkdir(installedScripts, { recursive: true });

  const manifest = JSON.parse(await readFile(path.join(templates, 'managed-assets.json'), 'utf8'));
  for (const asset of manifest.assets) {
    await cp(path.join(templates, asset.source), path.join(installed, asset.destination));
  }
  const adjacentFixture = path.join(installedScripts, 'tests/real_git_fixture.py');
  assert.equal(
    await import('node:fs').then(({ existsSync }) => existsSync(adjacentFixture)),
    false,
    'isolated installed layout must not duplicate canonical fixture authority',
  );
  const hostileFixture = 'raise RuntimeError("non-authoritative fixture was loaded")\n';
  await writeFile(adjacentFixture, hostileFixture);
  const guessedFixture = path.join(installed, 'juno-code/src/templates/scripts/tests/real_git_fixture.py');
  await mkdir(path.dirname(guessedFixture), { recursive: true });
  await writeFile(guessedFixture, hostileFixture);

  const packagePath = path.join(packageRoot, 'package.json');
  const inventoryPath = path.join(installed, '.juno_task/managed-assets.json');
  const identityPath = path.join(installed, '.juno_task/runtime/identity.json');
  const packageJson = JSON.parse(await readFile(packagePath, 'utf8'));
  const executable = path.join(packageRoot, 'dist/bin/cli.mjs');
  const executableBytes = await readFile(executable);
  const inventory = {
    schemaVersion: 1,
    packageName: '@yylo/cli',
    packageVersion: packageJson.version,
    assets: {},
  };
  const identity = {
    package: '@yylo/cli', version: packageJson.version, executable,
    executable_sha256: createHash('sha256').update(executableBytes).digest('hex'),
    source: 'installed-release', tracked: false,
  };
  await mkdir(path.dirname(identityPath), { recursive: true });
  await writeFile(inventoryPath, JSON.stringify(inventory));
  await writeFile(identityPath, JSON.stringify(identity));

  const selections = [
    ['test_task_workspace.py',
      'TaskWorkspaceTests.test_authoritative_juno_fixture_missing_asset_has_one_setup_diagnostic'],
    ['test_merge_queue.py',
      'NativeDeliveryTests.test_clean_divergent_merge_preserves_both_sides_and_separates_projection'],
    ['test_merge_queue.py',
      'NativeDeliveryTests.test_conflict_is_private_and_does_not_block_unrelated_task'],
    ['test_merge_queue.py',
      'NativeDeliveryTests.test_target_move_requires_recomposition'],
    ['test_merge_queue.py',
      'NativeDeliveryTests.test_ledger_failure_occurs_after_git_and_does_not_repeat_integration'],
    ['test_integration_workspace.py',
      'IntegrationWorkspaceTests.test_status_is_offline_and_reports_stale_owner_as_data'],
  ];
  const testEnv = { ...process.env, JUNO_TASK_ROOT: installed,
    PYTHONPYCACHEPREFIX: path.join(temporary, 'pycache') };
  for (const [file, selection] of selections) {
    execFileSync('python3', [path.join(installedScripts, 'tests', file), selection], {
      cwd: installed, env: testEnv, stdio: 'pipe',
    });
  }

  const fixtureProbe = () => spawnSync(
    'python3', [path.join(installedScripts, 'tests/test_task_workspace.py'), '--help'],
    { cwd: installed, env: testEnv, encoding: 'utf8' },
  );
  const assertAvailable = (label) => {
    const available = fixtureProbe();
    assert.equal(available.status, 0,
      `${label} package fixture must load: ${available.stderr || available.stdout}`);
  };
  const assertUnavailable = (label) => {
    const unavailable = fixtureProbe();
    assert.notEqual(unavailable.status, 0, `${label} installed tests must fail closed`);
    assert.equal((unavailable.stderr.match(/package-bound test fixture unavailable/g) ?? []).length, 1,
      `${label} package fixture must emit one setup diagnostic`);
    assert.match(unavailable.stderr, /yy scripts update --force/,
      `${label} package fixture diagnostic must name supported recovery`);
  };
  const writeBindings = async (bindings) => {
    const identityVersion = bindings.identityVersion;
    const inventoryVersion = Object.hasOwn(bindings, 'inventoryVersion')
      ? bindings.inventoryVersion : identityVersion;
    const packageVersion = Object.hasOwn(bindings, 'packageVersion')
      ? bindings.packageVersion : identityVersion;
    const nextIdentity = { ...identity };
    const nextInventory = { ...inventory };
    const nextPackage = { ...packageJson };
    if (identityVersion === undefined) delete nextIdentity.version;
    else nextIdentity.version = identityVersion;
    if (inventoryVersion === undefined) delete nextInventory.packageVersion;
    else nextInventory.packageVersion = inventoryVersion;
    if (packageVersion === undefined) delete nextPackage.version;
    else nextPackage.version = packageVersion;
    await writeFile(identityPath, JSON.stringify(nextIdentity));
    await writeFile(inventoryPath, JSON.stringify(nextInventory));
    await writeFile(packagePath, JSON.stringify(nextPackage));
  };

  for (const [label, version] of Object.entries({
    stable: '2.1.3',
    prerelease: '2.1.3-rc.0.11',
    build: '2.1.3+build.11',
    prerelease_build: '2.1.3-rc.0.11+build.7',
  })) {
    await writeBindings({ identityVersion: version });
    assertAvailable(label);
  }
  for (const [label, version] of Object.entries({
    leading_zero_core: '02.1.3',
    leading_zero_prerelease: '2.1.3-rc.01',
    empty_prerelease_identifier: '2.1.3-rc..1',
    invalid_build_character: '2.1.3+build_1',
  })) {
    await writeBindings({ identityVersion: version });
    assertUnavailable(label);
  }
  await writeBindings({ identityVersion: undefined });
  assertUnavailable('missing-versions');
  await writeBindings({ identityVersion: 213 });
  assertUnavailable('non-string-versions');
  await writeBindings({ identityVersion: '2.1.3', inventoryVersion: undefined });
  assertUnavailable('missing-inventory-version');
  await writeBindings({ identityVersion: '2.1.3', inventoryVersion: 213 });
  assertUnavailable('non-string-inventory-version');
  await writeBindings({ identityVersion: '2.1.3', packageVersion: undefined });
  assertUnavailable('missing-package-version');
  await writeBindings({ identityVersion: '2.1.3', packageVersion: 213 });
  assertUnavailable('non-string-package-version');
  await writeBindings({ identityVersion: '2.1.3-rc.0.11', inventoryVersion: '2.1.3-rc.0.10' });
  assertUnavailable('inventory-version-mismatch');
  await writeBindings({ identityVersion: '2.1.3-rc.0.11', packageVersion: '2.1.3-rc.0.10' });
  assertUnavailable('package-version-mismatch');
  await writeBindings({ identityVersion: packageJson.version });

  const hiddenIdentity = identityPath + '.unavailable';
  await rename(identityPath, hiddenIdentity);
  assertUnavailable('unbound');
  await writeFile(identityPath, JSON.stringify({ ...identity, executable_sha256: '0'.repeat(64) }));
  assertUnavailable('invalid-identity');
  await rm(identityPath);
  await rename(hiddenIdentity, identityPath);
  console.log('Installed consumers ignored conflicting adjacent/guessed fixtures, loaded canonical package bytes, and passed focused setup.');
} finally {
  await rm(temporary, { recursive: true, force: true });
}
