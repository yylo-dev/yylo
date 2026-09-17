/**
 * Tests for execution status determination when iterations fail.
 *
 * When all iterations fail (e.g., subagent crashes with non-zero exit code),
 * the execution status should be FAILED, not COMPLETED.
 * This prevents misleading "Execution completed successfully!" messages
 * and ensures the process exit code is 1 (not 0).
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { EventEmitter } from 'node:events';
import {
  ExecutionEngine,
  ExecutionStatus,
  type ExecutionRequest,
  type ExecutionEngineConfig,
  DEFAULT_ERROR_RECOVERY_CONFIG,
  DEFAULT_RATE_LIMIT_CONFIG,
  DEFAULT_PROGRESS_CONFIG,
} from '../engine.js';
import type { ToolCallResult, ProgressEvent } from '../../types/execution.js';
import type { JunoTaskConfig } from '../../types/index.js';

// This unit suite exercises iteration outcomes, not installed-controller
// authentication. Real readiness/refusal is covered by agent-startup and
// invocation-lifecycle tests; the global fixture has no artifact evidence.
vi.mock('../../utils/agent-startup.js', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../utils/agent-startup.js')>(),
  checkAgentReadiness: vi.fn().mockResolvedValue(undefined),
}));

// Mock the shell-backend module so the engine can create backends
vi.mock('../backends/shell-backend.js', () => {
  // The mock will be configured per-test via mockShellBackendExecute
  return {
    ShellBackend: vi.fn().mockImplementation(() => ({
      type: 'shell',
      name: 'Shell Backend',
      configure: vi.fn(),
      initialize: vi.fn().mockResolvedValue(undefined),
      execute: vi.fn(),
      cleanup: vi.fn().mockResolvedValue(undefined),
      isAvailable: vi.fn().mockResolvedValue(true),
      onProgress: vi.fn().mockReturnValue(() => {}),
    })),
    formatDuration: vi.fn().mockReturnValue('0s'),
  };
});

const createConfig = (): JunoTaskConfig => ({
  debug: false,
  logLevel: 'info',
  mcp: {
    serverCommand: 'test',
    serverArgs: [],
    timeout: 30000,
    maxConnections: 1,
    retryAttempts: 1,
  },
  subagents: { default: 'claude', available: ['claude', 'pi'] },
  execution: {
    maxIterations: 10,
    timeout: 300000,
    workingDirectory: process.env.JUNO_TASK_ROOT || process.cwd(),
    parallelism: 1,
  },
  ai: { model: 'test', temperature: 0.1, maxTokens: 4096 },
  templates: { searchPaths: ['./templates'], builtInEnabled: true, customEnabled: true },
});

const createRequest = (overrides: Partial<ExecutionRequest> = {}): ExecutionRequest => ({
  requestId: 'test-req-1',
  instruction: 'Test instruction',
  subagent: 'pi',
  workingDirectory: process.env.JUNO_TASK_ROOT || process.cwd(),
  maxIterations: 1,
  ...overrides,
});

/**
 * Helper to configure the mocked ShellBackend's execute method.
 * Must be called before engine.execute() to set up the desired behavior.
 */
async function configureMockBackend(executeFn: (...args: any[]) => Promise<ToolCallResult>) {
  const { ShellBackend } = await import('../backends/shell-backend.js');
  vi.mocked(ShellBackend).mockImplementation(
    () =>
      ({
        type: 'shell',
        name: 'Shell Backend',
        configure: vi.fn(),
        initialize: vi.fn().mockResolvedValue(undefined),
        execute: executeFn,
        cleanup: vi.fn().mockResolvedValue(undefined),
        isAvailable: vi.fn().mockResolvedValue(true),
        onProgress: vi.fn().mockReturnValue(() => {}),
      }) as any,
  );
}

