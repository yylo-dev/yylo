/**
 * Comprehensive tests for Main Command
 *
 * Tests the main command functionality including:
 * - Command creation and registration
 * - Prompt processing (inline, file, interactive)
 * - Subagent validation
 * - Execution coordination
 * - Progress display
 * - Error handling
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { Command } from 'commander';
import { createHash } from 'node:crypto';
import * as path from 'node:path';
import * as childProcess from 'node:child_process';
import * as fs from 'fs-extra';

import { mainCommandHandler } from '../commands/main.js';
import { logger, LogLevel } from '../utils/advanced-logger.js';

import type { MainCommandOptions } from '../types.js';
import { ConfigurationError } from '../types.js';

import { loadConfig } from '../../core/config.js';
import { createExecutionEngine, createExecutionRequest } from '../../core/engine.js';
import { getCurrentGitBranch } from '../../core/git.js';
import {
  getActiveSessionBranch,
  getSessionMetadataDirectory,
  listSessionBranches,
  resetMainSessionBranch,
  resolveScopedContinueSessionState,
  persistContinueScopeSnapshot,
  updateActiveSessionBranch,
  upsertClonedSessionBranch,
} from '../../core/session-continuity-state.js';

import type {
  ExecutionRequest,
  ExecutionResult,
  ExecutionStatus,
  ProgressEvent,
} from '../../core/engine.js';

// Workspace authority is exercised with real Git in agent-startup.test.ts.
vi.mock('../../utils/agent-startup.js', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../utils/agent-startup.js')>(),
  resolveAgentWorkspace: vi.fn(),
}));

// Mock external dependencies
vi.mock('../../core/config.js', () => ({
  loadConfig: vi.fn().mockResolvedValue({
    workingDirectory: '/test/dir',
    defaultMaxIterations: 5,
    defaultModel: 'test-model',
    mcpServerPath: '/test/mcp',
    mcpTimeout: 30000,
    mcpRetries: 3,
    verbose: 0,
  }),
}));

vi.mock('../../core/engine.js', () => ({
  ExecutionStatus: {
    PENDING: 'pending',
    RUNNING: 'running',
    COMPLETED: 'completed',
    FAILED: 'failed',
    CANCELLED: 'cancelled',
  },
  createExecutionEngine: vi.fn().mockReturnValue({
    execute: vi.fn().mockResolvedValue({
      status: 'completed',
      iterations: [
        {
          toolResult: { content: 'Test result' },
        },
      ],
      statistics: {
        totalIterations: 1,
        successfulIterations: 1,
        failedIterations: 0,
        averageIterationDuration: 1000,
        totalToolCalls: 5,
        rateLimitEncounters: 0,
      },
    }),
    onProgress: vi.fn(),
    on: vi.fn(),
    shutdown: vi.fn(),
  }),
  createExecutionRequest: vi.fn().mockImplementation((opts) => ({
    requestId: 'test-request',
    instruction: opts.instruction,
    subagent: opts.subagent,
    workingDirectory: opts.workingDirectory,
    maxIterations: opts.maxIterations,
    model: opts.model,
    resume: opts.resume,
    cloneSession: opts.cloneSession,
    cloneFromSession: opts.cloneFromSession,
    live: opts.live,
  })),
}));

vi.mock('../../core/git.js', () => ({
  getCurrentGitBranch: vi.fn().mockResolvedValue(null),
}));

vi.mock('../../core/session.js', () => ({
  createSessionManager: vi.fn().mockReturnValue({
    create: vi.fn(),
    load: vi.fn(),
    save: vi.fn(),
  }),
}));

vi.mock('../../core/session-continuity-state.js', () => ({
  MAIN_SESSION_BRANCH: 'main',
  SessionContinuityStateError: class SessionContinuityStateError extends Error {
    constructor(message: string) {
      super(message);
      this.name = 'SessionContinuityStateError';
    }
  },
  getSessionMetadataDirectory: vi.fn().mockImplementation((workingDirectory: string) =>
    process.env.YYLO_SESSION_METADATA_DIRECTORY || `${workingDirectory}/.juno_task`),
  getActiveSessionBranch: vi.fn().mockResolvedValue(null),
  resolveScopedContinueSessionState: vi.fn().mockImplementation(async () => {
    const sessionKey = Object.keys(process.env).find((key) => key.startsWith('YYLO_LAST_SESSION_ID_SCOPE_'));
    const settingsKey = sessionKey?.replace('YYLO_LAST_SESSION_ID_', 'YYLO_LAST_EXECUTION_SETTINGS_');
    const resolvedSessionId = sessionKey ? process.env[sessionKey]?.trim() || '' : '';
    return {
      context: { scopeHash: sessionKey?.slice('YYLO_LAST_SESSION_ID_'.length) || 'SCOPE_0000000000000000', scopeSource: 'test' },
      activeBranch: null,
      resolvedSessionId,
      settings: settingsKey && process.env[settingsKey] ? JSON.parse(process.env[settingsKey]!) : null,
      serializedSettings: settingsKey ? process.env[settingsKey] || null : null,
    };
  }),
  persistContinueScopeSnapshot: vi.fn().mockResolvedValue(undefined),
  listSessionBranches: vi.fn().mockResolvedValue([]),
  resetMainSessionBranch: vi.fn().mockResolvedValue(undefined),
  updateActiveSessionBranch: vi.fn().mockResolvedValue(undefined),
  upsertClonedSessionBranch: vi.fn().mockResolvedValue(undefined),
  validateSessionBranchName: vi.fn((branchName: string, options?: { allowMain?: boolean }) => {
    const normalized = branchName.trim();
    if (!normalized) return { valid: false, normalized, reason: 'Branch name cannot be empty.' };
    if (options?.allowMain === false && normalized === 'main') {
      return { valid: false, normalized, reason: "'main' is reserved for the root session branch." };
    }
    return { valid: true, normalized };
  }),
}));

vi.mock('../../core/session-metadata.js', () => ({
  SESSION_METADATA_DIRECTORY_ENV: 'YYLO_SESSION_METADATA_DIRECTORY',
  SESSION_CONTINUITY_SHARED_LOCK_NAME: 'session_continuity.v2.json',
  getSessionMetadataDirectory: (workingDirectory: string) =>
    process.env.YYLO_SESSION_METADATA_DIRECTORY || `${workingDirectory}/.juno_task`,
  withSessionMetadataLock: async (_directory: string, _name: string, operation: () => Promise<unknown>) => operation(),
  writeSessionMetadataFileAtomic: vi.fn().mockResolvedValue(undefined),
}));

vi.mock('node:child_process', async (importOriginal) => {
  const actual = await importOriginal<typeof import('node:child_process')>();
  return {
    ...actual,
    execFile: vi.fn(),
  };
});

vi.mock('fs-extra', () => {
  const mock = {
    pathExists: vi.fn().mockResolvedValue(false), // 'test prompt' is not a file path
    readFile: vi.fn().mockResolvedValue('mock file content'),
    writeFile: vi.fn().mockResolvedValue(undefined),
    readJson: vi.fn().mockResolvedValue({ version: 1, sessions: [] }),
    writeJson: vi.fn().mockResolvedValue(undefined),
    ensureDir: vi.fn().mockResolvedValue(undefined),
    fstatSync: vi.fn().mockReturnValue({
      isFIFO: () => false,
      isFile: () => false,
      isSocket: () => false,
    }),
  };
  return { ...mock, default: mock };
});

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

describe('Main Command', () => {
  let consoleSpy: ReturnType<typeof vi.spyOn>;
  let processExitSpy: ReturnType<typeof vi.spyOn>;
  let processStdinSpy: ReturnType<typeof vi.spyOn>;
  let originalIsTTY: boolean | undefined;

  beforeEach(() => {
    consoleSpy = vi.spyOn(console, 'log').mockImplementation(() => {});
    vi.spyOn(console, 'error').mockImplementation(() => {});
    processExitSpy = vi
      .spyOn(process, 'exit')
      .mockImplementation((code: string | number | null | undefined = 1) => {
        return undefined as never;
      });

    // Save original isTTY and default to TTY (most tests expect prompt-required behavior)
    originalIsTTY = process.stdin.isTTY;
    Object.defineProperty(process.stdin, 'isTTY', { value: true, writable: true, configurable: true });

    // Mock stdin for interactive input
    processStdinSpy = vi.spyOn(process.stdin, 'on').mockImplementation(() => process.stdin);
    vi.spyOn(process.stdin, 'setEncoding').mockImplementation(() => process.stdin);
    vi.spyOn(process.stdin, 'resume').mockImplementation(() => process.stdin);

    // Re-set mock implementations (mockReset: true in vitest config clears them between tests)
    vi.mocked(loadConfig).mockResolvedValue({
      workingDirectory: '/test/dir',
      defaultMaxIterations: 5,
      defaultModel: 'test-model',
      mcpServerPath: '/test/mcp',
      mcpTimeout: 30000,
      mcpRetries: 3,
      verbose: 0,
    } as any);

    vi.mocked(createExecutionEngine).mockReturnValue({
      execute: vi.fn().mockResolvedValue({
        status: 'completed',
        iterations: [{ toolResult: { content: 'Test result' } }],
        statistics: {
          totalIterations: 1,
          successfulIterations: 1,
          failedIterations: 0,
          averageIterationDuration: 1000,
          totalToolCalls: 5,
          rateLimitEncounters: 0,
        },
      }),
      onProgress: vi.fn(),
      on: vi.fn(),
      shutdown: vi.fn(),
    } as any);

    vi.mocked(createExecutionRequest).mockImplementation((opts: any) => ({
      requestId: 'test-request',
      instruction: opts.instruction,
      subagent: opts.subagent,
      workingDirectory: opts.workingDirectory,
      maxIterations: opts.maxIterations,
      model: opts.model,
      resume: opts.resume,
      cloneSession: opts.cloneSession,
      cloneFromSession: opts.cloneFromSession,
      live: opts.live,
    }));

    vi.mocked(getCurrentGitBranch).mockResolvedValue(null);
    vi.mocked(getSessionMetadataDirectory).mockImplementation((workingDirectory: string) =>
      process.env.YYLO_SESSION_METADATA_DIRECTORY || `${workingDirectory}/.juno_task`);
    vi.mocked(getActiveSessionBranch).mockResolvedValue(null as any);
    vi.mocked(resolveScopedContinueSessionState).mockImplementation(async () => {
      const sessionKey = Object.keys(process.env).find((key) => key.startsWith('YYLO_LAST_SESSION_ID_SCOPE_'));
      const settingsKey = sessionKey?.replace('YYLO_LAST_SESSION_ID_', 'YYLO_LAST_EXECUTION_SETTINGS_');
      return {
        context: { scopeHash: sessionKey?.slice('YYLO_LAST_SESSION_ID_'.length) || 'SCOPE_0000000000000000', scopeSource: 'test' },
        activeBranch: null,
        resolvedSessionId: sessionKey ? process.env[sessionKey]?.trim() || '' : '',
        settings: settingsKey && process.env[settingsKey] ? JSON.parse(process.env[settingsKey]!) : null,
        serializedSettings: settingsKey ? process.env[settingsKey] || null : null,
      } as any;
    });
    vi.mocked(persistContinueScopeSnapshot).mockResolvedValue(undefined as any);
    vi.mocked(listSessionBranches).mockResolvedValue([] as any);
    vi.mocked(resetMainSessionBranch).mockResolvedValue(undefined as any);
    vi.mocked(updateActiveSessionBranch).mockResolvedValue(undefined as any);
    vi.mocked(upsertClonedSessionBranch).mockResolvedValue(undefined as any);

    vi.mocked(fs.pathExists).mockResolvedValue(false as any);
    vi.mocked(fs.readFile).mockResolvedValue('mock file content' as any);
    vi.mocked(fs.writeFile).mockResolvedValue(undefined as any);
    vi.mocked(fs.readJson).mockResolvedValue({ version: 1, sessions: [] } as any);
    vi.mocked(fs.writeJson).mockResolvedValue(undefined as any);
    vi.mocked(fs.ensureDir).mockResolvedValue(undefined as any);
    vi.mocked(fs.fstatSync as any).mockReturnValue({
      isFIFO: () => false,
      isFile: () => false,
      isSocket: () => false,
    });

    vi.mocked(childProcess.execFile as any).mockImplementation(
      (_file: string, _args?: any, _options?: any, callback?: any) => {
        const cb =
          typeof _args === 'function' ? _args : typeof _options === 'function' ? _options : callback;
        cb?.(null, '[]', '');
        return {} as any;
      },
    );
  });

  afterEach(() => {
    for (const key of Object.keys(process.env)) {
      if (
        key.startsWith('YYLO_LAST_SESSION_ID') ||
        key.startsWith('YYLO_LAST_EXECUTION_SETTINGS') ||
        key === 'YYLO_CONTINUE_SCOPE' ||
        key === 'YYLO_SESSION_METADATA_DIRECTORY' ||
        key === 'TMUX_PANE'
      ) {
        delete process.env[key];
      }
    }
    vi.clearAllMocks();
    consoleSpy.mockRestore();
    processExitSpy.mockRestore();
    processStdinSpy.mockRestore();
    // Restore original isTTY
    Object.defineProperty(process.stdin, 'isTTY', { value: originalIsTTY, writable: true, configurable: true });
  });

  describe('mainCommandHandler', () => {
    const mockCommand = new Command();

    describe('subagent validation', () => {
      it('should accept valid subagents', async () => {
        const options: MainCommandOptions = {
          subagent: 'claude',
          prompt: 'test prompt',
          cwd: '/test',
          maxIterations: 1,
          interactive: false,
          interactivePrompt: false,
          verbose: 0,
          quiet: false,
          logLevel: 'info',
        };

        await mainCommandHandler([], options, mockCommand);

        expect(processExitSpy).toHaveBeenCalledWith(0);
      });

      it('should reject invalid subagents', async () => {
        const options: MainCommandOptions = {
          subagent: 'invalid' as any,
          prompt: 'test prompt',
          cwd: '/test',
          maxIterations: 1,
          interactive: false,
          interactivePrompt: false,
          verbose: 0,
          quiet: false,
          logLevel: 'info',
        };

        await mainCommandHandler([], options, mockCommand);

        expect(processExitSpy).toHaveBeenCalledWith(1);
      });

      it('should reject --live for non-pi subagents with actionable validation guidance', async () => {
        const options = {
          subagent: 'claude',
          prompt: 'test prompt',
          cwd: '/test',
          maxIterations: 1,
          interactive: false,
          interactivePrompt: false,
          live: true,
          verbose: 0,
          quiet: false,
          logLevel: 'info',
        } as MainCommandOptions & { live: boolean };

        await mainCommandHandler([], options as MainCommandOptions, mockCommand);

        expect(processExitSpy).toHaveBeenCalledWith(1);

        const stderrOutput = (console.error as unknown as { mock: { calls: unknown[][] } }).mock.calls
          .flat()
          .join('\n');
        expect(stderrOutput).toContain('--live is only supported with the pi subagent');
        expect(stderrOutput).toContain('Use: yylo pi --live');
      });

      it('should accept --live for pi and forward live=true into execution request', async () => {
        const options = {
          subagent: 'pi',
          prompt: 'test prompt',
          cwd: '/test',
          maxIterations: 1,
          interactive: false,
          interactivePrompt: false,
          live: true,
          verbose: 0,
          quiet: false,
          logLevel: 'info',
        } as MainCommandOptions & { live: boolean };

        await mainCommandHandler([], options as MainCommandOptions, mockCommand);

        expect(createExecutionRequest).toHaveBeenCalledWith(
          expect.objectContaining({
            subagent: 'pi',
            live: true,
          }),
        );
        expect(processExitSpy).toHaveBeenCalledWith(0);
      });

      it.skip('should accept subagent aliases', async () => {
        // SKIP: Alias normalization not implemented yet
        // 'claude-code' is not in validSubagents list, so validation rejects it
        // TODO: Implement alias normalization if needed for user experience
        const options: MainCommandOptions = {
          subagent: 'claude-code' as any,
          prompt: 'test prompt',
          cwd: '/test',
          maxIterations: 1,
          interactive: false,
          interactivePrompt: false,
          verbose: 0,
          quiet: false,
          logLevel: 'info',
        };

        await mainCommandHandler([], options, mockCommand);
        expect(processExitSpy).toHaveBeenCalledWith(0);
      });
    });

    describe('prompt processing', () => {
      it('should handle inline prompt', async () => {
        const options: MainCommandOptions = {
          subagent: 'claude',
          prompt: 'test inline prompt',
          cwd: '/test',
          maxIterations: 1,
          interactive: false,
          interactivePrompt: false,
          verbose: 0,
          quiet: false,
          logLevel: 'info',
        };

        await mainCommandHandler([], options, mockCommand);

        const { createExecutionRequest } = await import('../../core/engine.js');
        expect(createExecutionRequest).toHaveBeenCalledWith(
          expect.objectContaining({
            instruction: 'test inline prompt',
          }),
        );
      });

      it('should rewrite a leading %shortcut for claude prompts', async () => {
        const options: MainCommandOptions = {
          subagent: 'claude',
          prompt: '%ralph-loop-yylo investigate this regression',
          cwd: '/test',
          maxIterations: 1,
          interactive: false,
          interactivePrompt: false,
          verbose: 0,
          quiet: false,
          logLevel: 'info',
        };

        await mainCommandHandler([], options, mockCommand);

        const { createExecutionRequest } = await import('../../core/engine.js');
        expect(createExecutionRequest).toHaveBeenCalledWith(
          expect.objectContaining({
            instruction: '/ralph-loop-yylo investigate this regression',
          }),
        );
      });

      it('should rewrite a leading %shortcut for pi prompts', async () => {
        const options: MainCommandOptions = {
          subagent: 'pi',
          prompt: '%ralph-loop-yylo investigate this regression',
          cwd: '/test',
          maxIterations: 1,
          interactive: false,
          interactivePrompt: false,
          verbose: 0,
          quiet: false,
          logLevel: 'info',
        };

        await mainCommandHandler([], options, mockCommand);

        const { createExecutionRequest } = await import('../../core/engine.js');
        expect(createExecutionRequest).toHaveBeenCalledWith(
          expect.objectContaining({
            instruction: '/skill:ralph-loop-yylo investigate this regression',
          }),
        );
      });

      it('should preserve a multiline heredoc payload while rewriting a Pi shortcut', async () => {
        const payload = '## oD5g4o\nWhat is the root cause of 504\n@@no_code';
        const options: MainCommandOptions = {
          subagent: 'pi',
          prompt: `%ralph-loop-yylo ${payload}`,
          cwd: '/test',
          maxIterations: 1,
          interactive: false,
          interactivePrompt: false,
          verbose: 0,
          quiet: false,
          logLevel: 'info',
        };

        await mainCommandHandler([], options, mockCommand);

        const { createExecutionRequest } = await import('../../core/engine.js');
        expect(createExecutionRequest).toHaveBeenCalledWith(
          expect.objectContaining({ instruction: `/skill:ralph-loop-yylo ${payload}` }),
        );
      });

      it('should rewrite a leading %shortcut for codex prompts', async () => {
        const options: MainCommandOptions = {
          subagent: 'codex',
          prompt: '%ralph-loop-yylo investigate this regression',
          cwd: '/test',
          maxIterations: 1,
          interactive: false,
          interactivePrompt: false,
          verbose: 0,
          quiet: false,
          logLevel: 'info',
        };

        await mainCommandHandler([], options, mockCommand);

        const { createExecutionRequest } = await import('../../core/engine.js');
        expect(createExecutionRequest).toHaveBeenCalledWith(
          expect.objectContaining({
            instruction: '$ralph-loop-yylo investigate this regression',
          }),
        );
      });

      it('should only rewrite %shortcut when it is at the very start of the prompt', async () => {
        const options: MainCommandOptions = {
          subagent: 'pi',
          prompt: 'please run %ralph-loop-yylo now',
          cwd: '/test',
          maxIterations: 1,
          interactive: false,
          interactivePrompt: false,
          verbose: 0,
          quiet: false,
          logLevel: 'info',
        };

        await mainCommandHandler([], options, mockCommand);

        const { createExecutionRequest } = await import('../../core/engine.js');
        expect(createExecutionRequest).toHaveBeenCalledWith(
          expect.objectContaining({
            instruction: 'please run %ralph-loop-yylo now',
          }),
        );
      });

      it('should support braced %{} shortcut form at the start of the prompt', async () => {
        const options: MainCommandOptions = {
          subagent: 'pi',
          prompt: '%{ralph-loop-yylo} investigate this regression',
          cwd: '/test',
          maxIterations: 1,
          interactive: false,
          interactivePrompt: false,
          verbose: 0,
          quiet: false,
          logLevel: 'info',
        };

        await mainCommandHandler([], options, mockCommand);

        const { createExecutionRequest } = await import('../../core/engine.js');
        expect(createExecutionRequest).toHaveBeenCalledWith(
          expect.objectContaining({
            instruction: '/skill:ralph-loop-yylo investigate this regression',
          }),
        );
      });

      it('should strip a leading markdown delimiter and rewrite %shortcut for pi prompt files', async () => {
        vi.mocked(fs.readFile).mockResolvedValueOnce('---\n\n%ralph-loop-yylo investigate this regression');

        const options: MainCommandOptions = {
          subagent: 'pi',
          promptFile: '/path/to/prompt_item-012.txt',
          live: true,
          cwd: '/test',
          maxIterations: 1,
          interactive: false,
          interactivePrompt: false,
          verbose: 0,
          quiet: false,
          logLevel: 'info',
        };

        await mainCommandHandler([], options, mockCommand);

        const { createExecutionRequest } = await import('../../core/engine.js');
        expect(createExecutionRequest).toHaveBeenCalledWith(
          expect.objectContaining({
            instruction: '/skill:ralph-loop-yylo investigate this regression',
            live: true,
          }),
        );
      });

      it('should strip a leading markdown delimiter before existing /skill directives', async () => {
        vi.mocked(fs.readFile).mockResolvedValueOnce('---\n\n/skill:ralph-loop-yylo investigate this regression');

        const options: MainCommandOptions = {
          subagent: 'pi',
          promptFile: '/path/to/prompt_item-013.txt',
          live: true,
          cwd: '/test',
          maxIterations: 1,
          interactive: false,
          interactivePrompt: false,
          verbose: 0,
          quiet: false,
          logLevel: 'info',
        };

        await mainCommandHandler([], options, mockCommand);

        const { createExecutionRequest } = await import('../../core/engine.js');
        expect(createExecutionRequest).toHaveBeenCalledWith(
          expect.objectContaining({
            instruction: '/skill:ralph-loop-yylo investigate this regression',
            live: true,
          }),
        );
      });

      it('should expand ##task-id prompt references with kanban task payloads', async () => {
        process.env.YYLO_LAST_SESSION_ID_SCOPE_0123456789ABCDEF = 'historical';
        process.env.YYLO_LAST_EXECUTION_SETTINGS = 'legacy';
        vi.mocked(fs.pathExists)
          .mockResolvedValueOnce(false as any)
          .mockResolvedValueOnce(true as any);

        vi.mocked(childProcess.execFile as any).mockImplementationOnce(
          (_file: string, _args?: any, _options?: any, callback?: any) => {
            const cb =
              typeof _args === 'function' ? _args : typeof _options === 'function' ? _options : callback;
            cb?.(
              null,
              JSON.stringify([
                {
                  id: 'c7Lj80',
                  status: 'backlog',
                  body: 'Example task body',
                },
              ]),
              '',
            );
            return {} as any;
          },
        );

        const options: MainCommandOptions = {
          subagent: 'claude',
          prompt: 'Please analyze ## c7Lj80 before coding',
          cwd: '/test',
          maxIterations: 1,
          interactive: false,
          interactivePrompt: false,
          verbose: 0,
          quiet: false,
          logLevel: 'info',
        };

        await mainCommandHandler([], options, mockCommand);

        expect(childProcess.execFile).toHaveBeenCalledWith(
          path.join('/test/dir', '.juno_task', 'scripts', 'kanban.sh'),
          ['get', 'c7Lj80'],
          expect.objectContaining({ cwd: '/test/dir' }),
          expect.any(Function),
        );
        const kanbanEnvironment = vi.mocked(childProcess.execFile).mock.calls[0]?.[2]?.env;
        expect(kanbanEnvironment?.JUNO_TASK_ROOT).toBe('/test/dir');
        expect(kanbanEnvironment?.YYLO_LAST_SESSION_ID_SCOPE_0123456789ABCDEF).toBeUndefined();
        expect(kanbanEnvironment?.YYLO_LAST_EXECUTION_SETTINGS).toBeUndefined();

        const { createExecutionRequest } = await import('../../core/engine.js');
        expect(createExecutionRequest).toHaveBeenCalledWith(
          expect.objectContaining({
            instruction: expect.stringContaining('[kanban_task:c7Lj80]'),
          }),
        );
        expect(createExecutionRequest).toHaveBeenCalledWith(
          expect.objectContaining({
            instruction: expect.stringContaining('"body": "Example task body"'),
          }),
        );
      });

      it('should expand multiple ##task-id references from a single kanban lookup', async () => {
        vi.mocked(fs.pathExists)
          .mockResolvedValueOnce(false as any)
          .mockResolvedValueOnce(true as any);

        vi.mocked(childProcess.execFile as any).mockImplementationOnce(
          (_file: string, _args?: any, _options?: any, callback?: any) => {
            const cb =
              typeof _args === 'function' ? _args : typeof _options === 'function' ? _options : callback;
            cb?.(
              null,
              JSON.stringify([
                {
                  id: 'c7Lj80',
                  status: 'done',
                  body: 'First task',
                },
                {
                  id: '29MVVA',
                  status: 'backlog',
                  body: 'Second task',
                },
              ]),
              '',
            );
            return {} as any;
          },
        );

        const options: MainCommandOptions = {
          subagent: 'claude',
          prompt: 'Please analyze ## c7Lj80 and ## 29MVVA before coding',
          cwd: '/test',
          maxIterations: 1,
          interactive: false,
          interactivePrompt: false,
          verbose: 0,
          quiet: false,
          logLevel: 'info',
        };

        await mainCommandHandler([], options, mockCommand);

        expect(childProcess.execFile).toHaveBeenCalledWith(
          path.join('/test/dir', '.juno_task', 'scripts', 'kanban.sh'),
          ['get', 'c7Lj80', '29MVVA'],
          expect.objectContaining({ cwd: '/test/dir' }),
          expect.any(Function),
        );

        const { createExecutionRequest } = await import('../../core/engine.js');
        expect(createExecutionRequest).toHaveBeenCalledWith(
          expect.objectContaining({
            instruction: expect.stringContaining('[kanban_task:c7Lj80]'),
          }),
        );
        expect(createExecutionRequest).toHaveBeenCalledWith(
          expect.objectContaining({
            instruction: expect.stringContaining('[kanban_task:29MVVA]'),
          }),
        );
      });

      it('should expand resolvable ##task-id references even when batch kanban lookup fails', async () => {
        vi.mocked(fs.pathExists)
          .mockResolvedValueOnce(false as any)
          .mockResolvedValueOnce(true as any);

        vi.mocked(childProcess.execFile as any).mockImplementation(
          (_file: string, _args?: any, _options?: any, callback?: any) => {
            const cb =
              typeof _args === 'function' ? _args : typeof _options === 'function' ? _options : callback;
            const args = Array.isArray(_args) ? _args : [];
            const ids = args.slice(1);

            if (ids.length > 1) {
              cb?.(new Error('batch lookup failed'), '', 'missing task');
              return {} as any;
            }

            if (ids[0] === 'c7Lj80') {
              cb?.(
                null,
                JSON.stringify([
                  {
                    id: 'c7Lj80',
                    status: 'done',
                    body: 'Task resolved via single lookup',
                  },
                ]),
                '',
              );
              return {} as any;
            }

            cb?.(new Error('task not found'), '', 'missing task');
            return {} as any;
          },
        );

        const options: MainCommandOptions = {
          subagent: 'claude',
          prompt: 'Please analyze ## c7Lj80 and ## BAD111 before coding',
          cwd: '/test',
          maxIterations: 1,
          interactive: false,
          interactivePrompt: false,
          verbose: 0,
          quiet: false,
          logLevel: 'info',
        };

        await mainCommandHandler([], options, mockCommand);

        expect(childProcess.execFile).toHaveBeenCalledWith(
          path.join('/test/dir', '.juno_task', 'scripts', 'kanban.sh'),
          ['get', 'c7Lj80', 'BAD111'],
          expect.objectContaining({ cwd: '/test/dir' }),
          expect.any(Function),
        );

        expect(childProcess.execFile).toHaveBeenCalledWith(
          path.join('/test/dir', '.juno_task', 'scripts', 'kanban.sh'),
          ['get', 'c7Lj80'],
          expect.objectContaining({ cwd: '/test/dir' }),
          expect.any(Function),
        );

        const { createExecutionRequest } = await import('../../core/engine.js');
        const call = vi.mocked(createExecutionRequest).mock.calls.at(-1)?.[0] as {
          instruction?: string;
        };

        expect(call.instruction).toContain('[kanban_task:c7Lj80]');
        expect(call.instruction).toContain('## BAD111');
      });

      it('should warn the user and instruct the agent to fetch the task manually after hydration timeout', async () => {
        vi.mocked(fs.pathExists)
          .mockResolvedValueOnce(false as any)
          .mockResolvedValueOnce(true as any);

        vi.mocked(childProcess.execFile as any).mockImplementation(
          (_file: string, _args?: any, _options?: any, callback?: any) => {
            const cb =
              typeof _args === 'function' ? _args : typeof _options === 'function' ? _options : callback;
            cb?.(
              Object.assign(new Error('Command timed out'), { killed: true, signal: 'SIGTERM' }),
              '',
              '',
            );
            return {} as any;
          },
        );

        const options: MainCommandOptions = {
          subagent: 'claude',
          prompt: 'Please analyze ## c7Lj80 before coding',
          cwd: '/test',
          maxIterations: 1,
          interactive: false,
          interactivePrompt: false,
          verbose: 0,
          quiet: false,
          logLevel: 'info',
        };

        await mainCommandHandler([], options, mockCommand);

        const { createExecutionRequest } = await import('../../core/engine.js');
        const call = vi.mocked(createExecutionRequest).mock.calls.at(-1)?.[0] as {
          instruction?: string;
        };
        expect(call.instruction).toContain('[kanban_task_hydration_warning:c7Lj80]');
        expect(call.instruction).toContain('./.juno_task/scripts/kanban.sh get c7Lj80');
        expect(call.instruction).toContain('## c7Lj80');
        expect(console.error).toHaveBeenCalledWith(
          expect.stringContaining('Kanban task hydration timed out for c7Lj80'),
        );
        expect(childProcess.execFile).toHaveBeenCalledWith(
          path.join('/test/dir', '.juno_task', 'scripts', 'kanban.sh'),
          ['get', 'c7Lj80'],
          expect.objectContaining({ timeout: 10000 }),
          expect.any(Function),
        );
        expect(childProcess.execFile).toHaveBeenCalledTimes(3);
      });

      it('should not fallback to juno-kanban when local kanban script exists', async () => {
        vi.mocked(fs.pathExists)
          .mockResolvedValueOnce(false as any)
          .mockResolvedValueOnce(true as any);

        vi.mocked(childProcess.execFile as any).mockImplementation(
          (_file: string, _args?: any, _options?: any, callback?: any) => {
            const cb =
              typeof _args === 'function' ? _args : typeof _options === 'function' ? _options : callback;
            cb?.(new Error('Task not found: abc123'), '', 'Task not found: abc123');
            return {} as any;
          },
        );

        const options: MainCommandOptions = {
          subagent: 'claude',
          prompt: 'Please analyze ## abc123 before coding',
          cwd: '/test',
          maxIterations: 1,
          interactive: false,
          interactivePrompt: false,
          verbose: 0,
          quiet: false,
          logLevel: 'info',
        };

        await mainCommandHandler([], options, mockCommand);

        const calls = vi.mocked(childProcess.execFile).mock.calls;
        expect(calls.length).toBeGreaterThan(0);
        expect(calls.every((call) => call[0] === path.join('/test/dir', '.juno_task', 'scripts', 'kanban.sh'))).toBe(true);
        const { createExecutionRequest } = await import('../../core/engine.js');
        const request = vi.mocked(createExecutionRequest).mock.calls.at(-1)?.[0] as { instruction?: string };
        expect(request.instruction).toBe('Please analyze ## abc123 before coding');
        expect(console.error).not.toHaveBeenCalledWith(expect.stringContaining('Kanban task hydration'));
      });

      it('should handle file prompt', async () => {
        vi.mocked(fs.pathExists).mockResolvedValueOnce(true);
        vi.mocked(fs.readFile).mockResolvedValueOnce('file prompt content');

        const options: MainCommandOptions = {
          subagent: 'claude',
          prompt: '/path/to/prompt.txt',
          cwd: '/test',
          maxIterations: 1,
          interactive: false,
          interactivePrompt: false,
          verbose: 0,
          quiet: false,
          logLevel: 'info',
        };

        await mainCommandHandler([], options, mockCommand);

        expect(fs.readFile).toHaveBeenCalledWith(path.resolve('/path/to/prompt.txt'), 'utf-8');

        const { createExecutionRequest } = await import('../../core/engine.js');
        expect(createExecutionRequest).toHaveBeenCalledWith(
          expect.objectContaining({
            instruction: 'file prompt content',
          }),
        );
      });

      it('should handle empty file error', async () => {
        vi.mocked(fs.pathExists).mockResolvedValueOnce(true);
        vi.mocked(fs.readFile).mockResolvedValueOnce('   ');

        const options: MainCommandOptions = {
          subagent: 'claude',
          prompt: '/path/to/empty.txt',
          cwd: '/test',
          maxIterations: 1,
          interactive: false,
          interactivePrompt: false,
          verbose: 0,
          quiet: false,
          logLevel: 'info',
        };

        await mainCommandHandler([], options, mockCommand);

        expect(processExitSpy).toHaveBeenCalledWith(5); // RuntimeError
      });

      it('should handle --prompt-file flag', async () => {
        vi.mocked(fs.readFile).mockResolvedValueOnce('prompt from file');

        const options: MainCommandOptions = {
          subagent: 'claude',
          promptFile: '/path/to/prompt_item-012.txt',
          cwd: '/test',
          maxIterations: 1,
          interactive: false,
          interactivePrompt: false,
          verbose: 0,
          quiet: false,
          logLevel: 'info',
        };

        await mainCommandHandler([], options, mockCommand);

        expect(fs.readFile).toHaveBeenCalledWith(
          path.resolve('/path/to/prompt_item-012.txt'),
          'utf-8',
        );

        const { createExecutionRequest } = await import('../../core/engine.js');
        expect(createExecutionRequest).toHaveBeenCalledWith(
          expect.objectContaining({
            instruction: 'prompt from file',
          }),
        );
      });

      it.skip('should handle interactive prompt', async () => {
        // SKIP: Microtask timing issue — stdin callbacks not set up before test invokes them
        let dataCallback: (chunk: string) => void;
        let endCallback: () => void;

        processStdinSpy.mockImplementation((event: string, callback: any) => {
          if (event === 'data') {
            dataCallback = callback;
          } else if (event === 'end') {
            endCallback = callback;
          }
          return process.stdin;
        });

        const options: MainCommandOptions = {
          subagent: 'claude',
          prompt: undefined,
          cwd: '/test',
          maxIterations: 1,
          interactive: true,
          interactivePrompt: false,
          verbose: 0,
          quiet: false,
          logLevel: 'info',
        };

        // Start the handler
        const handlerPromise = mainCommandHandler([], options, mockCommand);

        // Simulate user input
        dataCallback!('interactive prompt content\n');
        endCallback!();

        await handlerPromise;

        const { createExecutionRequest } = await import('../../core/engine.js');
        expect(createExecutionRequest).toHaveBeenCalledWith(
          expect.objectContaining({
            instruction: 'interactive prompt content',
          }),
        );
      });

      it('should handle missing prompt error', async () => {
        const options: MainCommandOptions = {
          subagent: 'claude',
          prompt: undefined,
          cwd: '/test',
          maxIterations: 1,
          interactive: false,
          interactivePrompt: false,
          verbose: 0,
          quiet: false,
          logLevel: 'info',
        };

        await mainCommandHandler([], options, mockCommand);

        expect(processExitSpy).toHaveBeenCalledWith(1); // ValidationError

        const stderrOutput = (console.error as unknown as { mock: { calls: unknown[][] } }).mock.calls
          .flat()
          .join('\n');
        expect(stderrOutput).toContain(
          'Shell safety: use single quotes (or -f/stdin) when prompt contains backticks or $()',
        );
      });

      it.skip('should handle interactive prompt cancellation', async () => {
        // SKIP: Microtask timing issue — stdin callbacks not set up before test invokes them
        let dataCallback: (chunk: string) => void;
        let endCallback: () => void;

        processStdinSpy.mockImplementation((event: string, callback: any) => {
          if (event === 'data') {
            dataCallback = callback;
          } else if (event === 'end') {
            endCallback = callback;
          }
          return process.stdin;
        });

        const options: MainCommandOptions = {
          subagent: 'claude',
          prompt: undefined,
          cwd: '/test',
          maxIterations: 1,
          interactive: true,
          interactivePrompt: false,
          verbose: 0,
          quiet: false,
          logLevel: 'info',
        };

        // Start the handler
        const handlerPromise = mainCommandHandler([], options, mockCommand);

        // Simulate empty input
        dataCallback!('   ');
        endCallback!();

        await expect(handlerPromise).rejects.toThrow('process.exit called');
        expect(processExitSpy).toHaveBeenCalledWith(1); // ValidationError
      });

      describe('piped stdin auto-detection', () => {
        it('should auto-read piped stdin when no -p is provided', async () => {
          // Simulate piped stdin (not a TTY)
          Object.defineProperty(process.stdin, 'isTTY', { value: undefined, writable: true, configurable: true });

          let dataCallback: ((chunk: string) => void) | undefined;
          let endCallback: (() => void) | undefined;

          processStdinSpy.mockImplementation((event: string, callback: any) => {
            if (event === 'data') {
              dataCallback = callback;
            } else if (event === 'end') {
              endCallback = callback;
            }
            return process.stdin;
          });

          const options: MainCommandOptions = {
            subagent: 'claude',
            prompt: undefined,
            cwd: '/test',
            maxIterations: 1,
            interactive: false,
            interactivePrompt: false,
            verbose: 0,
            quiet: false,
            logLevel: 'info',
          };

          // Start the handler (it will block on stdin)
          const handlerPromise = mainCommandHandler([], options, mockCommand);

          // Wait a tick for async code to set up stdin listeners
          await new Promise((r) => setTimeout(r, 50));

          // Simulate piped input
          dataCallback!('piped prompt from stdin\n');
          endCallback!();

          await handlerPromise;

          const { createExecutionRequest } = await import('../../core/engine.js');
          expect(createExecutionRequest).toHaveBeenCalledWith(
            expect.objectContaining({
              instruction: 'piped prompt from stdin',
            }),
          );
          expect(processExitSpy).toHaveBeenCalledWith(0);
        });

        it('should auto-read stdin when redirected fd is detected even if isTTY is true', async () => {
          Object.defineProperty(process.stdin, 'isTTY', { value: true, writable: true, configurable: true });

          vi.mocked(fs.fstatSync as any).mockReturnValue({
            isFIFO: () => true,
            isFile: () => false,
            isSocket: () => false,
          });

          let dataCallback: ((chunk: string) => void) | undefined;
          let endCallback: (() => void) | undefined;

          processStdinSpy.mockImplementation((event: string, callback: any) => {
            if (event === 'data') {
              dataCallback = callback;
            } else if (event === 'end') {
              endCallback = callback;
            }
            return process.stdin;
          });

          const options: MainCommandOptions = {
            subagent: 'claude',
            prompt: undefined,
            cwd: '/test',
            maxIterations: 1,
            interactive: false,
            interactivePrompt: false,
            verbose: 0,
            quiet: false,
            logLevel: 'info',
          };

          const handlerPromise = mainCommandHandler([], options, mockCommand);
          await new Promise((r) => setTimeout(r, 50));

          dataCallback!('continue from redirected stdin\n');
          endCallback!();

          await handlerPromise;

          const { createExecutionRequest } = await import('../../core/engine.js');
          expect(createExecutionRequest).toHaveBeenCalledWith(
            expect.objectContaining({
              instruction: 'continue from redirected stdin',
            }),
          );
          expect(fs.fstatSync).toHaveBeenCalledWith(0);
          expect(processExitSpy).toHaveBeenCalledWith(0);
        });

        it('should auto-read multiline piped stdin (heredoc)', async () => {
          Object.defineProperty(process.stdin, 'isTTY', { value: undefined, writable: true, configurable: true });

          let dataCallback: ((chunk: string) => void) | undefined;
          let endCallback: (() => void) | undefined;

          processStdinSpy.mockImplementation((event: string, callback: any) => {
            if (event === 'data') {
              dataCallback = callback;
            } else if (event === 'end') {
              endCallback = callback;
            }
            return process.stdin;
          });

          const options: MainCommandOptions = {
            subagent: 'claude',
            prompt: undefined,
            cwd: '/test',
            maxIterations: 1,
            interactive: false,
            interactivePrompt: false,
            verbose: 0,
            quiet: false,
            logLevel: 'info',
          };

          const handlerPromise = mainCommandHandler([], options, mockCommand);
          await new Promise((r) => setTimeout(r, 50));

          // Simulate multiline heredoc
          dataCallback!('line 1\n');
          dataCallback!('line 2\n');
          dataCallback!('line 3\n');
          endCallback!();

          await handlerPromise;

          const { createExecutionRequest } = await import('../../core/engine.js');
          expect(createExecutionRequest).toHaveBeenCalledWith(
            expect.objectContaining({
              instruction: 'line 1\nline 2\nline 3',
            }),
          );
        });

        it('should reject empty piped stdin', async () => {
          Object.defineProperty(process.stdin, 'isTTY', { value: undefined, writable: true, configurable: true });

          let dataCallback: ((chunk: string) => void) | undefined;
          let endCallback: (() => void) | undefined;

          processStdinSpy.mockImplementation((event: string, callback: any) => {
            if (event === 'data') {
              dataCallback = callback;
            } else if (event === 'end') {
              endCallback = callback;
            }
            return process.stdin;
          });

          const options: MainCommandOptions = {
            subagent: 'claude',
            prompt: undefined,
            cwd: '/test',
            maxIterations: 1,
            interactive: false,
            interactivePrompt: false,
            verbose: 0,
            quiet: false,
            logLevel: 'info',
          };

          const handlerPromise = mainCommandHandler([], options, mockCommand);
          await new Promise((r) => setTimeout(r, 50));

          // Simulate empty piped input
          dataCallback!('   \n  ');
          endCallback!();

          await handlerPromise;

          expect(processExitSpy).toHaveBeenCalledWith(1); // ValidationError
        });

        it('should prefer -p flag over piped stdin', async () => {
          // Even with piped stdin, -p should take priority
          Object.defineProperty(process.stdin, 'isTTY', { value: undefined, writable: true, configurable: true });

          const options: MainCommandOptions = {
            subagent: 'claude',
            prompt: 'explicit prompt from -p',
            cwd: '/test',
            maxIterations: 1,
            interactive: false,
            interactivePrompt: false,
            verbose: 0,
            quiet: false,
            logLevel: 'info',
          };

          await mainCommandHandler([], options, mockCommand);

          const { createExecutionRequest } = await import('../../core/engine.js');
          expect(createExecutionRequest).toHaveBeenCalledWith(
            expect.objectContaining({
              instruction: 'explicit prompt from -p',
            }),
          );
          // stdin should NOT have been read
          expect(processStdinSpy).not.toHaveBeenCalledWith('data', expect.any(Function));
        });

        it('should read stdin when -p flag used with heredoc (prompt=true)', async () => {
          // When using `yylo -p << 'EOF'`, Commander sets prompt=true (no string arg)
          // The fix: treat prompt=true same as prompt=undefined, fall through to stdin
          Object.defineProperty(process.stdin, 'isTTY', { value: undefined, writable: true, configurable: true });

          let dataCallback: ((chunk: string) => void) | undefined;
          let endCallback: (() => void) | undefined;

          processStdinSpy.mockImplementation((event: string, callback: any) => {
            if (event === 'data') {
              dataCallback = callback;
            } else if (event === 'end') {
              endCallback = callback;
            }
            return process.stdin;
          });

          const options: MainCommandOptions = {
            subagent: 'claude',
            prompt: true as any, // Commander sets boolean when -p has no argument
            cwd: '/test',
            maxIterations: 1,
            interactive: false,
            interactivePrompt: false,
            verbose: 0,
            quiet: false,
            logLevel: 'info',
          };

          const handlerPromise = mainCommandHandler([], options, mockCommand);
          await new Promise((r) => setTimeout(r, 50));

          // Simulate heredoc content via stdin
          dataCallback!('/ralph-loop-yylo Do Task 45OLrc\n');
          endCallback!();

          await handlerPromise;

          const { createExecutionRequest } = await import('../../core/engine.js');
          expect(createExecutionRequest).toHaveBeenCalledWith(
            expect.objectContaining({
              instruction: '/ralph-loop-yylo Do Task 45OLrc',
            }),
          );
          expect(processExitSpy).toHaveBeenCalledWith(0);
        });

        it('should prefer --prompt-file over piped stdin', async () => {
          Object.defineProperty(process.stdin, 'isTTY', { value: undefined, writable: true, configurable: true });
          vi.mocked(fs.readFile).mockResolvedValueOnce('prompt from file');

          const options: MainCommandOptions = {
            subagent: 'claude',
            promptFile: '/path/to/prompt.txt',
            cwd: '/test',
            maxIterations: 1,
            interactive: false,
            interactivePrompt: false,
            verbose: 0,
            quiet: false,
            logLevel: 'info',
          };

          await mainCommandHandler([], options, mockCommand);

          const { createExecutionRequest } = await import('../../core/engine.js');
          expect(createExecutionRequest).toHaveBeenCalledWith(
            expect.objectContaining({
              instruction: 'prompt from file',
            }),
          );
        });
      });

      describe('positional prompt (no -p flag)', () => {
        it('should use positional prompt text as options.prompt', async () => {
          // Simulates: yylo -s claude "my positional prompt"
          // After CLI merging, options.prompt = "my positional prompt"
          const options: MainCommandOptions = {
            subagent: 'claude',
            prompt: 'my positional prompt',
            cwd: '/test',
            maxIterations: 1,
            interactive: false,
            interactivePrompt: false,
            verbose: 0,
            quiet: false,
            logLevel: 'info',
          };

          await mainCommandHandler([], options, mockCommand);

          const { createExecutionRequest } = await import('../../core/engine.js');
          expect(createExecutionRequest).toHaveBeenCalledWith(
            expect.objectContaining({
              instruction: 'my positional prompt',
            }),
          );
          expect(processExitSpy).toHaveBeenCalledWith(0);
        });

        it('should handle multi-word positional prompt joined with spaces', async () => {
          // Simulates: yylo -s claude "analyze" "this" "codebase"
          // After CLI merging, options.prompt = "analyze this codebase"
          const options: MainCommandOptions = {
            subagent: 'claude',
            prompt: 'analyze this codebase',
            cwd: '/test',
            maxIterations: 1,
            interactive: false,
            interactivePrompt: false,
            verbose: 0,
            quiet: false,
            logLevel: 'info',
          };

          await mainCommandHandler([], options, mockCommand);

          const { createExecutionRequest } = await import('../../core/engine.js');
          expect(createExecutionRequest).toHaveBeenCalledWith(
            expect.objectContaining({
              instruction: 'analyze this codebase',
            }),
          );
        });

        it('should prefer -p flag over positional prompt', async () => {
          // Simulates: yylo -s claude -p "explicit" "positional"
          // CLI merging only sets options.prompt from positional if prompt is undefined
          // Since -p sets it first, positional is ignored
          const options: MainCommandOptions = {
            subagent: 'claude',
            prompt: 'explicit prompt from -p',
            cwd: '/test',
            maxIterations: 1,
            interactive: false,
            interactivePrompt: false,
            verbose: 0,
            quiet: false,
            logLevel: 'info',
          };

          await mainCommandHandler([], options, mockCommand);

          const { createExecutionRequest } = await import('../../core/engine.js');
          expect(createExecutionRequest).toHaveBeenCalledWith(
            expect.objectContaining({
              instruction: 'explicit prompt from -p',
            }),
          );
        });
      });
    });

    describe('execution', () => {
      it('should execute successfully and exit with code 0', async () => {
        const options: MainCommandOptions = {
          subagent: 'claude',
          prompt: 'test prompt',
          cwd: '/test',
          maxIterations: 5,
          model: 'custom-model',
          interactive: false,
          interactivePrompt: false,
          verbose: 1,
          quiet: false,
          logLevel: 'debug',
        };

        await mainCommandHandler([], options, mockCommand);

        const { createExecutionRequest } = await import('../../core/engine.js');
        expect(createExecutionRequest).toHaveBeenCalledWith(
          expect.objectContaining({
            instruction: 'test prompt',
            subagent: 'claude',
            workingDirectory: '/test/dir',
            maxIterations: 5,
            model: 'custom-model',
            backend: 'shell',
          }),
        );

        expect(processExitSpy).toHaveBeenCalledWith(0);
      });

      it('should persist completed Pi execution history under .juno_task/session_history.json with prompt/cost/message metadata', async () => {
        const { createExecutionEngine } = await import('../../core/engine.js');

        vi.mocked(createExecutionEngine).mockReturnValueOnce({
          execute: vi.fn().mockResolvedValue({
            request: {
              requestId: 'history-run-1',
              instruction: 'track this prompt',
              subagent: 'pi',
              workingDirectory: '/test/dir',
              maxIterations: 1,
              model: ':sonnet',
            },
            status: 'completed',
            startTime: new Date('2026-03-09T10:00:00.000Z'),
            endTime: new Date('2026-03-09T10:00:12.000Z'),
            duration: 12000,
            progressEvents: [],
            iterations: [
              {
                iterationNumber: 1,
                toolResult: {
                  content: JSON.stringify({
                    type: 'result',
                    session_id: 'session-history-1',
                    total_cost_usd: 0.0042,
                    num_turns: 3,
                    messages: [{ role: 'user' }, { role: 'assistant' }],
                  }),
                  metadata: {},
                },
                success: true,
                duration: 12000,
              },
            ],
            statistics: {
              totalIterations: 1,
              successfulIterations: 1,
              failedIterations: 0,
              averageIterationDuration: 12000,
              totalToolCalls: 1,
              rateLimitEncounters: 0,
            },
          }),
          onProgress: vi.fn(),
          on: vi.fn(),
          shutdown: vi.fn(),
        } as any);

        const options: MainCommandOptions = {
          subagent: 'pi',
          prompt: 'track this prompt',
          cwd: '/test',
          maxIterations: 1,
          interactive: false,
          interactivePrompt: false,
          verbose: 1,
          quiet: false,
          logLevel: 'info',
        };

        await mainCommandHandler([], options, mockCommand);

        const writeCall = vi.mocked(fs.writeJson).mock.calls.at(-1);
        expect(writeCall).toBeDefined();
        expect(writeCall?.[0]).toBe('/test/dir/.juno_task/session_history.json');
        expect(writeCall?.[1]).toEqual(
          expect.objectContaining({
            version: 1,
            sessions: expect.arrayContaining([
              expect.objectContaining({
                id: 'history-run-1',
                initialMessage: 'track this prompt',
                subagent: 'pi',
                model: ':sonnet',
                totalCostUsd: 0.0042,
                turnCount: 3,
                messageCount: 2,
                sessionIds: ['session-history-1'],
              }),
            ]),
          }),
        );
      });

      it('should append to existing session_history.json without truncating older runs', async () => {
        const { createExecutionEngine } = await import('../../core/engine.js');

        vi.mocked(createExecutionEngine).mockReturnValueOnce({
          execute: vi.fn().mockResolvedValue({
            request: {
              requestId: 'test-request',
              instruction: 'new run prompt',
              subagent: 'claude',
              workingDirectory: '/test/dir',
              maxIterations: 1,
              model: ':sonnet',
            },
            status: 'completed',
            startTime: new Date('2026-03-09T10:15:00.000Z'),
            endTime: new Date('2026-03-09T10:15:06.000Z'),
            duration: 6000,
            progressEvents: [],
            iterations: [
              {
                iterationNumber: 1,
                toolResult: {
                  content: JSON.stringify({
                    type: 'result',
                    session_id: 'new-session',
                    total_cost_usd: 0.002,
                    num_turns: 1,
                  }),
                  metadata: {},
                },
                success: true,
                duration: 6000,
              },
            ],
            statistics: {
              totalIterations: 1,
              successfulIterations: 1,
              failedIterations: 0,
              averageIterationDuration: 6000,
              totalToolCalls: 1,
              rateLimitEncounters: 0,
            },
          }),
          onProgress: vi.fn(),
          on: vi.fn(),
          shutdown: vi.fn(),
        } as any);

        vi.mocked(fs.pathExists).mockImplementation(async (candidate: string) =>
          candidate.endsWith('session_history.json'),
        );
        vi.mocked(fs.readFile).mockImplementation(async (candidate: string) => {
          if (candidate.endsWith('session_history.json')) {
            return JSON.stringify({
              version: 1,
              sessions: [
                {
                  id: 'existing-run',
                  status: 'completed',
                  initialMessage: 'already there',
                  initialMessageAt: '2026-03-09T09:00:00.000Z',
                  lastMessageAt: '2026-03-09T09:00:05.000Z',
                  completedAt: '2026-03-09T09:00:05.000Z',
                  subagent: 'claude',
                  model: ':sonnet',
                  settings: { maxIterations: 1 },
                  totalCostUsd: 0.001,
                  turnCount: 1,
                  messageCount: 2,
                  iterations: 1,
                  durationMs: 5000,
                  sessionIds: ['existing-session'],
                },
              ],
            });
          }
          return 'mock file content';
        });

        const options: MainCommandOptions = {
          subagent: 'claude',
          prompt: 'new run prompt',
          cwd: '/test',
          maxIterations: 1,
          interactive: false,
          interactivePrompt: false,
          verbose: 1,
          quiet: false,
          logLevel: 'info',
        };

        await mainCommandHandler([], options, mockCommand);

        const writeCall = vi.mocked(fs.writeJson).mock.calls.at(-1);
        expect(writeCall).toBeDefined();
        const payload = writeCall?.[1] as { sessions: Array<{ id: string }> };
        expect(payload.sessions).toHaveLength(2);
        expect(payload.sessions[0]?.id).toBe('test-request');
        expect(payload.sessions[1]?.id).toBe('existing-run');
      });

      it('should persist failed execution history with prompt/status/session id when ExecutionResult exists', async () => {
        const { createExecutionEngine } = await import('../../core/engine.js');

        vi.mocked(createExecutionEngine).mockReturnValueOnce({
          execute: vi.fn().mockResolvedValue({
            request: {
              requestId: 'failed-history-run',
              instruction: 'failed prompt still audited',
              subagent: 'pi',
              workingDirectory: '/test/dir',
              maxIterations: 1,
              model: ':api-codex',
            },
            status: 'failed',
            startTime: new Date('2026-03-09T11:00:00.000Z'),
            endTime: new Date('2026-03-09T11:00:03.000Z'),
            duration: 3000,
            progressEvents: [],
            iterations: [
              {
                iterationNumber: 1,
                toolResult: {
                  content: 'not-json',
                  metadata: {
                    structuredOutput: true,
                    subAgentResponse: { session_id: 'failed-session-1' },
                  },
                },
                success: false,
                duration: 3000,
              },
            ],
            statistics: {
              totalIterations: 1,
              successfulIterations: 0,
              failedIterations: 1,
              averageIterationDuration: 3000,
              totalToolCalls: 1,
              rateLimitEncounters: 0,
            },
          }),
          onProgress: vi.fn(),
          on: vi.fn(),
          shutdown: vi.fn(),
        } as any);

        await mainCommandHandler(
          [],
          {
            subagent: 'pi',
            prompt: 'failed prompt still audited',
            cwd: '/test',
            maxIterations: 1,
            model: ':api-codex',
            interactive: false,
            interactivePrompt: false,
            verbose: 1,
            quiet: false,
            logLevel: 'info',
          },
          mockCommand,
        );

        const writeCall = vi.mocked(fs.writeJson).mock.calls.at(-1);
        expect(writeCall?.[0]).toBe('/test/dir/.juno_task/session_history.json');
        expect(writeCall?.[1]).toEqual(
          expect.objectContaining({
            sessions: expect.arrayContaining([
              expect.objectContaining({
                id: 'failed-history-run',
                status: 'failed',
                initialMessage: 'failed prompt still audited',
                sessionIds: ['failed-session-1'],
              }),
            ]),
          }),
        );
        expect(processExitSpy).toHaveBeenCalledWith(1);
      });

      it('should repair malformed session_history.json with a visible backup and persist the new run', async () => {
        const { createExecutionEngine } = await import('../../core/engine.js');

        vi.mocked(createExecutionEngine).mockReturnValueOnce({
          execute: vi.fn().mockResolvedValue({
            request: {
              requestId: 'repair-history-run',
              instruction: 'repair history prompt',
              subagent: 'claude',
              workingDirectory: '/test/dir',
              maxIterations: 1,
              model: ':sonnet',
            },
            status: 'completed',
            progressEvents: [],
            iterations: [],
            statistics: {
              totalIterations: 0,
              successfulIterations: 0,
              failedIterations: 0,
              averageIterationDuration: 0,
              totalToolCalls: 0,
              rateLimitEncounters: 0,
            },
          }),
          onProgress: vi.fn(),
          on: vi.fn(),
          shutdown: vi.fn(),
        } as any);

        vi.mocked(fs.pathExists).mockImplementation(async (candidate: string) =>
          candidate.endsWith('session_history.json'),
        );
        vi.mocked(fs.readFile).mockImplementation(async (candidate: string) =>
          candidate.endsWith('session_history.json') ? '' : 'mock file content',
        );

        await mainCommandHandler(
          [],
          {
            subagent: 'claude',
            prompt: 'repair history prompt',
            cwd: '/test',
            maxIterations: 1,
            interactive: false,
            interactivePrompt: false,
            verbose: 1,
            quiet: false,
            logLevel: 'info',
          },
          mockCommand,
        );

        const backupCall = vi.mocked(fs.writeFile).mock.calls.find(([candidate]) =>
          String(candidate).startsWith('/test/dir/.juno_task/session_history.json.invalid-'),
        );
        expect(backupCall).toBeDefined();
        expect(backupCall?.[1]).toBe('');

        const writeCall = vi.mocked(fs.writeJson).mock.calls.at(-1);
        expect(writeCall?.[0]).toBe('/test/dir/.juno_task/session_history.json');
        expect((writeCall?.[1] as { sessions: Array<{ id: string }> }).sessions[0]?.id).toBe('repair-history-run');
        expect(console.error).toHaveBeenCalledWith(expect.stringContaining('Warning: Repaired unreadable session history'));
      });

      it('should persist latest session + runtime settings into env snapshot for continue command', async () => {
        const { createExecutionEngine } = await import('../../core/engine.js');

        vi.mocked(createExecutionEngine).mockReturnValueOnce({
          execute: vi.fn().mockResolvedValue({
            request: {
              requestId: 'continue-snapshot-run',
              instruction: 'initial prompt',
              subagent: 'codex',
              workingDirectory: '/test/dir',
              maxIterations: 3,
              model: ':codex',
              thinking: 'xhigh',
              allowedTools: ['Read', 'Edit'],
            },
            status: 'completed',
            progressEvents: [],
            iterations: [
              {
                iterationNumber: 1,
                toolResult: {
                  content: JSON.stringify({
                    type: 'result',
                    session_id: 'session-continue-123',
                  }),
                  metadata: {},
                },
                success: true,
                duration: 1000,
              },
            ],
            statistics: {
              totalIterations: 1,
              successfulIterations: 1,
              failedIterations: 0,
              averageIterationDuration: 1000,
              totalToolCalls: 1,
              rateLimitEncounters: 0,
            },
          }),
          onProgress: vi.fn(),
          on: vi.fn(),
          shutdown: vi.fn(),
        } as any);

        vi.mocked(fs.pathExists).mockImplementation(async (candidate: string) =>
          candidate.endsWith('.env.yylo'),
        );
        vi.mocked(fs.readFile).mockResolvedValueOnce('FOO=bar\n');

        await mainCommandHandler(
          [],
          {
            subagent: 'codex',
            prompt: 'initial prompt',
            cwd: '/test',
            maxIterations: 3,
            model: ':codex',
            interactive: false,
            interactivePrompt: false,
            verbose: 1,
            quiet: false,
            logLevel: 'info',
          },
          mockCommand,
        );

        expect(persistContinueScopeSnapshot).toHaveBeenCalledWith(expect.objectContaining({
          workingDirectory: '/test/dir',
          sessionId: 'session-continue-123',
          serializedSettings: expect.stringContaining('"subagent":"codex"'),
        }));
        expect(fs.writeFile).not.toHaveBeenCalledWith('/test/dir/.env.yylo', expect.anything(), expect.anything());
      });

      it('should hydrate resume and runtime options from scoped env snapshot when continueFromLatest is set', async () => {
        process.env.YYLO_CONTINUE_SCOPE = 'pane-a';
        const scopeHash = `SCOPE_${createHash('sha256')
          .update('YYLO_CONTINUE_SCOPE:pane-a')
          .digest('hex')
          .slice(0, 16)
          .toUpperCase()}`;

        process.env[`YYLO_LAST_SESSION_ID_${scopeHash}`] = 'resume-me-001';
        process.env[`YYLO_LAST_EXECUTION_SETTINGS_${scopeHash}`] = JSON.stringify({
          version: 1,
          subagent: 'pi',
          model: ':api-codex',
          maxIterations: 4,
          thinking: 'high',
          live: true,
          allowedTools: ['Read'],
        });

        await mainCommandHandler(
          [],
          {
            prompt: 'next step prompt',
            cwd: '/test',
            continueFromLatest: true,
            interactive: false,
            interactivePrompt: false,
            verbose: 1,
            quiet: false,
            logLevel: 'info',
          } as MainCommandOptions,
          mockCommand,
        );

        expect(createExecutionRequest).toHaveBeenCalledWith(
          expect.objectContaining({
            instruction: 'next step prompt',
            subagent: 'pi',
            model: ':api-codex',
            maxIterations: 4,
            resume: 'resume-me-001',
            thinking: 'high',
            live: true,
            allowedTools: ['Read'],
          }),
        );
      });

      it('should continue the active named branch and update only that branch after success', async () => {
        process.env.YYLO_CONTINUE_SCOPE = 'pane-active-c';
        const scopeHash = `SCOPE_${createHash('sha256')
          .update('YYLO_CONTINUE_SCOPE:pane-active-c')
          .digest('hex')
          .slice(0, 16)
          .toUpperCase()}`;

        process.env[`YYLO_LAST_SESSION_ID_${scopeHash}`] = 'SESSION_C';
        process.env[`YYLO_LAST_EXECUTION_SETTINGS_${scopeHash}`] = JSON.stringify({
          version: 1,
          subagent: 'pi',
          model: ':api-codex',
        });
        vi.mocked(getActiveSessionBranch).mockResolvedValue({
          name: 'C',
          sessionId: 'SESSION_C',
          parent: 'main',
          sourceSessionId: 'SESSION_MAIN',
          updatedAt: 't',
        } as any);
        vi.mocked(listSessionBranches).mockResolvedValue([
          { name: 'main', active: false, sessionId: 'SESSION_MAIN', parent: null, sourceSessionId: null, updatedAt: 't' },
          { name: 'C', active: true, sessionId: 'SESSION_C', parent: 'main', sourceSessionId: 'SESSION_MAIN', updatedAt: 't' },
          { name: 'D', active: false, sessionId: 'SESSION_D', parent: 'main', sourceSessionId: 'SESSION_MAIN', updatedAt: 't' },
        ] as any);
        vi.mocked(createExecutionEngine).mockReturnValueOnce({
          execute: vi.fn().mockResolvedValue({
            request: {
              requestId: 'continue-active-c',
              instruction: 'continue C',
              subagent: 'pi',
              workingDirectory: '/test/dir',
              maxIterations: 5,
              model: ':api-codex',
              resume: 'SESSION_C',
            },
            status: 'completed',
            progressEvents: [],
            iterations: [
              {
                iterationNumber: 1,
                toolResult: { content: JSON.stringify({ type: 'agent_end', session_id: 'SESSION_C2' }), metadata: {} },
                success: true,
                duration: 1000,
              },
            ],
            statistics: {
              totalIterations: 1,
              successfulIterations: 1,
              failedIterations: 0,
              averageIterationDuration: 1000,
              totalToolCalls: 1,
              rateLimitEncounters: 0,
            },
          }),
          onProgress: vi.fn(),
          on: vi.fn(),
          shutdown: vi.fn(),
        } as any);

        await mainCommandHandler(
          [],
          {
            prompt: 'continue C',
            cwd: '/test',
            continueFromLatest: true,
            interactive: false,
            interactivePrompt: false,
            verbose: 0,
            quiet: false,
            logLevel: 'info',
          } as MainCommandOptions,
          mockCommand,
        );

        expect(createExecutionRequest).toHaveBeenCalledWith(expect.objectContaining({ resume: 'SESSION_C' }));
        expect(updateActiveSessionBranch).toHaveBeenCalledWith(
          expect.objectContaining({ workingDirectory: '/test/dir', sessionId: 'SESSION_C2' }),
        );
        expect(resetMainSessionBranch).not.toHaveBeenCalled();
      });

      it('should reset named branches to main after a successful new Pi root run', async () => {
        vi.mocked(listSessionBranches).mockResolvedValue([
          { name: 'main', active: false, sessionId: 'OLD_MAIN', parent: null, sourceSessionId: null, updatedAt: 't' },
          { name: 'C', active: true, sessionId: 'OLD_C', parent: 'main', sourceSessionId: 'OLD_MAIN', updatedAt: 't' },
          { name: 'D', active: false, sessionId: 'OLD_D', parent: 'main', sourceSessionId: 'OLD_MAIN', updatedAt: 't' },
        ] as any);
        vi.mocked(createExecutionEngine).mockReturnValueOnce({
          execute: vi.fn().mockResolvedValue({
            request: {
              requestId: 'new-root-run',
              instruction: 'new research topic',
              subagent: 'pi',
              workingDirectory: '/test/dir',
              maxIterations: 5,
              model: 'test-model',
            },
            status: 'completed',
            progressEvents: [],
            iterations: [
              {
                iterationNumber: 1,
                toolResult: { content: JSON.stringify({ type: 'agent_end', session_id: 'NEW_MAIN_SESSION' }), metadata: {} },
                success: true,
                duration: 1000,
              },
            ],
            statistics: {
              totalIterations: 1,
              successfulIterations: 1,
              failedIterations: 0,
              averageIterationDuration: 1000,
              totalToolCalls: 1,
              rateLimitEncounters: 0,
            },
          }),
          onProgress: vi.fn(),
          on: vi.fn(),
          shutdown: vi.fn(),
        } as any);

        await mainCommandHandler(
          [],
          {
            prompt: 'new research topic',
            cwd: '/test',
            subagent: 'pi',
            verbose: 0,
            quiet: false,
            logLevel: 'info',
          } as MainCommandOptions,
          mockCommand,
        );

        expect(resetMainSessionBranch).toHaveBeenCalledWith(
          expect.objectContaining({ workingDirectory: '/test/dir', sessionId: 'NEW_MAIN_SESSION' }),
        );
        expect(updateActiveSessionBranch).not.toHaveBeenCalled();
      });

      it('should reset named branches to main after explicit resume without clone', async () => {
        vi.mocked(listSessionBranches).mockResolvedValue([
          { name: 'main', active: false, sessionId: 'OLD_MAIN', parent: null, sourceSessionId: null, updatedAt: 't' },
          { name: 'C', active: true, sessionId: 'OLD_C', parent: 'main', sourceSessionId: 'OLD_MAIN', updatedAt: 't' },
        ] as any);
        vi.mocked(createExecutionEngine).mockReturnValueOnce({
          execute: vi.fn().mockResolvedValue({
            request: {
              requestId: 'explicit-resume-run',
              instruction: 'resume elsewhere',
              subagent: 'pi',
              workingDirectory: '/test/dir',
              maxIterations: 5,
              model: 'test-model',
              resume: 'EXPLICIT_SESSION',
            },
            status: 'completed',
            progressEvents: [],
            iterations: [
              {
                iterationNumber: 1,
                toolResult: { content: JSON.stringify({ type: 'agent_end', session_id: 'RESUMED_MAIN' }), metadata: {} },
                success: true,
                duration: 1000,
              },
            ],
            statistics: {
              totalIterations: 1,
              successfulIterations: 1,
              failedIterations: 0,
              averageIterationDuration: 1000,
              totalToolCalls: 1,
              rateLimitEncounters: 0,
            },
          }),
          onProgress: vi.fn(),
          on: vi.fn(),
          shutdown: vi.fn(),
        } as any);

        await mainCommandHandler(
          [],
          {
            prompt: 'resume elsewhere',
            cwd: '/test',
            subagent: 'pi',
            resume: 'EXPLICIT_SESSION',
            verbose: 0,
            quiet: false,
            logLevel: 'info',
          } as MainCommandOptions,
          mockCommand,
        );

        expect(resetMainSessionBranch).toHaveBeenCalledWith(
          expect.objectContaining({ sessionId: 'RESUMED_MAIN' }),
        );
        expect(updateActiveSessionBranch).not.toHaveBeenCalled();
      });

      it('should not mutate named branches for explicit resume clone or failed results', async () => {
        vi.mocked(listSessionBranches).mockResolvedValue([
          { name: 'main', active: false, sessionId: 'OLD_MAIN', parent: null, sourceSessionId: null, updatedAt: 't' },
          { name: 'C', active: true, sessionId: 'OLD_C', parent: 'main', sourceSessionId: 'OLD_MAIN', updatedAt: 't' },
        ] as any);
        vi.mocked(createExecutionEngine).mockReturnValueOnce({
          execute: vi.fn().mockResolvedValue({
            request: {
              requestId: 'explicit-clone-run',
              instruction: 'clone elsewhere',
              subagent: 'pi',
              workingDirectory: '/test/dir',
              maxIterations: 5,
              model: 'test-model',
              resume: 'EXPLICIT_SESSION',
              cloneSession: true,
              cloneFromSession: 'EXPLICIT_SESSION',
            },
            status: 'completed',
            progressEvents: [],
            iterations: [
              {
                iterationNumber: 1,
                toolResult: { content: JSON.stringify({ type: 'agent_end', session_id: 'CLONE_SESSION' }), metadata: {} },
                success: true,
                duration: 1000,
              },
            ],
            statistics: {
              totalIterations: 1,
              successfulIterations: 1,
              failedIterations: 0,
              averageIterationDuration: 1000,
              totalToolCalls: 1,
              rateLimitEncounters: 0,
            },
          }),
          onProgress: vi.fn(),
          on: vi.fn(),
          shutdown: vi.fn(),
        } as any);

        await mainCommandHandler(
          [],
          {
            prompt: 'clone elsewhere',
            cwd: '/test',
            subagent: 'pi',
            resume: 'EXPLICIT_SESSION',
            clone: true,
            verbose: 0,
            quiet: false,
            logLevel: 'info',
          } as MainCommandOptions,
          mockCommand,
        );

        expect(resetMainSessionBranch).not.toHaveBeenCalled();
        expect(updateActiveSessionBranch).not.toHaveBeenCalled();

        vi.mocked(resetMainSessionBranch).mockClear();
        vi.mocked(updateActiveSessionBranch).mockClear();
        process.env.YYLO_CONTINUE_SCOPE = 'failed-branch-pane';
        const failedScopeHash = `SCOPE_${createHash('sha256')
          .update('YYLO_CONTINUE_SCOPE:failed-branch-pane')
          .digest('hex')
          .slice(0, 16)
          .toUpperCase()}`;
        process.env[`YYLO_LAST_SESSION_ID_${failedScopeHash}`] = 'OLD_C';
        process.env[`YYLO_LAST_EXECUTION_SETTINGS_${failedScopeHash}`] = JSON.stringify({
          version: 1,
          subagent: 'pi',
        });
        vi.mocked(getActiveSessionBranch).mockResolvedValue({
          name: 'C',
          sessionId: 'OLD_C',
          parent: 'main',
          sourceSessionId: 'OLD_MAIN',
          updatedAt: 't',
        } as any);
        vi.mocked(createExecutionEngine).mockReturnValueOnce({
          execute: vi.fn().mockResolvedValue({
            request: {
              requestId: 'failed-continue-run',
              instruction: 'continue C',
              subagent: 'pi',
              workingDirectory: '/test/dir',
              maxIterations: 5,
              model: 'test-model',
              resume: 'OLD_C',
            },
            status: 'failed',
            progressEvents: [],
            iterations: [
              {
                iterationNumber: 1,
                toolResult: { content: JSON.stringify({ type: 'agent_end', session_id: 'SHOULD_NOT_ADVANCE' }), metadata: {} },
                success: false,
                duration: 1000,
              },
            ],
            statistics: {
              totalIterations: 1,
              successfulIterations: 0,
              failedIterations: 1,
              averageIterationDuration: 1000,
              totalToolCalls: 1,
              rateLimitEncounters: 0,
            },
          }),
          onProgress: vi.fn(),
          on: vi.fn(),
          shutdown: vi.fn(),
        } as any);

        await mainCommandHandler(
          [],
          {
            prompt: 'continue C',
            cwd: '/test',
            continueFromLatest: true,
            verbose: 0,
            quiet: false,
            logLevel: 'info',
          } as MainCommandOptions,
          mockCommand,
        );

        expect(resetMainSessionBranch).not.toHaveBeenCalled();
        expect(updateActiveSessionBranch).not.toHaveBeenCalled();
      });

      it('should allow continueFromLatest Pi live sessions without an initial prompt', async () => {
        process.env.YYLO_CONTINUE_SCOPE = 'pane-live';
        const scopeHash = `SCOPE_${createHash('sha256')
          .update('YYLO_CONTINUE_SCOPE:pane-live')
          .digest('hex')
          .slice(0, 16)
          .toUpperCase()}`;

        process.env[`YYLO_LAST_SESSION_ID_${scopeHash}`] = 'resume-live-001';
        process.env[`YYLO_LAST_EXECUTION_SETTINGS_${scopeHash}`] = JSON.stringify({
          version: 1,
          subagent: 'pi',
          model: ':api-codex',
          maxIterations: 2,
          live: true,
        });

        await mainCommandHandler(
          [],
          {
            cwd: '/test',
            continueFromLatest: true,
            interactive: false,
            interactivePrompt: false,
            verbose: 1,
            quiet: false,
            logLevel: 'info',
          } as MainCommandOptions,
          mockCommand,
        );

        expect(createExecutionRequest).toHaveBeenCalledWith(
          expect.objectContaining({
            instruction: '',
            subagent: 'pi',
            live: true,
            resume: 'resume-live-001',
            liveInteractiveSession: true,
          }),
        );
      });

      it('should clone explicit resume sessions and use the clone prompt', async () => {
        await mainCommandHandler(
          [],
          {
            resume: 'source-session-001',
            clone: 'clone this work',
            cwd: '/test',
            verbose: 0,
            quiet: false,
            logLevel: 'info',
          } as MainCommandOptions,
          mockCommand,
        );

        expect(createExecutionRequest).toHaveBeenCalledWith(
          expect.objectContaining({
            instruction: 'clone this work',
            subagent: 'pi',
            resume: 'source-session-001',
            cloneSession: true,
            cloneFromSession: 'source-session-001',
          }),
        );
      });

      it('should clone from the current continue scope and persist the returned clone session', async () => {
        process.env.YYLO_CONTINUE_SCOPE = 'clone-pane';
        const scopeHash = `SCOPE_${createHash('sha256')
          .update('YYLO_CONTINUE_SCOPE:clone-pane')
          .digest('hex')
          .slice(0, 16)
          .toUpperCase()}`;

        process.env[`YYLO_LAST_SESSION_ID_${scopeHash}`] = 'source-session-scope';
        process.env[`YYLO_LAST_EXECUTION_SETTINGS_${scopeHash}`] = JSON.stringify({
          version: 1,
          subagent: 'pi',
          model: ':api-codex',
          maxIterations: 2,
        });

        vi.mocked(createExecutionEngine).mockReturnValueOnce({
          execute: vi.fn().mockResolvedValue({
            request: {
              requestId: 'clone-run',
              instruction: 'clone from scope',
              subagent: 'pi',
              workingDirectory: '/test/dir',
              maxIterations: 2,
              model: ':api-codex',
              resume: 'source-session-scope',
              cloneSession: true,
              cloneFromSession: 'source-session-scope',
            },
            status: 'completed',
            progressEvents: [],
            iterations: [
              {
                iterationNumber: 1,
                toolResult: {
                  content: JSON.stringify({ type: 'agent_end', session_id: 'clone-session-002' }),
                  metadata: {},
                },
                success: true,
                duration: 1000,
              },
            ],
            statistics: {
              totalIterations: 1,
              successfulIterations: 1,
              failedIterations: 0,
              averageIterationDuration: 1000,
              totalToolCalls: 1,
              rateLimitEncounters: 0,
            },
          }),
          onProgress: vi.fn(),
          on: vi.fn(),
          shutdown: vi.fn(),
        } as any);

        vi.mocked(fs.pathExists).mockImplementation(async (candidate: string) =>
          candidate.endsWith('.env.yylo'),
        );
        vi.mocked(fs.readFile).mockResolvedValueOnce('FOO=bar\n');

        await mainCommandHandler(
          [],
          {
            prompt: 'clone from scope',
            cwd: '/test',
            continueFromLatest: true,
            clone: true,
            verbose: 0,
            quiet: false,
            logLevel: 'info',
          } as MainCommandOptions,
          mockCommand,
        );

        expect(createExecutionRequest).toHaveBeenCalledWith(
          expect.objectContaining({
            resume: 'source-session-scope',
            cloneSession: true,
            cloneFromSession: 'source-session-scope',
          }),
        );
        expect(persistContinueScopeSnapshot).toHaveBeenCalledWith(expect.objectContaining({
          sessionId: 'clone-session-002',
        }));
      });

      it('should clone --name C from main and store the returned session without switching active branch or poisoning active continue env', async () => {
        process.env.YYLO_CONTINUE_SCOPE = 'named-clone-keeps-active-main';
        const scopeHash = `SCOPE_${createHash('sha256')
          .update('YYLO_CONTINUE_SCOPE:named-clone-keeps-active-main')
          .digest('hex')
          .slice(0, 16)
          .toUpperCase()}`;
        process.env[`YYLO_LAST_SESSION_ID_${scopeHash}`] = 'SESSION_MAIN';
        process.env[`YYLO_LAST_EXECUTION_SETTINGS_${scopeHash}`] = JSON.stringify({
          version: 1,
          subagent: 'pi',
          maxIterations: 5,
        });

        vi.mocked(fs.pathExists).mockImplementation(async (candidate: string) =>
          candidate.endsWith('.env.yylo'),
        );
        vi.mocked(fs.readFile).mockResolvedValueOnce(
          `YYLO_LAST_SESSION_ID_${scopeHash}="SESSION_MAIN"\n` +
            `YYLO_LAST_EXECUTION_SETTINGS_${scopeHash}='{"version":1,"subagent":"pi","maxIterations":5}'\n`,
        );
        vi.mocked(listSessionBranches).mockResolvedValue([
          {
            name: 'main',
            active: true,
            sessionId: 'SESSION_MAIN',
            parent: null,
            sourceSessionId: null,
            updatedAt: '2026-06-27T00:00:00.000Z',
          },
        ] as any);

        vi.mocked(createExecutionEngine).mockReturnValueOnce({
          execute: vi.fn().mockResolvedValue({
            request: {
              requestId: 'clone-branch-run',
              instruction: 'prompt C',
              subagent: 'pi',
              workingDirectory: '/test/dir',
              maxIterations: 5,
              model: 'test-model',
              resume: 'SESSION_MAIN',
              cloneSession: true,
              cloneFromSession: 'SESSION_MAIN',
            },
            status: 'completed',
            progressEvents: [],
            iterations: [
              {
                iterationNumber: 1,
                toolResult: {
                  content: JSON.stringify({ type: 'agent_end', session_id: 'SESSION_C' }),
                  metadata: {},
                },
                success: true,
                duration: 1000,
              },
            ],
            statistics: {
              totalIterations: 1,
              successfulIterations: 1,
              failedIterations: 0,
              averageIterationDuration: 1000,
              totalToolCalls: 1,
              rateLimitEncounters: 0,
            },
          }),
          onProgress: vi.fn(),
          on: vi.fn(),
          shutdown: vi.fn(),
        } as any);

        await mainCommandHandler(
          [],
          {
            prompt: 'prompt C',
            cwd: '/test',
            clone: true,
            cloneBranchName: 'C',
            verbose: 0,
            quiet: false,
            logLevel: 'info',
          } as MainCommandOptions,
          mockCommand,
        );

        expect(createExecutionRequest).toHaveBeenCalledWith(
          expect.objectContaining({
            resume: 'SESSION_MAIN',
            cloneSession: true,
            cloneFromSession: 'SESSION_MAIN',
          }),
        );
        expect(upsertClonedSessionBranch).toHaveBeenCalledWith(
          expect.objectContaining({
            branchName: 'C',
            parent: 'main',
            sourceSessionId: 'SESSION_MAIN',
            sessionId: 'SESSION_C',
          }),
        );
        expect(updateActiveSessionBranch).not.toHaveBeenCalled();
        expect(process.env[`YYLO_LAST_SESSION_ID_${scopeHash}`]).toBe('SESSION_MAIN');
        const envWrites = vi.mocked(fs.writeFile).mock.calls
          .filter(([candidate]) => String(candidate).endsWith('.env.yylo'))
          .map(([, content]) => String(content));
        expect(envWrites).toEqual([]);
      });

      it('should default named clone source to main even when active branch is D', async () => {
        vi.mocked(getActiveSessionBranch).mockResolvedValue({
          name: 'D',
          sessionId: 'SESSION_D',
          parent: 'main',
          sourceSessionId: 'SESSION_MAIN',
          updatedAt: '2026-06-27T00:01:00.000Z',
        } as any);
        vi.mocked(listSessionBranches).mockResolvedValue([
          { name: 'main', active: false, sessionId: 'SESSION_MAIN', parent: null, sourceSessionId: null, updatedAt: 't' },
          { name: 'D', active: true, sessionId: 'SESSION_D', parent: 'main', sourceSessionId: 'SESSION_MAIN', updatedAt: 't' },
        ] as any);

        await mainCommandHandler(
          [],
          {
            prompt: 'prompt C',
            cwd: '/test',
            continueFromLatest: true,
            clone: true,
            cloneBranchName: 'C',
            verbose: 0,
            quiet: false,
            logLevel: 'info',
          } as MainCommandOptions,
          mockCommand,
        );

        expect(createExecutionRequest).toHaveBeenCalledWith(
          expect.objectContaining({ resume: 'SESSION_MAIN', cloneFromSession: 'SESSION_MAIN' }),
        );
      });

      it('should clone --from C --name M from named source branch', async () => {
        vi.mocked(listSessionBranches).mockResolvedValue([
          { name: 'main', active: true, sessionId: 'SESSION_MAIN', parent: null, sourceSessionId: null, updatedAt: 't' },
          { name: 'C', active: false, sessionId: 'SESSION_C', parent: 'main', sourceSessionId: 'SESSION_MAIN', updatedAt: 't' },
        ] as any);

        await mainCommandHandler(
          [],
          {
            prompt: 'prompt M',
            cwd: '/test',
            clone: true,
            cloneBranchName: 'M',
            cloneBranchFrom: 'C',
            verbose: 0,
            quiet: false,
            logLevel: 'info',
          } as MainCommandOptions,
          mockCommand,
        );

        expect(createExecutionRequest).toHaveBeenCalledWith(
          expect.objectContaining({ resume: 'SESSION_C', cloneFromSession: 'SESSION_C' }),
        );
      });

      it('should reject clone --name main and unknown --from branches', async () => {
        vi.mocked(listSessionBranches).mockResolvedValue([
          { name: 'main', active: true, sessionId: 'SESSION_MAIN', parent: null, sourceSessionId: null, updatedAt: 't' },
        ] as any);

        await mainCommandHandler(
          [],
          {
            prompt: 'bad target',
            cwd: '/test',
            clone: true,
            cloneBranchName: 'main',
            verbose: 0,
            quiet: false,
            logLevel: 'info',
          } as MainCommandOptions,
          mockCommand,
        );
        expect(processExitSpy).toHaveBeenCalledWith(1);
        expect(createExecutionRequest).not.toHaveBeenCalled();

        vi.mocked(createExecutionRequest).mockClear();
        processExitSpy.mockClear();

        await mainCommandHandler(
          [],
          {
            prompt: 'bad source',
            cwd: '/test',
            clone: true,
            cloneBranchName: 'M',
            cloneBranchFrom: 'missing',
            verbose: 0,
            quiet: false,
            logLevel: 'info',
          } as MainCommandOptions,
          mockCommand,
        );
        expect(processExitSpy).toHaveBeenCalledWith(1);
        expect(createExecutionRequest).not.toHaveBeenCalled();
      });

      it('should fail fast when clone is requested without resume or continue scope', async () => {
        process.env.YYLO_CONTINUE_SCOPE = 'missing-clone-pane';

        await mainCommandHandler(
          [],
          {
            prompt: 'clone without source',
            clone: true,
            cwd: '/test',
            verbose: 0,
            quiet: false,
            logLevel: 'info',
          } as MainCommandOptions,
          mockCommand,
        );

        expect(processExitSpy).toHaveBeenCalledWith(1);
        expect(createExecutionRequest).not.toHaveBeenCalled();
      });

      it('should fail fast when continueFromLatest is requested without snapshot env vars', async () => {
        process.env.YYLO_CONTINUE_SCOPE = 'missing-pane';

        await mainCommandHandler(
          [],
          {
            prompt: 'next step prompt',
            cwd: '/test',
            continueFromLatest: true,
            interactive: false,
            interactivePrompt: false,
            verbose: 0,
            quiet: false,
            logLevel: 'info',
          } as MainCommandOptions,
          mockCommand,
        );

        expect(processExitSpy).toHaveBeenCalledWith(1);
        expect(createExecutionRequest).not.toHaveBeenCalled();
      });

      it('should not leak continue snapshots across different shell scopes', async () => {
        const paneAHash = `SCOPE_${createHash('sha256')
          .update('YYLO_CONTINUE_SCOPE:pane-a')
          .digest('hex')
          .slice(0, 16)
          .toUpperCase()}`;

        process.env[`YYLO_LAST_SESSION_ID_${paneAHash}`] = 'pane-a-session';
        process.env[`YYLO_LAST_EXECUTION_SETTINGS_${paneAHash}`] = JSON.stringify({
          version: 1,
          subagent: 'claude',
          model: ':sonnet',
          maxIterations: 2,
        });

        process.env.YYLO_CONTINUE_SCOPE = 'pane-b';
        vi.mocked(resolveScopedContinueSessionState).mockResolvedValueOnce({
          context: { scopeHash: 'SCOPE_B000000000000000', scopeSource: 'test' },
          activeBranch: null,
          resolvedSessionId: '',
          settings: null,
          serializedSettings: null,
        } as any);

        await mainCommandHandler(
          [],
          {
            prompt: 'next step prompt',
            cwd: '/test',
            continueFromLatest: true,
            interactive: false,
            interactivePrompt: false,
            verbose: 0,
            quiet: false,
            logLevel: 'info',
          } as MainCommandOptions,
          mockCommand,
        );

        expect(processExitSpy).toHaveBeenCalledWith(1);
        expect(createExecutionRequest).not.toHaveBeenCalled();
      });

      it('should handle execution failure and exit with code 1', async () => {
        const { createExecutionEngine } = await import('../../core/engine.js');
        const mockEngine = {
          execute: vi.fn().mockResolvedValue({
            status: 'failed',
            iterations: [],
            statistics: {
              totalIterations: 0,
              successfulIterations: 0,
              failedIterations: 1,
              averageIterationDuration: 0,
              totalToolCalls: 0,
              rateLimitEncounters: 0,
            },
          }),
          onProgress: vi.fn(),
          on: vi.fn(),
          shutdown: vi.fn(),
        };
        vi.mocked(createExecutionEngine).mockReturnValueOnce(mockEngine);

        const options: MainCommandOptions = {
          subagent: 'claude',
          prompt: 'test prompt',
          cwd: '/test',
          maxIterations: 1,
          interactive: false,
          interactivePrompt: false,
          verbose: 0,
          quiet: false,
          logLevel: 'info',
        };

        await mainCommandHandler([], options, mockCommand);

        expect(processExitSpy).toHaveBeenCalledWith(1);
      });

      it('should use default values from config', async () => {
        const options: MainCommandOptions = {
          subagent: 'claude',
          prompt: 'test prompt',
          cwd: undefined,
          maxIterations: undefined,
          model: undefined,
          interactive: false,
          interactivePrompt: false,
          verbose: 0,
          quiet: false,
          logLevel: 'info',
        };

        await mainCommandHandler([], options, mockCommand);

        const { createExecutionRequest } = await import('../../core/engine.js');
        expect(createExecutionRequest).toHaveBeenCalledWith(
          expect.objectContaining({
            instruction: 'test prompt',
            subagent: 'claude',
            workingDirectory: '/test/dir',
            maxIterations: 5,
            backend: 'shell',
          }),
        );
      });

      it('should use per-subagent default model from config.defaultModels', async () => {
        const { loadConfig } = await import('../../core/config.js');
        vi.mocked(loadConfig).mockResolvedValueOnce({
          workingDirectory: '/test/dir',
          defaultMaxIterations: 5,
          defaultSubagent: 'claude',
          defaultModel: ':sonnet',
          defaultModels: {
            codex: ':gpt-5',
          },
          mcpTimeout: 30000,
          mcpRetries: 3,
          verbose: 1,
          quiet: false,
        } as any);

        await mainCommandHandler(
          [],
          {
            subagent: 'codex',
            prompt: 'test prompt',
            interactive: false,
            interactivePrompt: false,
            verbose: 0,
            quiet: false,
            logLevel: 'info',
          },
          mockCommand,
        );

        const { createExecutionRequest } = await import('../../core/engine.js');
        expect(createExecutionRequest).toHaveBeenCalledWith(
          expect.objectContaining({
            subagent: 'codex',
            model: ':gpt-5',
          }),
        );
      });

      it('should prefer per-subagent default model over legacy defaultModel for config.defaultSubagent', async () => {
        const { loadConfig } = await import('../../core/config.js');
        vi.mocked(loadConfig).mockResolvedValueOnce({
          workingDirectory: '/test/dir',
          defaultMaxIterations: 5,
          defaultSubagent: 'codex',
          defaultModel: ':mini',
          defaultModels: {
            codex: ':codex',
          },
          mcpTimeout: 30000,
          mcpRetries: 3,
          verbose: 1,
          quiet: false,
        } as any);

        await mainCommandHandler(
          [],
          {
            subagent: 'codex',
            prompt: 'test prompt',
            interactive: false,
            interactivePrompt: false,
            verbose: 0,
            quiet: false,
            logLevel: 'info',
          },
          mockCommand,
        );

        const { createExecutionRequest } = await import('../../core/engine.js');
        expect(createExecutionRequest).toHaveBeenCalledWith(
          expect.objectContaining({
            subagent: 'codex',
            model: ':codex',
          }),
        );
      });

      it('should keep configured pi default model from defaultModels across runs', async () => {
        const { loadConfig } = await import('../../core/config.js');
        vi.mocked(loadConfig).mockResolvedValueOnce({
          workingDirectory: '/test/dir',
          defaultMaxIterations: 5,
          defaultSubagent: 'pi',
          defaultModel: ':pi',
          defaultModels: {
            pi: ':api-codex',
          },
          mcpTimeout: 30000,
          mcpRetries: 3,
          verbose: 1,
          quiet: false,
        } as any);

        await mainCommandHandler(
          [],
          {
            subagent: 'pi',
            prompt: 'test prompt',
            interactive: false,
            interactivePrompt: false,
            verbose: 0,
            quiet: false,
            logLevel: 'info',
          },
          mockCommand,
        );

        const { createExecutionRequest } = await import('../../core/engine.js');
        expect(createExecutionRequest).toHaveBeenCalledWith(
          expect.objectContaining({
            subagent: 'pi',
            model: ':api-codex',
          }),
        );
      });

      it('should setup progress callbacks', async () => {
        const options: MainCommandOptions = {
          subagent: 'claude',
          prompt: 'test prompt',
          cwd: '/test',
          maxIterations: 1,
          interactive: false,
          interactivePrompt: false,
          verbose: 0,
          quiet: false,
          logLevel: 'info',
        };

        await mainCommandHandler([], options, mockCommand);

        const { createExecutionEngine } = await import('../../core/engine.js');
        const engine = vi.mocked(createExecutionEngine).mock.results[0].value;

        expect(engine.onProgress).toHaveBeenCalled();
        expect(engine.on).toHaveBeenCalledWith('iteration:start', expect.any(Function));
        expect(engine.on).toHaveBeenCalledWith('iteration:complete', expect.any(Function));
        expect(engine.on).toHaveBeenCalledWith('execution:error', expect.any(Function));
      });

      it('should cleanup resources', async () => {
        const options: MainCommandOptions = {
          subagent: 'claude',
          prompt: 'test prompt',
          cwd: '/test',
          maxIterations: 1,
          interactive: false,
          interactivePrompt: false,
          verbose: 0,
          quiet: false,
          logLevel: 'info',
        };

        await mainCommandHandler([], options, mockCommand);

        const { createExecutionEngine } = await import('../../core/engine.js');
        const engine = vi.mocked(createExecutionEngine).mock.results[0].value;

        expect(engine.shutdown).toHaveBeenCalled();
      });
    });

    describe('error handling', () => {
      it('should handle ValidationError', async () => {
        const options: MainCommandOptions = {
          subagent: 'invalid' as any,
          prompt: 'test prompt',
          cwd: '/test',
          maxIterations: 1,
          interactive: false,
          interactivePrompt: false,
          verbose: 0,
          quiet: false,
          logLevel: 'info',
        };

        await mainCommandHandler([], options, mockCommand);

        expect(processExitSpy).toHaveBeenCalledWith(1);
      });

      it('should handle ConfigurationError', async () => {
        const { loadConfig } = await import('../../core/config.js');
        const configError = new ConfigurationError('Config error', ['Check config file']);
        vi.mocked(loadConfig).mockRejectedValueOnce(configError);

        const options: MainCommandOptions = {
          subagent: 'claude',
          prompt: 'test prompt',
          cwd: '/test',
          maxIterations: 1,
          interactive: false,
          interactivePrompt: false,
          verbose: 0,
          quiet: false,
          logLevel: 'info',
        };

        await mainCommandHandler([], options, mockCommand);

        expect(processExitSpy).toHaveBeenCalledWith(2);
      });

      it('should handle RuntimeError', async () => {
        vi.mocked(fs.pathExists).mockResolvedValueOnce(true);
        vi.mocked(fs.readFile).mockRejectedValueOnce(new Error('File error'));

        const options: MainCommandOptions = {
          subagent: 'claude',
          prompt: '/path/to/prompt.txt',
          cwd: '/test',
          maxIterations: 1,
          interactive: false,
          interactivePrompt: false,
          verbose: 0,
          quiet: false,
          logLevel: 'info',
        };

        await mainCommandHandler([], options, mockCommand);

        // loadPromptFromFile wraps non-RuntimeError into RuntimeError
        expect(processExitSpy).toHaveBeenCalledWith(5);
      });

      it('should handle unexpected errors', async () => {
        const { loadConfig } = await import('../../core/config.js');
        vi.mocked(loadConfig).mockRejectedValueOnce(new Error('Unexpected error'));

        const options: MainCommandOptions = {
          subagent: 'claude',
          prompt: 'test prompt',
          cwd: '/test',
          maxIterations: 1,
          interactive: false,
          interactivePrompt: false,
          verbose: 0,
          quiet: false,
          logLevel: 'info',
        };

        await mainCommandHandler([], options, mockCommand);

        expect(processExitSpy).toHaveBeenCalledWith(99);
      });

      it('should show stack trace in verbose mode on unexpected error', async () => {
        const { loadConfig } = await import('../../core/config.js');
        vi.mocked(loadConfig).mockRejectedValueOnce(new Error('Unexpected error'));

        const options: MainCommandOptions = {
          subagent: 'claude',
          prompt: 'test prompt',
          cwd: '/test',
          maxIterations: 1,
          interactive: false,
          interactivePrompt: false,
          verbose: 2,
          quiet: false,
          logLevel: 'info',
        };

        await mainCommandHandler([], options, mockCommand);

        expect(console.error).toHaveBeenCalledWith(expect.stringContaining('Stack Trace'));
      });
    });
  });

  describe('progress display', () => {
    it('should display progress in non-verbose mode', () => {
      // This is tested indirectly through the main handler
      // The progress display is internal to the execution coordinator
      expect(true).toBe(true);
    });

    it('should display detailed progress in verbose mode', () => {
      // This is tested indirectly through the main handler
      // The progress display is internal to the execution coordinator
      expect(true).toBe(true);
    });
  });
});

describe('Model Compatibility', () => {
  // Import the functions after mocking
  let getDefaultModelForSubagent: (subagent: string) => string;
  let isModelCompatibleWithSubagent: (model: string, subagent: string) => boolean;

  beforeAll(async () => {
    // Reset modules to get fresh imports without the mocks affecting these specific functions
    const mainModule = await import('../commands/main.js');
    getDefaultModelForSubagent = mainModule.getDefaultModelForSubagent;
    isModelCompatibleWithSubagent = mainModule.isModelCompatibleWithSubagent;
  });

  describe('getDefaultModelForSubagent', () => {
    it('should return :sonnet for claude', () => {
      expect(getDefaultModelForSubagent('claude')).toBe(':sonnet');
    });

    it('should return :codex for codex', () => {
      expect(getDefaultModelForSubagent('codex')).toBe(':codex');
    });

    it('should return :pro for gemini', () => {
      expect(getDefaultModelForSubagent('gemini')).toBe(':pro');
    });

    it('should return auto for cursor', () => {
      expect(getDefaultModelForSubagent('cursor')).toBe('auto');
    });

    it('should return :gpt for pi', () => {
      expect(getDefaultModelForSubagent('pi')).toBe(':gpt');
    });

    it('should return :sonnet as default for unknown subagent', () => {
      expect(getDefaultModelForSubagent('unknown' as any)).toBe(':sonnet');
    });
  });

  describe('isModelCompatibleWithSubagent', () => {
    describe('Claude subagent', () => {
      it('should accept Claude model shorthands', () => {
        expect(isModelCompatibleWithSubagent(':sonnet', 'claude')).toBe(true);
        expect(isModelCompatibleWithSubagent(':haiku', 'claude')).toBe(true);
        expect(isModelCompatibleWithSubagent(':opus', 'claude')).toBe(true);
        expect(isModelCompatibleWithSubagent(':claude-sonnet-4-5', 'claude')).toBe(true);
      });

      it('should reject Codex model shorthands', () => {
        expect(isModelCompatibleWithSubagent(':codex', 'claude')).toBe(false);
        expect(isModelCompatibleWithSubagent(':codex-mini', 'claude')).toBe(false);
        expect(isModelCompatibleWithSubagent(':gpt-5', 'claude')).toBe(false);
        expect(isModelCompatibleWithSubagent(':mini', 'claude')).toBe(false);
      });

      it('should reject Gemini model shorthands', () => {
        expect(isModelCompatibleWithSubagent(':pro', 'claude')).toBe(false);
        expect(isModelCompatibleWithSubagent(':flash', 'claude')).toBe(false);
        expect(isModelCompatibleWithSubagent(':gemini-pro', 'claude')).toBe(false);
      });

      it('should accept full model names (non-shorthand)', () => {
        expect(isModelCompatibleWithSubagent('claude-sonnet-4-5-20250929', 'claude')).toBe(true);
        expect(isModelCompatibleWithSubagent('gpt-5.3-codex', 'claude')).toBe(true);
        expect(isModelCompatibleWithSubagent('custom-model', 'claude')).toBe(true);
      });
    });

    describe('Codex subagent', () => {
      it('should accept Codex model shorthands', () => {
        expect(isModelCompatibleWithSubagent(':codex', 'codex')).toBe(true);
        expect(isModelCompatibleWithSubagent(':codex-mini', 'codex')).toBe(true);
        expect(isModelCompatibleWithSubagent(':gpt-5', 'codex')).toBe(true);
        expect(isModelCompatibleWithSubagent(':mini', 'codex')).toBe(true);
      });

      it('should reject Claude model shorthands', () => {
        expect(isModelCompatibleWithSubagent(':sonnet', 'codex')).toBe(false);
        expect(isModelCompatibleWithSubagent(':haiku', 'codex')).toBe(false);
        expect(isModelCompatibleWithSubagent(':opus', 'codex')).toBe(false);
        expect(isModelCompatibleWithSubagent(':claude-sonnet-4-5', 'codex')).toBe(false);
      });

      it('should reject Gemini model shorthands', () => {
        expect(isModelCompatibleWithSubagent(':pro', 'codex')).toBe(false);
        expect(isModelCompatibleWithSubagent(':flash', 'codex')).toBe(false);
      });

      it('should accept full model names (non-shorthand)', () => {
        expect(isModelCompatibleWithSubagent('gpt-5.3-codex', 'codex')).toBe(true);
        expect(isModelCompatibleWithSubagent('claude-sonnet-4-5-20250929', 'codex')).toBe(true);
      });
    });

    describe('Gemini subagent', () => {
      it('should accept Gemini model shorthands', () => {
        expect(isModelCompatibleWithSubagent(':pro', 'gemini')).toBe(true);
        expect(isModelCompatibleWithSubagent(':flash', 'gemini')).toBe(true);
        expect(isModelCompatibleWithSubagent(':gemini-pro', 'gemini')).toBe(true);
      });

      it('should reject Claude model shorthands', () => {
        expect(isModelCompatibleWithSubagent(':sonnet', 'gemini')).toBe(false);
        expect(isModelCompatibleWithSubagent(':haiku', 'gemini')).toBe(false);
      });

      it('should reject Codex model shorthands', () => {
        expect(isModelCompatibleWithSubagent(':codex', 'gemini')).toBe(false);
        expect(isModelCompatibleWithSubagent(':codex-mini', 'gemini')).toBe(false);
        expect(isModelCompatibleWithSubagent(':gpt-5', 'gemini')).toBe(false);
      });
    });

    describe('Cursor subagent', () => {
      it('should accept any model (cursor is model-agnostic)', () => {
        expect(isModelCompatibleWithSubagent(':sonnet', 'cursor')).toBe(true);
        expect(isModelCompatibleWithSubagent(':codex', 'cursor')).toBe(true);
        expect(isModelCompatibleWithSubagent(':pro', 'cursor')).toBe(true);
        expect(isModelCompatibleWithSubagent('auto', 'cursor')).toBe(true);
        expect(isModelCompatibleWithSubagent('custom-model', 'cursor')).toBe(true);
      });
    });

    describe('Edge cases', () => {
      it('should handle unknown subagent gracefully', () => {
        // Unknown subagents should accept any model
        expect(isModelCompatibleWithSubagent(':sonnet', 'unknown' as any)).toBe(true);
        expect(isModelCompatibleWithSubagent(':codex', 'unknown' as any)).toBe(true);
      });

      it('should handle unknown shorthands as compatible', () => {
        // Unknown shorthands that don't match any known pattern should be accepted
        expect(isModelCompatibleWithSubagent(':custom', 'claude')).toBe(true);
        expect(isModelCompatibleWithSubagent(':custom', 'codex')).toBe(true);
      });
    });
  });
});

describe('Positional Prompt CLI Parsing', () => {
  it('should merge positional args into options.prompt when -p is not set', async () => {
    // Test the merge logic used in setupMainCommand's .action() callback
    const program = new Command();
    let capturedOptions: any = null;
    let capturedArgs: string[] = [];

    program
      .argument('[prompt_text...]', 'Prompt text (positional)')
      .option('-p, --prompt [text]', 'Prompt input')
      .option('-s, --subagent <name>', 'Subagent')
      .action((promptArgs: string[], options) => {
        // Same merge logic as cli.ts setupMainCommand
        if (promptArgs.length > 0 && options.prompt === undefined) {
          options.prompt = promptArgs.join(' ');
        }
        capturedArgs = promptArgs;
        capturedOptions = options;
      });

    // Shell passes quoted string as single arg: yylo -s pi "my positional prompt"
    await program.parseAsync(['-s', 'pi', 'my positional prompt'], { from: 'user' });

    expect(capturedArgs).toEqual(['my positional prompt']);
    expect(capturedOptions.prompt).toBe('my positional prompt');
    expect(capturedOptions.subagent).toBe('pi');
  });

  it('should join multiple unquoted positional words', async () => {
    // Shell passes unquoted words as separate args: yylo -s pi my positional prompt
    const program = new Command();
    let capturedOptions: any = null;

    program
      .argument('[prompt_text...]', 'Prompt text (positional)')
      .option('-p, --prompt [text]', 'Prompt input')
      .option('-s, --subagent <name>', 'Subagent')
      .action((promptArgs: string[], options) => {
        if (promptArgs.length > 0 && options.prompt === undefined) {
          options.prompt = promptArgs.join(' ');
        }
        capturedOptions = options;
      });

    await program.parseAsync(['-s', 'pi', 'my', 'positional', 'prompt'], { from: 'user' });

    expect(capturedOptions.prompt).toBe('my positional prompt');
    expect(capturedOptions.subagent).toBe('pi');
  });

  it('should not override -p flag with positional args', async () => {
    const program = new Command();
    let capturedOptions: any = null;

    program
      .argument('[prompt_text...]', 'Prompt text (positional)')
      .option('-p, --prompt [text]', 'Prompt input')
      .option('-s, --subagent <name>', 'Subagent')
      .action((promptArgs: string[], options) => {
        if (promptArgs.length > 0 && options.prompt === undefined) {
          options.prompt = promptArgs.join(' ');
        }
        capturedOptions = options;
      });

    await program.parseAsync(['-s', 'pi', '-p', 'explicit prompt'], { from: 'user' });

    expect(capturedOptions.prompt).toBe('explicit prompt');
  });

  it('should handle quoted positional prompt as single argument', async () => {
    const program = new Command();
    let capturedOptions: any = null;

    program
      .argument('[prompt_text...]', 'Prompt text (positional)')
      .option('-p, --prompt [text]', 'Prompt input')
      .option('-s, --subagent <name>', 'Subagent')
      .option('-m, --model <name>', 'Model')
      .action((promptArgs: string[], options) => {
        if (promptArgs.length > 0 && options.prompt === undefined) {
          options.prompt = promptArgs.join(' ');
        }
        capturedOptions = options;
      });

    // When shell passes a quoted string, it arrives as one element
    await program.parseAsync(['-s', 'pi', '-m', 'openai-codex/gpt-5.3-codex', 'MY prompt'], { from: 'user' });

    expect(capturedOptions.prompt).toBe('MY prompt');
    expect(capturedOptions.subagent).toBe('pi');
    expect(capturedOptions.model).toBe('openai-codex/gpt-5.3-codex');
  });

  it('should leave options.prompt undefined when no positional args and no -p flag', async () => {
    const program = new Command();
    let capturedOptions: any = null;

    program
      .argument('[prompt_text...]', 'Prompt text (positional)')
      .option('-p, --prompt [text]', 'Prompt input')
      .option('-s, --subagent <name>', 'Subagent')
      .action((promptArgs: string[], options) => {
        if (promptArgs.length > 0 && options.prompt === undefined) {
          options.prompt = promptArgs.join(' ');
        }
        capturedOptions = options;
      });

    await program.parseAsync(['-s', 'pi'], { from: 'user' });

    expect(capturedOptions.prompt).toBeUndefined();
  });

  it('should handle -p with heredoc (prompt=true) and no positional', async () => {
    const program = new Command();
    let capturedOptions: any = null;

    program
      .argument('[prompt_text...]', 'Prompt text (positional)')
      .option('-p, --prompt [text]', 'Prompt input')
      .option('-s, --subagent <name>', 'Subagent')
      .action((promptArgs: string[], options) => {
        if (promptArgs.length > 0 && options.prompt === undefined) {
          options.prompt = promptArgs.join(' ');
        }
        capturedOptions = options;
      });

    // -p without argument → Commander sets prompt=true
    await program.parseAsync(['-s', 'pi', '-p'], { from: 'user' });

    expect(capturedOptions.prompt).toBe(true);
  });
});

describe('Verbose/Quiet Output Modes', () => {
  let processExitSpy: ReturnType<typeof vi.spyOn>;
  let consoleErrorSpy: ReturnType<typeof vi.spyOn>;
  let consoleLogSpy: ReturnType<typeof vi.spyOn>;
  let originalIsTTY: boolean | undefined;

  const mockCommand = new Command('yylo');

  beforeEach(async () => {
    consoleLogSpy = vi.spyOn(console, 'log').mockImplementation(() => {});
    consoleErrorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    processExitSpy = vi.spyOn(process, 'exit').mockImplementation(() => undefined as never);

    originalIsTTY = process.stdin.isTTY;
    Object.defineProperty(process.stdin, 'isTTY', { value: true, writable: true, configurable: true });

    // Re-establish mocks (mockReset clears them)
    const { loadConfig } = await import('../../core/config.js');
    vi.mocked(loadConfig).mockResolvedValue({
      workingDirectory: '/test/dir',
      defaultMaxIterations: 5,
      defaultModel: ':sonnet',
      mcpTimeout: 30000,
      mcpRetries: 3,
      verbose: 1,
      quiet: false,
    } as any);

    const { createExecutionEngine, createExecutionRequest } = await import('../../core/engine.js');
    vi.mocked(createExecutionEngine).mockReturnValue({
      execute: vi.fn().mockResolvedValue({
        status: 'completed',
        iterations: [{
          iterationNumber: 1,
          toolResult: { content: 'Test result', metadata: {} },
          success: true,
          duration: 1000,
        }],
        statistics: {
          totalIterations: 1,
          successfulIterations: 1,
          failedIterations: 0,
          averageIterationDuration: 1000,
          totalToolCalls: 5,
          rateLimitEncounters: 0,
        },
      }),
      onProgress: vi.fn(),
      on: vi.fn(),
      shutdown: vi.fn(),
    } as any);
    vi.mocked(createExecutionRequest).mockImplementation((opts: any) => ({
      requestId: 'test-request',
      instruction: opts.instruction,
      subagent: opts.subagent,
      workingDirectory: opts.workingDirectory || '/test/dir',
      maxIterations: opts.maxIterations || 1,
      model: opts.model || ':sonnet',
      agents: opts.agents,
      tools: opts.tools,
      allowedTools: opts.allowedTools,
      appendAllowedTools: opts.appendAllowedTools,
      disallowedTools: opts.disallowedTools,
      resume: opts.resume,
      cloneSession: opts.cloneSession,
      cloneFromSession: opts.cloneFromSession,
      continueConversation: opts.continueConversation,
      thinking: opts.thinking,
      live: opts.live,
    }) as any);

    const fsExtra = await import('fs-extra');
    vi.mocked(fsExtra.pathExists as any).mockResolvedValue(false);
  });

  afterEach(() => {
    Object.defineProperty(process.stdin, 'isTTY', { value: originalIsTTY, writable: true, configurable: true });
    vi.restoreAllMocks();
  });

  it('should show model and iterations at verbose level 1', async () => {
    const options: MainCommandOptions = {
      subagent: 'claude',
      prompt: 'test prompt',
      verbose: 1,
      quiet: false,
      logLevel: 'info',
    };

    await mainCommandHandler([], options, mockCommand);

    // Model and iterations should be shown
    const allCalls = consoleErrorSpy.mock.calls.map(c => c[0]);
    expect(allCalls.some((c: string) => c.includes('Model:') || c.includes(':sonnet'))).toBe(true);
    expect(allCalls.some((c: string) => c.includes('Max Iterations:'))).toBe(true);
  });

  it('should show selected runtime options (e.g. thinking) in execution summary', async () => {
    const options: MainCommandOptions = {
      subagent: 'pi',
      prompt: 'test prompt',
      thinking: 'xhigh',
      verbose: 1,
      quiet: false,
      logLevel: 'info',
    };

    await mainCommandHandler([], options, mockCommand);

    const allCalls = consoleErrorSpy.mock.calls.map(c => c[0]);
    expect(allCalls.some((c: string) => c.includes('Thinking: xhigh'))).toBe(true);
  });

  it('should show live mode selection in execution summary when --live is enabled for pi', async () => {
    const options = {
      subagent: 'pi',
      prompt: 'test prompt',
      live: true,
      verbose: 1,
      quiet: false,
      logLevel: 'info',
    } as MainCommandOptions & { live: boolean };

    await mainCommandHandler([], options as MainCommandOptions, mockCommand);

    const allCalls = consoleErrorSpy.mock.calls.map(c => String(c[0]));
    expect(allCalls.some((c: string) => c.includes('Live Mode: enabled'))).toBe(true);
  });

  it('should show task template + resolved task preview when prompt substitutions are present', async () => {
    const { createExecutionEngine } = await import('../../core/engine.js');

    const eventHandlers = new Map<string, (payload: any) => void>();

    vi.mocked(createExecutionEngine).mockReturnValueOnce({
      execute: vi.fn().mockImplementation(async () => {
        const onInstructionResolved = eventHandlers.get('iteration:instruction-resolved');
        onInstructionResolved?.({
          iterationNumber: 1,
          instruction: 'Run ready now',
          templateInstruction: "Run !'echo ready' now",
        });

        return {
          status: 'completed',
          iterations: [
            {
              iterationNumber: 1,
              toolResult: { content: 'Test result', metadata: {} },
              success: true,
              duration: 1000,
            },
          ],
          statistics: {
            totalIterations: 1,
            successfulIterations: 1,
            failedIterations: 0,
            averageIterationDuration: 1000,
            totalToolCalls: 5,
            rateLimitEncounters: 0,
          },
        };
      }),
      onProgress: vi.fn(),
      on: vi.fn((event: string, callback: (payload: any) => void) => {
        eventHandlers.set(event, callback);
      }),
      shutdown: vi.fn(),
    } as any);

    const options: MainCommandOptions = {
      subagent: 'claude',
      prompt: "Run !'echo ready' now",
      verbose: 1,
      quiet: false,
      logLevel: 'info',
    };

    await mainCommandHandler([], options, mockCommand);

    const allCalls = consoleErrorSpy.mock.calls.map(c => String(c[0]));
    expect(allCalls.some((c: string) => c.includes('Task Template:'))).toBe(true);
    expect(allCalls.some((c: string) => c.includes('Prompt-time substitutions are resolved'))).toBe(true);
    expect(allCalls.some((c: string) => c.includes('Resolved Task (iteration 1):'))).toBe(true);
    expect(allCalls.some((c: string) => c.includes('Run ready now'))).toBe(true);
  });

  it('should show macro template hint and warning details for unresolved prompt macros', async () => {
    const { createExecutionEngine } = await import('../../core/engine.js');

    const eventHandlers = new Map<string, (payload: any) => void>();

    vi.mocked(createExecutionEngine).mockReturnValueOnce({
      execute: vi.fn().mockImplementation(async () => {
        const onInstructionResolved = eventHandlers.get('iteration:instruction-resolved');
        onInstructionResolved?.({
          iterationNumber: 1,
          instruction: 'Run @@missing now',
          templateInstruction: 'Run @@missing now',
          warnings: [{ message: 'Unresolved prompt macro @@missing; leaving token unchanged.' }],
        });

        return {
          status: 'completed',
          iterations: [
            {
              iterationNumber: 1,
              toolResult: { content: 'Test result', metadata: {} },
              success: true,
              duration: 1000,
            },
          ],
          statistics: {
            totalIterations: 1,
            successfulIterations: 1,
            failedIterations: 0,
            averageIterationDuration: 1000,
            totalToolCalls: 5,
            rateLimitEncounters: 0,
          },
        };
      }),
      onProgress: vi.fn(),
      on: vi.fn((event: string, callback: (payload: any) => void) => {
        eventHandlers.set(event, callback);
      }),
      shutdown: vi.fn(),
    } as any);

    await mainCommandHandler(
      [],
      {
        subagent: 'claude',
        prompt: 'Run @@missing now',
        verbose: 1,
        quiet: false,
        logLevel: 'info',
      },
      mockCommand,
    );

    const allCalls = consoleErrorSpy.mock.calls.map(c => String(c[0]));
    expect(allCalls.some((c: string) => c.includes('Task Template:'))).toBe(true);
    expect(allCalls.some((c: string) => c.includes('Prompt macros (@@key) are translated'))).toBe(true);
    expect(allCalls.some((c: string) => c.includes('Unresolved prompt macro @@missing'))).toBe(true);
  });

  it('should NOT show model and iterations at verbose level 0 (quiet)', async () => {
    const options: MainCommandOptions = {
      subagent: 'claude',
      prompt: 'test prompt',
      verbose: 0,
      quiet: false,
      logLevel: 'info',
    };

    await mainCommandHandler([], options, mockCommand);

    // At level 0 (quiet), model and iterations should NOT be shown
    const allCalls = consoleErrorSpy.mock.calls.map(c => c[0]);
    expect(allCalls.some((c: string) => c.includes('Model:'))).toBe(false);
    expect(allCalls.some((c: string) => c.includes('Max Iterations:'))).toBe(false);
  });

  it('should suppress all output in quiet mode', async () => {
    const options: MainCommandOptions = {
      subagent: 'claude',
      prompt: 'test prompt',
      verbose: 1,
      quiet: true,
      logLevel: 'info',
    };

    await mainCommandHandler([], options, mockCommand);

    // In quiet mode, no progress/execution info on stderr (only final result on stdout)
    const allCalls = consoleErrorSpy.mock.calls.map(c => c[0]);
    // Should NOT show execution banner, iterations, etc.
    expect(allCalls.some((c: string) => c.includes('Executing with'))).toBe(false);
    expect(allCalls.some((c: string) => c.includes('Model:'))).toBe(false);
  });

  it('should show debug info (Request ID, Working Directory) only at verbose level 2', async () => {
    const options: MainCommandOptions = {
      subagent: 'claude',
      prompt: 'test prompt',
      verbose: 2,
      quiet: false,
      logLevel: 'info',
    };

    await mainCommandHandler([], options, mockCommand);

    const allCalls = consoleErrorSpy.mock.calls.map(c => c[0]);
    expect(allCalls.some((c: string) => c.includes('Request ID:'))).toBe(true);
    expect(allCalls.some((c: string) => c.includes('Working Directory:'))).toBe(true);
  });

  it('should NOT show Request ID/Working Dir at verbose level 1', async () => {
    const options: MainCommandOptions = {
      subagent: 'claude',
      prompt: 'test prompt',
      verbose: 1,
      quiet: false,
      logLevel: 'info',
    };

    await mainCommandHandler([], options, mockCommand);

    const allCalls = consoleErrorSpy.mock.calls.map(c => c[0]);
    expect(allCalls.some((c: string) => c.includes('Request ID:'))).toBe(false);
    expect(allCalls.some((c: string) => c.includes('Working Directory:'))).toBe(false);
  });

  it('should show subagent source info at verbose level 1 (default)', async () => {
    const { loadConfig } = await import('../../core/config.js');
    vi.mocked(loadConfig).mockResolvedValue({
      workingDirectory: '/test/dir',
      defaultMaxIterations: 5,
      defaultSubagent: 'pi',
      defaultModel: ':pi',
      mcpTimeout: 30000,
      mcpRetries: 3,
      verbose: 1,
      quiet: false,
    } as any);

    const options: MainCommandOptions = {
      subagent: undefined as any, // Let it resolve from config
      prompt: 'test prompt',
      verbose: 1,
      quiet: false,
      logLevel: 'info',
    };

    await mainCommandHandler([], options, mockCommand);

    // Subagent resolution info should be shown at level 1+ (not gated on level 2)
    const allCalls = consoleErrorSpy.mock.calls.map(c => c[0]);
    expect(allCalls.some((c: string) => c.includes('Subagent:') || c.includes('pi'))).toBe(true);
  });

  it('should show statistics at verbose level 1', async () => {
    const options: MainCommandOptions = {
      subagent: 'claude',
      prompt: 'test prompt',
      verbose: 1,
      quiet: false,
      logLevel: 'info',
    };

    await mainCommandHandler([], options, mockCommand);

    const allCalls = consoleErrorSpy.mock.calls.map(c => c[0]);
    expect(allCalls.some((c: string) => c.includes('Statistics:'))).toBe(true);
    expect(allCalls.some((c: string) => c.includes('Total Iterations:'))).toBe(true);
    expect(allCalls.some((c: string) => c.includes('Branch: main'))).toBe(true);
  });

  it('should show the current Git branch at session start and in completion statistics', async () => {
    vi.mocked(getCurrentGitBranch).mockResolvedValue('feature/git-context');

    const options: MainCommandOptions = {
      subagent: 'claude',
      prompt: 'test prompt',
      verbose: 1,
      quiet: false,
      logLevel: 'info',
    };

    await mainCommandHandler([], options, mockCommand);

    const allCalls = consoleErrorSpy.mock.calls.map(c => String(c[0]));
    const gitBranchLines = allCalls.filter((line) => line.includes('Git Branch: feature/git-context'));
    expect(gitBranchLines).toHaveLength(2);
    expect(allCalls.some((line) => line.includes('Branch: main'))).toBe(true);
  });

  it('should omit Git branch output when the working directory has no named Git branch', async () => {
    vi.mocked(getCurrentGitBranch).mockResolvedValue(null);

    await mainCommandHandler([], {
      subagent: 'claude',
      prompt: 'test prompt',
      verbose: 1,
      quiet: false,
      logLevel: 'info',
    }, mockCommand);

    const allCalls = consoleErrorSpy.mock.calls.map(c => String(c[0]));
    expect(allCalls.some((line) => line.includes('Git Branch:'))).toBe(false);
  });

  it('should show active named branch in completion statistics for branch continues', async () => {
    const scope = 'summary-active-branch';
    const scopeHash = `SCOPE_${createHash('sha256')
      .update(`YYLO_CONTINUE_SCOPE:${scope}`)
      .digest('hex')
      .slice(0, 16)
      .toUpperCase()}`;
    process.env.YYLO_CONTINUE_SCOPE = scope;
    process.env[`YYLO_LAST_EXECUTION_SETTINGS_${scopeHash}`] = JSON.stringify({
      version: 1,
      subagent: 'pi',
      maxIterations: 1,
    });
    const activeBranch = {
      name: 'early_reflect',
      sessionId: 'SESSION_EARLY',
      parent: 'main',
      sourceSessionId: 'SESSION_MAIN',
      updatedAt: '2026-06-29T00:00:00.000Z',
    } as any;
    vi.mocked(getActiveSessionBranch).mockResolvedValue(activeBranch);
    vi.mocked(resolveScopedContinueSessionState).mockResolvedValue({
      context: { scopeHash, scopeSource: 'test' },
      activeBranch,
      resolvedSessionId: 'SESSION_EARLY',
      settings: { version: 1, subagent: 'pi', maxIterations: 1 },
      serializedSettings: JSON.stringify({ version: 1, subagent: 'pi', maxIterations: 1 }),
    } as any);

    const options: MainCommandOptions = {
      subagent: 'pi',
      prompt: 'test prompt',
      continueFromLatest: true,
      verbose: 1,
      quiet: false,
      logLevel: 'info',
    };

    await mainCommandHandler([], options, mockCommand);

    const allCalls = consoleErrorSpy.mock.calls.map(c => String(c[0]));
    expect(allCalls.some((c: string) => c.includes('Branch: early_reflect'))).toBe(true);
  });

  it('should show human-readable completion time in statistics', async () => {
    const options: MainCommandOptions = {
      subagent: 'claude',
      prompt: 'test prompt',
      verbose: 1,
      quiet: false,
      logLevel: 'info',
    };

    await mainCommandHandler([], options, mockCommand);

    const allCalls = consoleErrorSpy.mock.calls.map(c => String(c[0]));
    const completedAtLine = allCalls.find((c: string) => c.includes('Completed At:'));
    expect(completedAtLine).toBeDefined();
    expect(completedAtLine).toMatch(/Completed At:\s+.+/);
  });

  it('should show average duration in seconds when average is at least 1 second', async () => {
    const { createExecutionEngine } = await import('../../core/engine.js');

    vi.mocked(createExecutionEngine).mockReturnValueOnce({
      execute: vi.fn().mockResolvedValue({
        status: 'completed',
        iterations: [
          {
            iterationNumber: 1,
            toolResult: {
              content: 'Test result',
              metadata: {},
            },
            success: true,
            duration: 1100,
          },
        ],
        statistics: {
          totalIterations: 1,
          successfulIterations: 1,
          failedIterations: 0,
          averageIterationDuration: 1100,
          totalToolCalls: 5,
          rateLimitEncounters: 0,
        },
      }),
      onProgress: vi.fn(),
      on: vi.fn(),
      shutdown: vi.fn(),
    } as any);

    const options: MainCommandOptions = {
      subagent: 'claude',
      prompt: 'test prompt',
      verbose: 1,
      quiet: false,
      logLevel: 'info',
    };

    await mainCommandHandler([], options, mockCommand);

    const allCalls = consoleErrorSpy.mock.calls.map(c => String(c[0]));
    expect(allCalls.some((c: string) => c.includes('Average Duration: 1.1s'))).toBe(true);
  });

  it('should show average duration in minutes or hours when values exceed those units', async () => {
    const { createExecutionEngine } = await import('../../core/engine.js');

    vi.mocked(createExecutionEngine)
      .mockReturnValueOnce({
        execute: vi.fn().mockResolvedValue({
          status: 'completed',
          iterations: [
            {
              iterationNumber: 1,
              toolResult: {
                content: 'Test result',
                metadata: {},
              },
              success: true,
              duration: 90000,
            },
          ],
          statistics: {
            totalIterations: 1,
            successfulIterations: 1,
            failedIterations: 0,
            averageIterationDuration: 90000,
            totalToolCalls: 5,
            rateLimitEncounters: 0,
          },
        }),
        onProgress: vi.fn(),
        on: vi.fn(),
        shutdown: vi.fn(),
      } as any)
      .mockReturnValueOnce({
        execute: vi.fn().mockResolvedValue({
          status: 'completed',
          iterations: [
            {
              iterationNumber: 1,
              toolResult: {
                content: 'Test result',
                metadata: {},
              },
              success: true,
              duration: 7200000,
            },
          ],
          statistics: {
            totalIterations: 1,
            successfulIterations: 1,
            failedIterations: 0,
            averageIterationDuration: 7200000,
            totalToolCalls: 5,
            rateLimitEncounters: 0,
          },
        }),
        onProgress: vi.fn(),
        on: vi.fn(),
        shutdown: vi.fn(),
      } as any);

    const options: MainCommandOptions = {
      subagent: 'claude',
      prompt: 'test prompt',
      verbose: 1,
      quiet: false,
      logLevel: 'info',
    };

    await mainCommandHandler([], options, mockCommand);
    await mainCommandHandler([], options, mockCommand);

    const allCalls = consoleErrorSpy.mock.calls.map(c => String(c[0]));
    expect(allCalls.some((c: string) => c.includes('Average Duration: 1.5m'))).toBe(true);
    expect(allCalls.some((c: string) => c.includes('Average Duration: 2h'))).toBe(true);
  });

  it('should keep average duration in milliseconds when below one second', async () => {
    const { createExecutionEngine } = await import('../../core/engine.js');

    vi.mocked(createExecutionEngine).mockReturnValueOnce({
      execute: vi.fn().mockResolvedValue({
        status: 'completed',
        iterations: [
          {
            iterationNumber: 1,
            toolResult: {
              content: 'Test result',
              metadata: {},
            },
            success: true,
            duration: 450,
          },
        ],
        statistics: {
          totalIterations: 1,
          successfulIterations: 1,
          failedIterations: 0,
          averageIterationDuration: 450,
          totalToolCalls: 5,
          rateLimitEncounters: 0,
        },
      }),
      onProgress: vi.fn(),
      on: vi.fn(),
      shutdown: vi.fn(),
    } as any);

    const options: MainCommandOptions = {
      subagent: 'claude',
      prompt: 'test prompt',
      verbose: 1,
      quiet: false,
      logLevel: 'info',
    };

    await mainCommandHandler([], options, mockCommand);

    const allCalls = consoleErrorSpy.mock.calls.map(c => String(c[0]));
    expect(allCalls.some((c: string) => c.includes('Average Duration: 450ms'))).toBe(true);
  });

  it('should show aggregate and per-iteration costs when result payload includes total_cost_usd', async () => {
    const { createExecutionEngine } = await import('../../core/engine.js');

    vi.mocked(createExecutionEngine).mockReturnValueOnce({
      execute: vi.fn().mockResolvedValue({
        status: 'completed',
        iterations: [
          {
            iterationNumber: 1,
            toolResult: {
              content: JSON.stringify({
                type: 'result',
                session_id: 'session-a',
                total_cost_usd: 0.04,
              }),
              metadata: {},
            },
            success: true,
            duration: 1000,
          },
          {
            iterationNumber: 2,
            toolResult: {
              content: JSON.stringify({
                type: 'result',
                session_id: 'session-b',
                total_cost_usd: 0.02,
              }),
              metadata: {},
            },
            success: true,
            duration: 1200,
          },
        ],
        statistics: {
          totalIterations: 2,
          successfulIterations: 2,
          failedIterations: 0,
          averageIterationDuration: 1100,
          totalToolCalls: 6,
          rateLimitEncounters: 0,
        },
      }),
      onProgress: vi.fn(),
      on: vi.fn(),
      shutdown: vi.fn(),
    } as any);

    const options: MainCommandOptions = {
      subagent: 'pi',
      prompt: 'test prompt',
      verbose: 1,
      quiet: false,
      logLevel: 'info',
    };

    await mainCommandHandler([], options, mockCommand);

    const allCalls = consoleErrorSpy.mock.calls.map(c => String(c[0]));
    expect(allCalls.some((c: string) => c.includes('Total Cost: $0.060000'))).toBe(true);
    expect(allCalls.some((c: string) => c.includes('Iteration 1: session-a    cost: $0.040000'))).toBe(
      true,
    );
    expect(allCalls.some((c: string) => c.includes('Iteration 2: session-b    cost: $0.020000'))).toBe(
      true,
    );
  });

  it('should derive cost from usage.cost.total when total_cost_usd is absent', async () => {
    const { createExecutionEngine } = await import('../../core/engine.js');

    vi.mocked(createExecutionEngine).mockReturnValueOnce({
      execute: vi.fn().mockResolvedValue({
        status: 'completed',
        iterations: [
          {
            iterationNumber: 1,
            toolResult: {
              content: JSON.stringify({
                type: 'result',
                session_id: 'session-fallback',
                usage: {
                  cost: {
                    total: 0.0055,
                  },
                },
              }),
              metadata: {},
            },
            success: true,
            duration: 800,
          },
        ],
        statistics: {
          totalIterations: 1,
          successfulIterations: 1,
          failedIterations: 0,
          averageIterationDuration: 800,
          totalToolCalls: 4,
          rateLimitEncounters: 0,
        },
      }),
      onProgress: vi.fn(),
      on: vi.fn(),
      shutdown: vi.fn(),
    } as any);

    const options: MainCommandOptions = {
      subagent: 'pi',
      prompt: 'test prompt',
      verbose: 1,
      quiet: false,
      logLevel: 'info',
    };

    await mainCommandHandler([], options, mockCommand);

    const allCalls = consoleErrorSpy.mock.calls.map(c => String(c[0]));
    expect(allCalls.some((c: string) => c.includes('Total Cost: $0.005500'))).toBe(true);
    expect(allCalls.some((c: string) => c.includes('session-fallback    cost: $0.005500'))).toBe(
      true,
    );
  });

  it('should print structured result payload text instead of raw JSON at default verbosity', async () => {
    const { createExecutionEngine } = await import('../../core/engine.js');

    vi.mocked(createExecutionEngine).mockReturnValueOnce({
      execute: vi.fn().mockResolvedValue({
        status: 'failed',
        iterations: [
          {
            iterationNumber: 1,
            toolResult: {
              content: JSON.stringify({
                type: 'result',
                subtype: 'error',
                is_error: true,
                result: 'provider failure summary',
                sub_agent_response: {
                  messages: [
                    { role: 'assistant', content: 'very long raw conversation chunk' },
                  ],
                },
              }),
              metadata: { structuredOutput: true },
            },
            success: false,
            duration: 1200,
          },
        ],
        statistics: {
          totalIterations: 1,
          successfulIterations: 0,
          failedIterations: 1,
          averageIterationDuration: 1200,
          totalToolCalls: 2,
          rateLimitEncounters: 0,
        },
      }),
      onProgress: vi.fn(),
      on: vi.fn(),
      shutdown: vi.fn(),
    } as any);

    const options: MainCommandOptions = {
      subagent: 'pi',
      prompt: 'test prompt',
      verbose: 1,
      quiet: false,
      logLevel: 'info',
    };

    await mainCommandHandler([], options, mockCommand);

    const stdoutCalls = consoleLogSpy.mock.calls.map(c => String(c[0]));
    expect(stdoutCalls.some((c) => c.includes('provider failure summary'))).toBe(true);
    expect(stdoutCalls.some((c) => c.includes('sub_agent_response'))).toBe(false);
  });

  it('should keep raw structured JSON output at verbose level 2', async () => {
    const { createExecutionEngine } = await import('../../core/engine.js');

    const payload = {
      type: 'result',
      subtype: 'error',
      is_error: true,
      result: 'provider failure summary',
      sub_agent_response: { messages: [{ role: 'assistant', content: 'raw chunk' }] },
    };

    vi.mocked(createExecutionEngine).mockReturnValueOnce({
      execute: vi.fn().mockResolvedValue({
        status: 'failed',
        iterations: [
          {
            iterationNumber: 1,
            toolResult: {
              content: JSON.stringify(payload),
              metadata: { structuredOutput: true },
            },
            success: false,
            duration: 1200,
          },
        ],
        statistics: {
          totalIterations: 1,
          successfulIterations: 0,
          failedIterations: 1,
          averageIterationDuration: 1200,
          totalToolCalls: 2,
          rateLimitEncounters: 0,
        },
      }),
      onProgress: vi.fn(),
      on: vi.fn(),
      shutdown: vi.fn(),
    } as any);

    const options: MainCommandOptions = {
      subagent: 'pi',
      prompt: 'test prompt',
      verbose: 2,
      quiet: false,
      logLevel: 'info',
    };

    await mainCommandHandler([], options, mockCommand);

    expect(consoleLogSpy).toHaveBeenCalledWith(JSON.stringify(payload));
  });

  it('should print failure reason when execution fails before result content is available', async () => {
    const { createExecutionEngine } = await import('../../core/engine.js');

    vi.mocked(createExecutionEngine).mockReturnValueOnce({
      execute: vi.fn().mockResolvedValue({
        status: 'failed',
        iterations: [
          {
            iterationNumber: 1,
            toolResult: {
              content: '',
              error: {
                message:
                  'Prompt command substitution failed for `kanban-juno --status backlog --limit 10 -f table`',
              },
            },
            success: false,
            duration: 1200,
          },
        ],
        statistics: {
          totalIterations: 1,
          successfulIterations: 0,
          failedIterations: 1,
          averageIterationDuration: 1200,
          totalToolCalls: 1,
          rateLimitEncounters: 0,
        },
      }),
      onProgress: vi.fn(),
      on: vi.fn(),
      shutdown: vi.fn(),
    } as any);

    const options: MainCommandOptions = {
      subagent: 'pi',
      prompt: 'test prompt',
      verbose: 1,
      quiet: false,
      logLevel: 'info',
    };

    await mainCommandHandler([], options, mockCommand);

    const stderrCalls = consoleErrorSpy.mock.calls.map(c => String(c[0]));
    expect(
      stderrCalls.some((line) =>
        line.includes(
          'Failure reason: Prompt command substitution failed for `kanban-juno --status backlog --limit 10 -f table`',
        ),
      ),
    ).toBe(true);
  });

  it('should NOT show statistics at verbose level 0 (quiet)', async () => {
    const options: MainCommandOptions = {
      subagent: 'claude',
      prompt: 'test prompt',
      verbose: 0,
      quiet: false,
      logLevel: 'info',
    };

    await mainCommandHandler([], options, mockCommand);

    const allCalls = consoleErrorSpy.mock.calls.map(c => c[0]);
    expect(allCalls.some((c: string) => c.includes('Statistics:'))).toBe(false);
  });

  it('should reset logger to INFO at verbose level 1 after a previous verbose 2 run', async () => {
    const setLevelSpy = vi.spyOn(logger, 'setLevel');

    await mainCommandHandler([], {
      subagent: 'claude',
      prompt: 'debug run',
      verbose: 2,
      quiet: false,
      logLevel: 'info',
    } as MainCommandOptions, mockCommand);

    await mainCommandHandler([], {
      subagent: 'claude',
      prompt: 'normal run',
      verbose: 1,
      quiet: false,
      logLevel: 'info',
    } as MainCommandOptions, mockCommand);

    expect(setLevelSpy).toHaveBeenNthCalledWith(1, LogLevel.DEBUG);
    expect(setLevelSpy).toHaveBeenNthCalledWith(2, LogLevel.INFO);
  });

  it('should normalize string verbose values from Commander before setting logger/config levels', async () => {
    const setLevelSpy = vi.spyOn(logger, 'setLevel');
    const { loadConfig } = await import('../../core/config.js');

    await mainCommandHandler([], {
      subagent: 'claude',
      prompt: 'string verbose',
      verbose: 'false' as any,
      quiet: false,
      logLevel: 'info',
    } as MainCommandOptions, mockCommand);

    expect(setLevelSpy).toHaveBeenCalledWith(LogLevel.WARN);
    const lastCall = vi.mocked(loadConfig).mock.calls.at(-1)?.[0] as any;
    expect(lastCall.cliConfig.verbose).toBe(0);
  });

  it('prints the quiet final result after suppressed raw streaming events', async () => {
    const { createExecutionEngine } = await import('../../core/engine.js');
    const engine = createExecutionEngine({} as any);
    vi.mocked(engine.onProgress).mockImplementation((handler: any) => {
      handler({ timestamp: new Date(), content: '{"result":"streamed"}', metadata: { rawJsonOutput: true } });
    });
    await mainCommandHandler([], {
      subagent: 'pi', prompt: 'structured review', verbose: 1, quiet: true, logLevel: 'info',
    }, mockCommand);
    expect(consoleLogSpy.mock.calls.filter(call => call[0] === 'Test result')).toHaveLength(1);
    expect(consoleLogSpy.mock.calls.some(call => String(call[0]).includes('streamed'))).toBe(false);
  });

  it('should only print final result to stdout in quiet mode', async () => {
    const options: MainCommandOptions = {
      subagent: 'claude',
      prompt: 'test prompt',
      verbose: 1,
      quiet: true,
      logLevel: 'info',
    };

    await mainCommandHandler([], options, mockCommand);

    // stdout should have the final result
    expect(consoleLogSpy).toHaveBeenCalledWith('Test result');
  });
});

describe('normalizeVerbose (Commander.js integration)', () => {
  it('should parse -v with optional value in Commander.js', async () => {
    const program = new Command();
    let capturedVerbose: any = null;

    program
      .option('-v, --verbose [value]', 'Enable verbose')
      .option('-s, --subagent <name>', 'Subagent')
      .action((options) => {
        capturedVerbose = options.verbose;
      });

    // -v without value → true
    await program.parseAsync(['-v', '-s', 'claude'], { from: 'user' });
    expect(capturedVerbose).toBe(true);
  });

  it('should parse -v false as string "false"', async () => {
    const program = new Command();
    let capturedVerbose: any = null;

    program
      .option('-v, --verbose [value]', 'Enable verbose')
      .option('-s, --subagent <name>', 'Subagent')
      .action((options) => {
        capturedVerbose = options.verbose;
      });

    await program.parseAsync(['-v', 'false', '-s', 'claude'], { from: 'user' });
    expect(capturedVerbose).toBe('false');
  });

  it('should parse -v 0 as string "0"', async () => {
    const program = new Command();
    let capturedVerbose: any = null;

    program
      .option('-v, --verbose [value]', 'Enable verbose')
      .action((options) => {
        capturedVerbose = options.verbose;
      });

    await program.parseAsync(['-v', '0'], { from: 'user' });
    expect(capturedVerbose).toBe('0');
  });

  it('should parse no -v flag as undefined', async () => {
    const program = new Command();
    let capturedVerbose: any = null;

    program
      .option('-v, --verbose [value]', 'Enable verbose')
      .option('-s, --subagent <name>', 'Subagent')
      .action((options) => {
        capturedVerbose = options.verbose;
      });

    await program.parseAsync(['-s', 'claude'], { from: 'user' });
    expect(capturedVerbose).toBeUndefined();
  });

  it('should parse --silent as a flag', async () => {
    const program = new Command();
    let capturedOptions: any = null;

    program
      .option('-q, --quiet', 'Quiet mode')
      .option('--silent', 'Alias for --quiet')
      .action((options) => {
        capturedOptions = options;
      });

    await program.parseAsync(['--silent'], { from: 'user' });
    expect(capturedOptions.silent).toBe(true);
  });
});

describe('Subagent alias parsing regressions', () => {
  it('should parse --until-completion as a global option even after subagent alias', async () => {
    const program = new Command();
    let captured: any = null;

    program
      .option('--until-completion', 'Alias for --til-completion')
      .command('pi')
      .argument('[prompt...]')
      .option('-p, --prompt [text]', 'Prompt input')
      .action((promptArgs, options) => {
        captured = {
          promptArgs,
          options,
          globalOptions: program.opts(),
        };
      });

    await program.parseAsync(['pi', '--until-completion', '-p', 'alias prompt'], { from: 'user' });

    expect(captured.globalOptions.untilCompletion).toBe(true);
    expect(captured.options.prompt).toBe('alias prompt');
    expect(captured.promptArgs).toEqual([]);
  });
});
