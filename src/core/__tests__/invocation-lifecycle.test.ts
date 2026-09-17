import * as childProcess from 'node:child_process';
import * as fsNode from 'node:fs';
import * as path from 'node:path';
import fs from 'fs-extra';
import { build } from 'esbuild';
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';

import { getInvocationTelemetryDirectory } from '../invocation-telemetry.js';
import { InvocationLifecycle } from '../invocation-lifecycle.js';

const roots: string[] = [];
const helper = path.resolve('src/core/__tests__/helpers/invocation-lifecycle-subprocess.ts');
const cliSource = path.resolve('src/bin/cli.ts');
const wrapperSource = path.resolve('src/bin/yylo.sh');
const yplSource = path.resolve('src/bin/ypl.sh');
const tsxLoader = path.resolve('node_modules/tsx/dist/loader.mjs');
let wrapperFixtureRoot: string;
let canonicalWrapper: string;
let canonicalYy: string;
let canonicalYpl: string;

async function temp(name: string): Promise<string> {
  const root = path.join('/tmp', `juno-lifecycle-${name}-${process.pid}-${Date.now()}-${roots.length}`);
  roots.push(root);
  await fs.ensureDir(root);
  return root;
}

function actualProjectEnvironment(root: string, home: string): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = { ...process.env, HOME: home, XDG_STATE_HOME: path.join(root, 'state') };
  // The suite's registered fixture controller is unrelated to this subprocess.
  for (const key of Object.keys(env)) if (/^(JUNO_|YYLO_|GIT_)/.test(key)) delete env[key];
  return env;
}

async function createActualProject(root: string, piSource: string): Promise<{ project: string; home: string }> {
  const project = path.join(root, 'project');
  const home = path.join(root, 'home');
  const services = path.join(home, '.yylo', 'services');
  // Provider lifecycle tests need a generic project, not an unauthenticated
  // managed controller. Generation refusal has its own test below.
  await fs.ensureDir(project);
  await fs.ensureDir(services);
  const packageJson = await fs.readJson(path.resolve('package.json'));
  await fs.writeFile(path.join(services, '.version'), `${packageJson.version}\n`);
  for (const name of ['claude.py', 'codex.py', 'gemini.py', 'environment_boundary.py']) {
    await fs.writeFile(path.join(services, name), '#!/usr/bin/env python3\n', { mode: 0o755 });
  }
  await fs.writeFile(path.join(services, 'pi.py'), piSource, { mode: 0o755 });
  return { project, home };
}

async function waitForEvent(root: string, stateRoot: string = root): Promise<void> {
  const telemetry = telemetryDirectory(root, stateRoot);
  await waitUntil(async () => (await events(root, stateRoot)).length > 0, [telemetry]);
}

/**
 * Event-first bounded wait: subscribe to directory watchers before probing so
 * child readiness observed through file creation/telemetry writes re-probes
 * immediately, with a bounded poll fallback that guards against missed events.
 * The overall deadline is a contention budget rather than a fixed retry count,
 * so shared-host load extends the wait instead of producing phantom admission
 * failures. Timeouts stay non-throwing (returns false) to preserve the
 * historical poll-loop semantics.
 */
async function waitUntil(
  probe: () => Promise<boolean>,
  watchDirectories: readonly string[],
  options: { contentionBudgetMs?: number; pollIntervalMs?: number } = {},
): Promise<boolean> {
  const contentionBudgetMs = options.contentionBudgetMs ?? 30_000;
  const pollIntervalMs = options.pollIntervalMs ?? 25;
  const deadline = Date.now() + contentionBudgetMs;
  const watchers: fsNode.FSWatcher[] = [];
  let wake: (() => void) | null = null;

  const watchNearest = (directory: string): void => {
    let candidate = directory;
    let depth = 0;
    // Watch the directory itself, or the nearest existing ancestor. Watching
    // an ancestor is only a best-effort signal; the bounded poll remains the
    // correctness guarantee.
    while (depth < 8) {
      try {
        watchers.push(fsNode.watch(candidate, { persistent: false }, () => wake?.()));
        return;
      } catch {
        const parent = path.dirname(candidate);
        if (parent === candidate) return;
        candidate = parent;
        depth += 1;
      }
    }
  };

  for (const directory of new Set(watchDirectories)) watchNearest(directory);
  try {
    for (;;) {
      if (await probe()) return true;
      const remaining = deadline - Date.now();
      if (remaining <= 0) return false;
      const event = new Promise<void>((resolve) => { wake = resolve; });
      const slice = Math.max(1, Math.min(pollIntervalMs, remaining));
      const timer = new Promise<'timer'>((resolve) => setTimeout(resolve, slice, 'timer'));
      await Promise.race([event.then(() => 'event' as const), timer]);
      wake = null;
    }
  } finally {
    wake = null;
    for (const watcher of watchers) watcher.close();
  }
}

function telemetryDirectory(root: string, stateRoot: string = root): string {
  return getInvocationTelemetryDirectory(root, { XDG_STATE_HOME: path.join(stateRoot, 'state') });
}

async function events(root: string, stateRoot: string = root): Promise<Record<string, any>[]> {
  const directory = getInvocationTelemetryDirectory(root, { XDG_STATE_HOME: path.join(stateRoot, 'state') });
  if (!(await fs.pathExists(directory))) return [];
  const names = (await fs.readdir(directory)).filter((name) => name.endsWith('.json'));
  return Promise.all(names.map(async (name) => fs.readJson(path.join(directory, name))));
}

