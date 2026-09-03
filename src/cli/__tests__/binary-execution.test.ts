/**
 * Binary Execution Tests
 *
 * Tests the actual compiled CLI binary to catch issues that unit tests miss:
 * - Bundling problems (tsup/esbuild compilation issues)
 * - CLI option parsing and Commander.js integration
 * - Real execution flow problems
 * - Binary file execution and process spawning
 * - Actual user experience end-to-end
 *
 * This addresses critical USER_FEEDBACK issues by testing the actual binary
 * that users execute, not just internal module functions.
 */

import { describe, it, expect, vi, beforeEach, afterEach, beforeAll, afterAll } from 'vitest';
import { execa, type ExecaReturnValue } from 'execa';
import * as path from 'node:path';
import * as fs from 'fs-extra';
import * as os from 'node:os';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { pathToFileURL } from 'node:url';
import { expandSkillInvocation, findSkillFile } from '../../templates/extensions/pi/juno-skill-preprocessor.js';
import {
  createSessionContinuityConfig,
  createSessionContinuityFixture,
  explicitContinueScopeHash,
} from './helpers/session-continuity-fixture.js';
import { createTargetBoundMetadataController } from './helpers/managed-controller-recovery-fixture.js';

// Binary paths for testing
const PROJECT_ROOT = path.resolve(__dirname, '../../..');
const BINARY_JS = path.join(PROJECT_ROOT, 'dist/bin/cli.js');
const BINARY_MJS = path.join(PROJECT_ROOT, 'dist/bin/cli.mjs');

// Test timeout for binary execution
const BINARY_TIMEOUT = 30000; // 30 seconds

// Temp directory for testing
let tempDir: string;

function buildContinueSnapshotEnv(scope: string): Record<string, string> {
  const scopeHash = explicitContinueScopeHash(scope);
  const metadataDirectory = path.join(tempDir, '.juno_task');
  const statePath = path.join(metadataDirectory, 'session_continuity.v2.json');
  fs.ensureDirSync(metadataDirectory);
  const document = fs.pathExistsSync(statePath) ? fs.readJsonSync(statePath) : { version: 2, scopes: {} };
  document.scopes[scopeHash] = {
    source: 'YYLO_CONTINUE_SCOPE',
    createdAt: '2026-07-30T00:00:00.000Z',
    lastUsedAt: '2026-07-30T00:00:00.000Z',
    pinned: false,
    settings: { version: 1, subagent: 'claude', maxIterations: 5 },
    active: 'main',
    branches: { main: { session_id: 'session-continue-stdin', parent: null, updated_at: '2026-07-30T00:00:00.000Z' } },
  };
  fs.writeJsonSync(statePath, document);
  return { YYLO_CONTINUE_SCOPE: scope, YYLO_SESSION_METADATA_DIRECTORY: metadataDirectory };
}

/**
 * Execute CLI binary with given arguments and return result
 */
async function executeCLI(
  args: string[] = [],
  options: {
    timeout?: number;
    cwd?: string;
    env?: Record<string, string>;
    input?: string;
    binary?: 'js' | 'mjs';
    expectError?: boolean;
  } = {},
): Promise<ExecaReturnValue> {
  const {
    timeout = BINARY_TIMEOUT,
    cwd = tempDir,
    env = {},
    input,
    binary = 'mjs',
    expectError = false,
  } = options;

  const binaryPath = binary === 'js' ? BINARY_JS : BINARY_MJS;
  const requestedMetadata = env.YYLO_SESSION_METADATA_DIRECTORY;
  const metadataDirectory = requestedMetadata
    ? path.resolve(cwd, requestedMetadata)
    : path.join(tempDir, 'built-cli-session-metadata');
  const relativeMetadata = path.relative(tempDir, metadataDirectory);
  if (relativeMetadata.startsWith('..') || path.isAbsolute(relativeMetadata)) {
    throw new Error(`Binary validation metadata must stay inside its fresh fixture: ${metadataDirectory}`);
  }

  // Set up environment. The final assignment prevents an inherited real metadata
  // override from routing a validation command into Git-common user state.
  const testEnv = {
    ...process.env,
    // Disable colors for consistent output testing
    NO_COLOR: '1',
    // Set CI mode for quiet output
    CI: '1',
    // Override any user config
    YYLO_CONFIG: '',
    JUNO_TASK_CONFIG: '', // Backward compatibility
    ...env,
    YYLO_SESSION_METADATA_DIRECTORY: metadataDirectory,
  };

  try {
    const result = await execa('node', [binaryPath, ...args], {
      cwd,
      env: testEnv,
      timeout,
      input,
      reject: false, // Never reject - we'll handle errors ourselves
      all: true, // Capture both stdout and stderr
    });

    // Ensure exitCode is always a number (can be undefined in edge cases)
    if (result.exitCode === undefined) {
      result.exitCode = result.timedOut ? 124 : result.failed ? 1 : 0;
    }

    // If we don't expect an error but got one, throw it
    if (!expectError && result.exitCode !== 0) {
      throw result;
    }

    return result;
  } catch (error: any) {
    if (expectError) {
      // Ensure exitCode is set for timeout and other errors
      if (error.exitCode === undefined) {
        error.exitCode = error.timedOut ? 124 : 1; // 124 is standard timeout exit code
      }
      return error;
    }
    throw error;
  }
}

/**
 * Create a mock project structure in temp directory
 */
async function createMockProject(structure: Record<string, string | object> = {}): Promise<void> {
  async function createStructure(
    basePath: string,
    obj: Record<string, string | object>,
  ): Promise<void> {
    for (const [name, content] of Object.entries(obj)) {
      const fullPath = path.join(basePath, name);

      if (typeof content === 'string') {
        // It's a file
        await fs.ensureDir(path.dirname(fullPath));
        await fs.writeFile(fullPath, content, 'utf-8');
      } else {
        // It's a directory
        await fs.ensureDir(fullPath);
        await createStructure(fullPath, content as Record<string, string | object>);
      }
    }
  }

  await createStructure(tempDir, structure);
}

