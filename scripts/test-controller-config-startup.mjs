// Built-package CLI migration + Pi configuration/preflight smoke, with provider dispatch stubbed.
// Run after npm run build. Creates disposable real-Git fixtures; no model/network/package installs.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { execFileSync, spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const packageRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const cli = path.join(packageRoot, 'dist/bin/cli.mjs');
const templates = path.join(packageRoot, 'dist/templates');
for (const key of Object.keys(process.env)) {
  if (/^(JUNO_|YYLO_|GIT_)/.test(key)) delete process.env[key];
}
process.env.YYLO_PROJECT_BOOTSTRAP_WRITES = '0';
const temp = await fs.mkdtemp(path.join(os.tmpdir(), 'yy-controller-config-smoke-'));
const root = path.join(temp, 'controller');
await fs.mkdir(path.join(root, '.juno_task/config'), { recursive: true });
const git = (...args) => execFileSync('git', ['-C', root, ...args], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();
const run = (...args) => spawnSync(process.execPath, [cli, ...args], {
  cwd: root, env: process.env, encoding: 'utf8', timeout: 30000,
});
try {
  git('init', '-b', 'product');
  git('config', 'user.name', 'Smoke');
  git('config', 'user.email', 'smoke@example.invalid');
  await fs.writeFile(path.join(root, 'README.md'), 'product\n');
  git('add', '.'); git('commit', '-m', 'product');
  const product = git('rev-parse', 'HEAD');
  git('switch', '-c', 'controller');
  const policy = JSON.parse(await fs.readFile(path.join(templates, 'config/metadata-controller.json'), 'utf8'));
  policy.controller_branch = 'refs/heads/controller';
  policy.product_ref = 'refs/heads/product';
  await fs.writeFile(path.join(root, '.juno_task/config/metadata-controller.json'), JSON.stringify(policy));
  const configPath = path.join(root, '.juno_task/config.json');
  const legacy = { controllerWorkspace: { mode: 'metadata-only' }, workingDirectory: root,
    hooks: { START_RUN: { commands: ['touch forbidden-hook'] } }, defaultMaxIterations: 1,
    agentProfile: { version: 1, promptAssetRoot: '.juno_task/prompts' } };
  await fs.writeFile(configPath, JSON.stringify(legacy));
  git('add', '.'); git('commit', '-m', 'legacy config');
  git('config', 'extensions.worktreeConfig', 'true');
  for (const [key, value] of Object.entries({ 'juno.controller.path': root, 'juno.controller.branch': 'refs/heads/controller' })) git('config', '--local', key, value);
  const manifest = JSON.parse(await fs.readFile(path.join(packageRoot, 'package.json'), 'utf8'));
  for (const [key, value] of Object.entries({ 'juno.workspace.role': 'controller', 'juno.controller.mode': 'metadata-only',
    'juno.controller.runtimeVersion': manifest.version, 'juno.controller.runtimeExecutable': cli })) git('config', '--worktree', key, value);

  const rejected = run('pi', '--live', '--config', configPath, 'smoke');
  assert.notEqual(rejected.status, 0);
  assert.match(rejected.stderr + rejected.stdout, /workingDirectory is product-only/);
  assert.match(rejected.stderr + rejected.stdout, /controller-config plan/);
  const plan = path.join(temp, 'plan.json');
  const planned = run('migrate', 'controller-config', 'plan', '--root', root, '--output', plan);
  assert.equal(planned.status, 0, planned.stderr + planned.stdout);
  assert.deepEqual(JSON.parse(await fs.readFile(configPath)), legacy);
  const applied = run('migrate', 'controller-config', 'apply', '--plan', plan,
    '--output', path.join(temp, 'applied.json'), '--authorize-config-repair');
  assert.equal(applied.status, 0, applied.stderr + applied.stdout);
  assert.equal(git('rev-parse', 'refs/heads/product'), product);
  assert.equal(git('status', '--porcelain'), '');

  const bin = path.join(temp, 'bin');
  await fs.mkdir(bin);
  const ledger = path.join(bin, 'yylo-ledger');
  await fs.writeFile(ledger, `#!/bin/sh\nprintf 'yylo-ledger ${manifest.yyloLedger.version}\\n'\n`, { mode: 0o755 });
  process.env.PATH = `${bin}${path.delimiter}${process.env.PATH}`;
  const { loadConfig, ExecutionEngine, createExecutionRequest, getDefaultHooks } = await import('../dist/index.mjs');
  const config = await loadConfig({ baseDir: root, configFile: configPath });
  assert.equal(config.workingDirectory, root);
  assert.equal(config.hooks, undefined);
  let dispatches = 0;
  const executeSmoke = async (cwd, selectedConfig) => {
  const { DEFAULT_ERROR_RECOVERY_CONFIG, DEFAULT_RATE_LIMIT_CONFIG, DEFAULT_PROGRESS_CONFIG } = await import('../dist/index.mjs');
  const listeners = new Map(['uncaughtException', 'unhandledRejection', 'SIGINT', 'SIGTERM'].map((event) => [event, process.listeners(event)]));
  const engine = new ExecutionEngine({ config: selectedConfig, errorRecovery: DEFAULT_ERROR_RECOVERY_CONFIG,
    rateLimitConfig: DEFAULT_RATE_LIMIT_CONFIG, progressConfig: DEFAULT_PROGRESS_CONFIG });
  for (const [event, previous] of listeners) {
    for (const listener of process.listeners(event)) if (!previous.includes(listener)) process.removeListener(event, listener);
  }
  // Stub only the provider boundary, leaving packaged config and START_RUN preflight real.
  engine.initializeBackend = async () => {
    engine.currentBackend = {
      execute: async (request) => { dispatches++; return { content: 'smoke complete', status: 'completed',
        startTime: new Date(), endTime: new Date(), duration: 0, progressEvents: [], request }; },
      cleanup: async () => {},
    };
  };
  try {
    const result = await engine.execute(createExecutionRequest({
      instruction: 'smoke', subagent: 'pi', backend: 'shell', workingDirectory: cwd, maxIterations: 1,
    }));
    assert.equal(result.status, 'completed', JSON.stringify(result));
  } finally { await engine.shutdown(1); }
  };
  await executeSmoke(root, config);
  assert.equal(dispatches, 1);
  const genericConfig = await loadConfig({ baseDir: temp });
  genericConfig.hooks = getDefaultHooks();
  genericConfig.hooks.START_RUN.commands = ['./.juno_task/scripts/install_requirements.sh'];
  const beforeParent = await fs.readdir(temp);
  await executeSmoke(temp, genericConfig);
  assert.equal(dispatches, 2);
  assert.deepEqual(await fs.readdir(temp), beforeParent);
  await assert.rejects(fs.stat(path.join(temp, '.juno_task')), { code: 'ENOENT' });
  const refused = spawnSync(process.execPath, [cli, 'pi', '--live', 'smoke'], {
    cwd: temp, env: { ...process.env, JUNO_TASK_ROOT: root }, encoding: 'utf8', timeout: 30000,
  });
  assert.equal(refused.status, 2, refused.stderr + refused.stdout);
  assert.equal((refused.stderr.match(/cannot inherit controller authority/g) ?? []).length, 1);
  assert.doesNotMatch(refused.stderr, /\[object Object\]|Unexpected Error/);
  assert.equal(git('status', '--porcelain'), '');
  await assert.rejects(fs.stat(path.join(root, 'forbidden-hook')), { code: 'ENOENT' });
  console.log('PASS packaged controller-config migration and stubbed Pi startup');
} finally {
  await fs.rm(temp, { recursive: true, force: true });
}