function spawn(mode: string, root: string): childProcess.ChildProcess {
  return childProcess.spawn(process.execPath, ['--import', tsxLoader, helper, mode], {
    cwd: root,
    env: { ...process.env, XDG_STATE_HOME: path.join(root, 'state'), YYLO_LAUNCH_SURFACE: 'yy' },
    stdio: 'ignore',
  });
}

function close(child: childProcess.ChildProcess): Promise<{ code: number | null; signal: NodeJS.Signals | null }> {
  return new Promise((resolve, reject) => {
    child.once('error', reject);
    child.once('close', (code, signal) => resolve({ code, signal }));
  });
}

beforeAll(async () => {
  wrapperFixtureRoot = await fs.mkdtemp(path.join('/tmp', 'juno-wrapper-fixture-'));
  const bin = path.join(wrapperFixtureRoot, 'bin');
  await fs.ensureDir(bin);
  await fs.symlink(path.resolve('node_modules'), path.join(wrapperFixtureRoot, 'node_modules'));
  canonicalWrapper = path.join(bin, 'yylo');
  canonicalYy = path.join(bin, 'yy');
  canonicalYpl = path.join(bin, 'ypl');
  await Promise.all([
    fs.copy(wrapperSource, canonicalWrapper),
    fs.copy(wrapperSource, path.join(bin, 'yylo.sh')),
    fs.copy(yplSource, canonicalYpl),
  ]);
  // `yy` must resolve to the same installation as its canonical `yylo` peer,
  // exactly like a real package-manager bin layout. A bare copy would be a
  // separate inode: with @yylo/cli installed globally, `command -v yylo` then
  // resolves to the global peer and the mixed-installation guard must refuse.
  await fs.symlink('yylo', canonicalYy);
  await Promise.all([
    fs.chmod(canonicalWrapper, 0o755),
    fs.chmod(path.join(bin, 'yylo.sh'), 0o755),
    fs.chmod(canonicalYpl, 0o755),
  ]);
  const packageJson = await fs.readJson(path.resolve('package.json'));
  await fs.copy(path.resolve('package.json'), path.join(wrapperFixtureRoot, 'package.json'));
  await fs.ensureDir(path.join(wrapperFixtureRoot, 'templates', 'scripts'));
  await fs.copy(
    path.resolve('src/templates/scripts/controller_resolver.py'),
    path.join(wrapperFixtureRoot, 'templates', 'scripts', 'controller_resolver.py'),
  );
  await build({
    entryPoints: [path.resolve('src/bin/invocation-boundary.ts')],
    outfile: path.join(bin, 'invocation-boundary.mjs'),
    bundle: true,
    packages: 'external',
    platform: 'node',
    format: 'esm',
    target: 'node20',
    define: { __VERSION__: JSON.stringify(packageJson.version), __DEV__: 'false' },
  });
  await fs.writeFile(path.join(bin, 'cli.mjs'), `
// YYLO_PREFLIGHT_ONLY capability fixture
if (process.env.YYLO_PREFLIGHT_ONLY === '1' && process.env[['JUNO', 'CODE', 'WRAPPER', 'LIFECYCLE'].join('_')]) process.exit(70);
if (process.argv.includes('--version')) console.log('yylo ${packageJson.version}');
else if (process.argv.includes('--definitely-invalid')) process.exit(2);
else process.exit(0);
`);
}, 30_000);

afterAll(async () => { await fs.remove(wrapperFixtureRoot); });

afterEach(async () => {
  for (const root of roots.splice(0)) await fs.remove(root);
});

