import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import * as path from 'node:path';
import fs from 'fs-extra';
import managedAssetManifest from '../../../templates/managed-assets.json';
import { version as packageVersion } from '../../../version.js';
import { ManagedProjectAssets } from '../../../utils/managed-project-assets.js';

const sha256 = (value: Buffer | string) => createHash('sha256').update(value).digest('hex');

export const REAL_METADATA_CONTROLLER_TARGET_REF =
  'bb5483cde24518d9a727c0633deece60ec0d56a9';
export const REAL_STALE_CONTROLLER_SCRIPTS = {
  'managed_agent_runner.py': {
    targetSha256: '01cd94c975de13c4b7744317fb6b1083c62f154328cea2c8cda2f783044fc17b',
    staleBlob: 'b1bbfa5e8a0220f2d60b1d162b91055ea64f686f',
    staleSha256: '506451eb01ab720bdfe6e32fa4ff435fde53ede01c2fc6c123d4884da83eead5',
  },
  'merge_queue.py': {
    targetSha256: '0d11220d6998fe456d0aeee8ebc113cbabb601515fa94873ea9f496919881c76',
    staleBlob: '52c166d2ef414ca84547a5bcb42b99fdf9d5e2d6',
    staleSha256: 'd46dca5d1309d3069480ad5b2cdfe67e581925fbe23e127437424a59da05e862',
  },
  'release_train.py': {
    targetSha256: '943a64576f6808137491d9eef8e419eefd21596b171ef36f27fd0968b593ed88',
    staleBlob: '17eac999bf505e5c83d5660d912a8dae88d150c7',
    staleSha256: '4a7a3e916d816057453d6aeb049cbd83a5ed2e86594527afcadcad1a9683119c',
  },
  'task_workspace.py': {
    targetSha256: '11e7dfc65e0be869fc8a1f2beafeebaf141d6c2c7f3fde3547a4c329b1214473',
    staleBlob: '989dfd43547c646d6e88478cfe47a27f7f997d8e',
    staleSha256: '861abba186bc8f87e76c3c1204e50eed8f07b6797bd6f38a582f6c6560e95c48',
  },
  'tests/test_managed_agent_runner.py': {
    targetSha256: '2d4856ad3e06f1f8c7cddb5885a3c8ce8379568c68b0f3cee4db43a9b902c9ec',
    staleBlob: 'f2fa9ecc94b14342d1591fb5b6326f29609e77bb',
    staleSha256: '30652580f93cb4150b4435c868e1b750cdfaa7a2e9b188d124fd3668f3d902f4',
  },
  'tests/test_merge_queue.py': {
    targetSha256: '79a545a5bb33993d5831d28cd6825130bcb197fadbaafbfb6698e1464900de6c',
    staleBlob: '842f255e1d93470af02b068c5171657e5090f8ec',
    staleSha256: 'a7d3f9619943537660cab3741096aeb51c60d71f7215ece6cf796e798c41d41f',
  },
  'tests/test_release_train.py': {
    targetSha256: 'af813530596ecec54f49996b399f19563ac84ceaaf8bd7f1658ba8d769c53b09',
    staleBlob: '8d7684b67957283e4ec418d59df317533fcbb955',
    staleSha256: '05e50092027419bf747fd864ab987c9d30e0f2975f43110e20627bbb757f11f3',
  },
  'tests/test_task_workspace.py': {
    targetSha256: '6200a57dbffd4e535a307fe7e4ca808e0a26c45b9c5db51b6391fcf3d4a292d5',
    staleBlob: '8eb5516fbcd6d71da53cc74b424078a304c55561',
    staleSha256: '1a12eee625de2f2f2b81fa4a5ff931a08b64807d8afab3b501a0c4f009791051',
  },
} as const;

function git(root: string, ...args: string[]): string {
  const result = spawnSync('git', ['-C', root, ...args], { encoding: 'utf8' });
  if (result.status !== 0) throw new Error(result.stderr || result.stdout);
  return result.stdout.trim();
}

function sourceGitBytes(...args: string[]): Buffer {
  const result = spawnSync('git', args, { cwd: path.resolve(process.cwd(), '..') });
  if (result.status !== 0) throw new Error(result.stderr.toString() || result.stdout.toString());
  return result.stdout;
}

