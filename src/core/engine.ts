/**
 * Core execution engine module for juno-task-ts
 *
 * This module provides the main execution orchestration engine that manages
 * the entire workflow for AI task execution, including session management,
 * backend integration, progress tracking, error handling, and cancellation.
 *
 * The implementation closely matches the Python budi-cli execution patterns,
 * including rate limit handling, iteration logic, and progress tracking.
 *
 * @module core/engine
 * @since 1.0.0
 */

import { EventEmitter } from 'node:events';
import { existsSync } from 'node:fs';
import path from 'node:path';
import type { JunoTaskConfig, SubagentType, ProgressEventType, BackendType } from '../types/index';
import type {
  ProgressEvent,
  ProgressCallback,
  ToolCallRequest,
  ToolCallResult,
  SessionContext,
} from '../types/execution.js';
import { ExecutionError, RateLimitError } from './errors';
import { executeHook, type HookExecutionResult } from '../utils/hooks.js';
import { engineLogger } from '../cli/utils/advanced-logger.js';
import type { Backend } from './backend-manager.js';
import { type QuotaLimitInfo, formatDuration } from './backends/shell-backend.js';
import { resolvePromptCommandSubstitutions } from './prompt-command-substitution.js';
import { resolvePromptMacros } from './prompt-macro-resolver.js';
import { getPromptMacroDictionary } from './config.js';
import { resolveController } from '../utils/controller-resolver.js';
import { buildChildProcessEnvironment } from './child-process-environment.js';

// =============================================================================
// Type Definitions
// =============================================================================

/**
 * Execution request interface for starting task execution
 */
export interface ExecutionRequest {
  /** Unique request identifier */
  readonly requestId: string;

  /** Task instruction or prompt text */
  readonly instruction: string;

  /** Subagent to use for execution */
  readonly subagent: SubagentType;

  /** Backend to use for execution */
  readonly backend: BackendType;

  /** Working directory for execution */
  readonly workingDirectory: string;

  /** Maximum number of iterations (-1 for unlimited) */
  readonly maxIterations: number;

  /** Optional specific model to use */
  readonly model?: string;

  /** Optional timeout in milliseconds (overrides config) */
  readonly timeoutMs?: number;

  /** Session metadata */
  readonly sessionMetadata?: Record<string, unknown>;

  /** Custom progress callbacks */
  readonly progressCallbacks?: ProgressCallback[];

  /** Request priority level */
  readonly priority?: 'low' | 'normal' | 'high';

  /** MCP server name (for MCP backend) */
  readonly mcpServerName?: string;

  /** Available tools from built-in set (only works with --print mode, forwarded to shell backend) */
  readonly tools?: string[];

  /** Permission-based filtering of specific tool instances (forwarded to shell backend) */
  readonly allowedTools?: string[];

  /** Disallowed tools (forwarded to shell backend) */
  readonly disallowedTools?: string[];

  /** Append tools to default allowed-tools list (forwarded to shell backend) */
  readonly appendAllowedTools?: string[];

  /** Agents configuration (forwarded to shell backend) */
  readonly agents?: string;

  /** Resume a conversation by session ID (forwarded to shell backend) */
  readonly resume?: string;

  /** Clone/fork the requested Pi session instead of resuming it directly. */
  readonly cloneSession?: boolean;

  /** Source Pi session ID/path to fork from when cloneSession is enabled. */
  readonly cloneFromSession?: string;

  /** Continue the most recent conversation (forwarded to shell backend) */
  readonly continueConversation?: boolean;

  /** Extended thinking level (forwarded to shell backend --thinking flag) */
  readonly thinking?: string;

  /** Run Pi subagent in interactive live mode (forwarded to shell backend --live flag) */
  readonly live?: boolean;

  /** Start Pi live mode without an initial prompt (interactive continue flow) */
  readonly liveInteractiveSession?: boolean;
}

function resolveExecutionController(request: ExecutionRequest) {
  const delegated = request.sessionMetadata?.['executionControllerDirectory'];
  if (typeof delegated === 'string' && delegated.trim() !== '') {
    return resolveController(delegated, 'orchestration', {
      ignoreEnvironmentAssertions: true,
      trustedResolver: true,
    });
  }
  return resolveController(request.workingDirectory, 'orchestration');
}

/**
 * Execution result interface for completed executions
 */
export interface ExecutionResult {
  /** Request that generated this result */
  readonly request: ExecutionRequest;

  /** Execution status */
  readonly status: ExecutionStatus;

  /** Execution start time */
  readonly startTime: Date;

  /** Execution end time */
  readonly endTime: Date;

  /** Total execution duration in milliseconds */
  readonly duration: number;

  /** All iteration results */
  readonly iterations: readonly IterationResult[];

  /** Final execution statistics */
  readonly statistics: ExecutionStatistics;

  /** Any error that terminated execution */
  readonly error?: ExecutionError;

  /** Session context information */
  readonly sessionContext: SessionContext;

  /** All progress events captured during execution */
  readonly progressEvents: readonly ProgressEvent[];
}

/**
 * Individual iteration result
 */
export interface IterationResult {
  /** Iteration number (1-based) */
  readonly iterationNumber: number;

  /** Iteration success status */
  readonly success: boolean;

  /** Iteration start time */
  readonly startTime: Date;

  /** Iteration end time */
  readonly endTime: Date;

  /** Iteration duration in milliseconds */
  readonly duration: number;

  /** Tool call result */
  readonly toolResult: ToolCallResult;

  /** Iteration-specific progress events */
  readonly progressEvents: readonly ProgressEvent[];

  /** Any error that occurred during iteration */
  readonly error?: Error;
}

/**
 * Execution status enumeration
 */
export enum ExecutionStatus {
  PENDING = 'pending',
  RUNNING = 'running',
  COMPLETED = 'completed',
  FAILED = 'failed',
  CANCELLED = 'cancelled',
  TIMEOUT = 'timeout',
  RATE_LIMITED = 'rate_limited',
}

/**
 * Execution statistics for performance tracking
 */
export interface ExecutionStatistics {
  /** Total iterations attempted */
  totalIterations: number;

  /** Number of successful iterations */
  successfulIterations: number;

  /** Number of failed iterations */
  failedIterations: number;

  /** Average iteration duration in milliseconds */
  averageIterationDuration: number;

  /** Total tool calls made */
  totalToolCalls: number;

  /** Total progress events processed */
  totalProgressEvents: number;

  /** Rate limit encounters */
  rateLimitEncounters: number;

  /** Total rate limit wait time in milliseconds */
  rateLimitWaitTime: number;

  /** Quota limit encounters (Claude-specific) */
  quotaLimitEncounters: number;

  /** Total quota limit wait time in milliseconds */
  quotaLimitWaitTime: number;

  /** Error breakdown by type */
  errorBreakdown: Record<string, number>;

  /** Performance metrics */
  performanceMetrics: PerformanceMetrics;
}

/**
 * Performance metrics for detailed analysis
 */
export interface PerformanceMetrics {
  /** CPU usage percentage during execution */
  cpuUsage: number;

  /** Memory usage in bytes */
  memoryUsage: number;

  /** Network requests made */
  networkRequests: number;

  /** File system operations */
  fileSystemOperations: number;

  /** Throughput metrics */
  throughput: ThroughputMetrics;
}

/**
 * Throughput metrics
 */
export interface ThroughputMetrics {
  /** Iterations per minute */
  iterationsPerMinute: number;

  /** Progress events per second */
  progressEventsPerSecond: number;

  /** Tool calls per minute */
  toolCallsPerMinute: number;
}

/**
 * Rate limit information for handling
 */
export interface RateLimitInfo {
  /** Whether currently rate limited */
  readonly isRateLimited: boolean;

  /** Rate limit reset time */
  readonly resetTime?: Date;