describe('direct CLI invocation lifecycle', () => {
  it.each([
    { args: ['--version'], expectedCode: 0, expectedStatus: 'success' },
    { args: ['--definitely-invalid'], expectedCode: 2, expectedStatus: 'failure' },
    { args: ['--__juno-launch-surface=ypl', '--version'], expectedCode: 2, expectedStatus: 'failure' },
  ])('instruments the actual direct CLI with $expectedStatus truth', async ({ args, expectedCode, expectedStatus }) => {
    const root = await temp(`actual-${expectedStatus}`);
    const result = childProcess.spawnSync(process.execPath, ['--import', tsxLoader, cliSource, ...args], {
      cwd: root,
      env: { ...process.env, XDG_STATE_HOME: path.join(root, 'state'), YYLO_LAUNCH_SURFACE: 'ypl' },
      encoding: 'utf8',
      timeout: 10_000,
    });
    expect(result.status).toBe(expectedCode);
    const written = await events(root);
    expect(written).toHaveLength(2);
    expect(written.find((event) => event.event_type === 'invocation_finished')).toMatchObject({
      launch_surface: 'yylo',
      juno_code_version: expect.any(String),
      status: expectedStatus,
      exit_code: expectedCode,
    });
  });

  it('does not expose the wrapper observation sink to provider descendants', async () => {
    const root = await temp('observation-env');
    const child = childProcess.spawn(process.execPath, ['--import', tsxLoader, helper, 'observation-env'], {
      cwd: root,
      env: {
        ...process.env,
        YYLO_WRAPPER_OBSERVATION: path.join(root, 'private-observation'),
      },
      stdio: 'ignore',
    });
    expect(await close(child)).toEqual({ code: 0, signal: null });
    expect(await fs.readJson(path.join(root, 'observation-env.json'))).toEqual({ current: null, child: '' });
  });

  it('does not adopt a spoofed regular descriptor or overwrite an ambient observation path', async () => {
    const root = await temp('ambient-spoof');
    const victim = path.join(root, 'victim');
    await fs.writeFile(victim, 'preserve');
    const descriptor = fs.openSync('/etc/hosts', 'r');
    let result: childProcess.SpawnSyncReturns<string>;
    try {
      result = childProcess.spawnSync(process.execPath, ['--import', tsxLoader, cliSource, '--version'], {
        cwd: root,
        env: {
          ...process.env,
          XDG_STATE_HOME: path.join(root, 'state'),
          YYLO_WRAPPER_LIFECYCLE: '9',
          YYLO_WRAPPER_OBSERVATION: victim,
        },
        stdio: ['ignore', 'pipe', 'pipe', 'ignore', 'ignore', 'ignore', 'ignore', 'ignore', 'ignore', descriptor],
        encoding: 'utf8', timeout: 10_000,
      });
    } finally {
      fs.closeSync(descriptor);
    }
    expect(result!.status, result!.stderr).toBe(0);
    expect(await fs.readFile(victim, 'utf8')).toBe('preserve');
    expect(await events(root)).toHaveLength(2);
  });

  it('does not let ambient control routing spoof direct project attribution', async () => {
    const root = await temp('routing-spoof');
    const spoofed = path.join(root, 'spoofed-project');
    await fs.ensureDir(spoofed);
    const result = childProcess.spawnSync(process.execPath, ['--import', tsxLoader, cliSource, '--version'], {
      cwd: root,
      env: {
        ...process.env,
        XDG_STATE_HOME: path.join(root, 'state'),
        JUNO_CONTROL_INVOCATION_ROOT: spoofed,
      },
      encoding: 'utf8', timeout: 10_000,
    });
    expect(result.status, result.stderr).toBe(0);
    expect(await events(root)).toHaveLength(2);
    expect(await events(spoofed, root)).toHaveLength(0);
  });

  it.each([
    ['yylo', () => canonicalWrapper],
    ['yy', () => canonicalYy],
    ['ypl', () => canonicalYpl],
  ] as const)('records exactly one lifecycle for the canonical %s wrapper', async (surface, executable) => {
    const root = await temp(`wrapper-${surface}`);
    // Resolve the wrapper's canonical peer inside the fixture installation so
    // an ambient global @yylo/cli installation cannot turn the fixture into a
    // mixed installation the wrapper must refuse.
    const fixtureBin = path.dirname(canonicalWrapper);
    const result = childProcess.spawnSync(executable(), ['--version'], {
      cwd: root,
      env: {
        ...process.env,
        PATH: `${fixtureBin}${path.delimiter}${process.env.PATH ?? ''}`,
        XDG_STATE_HOME: path.join(root, 'state'),
      },
      encoding: 'utf8',
      timeout: 10_000,
    });
    expect(result.status, result.stderr).toBe(0);
    const written = await events(root);
    expect(written.filter((event) => event.event_type === 'invocation_started')).toEqual([
      expect.objectContaining({ launch_surface: surface }),
    ]);
    expect(written.filter((event) => event.event_type === 'invocation_finished')).toEqual([
      expect.objectContaining({ launch_surface: surface, status: 'success', exit_code: 0 }),
    ]);
  });

  it('records exactly one canonical wrapper lifecycle on explicit-command refusal', async () => {
    const root = await temp('wrapper-refusal');
    const result = childProcess.spawnSync(canonicalWrapper, ['--definitely-invalid'], {
      cwd: root,
      env: { ...process.env, XDG_STATE_HOME: path.join(root, 'state') },
      encoding: 'utf8',
      timeout: 10_000,
    });
    expect(result.status, JSON.stringify({ stdout: result.stdout, stderr: result.stderr, error: result.error?.message })).toBe(2);
    const written = await events(root);
    expect(written.filter((event) => event.event_type === 'invocation_started')).toHaveLength(1);
    expect(written.filter((event) => event.event_type === 'invocation_finished')).toEqual([
      expect.objectContaining({ status: 'failure', exit_code: 2, launch_surface: 'yylo' }),
    ]);
  });

  it('does not replace command truth when the optional boundary module fails', async () => {
    const root = await temp('boundary-module-failure');
    const bin = path.join(root, 'bin');
    await fs.ensureDir(bin);
    const wrapper = path.join(bin, 'yylo');
    await fs.copy(wrapperSource, wrapper);
    await fs.chmod(wrapper, 0o755);
    await fs.copy(path.join(wrapperFixtureRoot, 'bin', 'cli.mjs'), path.join(bin, 'cli.mjs'));
    await fs.writeFile(path.join(bin, 'invocation-boundary.mjs'), "throw new Error('broken boundary fixture');\n");
    const result = childProcess.spawnSync(wrapper, ['--version'], {
      cwd: root, encoding: 'utf8', timeout: 10_000,
      env: { ...process.env, XDG_STATE_HOME: path.join(root, 'state') },
    });
    expect(result.status).toBe(0);
    expect(result.stdout).toContain('yylo');
  });

  it.each([
    ['yylo', false],
    ['ypl', true],
  ] as const)('preserves piped stdin through the canonical %s wrapper', async (name, useYpl) => {
    const root = await temp(`stdin-${name}`);
    const bin = path.join(root, 'bin');
    await fs.ensureDir(bin);
    await fs.copy(wrapperSource, path.join(bin, 'yylo.sh'));
    await fs.chmod(path.join(bin, 'yylo.sh'), 0o755);
    const executable = useYpl ? path.join(bin, 'ypl') : path.join(bin, 'yylo');
    if (useYpl) await fs.copy(yplSource, executable);
    else await fs.copy(wrapperSource, executable);
    await fs.chmod(executable, 0o755);
    await fs.writeFile(path.join(bin, 'cli.mjs'), `
let data = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => data += chunk);
process.stdin.on('end', () => process.stdout.write(data));
`);
    const result = childProcess.spawnSync(executable, [], {
      cwd: root,
      input: 'preserved-input\n',
      encoding: 'utf8',
      timeout: 10_000,
    });
    expect(result.status, result.stderr).toBe(0);
    expect(result.stdout).toBe('preserved-input\n');
  });

  it('preserves the producer PID when a lifecycle-capable current runtime is hard-killed', async () => {
    const root = await temp('wrapper-pid-kill');
    const bin = path.join(root, 'bin');
    await fs.ensureDir(bin);
    await fs.symlink(path.resolve('node_modules'), path.join(root, 'node_modules'));
    const wrapper = path.join(bin, 'yylo');
    await fs.copy(wrapperSource, wrapper);
    await fs.chmod(wrapper, 0o755);
    await fs.copy(path.join(wrapperFixtureRoot, 'bin', 'invocation-boundary.mjs'), path.join(bin, 'invocation-boundary.mjs'));
    await fs.writeFile(path.join(bin, 'cli.mjs'), `
// YYLO_WRAPPER_LIFECYCLE capability fixture
import { writeFileSync } from 'node:fs';
writeFileSync('runtime-pid', String(process.pid));
setInterval(() => {}, 1000);
`);
    const child = childProcess.spawn(wrapper, [], {
      cwd: root,
      env: { ...process.env, XDG_STATE_HOME: path.join(root, 'state') },
      stdio: 'ignore',
    });
    const runtimePid = path.join(root, 'runtime-pid');
    expect(await waitUntil(
      async () => {
        try {
          return Number(await fs.readFile(runtimePid, 'utf8')) === child.pid;
        } catch {
          return false;
        }
      },
      [root],
    )).toBe(true);
    expect(Number(await fs.readFile(runtimePid, 'utf8'))).toBe(child.pid);
    child.kill('SIGKILL');
    expect(await close(child)).toEqual({ code: null, signal: 'SIGKILL' });
    expect((await events(root)).map((event) => event.event_type)).toEqual(['invocation_started']);
  });

  it('leaves one canonical wrapper start unmatched when hard-killed during bootstrap', async () => {
    const root = await temp('wrapper-bootstrap-kill');
    const scripts = path.join(root, '.juno_task', 'scripts');
    await fs.ensureDir(scripts);
    await fs.writeFile(path.join(scripts, 'bootstrap.sh'), '#!/usr/bin/env bash\necho ready > bootstrap-ready\nwhile true; do sleep 1; done\n');
    const child = childProcess.spawn(canonicalWrapper, ['pi', '-p', 'test'], {
      cwd: root,
      env: { ...process.env, XDG_STATE_HOME: path.join(root, 'state') },
      stdio: 'ignore',
      detached: true,
    });
    await waitForEvent(root);
    process.kill(-child.pid!, 'SIGKILL');
    expect(await close(child)).toEqual({ code: null, signal: 'SIGKILL' });
    expect((await events(root)).map((event) => event.event_type)).toEqual(['invocation_started']);
  });

  it('records exactly one finish when bootstrap exits before runtime dispatch', async () => {
    const root = await temp('wrapper-bootstrap-failure');
    const scripts = path.join(root, '.juno_task', 'scripts');
    await fs.ensureDir(scripts);
    await fs.writeFile(path.join(scripts, 'bootstrap.sh'), '#!/usr/bin/env bash\nexit 17\n');
    const result = childProcess.spawnSync(canonicalWrapper, ['pi', '-p', 'test'], {
      cwd: root,
      env: { ...process.env, XDG_STATE_HOME: path.join(root, 'state') },
      encoding: 'utf8',
      timeout: 10_000,
    });
    expect(result.status, result.stderr).toBe(17);
    const written = await events(root);
    expect(written.filter((event) => event.event_type === 'invocation_started')).toHaveLength(1);
    expect(written.filter((event) => event.event_type === 'invocation_finished')).toEqual([
      expect.objectContaining({ status: 'failure', exit_code: 17 }),
    ]);
  });

  it('wrapper-finalizes a successful same-version pinned runtime without continuation support', async () => {
    const root = await temp('pinned-runtime');
    const controller = path.join(root, 'controller');
    const product = path.join(root, 'product');
    await fs.ensureDir(path.join(controller, '.juno_task'));
    const git = (args: string[], cwd: string) => childProcess.spawnSync('git', args, { cwd, encoding: 'utf8' });
    expect(git(['init', '-b', 'controller'], controller).status).toBe(0);
    expect(git(['config', 'user.email', 'test@example.invalid'], controller).status).toBe(0);
    expect(git(['config', 'user.name', 'Test'], controller).status).toBe(0);
    await fs.writeFile(path.join(controller, '.juno_task', '.keep'), '');
    expect(git(['add', '.'], controller).status).toBe(0);
    expect(git(['commit', '-m', 'fixture'], controller).status).toBe(0);
    expect(git(['worktree', 'add', '-b', 'product', product], controller).status).toBe(0);
    expect(git(['config', 'extensions.worktreeConfig', 'true'], product).status).toBe(0);
    expect(git(['config', 'juno.controller.path', controller], product).status).toBe(0);
    expect(git(['config', 'juno.controller.branch', 'refs/heads/controller'], product).status).toBe(0);
    expect(git(['config', '--worktree', 'juno.workspace.role', 'integration-owner'], product).status).toBe(0);
    expect(git(['config', '--worktree', 'juno.workspace.roleAuthority', 'protected-integration.v1'], product).status).toBe(0);
    const packageJson = await fs.readJson(path.resolve('package.json'));
    const runtime = path.join(controller, 'pinned-runtime.mjs');
    await fs.writeFile(runtime, `if (process.argv.includes('--version')) console.log('yylo ${packageJson.version}'); else process.exitCode = 0;\n`);
    expect(git(['config', '--worktree', 'juno.controller.runtimeExecutable', runtime], controller).status).toBe(0);

    // The fixture yy already resolves to the same installation as its yylo
    // peer (symlink); keep that invariant and resolve the peer inside the
    // fixture so an ambient global installation cannot cause a refusal.
    const yy = path.join(wrapperFixtureRoot, 'bin', 'yy');
    const result = childProcess.spawnSync(yy, ['merge', 'status'], {
      cwd: product,
      env: {
        ...process.env,
        PATH: `${path.dirname(yy)}${path.delimiter}${process.env.PATH ?? ''}`,
        XDG_STATE_HOME: path.join(root, 'state'),
      },
      encoding: 'utf8', timeout: 15_000,
    });
    expect(result.status, result.stderr).toBe(0);
    const written = await events(product, root);
    expect(written.filter((event) => event.event_type === 'invocation_started')).toHaveLength(1);
    expect(written.filter((event) => event.event_type === 'invocation_finished')).toEqual([
      expect.objectContaining({ launch_surface: 'yy', status: 'success', exit_code: 0 }),
    ]);
  }, 30_000);

  it('terminates an unauthenticated controller refusal before provider dispatch', async () => {
    const root = await temp('generation-refusal');
    const marker = path.join(root, 'provider-started');
    const { project, home } = await createActualProject(root,
      `#!/usr/bin/env python3\nopen(${JSON.stringify(marker)}, "w").close()\n`);
    await fs.ensureDir(path.join(project, '.juno_task'));
    const result = childProcess.spawnSync(process.execPath, [
      '--import', tsxLoader, cliSource, 'pi', '--cwd', project, '-p', 'test',
      '--model', 'openai/gpt-5.2', '--quiet', '--no-hooks',
    ], {
      cwd: root,
      env: actualProjectEnvironment(root, home),
      encoding: 'utf8', timeout: 20_000,
    });
    expect(result.error).toBeUndefined();
    expect(result.status, result.stderr).toBe(2);
    expect(result.stderr).toContain('generation_provenance_required');
    expect(result.stderr).toContain('Preserve controller bytes');
    expect(await fs.pathExists(marker)).toBe(false);
    expect(await fs.readdir(path.join(project, '.juno_task'))).toEqual([]);
    const written = await events(project, root);
    expect(written.filter(event => event.event_type === 'invocation_started')).toHaveLength(1);
    expect(written.filter(event => event.event_type === 'invocation_finished')).toEqual([
      expect.objectContaining({ status: 'failure', exit_code: 2 }),
    ]);
  }, 30_000);

  it('preserves a real provider main failure exit and records resolved request context', async () => {
    const root = await temp('actual-provider-failure');
    const { project, home } = await createActualProject(root, `#!/usr/bin/env python3
import json, sys
print(json.dumps({"type":"session","id":"provider-failure-session"}))
print("Error: joined provider failure", file=sys.stderr)
sys.exit(1)
`);

    const result = childProcess.spawnSync(process.execPath, [
      '--import', tsxLoader, cliSource, 'pi', '--cwd', project, '-p', 'test',
      '--model', 'openai/gpt-5.2', '--quiet', '--no-hooks',
    ], {
      cwd: root,
      env: actualProjectEnvironment(root, home),
      encoding: 'utf8',
      timeout: 20_000,
    });
    expect(result.status, result.stderr).toBe(1);
    expect(result.status).not.toBe(99);
    const written = await events(project, root);
    expect(written).toHaveLength(2);
    for (const event of written) {
      expect(event).toMatchObject({
        launch_surface: 'yylo',
        service: 'pi',
        requested_model: 'openai/gpt-5.2',
      });
    }
    expect(written.find((event) => event.event_type === 'invocation_finished')).toMatchObject({
      status: 'failure',
      exit_code: 1,
      provider_observations: {
        status: 'unavailable',
        execution_service: 'pi',
        observations: [],
      },
    });
  }, 25_000);

  it.each([
    ['semantic', `#!/usr/bin/env python3
import json
print(json.dumps({"type":"result","subtype":"error","is_error":True,"result":"semantic refusal"}))
`, 'failure'],
    ['transport', `#!/usr/bin/env python3
import sys
print("ECONNRESET: transport unavailable", file=sys.stderr)
sys.exit(1)
`, 'failure'],
  ] as const)('classifies an actual CLI %s terminal path', async (kind, source, expectedStatus) => {
    const root = await temp(`actual-${kind}`);
    const { project, home } = await createActualProject(root, source);
    const result = childProcess.spawnSync(process.execPath, [
      '--import', tsxLoader, cliSource, 'pi', '--cwd', project, '-p', 'test',
      '--quiet', '--no-hooks',
    ], {
      cwd: root,
      env: actualProjectEnvironment(root, home),
      encoding: 'utf8',
      timeout: 20_000,
    });
    expect(result.status, result.stderr).toBe(1);
    const written = await events(project, root);
    expect(written.filter((event) => event.event_type === 'invocation_started')).toHaveLength(1);
    expect(written.filter((event) => event.event_type === 'invocation_finished')).toEqual([
      expect.objectContaining({
        status: expectedStatus,
        exit_code: 1,
        service: 'pi',
        provider_observations: expect.objectContaining({ status: 'unavailable', observations: [] }),
      }),
    ]);
  }, 25_000);

  it('classifies an actual CLI until-completion timeout', async () => {
    const root = await temp('actual-timeout');
    const project = path.join(root, 'project');
    const script = path.join(project, '.juno_task', 'scripts', 'run_until_completion.sh');
    await fs.ensureDir(path.dirname(script));
    await fs.writeFile(script, '#!/usr/bin/env bash\nexit 124\n', { mode: 0o755 });
    const result = childProcess.spawnSync(process.execPath, [
      '--import', tsxLoader, cliSource, 'pi', '--until-completion', '--cwd', project, '-p', 'test',
    ], {
      cwd: root,
      env: { ...process.env, XDG_STATE_HOME: path.join(root, 'state') },
      encoding: 'utf8',
      timeout: 10_000,
    });
    expect(result.status, result.stderr).toBe(124);
    const written = await events(project, root);
    expect(written.filter((event) => event.event_type === 'invocation_started')).toHaveLength(1);
    expect(written.filter((event) => event.event_type === 'invocation_finished')).toEqual([
      expect.objectContaining({ status: 'timeout', exit_code: 124, service: 'pi' }),
    ]);
  });

  it.each(['SIGINT', 'SIGTERM'] as const)(
    'uses the actual CLI %s handler and emits exactly one graceful interrupted finish',
    async (signal) => {
      const root = await temp(`actual-${signal.toLowerCase()}`);
      const script = path.join(root, '.juno_task', 'scripts', 'run_until_completion.sh');
      await fs.ensureDir(path.dirname(script));
      await fs.writeFile(script, `#!/usr/bin/env bash\ntrap 'exit 0' INT TERM\necho ready > child-ready\nwhile true; do sleep 0.05; done\n`, { mode: 0o755 });
      const child = childProcess.spawn(process.execPath, [
        '--import', tsxLoader, cliSource, 'pi', '--until-completion', '-p', 'test',
      ], {
        cwd: root,
        env: { ...process.env, XDG_STATE_HOME: path.join(root, 'state') },
        stdio: 'ignore',
      });
      expect(await waitUntil(
        async () => fs.pathExists(path.join(root, 'child-ready')),
        [root],
      )).toBe(true);
      child.kill(signal);
      expect(await close(child)).toEqual({ code: 0, signal: null });
      const written = await events(root);
      expect(written.filter((event) => event.event_type === 'invocation_started')).toHaveLength(1);
      expect(written.filter((event) => event.event_type === 'invocation_finished')).toEqual([
        expect.objectContaining({ status: 'interrupted', exit_code: 0, service: 'pi' }),
      ]);
    },
    15_000,
  );

  it('keeps actual CLI success truthful when both telemetry writes fail visibly', async () => {
    const root = await temp('actual-write-failure');
    const invalidStateHome = path.join(root, 'not-a-directory');
    await fs.writeFile(invalidStateHome, 'x');
    const before = performance.now();
    const result = childProcess.spawnSync(process.execPath, ['--import', tsxLoader, cliSource, '--version'], {
      cwd: root,
      env: { ...process.env, XDG_STATE_HOME: invalidStateHome },
      encoding: 'utf8',
      timeout: 10_000,
    });
    expect(result.status).toBe(0);
    expect(performance.now() - before).toBeLessThan(3_000);
    expect(result.stderr).toContain('invocation_started write failed');
    expect(result.stderr).toContain('invocation_finished write failed');
    expect(result.stderr).not.toContain('Unexpected Error');
  });

  it.each([
    ['success', 0, 'success'],
    ['failure', 7, 'failure'],
    ['timeout', 124, 'timeout'],
  ] as const)('records one paired %s lifecycle with actual exit truth', async (mode, code, status) => {
    const root = await temp(mode);
    const child = spawn(mode, root);
    expect(await close(child)).toEqual({ code, signal: null });

    const written = await events(root);
    const started = written.find((event) => event.event_type === 'invocation_started')!;
    const finished = written.find((event) => event.event_type === 'invocation_finished')!;
    expect(written).toHaveLength(2);
    expect(started).toMatchObject({ launch_surface: 'yy', service: 'yylo', juno_code_version: '9.8.7-test' });
    expect(finished).toMatchObject({
      request_id: started.request_id,
      trace_id: started.trace_id,
      span_id: started.span_id,
      status,
      exit_code: code,
    });
    expect(new Date(started.started_at).toISOString()).toBe(started.started_at);
    expect(new Date(finished.finished_at).toISOString()).toBe(finished.finished_at);
    expect(finished.finished_monotonic_ms).toBeGreaterThanOrEqual(started.started_monotonic_ms);
    expect(finished.duration_ms).toBeCloseTo(
      finished.finished_monotonic_ms - started.started_monotonic_ms,
      8,
    );
  });

  it('reconstructs canonical parent-child spans while an unrelated subprocess remains a fresh root', async () => {
    const root = await temp('correlation-tree');
    const child = spawn('tree-parent', root);
    expect(await close(child)).toEqual({ code: 0, signal: null });

    const starts = (await events(root)).filter((event) => event.event_type === 'invocation_started');
    expect(starts).toHaveLength(5);
    const workflow = starts.find((event) => event.launch_surface === 'workflow_runner')!;
    const parent = starts.find((event) => event.span_id === workflow.parent_span_id)!;
    const managed = starts.find((event) => event.launch_surface === 'managed_agent_runner')!;
    const parallel = starts.find((event) => event.launch_surface === 'parallel_runner')!;
    const roots = starts.filter((event) => event.parent_span_id === null);
    const unrelated = roots.find((event) => event.span_id !== parent.span_id)!;

    expect(parent).toMatchObject({ launch_surface: 'yy', parent_span_id: null });
    expect(workflow).toMatchObject({
      trace_id: parent.trace_id,
      parent_span_id: parent.span_id,
      task_id: 'TASK-42',
      workflow_run_id: 'run-7',
      workflow_step_id: 'step-a',
    });
    expect(managed).toMatchObject({
      trace_id: parent.trace_id,
      parent_span_id: workflow.span_id,
      task_id: 'TASK-43',
      workflow_run_id: 'run-7',
      workflow_step_id: 'step-a',
    });
    expect(parallel).toMatchObject({
      trace_id: parent.trace_id,
      parent_span_id: parent.span_id,
      task_id: 'BATCH-1',
    });
    expect(unrelated.trace_id).not.toBe(parent.trace_id);
    expect(unrelated).toMatchObject({
      parent_span_id: null, task_id: null, workflow_run_id: null, workflow_step_id: null,
    });
  });

  it.each(['SIGINT', 'SIGTERM'] as const)(
    'preserves graceful %s exit 0 and emits exactly one interrupted finish',
    async (signal) => {
      const root = await temp(signal.toLowerCase());
      const child = spawn('wait', root);
      expect(await waitUntil(
        async () => (await events(root)).length > 0,
        [telemetryDirectory(root)],
        { contentionBudgetMs: 15_000 },
      )).toBe(true);
      child.kill(signal);
      expect(await close(child)).toEqual({ code: 0, signal: null });
      const written = await events(root);
      expect(written.filter((event) => event.event_type === 'invocation_started')).toHaveLength(1);
      expect(written.filter((event) => event.event_type === 'invocation_finished')).toEqual([
        expect.objectContaining({ status: 'interrupted', exit_code: 0 }),
      ]);
    },
  );

  it('does not dispatch when a signal arrives during the bounded start write', async () => {
    const root = await temp('slow-start-signal');
    const child = spawn('slow-start', root);
    // The child opens a 10ms signal grace window right after `ready` appears;
    // keep the poll slice tight so the kill lands inside that window.
    expect(await waitUntil(
      async () => fs.pathExists(path.join(root, 'ready')),
      [root],
      { pollIntervalMs: 1 },
    )).toBe(true);
    child.kill('SIGTERM');
    expect(await close(child)).toEqual({ code: 0, signal: null });
    expect(await fs.pathExists(path.join(root, 'dispatched'))).toBe(false);
    const written = await events(root);
    expect(written.map((event) => event.event_type).sort()).toEqual([
      'invocation_finished',
      'invocation_started',
    ]);
    expect(written.find((event) => event.event_type === 'invocation_finished')).toMatchObject({
      status: 'interrupted',
      exit_code: 0,
    });
  });

  it('joins until-completion child shutdown before its interrupted terminal event', async () => {
    const root = await temp('until-signal');
    const project = path.join(root, 'project');
    const script = path.join(project, '.juno_task', 'scripts', 'run_until_completion.sh');
    await fs.ensureDir(path.dirname(script));
    await fs.writeJson(path.join(project, '.juno_task', 'config.json'), {
      defaultModels: { pi: 'configured/model-is-resolved-not-requested' },
    });
    await fs.writeFile(script, `#!/usr/bin/env bash
trap 'sleep 0.2; echo done > child-finished; exit 0' TERM
echo ready > child-ready
while true; do sleep 0.05; done
`, { mode: 0o755 });

    const child = childProcess.spawn(process.execPath, [
      '--import', tsxLoader, cliSource, 'pi', '--until-completion', '--cwd', project, '-p', 'test',
    ], {
      cwd: root,
      env: { ...process.env, XDG_STATE_HOME: path.join(root, 'state') },
      stdio: 'ignore',
    });
    expect(await waitUntil(
      async () => fs.pathExists(path.join(project, 'child-ready')),
      [project],
    )).toBe(true);
    child.kill('SIGTERM');
    expect(await close(child)).toEqual({ code: 0, signal: null });
    expect(await fs.readFile(path.join(project, 'child-finished'), 'utf8')).toBe('done\n');
    const written = await events(project, root);
    expect(written.filter((event) => event.event_type === 'invocation_started')).toHaveLength(1);
    expect(written.filter((event) => event.event_type === 'invocation_finished')).toEqual([
      expect.objectContaining({
        status: 'interrupted',
        exit_code: 0,
        service: 'pi',
        requested_model: 'configured/model-is-resolved-not-requested',
      }),
    ]);
  });

  it('writes the actual CLI start before blocking config and leaves it unmatched after SIGKILL', async () => {
    const root = await temp('actual-sigkill');
    const fifo = path.join(root, 'blocked-config');
    expect(childProcess.spawnSync('mkfifo', [fifo]).status).toBe(0);
    const child = childProcess.spawn(process.execPath, [
      '--import', tsxLoader, cliSource, 'pi', '--config', fifo, '-p', 'test',
    ], {
      cwd: root,
      env: { ...process.env, XDG_STATE_HOME: path.join(root, 'state') },
      stdio: 'ignore',
    });
    await waitForEvent(root);
    child.kill('SIGKILL');
    expect(await close(child)).toEqual({ code: null, signal: 'SIGKILL' });
    expect((await events(root)).map((event) => event.event_type)).toEqual(['invocation_started']);
  });

  it('bounds and exposes telemetry failures without replacing command truth', async () => {
    const warnings: string[] = [];
    const lifecycle = new InvocationLifecycle({
      workingDirectory: '/tmp',
      junoCodeVersion: 'test',
      writeTimeoutMs: 20,
      writeEvent: async () => new Promise(() => undefined),
      warn: (message) => warnings.push(message),
    });
    const before = performance.now();
    await lifecycle.start();
    await lifecycle.finish(0);
    expect(performance.now() - before).toBeLessThan(200);
    expect(warnings).toEqual([
      expect.stringContaining('invocation_started write failed: write exceeded 20ms'),
      expect.stringContaining('invocation_finished write failed: write exceeded 20ms'),
    ]);
  });

  it('consumes child transport before runtime/provider descendants can inherit it', () => {
    const root = new InvocationLifecycle({ workingDirectory: '/tmp', junoCodeVersion: 'test', env: {} });
    const env: NodeJS.ProcessEnv = {
      YYLO_INVOCATION_CHILD: '1',
      YYLO_TRACE_ID: 'stale-trace',
      YYLO_PARENT_SPAN_ID: 'stale-span',
      YYLO_TASK_ID: 'stale-task',
      YYLO_LAUNCH_SURFACE: 'workflow_runner',
    };
    new InvocationLifecycle({
      workingDirectory: '/tmp', junoCodeVersion: 'test', env,
      continuation: root.continuation(),
    });
    expect(env).not.toHaveProperty('YYLO_INVOCATION_CHILD');
    expect(env).not.toHaveProperty('YYLO_TRACE_ID');
    expect(env).not.toHaveProperty('YYLO_PARENT_SPAN_ID');
    expect(env).not.toHaveProperty('YYLO_TASK_ID');
    expect(env).not.toHaveProperty('YYLO_LAUNCH_SURFACE');
    expect(env).toMatchObject({
      YYLO_ACTIVE_TRACE_ID: root.continuation().identity.trace_id,
      YYLO_ACTIVE_SPAN_ID: root.continuation().identity.span_id,
    });
  });

  it('records configured project routing, resolved service, and requested model on both events', async () => {
    const written: Array<{ cwd: string; event: Record<string, unknown> }> = [];
    const lifecycle = new InvocationLifecycle({
      workingDirectory: '/invocation',
      junoCodeVersion: 'test',
      launchSurface: 'ypl',
      env: { YYLO_LAUNCH_SURFACE: 'yy' },
      writeEvent: async (cwd, event) => { written.push({ cwd, event }); },
    });
    await lifecycle.start({
      workingDirectory: '/selected-project',
      service: 'pi',
      requestedModel: 'openai/gpt-5.2',
    });
    await lifecycle.finish(0);
    expect(written).toHaveLength(2);
    expect(written.map(({ cwd }) => cwd)).toEqual(['/selected-project', '/selected-project']);
    for (const { event } of written) {
      expect(event).toMatchObject({
        launch_surface: 'ypl',
        service: 'pi',
        requested_model: 'openai/gpt-5.2',
      });
    }
  });

  it('keeps immutable lifecycle fields while attaching resolved terminal observations', async () => {
    const written: Record<string, unknown>[] = [];
    const lifecycle = new InvocationLifecycle({
      workingDirectory: '/boundary-project',
      junoCodeVersion: 'test',
      writeEvent: async (_cwd, event) => { written.push(event); },
    });
    await lifecycle.start({ service: 'pi', requestedModel: 'explicit/model' });
    lifecycle.observeProviderResult({
      request: { subagent: 'pi' },
      iterations: [{
        toolResult: {
          content: JSON.stringify({
            type: 'result',
            session_id: 'provider-session',
            usage: { input: 3 },
            sub_agent_response: { provider: 'openai', model: 'gpt-5' },
          }),
          metadata: { structuredOutput: true },
        },
      }],
    });
    await lifecycle.start({ service: 'claude', requestedModel: 'configured/default' });
    await lifecycle.finish(0);
    expect(written).toHaveLength(2);
    expect(written[0]).toMatchObject({ service: 'pi', requested_model: 'explicit/model' });
    expect(written[1]).toMatchObject({
      service: 'claude',
      requested_model: 'configured/default',
      request_id: written[0]?.request_id,
      trace_id: written[0]?.trace_id,
      span_id: written[0]?.span_id,
      provider_observations: {
        status: 'partial',
        execution_service: 'pi',
        observations: [expect.objectContaining({
          session_id: 'provider-session', provider: 'openai', resolved_model: 'gpt-5',
        })],
      },
    });
  });

  it('adds explicit unavailable provider truth to non-provider terminal paths', async () => {
    const written: Record<string, unknown>[] = [];
    const lifecycle = new InvocationLifecycle({
      workingDirectory: '/tmp',
      junoCodeVersion: 'test',
      writeEvent: async (_cwd, event) => { written.push(event); },
    });
    await lifecycle.start({ service: 'yylo' });
    await lifecycle.finish(1);
    expect(written.at(-1)).toMatchObject({
      status: 'failure',
      provider_observations: {
        status: 'unavailable',
        execution_service: 'yylo',
        observations: [],
        usage: { status: 'unavailable', input_tokens: null, output_tokens: null },
        estimated_cost: { status: 'unavailable', amount: null, currency: null },
      },
    });
  });

  it('attempts only one finish under competing terminal paths', async () => {
    const written: Record<string, unknown>[] = [];
    const lifecycle = new InvocationLifecycle({
      workingDirectory: '/tmp',
      junoCodeVersion: 'test',
      writeEvent: async (_cwd, event) => { written.push(event); },
    });
    await lifecycle.start();
    await Promise.all([lifecycle.finish(1), lifecycle.finish(0), lifecycle.finish(124)]);
    expect(written.filter((event) => event.event_type === 'invocation_finished')).toHaveLength(1);
    expect(written.at(-1)).toMatchObject({ status: 'failure', exit_code: 1 });
  });
});