async function copyTargetPackageGeneration(
  root: string,
  targetRef: string,
  fixtureVersion: string,
): Promise<void> {
  const manifestBytes = sourceGitBytes(
    'show', `${targetRef}:juno-code/src/templates/managed-assets.json`,
  );
  const manifest = JSON.parse(manifestBytes.toString('utf8')) as typeof managedAssetManifest;
  await fs.outputFile(
    path.join(root, 'juno-code/src/templates/managed-assets.json'), manifestBytes,
  );
  const templateRoot = 'juno-code/src/templates/';
  const targetTemplatePaths = sourceGitBytes(
    'ls-tree', '-rz', '--name-only', targetRef, '--', templateRoot,
  ).toString('utf8').split('\0').filter(Boolean);
  if (!targetTemplatePaths.length || targetTemplatePaths.some((entry) =>
    !entry.startsWith(templateRoot) || entry.includes('\\') || entry.split('/').includes('..'))) {
    throw new Error('Immutable target template inventory is missing or unsafe');
  }
  for (const targetPath of targetTemplatePaths) {
    await fs.outputFile(
      path.join(root, targetPath),
      sourceGitBytes('show', `${targetRef}:${targetPath}`),
    );
  }
  const declaredSources = new Set([
    ...manifest.assets.map((asset) => asset.source),
    ...manifest.controllerOutputs.map((asset) => asset.source),
  ]);
  if ([...declaredSources].some((source) =>
    !targetTemplatePaths.includes(`${templateRoot}${source}`))) {
    throw new Error('Immutable target managed declaration references a missing template');
  }
  const targetPackage = JSON.parse(
    sourceGitBytes('show', `${targetRef}:juno-code/package.json`).toString('utf8'),
  );
  // Vitest compiles the package version as "test"; retain the immutable target
  // sources/declaration while adapting only that harness identity field.
  targetPackage.version = fixtureVersion;
  await fs.outputJson(path.join(root, 'juno-code/package.json'), targetPackage);
}

export function realStaleControllerScriptBytes(name: keyof typeof REAL_STALE_CONTROLLER_SCRIPTS): Buffer {
  return sourceGitBytes('cat-file', 'blob', REAL_STALE_CONTROLLER_SCRIPTS[name].staleBlob);
}

async function createInstalledPackage(
  root: string,
  fixtureVersion: string,
  routedCurrentPackage: boolean,
  packageTemplatesDir = path.resolve(process.cwd(), 'src/templates'),
): Promise<{ executable: string; scriptsDir: string }> {
  if (routedCurrentPackage) {
    return {
      executable: path.resolve(process.cwd(), 'dist/bin/cli.mjs'),
      scriptsDir: path.resolve(process.cwd(), 'dist/templates/scripts'),
    };
  }
  const packageRoot = path.join(root, 'installed/node_modules/@yylo/cli');
  const executable = path.join(packageRoot, 'dist/bin/cli.mjs');
  const templates = path.join(packageRoot, 'dist/templates');
  await fs.outputFile(executable, '#!/usr/bin/env node\n// fixture routed executable\n');
  await fs.writeJson(path.join(packageRoot, 'package.json'), {
    name: '@yylo/cli', version: fixtureVersion,
  });
  await fs.copy(packageTemplatesDir, templates);
  return { executable, scriptsDir: path.join(templates, 'scripts') };
}