  /** Remaining requests in current window */
  readonly remaining: number;

  /** Time to wait before next request in milliseconds */
  readonly waitTimeMs: number;

  /** Rate limit tier/category */
  readonly tier?: string;
}

/**
 * Error recovery strategy configuration
 */
export interface ErrorRecoveryConfig {
  /** Maximum recovery attempts per error type */
  readonly maxAttempts: Record<string, number>;

  /** Retry delays by error type in milliseconds */
  readonly retryDelays: Record<string, number>;

  /** Whether to continue on specific error types */
  readonly continueOnError: Record<string, boolean>;

  /** Custom recovery strategies */
  readonly customStrategies: Record<string, (error: ExecutionError) => Promise<boolean>>;
}

/**
 * Execution engine configuration
 */
export interface ExecutionEngineConfig {
  /** Base configuration */
  readonly config: JunoTaskConfig;

  /** No longer uses BackendManager; engine creates ShellBackend directly */

  /** Error recovery configuration */
  readonly errorRecovery: ErrorRecoveryConfig;

  /** Rate limit handling configuration */
  readonly rateLimitConfig: RateLimitHandlingConfig;

  /** Progress tracking configuration */
  readonly progressConfig: ProgressTrackingConfig;
}

/**
 * Rate limit handling configuration
 */
export interface RateLimitHandlingConfig {
  /** Enable automatic rate limit handling */
  readonly enabled: boolean;

  /** Maximum wait time for rate limits in milliseconds */
  readonly maxWaitTimeMs: number;

  /** Rate limit detection patterns */
  readonly detectionPatterns: readonly RegExp[];

  /** Custom rate limit parsers */
  readonly customParsers: readonly RateLimitParser[];
}

/**
 * Rate limit parser interface
 */
export interface RateLimitParser {
  /** Pattern to match rate limit messages */
  readonly pattern: RegExp;

  /** Parse function to extract reset time */
  readonly parse: (message: string) => Date | null;
}

/**
 * Progress tracking configuration
 */
export interface ProgressTrackingConfig {
  /** Enable progress tracking */
  readonly enabled: boolean;

  /** Buffer size for progress events */
  readonly bufferSize: number;

  /** Progress event filters */
  readonly filters: readonly ProgressEventFilter[];

  /** Custom progress processors */
  readonly processors: readonly ProgressEventProcessor[];
}

/**
 * Progress event filter
 */
export interface ProgressEventFilter {
  /** Filter type */
  readonly type: ProgressEventType | 'custom';

  /** Filter predicate */
  readonly predicate: (event: ProgressEvent) => boolean;
}

/**
 * Progress event processor
 */
export interface ProgressEventProcessor {
  /** Processor name */
  readonly name: string;

  /** Process function */
  readonly process: (event: ProgressEvent) => Promise<void>;
}

// =============================================================================
// Default Configurations
// =============================================================================

/**
 * Default error recovery configuration
 */
export const DEFAULT_ERROR_RECOVERY_CONFIG: ErrorRecoveryConfig = {
  maxAttempts: {
    connection: 3,
    timeout: 2,
    rate_limit: 5,
    tool_execution: 2,
    validation: 1,
    server_not_found: 1,
    protocol: 2,
    authentication: 1,
  },
  retryDelays: {
    connection: 1000,
    timeout: 2000,
    rate_limit: 0, // Wait time determined by rate limit reset
    tool_execution: 1500,
    validation: 0, // No retry for validation errors
    server_not_found: 0, // No retry for server not found
    protocol: 1000,
    authentication: 0, // No retry for auth errors
  },
  continueOnError: {
    connection: true,
    timeout: true,
    rate_limit: true,
    tool_execution: false,
    validation: false,
    server_not_found: false,
    protocol: true,
    authentication: false,
  },
  customStrategies: {},
};

/**
 * Default rate limit handling configuration
 */
export const DEFAULT_RATE_LIMIT_CONFIG: RateLimitHandlingConfig = {
  enabled: true,
  maxWaitTimeMs: 3600000, // 1 hour
  detectionPatterns: [
    /resets\s+(\d{1,2}):?(\d{2})?\s*(am|pm)?/i,
    /resets\s+(\d{1,2})\s*(am|pm)/i,
    /try again in (\d+)\s*(minutes?|hours?)/i,
    /5-hour limit reached.*resets\s+(\d{1,2})\s*(am|pm)/i,
  ],
  customParsers: [],
};

/**
 * Default progress tracking configuration
 */
export const DEFAULT_PROGRESS_CONFIG: ProgressTrackingConfig = {
  enabled: true,
  bufferSize: 10000,
  filters: [],
  processors: [],
};

// =============================================================================
// Main ExecutionEngine Class
// =============================================================================

/**
 * Main execution engine class for orchestrating AI task execution
 *
 * This class manages the complete execution lifecycle including:
 * - Session creation and management
 * - Iteration loop with rate limit handling
 * - Progress tracking and statistics collection
 * - Error handling with recovery strategies
 * - Cancellation and cleanup
 *
 * @example
 * ```typescript
 * const engine = createExecutionEngine(await loadConfig());
 *
 * const request: ExecutionRequest = {
 *   requestId: 'req-123',
 *   instruction: 'Implement a new feature',
 *   subagent: 'claude',
 *   workingDirectory: '/path/to/project',
 *   maxIterations: 10,
 * };
 *
 * const result = await engine.execute(request);
 * ```
 */
class DependencyPreflightError extends Error {
  constructor(command: string, exitCode: number) {
    super(`Dependency preflight failed (exit ${exitCode}): ${command.slice(0, 500)}`);
    this.name = 'DependencyPreflightError';
  }
}

export class ExecutionEngine extends EventEmitter {
  private readonly engineConfig: ExecutionEngineConfig;
  private readonly activeExecutions = new Map<string, ExecutionContext>();
  private readonly progressCallbacks: ProgressCallback[] = [];
  private readonly cleanupTasks: (() => Promise<void>)[] = [];
  private isShuttingDown = false;
  private currentBackend: Backend | null = null;

  /**
   * Create a new ExecutionEngine instance
   *
   * @param config - Engine configuration
   */
  constructor(config: ExecutionEngineConfig) {
    super();
    this.engineConfig = config;
    this.setupErrorHandling();
    this.setupProgressTracking();
  }

  // =============================================================================
  // Public API Methods
  // =============================================================================

  /**
   * Execute a task request with comprehensive orchestration
   *
   * @param request - Execution request parameters
   * @param abortSignal - Optional abort signal for cancellation
   * @returns Promise resolving to execution result
   */
  async execute(request: ExecutionRequest, abortSignal?: AbortSignal): Promise<ExecutionResult> {
    this.validateRequest(request);

    const context = this.createExecutionContext(request, abortSignal);
    this.activeExecutions.set(request.requestId, context);

    try {
      this.emit('execution:start', { request, context });

      const result = await this.executeInternal(context);

      this.emit('execution:complete', { request, result });
      return result;
    } catch (error) {
      const mcpError = this.wrapError(error);
      this.emit('execution:error', { request, error: mcpError });
      throw mcpError;
    } finally {
      this.activeExecutions.delete(request.requestId);
      await this.cleanupExecution(context);
    }
  }

  /**
   * Add a progress callback for all executions
   *
   * @param callback - Progress callback function
   * @returns Cleanup function to remove the callback
   */
  onProgress(callback: ProgressCallback): () => void {
    this.progressCallbacks.push(callback);
    return () => {
      const index = this.progressCallbacks.indexOf(callback);
      if (index !== -1) {
        this.progressCallbacks.splice(index, 1);
      }
    };
  }

  /**
   * Get current rate limit information
   *
   * @returns Current rate limit status
   */
  async getRateLimitInfo(): Promise<RateLimitInfo> {
    // Implementation would query backend for rate limit status
    return {
      isRateLimited: false,
      remaining: 100,
      resetTime: new Date(Date.now() + 60000), // 1 minute from now
      waitTimeMs: 0,
    };
  }

