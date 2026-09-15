/**
 * Tests for Hook system utility functions
 */

import { describe, it, expect, beforeEach, afterEach, vi, type MockedFunction } from 'vitest';
import { join } from 'path';
import { tmpdir } from 'os';
import { execFileSync } from 'node:child_process';
import fs from 'fs-extra';
import { execa } from 'execa';
import {
  executeHook,
  executeHooks,
  resolveHookWorkingDirectory,
  validateHooksConfig,
  type HookType,
  type HooksConfig,
  type HookExecutionContext,
  type HookExecutionOptions,
  type HookExecutionResult,
  type CommandExecutionResult,
} from '../hooks.js';

const mockContextLogger = vi.hoisted(() => ({
  info: vi.fn(),
  debug: vi.fn(),
  warn: vi.fn(),
  error: vi.fn(),
}));

// Mock dependencies
vi.mock('execa');
vi.mock('../../cli/utils/advanced-logger.js', () => ({
  logger: {
    child: vi.fn(() => mockContextLogger),
  },
  LogContext: {
    SYSTEM: 'SYSTEM',
    CLI: 'CLI',
    MCP: 'MCP',
    ENGINE: 'ENGINE',
    SESSION: 'SESSION',
    TEMPLATE: 'TEMPLATE',
    CONFIG: 'CONFIG',
    PERFORMANCE: 'PERFORMANCE',
  },
  LogLevel: {
    TRACE: 0,
    DEBUG: 1,
    INFO: 2,
    WARN: 3,
    ERROR: 4,
    FATAL: 5,
  },
}));

const mockedExeca = vi.mocked(execa);

