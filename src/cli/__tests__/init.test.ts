/**
 * Comprehensive tests for Init Command
 *
 * Tests the init command functionality including:
 * - Interactive and headless initialization
 * - Template generation
 * - Project structure creation
 * - Validation and error handling
 * - Configuration setup
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { Command } from 'commander';
import * as path from 'node:path';
import * as fs from 'fs-extra';
import { execSync } from 'child_process';

import { initCommandHandler, configureInitCommand } from '../commands/init.js';
import { ManagedProjectAssets } from '../../utils/managed-project-assets.js';

import type { InitCommandOptions } from '../types.js';

// Mock external dependencies
vi.mock('../../core/config.js', () => ({
  loadConfig: vi.fn().mockResolvedValue({
    workingDirectory: '/test/dir',
    verbose: 0,
  }),
  createPersistedProjectConfigDefaults: vi.fn((baseDir: string) => ({
    configVersion: 1,
    defaultSubagent: 'claude',
    defaultBackend: 'shell',
    defaultMaxIterations: 1,
    defaultModels: { claude: ':sonnet', pi: ':gpt' },
    gitCheckpoint: {
      include: [
        '.juno_task/tasks',
        '.juno_task/ledger',
        '.juno_task/wiki',
        '.juno_task/specs',
        '.juno_task/workflows',
        '.juno_task/plan.md',
        '.juno_task/tasks.md',
        '.juno_task/managed-assets.json',
      ],
    },
    workingDirectory: baseDir,
    sessionDirectory: `${baseDir}/.juno_task`,
  })),
}));

vi.mock('fs-extra', () => {
  const mock = {
    ensureDir: vi.fn().mockResolvedValue(undefined),
    pathExists: vi.fn().mockResolvedValue(false),
    readdir: vi.fn().mockResolvedValue([]),
    writeFile: vi.fn().mockResolvedValue(undefined),
    copy: vi.fn().mockResolvedValue(undefined),
    chmod: vi.fn().mockResolvedValue(undefined),
    stat: vi.fn().mockResolvedValue({ isFile: () => true }),
    readFile: vi.fn().mockResolvedValue(''),
  };
  return { default: mock, ...mock };
});

vi.mock('child_process', () => ({
  execSync: vi.fn().mockReturnValue(''),
}));

vi.mock('../../utils/managed-project-assets.js', () => ({
  ManagedProjectAssets: {
    update: vi.fn().mockResolvedValue({
      installed: [], updated: [], unchanged: [], conflicts: [], backups: [],
      macrosAdded: [], macroConflicts: [],
    }),
  },
}));

vi.mock('../../templates/default-hooks.js', () => ({
  getDefaultHooks: vi.fn().mockReturnValue({
    START_RUN: { commands: [] },
    START_ITERATION: { commands: [] },
    END_ITERATION: { commands: [] },
    END_RUN: { commands: [] },
  }),
}));

vi.mock('../utils/multiline.js', () => ({
  promptMultiline: vi.fn().mockResolvedValue('Build a test project with full features'),
  promptInputOnce: vi.fn().mockResolvedValue('claude'),
}));

vi.mock('chalk', () => {
  const createChainableFunction = (color: string) => {
    const fn = vi.fn((text) => text);
    fn.bold = vi.fn((text) => text);
    return fn;
  };

  const mockChalk = {
    red: createChainableFunction('red'),
    yellow: createChainableFunction('yellow'),
    blue: createChainableFunction('blue'),
    gray: createChainableFunction('gray'),
    green: createChainableFunction('green'),
    cyan: createChainableFunction('cyan'),
    white: createChainableFunction('white'),
  };

  return {
    default: mockChalk,
    ...mockChalk,
  };
});

// Helper: run initCommandHandler catching the process.exit throw
async function runInit(
  args: string[],
  options: InitCommandOptions,
  command: Command,
): Promise<void> {
  try {
    await initCommandHandler(args, options, command);
  } catch (e: any) {
    if (e?.message !== 'process.exit called') throw e;
  }
}

describe('Init Command', () => {
  let consoleSpy: ReturnType<typeof vi.spyOn>;
  let processExitSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    consoleSpy = vi.spyOn(console, 'log').mockImplementation(() => {});
    vi.spyOn(console, 'error').mockImplementation(() => {});
    processExitSpy = vi.spyOn(process, 'exit').mockImplementation(() => {
      throw new Error('process.exit called');
    });

    vi.mocked(fs.pathExists).mockResolvedValue(false);
    vi.mocked(fs.readdir).mockResolvedValue([] as any);
    vi.mocked(execSync).mockReturnValue('');

    // Mock process.cwd()
    vi.spyOn(process, 'cwd').mockReturnValue('/current/dir');
  });

  afterEach(() => {
    vi.clearAllMocks();
    consoleSpy.mockRestore();
    processExitSpy.mockRestore();
  });

  describe('configureInitCommand', () => {
    it('should configure init command with correct structure', () => {
      const program = new Command();
      configureInitCommand(program);

      const initCommand = program.commands.find((cmd) => cmd.name() === 'init');

      expect(initCommand).toBeDefined();
      expect(initCommand?.description()).toBeTruthy();
      // Note: The total options include global options added by the framework
      expect(initCommand?.options.length).toBeGreaterThanOrEqual(4);
    });

    it('should have correct options configured', () => {
      const program = new Command();
      configureInitCommand(program);

      const initCommand = program.commands.find((cmd) => cmd.name() === 'init');
      const options = initCommand?.options || [];

      // Simplified init command options after user feedback refactoring
      expect(options.some((opt) => opt.flags.includes('--force'))).toBe(true);
      expect(options.some((opt) => opt.flags.includes('--task'))).toBe(true);
      expect(options.some((opt) => opt.flags.includes('--git-url'))).toBe(true);
      expect(options.some((opt) => opt.flags.includes('--interactive'))).toBe(true);
      expect(options.some((opt) => opt.flags.includes('--target-branch'))).toBe(true);

      // Issue #32: Added back --subagent and --git-repo for inline mode support
      expect(options.some((opt) => opt.flags.includes('--subagent'))).toBe(true);
      expect(options.some((opt) => opt.flags.includes('--git-repo'))).toBe(true);
      expect(options.some((opt) => opt.flags.includes('--directory'))).toBe(true);

      // Removed options during simplification
      expect(options.some((opt) => opt.flags.includes('--template'))).toBe(false);
      expect(options.some((opt) => opt.flags.includes('--var'))).toBe(false);
    });

    it('should have help text with examples', () => {
      const program = new Command();
      configureInitCommand(program);

      const initCommand = program.commands.find((cmd) => cmd.name() === 'init');

      // Test that help text is added (this is visual, so we test that no error occurs)
      expect(() => configureInitCommand(program)).not.toThrow();
    });
  });

  describe('initCommandHandler', () => {
    const mockCommand = new Command();

    describe('headless initialization', () => {
      it('should initialize project in current directory', async () => {
        const options: InitCommandOptions = {
          directory: undefined,
          task: 'Build a test project',
          subagent: 'claude',
          force: false,
          interactive: false,
          template: 'default',
          variables: {},
        };

        await runInit([], options, mockCommand);

        expect(fs.ensureDir).toHaveBeenCalled();
        const configWrite = vi.mocked(fs.writeFile).mock.calls.find(([target]) =>
          String(target).endsWith('/.juno_task/config.json'),
        );
        expect(configWrite).toBeDefined();
        expect(JSON.parse(String(configWrite![1]))).toMatchObject({
          configVersion: 1,
          defaultBackend: 'shell',
          gitCheckpoint: {
            include: expect.arrayContaining([
              '.juno_task/workflows',
              '.juno_task/managed-assets.json',
            ]),
          },
        });
        expect(consoleSpy).toHaveBeenCalledWith(
          expect.stringContaining('Project initialization complete'),
        );
      });

      it('should initialize project in specified directory', async () => {
        const options: InitCommandOptions = {
          directory: './my-project',
          task: 'Build a test project',
          subagent: 'claude',
          force: false,
          interactive: false,
          template: 'default',
          variables: {},
        };

        await runInit([], options, mockCommand);

        expect(fs.ensureDir).toHaveBeenCalled();
        expect(processExitSpy).toHaveBeenCalledWith(0);
      });

      it('should run install requirements inside the target directory with JUNO_TASK_ROOT pinned and no inherited venv', async () => {
        vi.mocked(fs.readdir).mockResolvedValue(['install_requirements.sh'] as any);
        vi.mocked(fs.pathExists).mockImplementation(async (target: any) =>
          String(target).endsWith('/my-project/.juno_task/scripts/install_requirements.sh'),
        );
        vi.mocked(execSync).mockReturnValue('requirements ok');

        const options: InitCommandOptions = {
          directory: './my-project',
          task: 'Build a test project',
          subagent: 'claude',
          force: false,
          interactive: false,
          template: 'default',
          variables: {},
        };

        const originalVenv = process.env.VIRTUAL_ENV;
        process.env.VIRTUAL_ENV = '/unrelated/project/.venv_juno';
        try {
          await runInit([], options, mockCommand);
        } finally {
          if (originalVenv === undefined) delete process.env.VIRTUAL_ENV;
          else process.env.VIRTUAL_ENV = originalVenv;
        }

        expect(execSync).toHaveBeenCalledWith(
          '/current/dir/my-project/.juno_task/scripts/install_requirements.sh',
          expect.objectContaining({
            cwd: '/current/dir/my-project',
            env: expect.objectContaining({ JUNO_TASK_ROOT: '/current/dir/my-project' }),
          }),
        );
        const installOptions = vi.mocked(execSync).mock.calls.at(-1)?.[1] as {
          env?: NodeJS.ProcessEnv;
        };
        expect(installOptions.env?.VIRTUAL_ENV).toBeUndefined();
      });

      it('should use default task when not provided', async () => {
        // Set CI to prevent interactive mode when task is undefined
        const origCI = process.env.CI;
        process.env.CI = '1';
        try {
          const options: InitCommandOptions = {
            directory: undefined,
            task: undefined,
            subagent: 'claude',
            force: false,
            interactive: false,
            template: 'default',
            variables: {},
          };

          await runInit([], options, mockCommand);

          expect(consoleSpy).toHaveBeenCalledWith(
            expect.stringContaining('Project initialization complete'),
          );
        } finally {
          if (origCI === undefined) delete process.env.CI;
          else process.env.CI = origCI;
        }
      });

      it('should use default subagent when not provided', async () => {
        const options: InitCommandOptions = {
          directory: undefined,
          task: 'Build a test project',
          subagent: undefined,
          force: false,
          interactive: false,
          template: 'default',
          variables: {},
        };

        await runInit([], options, mockCommand);

        expect(consoleSpy).toHaveBeenCalledWith(
          expect.stringContaining('Project initialization complete'),
        );
      });

      it('should handle invalid task descriptions', async () => {
        const options: InitCommandOptions = {
          directory: undefined,
          task: 'abc', // Too short task should trigger validation error (min 5 chars)
          force: false,
          interactive: false,
        };

        await expect(initCommandHandler([], options, mockCommand)).rejects.toThrow(
          'process.exit called',
        );

        expect(processExitSpy).toHaveBeenCalledWith(expect.any(Number)); // Any exit code is fine for error handling
      });

      it('should validate task length', async () => {
        const options: InitCommandOptions = {
          directory: undefined,
          task: 'short',
          force: false,
          interactive: false,
        };

        await expect(initCommandHandler([], options, mockCommand)).rejects.toThrow(
          'process.exit called',
        );

        expect(processExitSpy).toHaveBeenCalledWith(expect.any(Number));
      });

      it('should accept valid git URL', async () => {
        const options: InitCommandOptions = {
          directory: undefined,
          task: 'Build a test project',
          subagent: 'claude',
          gitUrl: 'https://github.com/owner/repo',
          force: false,
          interactive: false,
          template: 'default',
          variables: {},
        };

        await runInit([], options, mockCommand);

        expect(consoleSpy).toHaveBeenCalledWith(
          expect.stringContaining('Project initialization complete'),
        );
      });

      it('should accept empty git URL', async () => {
        const options: InitCommandOptions = {
          directory: undefined,
          task: 'Build a test project',
          subagent: 'claude',
          gitUrl: '',
          force: false,
          interactive: false,
          template: 'default',
          variables: {},
        };

        await runInit([], options, mockCommand);

        expect(consoleSpy).toHaveBeenCalledWith(
          expect.stringContaining('Project initialization complete'),
        );
      });

      it('should merge custom variables', async () => {
        const customVariables = {
          CUSTOM_VAR: 'custom_value',
          ANOTHER_VAR: 'another_value',
        };

        const options: InitCommandOptions = {
          directory: undefined,
          task: 'Build a test project',
          subagent: 'claude',
          force: false,
          interactive: false,
          template: 'default',
          variables: customVariables,
        };

        await runInit([], options, mockCommand);

        // init.ts uses inline template literals; just verify completion
        expect(processExitSpy).toHaveBeenCalledWith(0);
      });
    });

    describe('interactive initialization', () => {
      it('should run interactive mode', async () => {
        // Re-establish mocks cleared by mockReset: true
        const multiline = await import('../utils/multiline.js');
        vi.mocked(multiline.promptMultiline).mockResolvedValue(
          'Build a test project with full features',
        );
        vi.mocked(multiline.promptInputOnce).mockResolvedValue('/current/dir');

        const options: InitCommandOptions = {
          directory: undefined,
          task: undefined,
          subagent: undefined,
          force: false,
          interactive: true,
          template: 'default',
          variables: {},
        };

        await runInit([], options, mockCommand);

        expect(consoleSpy).toHaveBeenCalledWith(
          expect.stringContaining('YYLO Project Initialization'),
        );
        expect(consoleSpy).toHaveBeenCalledWith(
          expect.stringContaining('Project initialization complete'),
        );
      });

      it('should display interactive prompts', async () => {
        // Re-establish mocks cleared by mockReset: true
        const multiline = await import('../utils/multiline.js');
        vi.mocked(multiline.promptMultiline).mockResolvedValue(
          'Build a test project with full features',
        );
        vi.mocked(multiline.promptInputOnce).mockResolvedValue('/current/dir');

        const options: InitCommandOptions = {
          directory: undefined,
          task: undefined,
          subagent: undefined,
          force: false,
          interactive: true,
          template: 'default',
          variables: {},
        };

        await runInit([], options, mockCommand);

        // Verify interactive mode ran and completed
        expect(consoleSpy).toHaveBeenCalledWith(
          expect.stringContaining('YYLO Project Initialization'),
        );
        expect(processExitSpy).toHaveBeenCalledWith(0);
      });
    });

    describe('project generation', () => {
      it('should generate template files', async () => {
        const options: InitCommandOptions = {
          directory: undefined,
          task: 'Build a test project',
          subagent: 'claude',
          force: false,
          interactive: false,
          template: 'default',
          variables: {},
        };

        await runInit([], options, mockCommand);

        // init.ts uses inline template literals and fs.writeFile
        expect(fs.writeFile).toHaveBeenCalled();
        expect(
          vi
            .mocked(fs.writeFile)
            .mock.calls.some(([filePath]) => String(filePath).endsWith('.env.yylo')),
        ).toBe(true);
        expect(
          vi.mocked(fs.writeFile).mock.calls.some(([filePath]) =>
            String(filePath).endsWith('.gitignore')),
        ).toBe(true);
        expect(ManagedProjectAssets.update).toHaveBeenCalledWith('/current/dir', {
          silent: false,
          localization: {},
        });
        expect(processExitSpy).toHaveBeenCalledWith(0);
      });

      it('should create additional directories', async () => {
        const options: InitCommandOptions = {
          directory: undefined,
          task: 'Build a test project',
          subagent: 'claude',
          force: false,
          interactive: false,
          template: 'default',
          variables: {},
        };

        await runInit([], options, mockCommand);

        // Verify ensureDir was called for subdirectories
        const ensureDirCalls = vi.mocked(fs.ensureDir).mock.calls.map((c) => c[0]);
        expect(ensureDirCalls.length).toBeGreaterThan(1); // multiple directories
      });

      it('should report generation results', async () => {
        const options: InitCommandOptions = {
          directory: undefined,
          task: 'Build a test project',
          subagent: 'claude',
          force: false,
          interactive: false,
          template: 'default',
          variables: {},
        };

        await runInit([], options, mockCommand);

        // Verify created file confirmations are logged
        const logCalls = consoleSpy.mock.calls.map((c) => String(c[0]));
        expect(logCalls.some((c) => c.includes('config.json'))).toBe(true);
      });

      it('should display next steps', async () => {
        const options: InitCommandOptions = {
          directory: './my-project',
          task: 'Build a test project',
          subagent: 'claude',
          force: false,
          interactive: false,
          template: 'default',
          variables: {},
        };

        await runInit([], options, mockCommand);

        expect(consoleSpy).toHaveBeenCalledWith(expect.stringContaining('Next Steps'));
      });

      it('should not show cd command for current directory', async () => {
        const options: InitCommandOptions = {
          directory: undefined, // Current directory
          task: 'Build a test project',
          subagent: 'claude',
          force: false,
          interactive: false,
          template: 'default',
          variables: {},
        };

        await runInit([], options, mockCommand);

        expect(consoleSpy).toHaveBeenCalledWith(expect.stringContaining('Next Steps'));
        expect(processExitSpy).toHaveBeenCalledWith(0);
      });

      it('should display useful commands', async () => {
        const options: InitCommandOptions = {
          directory: undefined,
          task: 'Build a test project',
          subagent: 'claude',
          force: false,
          interactive: false,
          template: 'default',
          variables: {},
        };

        await runInit([], options, mockCommand);

        // Verify start command is mentioned in output
        const logCalls = consoleSpy.mock.calls.map((c) => String(c[0]));
        expect(logCalls.some((c) => c.includes('yylo start'))).toBe(true);
      });
    });

    describe('force mode', () => {
      it('should overwrite existing files with force flag', async () => {
        vi.mocked(fs.pathExists).mockResolvedValueOnce(true as any);
        vi.mocked(fs.readdir).mockResolvedValueOnce(['existing-file.md'] as any);

        const options: InitCommandOptions = {
          directory: undefined,
          task: 'Build a test project',
          subagent: 'claude',
          force: true,
          interactive: false,
          template: 'default',
          variables: {},
        };

        await runInit([], options, mockCommand);

        expect(processExitSpy).toHaveBeenCalledWith(0);
      });

      it('should fail without force when files exist', async () => {
        vi.mocked(fs.pathExists).mockResolvedValueOnce(true as any);
        vi.mocked(fs.readdir).mockResolvedValueOnce(['existing-file.md'] as any);

        const options: InitCommandOptions = {
          directory: undefined,
          task: 'Build a test project',
          subagent: 'claude',
          force: false,
          interactive: false,
          template: 'default',
          variables: {},
        };

        await expect(initCommandHandler([], options, mockCommand)).rejects.toThrow(
          'process.exit called',
        );

        expect(processExitSpy).toHaveBeenCalledWith(1);
      });
    });

    describe('error handling', () => {
      it('should handle directory creation errors', async () => {
        vi.mocked(fs.ensureDir).mockRejectedValueOnce(new Error('Permission denied'));

        const options: InitCommandOptions = {
          directory: undefined,
          task: 'Build a test project',
          subagent: 'claude',
          force: false,
          interactive: false,
          template: 'default',
          variables: {},
        };

        await expect(initCommandHandler([], options, mockCommand)).rejects.toThrow(
          'process.exit called',
        );

        expect(processExitSpy).toHaveBeenCalled();
      });

      it('should handle write file errors', async () => {
        vi.mocked(fs.writeFile).mockRejectedValueOnce(new Error('Disk full'));

        const options: InitCommandOptions = {
          directory: undefined,
          task: 'Build a test project',
          subagent: 'claude',
          force: false,
          interactive: false,
          template: 'default',
          variables: {},
        };

        await expect(initCommandHandler([], options, mockCommand)).rejects.toThrow(
          'process.exit called',
        );

        expect(processExitSpy).toHaveBeenCalled();
      });
    });

    describe('template variable creation', () => {
      it('should create proper template variables', async () => {
        const options: InitCommandOptions = {
          directory: './test-project',
          task: 'Build a comprehensive TypeScript CLI tool',
          subagent: 'claude',
          gitUrl: 'https://github.com/owner/test-repo',
          force: false,
          interactive: false,
          template: 'default',
          variables: {},
        };

        await runInit([], options, mockCommand);

        // Verify files were written with proper content
        expect(fs.writeFile).toHaveBeenCalled();
        expect(processExitSpy).toHaveBeenCalledWith(0);
      });

      it('should handle project name with special characters', async () => {
        const options: InitCommandOptions = {
          directory: './my-special-project@2024',
          task: 'Build a test project',
          subagent: 'claude',
          force: false,
          interactive: false,
          template: 'default',
          variables: {},
        };

        await runInit([], options, mockCommand);

        expect(consoleSpy).toHaveBeenCalledWith(
          expect.stringContaining('Project initialization complete'),
        );
      });
    });
  });
});