  /**
   * Cancel all active executions and shutdown gracefully
   *
   * @param timeoutMs - Maximum time to wait for cleanup
   */
  async shutdown(timeoutMs: number = 30000): Promise<void> {
    if (this.isShuttingDown) {
      return;
    }

    this.isShuttingDown = true;
    this.emit('engine:shutdown:start');

    try {
      // Cancel all active executions
      const cancellationPromises = Array.from(this.activeExecutions.values()).map((context) =>
        this.cancelExecution(context),
      );

      // Wait for cancellations with timeout
      await Promise.race([
        Promise.all(cancellationPromises),
        new Promise((_, reject) =>
          setTimeout(() => reject(new Error('Shutdown timeout')), timeoutMs),
        ),
      ]);

      // Clean up backend
      if (this.currentBackend) {
        await this.currentBackend.cleanup();
        this.currentBackend = null;
      }

      // Run cleanup tasks
      await Promise.all(this.cleanupTasks.map((task) => task()));

      this.emit('engine:shutdown:complete');
    } catch (error) {
      this.emit('engine:shutdown:error', error);
      throw error;
    } finally {
      this.removeAllListeners();
    }
  }

  /**
   * Get statistics for all executions
   *
   * @returns Aggregate execution statistics
   */
  getExecutionStatistics(): ExecutionStatistics {
    const contexts = Array.from(this.activeExecutions.values());

    return {
      totalIterations: contexts.reduce((sum, ctx) => sum + ctx.statistics.totalIterations, 0),
      successfulIterations: contexts.reduce(
        (sum, ctx) => sum + ctx.statistics.successfulIterations,
        0,
      ),
      failedIterations: contexts.reduce((sum, ctx) => sum + ctx.statistics.failedIterations, 0),
      averageIterationDuration: this.calculateAverageIterationDuration(contexts),
      totalToolCalls: contexts.reduce((sum, ctx) => sum + ctx.statistics.totalToolCalls, 0),
      totalProgressEvents: contexts.reduce(
        (sum, ctx) => sum + ctx.statistics.totalProgressEvents,
        0,
      ),
      rateLimitEncounters: contexts.reduce(
        (sum, ctx) => sum + ctx.statistics.rateLimitEncounters,
        0,
      ),
      rateLimitWaitTime: contexts.reduce((sum, ctx) => sum + ctx.statistics.rateLimitWaitTime, 0),
      quotaLimitEncounters: contexts.reduce(
        (sum, ctx) => sum + (ctx.statistics.quotaLimitEncounters ?? 0),
        0,
      ),
      quotaLimitWaitTime: contexts.reduce(
        (sum, ctx) => sum + (ctx.statistics.quotaLimitWaitTime ?? 0),
        0,
      ),
      errorBreakdown: this.aggregateErrorBreakdown(contexts),
      performanceMetrics: this.calculatePerformanceMetrics(contexts),
    };
  }

  // =============================================================================
  // Private Implementation Methods
  // =============================================================================

  /**
   * Setup error handling for the engine
   */
  private setupErrorHandling(): void {
    // Backend-agnostic error handling
    process.on('uncaughtException', (error) => {
      this.emit('engine:uncaught-exception', error);
    });

    process.on('unhandledRejection', (reason) => {
      this.emit('engine:unhandled-rejection', reason);
    });
  }

  /**
   * Setup progress tracking for the engine
   */
  private setupProgressTracking(): void {
    // Progress tracking will be set up when backend is initialized
    // The backend manager will handle progress callbacks
  }

  /**
   * Display hook execution output to stderr at verbose level 2 (debug+hooks).
   * At level 2: shows hook name, command stdout/stderr output.
   * At level 0-1: output is suppressed (hooks still execute).
   */
  private displayHookOutput(hookResult: HookExecutionResult): void {
    if (this.engineConfig.config.verbose < 2) return;
    for (const cmdResult of hookResult.commandResults) {
      const prefix = `[hook:${hookResult.hookType}]`;
      if (cmdResult.stdout) {
        for (const line of cmdResult.stdout.split('\n')) {
          if (line.trim()) console.error(`${prefix} ${line}`);
        }
      }
      if (cmdResult.stderr) {
        for (const line of cmdResult.stderr.split('\n')) {
          if (line.trim()) console.error(`${prefix} ${line}`);
        }
      }
      if (!cmdResult.success) {
        console.error(`${prefix} command failed (exit ${cmdResult.exitCode}): ${cmdResult.command}`);
      }
    }
  }

  /**
   * Initialize backend for execution request.
   * Directly creates and configures a ShellBackend (no factory indirection).
   */
  private async initializeBackend(request: ExecutionRequest): Promise<void> {
    // Clean up existing backend if present
    if (this.currentBackend) {
      await this.currentBackend.cleanup();
      this.currentBackend = null;
    }

    // Create ShellBackend directly
    const { ShellBackend } = await import('./backends/shell-backend.js');
    const backend = new ShellBackend();

    const inheritedProjectPath = process.env.JUNO_PROJECT_PATH?.trim();
    const changedManagedWorktree = inheritedProjectPath !== undefined && inheritedProjectPath !== ''
      && existsSync(request.workingDirectory)
      && path.resolve(inheritedProjectPath) !== path.resolve(request.workingDirectory);
    // A managed parent may cd from its dispatch root into a registered task
    // worktree. In that case only, derive authority from persisted identity;
    // same-boundary explicit assertion mismatches remain fail-closed.
    const controller = typeof request.sessionMetadata?.['executionControllerDirectory'] === 'string'
      ? resolveExecutionController(request)
      : changedManagedWorktree
      ? resolveController(request.workingDirectory, 'orchestration', {
        ignoreEnvironmentAssertions: true, trustedResolver: true,
      })
      : resolveController(request.workingDirectory, 'orchestration');

    // Configure
    const modelShortcuts = this.engineConfig.config.modelShortcuts ?? {};
    (backend as any).configure({
      workingDirectory: request.workingDirectory,
      servicesPath: `${process.env.HOME || process.env.USERPROFILE}/.yylo/services`,
      debug: this.engineConfig.config.verbose >= 2,
      timeout: request.timeoutMs || this.engineConfig.config.mcpTimeout || 43200000,
      enableJsonStreaming: true,
      outputRawJson: this.engineConfig.config.verbose >= 1,
      environment: buildChildProcessEnvironment(process.env, {
        JUNO_TASK_ROOT: controller.path,
        JUNO_CONTROLLER_SOURCE: controller.source,
        JUNO_WORKSPACE_ROLE: controller.role,
        JUNO_MODEL_SHORTCUTS: JSON.stringify(modelShortcuts),
        JUNO_SELECTED_SUBAGENT: request.subagent,
        HEADLESS_UI_TURN_COST_DISPLAY_THRESHOLD_USD:
          process.env.HEADLESS_UI_TURN_COST_DISPLAY_THRESHOLD_USD?.trim()
          || String(this.engineConfig.config.headlessUi?.turnCostDisplayThresholdUsd ?? 0.5),
      }),
      sessionId: request.requestId,
    });

    // Initialize and check availability
    await backend.initialize();

    const isAvailable = await backend.isAvailable();
    if (!isAvailable) {
      throw new Error(
        'Shell backend is not available. Ensure ~/.yylo/services/ exists and contains service scripts.',
      );
    }

    this.currentBackend = backend;

    // Set up progress tracking for the selected backend
    backend.onProgress(async (event: ProgressEvent) => {
      try {
        // Process through configured processors
        for (const processor of this.engineConfig.progressConfig.processors) {
          await processor.process(event);
        }

        // Notify all registered callbacks
        await Promise.all([...this.progressCallbacks.map((callback) => callback(event))]);

        this.emit('progress:event', event);
      } catch (error) {
        this.emit('progress:error', { event, error });
      }
    });

    engineLogger.debug(`Initialized ${backend.name} backend for execution`);
  }

