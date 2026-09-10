import { describe, expect, it } from 'vitest';
import { execa } from 'execa';
import * as fs from 'fs-extra';
import * as os from 'node:os';
import * as path from 'node:path';

const PROJECT_ROOT = path.resolve(__dirname, '../../..');
const PACKAGE_JSON = path.join(PROJECT_ROOT, 'package.json');
const PACKAGE_LOCK_JSON = path.join(PROJECT_ROOT, 'package-lock.json');
const YPL_SOURCE = path.join(PROJECT_ROOT, 'src/bin/ypl.sh');
const YYLO_SOURCE = path.join(PROJECT_ROOT, 'src/bin/yylo.sh');
describe('ypl wrapper', () => {
  it('is exposed as an npm binary beside yy', async () => {
    const pkg = await fs.readJson(PACKAGE_JSON);

    const lock = await fs.readJson(PACKAGE_LOCK_JSON);

    expect(pkg.bin.yy).toBe('./dist/bin/yylo.sh');
    expect(pkg.bin.ypl).toBe('./dist/bin/ypl.sh');
    expect(lock.packages[''].bin.ypl).toBe('dist/bin/ypl.sh');
    expect(pkg.scripts['build:copy-wrapper']).toContain('src/bin/ypl.sh');
    expect(pkg.scripts['build:copy-wrapper']).toContain('dist/bin/ypl.sh');
  });

  it('keeps --version read-only by bypassing project bootstrap', async () => {
    const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-version-wrapper-'));
    try {
      const binDir = path.join(tempDir, 'bin');
      const scriptsDir = path.join(tempDir, '.juno_task', 'scripts');
      const bootstrapMarker = path.join(tempDir, 'bootstrap-ran');
      await fs.ensureDir(binDir);
      await fs.ensureDir(scriptsDir);
      await fs.copy(YYLO_SOURCE, path.join(binDir, 'yylo.sh'));
      await fs.chmod(path.join(binDir, 'yylo.sh'), 0o755);
      await fs.writeFile(
        path.join(binDir, 'cli.mjs'),
        'console.log(JSON.stringify(process.argv.slice(2)))\n',
        'utf8',
      );
      await fs.writeFile(
        path.join(scriptsDir, 'bootstrap.sh'),
        `#!/usr/bin/env bash\ntouch "${bootstrapMarker}"\nexit 91\n`,
        { mode: 0o755 },
      );

      const result = await execa(path.join(binDir, 'yylo.sh'), ['--version'], {
        cwd: tempDir,
        reject: false,
      });

      expect(result.exitCode, result.stderr).toBe(0);
      expect(JSON.parse(result.stdout)).toEqual(['--version']);
      expect(await fs.pathExists(bootstrapMarker)).toBe(false);
    } finally {
      await fs.remove(tempDir);
    }
  });

  it.each([
    ['--help'],
    ['ledger', '--help'],
    ['kanban', '--help'],
    ['task', '-h'],
    ['scripts', 'update', '--help'],
    ['integration', 'runtime-refresh', '-h'],
    ['migrate', 'target-runtime-provenance', 'plan', '--help'],
  ])('keeps terminal help out of wrapper lifecycle, routing, and bootstrap: %s', async (...args) => {
    const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-help-wrapper-'));
    try {
      const binDir = path.join(tempDir, 'bin');
      const scriptsDir = path.join(tempDir, '.juno_task', 'scripts');
      const bootstrapMarker = path.join(tempDir, 'bootstrap-ran');
      const lifecycleMarker = path.join(tempDir, 'lifecycle-ran');
      await fs.ensureDir(binDir);
      await fs.ensureDir(scriptsDir);
      await fs.copy(YYLO_SOURCE, path.join(binDir, 'yylo.sh'));
      await fs.chmod(path.join(binDir, 'yylo.sh'), 0o755);
      await fs.writeFile(path.join(binDir, 'cli.mjs'), 'console.log(JSON.stringify(process.argv.slice(2)))\n');
      await fs.writeFile(
        path.join(binDir, 'invocation-boundary.mjs'),
        `import { writeFileSync } from 'node:fs'; writeFileSync(${JSON.stringify(lifecycleMarker)}, 'ran');\n`,
      );
      await fs.writeFile(
        path.join(scriptsDir, 'bootstrap.sh'),
        `#!/usr/bin/env bash\ntouch "${bootstrapMarker}"\nexit 91\n`,
        { mode: 0o755 },
      );

      const result = await execa(path.join(binDir, 'yylo.sh'), args, { cwd: tempDir, reject: false });

      expect(result.exitCode, result.stderr).toBe(0);
      expect(JSON.parse(result.stdout)).toEqual(args);
      expect(await fs.pathExists(bootstrapMarker)).toBe(false);
      expect(await fs.pathExists(lifecycleMarker)).toBe(false);
    } finally {
      await fs.remove(tempDir);
    }
  });

  it.each([['ledger', 'list'], ['kanban', 'list'], ['task', 'status', 'T1'], ['merge', 'status'], ['integration', 'status'], ['info'], ['where', 'controller'], ['doctor', 'workspace']])(
    'classifies %s before checkout bootstrap',
    async (...args) => {
      const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-control-wrapper-'));
      try {
        const binDir = path.join(tempDir, 'bin');
        const scriptsDir = path.join(tempDir, '.juno_task', 'scripts');
        const marker = path.join(tempDir, 'bootstrap-ran');
        await fs.ensureDir(binDir);
        await fs.ensureDir(scriptsDir);
        await fs.copy(YYLO_SOURCE, path.join(binDir, 'yylo.sh'));
        await fs.chmod(path.join(binDir, 'yylo.sh'), 0o755);
        await fs.writeFile(path.join(binDir, 'cli.mjs'), 'console.log(JSON.stringify(process.argv.slice(2)))\n');
        await fs.writeFile(path.join(scriptsDir, 'bootstrap.sh'), `#!/usr/bin/env bash\ntouch "${marker}"\nexit 91\n`);
        const result = await execa(path.join(binDir, 'yylo.sh'), args, { cwd: tempDir, reject: false });
        expect(result.exitCode).toBe(0);
        expect(JSON.parse(result.stdout)).toEqual(args);
        expect(await fs.pathExists(marker)).toBe(false);
      } finally {
        await fs.remove(tempDir);
      }
    },
  );

  it.each([
    ['--quiet', 'ledger', 'list'],
    ['--quiet', 'kanban', 'list'],
    ['--config', 'controller.json', '--no-color', 'task', 'status', 'T1'],
    ['-v', '0', 'merge', 'status'],
  ])('classifies a control command after leading global options: %s', async (...args) => {
    const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-leading-option-wrapper-'));
    try {
      const binDir = path.join(tempDir, 'bin');
      const scriptsDir = path.join(tempDir, '.juno_task', 'scripts');
      const marker = path.join(tempDir, 'bootstrap-ran');
      await fs.ensureDir(binDir);
      await fs.ensureDir(scriptsDir);
      await fs.copy(YYLO_SOURCE, path.join(binDir, 'yylo.sh'));
      await fs.chmod(path.join(binDir, 'yylo.sh'), 0o755);
      await fs.writeFile(path.join(binDir, 'cli.mjs'), 'console.log(JSON.stringify(process.argv.slice(2)))\n');
      await fs.writeFile(path.join(scriptsDir, 'bootstrap.sh'), `#!/usr/bin/env bash\ntouch "${marker}"\nexit 91\n`);
      const result = await execa(path.join(binDir, 'yylo.sh'), args, { cwd: tempDir, reject: false });
      expect(result.exitCode).toBe(0);
      expect(JSON.parse(result.stdout)).toEqual(args);
      expect(await fs.pathExists(marker)).toBe(false);
    } finally {
      await fs.remove(tempDir);
    }
  });

  it.each([
    ['please', 'list', 'tasks'],
    ['debug', 'status', 'endpoint'],
    ['explain', 'sync', 'behavior'],
    ['future-command', 'status'],
  ])('preserves unknown-leading free-form prompt input before bootstrap: %s', async (...args) => {
    const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-free-prompt-wrapper-'));
    try {
      const binDir = path.join(tempDir, 'bin');
      await fs.ensureDir(binDir);
      // Complete fixture installation: the canonical yylo peer sits beside yy
      // and yy resolves to it, so an ambient global @yylo/cli installation
      // cannot turn this fixture into a refused mixed installation.
      await fs.copy(YYLO_SOURCE, path.join(binDir, 'yylo'));
      await fs.chmod(path.join(binDir, 'yylo'), 0o755);
      await fs.symlink('yylo', path.join(binDir, 'yy'));
      await fs.writeFile(
        path.join(binDir, 'cli.mjs'),
        `// YYLO_PREFLIGHT_ONLY\nif (process.env.YYLO_PREFLIGHT_ONLY !== '1') console.log(JSON.stringify(process.argv.slice(2)));\n`,
      );

      const result = await execa(path.join(binDir, 'yy'), args, {
        cwd: tempDir,
        reject: false,
        env: { ...process.env, PATH: `${binDir}${path.delimiter}${process.env.PATH ?? ''}` },
      });
      expect(result.exitCode).toBe(0);
      expect(JSON.parse(result.stdout)).toEqual(args);
    } finally {
      await fs.remove(tempDir);
    }
  });

  it('keeps pi on the normal product bootstrap path and cwd', async () => {
    const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-pi-wrapper-'));
    try {
      const binDir = path.join(tempDir, 'bin');
      const scriptsDir = path.join(tempDir, '.juno_task', 'scripts');
      await fs.ensureDir(binDir);
      await fs.ensureDir(scriptsDir);
      // Same complete fixture installation invariant as the free-form tests:
      // yy resolves to its canonical yylo peer inside the fixture bin.
      await fs.copy(YYLO_SOURCE, path.join(binDir, 'yylo'));
      await fs.chmod(path.join(binDir, 'yylo'), 0o755);
      await fs.symlink('yylo', path.join(binDir, 'yy'));
      await fs.writeFile(path.join(binDir, 'cli.mjs'), 'unused\n');
      await fs.writeFile(
        path.join(scriptsDir, 'bootstrap.sh'),
        '#!/usr/bin/env bash\nprintf "cwd=%s\\n" "$PWD"\nprintf "arg=%s\\n" "$@"\n',
      );
      const result = await execa(path.join(binDir, 'yy'), ['pi', '--cwd', tempDir, 'prompt'], {
        cwd: tempDir,
        reject: false,
        env: { ...process.env, PATH: `${binDir}${path.delimiter}${process.env.PATH ?? ''}` },
      });
      expect(result.exitCode).toBe(0);
      expect(result.stdout).toContain(`cwd=${await fs.realpath(tempDir)}`);
      expect(result.stdout).toContain('arg=pi');
      expect(result.stdout).toContain(`arg=${tempDir}`);
    } finally {
      await fs.remove(tempDir);
    }
  });

  it('routes yy task preflight from a registered product workspace to the pinned controller runtime', async () => {
    const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-routed-wrapper-'));
    try {
      const controller = path.join(tempDir, 'controller');
      const integration = path.join(tempDir, 'integration');
      const launcherBin = path.join(tempDir, 'launcher-bin');
      const packagedScripts = path.join(tempDir, 'templates', 'scripts');
      const untrustedResolverMarker = path.join(tempDir, 'untrusted-resolver-ran');
      await fs.ensureDir(path.join(controller, '.juno_task', 'scripts'));
      await fs.ensureDir(path.join(controller, 'nested', 'directory'));
      await fs.writeFile(path.join(controller, 'nested', 'directory', '.keep'), '');
      await fs.copy(
        path.join(PROJECT_ROOT, 'src/templates/scripts/controller_resolver.py'),
        path.join(controller, '.juno_task', 'scripts', 'controller_resolver.py'),
      );
      await execa('git', ['init', '-b', 'controller'], { cwd: controller });
      await execa('git', ['config', 'user.email', 'test@example.invalid'], { cwd: controller });
      await execa('git', ['config', 'user.name', 'Test'], { cwd: controller });
      await execa('git', ['add', '.'], { cwd: controller });
      await execa('git', ['commit', '-m', 'fixture'], { cwd: controller });
      await execa('git', ['worktree', 'add', '-b', 'product', integration], { cwd: controller });
      await execa('git', ['config', 'extensions.worktreeConfig', 'true'], { cwd: integration });
      await execa('git', ['config', 'juno.controller.path', controller], { cwd: integration });
      await execa('git', ['config', 'juno.controller.branch', 'refs/heads/controller'], { cwd: integration });
      await execa('git', ['config', '--worktree', 'juno.workspace.role', 'integration-owner'], { cwd: integration });
      await execa('git', ['config', '--worktree', 'juno.workspace.roleAuthority', 'protected-integration.v1'], { cwd: integration });
      const runtime = path.join(controller, 'controller-runtime.mjs');
      await fs.writeFile(runtime, `console.log(JSON.stringify({argv0:process.argv0,args:process.argv.slice(2),cwd:process.cwd(),env:{invocation:process.env.JUNO_CONTROL_INVOCATION_ROOT,role:process.env.JUNO_CONTROL_INVOCATION_ROLE,effective:process.env.JUNO_CONTROL_EFFECTIVE_ROOT,asserted:process.env.JUNO_WORKSPACE_ROLE,enforcement:process.env.JUNO_WORKSPACE_ENFORCEMENT}}))\n`);
      await execa('git', ['config', '--worktree', 'juno.controller.runtimeExecutable', runtime], { cwd: controller });
      await fs.ensureDir(launcherBin);
      await fs.ensureDir(packagedScripts);
      await fs.copy(
        path.join(PROJECT_ROOT, 'src/templates/scripts/controller_resolver.py'),
        path.join(packagedScripts, 'controller_resolver.py'),
      );
      await fs.copy(YYLO_SOURCE, path.join(launcherBin, 'yylo'));
      await fs.chmod(path.join(launcherBin, 'yylo'), 0o755);
      await fs.symlink('yylo', path.join(launcherBin, 'yy'));
      await fs.writeFile(path.join(launcherBin, 'cli.mjs'), 'process.exit(98)\n');
      await fs.writeFile(
        path.join(integration, '.juno_task', 'scripts', 'controller_resolver.py'),
        `#!/usr/bin/env python3\nfrom pathlib import Path\nPath(${JSON.stringify(untrustedResolverMarker)}).write_text('ran')\nraise SystemExit(97)\n`,
      );
      const before = await execa('git', ['status', '--porcelain=v1', '--untracked-files=all'], { cwd: integration });

      const result = await execa(path.join(launcherBin, 'yy'), ['task', 'preflight', 'T1'], {
        cwd: path.join(integration, 'nested', 'directory'),
        reject: false,
        env: { ...process.env, PATH: `${launcherBin}${path.delimiter}${process.env.PATH ?? ''}` },
      });
      expect(result.exitCode).toBe(0);
      expect(JSON.parse(result.stdout)).toEqual({
        argv0: 'yy', args: ['task', 'preflight', 'T1'], cwd: await fs.realpath(controller),
        env: { invocation: await fs.realpath(integration), role: 'integration-owner',
          effective: await fs.realpath(controller), asserted: 'controller', enforcement: 'strict' },
      });
      expect(await fs.pathExists(path.join(integration, '.venv_juno'))).toBe(false);
      expect(await fs.pathExists(untrustedResolverMarker)).toBe(false);
      const after = await execa('git', ['status', '--porcelain=v1', '--untracked-files=all'], { cwd: integration });
      expect(after.stdout).toBe(before.stdout);

      await execa('git', ['config', '--local', '--unset-all', 'juno.controller.path'], { cwd: integration });
      await execa('git', ['config', '--local', '--unset-all', 'juno.controller.branch'], { cwd: integration });
      const missingRegistration = await execa(path.join(launcherBin, 'yy'), ['merge', 'status'], {
        cwd: path.join(integration, 'nested', 'directory'), reject: false,
        env: { ...process.env, PATH: `${launcherBin}${path.delimiter}${process.env.PATH ?? ''}` },
      });
      expect(missingRegistration.exitCode).toBe(2);
      expect(missingRegistration.stdout).toBe('');
      expect(missingRegistration.stderr.trim()).toBe(
        'controller-resolver: linked product workspace requires exactly one non-empty controller path and branch registration',
      );
      expect(await fs.pathExists(path.join(integration, '.venv_juno'))).toBe(false);
      expect(await fs.pathExists(untrustedResolverMarker)).toBe(false);
      expect((await execa('git', ['status', '--porcelain=v1', '--untracked-files=all'], { cwd: integration })).stdout).toBe(before.stdout);
      await execa('git', ['config', '--local', 'juno.controller.path', controller], { cwd: integration });
      await execa('git', ['config', '--local', 'juno.controller.branch', 'refs/heads/controller'], { cwd: integration });

      for (const surface of ['ledger', 'kanban']) {
        const leadingOption = await execa(path.join(launcherBin, 'yy'), ['--quiet', surface, 'list'], {
          cwd: path.join(integration, 'nested', 'directory'), reject: false,
          env: { ...process.env, PATH: `${launcherBin}${path.delimiter}${process.env.PATH ?? ''}` },
        });
        expect(leadingOption.exitCode).toBe(0);
        expect(JSON.parse(leadingOption.stdout).args).toEqual(['--quiet', surface, 'list']);
        expect((await execa('git', ['status', '--porcelain=v1', '--untracked-files=all'], { cwd: integration })).stdout).toBe(before.stdout);
      }

      await execa('git', ['config', '--local', '--add', 'juno.controller.path', controller], { cwd: integration });
      const ambiguous = await execa(path.join(launcherBin, 'yy'), ['merge', 'status'], {
        cwd: path.join(integration, 'nested'), reject: false,
      env: { ...process.env, PATH: `${launcherBin}${path.delimiter}${process.env.PATH ?? ''}` },
      });
      expect(ambiguous.exitCode).toBe(2);
      expect(ambiguous.stdout).toBe('');
      expect(ambiguous.stderr).toContain('controller registration is ambiguous: juno.controller.path has multiple values');
      expect(await fs.pathExists(path.join(integration, '.venv_juno'))).toBe(false);
      await execa('git', ['config', '--local', '--unset-all', 'juno.controller.path'], { cwd: integration });
      await execa('git', ['config', '--local', 'juno.controller.path', controller], { cwd: integration });

      const localController = await execa(path.join(launcherBin, 'yy'), ['merge', 'status'], {
        cwd: controller, reject: false,
      env: { ...process.env, PATH: `${launcherBin}${path.delimiter}${process.env.PATH ?? ''}` },
      });
      expect(localController.exitCode).toBe(98);
      expect(localController.stdout).toBe('');

      await execa('git', ['config', '--worktree', 'juno.controller.runtimeExecutable', path.join(controller, 'missing-runtime.mjs')], { cwd: controller });
      const invalidRuntime = await execa(path.join(launcherBin, 'yy'), ['merge', 'status'], {
        cwd: path.join(integration, 'nested'), reject: false,
      env: { ...process.env, PATH: `${launcherBin}${path.delimiter}${process.env.PATH ?? ''}` },
      });
      expect(invalidRuntime.exitCode).toBe(2);
      expect(invalidRuntime.stderr).toContain('registered controller runtime is missing or stale');
      await execa('git', ['config', '--worktree', 'juno.controller.runtimeExecutable', runtime], { cwd: controller });

      const staleRuntimeMarker = path.join(tempDir, 'stale-runtime-ran');
      await fs.writeFile(
        path.join(launcherBin, 'cli.mjs'),
        `// YYLO_PREFLIGHT_ONLY\nif (process.argv.includes('--version')) console.log('2.1.2');\n`,
      );
      await fs.writeFile(
        runtime,
        `if (process.argv.includes('--version')) console.log('2.1.1'); else require('node:fs').writeFileSync(${JSON.stringify(staleRuntimeMarker)}, 'ran');\n`,
      );
      const staleRuntime = await execa(path.join(launcherBin, 'yy'), ['integration', 'sync'], {
        cwd: path.join(integration, 'nested'), reject: false,
      env: { ...process.env, PATH: `${launcherBin}${path.delimiter}${process.env.PATH ?? ''}` },
      });
      expect(staleRuntime.exitCode).toBe(2);
      expect(staleRuntime.stderr).toContain('selected controller runtime cannot be proven to support this explicit command');
      expect(staleRuntime.stderr).toContain(`launcher executable: ${path.join(launcherBin, 'cli.mjs')}`);
      expect(staleRuntime.stderr).toContain('launcher version: 2.1.2');
      expect(staleRuntime.stderr).toContain(`effective executable: ${runtime}`);
      expect(staleRuntime.stderr).toContain('effective version: 2.1.1');
      expect(await fs.pathExists(staleRuntimeMarker)).toBe(false);

      await execa('git', ['config', '--worktree', '--unset-all', 'juno.workspace.role'], { cwd: integration });
      const invalidRole = await execa(path.join(launcherBin, 'yy'), ['task', 'status', 'T1'], {
        cwd: path.join(integration, 'nested'), reject: false,
      env: { ...process.env, PATH: `${launcherBin}${path.delimiter}${process.env.PATH ?? ''}` },
      });
      expect(invalidRole.exitCode).toBe(2);
      expect(invalidRole.stderr).toContain('no persisted workspace role registration');
      expect(await fs.pathExists(path.join(integration, '.venv_juno'))).toBe(false);
    } finally {
      await fs.remove(tempDir);
    }
  }, 30_000);

  it.each(['start', 'status', 'preflight', 'finish'] as const)(
    'fails task %s closed in an exact task worktree before a stale registered runtime executes',
    async (operation) => {
      const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), `juno-stale-task-${operation}-`));
      try {
        const controller = path.join(tempDir, 'controller');
        const task = path.join(tempDir, 'task');
        const launcherBin = path.join(tempDir, 'launcher-bin');
        const packagedScripts = path.join(tempDir, 'templates', 'scripts');
        const staleRuntimeMarker = path.join(tempDir, 'stale-runtime-ran');
        await fs.ensureDir(path.join(controller, '.juno_task', 'scripts'));
        await fs.copy(
          path.join(PROJECT_ROOT, 'src/templates/scripts/controller_resolver.py'),
          path.join(controller, '.juno_task/scripts/controller_resolver.py'),
        );
        await execa('git', ['init', '-b', 'controller'], { cwd: controller });
        await execa('git', ['config', 'user.email', 'test@example.invalid'], { cwd: controller });
        await execa('git', ['config', 'user.name', 'Test'], { cwd: controller });
        await fs.writeFile(path.join(controller, 'fixture'), 'fixture\n');
        await execa('git', ['add', '.'], { cwd: controller });
        await execa('git', ['commit', '-m', 'fixture'], { cwd: controller });
        await execa('git', ['worktree', 'add', '-b', 'juno/task-T1', task], { cwd: controller });
        await execa('git', ['config', 'extensions.worktreeConfig', 'true'], { cwd: task });
        await execa('git', ['config', 'juno.controller.path', controller], { cwd: task });
        await execa('git', ['config', 'juno.controller.branch', 'refs/heads/controller'], { cwd: task });
        await execa('git', ['config', '--worktree', 'juno.workspace.role', 'task'], { cwd: task });
        await execa('git', ['config', '--worktree', 'juno.workspace.taskId', 'T1'], { cwd: task });
        for (const key of ['manifestIdentity', 'createReceiptSha256', 'expectedPathsSha256']) {
          await execa('git', ['config', '--worktree', `juno.workspace.${key}`, 'a'.repeat(64)], { cwd: task });
        }

        const runtime = path.join(controller, 'controller-runtime.mjs');
        await fs.writeFile(runtime, [
          "import { writeFileSync } from 'node:fs';",
          `if (process.argv.includes('--version')) console.log('2.1.1');`,
          `else writeFileSync(${JSON.stringify(staleRuntimeMarker)}, 'ran');`,
        ].join('\n'));
        await execa('git', ['config', '--worktree', 'juno.controller.runtimeExecutable', runtime], {
          cwd: controller,
        });
        await fs.ensureDir(launcherBin);
        await fs.ensureDir(packagedScripts);
        await fs.copy(
          path.join(PROJECT_ROOT, 'src/templates/scripts/controller_resolver.py'),
          path.join(packagedScripts, 'controller_resolver.py'),
        );
        const launcher = path.join(launcherBin, 'yy');
        const launcherCli = path.join(launcherBin, 'cli.mjs');
        // Complete fixture installation: yy resolves to its canonical yylo peer.
        await fs.copy(YYLO_SOURCE, path.join(launcherBin, 'yylo'));
        await fs.chmod(path.join(launcherBin, 'yylo'), 0o755);
        await fs.symlink('yylo', launcher);
        await fs.writeFile(
          launcherCli,
          `// YYLO_PREFLIGHT_ONLY\nif (process.argv.includes('--version')) console.log('2.1.2');\n`,
        );

        const result = await execa(launcher, ['task', operation, 'T1'], {
          cwd: task, reject: false,
          env: { ...process.env, PATH: `${launcherBin}${path.delimiter}${process.env.PATH ?? ''}` },
        });
        expect(result.exitCode).toBe(2);
        expect(result.stderr).toContain('selected controller runtime cannot be proven');
        expect(result.stderr).toContain(`launcher executable: ${launcherCli}`);
        expect(result.stderr).toContain('launcher version: 2.1.2');
        expect(result.stderr).toContain(`effective executable: ${runtime}`);
        expect(result.stderr).toContain('effective version: 2.1.1');
        expect(await fs.pathExists(staleRuntimeMarker)).toBe(false);
      } finally {
        await fs.remove(tempDir);
      }
    },
    30_000,
  );

  it.each([
    { args: ['kanban', 'list'], operation: 'kanban' },
    { args: ['task', 'status', 'T1'], operation: 'kanban' },
    { args: ['task', 'preflight', 'T1'], operation: 'kanban' },
    { args: ['task', 'doctor'], operation: 'kanban' },
    { args: ['task', 'recovery-plan', 'T1'], operation: null },
    { args: ['evidence', 'status', 'T1'], operation: 'kanban' },
    { args: ['merge', 'status'], operation: 'kanban' },
    { args: ['merge', 'plan', 'T1'], operation: 'kanban' },
    { args: ['task', 'start', 'T1'], operation: 'orchestration' },
    { args: ['task', 'run', 'T1'], operation: 'orchestration' },
    { args: ['task', 'recover-predispatch', 'T1', '--run-id', 'run-12345678'], operation: 'orchestration' },
    { args: ['task', 'recover-wall-budget', 'T1', '--run-id', 'run-12345678', '--attempt', '1', '--predispatch-receipt-sha256', 'a'.repeat(64), '--original-deadline-unix-ns', '1787895956343575000'], operation: 'orchestration' },
    { args: ['task', 'hydrate', 'T1'], operation: 'orchestration' },
    { args: ['task', 'finish', 'T1'], operation: 'orchestration' },
    { args: ['task', 'checkpoint', 'T1'], operation: 'orchestration' },
    { args: ['task', 'sync', 'T1'], operation: 'orchestration' },
    { args: ['evidence', 'run', 'T1'], operation: 'orchestration' },
    { args: ['evidence', 'await', 'T1'], operation: 'orchestration' },
    { args: ['merge', 'next'], operation: 'orchestration' },
    { args: ['merge', 'drive'], operation: 'orchestration' },
    { args: ['merge', 'drive', '--through', 'T1'], operation: 'orchestration' },
    { args: ['merge', 'withdraw', 'T1'], operation: 'orchestration' },
    { args: ['merge', 'next'], operation: 'orchestration' },
    { args: ['merge', 'resolve', 'T1'], operation: 'orchestration' },
    { args: ['merge', 'review', 'T1'], operation: 'orchestration' },
    { args: ['merge', 'reopen', 'T1'], operation: 'orchestration' },
    { args: ['merge', 'reconcile', 'plan', 'T1'], operation: 'orchestration' },
    { args: ['merge', 'refresh', 'plan', 'T1'], operation: 'orchestration' },
    { args: ['integration', 'status'], operation: 'kanban' },
    { args: ['integration', 'sync'], operation: 'orchestration' },
    { args: ['integration', 'runtime-doctor'], operation: 'orchestration' },
    { args: ['integration', 'runtime-refresh', '--previous-sha', 'a'.repeat(40)], operation: 'orchestration' },
    { args: ['task', 'runtime-bootstrap', '--dry-run'], operation: 'orchestration' },
    { args: ['integration', 'register', '/owner'], operation: 'orchestration' },
    { args: ['integration', 'repair', '--dry-run'], operation: 'orchestration' },
    { args: ['integration', 'push', '--dry-run'], operation: 'orchestration' },
    { args: ['task', 'mystery'], operation: null },
    { args: ['integration', 'mystery'], operation: null },
    { args: ['merge', 'mystery'], operation: null },
    { args: ['evidence', 'mystery'], operation: null },
  ])('authorizes $operation before dispatching controller runtime bytes', async ({ args, operation }) => {
    const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-wrapper-operation-gate-'));
    try {
      const controller = path.join(tempDir, 'controller');
      const integration = path.join(tempDir, 'integration');
      const launcherBin = path.join(tempDir, 'launcher-bin');
      const packagedScripts = path.join(tempDir, 'templates', 'scripts');
      const operationMarker = path.join(tempDir, 'resolver-operation');
      const runtimeMarker = path.join(tempDir, 'runtime-ran');
      await fs.ensureDir(controller);
      await fs.ensureDir(path.join(integration, '.juno_task'));
      await fs.ensureDir(launcherBin);
      await fs.ensureDir(packagedScripts);
      await execa('git', ['init', '-b', 'controller'], { cwd: controller });
      const runtime = path.join(controller, 'controller-runtime.mjs');
      await fs.writeFile(runtime, `require('node:fs').writeFileSync(${JSON.stringify(runtimeMarker)}, 'ran')\n`);
      await execa('git', ['config', 'extensions.worktreeConfig', 'true'], { cwd: controller });
      await execa('git', ['config', '--worktree', 'juno.controller.runtimeExecutable', runtime], { cwd: controller });
      await fs.writeFile(
        path.join(packagedScripts, 'controller_resolver.py'),
        [
          'import json, pathlib, sys',
          `marker = pathlib.Path(${JSON.stringify(operationMarker)})`,
          "operation = sys.argv[sys.argv.index('--operation') + 1]",
          'marker.write_text(operation)',
          "if operation != 'diagnostic':",
          "    print('operation-specific controller policy refused dirty state', file=sys.stderr)",
          '    raise SystemExit(77)',
          `print(json.dumps({'path': ${JSON.stringify(controller)}, 'current_root': ${JSON.stringify(integration)}, 'role': 'integration-owner', 'expected_branch': 'refs/heads/controller', 'source': 'registration'}))`,
        ].join('\n'),
      );
      // Complete fixture installation: yy resolves to its canonical yylo peer
      // inside the launcher bin so the wrapper accepts the fixture regardless
      // of an ambient global @yylo/cli installation.
      await fs.copy(YYLO_SOURCE, path.join(launcherBin, 'yylo'));
      await fs.chmod(path.join(launcherBin, 'yylo'), 0o755);
      await fs.symlink('yylo', path.join(launcherBin, 'yy'));
      await fs.writeFile(path.join(launcherBin, 'cli.mjs'), 'process.exit(98)\n');

      const result = await execa(path.join(launcherBin, 'yy'), args, {
        cwd: integration, reject: false,
        env: { ...process.env, PATH: `${launcherBin}${path.delimiter}${process.env.PATH ?? ''}` },
      });

      expect(result.exitCode).toBe(2);
      if (operation === null) {
        expect(await fs.pathExists(operationMarker)).toBe(false);
        expect(result.stderr).toContain(
          `control-plane routing refused unknown ${args[0]} subcommand '${args[1]}'`,
        );
      } else {
        expect(await fs.readFile(operationMarker, 'utf8')).toBe(operation);
        expect(result.stderr).toContain('operation-specific controller policy refused dirty state');
      }
      expect(await fs.pathExists(runtimeMarker)).toBe(false);
    } finally {
      await fs.remove(tempDir);
    }
  });

  it('forwards the effective task policy to the pinned controller runtime', async () => {
    const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-wrapper-forwarded-policy-'));
    try {
      const controller = path.join(tempDir, 'controller');
      const integration = path.join(tempDir, 'integration');
      const launcherBin = path.join(tempDir, 'launcher-bin');
      const packagedScripts = path.join(tempDir, 'templates', 'scripts');
      const runtimeMarker = path.join(tempDir, 'runtime-policy');
      await fs.ensureDir(controller);
      await fs.ensureDir(integration);
      await fs.ensureDir(launcherBin);
      await fs.ensureDir(packagedScripts);
      await execa('git', ['init', '-b', 'controller'], { cwd: controller });
      const runtime = path.join(controller, 'controller-runtime.mjs');
      await fs.writeFile(
        runtime,
        `import { writeFileSync } from 'node:fs'; writeFileSync(${JSON.stringify(runtimeMarker)}, process.env.JUNO_CONTROL_OPERATION || '')\n`,
      );
      await execa('git', ['config', 'extensions.worktreeConfig', 'true'], { cwd: controller });
      await execa('git', ['config', '--worktree', 'juno.controller.runtimeExecutable', runtime], {
        cwd: controller,
      });
      await fs.writeFile(
        path.join(packagedScripts, 'controller_resolver.py'),
        [
          'import json',
          `print(json.dumps({'path': ${JSON.stringify(controller)}, 'current_root': ${JSON.stringify(integration)}, 'role': 'integration-owner', 'expected_branch': 'refs/heads/controller', 'source': 'registration'}))`,
        ].join('\n'),
      );
      // Complete fixture installation: yy resolves to its canonical yylo peer
      // inside the launcher bin so the wrapper accepts the fixture regardless
      // of an ambient global @yylo/cli installation.
      await fs.copy(YYLO_SOURCE, path.join(launcherBin, 'yylo'));
      await fs.chmod(path.join(launcherBin, 'yylo'), 0o755);
      await fs.symlink('yylo', path.join(launcherBin, 'yy'));
      await fs.writeFile(path.join(launcherBin, 'cli.mjs'), 'process.exit(98)\n');

      const result = await execa(path.join(launcherBin, 'yy'), ['task', 'finish', 'T1'], {
        cwd: integration,
        reject: false,
      env: { ...process.env, PATH: `${launcherBin}${path.delimiter}${process.env.PATH ?? ''}` },
      });
      expect(result.exitCode).toBe(0);
      expect(await fs.readFile(runtimeMarker, 'utf8')).toBe('orchestration');
    } finally {
      await fs.remove(tempDir);
    }
  });

  it('keeps fake-node wrapper assertions compatible with the runtime version probe', async () => {
    const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-compatible-node-wrapper-'));
    try {
      const binDir = path.join(tempDir, 'bin');
      const fakeBin = path.join(tempDir, 'fake-bin');
      await fs.ensureDir(binDir);
      await fs.ensureDir(fakeBin);
      await fs.copy(YYLO_SOURCE, path.join(binDir, 'yylo.sh'));
      await fs.chmod(path.join(binDir, 'yylo.sh'), 0o755);
      await fs.writeFile(path.join(binDir, 'cli.mjs'), 'unused\n');
      await fs.writeFile(
        path.join(fakeBin, 'node'),
        '#!/usr/bin/env bash\nif [ "$1" = "-p" ] && [ "$2" = "process.versions.node" ]; then echo 22.22.0; exit 0; fi\n[ "$YYLO_NODE_EXECUTABLE" = "$0" ] || exit 88\n[ "${PATH%%:*}" = "$(dirname "$0")" ] || exit 89\nprintf "%s\\n" "$@"\n',
        { mode: 0o755 },
      );
      const result = await execa(path.join(binDir, 'yylo.sh'), ['--version'], {
        cwd: tempDir, env: { PATH: `${fakeBin}:${process.env.PATH}` }, reject: false,
      });
      expect(result.exitCode).toBe(0);
      expect(result.stdout.split('\n')).toEqual([path.join(binDir, 'cli.mjs'), '--version']);
    } finally {
      await fs.remove(tempDir);
    }
  });

  it('preserves the caller environment when benchmark delegation has competing binaries', async () => {
    const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-benchmark-wrapper-env-'));
    try {
      const binDir = path.join(tempDir, 'bin');
      const preferredBin = path.join(tempDir, 'preferred-bin');
      const runtimeBin = path.join(tempDir, 'runtime-bin');
      const scriptsDir = path.join(tempDir, '.juno_task', 'scripts');
      const record = path.join(tempDir, 'record.json');
      await Promise.all([binDir, preferredBin, runtimeBin, scriptsDir].map((directory) => fs.ensureDir(directory)));
      // Complete fixture installation: yy resolves to its canonical yylo peer.
      await fs.copy(YYLO_SOURCE, path.join(binDir, 'yylo'));
      await fs.chmod(path.join(binDir, 'yylo'), 0o755);
      await fs.symlink('yylo', path.join(binDir, 'yy'));
      await fs.writeFile(
        path.join(binDir, 'cli.mjs'),
        [
          "import { spawnSync } from 'node:child_process';",
          "import fs from 'node:fs';",
          "const child = spawnSync('juno-benchmark', ['probe'], { encoding: 'utf8' });",
          `fs.writeFileSync(${JSON.stringify(record)}, JSON.stringify({`,
          "  delegate: child.stdout.trim(), marker: process.env.DELEGATE_MARKER,",
          "  path: process.env.PATH, nodeExecutablePresent: Object.prototype.hasOwnProperty.call(process.env, 'YYLO_NODE_EXECUTABLE')",
          '}));',
          'process.exit(child.status ?? 1);',
        ].join('\n'),
      );
      await fs.writeFile(
        path.join(runtimeBin, 'node'),
        `#!/usr/bin/env bash\nif [ "$1" = "-p" ]; then echo 22.22.3; exit 0; fi\nexec ${JSON.stringify(process.execPath)} "$@"\n`,
        { mode: 0o755 },
      );
      await fs.writeFile(path.join(preferredBin, 'juno-benchmark'), '#!/bin/sh\nprintf preferred', { mode: 0o755 });
      await fs.writeFile(path.join(runtimeBin, 'juno-benchmark'), '#!/bin/sh\nprintf competitor', { mode: 0o755 });
      await fs.writeFile(path.join(scriptsDir, 'bootstrap.sh'), '#!/bin/sh\nexit 91', { mode: 0o755 });
      const callerPath = `${binDir}${path.delimiter}${preferredBin}${path.delimiter}${runtimeBin}${path.delimiter}${process.env.PATH ?? ''}`;

      // Hermetic caller environment: the transparent delegate must observe the
      // caller's environment exactly, so remove both the canonical and the
      // bounded-migration legacy Node override. The wrapper canonicalizes
      // JUNO_CODE_* names before capturing the caller contract, so an ambient
      // legacy name would otherwise leak the wrapper's owned Node selection
      // into the delegate and couple this test to how the suite was launched.
      const callerEnv = { ...process.env, PATH: callerPath, DELEGATE_MARKER: 'exact caller value' };
      delete callerEnv.YYLO_NODE_EXECUTABLE;
      delete callerEnv.JUNO_CODE_NODE_EXECUTABLE;
      const result = await execa(path.join(binDir, 'yy'), ['benchmark', 'probe'], {
        cwd: tempDir,
        env: callerEnv,
        extendEnv: false,
        reject: false,
      });

      expect(result.exitCode).toBe(0);
      // A caller that pins no Node override must not observe the wrapper's
      // internal Node selection in the delegated environment.
      expect(await fs.readJson(record)).toEqual({
        delegate: 'preferred',
        marker: 'exact caller value',
        path: callerPath,
        nodeExecutablePresent: false,
      });
    } finally {
      await fs.remove(tempDir);
    }
  });

  it('refuses modern distribution code under an unsupported ambient Node', async () => {
    const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-node-wrapper-'));
    try {
      const binDir = path.join(tempDir, 'bin');
      const fakeBin = path.join(tempDir, 'fake-bin');
      const marker = path.join(tempDir, 'cli-ran');
      await fs.ensureDir(binDir);
      await fs.ensureDir(fakeBin);
      await fs.copy(YYLO_SOURCE, path.join(binDir, 'yylo.sh'));
      await fs.chmod(path.join(binDir, 'yylo.sh'), 0o755);
      await fs.writeFile(path.join(binDir, 'cli.mjs'), `require('fs').writeFileSync(${JSON.stringify(marker)}, 'ran')\n`);
      await fs.writeFile(path.join(fakeBin, 'node'), '#!/usr/bin/env bash\nif [ "$1" = "-p" ]; then echo 18.19.0; else exit 97; fi\n', { mode: 0o755 });
      const result = await execa(path.join(binDir, 'yylo.sh'), ['--version'], {
        cwd: tempDir,
        env: { PATH: `${fakeBin}:${process.env.PATH}`, NVM_DIR: path.join(tempDir, 'missing-nvm') },
        reject: false,
      });
      expect(result.exitCode).toBe(69);
      expect(result.stderr).toContain('unsupported Node 18.19.0');
      expect(await fs.pathExists(marker)).toBe(false);
    } finally {
      await fs.remove(tempDir);
    }
  });

  it('selects the highest compatible installed NVM Node when ambient Node is stale', async () => {
    const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-stale-nvm-wrapper-'));
    try {
      const binDir = path.join(tempDir, 'bin');
      const fakeBin = path.join(tempDir, 'fake-bin');
      const nvmDir = path.join(tempDir, 'nvm');
      const selectedMarker = path.join(tempDir, 'selected-node');
      await fs.ensureDir(binDir);
      await fs.ensureDir(fakeBin);
      await fs.copy(YYLO_SOURCE, path.join(binDir, 'yylo.sh'));
      await fs.chmod(path.join(binDir, 'yylo.sh'), 0o755);
      await fs.writeFile(path.join(binDir, 'cli.mjs'), "console.log('selected-compatible-node')\n");
      await fs.writeFile(
        path.join(fakeBin, 'node'),
        '#!/usr/bin/env bash\nif [ "$1" = "-p" ]; then echo 18.19.0; else exit 97; fi\n',
        { mode: 0o755 },
      );
      for (const version of ['20.10.0', '22.22.3']) {
        const candidate = path.join(nvmDir, 'versions', 'node', `v${version}`, 'bin', 'node');
        await fs.ensureDir(path.dirname(candidate));
        await fs.writeFile(
          candidate,
          `#!/usr/bin/env bash\nif [ "$1" = "-p" ]; then echo ${version}; exit 0; fi\nprintf '%s' ${JSON.stringify(version)} > ${JSON.stringify(selectedMarker)}\nexec ${JSON.stringify(process.execPath)} "$@"\n`,
          { mode: 0o755 },
        );
      }

      const result = await execa(path.join(binDir, 'yylo.sh'), ['--version'], {
        cwd: tempDir,
        env: { PATH: `${fakeBin}:${process.env.PATH}`, NVM_DIR: nvmDir },
        reject: false,
      });

      expect(result.exitCode).toBe(0);
      expect(result.stdout).toBe('selected-compatible-node');
      expect(await fs.readFile(selectedMarker, 'utf8')).toBe('22.22.3');
    } finally {
      await fs.remove(tempDir);
    }
  });

  it.each([
    ['yy', 'yy'],
    ['yylo', 'yylo'],
  ])('preserves the %s launch identity through the common wrapper', async (fileName, launchSurface) => {
    const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-launch-surface-'));
    try {
      const binDir = path.join(tempDir, 'bin');
      await fs.ensureDir(binDir);
      // Complete fixture installation: the canonical yylo peer sits beside yy.
      await fs.copy(YYLO_SOURCE, path.join(binDir, 'yylo'));
      await fs.chmod(path.join(binDir, 'yylo'), 0o755);
      const wrapper = path.join(binDir, fileName);
      if (fileName === 'yy') {
        await fs.symlink('yylo', wrapper);
      }
      await fs.writeFile(path.join(binDir, 'cli.mjs'), "console.log(process.argv0)\n");
      const result = await execa(wrapper, ['--version'], {
        cwd: tempDir,
        reject: false,
        env: {
          YYLO_LAUNCH_SURFACE: 'ypl',
          PATH: `${binDir}${path.delimiter}${process.env.PATH ?? ''}`,
        },
      });
      expect(result.exitCode).toBe(0);
      expect(result.stdout).toBe(launchSurface);
    } finally {
      await fs.remove(tempDir);
    }
  });

  it('executes the yylo wrapper with pi --live before forwarded args', async () => {
    const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-ypl-wrapper-'));
    try {
      const binDir = path.join(tempDir, 'bin');
      await fs.ensureDir(binDir);
      await fs.copy(YPL_SOURCE, path.join(binDir, 'ypl.sh'));
      await fs.copy(YYLO_SOURCE, path.join(binDir, 'yylo.sh'));
      await fs.chmod(path.join(binDir, 'ypl.sh'), 0o755);
      await fs.chmod(path.join(binDir, 'yylo.sh'), 0o755);
      await fs.writeFile(
        path.join(binDir, 'cli.mjs'),
        "console.log(JSON.stringify({ args: process.argv.slice(2), launchSurface: process.argv0 }))\n",
        'utf8',
      );

      const result = await execa(path.join(binDir, 'ypl.sh'), ['hello world', '--model', 'sonnet'], {
        cwd: tempDir,
        reject: false,
        env: { YYLO_LAUNCH_SURFACE: 'yy' },
      });

      expect(result.exitCode).toBe(0);
      expect(JSON.parse(result.stdout)).toEqual({
        args: ['pi', '--live', 'hello world', '--model', 'sonnet'],
        launchSurface: 'ypl',
      });
    } finally {
      await fs.remove(tempDir);
    }
  });
});