describe('Execution status determination', () => {
  let engine: ExecutionEngine;

  const createEngine = () => {
    const config: ExecutionEngineConfig = {
      config: createConfig(),
      errorRecovery: DEFAULT_ERROR_RECOVERY_CONFIG,
      rateLimitConfig: DEFAULT_RATE_LIMIT_CONFIG,
      progressConfig: DEFAULT_PROGRESS_CONFIG,
    };
    return new ExecutionEngine(config);
  };

  it('should set status to FAILED when all iterations fail', async () => {
    const failResult: ToolCallResult = {
      content: JSON.stringify({
        type: 'result',
        subtype: 'error',
        is_error: true,
        result: 'Error: No API key found',
        error: 'Error: No API key found',
        exit_code: 1,
      }),
      status: 'failed',
      startTime: new Date(),
      endTime: new Date(),
      duration: 100,
      progressEvents: [],
      request: {} as any,
      error: { type: 'shell_execution', message: 'Error: No API key found', timestamp: new Date() },
    };
    await configureMockBackend(vi.fn().mockResolvedValue(failResult));
    engine = createEngine();
    const result = await engine.execute(createRequest({ maxIterations: 1 }));

    expect(result.status).toBe(ExecutionStatus.FAILED);
    expect(result.statistics.failedIterations).toBe(1);
    expect(result.statistics.successfulIterations).toBe(0);
  });

  it('should set status to COMPLETED when at least one iteration succeeds', async () => {
    let callCount = 0;
    await configureMockBackend(
      vi.fn().mockImplementation(async () => {
        callCount++;
        if (callCount === 1) {
          return {
            content: '{"is_error":true}',
            status: 'failed',
            startTime: new Date(),
            endTime: new Date(),
            duration: 100,
            progressEvents: [],
            request: {} as any,
            error: { type: 'shell_execution', message: 'fail', timestamp: new Date() },
          };
        }
        return {
          content: '{"result":"ok"}',
          status: 'completed',
          startTime: new Date(),
          endTime: new Date(),
          duration: 100,
          progressEvents: [],
          request: {} as any,
        };
      }),
    );
    engine = createEngine();
    const result = await engine.execute(createRequest({ maxIterations: 2 }));

    expect(result.status).toBe(ExecutionStatus.COMPLETED);
    expect(result.statistics.successfulIterations).toBe(1);
    expect(result.statistics.failedIterations).toBe(1);
  });

  it('should set status to COMPLETED when all iterations succeed', async () => {
    await configureMockBackend(
      vi.fn().mockResolvedValue({
        content: '{"result":"ok"}',
        status: 'completed',
        startTime: new Date(),
        endTime: new Date(),
        duration: 100,
        progressEvents: [],
        request: {} as any,
      }),
    );
    engine = createEngine();
    const result = await engine.execute(createRequest({ maxIterations: 1 }));

    expect(result.status).toBe(ExecutionStatus.COMPLETED);
    expect(result.statistics.successfulIterations).toBe(1);
    expect(result.statistics.failedIterations).toBe(0);
  });

  it('should set status to FAILED when multiple iterations all fail', async () => {
    await configureMockBackend(
      vi.fn().mockResolvedValue({
        content: '{"is_error":true,"error":"crash"}',
        status: 'failed',
        startTime: new Date(),
        endTime: new Date(),
        duration: 100,
        progressEvents: [],
        request: {} as any,
        error: { type: 'shell_execution', message: 'crash', timestamp: new Date() },
      }),
    );
    engine = createEngine();
    const result = await engine.execute(createRequest({ maxIterations: 3 }));

    expect(result.status).toBe(ExecutionStatus.FAILED);
    expect(result.statistics.failedIterations).toBe(3);
    expect(result.statistics.successfulIterations).toBe(0);
  });

  it('should use exit code 1 when execution status is FAILED', async () => {
    await configureMockBackend(
      vi.fn().mockResolvedValue({
        content: '{"is_error":true}',
        status: 'failed',
        startTime: new Date(),
        endTime: new Date(),
        duration: 100,
        progressEvents: [],
        request: {} as any,
        error: { type: 'shell_execution', message: 'fail', timestamp: new Date() },
      }),
    );
    engine = createEngine();
    const result = await engine.execute(createRequest());

    // The exit code logic in main.ts: result.status === COMPLETED ? 0 : 1
    const exitCode = result.status === ExecutionStatus.COMPLETED ? 0 : 1;
    expect(exitCode).toBe(1);
  });
});