  /**
   * Validate execution request parameters
   */
  private validateRequest(request: ExecutionRequest): void {
    if (!request.requestId?.trim()) {
      throw new Error('Request ID is required');
    }

    const allowEmptyInstructionForPiLiveInteractiveSession =
      request.subagent === 'pi' &&
      request.live === true &&
      request.liveInteractiveSession === true &&
      typeof request.resume === 'string' &&
      request.resume.trim().length > 0;

    if (!request.instruction?.trim() && !allowEmptyInstructionForPiLiveInteractiveSession) {
      throw new Error('Instruction is required');
    }

    if (!request.subagent?.trim()) {
      throw new Error('Subagent is required');
    }

    if (!request.workingDirectory?.trim()) {
      throw new Error('Working directory is required');
    }

    if (request.cloneSession === true) {
      const cloneSource = request.cloneFromSession ?? request.resume;
      if (request.subagent !== 'pi') {
        throw new Error('Session cloning is only supported for the Pi subagent');
      }
      if (typeof cloneSource !== 'string' || cloneSource.trim().length === 0) {
        throw new Error('Pi session cloning requires a source session via cloneFromSession or resume');
      }
    }

    if (
      Number.isNaN(request.maxIterations) ||
      request.maxIterations < -1 ||
      request.maxIterations === 0
    ) {
      throw new Error('Max iterations must be a positive number or -1 for unlimited');
    }
  }

  /**
   * Create execution context for a request
   */
  private createExecutionContext(
    request: ExecutionRequest,
    abortSignal?: AbortSignal,
  ): ExecutionContext {
    const abortController = new AbortController();

    // Chain external abort signal if provided
    if (abortSignal) {
      abortSignal.addEventListener('abort', () => {
        abortController.abort();
      });
    }

    return {
      request,
      status: ExecutionStatus.PENDING,
      startTime: new Date(),
      endTime: null,
      iterations: [],
      statistics: this.createInitialStatistics(),
      progressEvents: [],
      error: null,
      abortController,
      sessionContext: this.createSessionContext(request),
      rateLimitInfo: {
        isRateLimited: false,
        remaining: 100,
        waitTimeMs: 0,
      },
    };
  }

  /**
   * Create initial statistics object
   */
  private createInitialStatistics(): ExecutionStatistics {
    return {
      totalIterations: 0,
      successfulIterations: 0,
      failedIterations: 0,
      averageIterationDuration: 0,
      totalToolCalls: 0,
      totalProgressEvents: 0,
      rateLimitEncounters: 0,
      rateLimitWaitTime: 0,
      quotaLimitEncounters: 0,
      quotaLimitWaitTime: 0,
      errorBreakdown: {},
      performanceMetrics: {
        cpuUsage: 0,
        memoryUsage: 0,
        networkRequests: 0,
        fileSystemOperations: 0,
        throughput: {
          iterationsPerMinute: 0,
          progressEventsPerSecond: 0,
          toolCallsPerMinute: 0,
        },
      },
    };
  }

  /**
   * Create session context for execution
   */
  private createSessionContext(request: ExecutionRequest): SessionContext {
    return {
      sessionId: `session-${request.requestId}`,
      startTime: new Date(),
      userId: 'system',
      metadata: {
        ...request.sessionMetadata,
        subagent: request.subagent,
        workingDirectory: request.workingDirectory,
      },
      activeToolCalls: [],
      state: 'initializing' as any,
      lastActivity: new Date(),
    };
  }

  /**
   * Internal execution implementation
   */
  private async executeInternal(context: ExecutionContext): Promise<ExecutionResult> {
    context.status = ExecutionStatus.RUNNING;
    context.sessionContext = { ...context.sessionContext, state: 'active' as any };

    // Resolve once at the orchestration boundary. Explicit or registered
    // controller settings are authoritative and invalid settings fail closed.
    resolveExecutionController(context.request);

    // Initialize backend for this execution request
    await this.initializeBackend(context.request);

    // Execute START_RUN hook
    try {
      if (this.engineConfig.config.hooks && !this.engineConfig.config.skipHooks) {
        const hookResult = await executeHook(
          'START_RUN',
          this.engineConfig.config.hooks,
          {
            workingDirectory: context.request.workingDirectory,
            sessionId: context.sessionContext.sessionId,
            runId: context.request.requestId,
            metadata: {
              sessionId: context.sessionContext.sessionId,
              requestId: context.request.requestId,
              subagent: context.request.subagent,
              backend: context.request.backend,
              maxIterations: context.request.maxIterations,
              instruction: context.request.instruction,
            },
          },
          {
            commandTimeout: this.engineConfig.config.hookCommandTimeout,
          },
        );
        this.displayHookOutput(hookResult);
        const failedDependencyPreflight = hookResult.commandResults.find((result) =>
          !result.success && /(?:^|[\s/])install_requirements\.sh(?:\s|$)/.test(result.command),
        );
        if (failedDependencyPreflight) {
          throw new DependencyPreflightError(
            failedDependencyPreflight.command,
            failedDependencyPreflight.exitCode,
          );
        }
      }
    } catch (error) {
      engineLogger.warn('Hook START_RUN failed', { error });
      // Dependency/version repair is an agent-dispatch prerequisite. Other
      // owner-defined hook failures retain the historical best-effort behavior.
      if (error instanceof DependencyPreflightError) throw error;
    }

    try {
      await this.runIterationLoop(context);

      // Determine final status: if all iterations failed, mark execution as failed
      const hasSuccessfulIteration = context.statistics.successfulIterations > 0;
      if (hasSuccessfulIteration || context.statistics.totalIterations === 0) {
        context.status = ExecutionStatus.COMPLETED;
        context.sessionContext = { ...context.sessionContext, state: 'completed' as any };
      } else {
        context.status = ExecutionStatus.FAILED;
        context.sessionContext = { ...context.sessionContext, state: 'failed' as any };
      }
    } catch (error) {
      context.error = this.wrapError(error);
      context.status = this.determineErrorStatus(context.error);
      context.sessionContext = { ...context.sessionContext, state: 'failed' as any };
    } finally {
      context.endTime = new Date();
    }

    // Execute END_RUN hook
    try {
      if (this.engineConfig.config.hooks && !this.engineConfig.config.skipHooks) {
        const hookResult = await executeHook(
          'END_RUN',
          this.engineConfig.config.hooks,
          {
            workingDirectory: context.request.workingDirectory,
            sessionId: context.sessionContext.sessionId,
            runId: context.request.requestId,
            metadata: {
              sessionId: context.sessionContext.sessionId,
              requestId: context.request.requestId,
              status: context.status,
              totalIterations: context.statistics.totalIterations,
              successfulIterations: context.statistics.successfulIterations,
              failedIterations: context.statistics.failedIterations,
              duration: context.endTime
                ? context.endTime.getTime() - context.startTime.getTime()
                : 0,
              success: context.status === ExecutionStatus.COMPLETED,
            },
          },
          {
            commandTimeout: this.engineConfig.config.hookCommandTimeout,
          },
        );
        this.displayHookOutput(hookResult);
      }
    } catch (error) {
      engineLogger.warn('Hook END_RUN failed', { error });
      // Continue execution despite hook failure
    }

    return this.createExecutionResult(context);
  }