describe('hooks', () => {
  let testDir: string;

  beforeEach(async () => {
    // Create unique test directory for each test
    testDir = join(tmpdir(), `hooks-test-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`);
    await fs.ensureDir(testDir);

    // Reset all mocks
    vi.clearAllMocks();
  });

  it('runs sparse-controller hooks from the registered product surface', async () => {
    const repository = join(testDir, 'repository');
    const controller = join(testDir, 'controller');
    const owner = join(testDir, 'integration owner');
    const git = (cwd: string, ...args: string[]) => execFileSync('git', ['-C', cwd, ...args]);
    await fs.ensureDir(repository);
    git(repository, 'init', '-q', '-b', 'product');
    git(repository, 'config', 'user.name', 'Test');
    git(repository, 'config', 'user.email', 'test@example.invalid');
    await fs.writeFile(join(repository, 'README.md'), 'product\n');
    git(repository, 'add', '.'); git(repository, 'commit', '-qm', 'base');
    git(repository, 'branch', 'controller');
    git(repository, 'worktree', 'add', '-q', controller, 'controller');
    git(repository, 'switch', '-q', '--detach');
    git(repository, 'worktree', 'add', '-q', '--detach', owner, 'product');
    git(repository, 'config', 'extensions.worktreeConfig', 'true');
    git(controller, 'config', '--worktree', 'juno.workspace.role', 'controller');
    git(owner, 'config', '--worktree', 'juno.workspace.role', 'integration-owner');
    git(owner, 'config', '--worktree', 'juno.workspace.roleAuthority', 'protected-integration.v1');
    git(repository, 'config', 'juno.integration.ownerPath', owner);
    await fs.outputFile(join(owner, '.juno_task/scripts/cleanup_feedback.sh'), '#!/bin/sh\n');
    mockedExeca.mockResolvedValueOnce({ exitCode: 0, stdout: '', stderr: '' } as never);

    const result = await executeHook('START_ITERATION', {
      START_ITERATION: { commands: ['./.juno_task/scripts/cleanup_feedback.sh'] },
    }, { workingDirectory: controller });

    expect(result.success).toBe(true);
    expect(mockedExeca).toHaveBeenCalledWith('./.juno_task/scripts/cleanup_feedback.sh',
      expect.objectContaining({ cwd: await fs.realpath(owner), shell: true, input: '' }));
  });

  it('explicitly skips a registered hook surface from an unrelated repository', async () => {
    const controller = join(testDir, 'controller-repository');
    const unrelated = join(testDir, 'unrelated-owner');
    for (const repository of [controller, unrelated]) {
      await fs.ensureDir(repository);
      execFileSync('git', ['-C', repository, 'init', '-q']);
      execFileSync('git', ['-C', repository, 'config', 'extensions.worktreeConfig', 'true']);
    }
    execFileSync('git', ['-C', controller, 'config', '--worktree', 'juno.workspace.role', 'controller']);
    execFileSync('git', ['-C', controller, 'config', 'juno.integration.ownerPath', unrelated]);
    execFileSync('git', ['-C', unrelated, 'config', '--worktree', 'juno.workspace.role', 'integration-owner']);
    execFileSync('git', ['-C', unrelated, 'config', '--worktree', 'juno.workspace.roleAuthority', 'protected-integration.v1']);

    const result = await executeHook('START_RUN', {
      START_RUN: { commands: ['./product-owned-hook.sh'] },
    }, { workingDirectory: controller });

    expect(result).toMatchObject({ success: true, commandsExecuted: 0 });
    expect(mockedExeca).not.toHaveBeenCalled();
    expect(mockContextLogger.warn).toHaveBeenCalledWith(
      expect.stringContaining('canonical product surface is missing or invalid'),
    );
  });

  it('explicitly skips controller hooks when no canonical product surface exists', async () => {
    const controller = join(testDir, 'controller-only');
    await fs.ensureDir(controller);
    const git = (...args: string[]) => execFileSync('git', ['-C', controller, ...args]);
    git('init', '-q');
    git('config', 'extensions.worktreeConfig', 'true');
    git('config', '--worktree', 'juno.workspace.role', 'controller');

    const result = await executeHook('START_RUN', {
      START_RUN: { commands: ['./product-owned-hook.sh'] },
    }, { workingDirectory: controller });

    expect(result).toMatchObject({ success: true, commandsExecuted: 0 });
    expect(mockedExeca).not.toHaveBeenCalled();
    expect(mockContextLogger.warn).toHaveBeenCalledWith(
      expect.stringContaining('sparse controller has no canonical product surface'),
    );
  });

  afterEach(async () => {
    // Clean up test directory
    try {
      await fs.remove(testDir);
    } catch (error) {
      console.warn('Failed to clean up test directory:', error);
    }
  });

  describe('executeHook', () => {
    const mockHooks: HooksConfig = {
      START_ITERATION: {
        commands: ['echo "Starting iteration"', 'npm test'],
      },
      END_ITERATION: {
        commands: ['echo "Ending iteration"'],
      },
    };

    const mockContext: HookExecutionContext = {
      iteration: 1,
      sessionId: 'test-session-123',
      workingDirectory: testDir,
      metadata: { testKey: 'testValue' },
      runId: 'run-456',
      totalIterations: 5,
    };

    it('should execute hook successfully with all commands', async () => {
      // Mock successful command execution
      mockedExeca
        .mockResolvedValueOnce({
          exitCode: 0,
          stdout: 'Starting iteration',
          stderr: '',
          all: 'Starting iteration',
        } as any)
        .mockResolvedValueOnce({
          exitCode: 0,
          stdout: 'All tests passed',
          stderr: '',
          all: 'All tests passed',
        } as any);

      const result = await executeHook('START_ITERATION', mockHooks, mockContext);

      expect(result.success).toBe(true);
      expect(result.hookType).toBe('START_ITERATION');
      expect(result.commandsExecuted).toBe(2);
      expect(result.commandsFailed).toBe(0);
      expect(result.commandResults).toHaveLength(2);
      expect(result.totalDuration).toBeGreaterThanOrEqual(0);

      // Check first command result
      expect(result.commandResults[0].command).toBe('echo "Starting iteration"');
      expect(result.commandResults[0].success).toBe(true);
      expect(result.commandResults[0].exitCode).toBe(0);
      expect(result.commandResults[0].stdout).toBe('Starting iteration');

      // Check second command result
      expect(result.commandResults[1].command).toBe('npm test');
      expect(result.commandResults[1].success).toBe(true);
      expect(result.commandResults[1].exitCode).toBe(0);
      expect(result.commandResults[1].stdout).toBe('All tests passed');
    });

    it('should handle command with stdout and stderr output', async () => {
      const hooksWithOutput: HooksConfig = {
        START_RUN: {
          commands: ['echo "stdout" && echo "stderr" >&2'],
        },
      };

      mockedExeca.mockResolvedValueOnce({
        exitCode: 0,
        stdout: 'stdout',
        stderr: 'stderr',
        all: 'stdout\nstderr',
      } as any);

      const result = await executeHook('START_RUN', hooksWithOutput, mockContext);

      expect(result.success).toBe(true);
      expect(result.commandResults[0].stdout).toBe('stdout');
      expect(result.commandResults[0].stderr).toBe('stderr');
    });

    it('should handle command failure with non-zero exit code', async () => {
      const hooksWithFailure: HooksConfig = {
        END_RUN: {
          commands: ['exit 1', 'echo "should still run"'],
        },
      };

      mockedExeca
        .mockResolvedValueOnce({
          exitCode: 1,
          stdout: '',
          stderr: 'Command failed',
          all: 'Command failed',
        } as any)
        .mockResolvedValueOnce({
          exitCode: 0,
          stdout: 'should still run',
          stderr: '',
          all: 'should still run',
        } as any);

      const result = await executeHook('END_RUN', hooksWithFailure, mockContext);

      expect(result.success).toBe(false);
      expect(result.commandsExecuted).toBe(2);
      expect(result.commandsFailed).toBe(1);

      // First command should fail
      expect(result.commandResults[0].success).toBe(false);
      expect(result.commandResults[0].exitCode).toBe(1);

      // Second command should still run (continueOnError default is true)
      expect(result.commandResults[1].success).toBe(true);
      expect(result.commandResults[1].exitCode).toBe(0);
    });

    it('should log failed hook command, exit code, stderr, and stdout in the visible message', async () => {
      const hooksWithFailure: HooksConfig = {
        END_RUN: {
          commands: ['npm run failing-hook'],
        },
      };

      mockedExeca.mockResolvedValueOnce({
        exitCode: 42,
        stdout: 'stdout diagnostics',
        stderr: 'stderr diagnostics',
        all: 'stdout diagnostics\nstderr diagnostics',
      } as any);

      await executeHook('END_RUN', hooksWithFailure, mockContext);

      expect(mockContextLogger.error).toHaveBeenCalledWith(
        expect.stringContaining('Command failed: npm run failing-hook'),
        expect.any(Object),
      );
      expect(mockContextLogger.error).toHaveBeenCalledWith(
        expect.stringContaining('Exit code: 42'),
        expect.any(Object),
      );
      expect(mockContextLogger.error).toHaveBeenCalledWith(
        expect.stringContaining('stderr diagnostics'),
        expect.any(Object),
      );
      expect(mockContextLogger.error).toHaveBeenCalledWith(
        expect.stringContaining('stdout diagnostics'),
        expect.any(Object),
      );
    });

    it('should handle command timeout', async () => {
      const hooksWithTimeout: HooksConfig = {
        START_RUN: {
          commands: ['sleep 10'],
        },
      };

      const timeoutError = new Error('Command timed out');
      (timeoutError as any).timedOut = true;
      mockedExeca.mockRejectedValueOnce(timeoutError);

      const result = await executeHook('START_RUN', hooksWithTimeout, mockContext, {
        commandTimeout: 100,
      });

      expect(result.success).toBe(false);
      expect(result.commandsExecuted).toBe(1);
      expect(result.commandsFailed).toBe(1);
      expect(result.commandResults[0].success).toBe(false);
      expect(result.commandResults[0].exitCode).toBe(-1);
      expect(result.commandResults[0].error).toBeDefined();
      expect(mockContextLogger.error).toHaveBeenCalledWith(
        expect.stringContaining('Command failed: sleep 10'),
        expect.any(Object),
      );
      expect(mockContextLogger.error).toHaveBeenCalledWith(
        expect.stringContaining('Timeout: 100ms'),
        expect.any(Object),
      );
      expect(mockContextLogger.error).toHaveBeenCalledWith(
        expect.stringContaining('Error: Command timed out'),
        expect.any(Object),
      );
    });

    it('should return silently when hook is not defined', async () => {
      const result = await executeHook('START_RUN', {}, mockContext);

      expect(result.success).toBe(true);
      expect(result.commandsExecuted).toBe(0);
      expect(result.commandsFailed).toBe(0);
      expect(result.commandResults).toHaveLength(0);
      expect(mockedExeca).not.toHaveBeenCalled();
    });

    it('should return silently when commands array is empty', async () => {
      const emptyHooks: HooksConfig = {
        START_RUN: {
          commands: [],
        },
      };

      const result = await executeHook('START_RUN', emptyHooks, mockContext);

      expect(result.success).toBe(true);
      expect(result.commandsExecuted).toBe(0);
      expect(result.commandsFailed).toBe(0);
      expect(result.commandResults).toHaveLength(0);
      expect(mockedExeca).not.toHaveBeenCalled();
    });

    it('should execute multiple commands in sequence', async () => {
      const sequentialHooks: HooksConfig = {
        START_ITERATION: {
          commands: ['echo "first"', 'echo "second"', 'echo "third"'],
        },
      };

      mockedExeca
        .mockResolvedValueOnce({
          exitCode: 0,
          stdout: 'first',
          stderr: '',
          all: 'first',
        } as any)
        .mockResolvedValueOnce({
          exitCode: 0,
          stdout: 'second',
          stderr: '',
          all: 'second',
        } as any)
        .mockResolvedValueOnce({
          exitCode: 0,
          stdout: 'third',
          stderr: '',
          all: 'third',
        } as any);

      const result = await executeHook('START_ITERATION', sequentialHooks, mockContext);

      expect(result.success).toBe(true);
      expect(result.commandsExecuted).toBe(3);
      expect(mockedExeca).toHaveBeenCalledTimes(3);

      // Verify commands were called in order
      expect(mockedExeca).toHaveBeenNthCalledWith(
        1,
        'echo "first"',
        expect.objectContaining({
          shell: true,
        }),
      );
      expect(mockedExeca).toHaveBeenNthCalledWith(
        2,
        'echo "second"',
        expect.objectContaining({
          shell: true,
        }),
      );
      expect(mockedExeca).toHaveBeenNthCalledWith(
        3,
        'echo "third"',
        expect.objectContaining({
          shell: true,
        }),
      );
    });

    it('should inject environment variables from context', async () => {
      const envHooks: HooksConfig = {
        START_ITERATION: {
          commands: ['echo $HOOK_TYPE $ITERATION $SESSION_ID'],
        },
      };

      mockedExeca.mockResolvedValueOnce({
        exitCode: 0,
        stdout: 'START_ITERATION 1 test-session-123',
        stderr: '',
        all: 'START_ITERATION 1 test-session-123',
      } as any);

      await executeHook('START_ITERATION', envHooks, mockContext);

      expect(mockedExeca).toHaveBeenCalledWith(
        'echo $HOOK_TYPE $ITERATION $SESSION_ID',
        expect.objectContaining({
          shell: true,
          env: expect.objectContaining({
            HOOK_TYPE: 'START_ITERATION',
            ITERATION: '1',
            SESSION_ID: 'test-session-123',
            RUN_ID: 'run-456',
            TOTAL_ITERATIONS: '5',
            JUNO_TESTKEY: 'testValue', // metadata prefixed with JUNO_
          }),
        }),
      );
    });

    it('should use custom environment variables from options', async () => {
      const customEnvHooks: HooksConfig = {
        START_RUN: {
          commands: ['echo $CUSTOM_VAR'],
        },
      };

      mockedExeca.mockResolvedValueOnce({
        exitCode: 0,
        stdout: 'custom-value',
        stderr: '',
        all: 'custom-value',
      } as any);

      await executeHook('START_RUN', customEnvHooks, mockContext, {
        env: {
          CUSTOM_VAR: 'custom-value',
          YYLO_LAST_SESSION_ID_SCOPE_0123456789ABCDEF: 'historical-session',
          YYLO_LAST_EXECUTION_SETTINGS: 'legacy-settings',
        },
      });

      expect(mockedExeca).toHaveBeenCalledWith(
        'echo $CUSTOM_VAR',
        expect.objectContaining({
          shell: true,
          env: expect.objectContaining({
            CUSTOM_VAR: 'custom-value',
          }),
        }),
      );
      const childEnvironment = mockedExeca.mock.calls[0]?.[1]?.env;
      expect(childEnvironment?.YYLO_LAST_SESSION_ID_SCOPE_0123456789ABCDEF).toBeUndefined();
      expect(childEnvironment?.YYLO_LAST_EXECUTION_SETTINGS).toBeUndefined();
    });

    it('should stop execution on error when continueOnError is false', async () => {
      const stopOnErrorHooks: HooksConfig = {
        END_ITERATION: {
          commands: ['exit 1', 'echo "should not run"'],
        },
      };

      mockedExeca.mockResolvedValueOnce({
        exitCode: 1,
        stdout: '',
        stderr: 'Command failed',
        all: 'Command failed',
      } as any);

      const result = await executeHook('END_ITERATION', stopOnErrorHooks, mockContext, {
        continueOnError: false,
      });

      expect(result.success).toBe(false);
      expect(result.commandsExecuted).toBe(1); // Should stop after first failure
      expect(result.commandsFailed).toBe(1);
      expect(mockedExeca).toHaveBeenCalledTimes(1);
    });

    it('should handle execution errors gracefully', async () => {
      const errorHooks: HooksConfig = {
        START_RUN: {
          commands: ['invalid-command-xyz'],
        },
      };

      const executionError = new Error('Command not found');
      mockedExeca.mockRejectedValueOnce(executionError);

      const result = await executeHook('START_RUN', errorHooks, mockContext);

      expect(result.success).toBe(false);
      expect(result.commandsExecuted).toBe(1);
      expect(result.commandsFailed).toBe(1);
      expect(result.commandResults[0].success).toBe(false);
      expect(result.commandResults[0].exitCode).toBe(-1);
      expect(result.commandResults[0].error).toBe(executionError);
    });

    it('should use custom command timeout', async () => {
      const timeoutHooks: HooksConfig = {
        START_RUN: {
          commands: ['echo "test"'],
        },
      };

      mockedExeca.mockResolvedValueOnce({
        exitCode: 0,
        stdout: 'test',
        stderr: '',
        all: 'test',
      } as any);

      await executeHook('START_RUN', timeoutHooks, mockContext, {
        commandTimeout: 5000,
      });

      expect(mockedExeca).toHaveBeenCalledWith(
        'echo "test"',
        expect.objectContaining({
          shell: true,
          timeout: 5000,
        }),
      );
    });

    it('should canonicalize the default working directory when not specified in context', async () => {
      const defaultWorkingDirectory = process.cwd();
      const canonicalWorkingDirectory = resolveHookWorkingDirectory(defaultWorkingDirectory).directory;
      expect(canonicalWorkingDirectory).toBeTruthy();
      const cwdSpy = vi.spyOn(process, 'cwd').mockReturnValue(defaultWorkingDirectory);
      const basicHooks: HooksConfig = {
        START_RUN: {
          commands: ['pwd'],
        },
      };

      mockedExeca.mockResolvedValueOnce({
        exitCode: 0,
        stdout: defaultWorkingDirectory,
        stderr: '',
        all: defaultWorkingDirectory,
      } as any);

      await executeHook('START_RUN', basicHooks, {});

      expect(mockedExeca).toHaveBeenCalledWith(
        'pwd',
        expect.objectContaining({
          shell: true,
          cwd: canonicalWorkingDirectory,
        }),
      );
      expect(cwdSpy).toHaveBeenCalledTimes(1);
    });
  });

  describe('executeHooks', () => {
    const batchHooks: HooksConfig = {
      START_RUN: {
        commands: ['echo "start"'],
      },
      START_ITERATION: {
        commands: ['echo "iteration"'],
      },
      END_ITERATION: {
        commands: ['echo "end iteration"'],
      },
      END_RUN: {
        commands: ['echo "end"'],
      },
    };

    it('should execute multiple hooks in sequence', async () => {
      mockedExeca
        .mockResolvedValueOnce({
          exitCode: 0,
          stdout: 'start',
          stderr: '',
          all: 'start',
        } as any)
        .mockResolvedValueOnce({
          exitCode: 0,
          stdout: 'iteration',
          stderr: '',
          all: 'iteration',
        } as any)
        .mockResolvedValueOnce({
          exitCode: 0,
          stdout: 'end',
          stderr: '',
          all: 'end',
        } as any);

      const hookTypes: HookType[] = ['START_RUN', 'START_ITERATION', 'END_RUN'];
      const results = await executeHooks(hookTypes, batchHooks);

      expect(results).toHaveLength(3);
      expect(results[0].hookType).toBe('START_RUN');
      expect(results[1].hookType).toBe('START_ITERATION');
      expect(results[2].hookType).toBe('END_RUN');
      expect(results.every((r) => r.success)).toBe(true);
    });

    it('should continue executing hooks even if one fails', async () => {
      mockedExeca
        .mockResolvedValueOnce({
          exitCode: 0,
          stdout: 'start',
          stderr: '',
          all: 'start',
        } as any)
        .mockResolvedValueOnce({
          exitCode: 1,
          stdout: '',
          stderr: 'iteration failed',
          all: 'iteration failed',
        } as any)
        .mockResolvedValueOnce({
          exitCode: 0,
          stdout: 'end',
          stderr: '',
          all: 'end',
        } as any);

      const hookTypes: HookType[] = ['START_RUN', 'START_ITERATION', 'END_RUN'];
      const results = await executeHooks(hookTypes, batchHooks);

      expect(results).toHaveLength(3);
      expect(results[0].success).toBe(true);
      expect(results[1].success).toBe(false); // Failed hook
      expect(results[2].success).toBe(true); // Should still execute
    });

    it('should handle empty hook types array', async () => {
      const results = await executeHooks([], batchHooks);

      expect(results).toHaveLength(0);
      expect(mockedExeca).not.toHaveBeenCalled();
    });
  });

  describe('validateHooksConfig', () => {
    it('should validate valid hooks configuration', () => {
      const validHooks: HooksConfig = {
        START_RUN: {
          commands: ['echo "starting"', 'npm install'],
        },
        END_ITERATION: {
          commands: ['npm test'],
        },
      };

      const result = validateHooksConfig(validHooks);

      expect(result.valid).toBe(true);
      expect(result.issues).toHaveLength(0);
      expect(result.warnings).toHaveLength(0);
    });

    it('should detect invalid hook types', () => {
      const invalidHooks = {
        INVALID_HOOK: {
          commands: ['echo "test"'],
        },
        START_RUN: {
          commands: ['echo "valid"'],
        },
      };

      const result = validateHooksConfig(invalidHooks);

      expect(result.valid).toBe(true); // Still valid, just warnings
      expect(result.issues).toHaveLength(0);
      expect(result.warnings).toContain(
        'Unknown hook type: INVALID_HOOK. Valid types are: START_RUN, START_ITERATION, END_ITERATION, END_RUN, ON_STALE',
      );
    });

    it('should detect missing commands array', () => {
      const hooksWithMissingCommands = {
        START_RUN: {
          // Missing commands array
        },
      };

      const result = validateHooksConfig(hooksWithMissingCommands as any);

      expect(result.valid).toBe(false);
      expect(result.issues).toContain("Hook START_RUN is missing 'commands' array");
    });

    it('should detect non-array commands field', () => {
      const hooksWithInvalidCommands = {
        START_RUN: {
          commands: 'not an array',
        },
      };

      const result = validateHooksConfig(hooksWithInvalidCommands as any);

      expect(result.valid).toBe(false);
      expect(result.issues).toContain("Hook START_RUN 'commands' must be an array");
    });

    it('should detect non-string commands', () => {
      const hooksWithInvalidCommandTypes = {
        START_RUN: {
          commands: ['valid command', 42, true, 'another valid'],
        },
      };

      const result = validateHooksConfig(hooksWithInvalidCommandTypes as any);

      expect(result.valid).toBe(false);
      expect(result.issues).toContain('Hook START_RUN command 1 must be a string, got number');
      expect(result.issues).toContain('Hook START_RUN command 2 must be a string, got boolean');
    });

    it('should warn about empty commands', () => {
      const hooksWithEmptyCommands: HooksConfig = {
        START_RUN: {
          commands: ['echo "valid"', '', '   ', 'echo "also valid"'],
        },
      };

      const result = validateHooksConfig(hooksWithEmptyCommands);

      expect(result.valid).toBe(true);
      expect(result.warnings).toContain('Hook START_RUN command 1 is empty');
      expect(result.warnings).toContain('Hook START_RUN command 2 is empty');
    });

    it('should warn about dangerous commands', () => {
      const dangerousHooks: HooksConfig = {
        START_RUN: {
          commands: [
            'rm -rf /',
            'sudo rm -rf /important',
            'format c:',
            'del /s /q',
            'echo "safe command"',
          ],
        },
      };

      const result = validateHooksConfig(dangerousHooks);

      expect(result.valid).toBe(true);
      expect(result.warnings.some((w) => w.includes('rm -rf /'))).toBe(true);
      expect(result.warnings.some((w) => w.includes('sudo rm -rf /important'))).toBe(true);
      expect(result.warnings.some((w) => w.includes('format c:'))).toBe(true);
      expect(result.warnings.some((w) => w.includes('del /s /q'))).toBe(true);
    });

    it('should handle empty hooks configuration', () => {
      const result = validateHooksConfig({});

      expect(result.valid).toBe(true);
      expect(result.issues).toHaveLength(0);
      expect(result.warnings).toHaveLength(0);
    });

    it('should validate all hook types at once', () => {
      const completeHooks: HooksConfig = {
        START_RUN: {
          commands: ['echo "start run"'],
        },
        START_ITERATION: {
          commands: ['echo "start iteration"'],
        },
        END_ITERATION: {
          commands: ['echo "end iteration"'],
        },
        END_RUN: {
          commands: ['echo "end run"'],
        },
      };

      const result = validateHooksConfig(completeHooks);

      expect(result.valid).toBe(true);
      expect(result.issues).toHaveLength(0);
      expect(result.warnings).toHaveLength(0);
    });
  });

  describe('error handling and logging', () => {
    it('should not throw exceptions on command failure', async () => {
      const failingHooks: HooksConfig = {
        START_RUN: {
          commands: ['exit 1'],
        },
      };

      mockedExeca.mockResolvedValueOnce({
        exitCode: 1,
        stdout: '',
        stderr: 'Command failed',
        all: 'Command failed',
      } as any);

      // Should not throw
      const result = await executeHook('START_RUN', failingHooks);

      expect(result.success).toBe(false);
      expect(result.commandResults[0].success).toBe(false);
    });

    it('should continue execution after command failure', async () => {
      const mixedHooks: HooksConfig = {
        START_ITERATION: {
          commands: ['exit 1', 'echo "continued"', 'exit 2'],
        },
      };

      mockedExeca
        .mockResolvedValueOnce({
          exitCode: 1,
          stdout: '',
          stderr: 'First failure',
          all: 'First failure',
        } as any)
        .mockResolvedValueOnce({
          exitCode: 0,
          stdout: 'continued',
          stderr: '',
          all: 'continued',
        } as any)
        .mockResolvedValueOnce({
          exitCode: 2,
          stdout: '',
          stderr: 'Second failure',
          all: 'Second failure',
        } as any);

      const result = await executeHook('START_ITERATION', mixedHooks);

      expect(result.commandsExecuted).toBe(3);
      expect(result.commandsFailed).toBe(2);
      expect(result.success).toBe(false);

      // All commands should have been executed
      expect(result.commandResults[0].exitCode).toBe(1);
      expect(result.commandResults[1].exitCode).toBe(0);
      expect(result.commandResults[2].exitCode).toBe(2);
    });

    it('should log execution details', async () => {
      const testHooks: HooksConfig = {
        START_RUN: {
          commands: ['echo "test"'],
        },
      };

      mockedExeca.mockResolvedValueOnce({
        exitCode: 0,
        stdout: 'test',
        stderr: '',
        all: 'test',
      } as any);

      await executeHook('START_RUN', testHooks);

      // Logger should have been called with appropriate context
      const { logger } = await import('../../cli/utils/advanced-logger.js');
      expect(logger.child).toHaveBeenCalled();
    });
  });

  describe('auto-migration functionality', () => {
    it('should create config.json when missing', async () => {
      const { loadConfig } = await import('../../core/config.js');

      // Ensure clean test directory
      const configDir = join(testDir, '.juno_task');
      const configPath = join(configDir, 'config.json');

      expect(await fs.pathExists(configPath)).toBe(false);

      // Call loadConfig which should trigger ensureHooksConfig internally
      await loadConfig({ baseDir: testDir });

      // Check that config was created
      expect(await fs.pathExists(configPath)).toBe(true);

      const config = await fs.readJson(configPath);
      // Default hooks do not install or upgrade dependencies at agent startup.
      expect(config.hooks).toMatchObject({
        START_RUN: { commands: [] },
        START_ITERATION: {
          commands: expect.arrayContaining([
            expect.stringContaining('CLAUDE.md'),
            expect.stringContaining('AGENTS.md'),
            expect.stringContaining('--reject-duplicates'),
          ]),
        },
      });
    });

    it('should add hooks field to existing config', async () => {
      const { loadConfig } = await import('../../core/config.js');

      const configDir = join(testDir, '.juno_task');
      const configPath = join(configDir, 'config.json');

      // Create existing config without hooks field
      await fs.ensureDir(configDir);
      await fs.writeJson(configPath, {
        defaultSubagent: 'claude',
        logLevel: 'info',
        verbose: 0,
      });

      await loadConfig({ baseDir: testDir });

      const config = await fs.readJson(configPath);
      // Default hooks do not install or upgrade dependencies at agent startup.
      expect(config.hooks).toMatchObject({
        START_RUN: { commands: [] },
        START_ITERATION: {
          commands: expect.arrayContaining([
            expect.stringContaining('CLAUDE.md'),
            expect.stringContaining('AGENTS.md'),
            expect.stringContaining('--reject-duplicates'),
          ]),
        },
      });
      expect(config.defaultSubagent).toBe('claude'); // Preserve existing config
      expect(config.logLevel).toBe('info');
    });

    it('should preserve an existing hooks section without injecting commands', async () => {
      const { loadConfig } = await import('../../core/config.js');

      const configDir = join(testDir, '.juno_task');
      const configPath = join(configDir, 'config.json');

      await fs.ensureDir(configDir);
      await fs.writeJson(configPath, {
        defaultSubagent: 'claude',
        hooks: {
          START_RUN: {
            commands: ['echo "existing"'],
          },
        },
        promptMacros: {
          local: { ship: 'run tests' },
        },
      });

      await loadConfig({ baseDir: testDir });

      const config = await fs.readJson(configPath);
      expect(config.hooks.START_RUN.commands).toEqual(['echo "existing"']);
      expect(config.promptMacros.local.ship).toBe('run tests');
    });

    it('should preserve explicitly empty START_RUN commands', async () => {
      const { loadConfig } = await import('../../core/config.js');

      const configDir = join(testDir, '.juno_task');
      const configPath = join(configDir, 'config.json');

      await fs.ensureDir(configDir);
      await fs.writeJson(configPath, {
        defaultSubagent: 'claude',
        hooks: { START_RUN: { commands: [] } },
      });

      await loadConfig({ baseDir: testDir });

      const config = await fs.readJson(configPath);
      expect(config.hooks.START_RUN.commands).toEqual([]);
    });

    it('should not duplicate existing dependency updater command variants', async () => {
      const { loadConfig } = await import('../../core/config.js');

      const configDir = join(testDir, '.juno_task');
      const configPath = join(configDir, 'config.json');
      const existingCommand = 'bash ./.juno_task/scripts/install_requirements.sh --force-update';

      await fs.ensureDir(configDir);
      await fs.writeJson(configPath, {
        defaultSubagent: 'claude',
        hooks: { START_RUN: { commands: ['echo before', existingCommand] } },
      });

      await loadConfig({ baseDir: testDir });

      const config = await fs.readJson(configPath);
      expect(config.hooks.START_RUN.commands).toEqual(['echo before', existingCommand]);
    });

    it('should not inject dependency updater when autoDependencyUpdate is false', async () => {
      const { loadConfig } = await import('../../core/config.js');

      const configDir = join(testDir, '.juno_task');
      const configPath = join(configDir, 'config.json');

      await fs.ensureDir(configDir);
      await fs.writeJson(configPath, {
        defaultSubagent: 'claude',
        autoDependencyUpdate: false,
        hooks: { START_RUN: { commands: [] } },
      });

      await loadConfig({ baseDir: testDir });

      const config = await fs.readJson(configPath);
      expect(config.autoDependencyUpdate).toBe(false);
      expect(config.hooks.START_RUN.commands).toEqual([]);
    });

    it('should preserve an explicit legacy defaultMaxIterations value', async () => {
      const { loadConfig } = await import('../../core/config.js');

      const configDir = join(testDir, '.juno_task');
      const configPath = join(configDir, 'config.json');

      // Create config with old default of 50
      await fs.ensureDir(configDir);
      await fs.writeJson(configPath, {
        defaultSubagent: 'claude',
        defaultMaxIterations: 50,
        hooks: { START_RUN: { commands: [] } },
      });

      await loadConfig({ baseDir: testDir });

      const config = await fs.readJson(configPath);
      expect(config.defaultMaxIterations).toBe(50);
      expect(config.defaultSubagent).toBe('claude'); // Preserve other fields
    });

    it('should not change defaultMaxIterations if user set a custom value', async () => {
      const { loadConfig } = await import('../../core/config.js');

      const configDir = join(testDir, '.juno_task');
      const configPath = join(configDir, 'config.json');

      // Create config with user-chosen value (not the old default of 50)
      await fs.ensureDir(configDir);
      await fs.writeJson(configPath, {
        defaultSubagent: 'claude',
        defaultMaxIterations: 10,
        hooks: { START_RUN: { commands: [] } },
      });

      await loadConfig({ baseDir: testDir });

      const config = await fs.readJson(configPath);
      expect(config.defaultMaxIterations).toBe(10); // Should be preserved
    });

    it('should not migrate valid shorthand model names', async () => {
      const { loadConfig } = await import('../../core/config.js');

      const validShorthands = [':sonnet', ':haiku', ':opus', ':codex', ':pro', ':flash'];

      for (const shorthand of validShorthands) {
        const configDir = join(testDir, '.juno_task');
        const configPath = join(configDir, 'config.json');

        await fs.ensureDir(configDir);
        await fs.writeJson(configPath, {
          defaultSubagent: 'claude',
          defaultModel: shorthand,
          hooks: { START_RUN: { commands: [] } },
        });

        await loadConfig({ baseDir: testDir });

        const config = await fs.readJson(configPath);
        expect(config.defaultModel).toBe(shorthand);
      }
    });

    it('should not migrate unknown full model names (user-configured)', async () => {
      const { loadConfig } = await import('../../core/config.js');

      const configDir = join(testDir, '.juno_task');
      const configPath = join(configDir, 'config.json');

      await fs.ensureDir(configDir);
      await fs.writeJson(configPath, {
        defaultSubagent: 'claude',
        defaultModel: 'my-custom-model-v2',
        hooks: { START_RUN: { commands: [] } },
      });

      await loadConfig({ baseDir: testDir });

      const config = await fs.readJson(configPath);
      expect(config.defaultModel).toBe('my-custom-model-v2');
    });

    it('should set codex default to :codex shorthand (not full name) when missing', async () => {
      const { loadConfig } = await import('../../core/config.js');

      const configDir = join(testDir, '.juno_task');
      const configPath = join(configDir, 'config.json');

      await fs.ensureDir(configDir);
      await fs.writeJson(configPath, {
        defaultSubagent: 'codex',
        hooks: { START_RUN: { commands: [] } },
      });

      await loadConfig({ baseDir: testDir });

      const config = await fs.readJson(configPath);
      expect(config.defaultModel).toBe(':codex');
    });

    it('should set gemini default to :pro shorthand when missing', async () => {
      const { loadConfig } = await import('../../core/config.js');

      const configDir = join(testDir, '.juno_task');
      const configPath = join(configDir, 'config.json');

      await fs.ensureDir(configDir);
      await fs.writeJson(configPath, {
        defaultSubagent: 'gemini',
        hooks: { START_RUN: { commands: [] } },
      });

      await loadConfig({ baseDir: testDir });

      const config = await fs.readJson(configPath);
      expect(config.defaultModel).toBe(':pro');
    });
  });

  describe('edge cases and boundary conditions', () => {
    it('should handle undefined context gracefully', async () => {
      const simpleHooks: HooksConfig = {
        START_RUN: {
          commands: ['echo "test"'],
        },
      };

      mockedExeca.mockResolvedValueOnce({
        exitCode: 0,
        stdout: 'test',
        stderr: '',
        all: 'test',
      } as any);

      const result = await executeHook('START_RUN', simpleHooks);

      expect(result.success).toBe(true);
      expect(mockedExeca).toHaveBeenCalledWith(
        'echo "test"',
        expect.objectContaining({
          shell: true,
          env: expect.objectContaining({
            HOOK_TYPE: 'START_RUN',
            ITERATION: '',
            SESSION_ID: '',
            RUN_ID: '',
            TOTAL_ITERATIONS: '',
          }),
        }),
      );
    });

    it('should handle very long command output', async () => {
      const longOutputHooks: HooksConfig = {
        START_RUN: {
          commands: ['echo "test"'],
        },
      };

      const longOutput = 'x'.repeat(10000);
      mockedExeca.mockResolvedValueOnce({
        exitCode: 0,
        stdout: longOutput,
        stderr: '',
        all: longOutput,
      } as any);

      const result = await executeHook('START_RUN', longOutputHooks);

      expect(result.success).toBe(true);
      expect(result.commandResults[0].stdout).toBe(longOutput);
    });

    it('should handle special characters in commands', async () => {
      const specialCharHooks: HooksConfig = {
        START_RUN: {
          commands: ['echo "Hello & goodbye; echo done | cat"'],
        },
      };

      mockedExeca.mockResolvedValueOnce({
        exitCode: 0,
        stdout: 'Hello & goodbye; echo done | cat',
        stderr: '',
        all: 'Hello & goodbye; echo done | cat',
      } as any);

      const result = await executeHook('START_RUN', specialCharHooks);

      expect(result.success).toBe(true);
      expect(mockedExeca).toHaveBeenCalledWith(
        'echo "Hello & goodbye; echo done | cat"',
        expect.any(Object),
      );
    });

    it('should handle metadata with special characters', async () => {
      const specialMetadataContext: HookExecutionContext = {
        metadata: {
          'special-key': 'value with spaces',
          number_key: 123,
          boolean_key: true,
        },
      };

      const metadataHooks: HooksConfig = {
        START_RUN: {
          commands: ['echo "test"'],
        },
      };

      mockedExeca.mockResolvedValueOnce({
        exitCode: 0,
        stdout: 'test',
        stderr: '',
        all: 'test',
      } as any);

      await executeHook('START_RUN', metadataHooks, specialMetadataContext);

      expect(mockedExeca).toHaveBeenCalledWith(
        'echo "test"',
        expect.objectContaining({
          env: expect.objectContaining({
            'JUNO_SPECIAL-KEY': 'value with spaces',
            JUNO_NUMBER_KEY: '123',
            JUNO_BOOLEAN_KEY: 'true',
          }),
        }),
      );
    });
  });
});