export async function createTargetBoundMetadataController(
  root: string,
  fixtureVersion = packageVersion,
  options: { exactTargetRef?: string; routedCurrentPackage?: boolean } = {},
): Promise<{
  targetSha: string;
  changedScripts: string[];
  packageScriptsDir: string;
}> {
  git(root, 'init', '-q', '-b', 'customer/controller');
  git(root, 'config', 'user.name', 'YYLO Recovery Test');
  git(root, 'config', 'user.email', 'recovery@example.invalid');

  const targetTemplates = path.join(root, 'juno-code/src/templates');
  if (options.exactTargetRef) {
    await copyTargetPackageGeneration(root, options.exactTargetRef, fixtureVersion);
  } else {
    await fs.copy(path.resolve(process.cwd(), 'src/templates'), targetTemplates);
    await fs.writeJson(path.join(root, 'juno-code/package.json'), {
      name: '@yylo/cli', version: fixtureVersion,
    });
  }
  const changedScripts = Object.keys(REAL_STALE_CONTROLLER_SCRIPTS);
  // Generic binary coverage uses target-only markers to exercise the same
  // eight-path mismatch without depending on repository history. The exact
  // protected-target regression below instead uses the immutable Git bytes.
  if (!options.exactTargetRef) {
    for (const name of changedScripts) {
      await fs.appendFile(
        path.join(targetTemplates, 'scripts', name),
        `\n# exact target-only recovery fixture: ${name}\n`,
      );
    }
  }
  const targetScriptBytes = new Map<string, Buffer>();
  for (const asset of managedAssetManifest.assets.filter(
    (entry) => entry.type !== 'config' && entry.installClass === 'script',
  )) {
    targetScriptBytes.set(asset.destination, await fs.readFile(
      path.join(targetTemplates, asset.source),
    ));
  }
  git(root, 'add', 'juno-code');
  git(root, 'commit', '-qm', 'target package source generation');
  const targetSha = git(root, 'rev-parse', 'HEAD');
  git(root, 'branch', 'customer/product', targetSha);
  const installedPackage = await createInstalledPackage(
    root,
    fixtureVersion,
    Boolean(options.routedCurrentPackage),
    options.exactTargetRef ? targetTemplates : undefined,
  );
  git(root, 'rm', '-q', '-r', 'juno-code');

  const configDir = path.join(root, '.juno_task/config');
  await fs.ensureDir(configDir);
  const controllerDestinations = [
    ...managedAssetManifest.assets
      .filter((asset) => asset.type !== 'config')
      .map((asset) => asset.destination),
    ...managedAssetManifest.controllerOutputs.map((asset) => asset.destination),
  ];
  const trackedBundle = [
    '.juno_task/managed-assets.json',
    ...controllerDestinations.filter((destination) =>
      /^\.juno_task\/(prompts|wiki|workflows)\//.test(destination)),
  ];
  const metadata = await fs.readJson(
    path.resolve(process.cwd(), 'src/templates/config/metadata-controller.json'),
  );
  metadata.controller_branch = 'refs/heads/customer/controller';
  metadata.product_ref = 'refs/heads/customer/product';
  metadata.generated_metadata = [...new Set([
    ...metadata.generated_metadata, ...trackedBundle,
  ])].sort();
  metadata.tracked_exact = [...new Set([
    ...metadata.tracked_exact, ...trackedBundle,
  ])].sort();
  const metadataBytes = Buffer.from(`${JSON.stringify(metadata, null, 2)}\n`);
  await fs.writeFile(path.join(configDir, 'metadata-controller.json'), metadataBytes);
  for (const name of ['task-workspace.json', 'integration-workspace.json', 'risk-policy.json']) {
    await fs.copyFile(
      path.resolve(process.cwd(), 'src/templates/config', name),
      path.join(configDir, name),
    );
  }
  await fs.writeJson(path.join(root, '.juno_task/config.json'), {
    controllerWorkspace: {
      mode: 'metadata-only', policy: '.juno_task/config/metadata-controller.json',
    },
    gitCheckpoint: {
      include: [
        '.juno_task/config', '.juno_task/config.json', '.juno_task/managed-assets.json',
        '.juno_task/prompts', '.juno_task/wiki', '.juno_task/workflows',
      ],
    },
  }, { spaces: 2 });
  await fs.writeFile(path.join(root, '.gitignore'), [
    '.juno_task/scripts/', '.juno_task/runtime/', '.juno_task/managed-conflicts/',
    '.venv_juno/', '/AGENTS.md', '/CLAUDE.md', '/.agents/', '/.claude/', '/.pi/', '',
  ].join('\n'));
  await fs.outputFile(path.join(root, '.juno_task/ledger/owner.txt'), 'owner metadata\n');

  await ManagedProjectAssets.update(root, { force: true, silent: true });
  const definitions = managedAssetManifest.assets.filter(
    (asset) => asset.type !== 'config' && asset.installClass === 'script',
  );
  const scripts: Record<string, {
    classification: 'exact'; source_sha256: string; actual_sha256: string;
  }> = {};
  for (const asset of definitions) {
    const target = targetScriptBytes.get(asset.destination) as Buffer;
    await fs.outputFile(path.join(root, asset.destination), target);
    await fs.chmod(path.join(root, asset.destination), 0o755);
    scripts[asset.destination] = {
      classification: 'exact', source_sha256: sha256(target), actual_sha256: sha256(target),
    };
  }
  await fs.outputJson(path.join(root, '.juno_task/runtime/identity.json'), {
    package: '@yylo/cli', version: fixtureVersion, executable: installedPackage.executable,
    executable_sha256: sha256(await fs.readFile(installedPackage.executable)),
    source: 'installed-release', tracked: false,
  });
  await fs.outputJson(
    path.join(root, '.juno_task/runtime/managed-controller/generation.json'),
    {
      schema_version: 'juno_managed_controller_runtime.v1',
      target_sha: targetSha,
      package_version: fixtureVersion,
      scripts,
      policy_sha256: sha256(await fs.readFile(path.join(configDir, 'task-workspace.json'))),
    },
  );

  git(root, 'add', '.gitignore', '.juno_task/config', '.juno_task/config.json',
    '.juno_task/ledger', '.juno_task/managed-assets.json', '.juno_task/prompts',
    '.juno_task/wiki', '.juno_task/workflows');
  git(root, 'commit', '-qm', 'metadata controller generation');
  return { targetSha, changedScripts, packageScriptsDir: installedPackage.scriptsDir };
}