  /**
   * Run the main iteration loop
   */
  private async runIterationLoop(context: ExecutionContext): Promise<void> {
    let iterationNumber = 1;

    while (!this.shouldStopIterating(context, iterationNumber)) {
      this.checkAbortSignal(context);

      try {
        const quotaLimitInfo = await this.executeIteration(context, iterationNumber);

        // Check if a quota limit was encountered
        if (quotaLimitInfo?.detected) {
          const shouldRetry = await this.handleQuotaLimit(context, quotaLimitInfo);
          if (shouldRetry) {
            // Don't increment iteration number, retry same iteration
            continue;
          }
        }

        iterationNumber++;
      } catch (error) {
        if (error instanceof RateLimitError) {
          await this.handleRateLimit(context, error);
          // Don't increment iteration number, retry same iteration
          continue;
        }

        const shouldContinue = await this.handleIterationError(context, error, iterationNumber);
        if (!shouldContinue) {
          throw error;
        }

        iterationNumber++;
      }

      // Brief delay between iterations
      await this.sleep(2000);
    }
  }

  /**
   * Execute a single iteration
   * @returns QuotaLimitInfo if a quota limit was detected, null otherwise
   */
  private async executeIteration(
    context: ExecutionContext,
    iterationNumber: number,
  ): Promise<QuotaLimitInfo | null> {
    const iterationStart = new Date();

    // Execute START_ITERATION hook
    try {
      if (this.engineConfig.config.hooks && !this.engineConfig.config.skipHooks) {
        const hookResult = await executeHook(
          'START_ITERATION',
          this.engineConfig.config.hooks,
          {
            workingDirectory: context.request.workingDirectory,
            sessionId: context.sessionContext.sessionId,
            runId: context.request.requestId,
            iteration: iterationNumber,
            totalIterations: context.request.maxIterations,
            metadata: {
              sessionId: context.sessionContext.sessionId,
              requestId: context.request.requestId,
              iterationNumber,
              maxIterations: context.request.maxIterations,
              subagent: context.request.subagent,
            },
          },
          {
            commandTimeout: this.engineConfig.config.hookCommandTimeout,
          },
        );
        this.displayHookOutput(hookResult);
      }
    } catch (error) {
      engineLogger.warn('Hook START_ITERATION failed', { error, iterationNumber });
      // Continue execution despite hook failure
    }

    this.emit('iteration:start', { context, iterationNumber });

    let toolRequest: ToolCallRequest | null = null;

    try {
      const instructionTemplate = context.request.instruction;
      const promptMacros = this.engineConfig.config.promptMacros;
      const macroOrder = promptMacros?.order ?? 'before_command_substitution';
      const macroEnabled = promptMacros?.enabled ?? true;
      const macroMaxDepth = promptMacros?.maxDepth ?? 10;
      const macroDictionary = getPromptMacroDictionary(this.engineConfig.config);

      const macroWarnings: Array<{ message: string }> = [];

      const applyPromptMacros = (input: string): string => {
        if (!macroEnabled) {
          return input;
        }

        const result = resolvePromptMacros(input, {
          dictionary: macroDictionary,
          maxDepth: macroMaxDepth,
        });

        for (const warning of result.warnings) {
          macroWarnings.push({ message: warning.message });
        }

        return result.resolvedPrompt;
      };

      const applyCommandSubstitutions = async (input: string): Promise<string> =>
        resolvePromptCommandSubstitutions(input, {
          workingDirectory: context.request.workingDirectory,
          environment: buildChildProcessEnvironment(process.env, {
            JUNO_TASK_ROOT: resolveExecutionController(context.request).path,
          }),
        });

      const resolvedInstruction =
        macroOrder === 'after_command_substitution'
          ? applyPromptMacros(await applyCommandSubstitutions(instructionTemplate))
          : await applyCommandSubstitutions(applyPromptMacros(instructionTemplate));

      this.emit('iteration:instruction-resolved', {
        context,
        iterationNumber,
        instruction: resolvedInstruction,
        templateInstruction: instructionTemplate,
        warnings: macroWarnings,
      });

      const cloneFromSession =
        context.request.cloneSession === true
          ? (context.request.cloneFromSession ?? context.request.resume)
          : undefined;

      toolRequest = {
        toolName: this.getToolNameForSubagent(context.request.subagent),
        arguments: {
          instruction: resolvedInstruction,
          project_path: context.request.workingDirectory,
          ...(context.request.model !== undefined && { model: context.request.model }),
          ...(context.request.agents !== undefined && { agents: context.request.agents }),
          ...(context.request.tools !== undefined && { tools: context.request.tools }),
          ...(context.request.allowedTools !== undefined && {
            allowedTools: context.request.allowedTools,
          }),
          ...(context.request.appendAllowedTools !== undefined && {
            appendAllowedTools: context.request.appendAllowedTools,
          }),
          ...(context.request.disallowedTools !== undefined && {
            disallowedTools: context.request.disallowedTools,
          }),
          ...(context.request.resume !== undefined &&
            context.request.cloneSession !== true && { resume: context.request.resume }),
          ...(context.request.cloneSession !== undefined && {
            cloneSession: context.request.cloneSession,
          }),
          ...(cloneFromSession !== undefined && { cloneFromSession }),
          ...(context.request.continueConversation !== undefined && {
            continueConversation: context.request.continueConversation,
          }),
          ...(context.request.thinking !== undefined && { thinking: context.request.thinking }),
          ...(context.request.live !== undefined && { live: context.request.live }),
          ...(context.request.liveInteractiveSession !== undefined && {
            liveInteractiveSession: context.request.liveInteractiveSession,
          }),
          iteration: iterationNumber,
        },
        timeout: context.request.timeoutMs || this.engineConfig.config.mcpTimeout,
        priority: context.request.priority || 'normal',
        metadata: {
          sessionId: context.sessionContext.sessionId,
          iterationNumber,
        },
        progressCallback: async (event: ProgressEvent) => {
          context.progressEvents.push(event);
          context.statistics.totalProgressEvents++;
          await this.processProgressEvent(context, event);
        },
      };

      if (!this.currentBackend) {
        throw new Error('No backend initialized. Call initializeBackend() first.');
      }
      const toolResult = await this.currentBackend.execute(toolRequest);

      const iterationEnd = new Date();
      const duration = iterationEnd.getTime() - iterationStart.getTime();

      const iterationResult: IterationResult = {
        iterationNumber,
        success: toolResult.status?.toLowerCase() === 'completed',
        startTime: iterationStart,
        endTime: iterationEnd,
        duration,
        toolResult,
        progressEvents: toolResult.progressEvents,
        ...(toolResult.error !== undefined &&
          toolResult.error !== null && { error: toolResult.error }),
      };

      context.iterations.push(iterationResult);
      this.updateStatistics(context, iterationResult);

      this.emit('iteration:complete', { context, iterationResult });

      // Execute END_ITERATION hook for successful iteration
      try {
        if (this.engineConfig.config.hooks && !this.engineConfig.config.skipHooks) {
          const hookResult = await executeHook(
            'END_ITERATION',
            this.engineConfig.config.hooks,
            {
              workingDirectory: context.request.workingDirectory,
              sessionId: context.sessionContext.sessionId,
              runId: context.request.requestId,
              iteration: iterationNumber,
              totalIterations: context.request.maxIterations,
              metadata: {
                sessionId: context.sessionContext.sessionId,
                requestId: context.request.requestId,
                iterationNumber,
                success: iterationResult.success,
                duration: iterationResult.duration,
                toolCallStatus: iterationResult.toolResult.status,
              },
            },
            {
              commandTimeout: this.engineConfig.config.hookCommandTimeout,
            },
          );
          this.displayHookOutput(hookResult);
        }
      } catch (error) {
        engineLogger.warn('Hook END_ITERATION failed', { error, iterationNumber });
        // Continue execution despite hook failure
      }

      // Check for quota limit in the result
      const quotaLimitInfo = this.extractQuotaLimitInfo(toolResult);
      return quotaLimitInfo;
    } catch (error) {
      const iterationEnd = new Date();
      const duration = iterationEnd.getTime() - iterationStart.getTime();
      const mcpError = this.wrapError(error);

      const iterationResult: IterationResult = {
        iterationNumber,
        success: false,
        startTime: iterationStart,
        endTime: iterationEnd,
        duration,
        toolResult: {
          content: '',
          status: 'failed' as any,
          startTime: iterationStart,
          endTime: iterationEnd,
          duration,
          error: mcpError,
          progressEvents: [],
          request:
            toolRequest ??
            ({
              toolName: this.getToolNameForSubagent(context.request.subagent),
              arguments: {},
            } as ToolCallRequest),
        },
        progressEvents: [],
        error: mcpError,
      };

      context.iterations.push(iterationResult);
      this.updateStatistics(context, iterationResult);

      this.emit('iteration:error', { context, iterationResult });

      // Execute END_ITERATION hook for failed iteration
      try {
        if (this.engineConfig.config.hooks && !this.engineConfig.config.skipHooks) {
          const hookResult = await executeHook(
            'END_ITERATION',
            this.engineConfig.config.hooks,
            {
              workingDirectory: context.request.workingDirectory,
              sessionId: context.sessionContext.sessionId,
              runId: context.request.requestId,
              iteration: iterationNumber,
              totalIterations: context.request.maxIterations,
              metadata: {
                sessionId: context.sessionContext.sessionId,
                requestId: context.request.requestId,
                iterationNumber,
                success: false,
                duration: iterationResult.duration,
                error: mcpError.message,
                errorType: mcpError.type,
              },
            },
            {
              commandTimeout: this.engineConfig.config.hookCommandTimeout,
            },
          );
          this.displayHookOutput(hookResult);
        }
      } catch (hookError) {
        engineLogger.warn('Hook END_ITERATION failed', { error: hookError, iterationNumber });
        // Continue execution despite hook failure
      }

      throw error;
    }
  }