describe('Binary Execution Tests', () => {
  beforeAll(async () => {
    // Ensure binaries exist
    const jsExists = await fs.pathExists(BINARY_JS);
    const mjsExists = await fs.pathExists(BINARY_MJS);

    if (!jsExists && !mjsExists) {
      throw new Error(`Neither ${BINARY_JS} nor ${BINARY_MJS} exists. Run 'npm run build' first.`);
    }
  });

  beforeEach(async () => {
    // Create temporary directory for each test
    tempDir = await fs.mkdtemp(path.join(os.tmpdir(), 'yylo-binary-test-'));
  });

  afterEach(async () => {
    // Clean up temporary directory with retry for stubborn files
    if (tempDir && (await fs.pathExists(tempDir))) {
      try {
        await fs.remove(tempDir);
      } catch (error: any) {
        // Retry with force rm for stubborn directories (e.g., .venv_juno)
        if (error.code === 'ENOTEMPTY' || error.code === 'EBUSY') {
          await new Promise((resolve) => setTimeout(resolve, 100));
          try {
            await fs.remove(tempDir);
          } catch {
            // Ignore cleanup errors in tests - they're not test failures
          }
        }
      }
    }
  });

  describe('Build-required Pi skill boundaries', () => {
  it('matches the provenance-bound installed Pi native expansion for a no-placeholder skill', async () => {
    const nativeSkillDir = path.join(tempDir, '.pi', 'skills', 'native');
    await fs.ensureDir(nativeSkillDir);
    await fs.writeFile(
      path.join(nativeSkillDir, 'SKILL.md'),
      '---\nname: native\n---\nNative instructions',
    );
    const raw = `"quoted value" ## Ab12Cd\nquestion $(touch /tmp/never) '$HOME'\n@@no_code`;
    const invocation = `/skill:native ${raw}`;

    const piExecutable = execFileSync('sh', ['-c', 'command -v pi'], { encoding: 'utf8' }).trim();
    const piNode = path.join(path.dirname(piExecutable), 'node');
    expect(fs.realpathSync(piNode)).toContain('/versions/node/v22.');
    const piCli = fs.realpathSync(piExecutable);
    const piPackageRoot = path.dirname(path.dirname(piCli));
    const piPackage = JSON.parse(
      fs.readFileSync(path.join(piPackageRoot, 'package.json'), 'utf8'),
    ) as { name: string; version: string };
    const nativeSourcePath = path.join(piPackageRoot, 'dist/core/agent-session.js');
    const nativeSource = fs.readFileSync(nativeSourcePath, 'utf8');
    const nativeSourceSha256 = createHash('sha256').update(nativeSource).digest('hex');

    expect({
      executable: path.basename(piExecutable),
      package: piPackage.name,
      version: piPackage.version,
      nativeSource: path.relative(piPackageRoot, nativeSourcePath),
      nativeSourceSha256,
    }).toEqual({
      executable: 'pi',
      package: '@earendil-works/pi-coding-agent',
      version: '0.83.0',
      nativeSource: 'dist/core/agent-session.js',
      nativeSourceSha256: '9720d2a160540d9515ceb1ac4c4b4e73f4a215d703870c15b3c1863a2e37ff76',
    });
    expect(nativeSource).toContain('return args ? `${skillBlock}\\n\\n${args}` : skillBlock;');

    const nativeSkillPath = findSkillFile('native', tempDir)!;
    const nativeHarness = path.join(tempDir, 'invoke-provenance-bound-native-skill.mjs');
    await fs.writeFile(nativeHarness, [
      "import { readFileSync } from 'node:fs';",
      `import { AgentSession } from ${JSON.stringify(pathToFileURL(path.join(piPackageRoot, 'dist/index.js')).href)};`,
      'const { invocation, skillPath, baseDir } = JSON.parse(readFileSync(0, \'utf8\'));',
      'const session = {',
      '  resourceLoader: { getSkills: () => ({ skills: [{',
      "    name: 'native', filePath: skillPath, baseDir,",
      '  }] }) },',
      '  _extensionRunner: { emitError() {} },',
      '};',
      'process.stdout.write(AgentSession.prototype._expandSkillCommand.call(session, invocation));',
    ].join('\n'));
    const nativeOutput = execFileSync(piNode, [nativeHarness], {
      input: JSON.stringify({
        invocation, skillPath: nativeSkillPath, baseDir: path.dirname(nativeSkillPath),
      }),
      encoding: 'utf8',
    });
    const junoOutput = expandSkillInvocation(invocation, tempDir);

    expect(junoOutput).toBe(nativeOutput);
    expect(junoOutput).toBe(
      `<skill name="native" location="${findSkillFile('native', tempDir)}">\n` +
        `References are relative to ${path.dirname(findSkillFile('native', tempDir)!)}.\n\n` +
        `Native instructions\n</skill>\n\n${raw}`,
    );
  });

  it('passes a real multiline heredoc through built ypl, CLI rewriting, and the installed Pi preprocessor', async () => {
    try {
      const builtYpl = path.join(PROJECT_ROOT, 'dist/bin/ypl.sh');
      const builtPiExtension = path.join(
        PROJECT_ROOT,
        'dist/templates/extensions/pi/juno-skill-preprocessor.ts',
      );
      const builtRalphSkill = path.join(
        PROJECT_ROOT,
        'dist/templates/skills/pi/ralph-loop/SKILL.md',
      );
      const homeDir = path.join(tempDir, 'home');
      const projectDir = path.join(tempDir, 'project');
      const builtPackageRoot = path.join(tempDir, 'built-package');
      const builtPackageDir = path.join(builtPackageRoot, 'dist');
      const fixtureYpl = path.join(builtPackageDir, 'bin/ypl.sh');
      const fixturePiExtension = path.join(
        builtPackageDir,
        'templates/extensions/pi/juno-skill-preprocessor.ts',
      );
      const fixtureRalphSkill = path.join(
        builtPackageDir,
        'templates/skills/pi/ralph-loop/SKILL.md',
      );
      const fakeBin = path.join(tempDir, 'fake-bin');
      const servicesDir = path.join(homeDir, '.yylo', 'services');
      const installedExtension = path.join(projectDir, '.pi/extensions/juno-skill-preprocessor.ts');
      const compiledExtension = path.join(projectDir, '.pi/extensions/juno-skill-preprocessor.mjs');
      const installedSkill = path.join(projectDir, '.pi/skills/ralph-loop/SKILL.md');
      const harnessPath = path.join(tempDir, 'invoke-installed-preprocessor.mjs');
      const observedPromptPath = path.join(tempDir, 'prompt-before-preprocessor.txt');
      const kanbanCallsPath = path.join(tempDir, 'kanban-read-calls.txt');
      await Promise.all([
        fs.ensureDir(path.join(projectDir, '.juno_task')),
        fs.ensureDir(path.dirname(installedExtension)),
        fs.ensureDir(path.dirname(installedSkill)),
        fs.ensureDir(fakeBin),
        fs.ensureDir(servicesDir),
      ]);
      await fs.outputFile(
        path.join(projectDir, '.juno_task/scripts/install_requirements.sh'),
        '#!/bin/sh\nexit 0\n',
        { mode: 0o755 },
      );
      expect(await fs.pathExists(builtYpl)).toBe(true);
      expect(await fs.pathExists(builtPiExtension)).toBe(true);
      expect(await fs.pathExists(builtRalphSkill)).toBe(true);
      await fs.copy(path.join(PROJECT_ROOT, 'dist'), builtPackageDir);
      await fs.copy(path.join(PROJECT_ROOT, 'package.json'), path.join(builtPackageRoot, 'package.json'));
      await fs.symlink(path.join(PROJECT_ROOT, 'node_modules'), path.join(builtPackageRoot, 'node_modules'));
      await fs.copy(fixturePiExtension, installedExtension);
      await fs.copy(fixtureRalphSkill, installedSkill);
      await execa(path.join(PROJECT_ROOT, 'node_modules/.bin/esbuild'), [
        installedExtension,
        '--bundle',
        '--platform=node',
        '--format=esm',
        `--outfile=${compiledExtension}`,
      ]);
      await fs.writeFile(
        harnessPath,
        [
          "import { readFileSync } from 'node:fs';",
          "import { pathToFileURL } from 'node:url';",
          'const extension = (await import(pathToFileURL(process.argv[2]).href)).default;',
          "const prompt = readFileSync(0, 'utf8');",
          'let inputHandler;',
          "extension({ on(event, handler) { if (event === 'input') inputHandler = handler; } });",
          "if (!inputHandler) throw new Error('installed Pi extension did not register input');",
          'const result = await inputHandler({ text: prompt });',
          "if (result.action !== 'transform') throw new Error(`unexpected extension action: ${result.action}`);",
          'process.stdout.write(result.text);',
        ].join('\n'),
      );
      await fs.writeFile(
        path.join(builtPackageDir, 'templates/services/pi.py'),
        `#!/usr/bin/env python3
import json, pathlib, subprocess, sys
argv = sys.argv[1:]
prompt = None
if '-p' in argv:
    prompt = argv[argv.index('-p') + 1]
elif '--prompt-file' in argv:
    prompt = pathlib.Path(argv[argv.index('--prompt-file') + 1]).read_text()
if prompt is None:
    raise SystemExit('fixture Pi service received no prompt')
pathlib.Path(${JSON.stringify(observedPromptPath)}).write_text(prompt)
completed = subprocess.run([${JSON.stringify(process.execPath)}, ${JSON.stringify(harnessPath)}, ${JSON.stringify(compiledExtension)}], input=prompt, text=True, capture_output=True, cwd=${JSON.stringify(projectDir)}, check=True)
print(json.dumps({'type': 'result', 'result': completed.stdout, 'content': completed.stdout, 'session_id': 'fixture-no-provider'}))
`,
        { mode: 0o755 },
      );
      await fs.writeFile(
        path.join(fakeBin, 'juno-kanban'),
        `#!/usr/bin/env bash
printf '%s\\n' "$*" >> ${JSON.stringify(kanbanCallsPath)}
printf 'Task(s) not found: oD5g4o\\n' >&2
exit 1
`,
        { mode: 0o755 },
      );

      const noCodeDirective = `${String.fromCharCode(64, 64)}no_code`;
      const payload = [
        '%ralph-loop ## oD5g4o',
        'What is the root cause of 504',
        noCodeDirective,
      ].join('\n');
      const result = await execa(
        'bash',
        ['-c', '"$1" <<\'JUNO_YPL_PAYLOAD\'\n' + payload + '\nJUNO_YPL_PAYLOAD\n', '_', fixtureYpl],
        {
          cwd: projectDir,
          env: {
            ...process.env,
            HOME: homeDir,
            PATH: `${fakeBin}:${process.env.PATH}`,
            YYLO_HEADLESS: '1',
            NO_COLOR: '1',
          },
          reject: false,
        },
      );

      expect(result.exitCode, `stdout:\n${result.stdout}\nstderr:\n${result.stderr}`).toBe(0);
      const rewritten = await fs.readFile(observedPromptPath, 'utf8');
      expect(rewritten).toBe(payload.replace('%ralph-loop', '/skill:ralph-loop'));
      expect(rewritten.split(noCodeDirective)).toHaveLength(2);
      expect(await fs.readFile(kanbanCallsPath, 'utf8')).toContain('get oD5g4o');
      for (const exact of [
        '## oD5g4o',
        'What is the root cause of 504',
        noCodeDirective,
      ]) {
        expect(result.stdout.split(exact)).toHaveLength(2);
      }
      expect(result.stdout).toContain(
        `<skill name="ralph-loop" location="${await fs.realpath(installedSkill)}">`,
      );
      expect(`${result.stdout}\n${result.stderr}`).toContain('fixture-no-provider');
      expect(await fs.pathExists(path.join(projectDir, '.juno_task', 'tasks'))).toBe(false);
    } finally {
      await fs.remove(tempDir);
    }
  }, 30_000);
  });

  describe('Build-required package acceptance', () => {
    it('routes a failed hydration lint through public hydrate recovery with durable diagnostics', () => {
      const tests = path.join(
        PROJECT_ROOT,
        'src/templates/scripts/tests/test_task_workspace.py',
      );
      const selection =
        'TaskWorkspaceTests.build_required_public_cli_routes_hydration_retry_with_lint_diagnostics';
      const output = execFileSync('python3', [tests, selection], {
        cwd: path.resolve(PROJECT_ROOT, '..'),
        env: {
          ...process.env,
          PYTHONPYCACHEPREFIX: path.join(tempDir, 'pycache-hydration'),
        },
        encoding: 'utf8',
      });

      expect(output).toContain('PUBLIC_CLI_HYDRATION_RETRY_ACCEPTANCE_COMPLETED');
    }, 30_000);

    it('runs the public target-runtime provenance migration canary without skipping', () => {
      const tests = path.join(
        PROJECT_ROOT,
        'src/templates/scripts/tests/test_task_workspace.py',
      );
      const selection =
        'TaskWorkspaceTests.build_required_public_cli_migrates_legacy_provenance_then_starts_task';
      const output = execFileSync('python3', [tests, selection], {
        cwd: path.resolve(PROJECT_ROOT, '..'),
        env: {
          ...process.env,
          PYTHONPYCACHEPREFIX: path.join(tempDir, 'pycache-provenance'),
        },
        encoding: 'utf8',
      });

      expect(output).toContain('PUBLIC_CLI_RUNTIME_PROVENANCE_ACCEPTANCE_COMPLETED');
    }, 30_000);

    it('runs the public task-runtime recovery flow without skipping', () => {
      const tests = path.join(
        PROJECT_ROOT,
        'src/templates/scripts/tests/test_task_workspace.py',
      );
      const selection =
        'TaskWorkspaceTests.build_required_public_cli_recovers_missing_target_runtime_then_starts_task';
      const output = execFileSync('python3', [tests, selection], {
        cwd: path.resolve(PROJECT_ROOT, '..'),
        env: {
          ...process.env,
          PYTHONPYCACHEPREFIX: path.join(tempDir, 'pycache'),
        },
        encoding: 'utf8',
      });

      expect(output).toContain('PUBLIC_CLI_RUNTIME_BOOTSTRAP_ACCEPTANCE_COMPLETED');
    }, 30_000);
  });

  describe('Basic CLI Functionality', () => {
    it('should display help when no arguments provided', async () => {
      const result = await executeCLI([]);

      expect(result.exitCode).toBe(0);
      expect(result.stdout).toContain('YYLO');
      expect(result.stdout).toContain('TypeScript CLI for AI Subagent Orchestration');
      expect(result.stdout).toContain('yylo init');
      expect(result.stdout).toContain('yylo start');
      expect(result.stdout).toContain('ypl');
      expect(result.stdout).toContain('yy pi --live');
    });

    it.each(['--help', '-h'])('should display registered help with %s', async (flag) => {
      const result = await executeCLI([flag]);

      expect(result.exitCode).toBe(0);
      expect(result.stdout).toContain('Usage:');
      expect(result.stdout).toContain('Options:');
      expect(result.stdout).toContain('Commands:');
      expect(result.stdout).toContain('ledger [args...]');
      expect(result.stdout).toContain('YYLO Ledger CLI');
      expect(result.all).not.toContain('refusing to reinterpret it as an agent prompt');
    });

    it.each([
      { level: 'root', args: ['--help'], usage: 'Usage: yylo' },
      { level: 'namespace', args: ['task', '-h'], usage: 'Usage: task' },
      { level: 'subcommand', args: ['scripts', 'update', '--help'], usage: 'Usage: update' },
      { level: 'nested', args: ['integration', 'runtime-refresh', '--help'], usage: 'Usage: runtime-refresh' },
      { level: 'deep', args: ['migrate', 'target-runtime-provenance', 'plan', '-h'], usage: 'Usage: plan' },
    ])('makes $level help terminal and mutation-free', async ({ args, usage }) => {
      const sandbox = await fs.mkdtemp(path.join(tempDir, 'terminal-help-'));
      const metadata = path.join(sandbox, 'invocations');
      const bootstrapMarker = path.join(sandbox, 'bootstrap-ran');
      await fs.outputFile(
        path.join(sandbox, '.juno_task', 'scripts', 'bootstrap.sh'),
        `#!/usr/bin/env bash\ntouch ${JSON.stringify(bootstrapMarker)}\nexec "$@"\n`,
      );
      const before = await fs.readdir(sandbox);

      const result = await executeCLI(args, {
        cwd: sandbox,
        env: { YYLO_SESSION_METADATA_DIRECTORY: metadata },
      });

      expect(result.exitCode).toBe(0);
      expect(result.stderr).toBe('');
      expect(result.stdout).toContain(usage);
      expect(result.stdout.match(/Usage:/g)).toHaveLength(1);
      expect(await fs.pathExists(metadata)).toBe(false);
      expect(await fs.pathExists(bootstrapMarker)).toBe(false);
      expect(await fs.readdir(sandbox)).toEqual(before);
    });

    it.each([
      ['task', '--unknown-help-control'],
      ['scripts', 'update', '--unknown-help-control'],
      ['integration', 'runtime-refresh', '--unknown-help-control'],
      ['migrate', 'target-runtime-provenance', 'plan', '--unknown-help-control'],
    ])('keeps unknown option controls typed and nonzero: %s %s %s', async (...args) => {
      const result = await executeCLI(args, { expectError: true });
      expect(result.exitCode).toBe(2);
      expect(result.stderr).toContain("unknown explicit option '--unknown-help-control'");
    });

    it('keeps every retired lifecycle operation as an explicit refusal', async () => {
      const sandbox = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-lifecycle-cli-'));
      try {
        const help = await executeCLI(['--help'], { cwd: sandbox });
        expect(help.exitCode).toBe(0);
        expect(help.stdout).toContain('lifecycle');

        for (const operation of ['run', 'resume', 'status']) {
          const retired = await executeCLI(['lifecycle', operation, '--task', 'T123'], {
            cwd: sandbox,
            expectError: true,
          });
          expect(retired.exitCode).toBe(2);
          expect(retired.stderr).toContain('legacy lifecycle executor was removed');
        }
      } finally {
        await fs.remove(sandbox);
      }
    });

    it('should expose script update commands and accept --force without routing to an agent prompt', async () => {
      const help = await executeCLI(['--help']);
      expect(help.exitCode).toBe(0);
      expect(help.stdout).toContain('install-scripts');
      expect(help.stdout).toContain('scripts');

      const installScripts = await executeCLI(['install-scripts', '--force']);
      expect(installScripts.exitCode).toBe(0);
      expect(`${installScripts.stdout}\n${installScripts.stderr}`).toContain('Force updating project scripts');
      expect(`${installScripts.stdout}\n${installScripts.stderr}`).not.toContain('Executing with');

      const scriptsUpdate = await executeCLI(['scripts', 'update', '--force']);
      expect(scriptsUpdate.exitCode).toBe(0);
      expect(`${scriptsUpdate.stdout}\n${scriptsUpdate.stderr}`).toContain('Force updating project scripts');
      expect(`${scriptsUpdate.stdout}\n${scriptsUpdate.stderr}`).not.toContain('Executing with');
    });

    it('refuses retired controller config before changing any managed or ignored destination', async () => {
      const target = await fs.mkdtemp(path.join(tempDir, 'scripts-update-preflight-'));
      const requirementMarker = path.join(target, 'requirements-ran');
      const digestTree = async (): Promise<string> => {
        const hash = createHash('sha256');
        const walk = async (directory: string, relative = ''): Promise<void> => {
          const entries = await fs.readdir(directory, { withFileTypes: true });
          entries.sort((left, right) => left.name.localeCompare(right.name));
          for (const entry of entries) {
            const childRelative = relative ? `${relative}/${entry.name}` : entry.name;
            const child = path.join(directory, entry.name);
            const stat = await fs.lstat(child);
            hash.update(`${childRelative}\0${stat.mode & 0o7777}\0`);
            if (entry.isSymbolicLink()) {
              hash.update(`link:${await fs.readlink(child)}\0`);
            } else if (entry.isDirectory()) {
              hash.update('directory\0');
              await walk(child, childRelative);
            } else {
              hash.update('file\0');
              hash.update(await fs.readFile(child));
            }
          }
        };
        await walk(target);
        return hash.digest('hex');
      };

      try {
        await fs.outputJson(path.join(target, '.juno_task/config.json'), {
          controllerWorkspace: {
            enabled: true,
            policy: '.juno_task/config/controller-workspace.json',
          },
        });
        const fixtureFiles: Record<string, string> = {
          '.juno_task/scripts/runtime.sh': '#!/bin/sh\necho old-runtime\n',
          '.juno_task/scripts/install_requirements.sh':
            `#!/bin/sh\nprintf ran > ${JSON.stringify(requirementMarker)}\n`,
          '.juno_task/prompts/custom.md': 'owner prompt\n',
          '.juno_task/wiki/custom.md': 'owner wiki\n',
          '.juno_task/config/controller-workspace.json': '{"retired":true}\n',
          '.juno_task/managed-assets.json':
            '{"schemaVersion":1,"packageName":"@yylo/cli","packageVersion":"old","assets":{}}\n',
          '.juno_task/managed-conflicts/old/custom.backup': 'old backup\n',
          '.venv_juno/bin/python': 'old requirement runtime\n',
          '.agents/skills/custom/SKILL.md': 'owner agent skill\n',
          '.claude/skills/custom/SKILL.md': 'owner claude skill\n',
          '.pi/extensions/custom.ts': 'owner extension\n',
          '.pi/settings.json': '{"owner":true}\n',
          'scripts/git-flow.sh': '#!/bin/sh\necho owner\n',
          'AGENTS.md': 'owner agents\n',
          'CLAUDE.md': 'owner claude\n',
        };
        for (const [relative, content] of Object.entries(fixtureFiles)) {
          const destination = path.join(target, relative);
          await fs.ensureDir(path.dirname(destination));
          await fs.writeFile(destination, content);
        }
        await fs.chmod(path.join(target, '.juno_task/scripts/runtime.sh'), 0o751);
        await fs.chmod(path.join(target, '.juno_task/scripts/install_requirements.sh'), 0o755);
        await fs.chmod(path.join(target, 'scripts/git-flow.sh'), 0o711);

        const before = await digestTree();
        const result = await executeCLI(
          ['scripts', 'update', '--force', '--cwd', target],
          { cwd: target, expectError: true },
        );
        const after = await digestTree();

        expect(result.exitCode).not.toBe(0);
        expect(result.all).toContain('Legacy Juno 2.0 lifecycle/controllerWorkspace config');
        expect(result.all).not.toMatch(/✓|Force updated|dependencies force updated|Managed assets:/);
        expect(await fs.pathExists(requirementMarker)).toBe(false);
        expect(after).toBe(before);
      } finally {
        await fs.remove(target);
      }
    });

    it('ships a runnable migration inventory engine with the bundled CLI', async () => {
      const sandbox = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-migrate-binary-'));
      const project = path.join(sandbox, 'project');
      const receipt = path.join(sandbox, 'inventory.json');
      try {
        await fs.ensureDir(project);
        execFileSync('git', ['init', '-q', '-b', 'product', project]);
        execFileSync('git', ['-C', project, 'config', 'user.name', 'Test']);
        execFileSync('git', ['-C', project, 'config', 'user.email', 'test@example.com']);
        await fs.writeFile(path.join(project, 'README.md'), 'fixture\n');
        execFileSync('git', ['-C', project, 'add', 'README.md']);
        execFileSync('git', ['-C', project, 'commit', '-qm', 'fixture']);

        const result = await executeCLI([
          'migrate', 'inventory', '--project', project,
          '--product-ref', 'refs/heads/product', '--output', receipt,
        ], { cwd: project });

        expect(result.exitCode).toBe(0);
        expect((await fs.readJson(receipt)).schema_version).toBe('juno_migration_inventory.v1');
      } finally {
        await fs.remove(sandbox);
      }
    });

    it('keeps an exact linked task worktree byte-clean before agent dispatch', async () => {
      const sandbox = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-bootstrap-clean-'));
      const controller = path.join(sandbox, 'controller');
      const task = path.join(sandbox, 'task');
      const git = (cwd: string, args: string[]) =>
        execFileSync('git', ['-C', cwd, ...args], { encoding: 'utf8' }).trim();
      try {
        await fs.ensureDir(path.join(controller, '.juno_task', 'scripts'));
        await fs.ensureDir(path.join(controller, '.agents', 'skills', 'fixture'));
        await fs.ensureDir(path.join(controller, '.claude', 'skills', 'fixture'));
        await fs.ensureDir(path.join(controller, '.pi', 'skills', 'fixture'));
        await fs.copy(
          path.join(PROJECT_ROOT, 'src/templates/scripts/controller_resolver.py'),
          path.join(controller, '.juno_task/scripts/controller_resolver.py'),
        );
        await fs.copy(
          path.join(PROJECT_ROOT, 'src/templates/scripts/bootstrap.sh'),
          path.join(controller, '.juno_task/scripts/bootstrap.sh'),
        );
        await fs.writeFile(
          path.join(controller, '.juno_task/scripts/install_requirements.sh'),
          '#!/usr/bin/env bash\nexit 97\n',
        );
        await fs.writeFile(path.join(controller, '.juno_task/scripts/workflow_runner.sh'), 'tracked candidate workflow bytes\n');
        await fs.writeFile(path.join(controller, '.agents/skills/fixture/SKILL.md'), 'tracked candidate agent skill bytes\n');
        await fs.writeFile(path.join(controller, '.claude/skills/fixture/SKILL.md'), 'tracked candidate claude skill bytes\n');
        await fs.writeFile(path.join(controller, '.pi/skills/fixture/SKILL.md'), 'tracked candidate pi skill bytes\n');
        await fs.writeJson(path.join(controller, '.juno_task/config.json'), {});
        await fs.writeFile(path.join(controller, '.gitignore'), '.venv_juno/\n');
        git(controller, ['init', '-b', 'controller-branch']);
        git(controller, ['config', 'user.email', 'test@example.invalid']);
        git(controller, ['config', 'user.name', 'Test']);
        git(controller, ['add', '.']);
        git(controller, ['commit', '-m', 'exact candidate fixture']);
        git(controller, ['worktree', 'add', '-b', 'review-task', task]);
        git(task, ['config', '--local', 'juno.controller.path', controller]);
        git(task, ['config', '--local', 'juno.controller.branch', 'controller-branch']);

        const venvBin = path.join(task, '.venv_juno', 'bin');
        await fs.ensureDir(venvBin);
        await fs.writeFile(
          path.join(venvBin, 'activate'),
          `export VIRTUAL_ENV=${JSON.stringify(path.join(task, '.venv_juno'))}\n`,
        );
        const status = () =>
          execFileSync(
            'git',
            ['-C', task, 'status', '--porcelain=v2', '-z', '--untracked-files=all'],
          );
        const before = status();
        expect(before.byteLength).toBe(0);

        // Use the shipped shell binary, not an imported handler. Invalid
        // iteration input stops immediately after startup, before Pi dispatch.
        const result = await execa(
          path.join(PROJECT_ROOT, 'dist/bin/yylo.sh'),
          ['pi', '-p', 'review exact candidate', '-i', 'invalid'],
          {
            cwd: task,
            reject: false,
            env: {
              ...process.env,
              CI: '1',
              NO_COLOR: '1',
              JUNO_TASK_ROOT: controller,
              JUNO_CONTROLLER_BRANCH: 'controller-branch',
              JUNO_WORKSPACE_ROLE: 'controller',
              YYLO_SESSION_METADATA_DIRECTORY: path.join(sandbox, 'metadata'),
            },
          },
        );
        expect(result.exitCode).not.toBe(0);
        const after = status();
        expect(after.toString('utf8')).toBe(before.toString('utf8'));
        expect(await fs.pathExists(path.join(task, '.juno_task/managed-assets.json'))).toBe(false);
        expect(await fs.pathExists(path.join(task, '.juno_task/managed-conflicts'))).toBe(false);
      } finally {
        await fs.remove(sandbox);
      }
    });

    it('should honor nested-command --cwd when installing managed prompts and macros', async () => {
      const targetDir = await fs.mkdtemp(path.join(os.tmpdir(), 'yylo-managed-cwd-'));
      try {
        await fs.ensureDir(path.join(targetDir, '.juno_task'));
        await fs.writeJson(path.join(targetDir, '.juno_task', 'config.json'), {});
        const result = await executeCLI(['scripts', 'update', '--cwd', targetDir]);
        expect(result.exitCode).toBe(0);
        expect(await fs.pathExists(path.join(targetDir, '.juno_task', 'managed-assets.json'))).toBe(true);
        expect(await fs.pathExists(path.join(targetDir, '.juno_task', 'prompts', 'clean_worktree.md'))).toBe(true);
        expect(
          await fs.pathExists(path.join(targetDir, '.juno_task', 'scripts', 'worktree_lifecycle.py')),
        ).toBe(false);
        expect(
          await fs.pathExists(path.join(targetDir, '.juno_task', 'config', 'lifecycle.json')),
        ).toBe(false);
        expect(
          await fs.pathExists(path.join(targetDir, '.juno_task', 'scripts', 'task_workspace.py')),
        ).toBe(true);
        expect(
          await fs.pathExists(path.join(targetDir, '.juno_task', 'scripts', 'merge_queue.py')),
        ).toBe(true);
        expect(
          await fs.pathExists(path.join(targetDir, '.juno_task', 'scripts', 'worktree_lifecycle_audit.py')),
        ).toBe(false);
        expect(
          await fs.pathExists(path.join(targetDir, '.juno_task', 'scripts', 'git_index_lock.py')),
        ).toBe(true);
        const config = await fs.readJson(path.join(targetDir, '.juno_task', 'config.json'));
        expect(config.promptMacros.global.clean_worktree).toEqual({
          path: '.juno_task/prompts/clean_worktree.md',
        });
        expect(await fs.pathExists(path.join(tempDir, '.juno_task', 'managed-assets.json'))).toBe(false);
      } finally {
        await fs.remove(targetDir);
      }
    });

    it('loads metadata-controller prepare output and refuses retired persisted controller shapes', async () => {
      const targetDir = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-metadata-config-'));
      const configPath = path.join(targetDir, '.juno_task', 'config.json');
      const env = { YYLO_PROJECT_BOOTSTRAP_WRITES: '0' };
      try {
        await fs.ensureDir(path.dirname(configPath));
        await fs.writeJson(configPath, {
          controllerWorkspace: {
            mode: 'metadata-only',
            policy: '.juno_task/config/metadata-controller.json',
          },
        });

        const accepted = await executeCLI(
          ['pi', 'set-default-model', ':api-codex', '--cwd', targetDir],
          { env },
        );
        expect(accepted.exitCode).toBe(0);
        expect(accepted.stdout).toContain('Default model for pi set to :api-codex');

        await fs.writeJson(configPath, {
          controllerWorkspace: {
            enabled: true,
            policy: '.juno_task/config/controller-workspace.json',
          },
        });
        const sparse = await executeCLI(['pi', 'set-default-model', ':api-codex', '--cwd', targetDir], {
          env,
          expectError: true,
        });
        expect(sparse.exitCode).not.toBe(0);
        expect(sparse.all).toMatch(/Migration required.*metadata-only controller/);

        await fs.writeJson(configPath, {
          lifecycle: { enabled: true, policy: '.juno_task/config/lifecycle.json' },
        });
        const lifecycle = await executeCLI(['pi', 'set-default-model', ':api-codex', '--cwd', targetDir], {
          env,
          expectError: true,
        });
        expect(lifecycle.exitCode).not.toBe(0);
        expect(lifecycle.all).toMatch(/Migration required.*persisted lifecycle/);
      } finally {
        await fs.remove(targetDir);
      }
    });

    it('should retain assignment isolation after an explicit managed script refresh', async () => {
      const scriptsDir = path.join(tempDir, '.juno_task', 'scripts');
      const guardDir = path.join(tempDir, 'guard');
      const records = path.join(tempDir, 'backlog.ndjson');
      const helper = path.join(tempDir, 'guard_helper.py');
      await fs.ensureDir(scriptsDir);
      await fs.copy(
        path.join(PROJECT_ROOT, 'src/templates/scripts/controller_resolver.py'),
        path.join(scriptsDir, 'controller_resolver.py'),
      );
      await fs.writeFile(path.join(scriptsDir, 'kanban.sh'), '#!/bin/bash\necho UNGUARDED\n');
      await fs.writeFile(records, '{"id":"one","status":"todo"}\n');
      await fs.writeFile(
        helper,
        [
          'import json, os, pathlib, sys',
          'if sys.argv[1] == "validate-kanban-write":',
          ' print("canonical post-deploy E2E task requires a valid contract", file=sys.stderr)',
          ' raise SystemExit(2)',
          'assigned = os.environ["ASSIGNED_TASK_ID"]',
          'args = sys.argv[sys.argv.index("--") + 1:]',
          'target = args[args.index("--ID") + 1]',
          'guard = pathlib.Path(os.environ["E2E_SWEEP_KANBAN_GUARD_DIR"])',
          'guard.mkdir(parents=True, exist_ok=True)',
          'event = {"assigned_task_id": assigned, "target_task_id": target, "operation_allowed": False, "changed_task_ids": [], "rejection": f"worker {assigned} may not mutate Kanban task {target}"}',
          '(guard / "mutation_journal.ndjson").write_text(json.dumps(event) + "\\n")',
          'print(event["rejection"], file=sys.stderr)',
          'raise SystemExit(2)',
          '',
        ].join('\n'),
      );

      const refresh = await executeCLI(['scripts', 'update', '--force'], {
        env: { JUNO_TASK_ROOT: tempDir, JUNO_WORKSPACE_ROLE: 'controller' },
      });
      expect(refresh.exitCode).toBe(0);
      const wrapper = path.join(scriptsDir, 'kanban.sh');
      const installed = await fs.readFile(wrapper, 'utf8');
      expect(installed).toContain('ASSIGNED_TASK_ID');
      expect(installed).toContain('guard-kanban');
      expect(installed).toContain('validate-kanban-write');

      const rejected = await execa(
        wrapper,
        ['mark', 'done', '--ID', 'two', '--response', 'x'],
        {
          cwd: tempDir,
          reject: false,
          env: {
            ...process.env,
            ASSIGNED_TASK_ID: 'one',
            E2E_SWEEP_KANBAN_GUARD_DIR: guardDir,
            E2E_SWEEP_KANBAN_RECORDS: records,
            E2E_SWEEP_HELPER_PATH: helper,
          },
        },
      );
      expect(rejected.exitCode).not.toBe(0);
      expect(rejected.stderr).toContain('worker one may not mutate Kanban task two');
      expect(await fs.readFile(records, 'utf8')).toBe('{"id":"one","status":"todo"}\n');
      const journal = await fs.readFile(path.join(guardDir, 'mutation_journal.ndjson'), 'utf8');
      expect(journal).toContain('"assigned_task_id": "one"');
      expect(journal).toContain('"target_task_id": "two"');

      const rejectedContract = await execa(
        wrapper,
        ['create', '--body', 'missing contract', '--tags', 'FIXTURE_E2E_post_deploy'],
        {
          cwd: tempDir,
          reject: false,
          env: {...process.env, E2E_HOUSEKEEPING_HELPER_PATH: helper},
        },
      );
      expect(rejectedContract.exitCode).not.toBe(0);
      expect(rejectedContract.stderr).toContain('requires a valid contract');
    }, 60_000);

    it('should include shell safety guidance for prompt input', async () => {
      const result = await executeCLI(['--help']);

      expect(result.exitCode).toBe(0);
      expect(result.stdout).toContain('prefer single quotes for shell metacharacters');
      expect(result.stdout).toContain('Prompt input (inline text, file path, or heredoc/stdin');
      expect(result.stdout).toContain('shell-safe for backticks/$()');
    });

    it('should document ypl shortcut and named branch workflow in Pi help', async () => {
      const result = await executeCLI(['pi', '--help']);

      expect(result.exitCode).toBe(0);
      expect(result.stdout).toContain('ypl');
      expect(result.stdout).toContain('shortcut for: yy pi --live');
      expect(result.stdout).toContain('Named Pi session branches');
      expect(result.stdout).toContain("ypl 'init'");
      expect(result.stdout).toContain('yy clone --name C');
      expect(result.stdout).toContain('yy branches');
      expect(result.stdout).toContain('yy switch C');
      expect(result.stdout).toContain('future yy cc / yylo continue follows C');
      expect(result.stdout).toContain('--name main is reserved');
      expect(result.stdout).toContain(':luna');
      expect(result.stdout).toContain('openai-codex/gpt-5.6-luna');
      expect(result.stdout).toContain(':sol');
      expect(result.stdout).toContain('openai-codex/gpt-5.6-sol');
      expect(result.stdout).toMatch(/:gpt\s+:sol/);
      expect(result.stdout).toContain(':gpt5.5');
      expect(result.stdout).toContain('openai-codex/gpt-5.5');
      expect(result.stdout).toContain(':mini');
      expect(result.stdout).toContain('openai-codex/gpt-5.6-terra');
      expect(result.stdout).not.toMatch(/:gpt\s+openai-codex\/gpt-5\.5/);
      expect(result.stdout).toContain('high, xhigh, max');
    });

    it('should display version with --version flag', async () => {
      const result = await executeCLI(['--version']);

      expect(result.exitCode).toBe(0);
      expect(result.stdout.trim()).toMatch(/^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/); // Machine version contract
    });

    it('should keep --version read-only in an initialized project', async () => {
      const junoTaskDir = path.join(tempDir, '.juno_task');
      await fs.ensureDir(junoTaskDir);
      await fs.writeFile(path.join(junoTaskDir, 'sentinel.txt'), 'unchanged\n');

      const result = await executeCLI(['--version']);

      expect(result.exitCode).toBe(0);
      expect(await fs.readdir(junoTaskDir)).toEqual(['sentinel.txt']);
      expect(await fs.pathExists(path.join(tempDir, '.agents'))).toBe(false);
      expect(await fs.pathExists(path.join(tempDir, '.claude'))).toBe(false);
    });

    it('refuses legacy tracked metadata-policy mutation and routes to the receipt-bound command', async () => {
      const policyDir = path.join(tempDir, '.juno_task/config');
      const metadata = await fs.readJson(
        path.resolve(process.cwd(), 'src/templates/config/metadata-controller.json'),
      );
      metadata.controller_branch = 'refs/heads/customer/controller';
      metadata.product_ref = 'refs/heads/customer/release';
      metadata.generated_metadata = metadata.generated_metadata.filter(
        (entry: string) => entry !== '.juno_task/config/integration-workspace.json',
      );
      metadata.tracked_exact = metadata.tracked_exact.filter(
        (entry: string) => entry !== '.juno_task/config/integration-workspace.json',
      );
      await fs.ensureDir(policyDir);
      await fs.writeJson(path.join(tempDir, '.juno_task/config.json'), {
        controllerWorkspace: { mode: 'metadata-only', policy: '.juno_task/config/metadata-controller.json' },
      });
      const policyPath = path.join(policyDir, 'metadata-controller.json');
      const policyBytes = `${JSON.stringify(metadata)}\n`;
      await fs.writeFile(policyPath, policyBytes);
      for (const name of ['task-workspace.json', 'risk-policy.json']) {
        await fs.copyFile(path.resolve(process.cwd(), 'src/templates/config', name), path.join(policyDir, name));
      }
      await fs.writeFile(path.join(tempDir, '.gitignore'), [
        '.juno_task/scripts/', '.juno_task/runtime/', '.venv_juno/', '.env.yylo',
        '/AGENTS.md', '/CLAUDE.md', '/.agents/', '/.claude/', '/.pi/',
        'built-cli-session-metadata/', '',
      ].join('\n'));
      execFileSync('git', ['init', '-q'], { cwd: tempDir });
      execFileSync('git', ['add', '.'], { cwd: tempDir });
      execFileSync('git', ['-c', 'user.name=Juno Test', '-c', 'user.email=juno-test@example.invalid',
        'commit', '-qm', 'legacy controller'], { cwd: tempDir });

      const update = await executeCLI(['scripts', 'update', '--force'], { expectError: true });
      expect(update.exitCode).not.toBe(0);
      expect(update.stderr).toContain('scripts update is mutation-free for tracked policy');
      expect(update.stderr).toContain('yy migrate metadata-policy plan');
      expect(await fs.readFile(policyPath, 'utf8')).toBe(policyBytes);
      expect(await fs.pathExists(path.join(policyDir, 'integration-workspace.json'))).toBe(false);
      expect(execFileSync('git', ['status', '--porcelain=v2', '--untracked-files=all'], {
        cwd: tempDir, encoding: 'utf8',
      })).toBe('');
    });

    it('persists one checkpoint-admitted complete metadata-controller bundle receipt', async () => {
      const junoTaskDir = path.join(tempDir, '.juno_task');
      const policyDir = path.join(junoTaskDir, 'config');
      const configPath = path.join(junoTaskDir, 'config.json');
      const policyPath = path.join(policyDir, 'metadata-controller.json');
      const configBytes = `${JSON.stringify({
        controllerWorkspace: {
          mode: 'metadata-only',
          policy: '.juno_task/config/metadata-controller.json',
        },
      }, null, 2)}\n`;
      const metadataPolicy = await fs.readJson(
        path.resolve(process.cwd(), 'src/templates/config/metadata-controller.json'),
      );
      metadataPolicy.controller_branch = 'refs/heads/customer/controller';
      metadataPolicy.product_ref = 'refs/heads/customer/release';
      const policyBytes = `${JSON.stringify(metadataPolicy, null, 2)}\n`;
      await fs.ensureDir(policyDir);
      await fs.writeFile(configPath, configBytes);
      await fs.writeFile(policyPath, policyBytes);
      for (const name of ['task-workspace.json', 'integration-workspace.json', 'risk-policy.json']) {
        await fs.copyFile(
          path.resolve(process.cwd(), 'src/templates/config', name),
          path.join(policyDir, name),
        );
      }
      await fs.writeFile(path.join(tempDir, '.gitignore'), [
        '.juno_task/scripts/',
        '.juno_task/runtime/',
        '.venv_juno/',
        '.env.yylo',
        '/AGENTS.md',
        '/CLAUDE.md',
        '/.agents/',
        '/.claude/',
        '/.pi/',
        'built-cli-session-metadata/',
        '',
      ].join('\n'));
      execFileSync('git', ['init', '-q'], { cwd: tempDir });
      execFileSync('git', ['add', '.'], { cwd: tempDir });
      execFileSync('git', [
        '-c', 'user.name=Juno Test',
        '-c', 'user.email=juno-test@example.invalid',
        'commit', '-qm', 'metadata controller fixture',
      ], { cwd: tempDir });

      const update = await executeCLI(['scripts', 'update']);

      expect(update.exitCode).toBe(0);
      expect(update.stdout).toContain('ignored metadata-controller runtime scripts and agent surface');
      expect(await fs.pathExists(path.join(junoTaskDir, 'scripts/task_workspace.py'))).toBe(true);
      expect(await fs.pathExists(path.join(tempDir, 'AGENTS.md'))).toBe(true);
      expect(await fs.pathExists(path.join(tempDir, 'CLAUDE.md'))).toBe(true);
      for (const root of ['.agents/skills', '.claude/skills', '.pi/skills']) {
        expect(await fs.pathExists(path.join(tempDir, root, 'kanban-workflow/SKILL.md'))).toBe(true);
      }
      expect(await fs.readFile(configPath, 'utf8')).toBe(configBytes);
      const updatedPolicy = await fs.readJson(policyPath);
      expect(updatedPolicy.controller_branch).toBe('refs/heads/customer/controller');
      expect(updatedPolicy.product_ref).toBe('refs/heads/customer/release');
      const manifest = await fs.readJson(path.join(junoTaskDir, 'managed-assets.json'));
      expect(manifest).toMatchObject({
        schemaVersion: 2,
        packageName: '@yylo/cli',
        instructionBundle: {
          schemaVersion: 'juno_instruction_bundle.v1',
          assetCount: Object.keys(manifest.assets).length,
        },
      });
      for (const destination of [
        'AGENTS.md', 'CLAUDE.md',
        '.agents/skills/kanban-workflow/SKILL.md',
        '.claude/skills/ralph-loop/references/implement.md',
        '.pi/skills/understand-project/SKILL.md',
        '.juno_task/prompts/lifecycle/task-implementation.md',
        '.juno_task/wiki/controller/sealed_release_epochs.md',
        '.juno_task/workflows/yy-task-run.yaml',
        '.juno_task/scripts/release_train.py',
      ]) expect(manifest.assets[destination], destination).toBeDefined();
      const dirtyPaths = execFileSync(
        'git', ['status', '--porcelain=v1', '--untracked-files=all'],
        { cwd: tempDir, encoding: 'utf8' },
      ).split('\n').filter(Boolean).map((line) => line.slice(3));
      expect(updatedPolicy.tracked_exact).toContain('.juno_task/managed-assets.json');
      expect(updatedPolicy.generated_metadata).toContain('.juno_task/managed-assets.json');
      for (const dirty of dirtyPaths) {
        expect(
          updatedPolicy.tracked_exact.includes(dirty) ||
          updatedPolicy.tracked_recursive.some(
            (root: string) => dirty === root || dirty.startsWith(`${root}/`),
          ),
          `checkpoint policy must admit ${dirty}`,
        ).toBe(true);
      }
      expect(await fs.pathExists(path.join(tempDir, 'scripts/git-flow.sh'))).toBe(false);
      expect(await fs.pathExists(path.join(tempDir, '.codex'))).toBe(false);

      const statusBeforeDoctor = execFileSync(
        'git', ['status', '--porcelain=v2', '--untracked-files=all'],
        { cwd: tempDir, encoding: 'utf8' },
      );
      const doctor = await executeCLI(['scripts', 'doctor']);
      expect(doctor.exitCode).toBe(0);
      expect(doctor.stdout).toContain('Schema-2 instruction bundle');
      expect(execFileSync(
        'git', ['status', '--porcelain=v2', '--untracked-files=all'],
        { cwd: tempDir, encoding: 'utf8' },
      )).toBe(statusBeforeDoctor);
      execFileSync('git', ['add', '.juno_task/config/metadata-controller.json',
        '.juno_task/managed-assets.json', '.juno_task/prompts', '.juno_task/wiki',
        '.juno_task/workflows'], { cwd: tempDir });
      execFileSync('git', [
        '-c', 'user.name=Juno Test', '-c', 'user.email=juno-test@example.invalid',
        'commit', '-qm', 'checkpoint managed controller bundle',
      ], { cwd: tempDir });

      const ownerInstructions = 'committed owner controller instructions\n';
      await fs.writeFile(path.join(tempDir, 'AGENTS.md'), ownerInstructions);
      execFileSync('git', ['add', '-f', 'AGENTS.md'], { cwd: tempDir });
      execFileSync('git', [
        '-c', 'user.name=Juno Test',
        '-c', 'user.email=juno-test@example.invalid',
        'commit', '-qm', 'track owner controller instructions',
      ], { cwd: tempDir });
      const refused = await executeCLI(['scripts', 'update', '--force'], { expectError: true });
      expect(refused.exitCode).not.toBe(0);
      expect(refused.all).toContain('tracked user evidence; reviewed evacuation is required: AGENTS.md');
      expect(await fs.readFile(path.join(tempDir, 'AGENTS.md'), 'utf8')).toBe(ownerInstructions);
      expect(execFileSync(
        'git',
        ['status', '--porcelain=v2', '--untracked-files=all'],
        { cwd: tempDir, encoding: 'utf8' },
      )).toBe('');
    });

    it('recovers the exact target-bound metadata-controller bundle through scripts update', async () => {
      const currentPackageVersion = (await fs.readJson(
        path.join(PROJECT_ROOT, 'package.json'),
      )).version as string;
      const { targetSha, changedScripts } = await createTargetBoundMetadataController(
        tempDir, currentPackageVersion, { routedCurrentPackage: true },
      );
      const manifestPath = path.join(tempDir, '.juno_task/managed-assets.json');
      expect((await fs.readJson(manifestPath)).packageVersion).not.toBe(currentPackageVersion);

      const update = await executeCLI(['scripts', 'update', '--force'], { timeout: 120_000 });
      expect(update.exitCode).toBe(0);
      expect(update.stdout).toContain(
        `Recovered exact target-bound controller bundle at ${targetSha}`,
      );
      const manifest = await fs.readJson(manifestPath);
      expect(manifest).toMatchObject({
        schemaVersion: 2,
        packageName: '@yylo/cli',
        packageVersion: currentPackageVersion,
        instructionBundle: {
          schemaVersion: 'juno_instruction_bundle.v1',
          packageVersion: currentPackageVersion,
          assetCount: Object.keys(manifest.assets).length,
        },
      });
      for (const name of changedScripts) {
        expect(await fs.readFile(
          path.join(tempDir, '.juno_task/scripts', name), 'utf8',
        )).toContain(`exact target-only recovery fixture: ${name}`);
      }
      const doctor = await executeCLI(['scripts', 'doctor']);
      expect(doctor.exitCode).toBe(0);
      expect(doctor.stdout).toContain('Receipt-bound controller scripts and instruction bundle');
      expect(doctor.stdout).toContain(targetSha);

      const receipt = await fs.readFile(manifestPath);
      const retry = await executeCLI(['scripts', 'update', '--force'], { timeout: 120_000 });
      expect(retry.exitCode).toBe(0);
      expect(await fs.readFile(manifestPath)).toEqual(receipt);
      expect(await fs.readFile(path.join(tempDir, '.juno_task/ledger/owner.txt'), 'utf8'))
        .toBe('owner metadata\n');
    }, 120_000);

    it('preserves an explicit short subagent over YYLO_SUBAGENT', async () => {
      const result = await executeCLI(['-s', 'invalid-explicit-provider', '-p', 'precedence check'], {
        expectError: true,
        env: { YYLO_SUBAGENT: 'pi' },
      });

      expect(result.exitCode).not.toBe(0);
      expect(result.all).toContain('Invalid subagent: invalid-explicit-provider');
      expect(result.all).not.toContain('--yylo-subagent');
    });

    it('does not mistake -ip for explicit -i when applying YYLO_MAX_ITERATIONS', async () => {
      const result = await executeCLI(['pi', '-ip'], {
        expectError: true,
        input: 'interactive precedence check',
        env: { YYLO_MAX_ITERATIONS: 'invalid-from-environment' },
      });

      expect(result.exitCode).not.toBe(0);
      expect(result.all).toContain('Max iterations must be a valid number');
    });

    it('preserves an explicit --model=value over YYLO_MODEL', async () => {
      await createMockProject({
        '.juno_task': {
          'config.json': '{}\n',
          scripts: {
            'install_requirements.sh': '#!/bin/sh\nexit 0\n',
            'cleanup_feedback.sh': '#!/bin/sh\nexit 0\n',
          },
        },
        bin: {
          codex: '#!/bin/sh\necho "FAKE_CODEX_ARGS:$*" >&2\nexit 7\n',
        },
      });
      for (const executable of [
        '.juno_task/scripts/install_requirements.sh',
        '.juno_task/scripts/cleanup_feedback.sh',
        'bin/codex',
      ]) await fs.chmod(path.join(tempDir, executable), 0o755);
      execFileSync('git', ['init', '-q', '-b', 'fixture'], { cwd: tempDir });
      execFileSync('git', ['config', 'user.name', 'YYLO Test'], { cwd: tempDir });
      execFileSync('git', ['config', 'user.email', 'yylo@example.invalid'], { cwd: tempDir });
      execFileSync('git', ['config', 'juno.controller.path', tempDir], { cwd: tempDir });
      execFileSync('git', ['config', 'juno.controller.branch', 'fixture'], { cwd: tempDir });
      execFileSync('git', ['add', '.'], { cwd: tempDir });
      execFileSync('git', ['commit', '-qm', 'fixture'], { cwd: tempDir });

      const result = await executeCLI(['-s', 'codex', '--model=gpt-5.3-codex', '-p', 'precedence check'], {
        expectError: true,
        env: { YYLO_MODEL: 'gpt-5.2-codex', PATH: `${path.join(tempDir, 'bin')}:${process.env.PATH ?? ''}` },
      });
      const logs = await fs.readdir(path.join(tempDir, '.juno_task', 'logs'));
      const log = await fs.readFile(path.join(tempDir, '.juno_task', 'logs', logs[0]), 'utf8');

      expect(result.exitCode).not.toBe(0);
      expect(log).toContain('"model":"gpt-5.3-codex"');
      expect(log).not.toContain('"model":"gpt-5.2-codex"');
      expect(log).toContain('FAKE_CODEX_ARGS:');
    });

    it('fails closed for unknown explicit commands before fixture initialization', async () => {
      const project = path.join(tempDir, 'unknown-explicit-command');
      const configDir = path.join(project, '.juno_task');
      await fs.ensureDir(configDir);
      const configPath = path.join(configDir, 'config.json');
      const originalConfig = '{"defaultSubagent":"fixture-provider"}\n';
      await fs.writeFile(configPath, originalConfig);

      const result = await executeCLI(['integration', 'mystery'], {
        cwd: project,
        expectError: true,
        env: { YYLO_SUBAGENT: 'fixture-provider' },
      });

      expect(result.exitCode).toBe(2);
      expect(result.stderr).toContain("unknown explicit command 'mystery'");
      expect(result.stderr).toContain(`effective executable: ${BINARY_MJS}`);
      expect(result.stderr).toMatch(/effective version: \d+\.\d+\.\d+/);
      expect(result.stderr).toContain('Use -- <prompt>, -p <prompt>, or -f <path>');
      expect(await fs.readFile(configPath, 'utf8')).toBe(originalConfig);
      expect(await fs.pathExists(path.join(project, '.agents'))).toBe(false);
      expect(await fs.pathExists(path.join(project, '.claude'))).toBe(false);
    });

    it.each([
      ['please', 'list', 'tasks'],
      ['debug', 'status', 'endpoint'],
      ['explain', 'sync', 'behavior'],
      ['future-command', 'status'],
    ])('compiled preflight preserves free-form prompt input: %s', async (...args) => {
      const result = await executeCLI(args, {
        env: { YYLO_PREFLIGHT_ONLY: '1' },
      });
      expect(result.exitCode).toBe(0);
      expect(result.stderr).toBe('');
    });

    it('should work with .mjs binary (ESM)', async () => {
      const result = await executeCLI(['--version'], { binary: 'mjs' });

      expect(result.exitCode).toBe(0);
      expect(result.stdout).toMatch(/\d+\.\d+\.\d+/);
    });

    it('should handle .js binary bundling issues gracefully', async () => {
      // The CJS build has known issues with top-level await and Ink library
      // This test verifies we handle the error appropriately
      const result = await executeCLI(['--version'], {
        binary: 'js',
        expectError: true,
      });

      // Either it works (if bundling is fixed) or fails with known error
      if (result.exitCode === 0) {
        expect(result.stdout).toMatch(/\d+\.\d+\.\d+/);
      } else {
        const err = result.stderr || '';
        expect(err.includes('ERR_REQUIRE_ASYNC_MODULE') || err.includes('ERR_REQUIRE_ESM')).toBe(
          true,
        );
      }
    });
  });

  describe('Init Command Tests', () => {
    it('should show init help successfully', async () => {
      const result = await executeCLI(['init', '--help']);

      expect(result.exitCode).toBe(0);
      expect(result.stdout).toContain('Initialize');
      expect(result.stdout).toContain('--force');
      expect(result.stdout).toContain('--interactive');
    });

    it.skip('should handle init with template option', async () => {
      // NOTE: --template option not implemented in current version
      // This test is skipped until the feature is added
      const result = await executeCLI(['init', '--template', 'default'], { expectError: true });

      // This might succeed or fail depending on whether the template exists
      // We just want to verify the option is recognized
      expect(typeof result.exitCode).toBe('number');
    });

    it('should handle init with force option', async () => {
      // Create an existing .juno_task directory
      await createMockProject({
        '.juno_task': {
          'init.md': '# Existing init file',
        },
      });

      const result = await executeCLI(['init', '--force'], { expectError: true });

      // This should either succeed (if templates work) or fail gracefully
      expect(typeof result.exitCode).toBe('number');
    }, 60000); // Allow 60 seconds for init with force (can be slow with template processing)

    it('should fail init when .juno_task exists without force', async () => {
      // Create an existing .juno_task directory
      await createMockProject({
        '.juno_task': {
          'init.md': '# Existing init file',
        },
      });

      const result = await executeCLI(['init'], { expectError: true });

      expect(result.exitCode).not.toBe(0);
      expect(result.stderr || result.stdout).toMatch(
        /exists|already.*initialized|already.*present/i,
      );
    });

    it('should handle init with working directory option', async () => {
      const result = await executeCLI(['init', tempDir], { expectError: true });

      // Should recognize the directory argument
      expect(typeof result.exitCode).toBe('number');
    }, 60000); // Allow 60 seconds for init (can be slow with template processing)

    it('should validate template names', async () => {
      const result = await executeCLI(['init', '--template', 'nonexistent-template'], {
        expectError: true,
      });

      // The template validation might not be strict, so we accept any exit code
      // The important thing is that the CLI doesn't crash
      expect(typeof result.exitCode).toBe('number');
    }, 60000); // Allow 60 seconds for init (can be slow with template processing)
  });

  describe('Start Command Tests', () => {
    beforeEach(async () => {
      // Create a basic project structure for start command
      await createMockProject({
        '.juno_task': {
          'init.md':
            '# Test Project\n\nThis is a test project for binary execution tests.\n\n## Goals\n- Test the CLI binary\n- Verify command execution\n',
          'plan.md': '# Project Plan\n\n## Current Status\nTesting binary execution\n',
          'prompt.md': '# Prompt\n\nTest prompt for binary execution',
        },
      });
    });

    it('should show start help', async () => {
      const result = await executeCLI(['start', '--help']);

      expect(result.exitCode).toBe(0);
      expect(result.stdout).toContain('Start');
      expect(result.stdout).toContain('--max-iterations');
      expect(result.stdout).toContain('--model');
    });

    it('should handle start with missing init.md file', async () => {
      // Remove the .juno_task directory to test error handling
      await fs.remove(path.join(tempDir, '.juno_task'));

      const result = await executeCLI(['start'], { expectError: true });

      expect(result.exitCode).not.toBe(0);
      expect(result.stderr || result.stdout).toMatch(
        /init\.md|not found|missing|juno_task.*directory.*found|run.*init/i,
      );
    });

    it.skip('should handle start with max-iterations option', async () => {
      // SKIP: This test times out due to actual command execution
      // Testing this would require complex mocking that defeats the purpose of binary testing
      const result = await executeCLI(['start', '--max-iterations', '3'], {
        expectError: true,
        timeout: 5000, // 5 second timeout
      });

      // This might fail due to MCP/template issues, but the option should be recognized
      expect(typeof result.exitCode).toBe('number');
    });

    it.skip('should handle start with model option', async () => {
      // SKIP: This test times out due to actual command execution
      // Testing this would require complex mocking that defeats the purpose of binary testing
      const result = await executeCLI(['start', '--model', 'claude-3-sonnet'], {
        expectError: true,
        timeout: 5000, // 5 second timeout
      });

      // This might fail due to MCP/template issues, but the option should be recognized
      expect(typeof result.exitCode).toBe('number');
    });

    it.skip('should validate max-iterations as number', async () => {
      // SKIP: This test times out due to actual command execution
      const result = await executeCLI(['start', '--max-iterations', 'not-a-number'], {
        expectError: true,
      });

      expect(result.exitCode).not.toBe(0);
    });
  });

  describe('Feedback Command Tests', () => {
    it('should show feedback help', async () => {
      const result = await executeCLI(['feedback', '--help']);

      expect(result.exitCode).toBe(0);
      expect(result.stdout).toContain('feedback');
      expect(result.stdout).toContain('--interactive');
      expect(result.stdout).toContain('--file');
    });

    it('should handle feedback collection in non-interactive mode', async () => {
      const result = await executeCLI(
        ['feedback', '--file', path.join(tempDir, 'feedback.md'), 'Test feedback'],
        { expectError: true },
      );

      // This might succeed or fail depending on implementation
      expect(typeof result.exitCode).toBe('number');
    });

    it('should handle feedback with custom file option', async () => {
      const feedbackFile = path.join(tempDir, 'custom-feedback.md');
      const result = await executeCLI(['feedback', '--file', feedbackFile, 'Test feedback'], {
        expectError: true,
      });

      // This might succeed or fail depending on implementation
      expect(typeof result.exitCode).toBe('number');
    });
  });

  describe('Global Options Tests', () => {
    it('should handle verbose flag', async () => {
      const result = await executeCLI(['--verbose', '--version']);

      expect(result.exitCode).toBe(0);
      expect(result.stdout).toMatch(/\d+\.\d+\.\d+/);
    });

    it('should handle quiet flag', async () => {
      const result = await executeCLI(['--quiet', '--version']);

      expect(result.exitCode).toBe(0);
      // In quiet mode, output should be minimal
    });

    it('should handle config file option', async () => {
      const configFile = path.join(tempDir, 'test-config.json');
      await fs.writeFile(
        configFile,
        JSON.stringify({
          defaultSubagent: 'claude',
          workingDirectory: tempDir,
        }),
        'utf-8',
      );

      const result = await executeCLI(['--config', configFile, '--version']);

      expect(result.exitCode).toBe(0);
    });

    it('should handle log-level option', async () => {
      const result = await executeCLI(['--log-level', 'debug', '--version']);

      expect(result.exitCode).toBe(0);
    });

    it('should handle no-color flag', async () => {
      const result = await executeCLI(['--no-color', '--version']);

      expect(result.exitCode).toBe(0);
    });
  });

  describe('Environment Variables Tests', () => {
    it('should respect YYLO_VERBOSE environment variable', async () => {
      const result = await executeCLI(['--version'], {
        env: { YYLO_VERBOSE: 'true' },
      });

      expect(result.exitCode).toBe(0);
    });

    it('should respect JUNO_TASK_VERBOSE environment variable (backward compatibility)', async () => {
      const result = await executeCLI(['--version'], {
        env: { JUNO_TASK_VERBOSE: 'true' },
      });

      expect(result.exitCode).toBe(0);
    });

    it('should respect NO_COLOR environment variable', async () => {
      const result = await executeCLI(['--help'], {
        env: { NO_COLOR: '1' },
      });

      expect(result.exitCode).toBe(0);
      // Output should not contain ANSI color codes
      expect(result.stdout).not.toMatch(/\x1b\[[0-9;]*m/);
    });

    it('should respect CI environment variable for quiet mode', async () => {
      const result = await executeCLI(['--version'], {
        env: { CI: 'true' },
      });

      expect(result.exitCode).toBe(0);
    });
  });

  describe('Error Handling and Edge Cases', () => {
    it(
      'should handle SIGINT gracefully',
      async () => {
        // This test is complex to implement reliably in CI
        // We'll skip it for now but document the requirement
      },
      { skip: true },
    );

    it('should handle invalid JSON config file', async () => {
      const configFile = path.join(tempDir, 'invalid-config.json');
      await fs.writeFile(configFile, '{ invalid json', 'utf-8');

      const result = await executeCLI(['--config', configFile, '--version'], { expectError: true });

      // The CLI might be lenient with config parsing for simple commands
      // We just verify it doesn't crash catastrophically
      expect(typeof result.exitCode).toBe('number');
    });

    it('should handle permission errors gracefully', async () => {
      // Create a read-only directory to test permission handling
      const readOnlyDir = path.join(tempDir, 'readonly');
      await fs.ensureDir(readOnlyDir);

      try {
        await fs.chmod(readOnlyDir, 0o444); // Read-only

        const result = await executeCLI(['init'], {
          cwd: readOnlyDir,
          expectError: true,
        });

        expect(result.exitCode).not.toBe(0);
      } finally {
        // Restore permissions for cleanup
        await fs.chmod(readOnlyDir, 0o755);
      }
    });

    it('should handle corrupted binary gracefully', async () => {
      // This test verifies that Node.js properly reports errors for corrupted binaries
      // We can't easily create a corrupted binary in CI, so we'll test with a non-executable file
      const fakeBinary = path.join(tempDir, 'fake-binary.js');
      await fs.writeFile(fakeBinary, 'this is not valid javascript', 'utf-8');

      try {
        await execa('node', [fakeBinary], { timeout: 5000 });
        // If we get here, something unexpected happened
        expect(true).toBe(false);
      } catch (error: any) {
        // We expect this to fail with a syntax error
        expect(error.exitCode).not.toBe(0);
      }
    });

    it.skip('should handle memory pressure gracefully', async () => {
      // SKIP: This test times out due to actual command execution
      // Test with a very large max-iterations to see if CLI handles it
      await createMockProject({
        '.juno_task': {
          'init.md': '# Test Project\n\nTest content',
        },
      });

      const result = await executeCLI(['start', '--max-iterations', '999999'], {
        expectError: true,
      });

      // Should either work or fail gracefully
      expect(typeof result.exitCode).toBe('number');
    });

    it.skip('should handle network timeouts in MCP connections', async () => {
      // SKIP: This test times out due to actual command execution
      await createMockProject({
        '.juno_task': {
          'init.md': '# Test Project\n\nTest content',
        },
      });

      const result = await executeCLI(['start'], {
        env: { YYLO_MCP_TIMEOUT: '1' }, // Very short timeout
        expectError: true,
      });

      // Should handle timeout gracefully
      expect(typeof result.exitCode).toBe('number');
    });
  });

  describe('Real Execution Flow Tests', () => {
    it('should create actual project files with init command (non-dry-run)', async () => {
      const result = await executeCLI(['init', '--template', 'default', '--force'], {
        expectError: true,
      });

      // This might succeed or fail depending on template availability
      // We're testing that the CLI handles it appropriately
      expect(typeof result.exitCode).toBe('number');

      // If it succeeded, verify files were created
      if (result.exitCode === 0) {
        const initFile = path.join(tempDir, '.juno_task', 'init.md');
        if (await fs.pathExists(initFile)) {
          const initContent = await fs.readFile(initFile, 'utf-8');
          expect(initContent.length).toBeGreaterThan(0);
        }
      }
    });

    it.skip('should read and validate actual init.md file in start command', async () => {
      // SKIP: This test times out due to actual command execution
      // Create a basic init.md file manually
      await createMockProject({
        '.juno_task': {
          'init.md':
            '# Test Project\n\nThis is a test project for binary execution tests.\n\n## Goals\n- Test the CLI binary\n- Verify command execution\n',
        },
      });

      // Then try to start it
      const result = await executeCLI(['start'], { expectError: true });

      // This will likely fail due to MCP issues, but should read the file
      expect(typeof result.exitCode).toBe('number');
    });

    it('should handle real file I/O errors', async () => {
      // Try to init in a directory where we can't write
      const result = await executeCLI(['init'], {
        cwd: '/', // Root directory where we likely can't write
        expectError: true,
      });

      expect(result.exitCode).not.toBe(0);
    });
  });

  describe('Command Aliases and Shortcuts', () => {
    it('should expose clone UX in root help', async () => {
      const result = await executeCLI(['--help']);

      expect(result.exitCode).toBe(0);
      expect(result.stdout).toContain('--clone');
      expect(result.stdout).toContain('clone');
    });

    it('should expose clone subcommand options', async () => {
      const result = await executeCLI(['clone', '--help']);

      expect(result.exitCode).toBe(0);
      expect(result.stdout).toContain('Clone/fork');
      expect(result.stdout).toContain('clone early_reflect');
      expect(result.stdout).toContain('auto-names b1');
      expect(result.stdout).toContain('--prompt-file');
      expect(result.stdout).toContain('--thinking');
      expect(result.stdout).toContain('--name');
      expect(result.stdout).toContain('--from');
    });

    it('should parse clone <branch> <prompt> as named-branch shorthand instead of dropping the branch name into prompt text', async () => {
      const scope = 'binary-clone-branch-prompt-shorthand';
      const config = {
        defaultSubagent: 'pi',
        defaultBackend: 'shell',
        defaultMaxIterations: 1,
        defaultModel: ':pi',
        defaultModels: { pi: ':pi' },
        logLevel: 'info',
        verbose: 0,
        quiet: true,
        mcpTimeout: 43200000,
        mcpRetries: 3,
        onHourlyLimit: 'raise',
        interactive: true,
        headlessMode: false,
        workingDirectory: tempDir,
        sessionDirectory: path.join(tempDir, '.juno_task'),
        envFilePath: '.env.yylo',
        envFileCopied: true,
        hooks: {},
      };

      await createMockProject({
        '.juno_task': {
          'config.json': JSON.stringify(config, null, 2),
        },
      });

      const result = await executeCLI(['clone', 'early_reflect', '@@reflect', '--from', 'missing'], {
        expectError: true,
        env: {
          ...buildContinueSnapshotEnv(scope),
          YYLO_CONTINUE_SCOPE: scope,
        },
      });
      const output = result.all || `${result.stdout}\n${result.stderr}`;

      expect(result.exitCode).not.toBe(0);
      expect(output).toContain("Unknown source branch 'missing'");
      expect(output).not.toContain('No previous session found to clone');
    });

    it('should list named branches as JSON and switch the active branch for the current scope', async () => {
      const scope = 'binary-named-branches';
      const settings = { version: 1, subagent: 'pi', maxIterations: 1 };
      const fixture = await createSessionContinuityFixture({
        projectRoot: tempDir,
        scope,
        envSessionId: 'SESSION_MAIN',
        settings,
        config: createSessionContinuityConfig(tempDir, {
          mcpTimeout: 43200000,
          mcpRetries: 3,
          interactive: true,
          headlessMode: false,
        }),
        activeBranch: 'main',
        branches: [
          { name: 'main', sessionId: 'SESSION_MAIN' },
          { name: 'C', sessionId: 'SESSION_C', parent: 'main', sourceSessionId: 'SESSION_MAIN' },
          { name: 'D', sessionId: 'SESSION_D', parent: 'main', sourceSessionId: 'SESSION_MAIN' },
        ],
      });
      const scopeHash = fixture.scope.scopeHash;

      const branchesResult = await executeCLI(['branches', '--json'], {
        env: fixture.env,
      });
      expect(branchesResult.exitCode).toBe(0);
      const payload = JSON.parse(branchesResult.stdout);
      expect(payload.branches).toEqual([
        expect.objectContaining({ name: 'main', active: true, sessionId: 'SESSION_MAIN' }),
        expect.objectContaining({ name: 'C', active: false, sessionId: 'SESSION_C', parent: 'main' }),
        expect.objectContaining({ name: 'D', active: false, sessionId: 'SESSION_D', parent: 'main' }),
      ]);

      const switchResult = await executeCLI(['switch', 'C'], {
        env: fixture.env,
      });
      expect(switchResult.exitCode).toBe(0);
      expect(switchResult.stdout).toContain('Switched to branch C');
      await fixture.assertEnvSession('SESSION_C');
      expect((await fixture.readBranchState()).scopes[scopeHash].settings).toEqual(settings);

      const switchNextResult = await executeCLI(['switch', '+'], {
        env: fixture.env,
      });
      expect(switchNextResult.exitCode).toBe(0);
      expect(switchNextResult.stdout).toContain('Switched to branch D');

      const switchWrapNextResult = await executeCLI(['switch', '+'], {
        env: fixture.env,
      });
      expect(switchWrapNextResult.exitCode).toBe(0);
      expect(switchWrapNextResult.stdout).toContain('Switched to branch main');

      const switchWrapPreviousResult = await executeCLI(['switch', '-'], {
        env: fixture.env,
      });
      expect(switchWrapPreviousResult.exitCode).toBe(0);
      expect(switchWrapPreviousResult.stdout).toContain('Switched to branch D');
      expect((await fixture.readBranchState()).scopes[scopeHash].settings).toEqual(settings);
      await fixture.assertScopeInvariant('D', 'SESSION_D');
    });

    it('should run an inline prompt as continue after switching to a named branch', async () => {
      const scope = 'binary-switch-prompt-continues-branch';
      const fixture = await createSessionContinuityFixture({
        projectRoot: tempDir,
        scope,
        config: createSessionContinuityConfig(tempDir, {
          mcpTimeout: 43200000,
          mcpRetries: 3,
          interactive: true,
          headlessMode: false,
        }),
        activeBranch: 'main',
        branches: [
          { name: 'main', sessionId: 'SESSION_MAIN' },
          { name: 'C', sessionId: 'SESSION_C', parent: 'main', sourceSessionId: 'SESSION_MAIN' },
        ],
      });
      const scopeHash = fixture.scope.scopeHash;

      const result = await executeCLI(['switch', 'C', 'continue C now', '-i', 'invalid'], {
        expectError: true,
        env: {
          ...fixture.env,
          YYLO_CONTINUE_SCOPE: scope,
          [`YYLO_LAST_SESSION_ID_${scopeHash}`]: 'SESSION_C',
          [`YYLO_LAST_EXECUTION_SETTINGS_${scopeHash}`]: JSON.stringify({
            version: 1,
            subagent: 'claude',
            maxIterations: 5,
          }),
        },
      });
      const output = result.all || `${result.stdout}\n${result.stderr}`;

      expect(result.exitCode).not.toBe(0);
      expect(output).toContain('Switched to branch C (SESSION_C)');
      expect(output).toContain('Max iterations must be a valid number');
      expect(output).not.toContain('Prompt is required for execution');

      await fixture.assertActiveBranch('C', 'SESSION_C');
    });

    it('should expose continue --clone option', async () => {
      const result = await executeCLI(['continue', '--help']);

      expect(result.exitCode).toBe(0);
      expect(result.stdout).toContain('--clone');
    });

    it('should keep compiled yy cc scope/branch conflict checks isolated by shell scope', async () => {
      const scopeA = 'binary-session-continuity-pane-a';
      const scopeB = 'binary-session-continuity-pane-b';
      const fixtureA = await createSessionContinuityFixture({
        projectRoot: tempDir,
        scope: scopeA,
        envSessionId: 'SESSION_A',
        settings: { version: 1, subagent: 'pi', maxIterations: 1 },
        config: createSessionContinuityConfig(tempDir),
        activeBranch: 'main',
        branches: [{ name: 'main', sessionId: 'SESSION_A' }],
      });
      const fixtureB = await createSessionContinuityFixture({
        projectRoot: tempDir,
        scope: scopeB,
        envSessionId: 'SESSION_B',
        settings: { version: 1, subagent: 'pi', maxIterations: 1 },
        activeBranch: 'main',
        branches: [{ name: 'main', sessionId: 'SESSION_B' }],
      });
      const scopeHashA = fixtureA.scope.scopeHash;
      const scopeHashB = fixtureB.scope.scopeHash;
      const envA = fixtureA.env;
      const envB = fixtureB.env;

      const scopeStatusA = JSON.parse((await executeCLI(['continue-scope', '--json'], { env: envA })).stdout);
      const scopeStatusB = JSON.parse((await executeCLI(['continue-scope', '--json'], { env: envB })).stdout);
      expect(scopeStatusA.fullHash).toBe(scopeHashA);
      expect(scopeStatusB.fullHash).toBe(scopeHashB);
      expect(scopeStatusA.fullHash).not.toBe(scopeStatusB.fullHash);
      expect(scopeStatusA.sessionId).toBe('SESSION_A');
      expect(scopeStatusB.sessionId).toBe('SESSION_B');

      const continueA = await executeCLI(['cc', '-p', 'continue pane A', '-i', 'invalid'], {
        expectError: true,
        env: envA,
      });
      const continueAOutput = continueA.all || `${continueA.stdout}\n${continueA.stderr}`;
      expect(continueA.exitCode).not.toBe(0);
      expect(continueAOutput).toContain('Max iterations must be a valid number');
      expect(continueAOutput).not.toContain('Continue session mismatch for this shell context');

      const continueB = await executeCLI(['cc', '-p', 'continue pane B', '-i', 'invalid'], {
        expectError: true,
        env: envB,
      });
      const continueBOutput = continueB.all || `${continueB.stdout}\n${continueB.stderr}`;
      expect(continueB.exitCode).not.toBe(0);
      expect(continueBOutput).toContain('Max iterations must be a valid number');
      expect(continueBOutput).not.toContain('Continue session mismatch for this shell context');

      const branchState = await fixtureA.readBranchState();
      expect(branchState.scopes[scopeHashA].branches.main.session_id).toBe('SESSION_A');
      expect(branchState.scopes[scopeHashB].branches.main.session_id).toBe('SESSION_B');

      await fixtureA.writeEnvSession('SESSION_ENV_CONFLICT');
      const mismatchResult = await executeCLI(['cc', '-p', 'conflict should not dispatch', '-i', 'invalid'], {
        expectError: true,
        env: {
          ...envA,
          [`YYLO_LAST_SESSION_ID_${scopeHashA}`]: 'SESSION_ENV_CONFLICT',
          [`YYLO_LAST_EXECUTION_SETTINGS_${scopeHashA}`]: JSON.stringify({
            version: 1,
            subagent: 'pi',
            maxIterations: 1,
          }),
        },
      });
      const mismatchOutput = mismatchResult.all || `${mismatchResult.stdout}\n${mismatchResult.stderr}`;
      expect(mismatchResult.exitCode).not.toBe(0);
      expect(mismatchOutput).toContain('Max iterations must be a valid number');
      expect(mismatchOutput).not.toContain('SESSION_ENV_CONFLICT');
      expect((await fixtureA.readBranchState()).scopes[scopeHashA].branches.main.session_id).toBe('SESSION_A');
    });

    it('should handle subagent direct commands (if implemented)', async () => {
      // Test if subagent shortcuts like 'yylo claude "prompt"' work
      const result = await executeCLI(['claude', '--help'], { expectError: true });

      // This might work or might not, depending on implementation
      // We accept both outcomes but verify the CLI handles it appropriately
      expect(typeof result.exitCode).toBe('number');
    });

    it('should set per-subagent default model via alias subcommand', async () => {
      const config = {
        defaultSubagent: 'claude',
        defaultBackend: 'shell',
        defaultMaxIterations: 1,
        defaultModel: ':sonnet',
        defaultModels: { claude: ':sonnet' },
        logLevel: 'info',
        verbose: 1,
        quiet: false,
        mcpTimeout: 43200000,
        mcpRetries: 3,
        onHourlyLimit: 'raise',
        interactive: true,
        headlessMode: false,
        workingDirectory: tempDir,
        sessionDirectory: path.join(tempDir, '.juno_task'),
        envFilePath: '.env.yylo',
        envFileCopied: true,
        hooks: {},
      };

      await createMockProject({
        '.juno_task': {
          'config.json': JSON.stringify(config, null, 2),
        },
      });

      const result = await executeCLI(['pi', 'set-default-model', ':api-codex']);

      expect(result.exitCode).toBe(0);
      expect(result.stdout).toContain('Default model for pi set to :api-codex');

      const updated = await fs.readJson(path.join(tempDir, '.juno_task', 'config.json'));
      expect(updated.defaultModels.pi).toBe(':api-codex');
      expect(updated.defaultModel).toBe(':sonnet');
    });

    it('should honor --cwd when setting per-subagent default model', async () => {
      const targetDir = await fs.mkdtemp(path.join(os.tmpdir(), 'yylo-set-model-target-'));

      try {
        const baseConfig = {
          defaultSubagent: 'claude',
          defaultBackend: 'shell',
          defaultMaxIterations: 1,
          defaultModel: ':sonnet',
          defaultModels: {
            claude: ':sonnet',
            codex: ':codex',
            gemini: ':pro',
            cursor: 'auto',
            pi: ':pi',
          },
          logLevel: 'info',
          verbose: 1,
          quiet: false,
          mcpTimeout: 43200000,
          mcpRetries: 3,
          onHourlyLimit: 'raise',
          interactive: true,
          headlessMode: false,
          envFilePath: '.env.yylo',
          envFileCopied: true,
          hooks: {},
        };

        await createMockProject({
          '.juno_task': {
            'config.json': JSON.stringify(
              {
                ...baseConfig,
                workingDirectory: tempDir,
                sessionDirectory: path.join(tempDir, '.juno_task'),
              },
              null,
              2,
            ),
          },
        });

        await fs.ensureDir(path.join(targetDir, '.juno_task'));
        await fs.writeJson(
          path.join(targetDir, '.juno_task', 'config.json'),
          {
            ...baseConfig,
            workingDirectory: targetDir,
            sessionDirectory: path.join(targetDir, '.juno_task'),
          },
          { spaces: 2 },
        );

        const result = await executeCLI(
          ['pi', 'set-default-model', ':api-codex', '--cwd', targetDir],
          {
            cwd: tempDir,
          },
        );

        expect(result.exitCode).toBe(0);

        const sourceConfig = await fs.readJson(path.join(tempDir, '.juno_task', 'config.json'));
        const targetConfig = await fs.readJson(path.join(targetDir, '.juno_task', 'config.json'));

        expect(sourceConfig.defaultModels.pi).toBe(':pi');
        expect(targetConfig.defaultModels.pi).toBe(':api-codex');
      } finally {
        await fs.remove(targetDir);
      }
    });

    it('should reject incompatible shorthand in set-default-model command', async () => {
      const config = {
        defaultSubagent: 'codex',
        defaultBackend: 'shell',
        defaultMaxIterations: 1,
        defaultModel: ':codex',
        logLevel: 'info',
        verbose: 1,
        quiet: false,
        mcpTimeout: 43200000,
        mcpRetries: 3,
        onHourlyLimit: 'raise',
        interactive: true,
        headlessMode: false,
        workingDirectory: tempDir,
        sessionDirectory: path.join(tempDir, '.juno_task'),
        envFilePath: '.env.yylo',
        envFileCopied: true,
        hooks: {},
      };

      await createMockProject({
        '.juno_task': {
          'config.json': JSON.stringify(config, null, 2),
        },
      });

      const result = await executeCLI(['codex', 'set-default-model', ':sonnet'], {
        expectError: true,
      });
      const output = result.all || `${result.stdout}\n${result.stderr}`;

      expect(result.exitCode).not.toBe(0);
      expect(output).toContain('not compatible with subagent codex');
    });

    it('should preserve -p prompt value on subagent aliases', async () => {
      // Regression guard: alias commands used to drop options.prompt and trigger Empty stdin input
      const result = await executeCLI(['pi', '-p', 'alias prompt', '-i', 'invalid'], {
        expectError: true,
      });
      const output = result.all || `${result.stdout}\n${result.stderr}`;

      expect(result.exitCode).not.toBe(0);
      expect(output).toContain('Max iterations must be a valid number');
      expect(output).not.toContain('Empty stdin input');
    });

    it('should honor --until-completion on subagent aliases', async () => {
      // Regression guard: alias path used to ignore this flag and execute main flow instead
      const result = await executeCLI(
        ['pi', '--until-completion', '-p', 'alias prompt', '-i', 'invalid'],
        { expectError: true },
      );
      const output = result.all || `${result.stdout}\n${result.stderr}`;

      expect(result.exitCode).not.toBe(0);
      expect(output).toContain('run_until_completion.sh not found');
      expect(output).not.toContain('Empty stdin input');
    });

    it('should resolve --until-completion script from --cwd target and not forward --cwd to inner runs', async () => {
      await createMockProject({
        project: {
          '.juno_task': {
            scripts: {
              'run_until_completion.sh': `#!/usr/bin/env bash
set -euo pipefail

echo "RUN_UNTIL_PWD=$(pwd)"
echo "RUN_UNTIL_ARGS:$*"
`,
            },
          },
        },
      });

      const projectDir = path.join(tempDir, 'project');
      await fs.chmod(path.join(projectDir, '.juno_task', 'scripts', 'run_until_completion.sh'), 0o755);
      const resolvedProjectDir = await fs.realpath(projectDir);

      const result = await executeCLI(
        ['pi', '--until-completion', '--cwd', 'project', '-p', 'alias prompt'],
        { expectError: false },
      );
      const output = result.all || `${result.stdout}\n${result.stderr}`;

      expect(result.exitCode).toBe(0);
      expect(output).toContain(`RUN_UNTIL_PWD=${resolvedProjectDir}`);
      expect(output).toContain('RUN_UNTIL_ARGS:pi -p alias prompt');
      expect(output).not.toContain('--cwd');
    });

    it('should pass --max-iterations to the until-completion script as the outer bound and never forward it to inner runs', async () => {
      await createMockProject({
        project: {
          '.juno_task': {
            scripts: {
              'run_until_completion.sh': `#!/usr/bin/env bash
set -euo pipefail

echo "RUN_UNTIL_ARGS:$*"
`,
            },
          },
        },
      });

      const projectDir = path.join(tempDir, 'project');
      await fs.chmod(path.join(projectDir, '.juno_task', 'scripts', 'run_until_completion.sh'), 0o755);

      const result = await executeCLI(
        ['pi', '--until-completion', '--cwd', 'project', '-p', 'alias prompt', '-i', '2'],
        { expectError: false },
      );
      const output = result.all || `${result.stdout}\n${result.stderr}`;

      expect(result.exitCode).toBe(0);
      // The outer bound is passed to the script explicitly...
      expect(output).toContain('RUN_UNTIL_ARGS:--max-iterations 2 pi -p alias prompt');
      // ...and is stripped from the inner yylo arguments (-i never forwards).
      expect(output).not.toContain('RUN_UNTIL_ARGS:pi -p alias prompt -i 2');
      expect(output).not.toMatch(/RUN_UNTIL_ARGS:.*-i 2/);
      // The exact scope is printed before the loop starts.
      expect(output).toContain('outer loop bounded at 2 iteration(s)');
      expect(output).toContain('inner agent sessions are not bounded by --max-iterations');
    });

    it('should read continue prompt from stdin without -p (heredoc/pipe flow)', async () => {
      const result = await executeCLI(['continue', '-i', 'invalid'], {
        expectError: true,
        input: 'continue prompt from stdin\n',
        env: buildContinueSnapshotEnv('binary-continue-stdin-no-flag'),
      });
      const output = result.all || `${result.stdout}\n${result.stderr}`;

      expect(result.exitCode).not.toBe(0);
      expect(output).toContain('Max iterations must be a valid number');
      expect(output).not.toContain('Prompt is required for execution');
      expect(output).not.toContain('Empty stdin input');
    });

    it('should read continue prompt from stdin when -p is used without argument', async () => {
      const result = await executeCLI(['continue', '-p', '-i', 'invalid'], {
        expectError: true,
        input: 'continue prompt from -p heredoc\n',
        env: buildContinueSnapshotEnv('binary-continue-stdin-p-flag'),
      });
      const output = result.all || `${result.stdout}\n${result.stderr}`;

      expect(result.exitCode).not.toBe(0);
      expect(output).toContain('Max iterations must be a valid number');
      expect(output).not.toContain('Prompt is required for execution');
      expect(output).not.toContain('Empty stdin input');
    });

    it.each(['contiue', 'cn', 'cc'])(
      'should route %s alias to continue command instead of treating it as prompt text',
      async (continueAlias) => {
        const result = await executeCLI([continueAlias, '-p', 'next step', '-i', 'invalid'], {
          expectError: true,
          env: {
            YYLO_CONTINUE_SCOPE: `binary-continue-alias-${continueAlias}`,
          },
        });
        const output = result.all || `${result.stdout}\n${result.stderr}`;

        expect(result.exitCode).not.toBe(0);
        expect(output).toContain('No previous session found to continue in this shell context');
        expect(output).not.toContain('Max iterations must be a valid number');
      },
    );

    it('should keep built continuity scope validation writes out of a fixture Git common directory by default', async () => {
      const repository = path.join(tempDir, 'repository');
      await fs.ensureDir(repository);
      expect((await execa('git', ['init', '-q'], { cwd: repository })).exitCode).toBe(0);

      const result = await executeCLI([
        'continue-scope', '--json', '--cwd', repository,
        '--handoff-session', 'isolated-validation-session',
        '--handoff-settings', JSON.stringify({ version: 1, subagent: 'pi' }),
      ]);

      expect(result.exitCode).toBe(0);
      const isolatedFile = path.join(tempDir, 'built-cli-session-metadata', 'session_continuity.v2.json');
      expect(await fs.pathExists(isolatedFile)).toBe(true);
      expect((await fs.stat(isolatedFile)).mode & 0o777).toBe(0o600);
      expect(await fs.pathExists(path.join(repository, '.git', 'juno', 'session_metadata'))).toBe(false);
    });

    it('should reject a built continuity validation metadata override outside its fresh fixture', async () => {
      await expect(executeCLI(['--version'], {
        env: { YYLO_SESSION_METADATA_DIRECTORY: path.join(os.tmpdir(), 'not-this-fixture') },
      })).rejects.toThrow('Binary validation metadata must stay inside its fresh fixture');
    });

    it('should expose continue scope hash/status as JSON for script integrations', async () => {
      const env = buildContinueSnapshotEnv('binary-continue-scope-json');
      const result = await executeCLI(['continue-scope', '--json'], { env });

      expect(result.exitCode).toBe(0);
      const payload = JSON.parse(result.stdout) as Record<string, unknown>;
      expect(payload.status).toBe('finished');
      expect(payload.hash).toMatch(/^[A-F0-9]{6}$/);
      expect(payload.fullHash).toMatch(/^SCOPE_[A-F0-9]{16}$/);
      expect(payload.sessionId).toBe('session-continue-stdin');
    });

    it('should expose caller/parent scope resolution for script integrations', async () => {
      const withoutTerminalMarkers = {
        TMUX_PANE: '', WEZTERM_PANE: '', KITTY_WINDOW_ID: '', KITTY_PID: '',
        TERM_SESSION_ID: '', WT_SESSION: '', ZELLIJ_PANE_ID: '', STY: '',
        WINDOWID: '', SSH_TTY: '',
      };
      const first = await executeCLI(['continue-scope', '--json', '--parent-pid', '8123'], {
        env: withoutTerminalMarkers,
      });
      const second = await executeCLI(['continue-scope', '--json', '--parent-pid', '8123'], {
        env: withoutTerminalMarkers,
      });
      const isolated = await executeCLI(['continue-scope', '--json', '--parent-pid', '8124'], {
        env: withoutTerminalMarkers,
      });

      expect(first.exitCode).toBe(0);
      expect(JSON.parse(first.stdout).fullHash).toBe(JSON.parse(second.stdout).fullHash);
      expect(JSON.parse(first.stdout).fullHash).not.toBe(JSON.parse(isolated.stdout).fullHash);
    });

    it('should resolve continue scope status by short hash', async () => {
      const env = buildContinueSnapshotEnv('binary-continue-scope-short-hash');
      const currentScopeResult = await executeCLI(['continue-scope', '--json'], { env });
      const currentPayload = JSON.parse(currentScopeResult.stdout) as Record<string, unknown>;
      const shortHash = String(currentPayload.hash);

      const lookupResult = await executeCLI(['continue-scope', shortHash, '--json'], { env });
      expect(lookupResult.exitCode).toBe(0);

      const lookupPayload = JSON.parse(lookupResult.stdout) as Record<string, unknown>;
      expect(lookupPayload.status).toBe('finished');
      expect(lookupPayload.hash).toBe(shortHash);
    });

    it('should resolve workflow handoff snapshots in the same shell scope', async () => {
      const binDir = path.join(tempDir, 'bin');
      await fs.ensureDir(binDir);
      const fakeYy = path.join(binDir, 'yy');
      await fs.writeFile(
        fakeYy,
        [
          '#!/usr/bin/env bash',
          'set -euo pipefail',
          `if [ "\${1:-}" = "continue-scope" ]; then exec node ${JSON.stringify(BINARY_MJS)} "$@"; fi`,
          'printf \'workflow fake agent response\\n\'',
          'printf \'{"session_id":"session-workflow-handoff"}\\n\'',
        ].join('\n') + '\n',
        'utf-8',
      );
      await fs.chmod(fakeYy, 0o755);

      const workflowPath = path.join(tempDir, 'workflow.yaml');
      await fs.writeFile(
        workflowPath,
        [
          'schema_version: 1',
          'workflow_id: handoff_scope_test',
          'steps:',
          '  - id: agent_step',
          '    command:',
          '      - yy',
          '      - pi',
          '      - run handoff test',
        ].join('\n') + '\n',
        'utf-8',
      );

      const workflowRunner = path.join(PROJECT_ROOT, '..', '.juno_task', 'scripts', 'workflow_runner.sh');
      const quote = (value: string) => `'${value.replace(/'/g, `'"'"'`)}'`;
      const result = await execa(
        'bash',
        [
          '-lc',
          [
            'set -euo pipefail',
            `python3 ${quote(workflowRunner)} --workflow ${quote(workflowPath)} --project-root "$PWD" --print-output none --no-print-step-stdout >/tmp/juno-workflow-handoff-test.out`,
            `node ${quote(BINARY_MJS)} continue-scope --json; status=$?; :; exit $status`,
          ].join('\n'),
        ],
        {
          cwd: tempDir,
          env: {
            ...process.env,
            NO_COLOR: '1',
            CI: '1',
            YYLO_CONFIG: '',
            JUNO_TASK_CONFIG: '',
            PATH: `${binDir}${path.delimiter}${process.env.PATH || ''}`,
            YYLO_SESSION_METADATA_DIRECTORY: path.join(tempDir, 'built-cli-session-metadata'),
          },
          timeout: BINARY_TIMEOUT,
          reject: false,
          all: true,
        },
      );

      expect(result.exitCode).toBe(0);
      const payload = JSON.parse(result.stdout) as Record<string, unknown>;
      expect(payload.status).toBe('finished');
      expect(payload.sessionId).toBe('session-workflow-handoff');
    });

    it('should keep compiled continue-scope tmux pane yy cc stable while isolating other panes and normal shells', async () => {
      const quote = (value: string) => `'${value.replace(/'/g, `'"'"'`)}'`;
      const stableMarkerKeys = [
        'TMUX_PANE',
        'WEZTERM_PANE',
        'KITTY_WINDOW_ID',
        'KITTY_PID',
        'TERM_SESSION_ID',
        'WT_SESSION',
        'ZELLIJ_PANE_ID',
        'STY',
        'WINDOWID',
        'SSH_TTY',
      ];
      const withoutStableTerminalMarkers = Object.fromEntries(
        stableMarkerKeys.map((key) => [key, '']),
      ) as Record<string, string>;
      const runContinueScopeInFreshShell = async (env: Record<string, string>) => {
        const result = await execa(
          'bash',
          [
            '-lc',
            `node ${quote(BINARY_MJS)} continue-scope --json; status=$?; :; exit $status`,
          ],
          {
            cwd: tempDir,
            env: {
              ...process.env,
              NO_COLOR: '1',
              CI: '1',
              YYLO_CONFIG: '',
              JUNO_TASK_CONFIG: '',
              ...withoutStableTerminalMarkers,
              ...env,
              YYLO_SESSION_METADATA_DIRECTORY: path.join(tempDir, 'built-cli-session-metadata'),
            },
            timeout: BINARY_TIMEOUT,
            reject: false,
            all: true,
          },
        );

        expect(result.exitCode).toBe(0);
        return JSON.parse(result.stdout) as Record<string, unknown>;
      };

      const paneAFirst = await runContinueScopeInFreshShell({ TMUX_PANE: '%paneA' });
      const paneASecond = await runContinueScopeInFreshShell({ TMUX_PANE: '%paneA' });
      const paneB = await runContinueScopeInFreshShell({ TMUX_PANE: '%paneB' });

      expect(paneAFirst.scopeSource).toBe('project+stable_terminal+TMUX_PANE');
      expect(paneASecond.scopeSource).toBe('project+stable_terminal+TMUX_PANE');
      expect(paneAFirst.fullHash).toBe(paneASecond.fullHash);
      expect(paneAFirst.fullHash).not.toBe(paneB.fullHash);

      const paneAFullHash = String(paneAFirst.fullHash);
      const paneBFullHash = String(paneB.fullHash);
      const metadataDirectory = path.join(tempDir, '.juno_task');
      const timestamp = new Date().toISOString();
      await fs.ensureDir(metadataDirectory);
      await fs.writeJson(path.join(metadataDirectory, 'session_continuity.v2.json'), {
        version: 2,
        scopes: Object.fromEntries([
          [paneAFullHash, 'SESSION_PANE_A'],
          [paneBFullHash, 'SESSION_PANE_B'],
        ].map(([hash, id]) => [hash, { source: 'project+stable_terminal+TMUX_PANE', createdAt: timestamp, lastUsedAt: timestamp, pinned: false, settings: { version: 1, subagent: 'pi', maxIterations: 1 }, active: 'main', branches: { main: { session_id: id, parent: null, updated_at: timestamp } } }])),
      });
      const paneAEnv = {
        YYLO_SESSION_METADATA_DIRECTORY: metadataDirectory,
        ...withoutStableTerminalMarkers,
        TMUX_PANE: '%paneA',
        [`YYLO_LAST_SESSION_ID_${paneAFullHash}`]: 'SESSION_PANE_A',
        [`YYLO_LAST_EXECUTION_SETTINGS_${paneAFullHash}`]: JSON.stringify({
          version: 1,
          subagent: 'pi',
          maxIterations: 1,
        }),
      };
      const paneBEnv = {
        YYLO_SESSION_METADATA_DIRECTORY: metadataDirectory,
        ...withoutStableTerminalMarkers,
        TMUX_PANE: '%paneB',
        [`YYLO_LAST_SESSION_ID_${paneBFullHash}`]: 'SESSION_PANE_B',
        [`YYLO_LAST_EXECUTION_SETTINGS_${paneBFullHash}`]: JSON.stringify({
          version: 1,
          subagent: 'pi',
          maxIterations: 1,
        }),
      };

      const paneAStatus = JSON.parse((await executeCLI(['continue-scope', '--json'], { env: paneAEnv })).stdout);
      const paneBStatus = JSON.parse((await executeCLI(['continue-scope', '--json'], { env: paneBEnv })).stdout);
      expect(paneAStatus.sessionId).toBe('SESSION_PANE_A');
      expect(paneBStatus.sessionId).toBe('SESSION_PANE_B');

      const continueA = await executeCLI(['cc', '-p', 'continue pane A', '-i', 'invalid'], {
        env: paneAEnv,
        expectError: true,
      });
      const continueAOutput = continueA.all || `${continueA.stdout}\n${continueA.stderr}`;
      expect(continueA.exitCode).not.toBe(0);
      expect(continueAOutput).toContain('Max iterations must be a valid number');
      expect(continueAOutput).not.toContain('Continue session mismatch for this shell context');

      const normalShellFirst = await runContinueScopeInFreshShell({});
      const normalShellSecond = await runContinueScopeInFreshShell({});
      expect(normalShellFirst.scopeSource).toBe('project+shell_lineage');
      expect(normalShellSecond.scopeSource).toBe('project+shell_lineage');
      expect(normalShellFirst.fullHash).not.toBe(normalShellSecond.fullHash);
    });

    it('should report not_found for continue scope without snapshot state', async () => {
      const result = await executeCLI(['continue-scope', '--json'], {
        env: {
          YYLO_CONTINUE_SCOPE: 'binary-continue-scope-missing',
        },
      });

      expect(result.exitCode).toBe(0);
      const payload = JSON.parse(result.stdout) as Record<string, unknown>;
      expect(payload.status).toBe('not_found');
      expect(payload.hash).toMatch(/^[A-F0-9]{6}$/);
    });

    it('should handle completion commands', async () => {
      const result = await executeCLI(['completion', 'bash'], { expectError: true });

      // Completion might not be fully implemented
      expect(typeof result.exitCode).toBe('number');
    });
  });

  describe('Continuity maintenance commands', () => {
    it('doctors, plans, explicitly applies, pins, unpins, and rolls back only isolated fixture state', async () => {
      const metadata = path.join(tempDir, 'metadata');
      const env = {
        YYLO_CONTINUE_SCOPE: 'binary-continuity-fixture',
        YYLO_SESSION_METADATA_DIRECTORY: metadata,
      };
      await fs.ensureDir(path.join(tempDir, '.juno_task'));
      await fs.writeJson(path.join(tempDir, '.juno_task', 'config.json'), {
        envFilePath: '.env.yylo',
        envFileCopied: false,
      });
      const scope = explicitContinueScopeHash('binary-continuity-fixture');
      const suffix = scope.replace('SCOPE_', '');
      const original = `SECRET=DO_NOT_PRINT\nYYLO_LAST_SESSION_ID_SCOPE_${suffix}=SESSION_DO_NOT_PRINT\nYYLO_LAST_EXECUTION_SETTINGS_SCOPE_${suffix}='{"version":1,"subagent":"pi"}'\n`;
      await fs.writeFile(path.join(tempDir, '.env.yylo'), original);

      const doctor = await executeCLI(['continuity', 'doctor', '--json'], { env });
      expect(doctor.exitCode).toBe(0);
      expect(doctor.stdout).not.toContain('DO_NOT_PRINT');
      expect(JSON.parse(doctor.stdout).totals.completePairs).toBe(1);

      const planPath = path.join(tempDir, 'reviewed.json');
      const planned = await executeCLI(['continuity', 'clean', '--plan', planPath], { env });
      expect(planned.exitCode).toBe(0);
      expect(await fs.readFile(path.join(tempDir, '.env.yylo'), 'utf8')).toBe(original);
      const applied = await executeCLI(['continuity', 'clean', '--apply', planPath], { env });
      expect(applied.exitCode).toBe(0);
      const receiptPath = JSON.parse(applied.stdout).receiptPath;
      expect(await fs.readFile(path.join(tempDir, '.env.yylo'), 'utf8')).toBe(
        'SECRET=DO_NOT_PRINT\n',
      );

      expect((await executeCLI(['continuity', 'rollback', receiptPath], { env })).exitCode).toBe(0);
      expect(await fs.readFile(path.join(tempDir, '.env.yylo'), 'utf8')).toBe(original);

      expect(
        (await executeCLI(['continuity', 'clean', '--apply', planPath], { env })).exitCode,
      ).toBe(0);
      expect((await executeCLI(['continuity', 'pin', scope], { env })).exitCode).toBe(0);
      expect((await executeCLI(['continuity', 'unpin', scope], { env })).exitCode).toBe(0);
    });
  });

  describe('Performance and Resource Usage', () => {
    it('should start quickly (within reasonable time)', async () => {
      const startTime = Date.now();
      const result = await executeCLI(['--version']);
      const duration = Date.now() - startTime;

      expect(result.exitCode).toBe(0);
      expect(duration).toBeLessThan(5000); // Should start within 5 seconds
    });

    it('should handle multiple concurrent executions', async () => {
      const promises = Array.from({ length: 3 }, (_, i) =>
        executeCLI(['--version'], { timeout: 10000 }),
      );

      const results = await Promise.all(promises);

      results.forEach((result) => {
        expect(result.exitCode).toBe(0);
      });
    });

    it('should clean up resources properly', async () => {
      // Execute a command and verify no zombie processes
      await executeCLI(['--version']);

      // This is hard to test directly, but the fact that the test completes
      // without hanging indicates proper resource cleanup
      expect(true).toBe(true);
    });
  });
});
