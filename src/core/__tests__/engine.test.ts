/**
 * @vitest-environment node
 * @fileoverview Tests for ExecutionEngine implementation
 *
 * The engine was refactored to use ShellBackend directly instead of MCPClient.
 * Tests mock the ShellBackend dynamic import and validate the full execution flow.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { EventEmitter } from 'node:events';
import type { ToolCallResult, ToolCallRequest } from '../../types/execution.js';
import type { JunoTaskConfig } from '../../types/index.js';

// ---------------------------------------------------------------------------
// Mock setup (hoisted so vi.mock factories can reference them)
// ---------------------------------------------------------------------------

const mocks = vi.hoisted(() => ({
  execute: vi.fn(),
  cleanup: vi.fn().mockResolvedValue(undefined),
  initialize: vi.fn().mockResolvedValue(undefined),
  isAvailable: vi.fn().mockResolvedValue(true),
  configure: vi.fn(),
  onProgress: vi.fn().mockReturnValue(() => {}),
  resolvePromptCommandSubstitutions: vi.fn(),
  resolvePromptMacros: vi.fn(),
  executeHook: vi.fn().mockResolvedValue(undefined),
  checkLedgerReadiness: vi.fn().mockResolvedValue('/fixture/yylo-ledger'),
  assessControllerGeneration: vi.fn(),
}));

vi.mock('../backends/shell-backend.js', () => ({
  ShellBackend: vi.fn().mockImplementation(() => ({
    execute: mocks.execute,
    cleanup: mocks.cleanup,
    initialize: mocks.initialize,
    isAvailable: mocks.isAvailable,
    configure: mocks.configure,
    onProgress: mocks.onProgress,
    name: 'Shell Backend',
    type: 'shell',
  })),
  formatDuration: (ms: number) => `${Math.round(ms / 1000)}s`,
}));

vi.mock('../../cli/utils/advanced-logger.js', () => ({
  engineLogger: {
    info: vi.fn(),
    warn: vi.fn(),
    error: vi.fn(),
    debug: vi.fn(),
  },
}));

vi.mock('../../cli/commands/ledger.js', () => ({ checkLedgerReadiness: mocks.checkLedgerReadiness }));
// Engine unit tests own execution orchestration, not real package migration.
// Generation admission (including refusal) is covered by the startup/packed suites.
vi.mock('../../utils/controller-generation-startup.js', () => ({ ensureControllerGeneration: mocks.assessControllerGeneration,
  reuseControllerCommandAdmission: vi.fn(async () => false) }));
vi.mock('../../utils/controller-generation-migration.js', () => ({
  packagedGenerationRoot: () => '/installed/package',
  acquireControllerGenerationReadLease: vi.fn(async () => Object.assign(async () => {}, { assertHeld() {} })),
}));

vi.mock('../../utils/hooks.js', () => ({
  executeHook: mocks.executeHook,
}));

vi.mock('../prompt-command-substitution.js', () => ({
  resolvePromptCommandSubstitutions: mocks.resolvePromptCommandSubstitutions,
}));

vi.mock('../prompt-macro-resolver.js', () => ({
  resolvePromptMacros: mocks.resolvePromptMacros,
}));

// ---------------------------------------------------------------------------
// Imports (after mocks)
// ---------------------------------------------------------------------------

import {
  ExecutionEngine,
  type ExecutionRequest,
  type ExecutionEngineConfig,
  ExecutionStatus,
  DEFAULT_ERROR_RECOVERY_CONFIG,
  DEFAULT_RATE_LIMIT_CONFIG,
  DEFAULT_PROGRESS_CONFIG,
  createExecutionEngine,
  createExecutionRequest,
} from '../engine.js';
import {
  RateLimitError,
  ConnectionError,
  TimeoutError,
  ValidationError,
  ToolError,
} from '../errors.js';

// ---------------------------------------------------------------------------
// Test helpers
// ---------------------------------------------------------------------------

function createMockToolResult(overrides?: Partial<ToolCallResult>): ToolCallResult {
  return {
    content: 'Test result',
    status: 'completed' as any,
    startTime: new Date(),
    endTime: new Date(),
    duration: 100,
    progressEvents: [],
    request: { toolName: 'claude_subagent', arguments: {} } as any,
    ...overrides,
  };
}

const createMockConfig = (): JunoTaskConfig => ({
  defaultSubagent: 'claude',
  defaultMaxIterations: 10,
  defaultBackend: 'shell',
  logLevel: 'info',
  verbose: 0,
  quiet: false,
  mcpTimeout: 30000,
  mcpRetries: 3,
  onHourlyLimit: 'raise',
  interactive: false,
  headlessMode: true,
  workingDirectory: process.env.JUNO_TASK_ROOT || process.cwd(),
  sessionDirectory: '/tmp/juno-test-sessions',
  skipHooks: true,
});

function makeRequest(overrides?: Partial<ExecutionRequest>): ExecutionRequest {
  return {
    requestId: 'test-request-123',
    instruction: 'Test instruction',
    subagent: 'claude',
    backend: 'shell',
    workingDirectory: process.env.JUNO_TASK_ROOT || process.cwd(),
    maxIterations: 1,
    ...overrides,
  } as ExecutionRequest;
}

// ---------------------------------------------------------------------------
// Test suite
// ---------------------------------------------------------------------------

describe('ExecutionEngine', () => {
  let engine: ExecutionEngine;
  let engineConfig: ExecutionEngineConfig;
  let processOnSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(async () => {
    // Prevent engine from registering global process handlers
    processOnSpy = vi.spyOn(process, 'on').mockReturnValue(process);

    // Re-set hoisted mock implementations (vitest mockReset clears them)
    mocks.cleanup.mockResolvedValue(undefined);
    mocks.initialize.mockResolvedValue(undefined);
    mocks.isAvailable.mockResolvedValue(true);
    mocks.configure.mockReturnValue(undefined);
    mocks.onProgress.mockReturnValue(() => {});
    mocks.resolvePromptCommandSubstitutions.mockImplementation(async (instruction: string) => instruction);
    mocks.resolvePromptMacros.mockImplementation((instruction: string) => ({
      resolvedPrompt: instruction,
      warnings: [],
    }));
    mocks.executeHook.mockResolvedValue(undefined);
    mocks.checkLedgerReadiness.mockResolvedValue('/fixture/yylo-ledger');
    mocks.assessControllerGeneration.mockResolvedValue({ disposition: 'ready' });

    // Re-set ShellBackend constructor (mockReset clears its mockImplementation)
    const { ShellBackend } = await import('../backends/shell-backend.js');
    (ShellBackend as any).mockImplementation(() => ({
      execute: mocks.execute,
      cleanup: mocks.cleanup,
      initialize: mocks.initialize,
      isAvailable: mocks.isAvailable,
      configure: mocks.configure,
      onProgress: mocks.onProgress,
      name: 'Shell Backend',
      type: 'shell',
    }));

    engineConfig = {
      config: createMockConfig(),
      errorRecovery: DEFAULT_ERROR_RECOVERY_CONFIG,
      rateLimitConfig: DEFAULT_RATE_LIMIT_CONFIG,
      progressConfig: DEFAULT_PROGRESS_CONFIG,
    };

    engine = new ExecutionEngine(engineConfig);
    engine.setMaxListeners(20);

    // Default backend mock: successful execution
    mocks.execute.mockResolvedValue(createMockToolResult());

    // Speed up tests by making sleep a no-op
    vi.spyOn(engine as any, 'sleep').mockResolvedValue(undefined);
  });

  afterEach(async () => {
    await engine.shutdown(1000);
    processOnSpy.mockRestore();
  });

  // =========================================================================
  // Initialization
  // =========================================================================

  describe('initialization', () => {
    it('should create an ExecutionEngine instance', () => {
      expect(engine).toBeInstanceOf(ExecutionEngine);
      expect(engine).toBeInstanceOf(EventEmitter);
    });

    it('should set up error handling on construction', () => {
      expect(processOnSpy).toHaveBeenCalledWith('uncaughtException', expect.any(Function));
      expect(processOnSpy).toHaveBeenCalledWith('unhandledRejection', expect.any(Function));
    });
  });

  // =========================================================================
  // Request validation
  // =========================================================================

  describe('request validation', () => {
    it('should validate a proper execution request', () => {
      const request = makeRequest();
      expect(() => engine['validateRequest'](request)).not.toThrow();
    });

    it('should reject request with empty instruction', () => {
      const request = makeRequest({ instruction: '' });
      expect(() => engine['validateRequest'](request)).toThrow('Instruction is required');
    });

    it('should allow empty instruction for pi live interactive continue sessions', () => {
      const request = makeRequest({
        instruction: '',
        subagent: 'pi',
        live: true,
        liveInteractiveSession: true,
        resume: 'resume-live-001',
      });
      expect(() => engine['validateRequest'](request)).not.toThrow();
    });

    it('should reject request with missing subagent', () => {
      const request = makeRequest({ subagent: '' as any });
      expect(() => engine['validateRequest'](request)).toThrow('Subagent is required');
    });

    it('should reject request with zero or negative iterations', () => {
      const request = makeRequest({ maxIterations: 0 });
      expect(() => engine['validateRequest'](request)).toThrow(
        'Max iterations must be a positive number or -1 for unlimited',
      );
    });

    it('should reject request with NaN iterations (Issue #57 fix)', () => {
      const request = makeRequest({ maxIterations: NaN });
      expect(() => engine['validateRequest'](request)).toThrow(
        'Max iterations must be a positive number or -1 for unlimited',
      );
    });

    it('should validate request ID', () => {
      const request = makeRequest({ requestId: '' });
      expect(() => engine['validateRequest'](request)).toThrow('Request ID is required');
    });

    it('should validate working directory', () => {
      const request = makeRequest({ workingDirectory: '' });
      expect(() => engine['validateRequest'](request)).toThrow('Working directory is required');
    });

    it('should handle whitespace-only fields', () => {
      const request = makeRequest({ requestId: '   ', instruction: '\t\n  ' });
      expect(() => engine['validateRequest'](request)).toThrow();
    });
  });

  // =========================================================================
  // Execution
  // =========================================================================

  describe('execution', () => {
    it('should execute a simple request successfully', async () => {
      const request = makeRequest();

      const result = await engine.execute(request);

      expect(result).toBeDefined();
      expect(result.status).toBe(ExecutionStatus.COMPLETED);
      expect(result.request).toEqual(request);
      expect(result.iterations).toHaveLength(1);
      expect(mocks.execute).toHaveBeenCalledOnce();
    });

    it('fails closed before hooks or backend initialization on a genuine readiness failure', async () => {
      engineConfig.config.skipHooks = true; // Dependency admission is not an optional user hook.
      mocks.checkLedgerReadiness.mockRejectedValue(new Error('known version mismatch'));
      await expect(engine.execute(makeRequest())).rejects.toThrow(/Dependency preflight.*known version mismatch/);
      expect(mocks.executeHook).not.toHaveBeenCalled();
      expect(mocks.initialize).not.toHaveBeenCalled();
      expect(mocks.execute).not.toHaveBeenCalled();
    });

    it('removes the legacy installer while preserving custom hooks and product cwd', async () => {
      engineConfig.config.skipHooks = false;
      engineConfig.config.hooks = { START_RUN: { commands: [
        './.juno_task/scripts/install_requirements.sh', 'echo owner hook',
      ] } } as any;
      mocks.executeHook.mockResolvedValue({ commandResults: [], success: true });
      const request = makeRequest();
      await engine.execute(request);
      expect(mocks.executeHook).toHaveBeenCalledWith('START_RUN',
        { START_RUN: { commands: ['echo owner hook'] } },
        expect.objectContaining({ workingDirectory: request.workingDirectory }), expect.anything());
      expect(mocks.checkLedgerReadiness).toHaveBeenCalledWith({ cwd: expect.any(String) });
    });

    it('should handle multiple iterations', async () => {
      const request = makeRequest({ maxIterations: 2 });

      const result = await engine.execute(request);

      expect(result.status).toBe(ExecutionStatus.COMPLETED);
      expect(result.iterations).toHaveLength(2);
      expect(mocks.execute).toHaveBeenCalledTimes(2);
    });

    it('should resolve prompt command substitutions on every iteration', async () => {
      mocks.resolvePromptCommandSubstitutions.mockImplementation(async (instruction: string) => `${instruction} [resolved]`);

      const request = makeRequest({
        instruction: "Run !'date' every iteration",
        maxIterations: 2,
      });

      const result = await engine.execute(request);

      expect(result.status).toBe(ExecutionStatus.COMPLETED);
      expect(mocks.resolvePromptCommandSubstitutions).toHaveBeenCalledTimes(2);
      expect(mocks.resolvePromptCommandSubstitutions).toHaveBeenNthCalledWith(
        1,
        request.instruction,
        expect.objectContaining({ workingDirectory: request.workingDirectory }),
      );
      expect(mocks.resolvePromptCommandSubstitutions).toHaveBeenNthCalledWith(
        2,
        request.instruction,
        expect.objectContaining({ workingDirectory: request.workingDirectory }),
      );
      expect(mocks.execute).toHaveBeenCalledTimes(2);
      expect(mocks.execute).toHaveBeenNthCalledWith(
        1,
        expect.objectContaining({
          arguments: expect.objectContaining({ instruction: `${request.instruction} [resolved]` }),
        }),
      );
    });

    it('should emit resolved instruction before executing the backend tool call', async () => {
      const request = makeRequest({ instruction: "Run !'echo ready'" });
      const resolvedInstruction = 'Run ready';
      const onInstructionResolved = vi.fn();

      mocks.resolvePromptCommandSubstitutions.mockResolvedValue(resolvedInstruction);
      engine.on('iteration:instruction-resolved', onInstructionResolved);

      mocks.execute.mockImplementation(async (toolRequest: ToolCallRequest) => {
        expect(onInstructionResolved).toHaveBeenCalledTimes(1);

        const payload = onInstructionResolved.mock.calls[0]?.[0] as Record<string, unknown>;
        expect(payload).toMatchObject({
          iterationNumber: 1,
          instruction: resolvedInstruction,
          templateInstruction: request.instruction,
        });

        expect(toolRequest.arguments).toMatchObject({ instruction: resolvedInstruction });
        return createMockToolResult();
      });

      const result = await engine.execute(request);

      expect(result.status).toBe(ExecutionStatus.COMPLETED);
      expect(onInstructionResolved).toHaveBeenCalledTimes(1);
    });

    it('should apply prompt macros before command substitution by default', async () => {
      const request = makeRequest({ instruction: 'Run @@ship then done' });
      mocks.resolvePromptMacros.mockReturnValue({
        resolvedPrompt: "Run !'echo ready' then done",
        warnings: [],
      });
      mocks.resolvePromptCommandSubstitutions.mockResolvedValue('Run ready then done');

      const result = await engine.execute(request);

      expect(result.status).toBe(ExecutionStatus.COMPLETED);
      expect(mocks.resolvePromptMacros).toHaveBeenCalledWith(
        request.instruction,
        expect.objectContaining({ maxDepth: 10 }),
      );
      expect(mocks.resolvePromptCommandSubstitutions).toHaveBeenCalledWith(
        "Run !'echo ready' then done",
        expect.objectContaining({ workingDirectory: request.workingDirectory }),
      );
    });

    it('should support after_command_substitution ordering override for prompt macros', async () => {
      engineConfig.config.promptMacros = {
        enabled: true,
        order: 'after_command_substitution',
        maxDepth: 10,
        global: {},
        local: {},
      };
      engine = new ExecutionEngine(engineConfig);
      vi.spyOn(engine as any, 'sleep').mockResolvedValue(undefined);

      const request = makeRequest({ instruction: "Run !'echo @@ship'" });
      mocks.resolvePromptCommandSubstitutions.mockResolvedValue('Run @@ship');
      mocks.resolvePromptMacros.mockReturnValue({ resolvedPrompt: 'Run deploy', warnings: [] });

      const result = await engine.execute(request);

      expect(result.status).toBe(ExecutionStatus.COMPLETED);
      expect(mocks.resolvePromptCommandSubstitutions).toHaveBeenCalledWith(
        request.instruction,
        expect.any(Object),
      );
      expect(mocks.resolvePromptMacros).toHaveBeenCalledWith('Run @@ship', expect.any(Object));
    });

    it('should include prompt macro warnings in instruction-resolved event payload', async () => {
      const request = makeRequest({ instruction: 'Run @@unk now' });
      const onInstructionResolved = vi.fn();
      engine.on('iteration:instruction-resolved', onInstructionResolved);

      mocks.resolvePromptMacros.mockReturnValue({
        resolvedPrompt: 'Run @@unk now',
        warnings: [
          {
            code: 'unresolved',
            key: 'unk',
            token: '@@unk',
            message: 'Unresolved prompt macro @@unk; leaving token unchanged.',
          },
        ],
      });

      const result = await engine.execute(request);

      expect(result.status).toBe(ExecutionStatus.COMPLETED);
      const payload = onInstructionResolved.mock.calls[0]?.[0] as Record<string, any>;
      expect(payload.warnings).toEqual([
        { message: 'Unresolved prompt macro @@unk; leaving token unchanged.' },
      ]);
    });

    it('should respect maxIterations limit', async () => {
      const request = makeRequest({ maxIterations: 2 });

      const result = await engine.execute(request);

      expect(result.status).toBe(ExecutionStatus.COMPLETED);
      expect(result.iterations).toHaveLength(2);
      expect(mocks.execute).toHaveBeenCalledTimes(2);
    });

    it('should handle execution cancellation via AbortSignal', async () => {
      const abortController = new AbortController();

      // Abort during first iteration so subsequent iterations are skipped
      mocks.execute.mockImplementation(async () => {
        abortController.abort();
        return createMockToolResult();
      });

      const request = makeRequest({ maxIterations: 5 });
      const result = await engine.execute(request, abortController.signal);

      // Should stop after first iteration due to abort
      expect(result.iterations.length).toBeLessThan(5);
      expect(result.status).toBe(ExecutionStatus.COMPLETED);
    });

    it('should initialize ShellBackend during execution', async () => {
      const request = makeRequest();
      await engine.execute(request);

      expect(mocks.configure).toHaveBeenCalledOnce();
      expect(mocks.initialize).toHaveBeenCalledOnce();
      expect(mocks.isAvailable).toHaveBeenCalledOnce();
    });

    it('should propagate project shortcuts and selected subagent to the service environment', async () => {
      engineConfig.config.modelShortcuts = {
        claude: { ':fav': 'anthropic/project-model' },
        codex: { ':fav': 'openai/project-model' },
      };
      await engine.execute(makeRequest({ subagent: 'codex' }));

      const configured = mocks.configure.mock.calls[0]?.[0];
      expect(configured.environment.JUNO_SELECTED_SUBAGENT).toBe('codex');
      expect(JSON.parse(configured.environment.JUNO_MODEL_SHORTCUTS)).toEqual(
        engineConfig.config.modelShortcuts,
      );
    });
  });

  // =========================================================================
  // Error handling
  // =========================================================================

  describe('error handling', () => {
    it('should handle rate limit errors with retry', async () => {
      const rateLimitError = RateLimitError.hourly(new Date(Date.now() + 1000), 0);

      mocks.execute
        .mockRejectedValueOnce(rateLimitError)
        .mockResolvedValueOnce(createMockToolResult());

      const request = makeRequest();

      const result = await engine.execute(request);

      expect(result.status).toBe(ExecutionStatus.COMPLETED);
      expect(mocks.execute).toHaveBeenCalledTimes(2);
    });

    it('should handle connection errors', async () => {
      const connectionError = ConnectionError.serverNotFound('test-server');
      mocks.execute.mockRejectedValueOnce(connectionError).mockResolvedValueOnce(
        createMockToolResult(),
      );

      const request = makeRequest({ maxIterations: 2 });

      const result = await engine.execute(request);

      expect(result.status).toBe(ExecutionStatus.COMPLETED);
      expect(result.iterations[0].error).toBeInstanceOf(ConnectionError);
      expect(result.statistics.errorBreakdown.connection).toBe(1);
    });

    it('should handle timeout errors', async () => {
      const timeoutError = TimeoutError.toolExecution('test-tool', 30000);
      mocks.execute.mockRejectedValueOnce(timeoutError).mockResolvedValueOnce(
        createMockToolResult(),
      );

      const request = makeRequest({ maxIterations: 2 });

      const result = await engine.execute(request);

      expect(result.status).toBe(ExecutionStatus.COMPLETED);
      expect(result.iterations[0].error).toBeInstanceOf(TimeoutError);
    });

    it('should handle validation errors', async () => {
      const validationError = ValidationError.required('instruction');
      mocks.execute.mockRejectedValue(validationError);

      const request = makeRequest();

      const result = await engine.execute(request);

      expect(result.status).toBe(ExecutionStatus.FAILED);
      expect(result.iterations[0].error).toBeInstanceOf(ValidationError);
    });

    it('should wrap unknown errors', async () => {
      const unknownError = new Error('Unknown error');
      mocks.execute.mockRejectedValue(unknownError);

      const request = makeRequest();

      const result = await engine.execute(request);

      expect(result.status).toBe(ExecutionStatus.FAILED);
      expect(result.iterations[0].error).toBeDefined();
    });

    it('should handle iteration errors that should not continue', async () => {
      const validationError = ValidationError.required('instruction');
      mocks.execute.mockRejectedValue(validationError);

      const request = makeRequest({ maxIterations: 3 });

      const result = await engine.execute(request);

      expect(result.status).toBe(ExecutionStatus.FAILED);
      expect(result.iterations).toHaveLength(1);
      expect(result.iterations[0].success).toBe(false);
      expect(result.statistics.errorBreakdown.validation).toBe(1);
    });
  });

  // =========================================================================
  // Progress tracking
  // =========================================================================

  describe('progress tracking', () => {
    it('should emit execution events', async () => {
      const startSpy = vi.fn();
      const completeSpy = vi.fn();

      engine.on('execution:start', startSpy);
      engine.on('execution:complete', completeSpy);

      const request = makeRequest();
      await engine.execute(request);

      expect(startSpy).toHaveBeenCalledOnce();
      expect(completeSpy).toHaveBeenCalledOnce();
    });

    it('should track progress events via tool request callback', async () => {
      // Make the backend's execute call the progressCallback
      mocks.execute.mockImplementation(async (toolRequest: ToolCallRequest) => {
        if (toolRequest.progressCallback) {
          await toolRequest.progressCallback({
            type: 'info',
            timestamp: new Date(),
            content: 'Progress update',
            sessionId: 'test-session',
            backend: 'claude',
            count: 1,
            toolId: 'tool-1',
          } as any);
        }
        return createMockToolResult();
      });

      const request = makeRequest();
      const result = await engine.execute(request);

      expect(result.status).toBe(ExecutionStatus.COMPLETED);
      expect(result.statistics.totalProgressEvents).toBe(1);
      expect(result.progressEvents).toHaveLength(1);
    });
  });

  // =========================================================================
  // Rate limit handling
  // =========================================================================

  describe('rate limit handling', () => {
    it('should provide rate limit information', async () => {
      const rateLimitInfo = await engine.getRateLimitInfo();

      expect(rateLimitInfo).toBeDefined();
      expect(rateLimitInfo.remaining).toBe(100);
      expect(rateLimitInfo.resetTime).toBeDefined();
    });

    it('should calculate rate limit wait time correctly', () => {
      const resetTime = new Date(Date.now() + 30000);
      const rateLimitError = RateLimitError.hourly(resetTime, 0);

      const waitTime = engine['calculateRateLimitWaitTime'](rateLimitError);

      expect(waitTime).toBeGreaterThan(25000);
      expect(waitTime).toBeLessThan(35000);
    });

    it('should handle rate limits exceeding maximum wait time', async () => {
      const customEngine = new ExecutionEngine({
        ...engineConfig,
        rateLimitConfig: { ...DEFAULT_RATE_LIMIT_CONFIG, maxWaitTimeMs: 1000 },
      });
      customEngine.setMaxListeners(20);
      vi.spyOn(customEngine as any, 'sleep').mockResolvedValue(undefined);

      const rateLimitError = RateLimitError.hourly(new Date(Date.now() + 3600000), 0);
      mocks.execute.mockRejectedValue(rateLimitError);

      const request = makeRequest();
      const result = await customEngine.execute(request);

      expect(result.status).toBe(ExecutionStatus.FAILED);
      expect(result.error?.message).toContain('exceeds maximum allowed');

      await customEngine.shutdown(1000);
    });

    it('should disable rate limit handling when configured', async () => {
      const customEngine = new ExecutionEngine({
        ...engineConfig,
        rateLimitConfig: { ...DEFAULT_RATE_LIMIT_CONFIG, enabled: false },
      });
      customEngine.setMaxListeners(20);
      vi.spyOn(customEngine as any, 'sleep').mockResolvedValue(undefined);

      const rateLimitError = RateLimitError.hourly(new Date(Date.now() + 60000), 0);
      mocks.execute.mockRejectedValue(rateLimitError);

      const request = makeRequest();
      const result = await customEngine.execute(request);

      expect(result.status).toBe(ExecutionStatus.RATE_LIMITED);
      expect(result.statistics.rateLimitWaitTime).toBe(0);

      await customEngine.shutdown(1000);
    });

    it('should emit proper rate limit events', async () => {
      const rateLimitStartSpy = vi.fn();
      const rateLimitEndSpy = vi.fn();

      engine.on('rate-limit:start', rateLimitStartSpy);
      engine.on('rate-limit:end', rateLimitEndSpy);

      const rateLimitError = RateLimitError.hourly(new Date(Date.now() + 500), 0);
      mocks.execute
        .mockRejectedValueOnce(rateLimitError)
        .mockResolvedValueOnce(createMockToolResult());

      const request = makeRequest();
      const result = await engine.execute(request);

      expect(result.status).toBe(ExecutionStatus.COMPLETED);
      expect(rateLimitStartSpy).toHaveBeenCalledWith(
        expect.objectContaining({
          error: rateLimitError,
          waitTimeMs: expect.any(Number),
        }),
      );
      expect(rateLimitEndSpy).toHaveBeenCalled();
    });

    it('should handle rate limit with tier information', async () => {
      const rateLimitError = new RateLimitError(
        'Rate limit exceeded',
        5,
        new Date(Date.now() + 30_000),
        'premium',
      );

      mocks.execute
        .mockRejectedValueOnce(rateLimitError)
        .mockResolvedValueOnce(createMockToolResult());

      const request = makeRequest();
      const result = await engine.execute(request);

      expect(result.status).toBe(ExecutionStatus.COMPLETED);
      expect(result.statistics.rateLimitEncounters).toBe(1);
      expect(result.statistics.rateLimitWaitTime).toBeGreaterThan(0);
    });
  });

  // =========================================================================
  // Shutdown
  // =========================================================================

  describe('shutdown', () => {
    it('should shutdown gracefully', async () => {
      await expect(engine.shutdown(5000)).resolves.not.toThrow();
    });

    it('should cancel active executions during shutdown', async () => {
      mocks.execute.mockImplementation(async () => {
        await new Promise((resolve) => setTimeout(resolve, 1000));
        return createMockToolResult();
      });

      const request = makeRequest();
      const executionPromise = engine.execute(request);
      const shutdownPromise = engine.shutdown(2000);

      await Promise.allSettled([executionPromise, shutdownPromise]);
    });

    it('should handle shutdown with timeout events', async () => {
      const shutdownStartSpy = vi.fn();
      const shutdownCompleteSpy = vi.fn();

      engine.on('engine:shutdown:start', shutdownStartSpy);
      engine.on('engine:shutdown:complete', shutdownCompleteSpy);

      await engine.shutdown(1000);

      expect(shutdownStartSpy).toHaveBeenCalled();
      expect(shutdownCompleteSpy).toHaveBeenCalled();
    });

    it('should handle multiple shutdown calls', async () => {
      await engine.shutdown(1000);
      await engine.shutdown(1000);
      expect(true).toBe(true);
    });

    it('should handle shutdown with error', async () => {
      const errorEngine = new ExecutionEngine(engineConfig);
      errorEngine.setMaxListeners(20);

      errorEngine['cleanupTasks'].push(async () => {
        throw new Error('Cleanup failed');
      });

      await expect(errorEngine.shutdown(1000)).rejects.toThrow('Cleanup failed');
    });
  });

  // =========================================================================
  // Utility methods
  // =========================================================================

  describe('utility methods', () => {
    it('should get correct tool name for subagent', () => {
      expect(engine['getToolNameForSubagent']('claude')).toBe('claude_subagent');
      expect(engine['getToolNameForSubagent']('cursor')).toBe('cursor_subagent');
      expect(engine['getToolNameForSubagent']('codex')).toBe('codex_subagent');
      expect(engine['getToolNameForSubagent']('gemini')).toBe('gemini_subagent');
    });

    it('should determine correct error status', () => {
      const connectionError = ConnectionError.serverNotFound('test');
      const rateLimitError = RateLimitError.hourly(new Date(), 0);
      const timeoutError = TimeoutError.toolExecution('test', 30000);

      expect(engine['determineErrorStatus'](connectionError)).toBe(ExecutionStatus.FAILED);
      expect(engine['determineErrorStatus'](rateLimitError)).toBe(ExecutionStatus.RATE_LIMITED);
      expect(engine['determineErrorStatus'](timeoutError)).toBe(ExecutionStatus.TIMEOUT);
    });
  });

  // =========================================================================
  // Session context creation
  // =========================================================================

  describe('session context creation', () => {
    it('should create proper session context', () => {
      const request = makeRequest();
      const sessionContext = engine['createSessionContext'](request);

      expect(sessionContext.sessionId).toBeDefined();
      expect(sessionContext.metadata.workingDirectory).toBe(request.workingDirectory);
      expect(sessionContext.metadata.subagent).toBe(request.subagent);
      expect(sessionContext.startTime).toBeInstanceOf(Date);
    });

    it('should include session metadata', () => {
      const request = makeRequest({
        sessionMetadata: { customField: 'test-value' },
      });

      const sessionContext = engine['createSessionContext'](request);

      expect(sessionContext.metadata.customField).toBe('test-value');
      expect(sessionContext.state).toBe('initializing');
      expect(sessionContext.userId).toBe('system');
    });

    it('should create session context with all metadata', () => {
      const request = makeRequest({
        requestId: 'test-session-context',
        subagent: 'cursor',
        workingDirectory: '/test/dir',
        maxIterations: 10,
        sessionMetadata: {
          customKey: 'customValue',
          userId: 'test-user',
          projectId: 'test-project',
        },
      });

      const sessionContext = engine['createSessionContext'](request);

      expect(sessionContext.sessionId).toBe(`session-${request.requestId}`);
      expect(sessionContext.startTime).toBeInstanceOf(Date);
      expect(sessionContext.userId).toBe('system');
      expect(sessionContext.metadata.subagent).toBe('cursor');
      expect(sessionContext.metadata.workingDirectory).toBe('/test/dir');
      expect(sessionContext.metadata.customKey).toBe('customValue');
      expect(sessionContext.metadata.userId).toBe('test-user');
      expect(sessionContext.metadata.projectId).toBe('test-project');
      expect(sessionContext.activeToolCalls).toEqual([]);
      expect(sessionContext.state).toBe('initializing');
      expect(sessionContext.lastActivity).toBeInstanceOf(Date);
    });
  });

  // =========================================================================
  // Advanced execution engine scenarios
  // =========================================================================

  describe('advanced execution engine scenarios', () => {
    it('should handle createExecutionContext with external abort signal', () => {
      const request = makeRequest({ maxIterations: 5 });
      const externalAbort = new AbortController();
      const context = engine['createExecutionContext'](request, externalAbort.signal);

      expect(context.request).toBe(request);
      expect(context.status).toBe(ExecutionStatus.PENDING);
      expect(context.startTime).toBeInstanceOf(Date);
      expect(context.endTime).toBeNull();
      expect(context.iterations).toEqual([]);
      expect(context.statistics.totalIterations).toBe(0);
      expect(context.progressEvents).toEqual([]);
      expect(context.error).toBeNull();
      expect(context.abortController).toBeInstanceOf(AbortController);
      expect(context.sessionContext.sessionId).toContain('session-');
      expect(context.rateLimitInfo.isRateLimited).toBe(false);

      // Test external abort signal chaining
      externalAbort.abort();
      expect(context.abortController.signal.aborted).toBe(true);
    });

    it('should create proper initial statistics', () => {
      const stats = engine['createInitialStatistics']();

      expect(stats.totalIterations).toBe(0);
      expect(stats.successfulIterations).toBe(0);
      expect(stats.failedIterations).toBe(0);
      expect(stats.averageIterationDuration).toBe(0);
      expect(stats.totalToolCalls).toBe(0);
      expect(stats.totalProgressEvents).toBe(0);
      expect(stats.rateLimitEncounters).toBe(0);
      expect(stats.rateLimitWaitTime).toBe(0);
      expect(stats.errorBreakdown).toEqual({});
      expect(stats.performanceMetrics.cpuUsage).toBe(0);
      expect(stats.performanceMetrics.memoryUsage).toBe(0);
      expect(stats.performanceMetrics.networkRequests).toBe(0);
      expect(stats.performanceMetrics.fileSystemOperations).toBe(0);
      expect(stats.performanceMetrics.throughput.iterationsPerMinute).toBe(0);
      expect(stats.performanceMetrics.throughput.progressEventsPerSecond).toBe(0);
      expect(stats.performanceMetrics.throughput.toolCallsPerMinute).toBe(0);
    });

    it('should handle shouldStopIterating with various conditions', () => {
      const request = makeRequest({ maxIterations: 3 });
      const context = engine['createExecutionContext'](request);

      expect(engine['shouldStopIterating'](context, 1)).toBe(false);
      expect(engine['shouldStopIterating'](context, 2)).toBe(false);
      expect(engine['shouldStopIterating'](context, 3)).toBe(false);

      // Should stop when exceeding max iterations
      expect(engine['shouldStopIterating'](context, 4)).toBe(true);

      // Test with unlimited iterations (-1)
      const unlimitedRequest = makeRequest({ maxIterations: -1 });
      const unlimitedContext = engine['createExecutionContext'](unlimitedRequest);
      expect(engine['shouldStopIterating'](unlimitedContext, 1000)).toBe(false);

      // Test with abort signal
      context.abortController.abort();
      expect(engine['shouldStopIterating'](context, 1)).toBe(true);

      // Test with shutdown
      engine['isShuttingDown'] = true;
      const normalContext = engine['createExecutionContext'](request);
      expect(engine['shouldStopIterating'](normalContext, 1)).toBe(true);
      engine['isShuttingDown'] = false;
    });

    it('should check abort signal and throw when aborted', () => {
      const request = makeRequest();
      const context = engine['createExecutionContext'](request);

      expect(() => engine['checkAbortSignal'](context)).not.toThrow();

      context.abortController.abort();
      expect(() => engine['checkAbortSignal'](context)).toThrow('Execution aborted');
    });

    it('should handle sleep utility correctly', async () => {
      const freshEngine = new ExecutionEngine(engineConfig);
      freshEngine.setMaxListeners(20);

      const startTime = Date.now();
      await freshEngine['sleep'](50);
      const endTime = Date.now();

      expect(endTime - startTime).toBeGreaterThanOrEqual(40);
      expect(endTime - startTime).toBeLessThan(200);

      await freshEngine.shutdown(1000);
    });
  });

  // =========================================================================
  // Comprehensive error handling scenarios
  // =========================================================================

  describe('comprehensive error handling scenarios', () => {
    it('should wrap non-execution errors correctly', () => {
      const regularError = new Error('Regular error');
      const wrappedError = engine['wrapError'](regularError);

      expect(wrappedError.type).toBe('tool_execution');
      expect(wrappedError.message).toContain('Regular error');
      expect(wrappedError.timestamp).toBeInstanceOf(Date);
    });

    it('should pass through execution errors unchanged', () => {
      const connectionError = ConnectionError.serverNotFound('test-server');
      const wrappedError = engine['wrapError'](connectionError);

      expect(wrappedError).toBe(connectionError);
    });

    it('should handle string errors', () => {
      const stringError = 'String error message';
      const wrappedError = engine['wrapError'](stringError);

      expect(wrappedError.type).toBe('tool_execution');
      expect(wrappedError.message).toBe('String error message');
    });

    it('should handle null/undefined errors', () => {
      const wrappedNull = engine['wrapError'](null);
      expect(wrappedNull.type).toBe('tool_execution');
      expect(wrappedNull.message).toBe('null');

      const wrappedUndefined = engine['wrapError'](undefined);
      expect(wrappedUndefined.message).toBe('undefined');
    });

    it('should classify connection-like errors', () => {
      const pipeError = new Error('EPIPE: broken pipe');
      const wrappedError = engine['wrapError'](pipeError);
      expect(wrappedError.type).toBe('connection');

      const socketError = new Error('socket hang up');
      const wrappedSocket = engine['wrapError'](socketError);
      expect(wrappedSocket.type).toBe('connection');
    });

    it('should handle iteration errors with recovery failure', async () => {
      const customRecoveryStrategy = vi.fn().mockRejectedValue(new Error('Recovery failed'));
      const customEngine = new ExecutionEngine({
        ...engineConfig,
        errorRecovery: {
          ...DEFAULT_ERROR_RECOVERY_CONFIG,
          continueOnError: {
            ...DEFAULT_ERROR_RECOVERY_CONFIG.continueOnError,
            connection: true,
          },
          customStrategies: { connection: customRecoveryStrategy },
        },
      });
      customEngine.setMaxListeners(20);
      vi.spyOn(customEngine as any, 'sleep').mockResolvedValue(undefined);

      const connectionError = ConnectionError.serverNotFound('test-server');
      mocks.execute
        .mockRejectedValueOnce(connectionError)
        .mockResolvedValueOnce(createMockToolResult());

      const request = makeRequest({ maxIterations: 2 });
      const result = await customEngine.execute(request);

      expect(customRecoveryStrategy).toHaveBeenCalledWith(connectionError);
      expect(result.status).toBe(ExecutionStatus.COMPLETED);
      expect(result.statistics.errorBreakdown.connection).toBe(1);

      await customEngine.shutdown(1000);
    });
  });

  // =========================================================================
  // Error recovery strategies
  // =========================================================================

  describe('error recovery strategies', () => {
    it('should apply custom recovery strategies', async () => {
      const customRecoveryStrategy = vi.fn().mockResolvedValue(true);
      const customEngine = new ExecutionEngine({
        ...engineConfig,
        errorRecovery: {
          ...DEFAULT_ERROR_RECOVERY_CONFIG,
          customStrategies: { connection: customRecoveryStrategy },
        },
      });
      customEngine.setMaxListeners(20);
      vi.spyOn(customEngine as any, 'sleep').mockResolvedValue(undefined);

      const connectionError = ConnectionError.serverNotFound('test-server');
      mocks.execute
        .mockRejectedValueOnce(connectionError)
        .mockResolvedValueOnce(createMockToolResult());

      const request = makeRequest({ maxIterations: 2 });
      const result = await customEngine.execute(request);

      expect(customRecoveryStrategy).toHaveBeenCalledWith(connectionError);
      expect(result.status).toBe(ExecutionStatus.COMPLETED);
      expect(result.statistics.errorBreakdown.connection).toBe(1);

      await customEngine.shutdown(1000);
    });

    it('should respect retry delays', async () => {
      const customEngine = new ExecutionEngine({
        ...engineConfig,
        errorRecovery: {
          ...DEFAULT_ERROR_RECOVERY_CONFIG,
          retryDelays: {
            ...DEFAULT_ERROR_RECOVERY_CONFIG.retryDelays,
            connection: 100,
          },
        },
      });
      customEngine.setMaxListeners(20);
      const sleepSpy = vi.spyOn(customEngine as any, 'sleep').mockResolvedValue(undefined);

      const connectionError = ConnectionError.serverNotFound('test-server');
      mocks.execute
        .mockRejectedValueOnce(connectionError)
        .mockResolvedValueOnce(createMockToolResult());

      const request = makeRequest({ maxIterations: 2 });
      await customEngine.execute(request);

      // Verify that sleep was called with the retry delay
      expect(sleepSpy).toHaveBeenCalledWith(100);

      await customEngine.shutdown(1000);
    });
  });

  // =========================================================================
  // Performance metrics and statistics
  // =========================================================================

  describe('performance metrics and statistics', () => {
    it('should calculate execution statistics correctly', async () => {
      const request = makeRequest({ maxIterations: 3 });

      const result = await engine.execute(request);

      expect(result.statistics.totalIterations).toBe(3);
      expect(result.statistics.successfulIterations).toBe(3);
      expect(result.statistics.failedIterations).toBe(0);
      expect(result.statistics.totalToolCalls).toBe(3);
      expect(result.statistics.averageIterationDuration).toBeGreaterThanOrEqual(0);
    });

    it('should track performance metrics', async () => {
      const request = makeRequest({ maxIterations: 2 });
      const result = await engine.execute(request);

      expect(result.statistics.performanceMetrics).toBeDefined();
      expect(result.statistics.performanceMetrics.memoryUsage).toBeGreaterThan(0);
    });

    it('should aggregate statistics across multiple executions', () => {
      const context1 = {
        statistics: {
          totalIterations: 2,
          successfulIterations: 2,
          failedIterations: 0,
          averageIterationDuration: 100,
          totalToolCalls: 2,
          totalProgressEvents: 5,
          rateLimitEncounters: 0,
          rateLimitWaitTime: 0,
          errorBreakdown: {},
          performanceMetrics: {
            cpuUsage: 50,
            memoryUsage: 1000000,
            networkRequests: 2,
            fileSystemOperations: 1,
            throughput: {
              iterationsPerMinute: 60,
              progressEventsPerSecond: 2,
              toolCallsPerMinute: 60,
            },
          },
        },
      };

      const context2 = {
        statistics: {
          totalIterations: 3,
          successfulIterations: 2,
          failedIterations: 1,
          averageIterationDuration: 200,
          totalToolCalls: 3,
          totalProgressEvents: 8,
          rateLimitEncounters: 1,
          rateLimitWaitTime: 1000,
          errorBreakdown: { timeout: 1 },
          performanceMetrics: {
            cpuUsage: 70,
            memoryUsage: 2000000,
            networkRequests: 3,
            fileSystemOperations: 2,
            throughput: {
              iterationsPerMinute: 40,
              progressEventsPerSecond: 3,
              toolCallsPerMinute: 40,
            },
          },
        },
      };

      const avgDuration = engine['calculateAverageIterationDuration']([context1, context2] as any);
      expect(avgDuration).toBeCloseTo(160);

      const errorBreakdown = engine['aggregateErrorBreakdown']([context1, context2] as any);
      expect(errorBreakdown.timeout).toBe(1);

      const perfMetrics = engine['calculatePerformanceMetrics']([context1, context2] as any);
      expect(perfMetrics.cpuUsage).toBe(60);
      expect(perfMetrics.memoryUsage).toBe(1500000);
    });

    it('should update statistics correctly for failed iterations', () => {
      const request = makeRequest();
      const context = engine['createExecutionContext'](request);
      const iterationResult = {
        iterationNumber: 1,
        success: false,
        startTime: new Date(),
        endTime: new Date(),
        duration: 250,
        toolResult: {} as any,
        progressEvents: [],
        error: TimeoutError.toolExecution('test-tool', 30000),
      };

      engine['updateStatistics'](context, iterationResult);

      expect(context.statistics.totalIterations).toBe(1);
      expect(context.statistics.successfulIterations).toBe(0);
      expect(context.statistics.failedIterations).toBe(1);
      expect(context.statistics.totalToolCalls).toBe(1);
      expect(context.statistics.averageIterationDuration).toBe(250);
    });

    it('should handle updatePerformanceMetrics with zero duration', () => {
      const request = makeRequest();
      const context = engine['createExecutionContext'](request);
      context.startTime = new Date();

      engine['updatePerformanceMetrics'](context);

      const metrics = context.statistics.performanceMetrics;
      expect(metrics.memoryUsage).toBeGreaterThan(0);
      expect(metrics.throughput.iterationsPerMinute).toBe(0);
    });

    it('should handle calculatePerformanceMetrics with empty contexts', () => {
      const perfMetrics = engine['calculatePerformanceMetrics']([]);

      expect(perfMetrics.cpuUsage).toBe(0);
      expect(perfMetrics.memoryUsage).toBe(0);
      expect(perfMetrics.networkRequests).toBe(0);
      expect(perfMetrics.fileSystemOperations).toBe(0);
    });

    it('should calculate average iteration duration with zero total iterations', () => {
      const contexts = [
        { statistics: { totalIterations: 0, averageIterationDuration: 100 } },
        { statistics: { totalIterations: 0, averageIterationDuration: 200 } },
      ];
      const avgDuration = engine['calculateAverageIterationDuration'](contexts as any);
      expect(avgDuration).toBe(0);
    });

    it('should handle aggregateErrorBreakdown with complex scenarios', () => {
      const contexts = [
        { statistics: { errorBreakdown: { connection: 2, timeout: 1 } } },
        { statistics: { errorBreakdown: { connection: 1, validation: 3, rate_limit: 1 } } },
        { statistics: { errorBreakdown: {} } },
      ];

      const breakdown = engine['aggregateErrorBreakdown'](contexts as any);

      expect(breakdown.connection).toBe(3);
      expect(breakdown.timeout).toBe(1);
      expect(breakdown.validation).toBe(3);
      expect(breakdown.rate_limit).toBe(1);
    });
  });

  // =========================================================================
  // Execution result creation
  // =========================================================================

  describe('execution result creation', () => {
    it('should create execution result without error', () => {
      const request = makeRequest();
      const context = engine['createExecutionContext'](request);
      context.status = ExecutionStatus.COMPLETED;
      context.endTime = new Date();
      context.iterations = [];
      context.progressEvents = [];

      const result = engine['createExecutionResult'](context);

      expect(result.request).toBe(request);
      expect(result.status).toBe(ExecutionStatus.COMPLETED);
      expect(result.startTime).toBe(context.startTime);
      expect(result.endTime).toBe(context.endTime);
      expect(result.duration).toBeGreaterThanOrEqual(0);
      expect(result.iterations).toEqual([]);
      expect(result.statistics).toBe(context.statistics);
      expect(result.error).toBeUndefined();
      expect(result.sessionContext).toBe(context.sessionContext);
      expect(result.progressEvents).toEqual([]);
    });

    it('should create execution result with error', () => {
      const request = makeRequest();
      const context = engine['createExecutionContext'](request);
      context.status = ExecutionStatus.FAILED;
      context.endTime = new Date();
      context.error = ConnectionError.serverNotFound('test-server');

      const result = engine['createExecutionResult'](context);

      expect(result.status).toBe(ExecutionStatus.FAILED);
      expect(result.error).toBe(context.error);
    });

    it('should handle missing endTime in execution result', () => {
      const request = makeRequest();
      const context = engine['createExecutionContext'](request);
      context.status = ExecutionStatus.COMPLETED;

      const result = engine['createExecutionResult'](context);

      expect(result.endTime).toBeInstanceOf(Date);
      expect(result.duration).toBeGreaterThanOrEqual(0);
    });
  });

  // =========================================================================
  // Execution engine lifecycle
  // =========================================================================

  describe('execution engine lifecycle', () => {
    it('should handle onProgress callback registration and cleanup', () => {
      const progressCallback1 = vi.fn();
      const progressCallback2 = vi.fn();

      const cleanup1 = engine.onProgress(progressCallback1);
      const cleanup2 = engine.onProgress(progressCallback2);

      expect(engine['progressCallbacks']).toHaveLength(2);

      cleanup1();
      expect(engine['progressCallbacks']).toHaveLength(1);
      expect(engine['progressCallbacks'][0]).toBe(progressCallback2);

      cleanup2();
      expect(engine['progressCallbacks']).toHaveLength(0);
    });

    it('should handle getRateLimitInfo', async () => {
      const rateLimitInfo = await engine.getRateLimitInfo();

      expect(rateLimitInfo.isRateLimited).toBe(false);
      expect(rateLimitInfo.remaining).toBe(100);
      expect(rateLimitInfo.resetTime).toBeInstanceOf(Date);
      expect(rateLimitInfo.waitTimeMs).toBe(0);
    });

    it('should handle getExecutionStatistics with no active executions', () => {
      const stats = engine.getExecutionStatistics();

      expect(stats.totalIterations).toBe(0);
      expect(stats.successfulIterations).toBe(0);
      expect(stats.failedIterations).toBe(0);
      expect(stats.averageIterationDuration).toBe(0);
      expect(stats.totalToolCalls).toBe(0);
      expect(stats.totalProgressEvents).toBe(0);
      expect(stats.rateLimitEncounters).toBe(0);
      expect(stats.rateLimitWaitTime).toBe(0);
      expect(stats.errorBreakdown).toEqual({});
      expect(stats.performanceMetrics).toBeDefined();
    });
  });

  // =========================================================================
  // Comprehensive iteration execution
  // =========================================================================

  describe('comprehensive iteration execution', () => {
    it('should handle tool call with all metadata correctly', async () => {
      const request = makeRequest({
        requestId: 'test-metadata',
        subagent: 'cursor',
        workingDirectory: process.env.JUNO_TASK_ROOT || process.cwd(),
        maxIterations: 1,
        model: 'gpt-4',
        timeoutMs: 45000,
        priority: 'high',
      });

      await engine.execute(request);

      expect(mocks.execute).toHaveBeenCalledWith(
        expect.objectContaining({
          toolName: 'cursor_subagent',
          arguments: expect.objectContaining({
            instruction: 'Test instruction',
            project_path: request.workingDirectory,
            model: 'gpt-4',
            iteration: 1,
          }),
          timeout: 45000,
          priority: 'high',
          metadata: expect.objectContaining({
            iterationNumber: 1,
          }),
          progressCallback: expect.any(Function),
        }),
      );
    });

    it('should handle tool call errors during iteration', async () => {
      const toolError = new Error('Tool execution failed');
      mocks.execute.mockRejectedValue(toolError);

      const request = makeRequest();
      const result = await engine.execute(request);

      expect(result.status).toBe(ExecutionStatus.FAILED);
      expect(result.iterations).toHaveLength(1);
      expect(result.iterations[0].success).toBe(false);
      expect(result.iterations[0].error).toBeDefined();
      expect(result.iterations[0].toolResult.status).toBe('failed');
    });

    it('should emit iteration events during execution', async () => {
      const iterationStartSpy = vi.fn();
      const iterationCompleteSpy = vi.fn();
      const iterationErrorSpy = vi.fn();

      engine.on('iteration:start', iterationStartSpy);
      engine.on('iteration:complete', iterationCompleteSpy);
      engine.on('iteration:error', iterationErrorSpy);

      const request = makeRequest({ maxIterations: 2 });
      await engine.execute(request);

      expect(iterationStartSpy).toHaveBeenCalledTimes(2);
      expect(iterationCompleteSpy).toHaveBeenCalledTimes(2);
      expect(iterationErrorSpy).not.toHaveBeenCalled();
    });
  });

  // =========================================================================
  // Factory functions
  // =========================================================================

  describe('factory functions', () => {
    it('should test createExecutionEngine factory', () => {
      const config = createMockConfig();
      const createdEngine = createExecutionEngine(config);

      expect(createdEngine).toBeInstanceOf(ExecutionEngine);
    });

    it('should test createExecutionRequest factory with all options', () => {
      const request = createExecutionRequest({
        instruction: 'Test instruction',
        subagent: 'cursor',
        workingDirectory: '/custom/path',
        maxIterations: 25,
        model: 'gpt-4',
        requestId: 'custom-123',
      });

      expect(request.instruction).toBe('Test instruction');
      expect(request.subagent).toBe('cursor');
      expect(request.workingDirectory).toBe('/custom/path');
      expect(request.maxIterations).toBe(25);
      expect(request.requestId).toBe('custom-123');
    });

    it('should test createExecutionRequest factory with defaults', () => {
      const request = createExecutionRequest({
        instruction: 'Test instruction',
      });

      expect(request.instruction).toBe('Test instruction');
      expect(request.subagent).toBe('claude');
      expect(request.backend).toBe('shell');
      expect(request.workingDirectory).toBe(process.cwd());
      expect(request.maxIterations).toBe(1);
      expect(request.requestId).toMatch(/^req-/);
    });
  });
});