  /**
   * Handle rate limit errors with automatic retry
   */
  private async handleRateLimit(context: ExecutionContext, error: RateLimitError): Promise<void> {
    if (!this.engineConfig.rateLimitConfig.enabled) {
      throw error;
    }

    context.statistics.rateLimitEncounters++;

    const waitTimeMs = this.calculateRateLimitWaitTime(error);
    if (waitTimeMs > this.engineConfig.rateLimitConfig.maxWaitTimeMs) {
      throw new Error(
        `Rate limit wait time (${waitTimeMs}ms) exceeds maximum allowed (${this.engineConfig.rateLimitConfig.maxWaitTimeMs}ms)`,
      );
    }

    context.statistics.rateLimitWaitTime += waitTimeMs;
    context.rateLimitInfo = {
      isRateLimited: true,
      ...(error.resetTime !== undefined && { resetTime: error.resetTime }),
      remaining: error.remaining || 0,
      waitTimeMs,
      ...(error.tier !== undefined && { tier: error.tier }),
    };

    this.emit('rate-limit:start', { context, error, waitTimeMs });

    await this.sleep(waitTimeMs);

    context.rateLimitInfo = {
      isRateLimited: false,
      remaining: 100,
      waitTimeMs: 0,
    };

    this.emit('rate-limit:end', { context });
  }

  /**
   * Calculate wait time for rate limit reset
   */
  private calculateRateLimitWaitTime(error: RateLimitError): number {
    if (error.resetTime) {
      const now = new Date();
      const waitTime = error.resetTime.getTime() - now.getTime();
      return Math.max(0, waitTime);
    }

    // Default wait time if no reset time provided
    return 60000; // 1 minute
  }

  /**
   * Handle Claude quota limit with automatic sleep and retry
   * @returns true if we should retry the iteration, false otherwise
   */
  private async handleQuotaLimit(
    context: ExecutionContext,
    quotaInfo: QuotaLimitInfo,
  ): Promise<boolean> {
    if (!quotaInfo.detected || !quotaInfo.sleepDurationMs) {
      return false;
    }

    context.statistics.quotaLimitEncounters++;

    // Check the onHourlyLimit configuration setting
    // Priority: config.json < ENV < Flag (all handled by the config loader)
    const onHourlyLimit = this.engineConfig.config.onHourlyLimit || 'raise';

    // If set to 'raise', exit immediately instead of waiting
    if (onHourlyLimit === 'raise') {
      const resetTimeStr = quotaInfo.resetTime
        ? quotaInfo.resetTime.toLocaleTimeString('en-US', {
            hour: 'numeric',
            minute: '2-digit',
            hour12: true,
            timeZoneName: 'short',
          })
        : 'unknown';

      const sourceLabel = quotaInfo.source === 'codex' ? 'Codex' : 'Claude';

      engineLogger.info(`╔════════════════════════════════════════════════════════════════╗`);
      engineLogger.info(
        `║  ${sourceLabel} Quota Limit Reached${' '.repeat(44 - sourceLabel.length - ' Quota Limit Reached'.length)}║`,
      );
      engineLogger.info(`╠════════════════════════════════════════════════════════════════╣`);
      engineLogger.info(`║  Quota resets at: ${resetTimeStr.padEnd(44)}║`);
      engineLogger.info(`║  Behavior:        raise (exit immediately)                     ║`);
      engineLogger.info(`╠════════════════════════════════════════════════════════════════╣`);
      engineLogger.info(`║  To auto-wait instead, use: --on-hourly-limit wait            ║`);
      engineLogger.info(`║  Or set: YYLO_ON_HOURLY_LIMIT=wait                        ║`);
      engineLogger.info(`║  Or in config.json: { "onHourlyLimit": "wait" }               ║`);
      engineLogger.info(`╚════════════════════════════════════════════════════════════════╝`);

      this.emit('quota-limit:raise', { context, quotaInfo });

      // Return false to NOT retry, which will cause the iteration loop to continue
      // but since this is an error condition, we need to throw to actually exit
      throw new Error(
        `${sourceLabel} quota limit reached. Quota resets at ${resetTimeStr}. Use --on-hourly-limit wait to auto-retry.`,
      );
    }

    // onHourlyLimit === 'wait' - proceed with waiting behavior
    const waitTimeMs = quotaInfo.sleepDurationMs;

    // Cap the wait time at 12 hours to prevent excessive waits
    const maxWaitTimeMs = 12 * 60 * 60 * 1000; // 12 hours
    if (waitTimeMs > maxWaitTimeMs) {
      engineLogger.warn(
        `Quota limit wait time (${formatDuration(waitTimeMs)}) exceeds maximum allowed (12 hours). Will not auto-retry.`,
      );
      return false;
    }

    context.statistics.quotaLimitWaitTime += waitTimeMs;

    // Format the reset time for user display
    const resetTimeStr = quotaInfo.resetTime
      ? quotaInfo.resetTime.toLocaleTimeString('en-US', {
          hour: 'numeric',
          minute: '2-digit',
          hour12: true,
          timeZoneName: 'short',
        })
      : 'unknown';

    const durationStr = formatDuration(waitTimeMs);

    // Log user-friendly message
    const waitSourceLabel = quotaInfo.source === 'codex' ? 'Codex' : 'Claude';

    engineLogger.info(`╔════════════════════════════════════════════════════════════════╗`);
    engineLogger.info(
      `║  ${waitSourceLabel} Quota Limit Reached${' '.repeat(44 - waitSourceLabel.length - ' Quota Limit Reached'.length)}║`,
    );
    engineLogger.info(`╠════════════════════════════════════════════════════════════════╣`);
    engineLogger.info(`║  Quota resets at: ${resetTimeStr.padEnd(44)}║`);
    engineLogger.info(`║  Sleeping for:    ${durationStr.padEnd(44)}║`);
    if (quotaInfo.timezone) {
      engineLogger.info(`║  Timezone:        ${quotaInfo.timezone.padEnd(44)}║`);
    }
    engineLogger.info(`╚════════════════════════════════════════════════════════════════╝`);

    this.emit('quota-limit:start', { context, quotaInfo, waitTimeMs });

    // Sleep with periodic progress updates
    await this.sleepWithProgress(waitTimeMs, (remaining) => {
      const remainingStr = formatDuration(remaining);
      engineLogger.info(`[Quota Wait] ${remainingStr} remaining until retry...`);
    });

    this.emit('quota-limit:end', { context });

    engineLogger.info(`Quota limit wait complete. Resuming execution...`);

    return true;
  }

  /**
   * Sleep with periodic progress updates
   */
  private async sleepWithProgress(
    totalMs: number,
    onProgress: (remainingMs: number) => void,
  ): Promise<void> {
    const updateIntervalMs = 60000; // Update every minute
    let remaining = totalMs;

    while (remaining > 0) {
      const sleepTime = Math.min(remaining, updateIntervalMs);
      await this.sleep(sleepTime);
      remaining -= sleepTime;

      if (remaining > 0) {
        onProgress(remaining);
      }
    }
  }

  /**
   * Check if tool result indicates a quota limit error
   */
  private extractQuotaLimitInfo(toolResult: ToolCallResult): QuotaLimitInfo | null {
    // Check metadata first (most reliable)
    const metadataQuotaInfo = (toolResult.metadata as any)?.quotaLimitInfo;
    if (metadataQuotaInfo?.detected) {
      return metadataQuotaInfo;
    }

    // Try to parse from content if metadata doesn't have it
    try {
      const content =
        typeof toolResult.content === 'string'
          ? JSON.parse(toolResult.content)
          : toolResult.content;

      if (content?.quota_limit?.detected) {
        return content.quota_limit;
      }
    } catch {
      // Ignore parse errors
    }

    return null;
  }

  /**
   * Handle iteration errors with recovery strategies
   */
  private async handleIterationError(
    context: ExecutionContext,
    error: unknown,
    iterationNumber: number,
  ): Promise<boolean> {
    const mcpError = this.wrapError(error);
    const errorType = mcpError.type;

    context.statistics.errorBreakdown[errorType] =
      (context.statistics.errorBreakdown[errorType] || 0) + 1;

    // Check if we should continue on this error type
    const shouldContinue = this.engineConfig.errorRecovery.continueOnError[errorType] ?? false;
    if (!shouldContinue) {
      return false;
    }

    // Attempt recovery if strategy exists
    const customStrategy = this.engineConfig.errorRecovery.customStrategies[errorType];
    if (customStrategy) {
      try {
        const recovered = await customStrategy(mcpError);
        if (recovered) {
          this.emit('error:recovered', { context, error: mcpError, iterationNumber });
          return true;
        }
      } catch (recoveryError) {
        this.emit('error:recovery-failed', {
          context,
          error: mcpError,
          recoveryError,
          iterationNumber,
        });
      }
    }

    // Apply retry delay if configured
    const retryDelay = this.engineConfig.errorRecovery.retryDelays[errorType] || 0;
    if (retryDelay > 0) {
      await this.sleep(retryDelay);
    }

    this.emit('error:continuing', { context, error: mcpError, iterationNumber });
    return true;
  }

  /**
   * Process individual progress events
   */
  private async processProgressEvent(
    context: ExecutionContext,
    event: ProgressEvent,
  ): Promise<void> {
    // Apply filters
    for (const filter of this.engineConfig.progressConfig.filters) {
      if (!filter.predicate(event)) {
        return;
      }
    }

    // Update session activity
    context.sessionContext = {
      ...context.sessionContext,
      lastActivity: new Date(),
    };

    this.emit('progress:processed', { context, event });
  }

  /**
   * Update execution statistics
   */
  private updateStatistics(context: ExecutionContext, iteration: IterationResult): void {
    const stats = context.statistics;

    stats.totalIterations++;
    stats.totalToolCalls++;

    if (iteration.success) {
      stats.successfulIterations++;
    } else {
      stats.failedIterations++;
    }

    // Update average iteration duration
    const totalDuration =
      stats.averageIterationDuration * (stats.totalIterations - 1) + iteration.duration;
    stats.averageIterationDuration = totalDuration / stats.totalIterations;

    // Update performance metrics
    this.updatePerformanceMetrics(context);
  }

  /**
   * Update performance metrics
   */
  private updatePerformanceMetrics(context: ExecutionContext): void {
    const metrics = context.statistics.performanceMetrics;

    // Get current resource usage
    const memUsage = process.memoryUsage();
    metrics.memoryUsage = memUsage.heapUsed;

    // Calculate throughput
    const duration = Date.now() - context.startTime.getTime();
    const durationMinutes = duration / (1000 * 60);

    if (durationMinutes > 0) {
      metrics.throughput.iterationsPerMinute = context.statistics.totalIterations / durationMinutes;
      metrics.throughput.toolCallsPerMinute = context.statistics.totalToolCalls / durationMinutes;
      metrics.throughput.progressEventsPerSecond =
        context.statistics.totalProgressEvents / (duration / 1000);
    }
  }

  /**
   * Check if iteration loop should stop
   */
  private shouldStopIterating(context: ExecutionContext, iterationNumber: number): boolean {
    // Check abort signal
    if (context.abortController.signal.aborted) {
      return true;
    }

    // Check max iterations
    if (context.request.maxIterations !== -1 && iterationNumber > context.request.maxIterations) {
      return true;
    }

    // Check for shutdown
    if (this.isShuttingDown) {
      return true;
    }

    return false;
  }

  /**
   * Check abort signal and throw if aborted
   */
  private checkAbortSignal(context: ExecutionContext): void {
    if (context.abortController.signal.aborted) {
      throw new Error('Execution aborted');
    }
  }

  /**
   * Get tool name for subagent
   */
  private getToolNameForSubagent(subagent: SubagentType): string {
    const mapping: Record<SubagentType, string> = {
      claude: 'claude_subagent',
      cursor: 'cursor_subagent',
      codex: 'codex_subagent',
      gemini: 'gemini_subagent',
      pi: 'pi_subagent',
    };

    return mapping[subagent] || 'claude_subagent';
  }

  /**
   * Wrap unknown errors as execution errors
   */
  private wrapError(error: unknown): ExecutionError {
    if (error instanceof ExecutionError) {
      return error;
    }

    // Classify common transport/socket failures as connection errors so the loop can continue
    const msg = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
    const lower = msg.toLowerCase();
    const isConnectionLike = [
      'epipe',
      'broken pipe',
      'econnreset',
      'socket hang up',
      'err_socket_closed',
      'connection reset by peer',
    ].some((token) => lower.includes(token));

    if (isConnectionLike) {
      return {
        type: 'connection',
        message: msg,
        timestamp: new Date(),
        code: 'MCP_CONNECTION_LOST' as any,
      } as any;
    }

    // Fallback: treat as tool execution error
    return {
      type: 'tool_execution',
      message: msg,
      timestamp: new Date(),
    } as any;
  }

  /**
   * Determine execution status from error
   */
  private determineErrorStatus(error: ExecutionError): ExecutionStatus {
    switch (error.type) {
      case 'rate_limit':
        return ExecutionStatus.RATE_LIMITED;
      case 'timeout':
        return ExecutionStatus.TIMEOUT;
      default:
        return ExecutionStatus.FAILED;
    }
  }

  /**
   * Create final execution result
   */
  private createExecutionResult(context: ExecutionContext): ExecutionResult {
    const endTime = context.endTime || new Date();
    return {
      request: context.request,
      status: context.status,
      startTime: context.startTime,
      endTime,
      duration: endTime.getTime() - context.startTime.getTime(),
      iterations: context.iterations,
      statistics: context.statistics,
      ...(context.error !== undefined && context.error !== null && { error: context.error }),
      sessionContext: context.sessionContext,
      progressEvents: context.progressEvents,
    };
  }

  /**
   * Cancel an execution
   */
  private async cancelExecution(context: ExecutionContext): Promise<void> {
    context.abortController.abort();
    context.status = ExecutionStatus.CANCELLED;
    this.emit('execution:cancelled', { context });
  }

  /**
   * Cleanup execution resources
   */
  private async cleanupExecution(context: ExecutionContext): Promise<void> {
    // Cleanup would happen here
    this.emit('execution:cleanup', { context });
  }

  /**
   * Calculate average iteration duration across contexts
   */
  private calculateAverageIterationDuration(contexts: ExecutionContext[]): number {
    if (contexts.length === 0) return 0;

    const totalDuration = contexts.reduce(
      (sum, ctx) => sum + ctx.statistics.averageIterationDuration * ctx.statistics.totalIterations,
      0,
    );
    const totalIterations = contexts.reduce((sum, ctx) => sum + ctx.statistics.totalIterations, 0);

    return totalIterations > 0 ? totalDuration / totalIterations : 0;
  }

  /**
   * Aggregate error breakdown across contexts
   */
  private aggregateErrorBreakdown(contexts: ExecutionContext[]): Record<string, number> {
    const breakdown: Record<string, number> = {};

    for (const context of contexts) {
      for (const [errorType, count] of Object.entries(context.statistics.errorBreakdown)) {
        breakdown[errorType] = (breakdown[errorType] || 0) + count;
      }
    }

    return breakdown;
  }

  /**
   * Calculate performance metrics across contexts
   */
  private calculatePerformanceMetrics(contexts: ExecutionContext[]): PerformanceMetrics {
    if (contexts.length === 0) {
      return {
        cpuUsage: 0,
        memoryUsage: 0,
        networkRequests: 0,
        fileSystemOperations: 0,
        throughput: {
          iterationsPerMinute: 0,
          progressEventsPerSecond: 0,
          toolCallsPerMinute: 0,
        },
      };
    }

    // Aggregate metrics from all contexts
    const avgMetrics = contexts.reduce(
      (acc, ctx) => ({
        cpuUsage: acc.cpuUsage + ctx.statistics.performanceMetrics.cpuUsage,
        memoryUsage: acc.memoryUsage + ctx.statistics.performanceMetrics.memoryUsage,
        networkRequests: acc.networkRequests + ctx.statistics.performanceMetrics.networkRequests,
        fileSystemOperations:
          acc.fileSystemOperations + ctx.statistics.performanceMetrics.fileSystemOperations,
        throughput: {
          iterationsPerMinute:
            acc.throughput.iterationsPerMinute +
            ctx.statistics.performanceMetrics.throughput.iterationsPerMinute,
          progressEventsPerSecond:
            acc.throughput.progressEventsPerSecond +
            ctx.statistics.performanceMetrics.throughput.progressEventsPerSecond,
          toolCallsPerMinute:
            acc.throughput.toolCallsPerMinute +
            ctx.statistics.performanceMetrics.throughput.toolCallsPerMinute,
        },
      }),
      {
        cpuUsage: 0,
        memoryUsage: 0,
        networkRequests: 0,
        fileSystemOperations: 0,
        throughput: {
          iterationsPerMinute: 0,
          progressEventsPerSecond: 0,
          toolCallsPerMinute: 0,
        },
      },
    );

    // Average the metrics
    const count = contexts.length;
    return {
      cpuUsage: avgMetrics.cpuUsage / count,
      memoryUsage: avgMetrics.memoryUsage / count,
      networkRequests: avgMetrics.networkRequests / count,
      fileSystemOperations: avgMetrics.fileSystemOperations / count,
      throughput: {
        iterationsPerMinute: avgMetrics.throughput.iterationsPerMinute / count,
        progressEventsPerSecond: avgMetrics.throughput.progressEventsPerSecond / count,
        toolCallsPerMinute: avgMetrics.throughput.toolCallsPerMinute / count,
      },
    };
  }

  /**
   * Sleep utility for delays
   */
  private async sleep(ms: number): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }
}

// =============================================================================
// Internal Types
// =============================================================================

/**
 * Internal execution context
 */
interface ExecutionContext {
  request: ExecutionRequest;
  status: ExecutionStatus;
  startTime: Date;
  endTime: Date | null;
  iterations: IterationResult[];
  statistics: ExecutionStatistics;
  progressEvents: ProgressEvent[];
  error: ExecutionError | null;
  abortController: AbortController;
  sessionContext: SessionContext;
  rateLimitInfo: RateLimitInfo;
}

// =============================================================================
// Factory Functions
// =============================================================================

/**
 * Create an execution engine with default configuration
 *
 * @param config - Base juno-task configuration
 * @returns Configured execution engine
 */
export function createExecutionEngine(config: JunoTaskConfig): ExecutionEngine {
  return new ExecutionEngine({
    config,
    errorRecovery: DEFAULT_ERROR_RECOVERY_CONFIG,
    rateLimitConfig: DEFAULT_RATE_LIMIT_CONFIG,
    progressConfig: DEFAULT_PROGRESS_CONFIG,
  });
}

/**
 * Create an execution request with defaults
 *
 * @param options - Request options
 * @returns Execution request
 */
export function createExecutionRequest(options: {
  instruction: string;
  subagent?: SubagentType;
  backend?: BackendType;
  workingDirectory?: string;
  maxIterations?: number;
  model?: string;
  agents?: string;
  tools?: string[];
  allowedTools?: string[];
  appendAllowedTools?: string[];
  disallowedTools?: string[];
  requestId?: string;
  mcpServerName?: string;
  resume?: string;
  cloneSession?: boolean;
  cloneFromSession?: string;
  continueConversation?: boolean;
  thinking?: string;
  live?: boolean;
  liveInteractiveSession?: boolean;
  sessionMetadata?: Record<string, unknown>;
}): ExecutionRequest {
  const result: ExecutionRequest = {
    requestId: options.requestId || `req-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`,
    instruction: options.instruction,
    subagent: options.subagent || 'claude',
    backend: options.backend || 'shell',
    workingDirectory: options.workingDirectory || process.cwd(),
    maxIterations: options.maxIterations ?? 1,
  };

  if (options.model !== undefined) {
    (result as any).model = options.model;
  }

  if (options.sessionMetadata !== undefined) {
    (result as any).sessionMetadata = options.sessionMetadata;
  }

  if (options.agents !== undefined) {
    (result as any).agents = options.agents;
  }

  if (options.tools !== undefined) {
    (result as any).tools = options.tools;
  }

  if (options.allowedTools !== undefined) {
    (result as any).allowedTools = options.allowedTools;
  }

  if (options.appendAllowedTools !== undefined) {
    (result as any).appendAllowedTools = options.appendAllowedTools;
  }

  if (options.disallowedTools !== undefined) {
    (result as any).disallowedTools = options.disallowedTools;
  }

  if (options.mcpServerName !== undefined) {
    (result as any).mcpServerName = options.mcpServerName;
  }

  if (options.resume !== undefined) {
    (result as any).resume = options.resume;
  }

  if (options.cloneSession !== undefined) {
    (result as any).cloneSession = options.cloneSession;
  }

  if (options.cloneFromSession !== undefined) {
    (result as any).cloneFromSession = options.cloneFromSession;
  }

  if (options.continueConversation !== undefined) {
    (result as any).continueConversation = options.continueConversation;
  }

  if (options.thinking !== undefined) {
    (result as any).thinking = options.thinking;
  }

  if (options.live !== undefined) {
    (result as any).live = options.live;
  }

  if (options.liveInteractiveSession !== undefined) {
    (result as any).liveInteractiveSession = options.liveInteractiveSession;
  }

  return result;
}
