/**
 * Shell Backend Implementation for yylo
 *
 * Executes shell scripts from ~/.yylo/services/ directory
 * Supports JSON streaming output and converts to progress events
 */

import { spawn, ChildProcess } from 'node:child_process';
import { promises as fs } from 'node:fs';
import * as path from 'node:path';
import os from 'node:os';
import fsExtra from 'fs-extra';
import type { Backend } from '../backend-manager.js';
import {
  ProgressEventType,
  ToolExecutionStatus,
} from '../../types/execution.js';
import type {
  ToolCallRequest,
  ToolCallResult,
  ProgressEvent,
  ProgressCallback,
  ToolExecutionMetadata,
} from '../../types/execution.js';
import { engineLogger } from '../../cli/utils/advanced-logger.js';
import { buildChildProcessEnvironment } from '../child-process-environment.js';
import { joinActiveInvocation } from '../invocation-lifecycle.js';

// =============================================================================
// Type Definitions
// =============================================================================

/**
 * Shell backend configuration
 */
export interface ShellBackendConfig {
  /** Working directory for execution */
  workingDirectory: string;

  /** Path to services directory (default: ~/.yylo/services) */
  servicesPath: string;

  /** Enable debug logging */
  debug?: boolean;

  /** Timeout for script execution in milliseconds */
  timeout?: number;

  /** Environment variables to pass to shell scripts */
  environment?: Record<string, string>;

  /** Enable JSON streaming parsing */
  enableJsonStreaming?: boolean;

  /** Output full JSON format instead of simplified messages (for verbose mode) */
  outputRawJson?: boolean;
}

/**
 * Script execution result
 */
interface ScriptExecutionResult {
  success: boolean;
  output: string;
  error?: string;
  exitCode: number;
  duration: number;
  subAgentResponse?: any;
  metadata?: Record<string, any>;
}

const DEFAULT_CAPTURE_LIMIT_BYTES = 1024 * 1024;
const DEFAULT_STREAM_BUFFER_LIMIT_BYTES = 1024 * 1024;
/**
 * Source of truth for shell-backend prompt argv/env safety. Python subagents
 * receive prompts in argv/env only while under this byte limit; larger prompts
 * are written to an internal prompt file and forwarded with --prompt-file.
 * Override with JUNO_PROMPT_ARG_MAX_BYTES for platform-specific spawn limits.
 */
const DEFAULT_PROMPT_ARG_MAX_BYTES = 64 * 1024;
const PROMPT_ARG_MAX_BYTES_ENV = 'JUNO_PROMPT_ARG_MAX_BYTES';

interface CappedTextCapture {
  text: string;
  bytes: number;
  truncated: boolean;
}

interface PromptTransportDecision {
  instruction: string;
  byteLength: number;
  mode: 'argv-env' | 'prompt-file' | 'empty' | 'non-python-env';
  promptFilePath?: string;
  thresholdBytes: number;
}

function getPositiveIntegerEnv(name: string, fallback: number): number {
  const value = process.env[name];
  if (!value) return fallback;
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function appendCappedText(
  capture: CappedTextCapture,
  chunk: string,
  limitBytes: number,
): CappedTextCapture {
  if (!chunk) return capture;

  const combined = capture.text + chunk;
  const combinedBytes = Buffer.byteLength(combined, 'utf8');
  if (combinedBytes <= limitBytes) {
    return { text: combined, bytes: combinedBytes, truncated: capture.truncated };
  }

  let text = combined;
  while (Buffer.byteLength(text, 'utf8') > limitBytes && text.length > 0) {
    const excessBytes = Buffer.byteLength(text, 'utf8') - limitBytes;
    text = text.slice(Math.max(1, Math.min(text.length, excessBytes)));
  }

  return {
    text,
    bytes: Buffer.byteLength(text, 'utf8'),
    truncated: true,
  };
}

/**
 * Quota limit information extracted from Claude or Codex response
 */
export interface QuotaLimitInfo {
  /** Whether a quota limit was detected */
  detected: boolean;
  /** The parsed reset time as a Date object */
  resetTime?: Date;
  /** Sleep duration in milliseconds until the reset time */
  sleepDurationMs?: number;
  /** The timezone extracted from the message */
  timezone?: string;
  /** Original error message */
  originalMessage?: string;
  /** Which subagent triggered the quota limit */
  source?: 'claude' | 'codex';
}

// =============================================================================
// Quota Limit Detection Utilities
// =============================================================================

/**
 * Common timezone aliases and their UTC offsets
 */
const TIMEZONE_OFFSETS: Record<string, number> = {
  // North American timezones
  'America/Toronto': -5,
  'America/New_York': -5,
  'US/Eastern': -5,
  'America/Chicago': -6,
  'US/Central': -6,
  'America/Denver': -7,
  'US/Mountain': -7,
  'America/Los_Angeles': -8,
  'US/Pacific': -8,
  // European timezones
  'Europe/London': 0,
  UTC: 0,
  GMT: 0,
  'Europe/Paris': 1,
  'Europe/Berlin': 1,
  CET: 1,
  // Other common timezones
  'Asia/Tokyo': 9,
  'Asia/Shanghai': 8,
  'Australia/Sydney': 11,
};

/**
 * Parse reset time from Claude quota limit message
 * Handles formats like:
 * - "resets 8pm (America/Toronto)"
 * - "resets 10am (UTC)"
 * - "resets 11:30pm (US/Eastern)"
 * - "resets 2:00 PM (America/New_York)"
 */
function parseResetTime(message: string): { resetTime: Date; timezone: string } | null {
  // Pattern to match: "resets HH[:MM] [AM/PM] (TIMEZONE)"
  const resetPattern = /resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*\(([^)]+)\)/i;
  const match = message.match(resetPattern);

  if (!match) {
    return null;
  }

  let hours = parseInt(match[1]!, 10);
  const minutes = match[2] ? parseInt(match[2]!, 10) : 0;
  const ampm = match[3]?.toLowerCase();
  const timezone = match[4]!.trim();

  // Convert to 24-hour format
  if (ampm === 'pm' && hours !== 12) {
    hours += 12;
  } else if (ampm === 'am' && hours === 12) {
    hours = 0;
  }

  // Get timezone offset (default to local if unknown)
  const timezoneOffset = TIMEZONE_OFFSETS[timezone];

  // Create reset time in the specified timezone
  const now = new Date();
  const resetTime = new Date();

  if (timezoneOffset !== undefined) {
    // Set the reset time in the target timezone
    resetTime.setUTCHours(hours - timezoneOffset, minutes, 0, 0);

    // If the reset time is in the past, add a day
    if (resetTime.getTime() <= now.getTime()) {
      resetTime.setTime(resetTime.getTime() + 24 * 60 * 60 * 1000);
    }
  } else {
    // Fallback: assume it's in the local timezone
    resetTime.setHours(hours, minutes, 0, 0);

    // If the reset time is in the past, add a day
    if (resetTime.getTime() <= now.getTime()) {
      resetTime.setTime(resetTime.getTime() + 24 * 60 * 60 * 1000);
    }
  }

  return { resetTime, timezone };
}

/**
 * Parse reset time from Codex quota limit message
 * Handles formats like:
 * - "try again at Feb 4th, 2026 1:50 AM"
 * - "try again at February 4, 2026 1:50 AM"
 * - "try again at Jan 15th, 2026 11:30 PM"
 */
function parseCodexResetTime(message: string): { resetTime: Date } | null {
  // Pattern to match: "try again at Month Day[st/nd/rd/th], Year HH:MM AM/PM"
  const resetPattern =
    /try again at\s+(\w+)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(\d{4})\s+(\d{1,2}):(\d{2})\s*(AM|PM)/i;
  const match = message.match(resetPattern);

  if (!match) {
    return null;
  }

  const monthStr = match[1]!;
  const day = parseInt(match[2]!, 10);
  const year = parseInt(match[3]!, 10);
  let hours = parseInt(match[4]!, 10);
  const minutes = parseInt(match[5]!, 10);
  const ampm = match[6]!.toUpperCase();

  // Convert month name to number
  const MONTH_MAP: Record<string, number> = {
    jan: 0,
    january: 0,
    feb: 1,
    february: 1,
    mar: 2,
    march: 2,
    apr: 3,
    april: 3,
    may: 4,
    jun: 5,
    june: 5,
    jul: 6,
    july: 6,
    aug: 7,
    august: 7,
    sep: 8,
    september: 8,
    oct: 9,
    october: 9,
    nov: 10,
    november: 10,
    dec: 11,
    december: 11,
  };

  const month = MONTH_MAP[monthStr.toLowerCase()];
  if (month === undefined) {
    return null;
  }

  // Convert 12-hour format to 24-hour format
  if (ampm === 'PM' && hours !== 12) {
    hours += 12;
  } else if (ampm === 'AM' && hours === 12) {
    hours = 0;
  }

  // Codex provides an absolute date/time, construct it directly
  // The time is assumed to be in the user's local timezone (Codex doesn't specify timezone)
  const resetTime = new Date(year, month, day, hours, minutes, 0, 0);

  // If the reset time is in the past, it's likely already passed; add 24h as fallback
  const now = new Date();
  if (resetTime.getTime() <= now.getTime()) {
    resetTime.setTime(resetTime.getTime() + 24 * 60 * 60 * 1000);
  }

  return { resetTime };
}

/**
 * Detect and parse quota limit error from Claude or Codex response
 */
export function detectQuotaLimit(message: string | undefined | null): QuotaLimitInfo {
  if (!message || typeof message !== 'string') {
    return { detected: false };
  }

  // Check for quota limit patterns:
  // Claude: "You've hit your limit · resets 8pm (America/Toronto)"
  // Codex: "You've hit your usage limit. ... try again at Feb 4th, 2026 1:50 AM."
  const claudePattern = /you'?ve hit your limit/i;
  const codexPattern = /you'?ve hit your usage limit/i;

  const isClaudeQuota = claudePattern.test(message) && !codexPattern.test(message);
  const isCodexQuota = codexPattern.test(message);

  if (!isClaudeQuota && !isCodexQuota) {
    return { detected: false };
  }

  const source = isCodexQuota ? 'codex' : 'claude';

  // Try to parse the reset time - Claude format first, then Codex format
  const parsedClaude = parseResetTime(message);
  if (parsedClaude) {
    const now = new Date();
    const sleepDurationMs = Math.max(0, parsedClaude.resetTime.getTime() - now.getTime());

    return {
      detected: true,
      resetTime: parsedClaude.resetTime,
      sleepDurationMs,
      timezone: parsedClaude.timezone,
      originalMessage: message,
      source,
    };
  }

  // Try Codex reset time format ("try again at Feb 4th, 2026 1:50 AM")
  const parsedCodex = parseCodexResetTime(message);
  if (parsedCodex) {
    const now = new Date();
    const sleepDurationMs = Math.max(0, parsedCodex.resetTime.getTime() - now.getTime());

    return {
      detected: true,
      resetTime: parsedCodex.resetTime,
      sleepDurationMs,
      timezone: 'local',
      originalMessage: message,
      source,
    };
  }

  // Quota limit detected but couldn't parse reset time
  // Default to 5 minutes wait
  return {
    detected: true,
    sleepDurationMs: 5 * 60 * 1000, // 5 minutes default
    originalMessage: message,
    source,
  };
}

/**
 * Format duration in human-readable form
 */
export function formatDuration(ms: number): string {
  const totalSeconds = Math.ceil(ms / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;

  const parts: string[] = [];
  if (hours > 0) parts.push(`${hours}h`);
  if (minutes > 0) parts.push(`${minutes}m`);
  if (seconds > 0 || parts.length === 0) parts.push(`${seconds}s`);

  return parts.join(' ');
}

// =============================================================================
// Shell Backend Implementation
// =============================================================================

/**
 * Shell backend that executes scripts from ~/.yylo/services/
 */
export class ShellBackend implements Backend {
  readonly type = 'shell' as const;
  readonly name = 'Shell Scripts Backend';

  private config: ShellBackendConfig | null = null;
  private progressCallbacks: ProgressCallback[] = [];
  private eventCounter = 0;
  private jsonBuffer = ''; // Buffer for handling partial JSON objects
  private logFilePath: string | null = null; // Path to current log file

  /**
   * Configure the shell backend
   */
  configure(config: ShellBackendConfig): void {
    this.config = config;
  }

  /**
   * Initialize the backend
   */
  async initialize(): Promise<void> {
    if (!this.config) {
      throw new Error('Shell backend not configured. Call configure() first.');
    }

    // Ensure services directory exists
    try {
      await fs.access(this.config.servicesPath);
    } catch (error) {
      throw new Error(
        `Services directory not found: ${this.config.servicesPath}. Please create the directory and add subagent scripts.`,
      );
    }

    if (this.config.debug) {
      engineLogger.info(
        `Shell backend initialized with services path: ${this.config.servicesPath}`,
      );
    }
  }

  /**
   * Execute a tool call request using shell scripts
   */
  async execute(request: ToolCallRequest): Promise<ToolCallResult> {
    if (!this.config) {
      throw new Error('Shell backend not configured');
    }

    const startTime = Date.now();
    const toolId = `shell_${request.toolName}_${startTime}`;

    // Extract subagent name and create log file
    const subagentType = this.extractSubagentFromToolName(request.toolName);
    try {
      this.logFilePath = await this.createLogFile(subagentType);
    } catch (error) {
      // Log creation failed - continue without file logging
      if (this.config.debug) {
        engineLogger.warn(
          `Failed to create log file, continuing without file logging: ${error instanceof Error ? error.message : String(error)}`,
        );
      }
      this.logFilePath = null;
    }

    // Emit tool start event
    await this.emitProgressEvent({
      sessionId: (request.metadata?.sessionId as string) || 'unknown',
      timestamp: new Date(),
      backend: 'shell',
      count: ++this.eventCounter,
      type: ProgressEventType.TOOL_START,
      content: `Starting ${request.toolName} via shell script`,
      toolId,
      metadata: {
        toolName: request.toolName,
        arguments: request.arguments,
        phase: 'initialization',
      },
    });

    try {
      // Find appropriate script for the subagent (already extracted above)
      const scriptPath = await this.findScriptForSubagent(subagentType);

      // Execute the script
      const result = await this.executeScript(scriptPath, request, toolId, subagentType);

      const duration = Date.now() - startTime;
      const structuredResult = this.buildStructuredOutput(subagentType, result);
      const structuredPayload = this.parseStructuredResultPayload(structuredResult.content);
      const structuredIndicatesError =
        structuredPayload?.is_error === true || structuredPayload?.subtype === 'error';
      const executionSucceeded = result.success && !structuredIndicatesError;

      // Emit completion event
      await this.emitProgressEvent({
        sessionId: (request.metadata?.sessionId as string) || 'unknown',
        timestamp: new Date(),
        backend: 'shell',
        count: ++this.eventCounter,
        type: ProgressEventType.TOOL_RESULT,
        content: executionSucceeded
          ? `${request.toolName} completed successfully (${duration}ms)`
          : `${request.toolName} completed with error (${duration}ms)`,
        toolId,
        metadata: {
          toolName: request.toolName,
          duration,
          success: executionSucceeded,
          phase: 'completion',
        },
      });

      const toolResult: Record<string, unknown> = {
        content: structuredResult.content,
        status: executionSucceeded ? ToolExecutionStatus.COMPLETED : ToolExecutionStatus.FAILED,
        startTime: new Date(startTime),
        endTime: new Date(),
        duration,
        progressEvents: [] as ProgressEvent[],
        request,
      };
      if (result.error) {
        toolResult.error = new Error(result.error);
      } else if (!executionSucceeded) {
        const structuredErrorMessage =
          (typeof structuredPayload?.error === 'string' && structuredPayload.error) ||
          (typeof structuredPayload?.result === 'string' && structuredPayload.result) ||
          `${request.toolName} reported a structured error`;
        toolResult.error = new Error(structuredErrorMessage);
      }
      if (structuredResult.metadata) {
        toolResult.metadata = structuredResult.metadata;
      }
      return toolResult as unknown as ToolCallResult;
    } catch (error) {
      const duration = Date.now() - startTime;

      // Emit error event
      await this.emitProgressEvent({
        sessionId: (request.metadata?.sessionId as string) || 'unknown',
        timestamp: new Date(),
        backend: 'shell',
        count: ++this.eventCounter,
        type: ProgressEventType.ERROR,
        content: `${request.toolName} failed: ${error instanceof Error ? error.message : String(error)}`,
        toolId,
        metadata: {
          toolName: request.toolName,
          duration,
          success: false,
          error: error instanceof Error ? error.message : String(error),
          phase: 'error',
        },
      });

      throw error;
    }
  }

  /**
   * Check if shell backend is available
   */
  async isAvailable(): Promise<boolean> {
    if (!this.config) {
      return false;
    }

    try {
      // Check if services directory exists
      const stats = await fs.stat(this.config.servicesPath);
      if (!stats.isDirectory()) {
        return false;
      }

      // Check if at least one subagent script exists
      const scripts = await this.findAvailableScripts();
      return scripts.length > 0;
    } catch (error) {
      return false;
    }
  }

  /**
   * Set progress callback
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
   * Clean up resources
   */
  async cleanup(): Promise<void> {
    // Nothing to clean up for shell backend
    this.progressCallbacks = [];
  }

  // =============================================================================
  // Private Implementation Methods
  // =============================================================================

  /**
   * Create log file path and ensure log directory exists
   */
  private async createLogFile(subagentName: string): Promise<string> {
    // Format timestamp as YYYYMMDD_HHMMSS
    const now = new Date();
    const timestamp =
      now.getFullYear().toString() +
      (now.getMonth() + 1).toString().padStart(2, '0') +
      now.getDate().toString().padStart(2, '0') +
      '_' +
      now.getHours().toString().padStart(2, '0') +
      now.getMinutes().toString().padStart(2, '0') +
      now.getSeconds().toString().padStart(2, '0');

    // Create log directory path
    const logDir = path.join(this.config!.workingDirectory, '.juno_task', 'logs');

    // Ensure log directory exists
    try {
      await fsExtra.ensureDir(logDir);
    } catch (error) {
      if (this.config?.debug) {
        engineLogger.warn(
          `Failed to create log directory: ${error instanceof Error ? error.message : String(error)}`,
        );
      }
      throw new Error(`Failed to create log directory: ${logDir}`);
    }

    // Create log file path
    const logFileName = `${subagentName}_shell_${timestamp}.log`;
    const logFilePath = path.join(logDir, logFileName);

    if (this.config?.debug) {
      engineLogger.debug(`Created log file path: ${logFilePath}`);
    }

    return logFilePath;
  }

  /**
   * Write log entry to file
   */
  private async writeToLogFile(message: string): Promise<void> {
    if (!this.logFilePath) {
      return; // No log file configured
    }

    try {
      // Append to log file with timestamp
      const timestamp = new Date().toISOString();
      const logEntry = `[${timestamp}] ${message}\n`;
      await fsExtra.appendFile(this.logFilePath, logEntry, 'utf-8');
    } catch (error) {
      // Don't throw - just log the error if debug is enabled
      if (this.config?.debug) {
        engineLogger.warn(
          `Failed to write to log file: ${error instanceof Error ? error.message : String(error)}`,
        );
      }
    }
  }

  /**
   * Extract subagent type from tool name
   */
  private extractSubagentFromToolName(toolName: string): string {
    // Map tool names to subagent types
    const toolMapping: Record<string, string> = {
      claude_subagent: 'claude',
      cursor_subagent: 'cursor',
      codex_subagent: 'codex',
      gemini_subagent: 'gemini',
      pi_subagent: 'pi',
    };

    return toolMapping[toolName] || toolName.replace('_subagent', '');
  }

  /**
   * Find script for a specific subagent
   */
  private async findScriptForSubagent(subagent: string): Promise<string> {
    const possibleNames = [
      `${subagent}.py`, // Subagent-specific Python script (e.g. claude.py, codex.py)
      `${subagent}.sh`, // Subagent-specific shell script
      ...(subagent === 'cursor' ? ['claude.py'] : []), // Cursor uses the Claude service wrapper
      `subagent.py`, // Generic Python script (fallback)
      `subagent.sh`, // Generic shell script (fallback)
    ];

    const checkedPaths: string[] = [];

    for (const name of possibleNames) {
      const scriptPath = path.join(this.config!.servicesPath, name);
      checkedPaths.push(scriptPath);

      try {
        const stats = await fs.stat(scriptPath);
        if (stats.isFile()) {
          if (this.config!.debug) {
            engineLogger.debug(`Found script for ${subagent}: ${scriptPath}`);
          }
          return scriptPath;
        }
      } catch (error) {
        // Continue to next possibility
        if (this.config!.debug) {
          engineLogger.debug(`Script not found: ${scriptPath}`);
        }
      }
    }

    throw new Error(
      `No script found for subagent: ${subagent}. Checked paths: ${checkedPaths.join(', ')}`,
    );
  }

  /**
   * Find all available scripts in services directory
   */
  private async findAvailableScripts(): Promise<string[]> {
    try {
      const files = await fs.readdir(this.config!.servicesPath);
      const scriptFiles = files.filter((file) => file.endsWith('.py') || file.endsWith('.sh'));
      return scriptFiles;
    } catch (error) {
      return [];
    }
  }

  private async preparePromptTransport(
    instructionValue: unknown,
    isPython: boolean,
    toolId: string,
  ): Promise<PromptTransportDecision> {
    const instruction = String(instructionValue ?? '');
    const byteLength = Buffer.byteLength(instruction, 'utf8');
    const thresholdBytes = getPositiveIntegerEnv(
      PROMPT_ARG_MAX_BYTES_ENV,
      DEFAULT_PROMPT_ARG_MAX_BYTES,
    );

    if (!instruction) {
      return { instruction, byteLength, mode: 'empty', thresholdBytes };
    }

    if (!isPython) {
      return { instruction, byteLength, mode: 'non-python-env', thresholdBytes };
    }

    if (byteLength <= thresholdBytes) {
      return { instruction, byteLength, mode: 'argv-env', thresholdBytes };
    }

    const promptDir = path.join(os.tmpdir(), 'yylo');
    await fsExtra.ensureDir(promptDir);
    const safeToolId = toolId.replace(/[^a-zA-Z0-9_.-]/g, '_');
    const promptFilePath = path.join(
      promptDir,
      `${Date.now()}-${process.pid}-${safeToolId}-prompt.md`,
    );
    await fs.writeFile(promptFilePath, instruction, 'utf8');

    return { instruction, byteLength, mode: 'prompt-file', promptFilePath, thresholdBytes };
  }

  /**
   * Execute a shell script
   */
  private async executeScript(
    scriptPath: string,
    request: ToolCallRequest,
    toolId: string,
    subagentType: string,
  ): Promise<ScriptExecutionResult> {
    return new Promise(async (resolve, reject) => {
      const startTime = Date.now();
      const isPython = scriptPath.endsWith('.py');
      const isGemini = subagentType === 'gemini';
      const promptTransport = await this.preparePromptTransport(
        request.arguments?.instruction,
        isPython,
        toolId,
      );

      // Prepare environment variables
      const env = buildChildProcessEnvironment(process.env, {
        ...this.config!.environment,
        // Pass resolved request data explicitly; continuity snapshots are never inherited.
        JUNO_PROJECT_PATH: String(request.arguments?.project_path ?? this.config!.workingDirectory),
        JUNO_MODEL: String(request.arguments?.model ?? ''),
        JUNO_ITERATION: String(request.arguments?.iteration ?? 1),
        JUNO_TOOL_ID: toolId,
      });

      if (promptTransport.mode !== 'prompt-file') {
        env.JUNO_INSTRUCTION = promptTransport.instruction;
      } else {
        // process.env/config may already contain JUNO_INSTRUCTION; oversized prompts must never
        // be copied into the child environment when prompt-file transport is selected.
        delete env.JUNO_INSTRUCTION;
      }

      // Force unbuffered Python stdout/stderr so progress events stream in real time
      // even when scripts run under piped stdio in headless mode.
      if (isPython) {
        env.PYTHONUNBUFFERED = '1';
      }

      if (isGemini) {
        env.GEMINI_OUTPUT_FORMAT = env.GEMINI_OUTPUT_FORMAT || 'stream-json';
      }

      // Capture file for structured subagent responses (claude.py, pi.py, codex.py support)
      let captureDir: string | null = null;
      let capturePath: string | null = null;
      if (subagentType === 'claude' || subagentType === 'pi' || subagentType === 'codex') {
        try {
          captureDir = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-shell-'));
          capturePath = path.join(captureDir, `subagent_${toolId}.json`);
          env.JUNO_SUBAGENT_CAPTURE_PATH = capturePath;
        } catch (error) {
          if (this.config?.debug) {
            engineLogger.warn(
              `Failed to prepare subagent capture path: ${error instanceof Error ? error.message : String(error)}`,
            );
          }
        }
      }

      // Build command arguments for the script
      const command = isPython ? 'python3' : 'bash';
      const args = [scriptPath];

      // For Python scripts, add the prompt as -p for small prompts and --prompt-file for oversized prompts.
      if (isPython && promptTransport.mode === 'argv-env') {
        args.push('-p', promptTransport.instruction);
      } else if (isPython && promptTransport.mode === 'prompt-file' && promptTransport.promptFilePath) {
        args.push('--prompt-file', promptTransport.promptFilePath);
      }

      // For Python scripts, add the model as -m argument if provided
      if (isPython && request.arguments?.model) {
        args.push('-m', String(request.arguments.model));
      }

      // For Gemini, force stream-json output format by default to preserve headless parity
      if (isPython && isGemini) {
        args.push('--output-format', env.GEMINI_OUTPUT_FORMAT || 'stream-json');
      }

      // For Python scripts, add the agents configuration if provided
      if (isPython && request.arguments?.agents) {
        args.push('--agents', String(request.arguments.agents));
      }

      // For Python scripts, add available tools from built-in set if provided (--tools)
      if (isPython && request.arguments?.tools && Array.isArray(request.arguments.tools)) {
        args.push('--tools');
        args.push(...(request.arguments.tools as string[]));
      }

      // For Python scripts, add permission-based allowed tools if provided (--allowedTools)
      if (
        isPython &&
        request.arguments?.allowedTools &&
        Array.isArray(request.arguments.allowedTools)
      ) {
        args.push('--allowedTools');
        args.push(...(request.arguments.allowedTools as string[]));
      }

      // For Python scripts, add append allowed tools if provided (--appendAllowedTools)
      if (
        isPython &&
        request.arguments?.appendAllowedTools &&
        Array.isArray(request.arguments.appendAllowedTools)
      ) {
        args.push('--appendAllowedTools');
        args.push(...(request.arguments.appendAllowedTools as string[]));
      }

      // For Python scripts, add disallowed tools if provided (--disallowedTools)
      if (
        isPython &&
        request.arguments?.disallowedTools &&
        Array.isArray(request.arguments.disallowedTools)
      ) {
        args.push('--disallowedTools');
        args.push(...(request.arguments.disallowedTools as string[]));
      }

      // Preserve the service's existing string transport as one argv value, including leading dashes.
      if (isPython && request.arguments?.additionalArgs !== undefined) {
        args.push(`--additional-args=${String(request.arguments.additionalArgs)}`);
      }

      // For Python scripts, add thinking level if provided (--thinking LEVEL)
      if (isPython && request.arguments?.thinking) {
        args.push('--thinking', String(request.arguments.thinking));
      }

      // For Pi clone requests, use Pi's native durable fork plumbing instead
      // of resuming the source session directly. This keeps Pi as the single
      // source of truth for branch metadata and session-file creation.
      if (isPython && subagentType === 'pi' && request.arguments?.cloneFromSession) {
        args.push('--fork', String(request.arguments.cloneFromSession));
      } else if (isPython && request.arguments?.resume) {
        // For Python scripts, add resume flag if provided (--resume SESSION_ID)
        args.push('--resume', String(request.arguments.resume));
      }

      // For Python scripts, add continue flag if provided (--continue)
      if (isPython && request.arguments?.continueConversation) {
        args.push('--continue');
      }

      // For Pi subagent, explicitly pass --cd for working directory
      if (isPython && subagentType === 'pi' && request.arguments?.project_path) {
        args.push('--cd', String(request.arguments.project_path));
      }

      // For Pi subagent, forward live mode flag when requested
      if (isPython && subagentType === 'pi' && request.arguments?.live === true) {
        args.push('--live');
      }

      // For Pi subagent, allow opening live TUI without initial prompt for continue flows.
      if (isPython && subagentType === 'pi' && request.arguments?.liveInteractiveSession === true) {
        args.push('--live-manual');
      }

      // For Python scripts, pass verbose flag if verbose mode is enabled
      if (isPython && this.config!.debug) {
        args.push('--verbose');
      }

      if (this.config!.debug) {
        engineLogger.debug(
          `Prompt transport: mode=${promptTransport.mode}, bytes=${promptTransport.byteLength}, threshold=${promptTransport.thresholdBytes}` +
            (promptTransport.promptFilePath ? `, file=${promptTransport.promptFilePath}` : ''),
        );
        // Show command with truncated prompt for readability
        const displayArgs = args.map((a, i) => {
          if (i > 0 && args[i - 1] === '-p' && a.length > 200) {
            return `"${a.substring(0, 200)}..." (${a.length} chars)`;
          }
          return a;
        });
        engineLogger.debug(`Executing script: ${command} ${displayArgs.join(' ')}`);
        engineLogger.debug(`Working directory: ${this.config!.workingDirectory}`);
        engineLogger.debug(`Subagent type: ${subagentType}`);
        engineLogger.debug(
          `Environment variables: ${Object.keys(env)
            .filter((k) => k.startsWith('JUNO_') || k.startsWith('PI_'))
            .join(', ')}`,
        );
      }

      const isPiLiveMode = isPython && subagentType === 'pi' && request.arguments?.live === true;
      const shouldAttachLiveTerminal = isPiLiveMode && process.stdout.isTTY === true;
      if (isPython && subagentType === 'pi' && !isPiLiveMode) {
        // Python stdout is an internal pipe, not the user's output destination.
        // Override inherited hints so redirected/nested runs cannot leak ANSI.
        env.JUNO_PI_OUTPUT_TTY = process.stdout.isTTY === true ? '1' : '0';
      }

      if (this.config!.debug && isPiLiveMode) {
        engineLogger.debug(
          `Pi live mode stdio: ${shouldAttachLiveTerminal ? 'inherit (interactive TTY or stdout-tty fallback)' : 'pipe (headless/non-TTY)'}`,
        );
      }

      const cleanupPromptFile = async (): Promise<void> => {
        if (!promptTransport.promptFilePath) return;
        try {
          await fs.rm(promptTransport.promptFilePath, { force: true });
        } catch (cleanupError) {
          if (this.config?.debug) {
            engineLogger.warn(
              `Failed to clean prompt file: ${cleanupError instanceof Error ? cleanupError.message : String(cleanupError)}`,
            );
          }
        }
      };

      // Spawn the process. Keep this in a try/catch because spawn can throw
      // synchronously for invalid options or platform argv/env failures before a
      // ChildProcess exists to emit an `error` event. Oversized prompt files must
      // still be cleaned in that path.
      let child: ChildProcess;
      const ownsDetachedGroup = process.platform !== 'win32' && !shouldAttachLiveTerminal;
      try {
        child = spawn(command, args, {
          env,
          cwd: this.config!.workingDirectory,
          stdio: shouldAttachLiveTerminal ? 'inherit' : ['pipe', 'pipe', 'pipe'],
          // Headless execution gets an owned group. Interactive Pi remains in the
          // terminal foreground group so normal TTY job control reaches it directly.
          detached: ownsDetachedGroup,
        });
      } catch (error) {
        await cleanupPromptFile();
        reject(new Error(`Failed to execute script: ${error instanceof Error ? error.message : String(error)}`));
        return;
      }

      // Close stdin immediately for headless mode to avoid waiting for input.
      // In live Pi mode with terminal passthrough (including stdout-tty fallback),
      // keep inherited stdin open so pi.py can reattach /dev/tty when needed.
      if (!shouldAttachLiveTerminal && child.stdin) {
        child.stdin.end();
      }

      const captureLimitBytes = getPositiveIntegerEnv(
        'JUNO_SHELL_CAPTURE_LIMIT_BYTES',
        DEFAULT_CAPTURE_LIMIT_BYTES,
      );
      let stdoutCapture: CappedTextCapture = { text: '', bytes: 0, truncated: false };
      let stderrCapture: CappedTextCapture = { text: '', bytes: 0, truncated: false };
      let isProcessKilled = false;
      let timedOutError: Error | null = null;
      const ownedGroupIsActive = (): boolean => {
        if (!child.pid || !ownsDetachedGroup) return false;
        try { process.kill(-child.pid, 0); return true; } catch { return false; }
      };
      if (this.config?.debug) {
        engineLogger.debug(`Shell process owner: pid=${child.pid ?? 'unknown'}, pgid=${ownsDetachedGroup ? child.pid ?? 'unknown' : 'tty-inherited'}`);
      }
      const signalOwnedGroup = (signal: NodeJS.Signals): void => {
        if (!child.pid) return;
        if (this.config?.debug) engineLogger.warn(`Signalling shell process group: pgid=${ownsDetachedGroup ? child.pid : 'tty-inherited'}, signal=${signal}`);
        try {
          process.kill(ownsDetachedGroup ? -child.pid : child.pid, signal);
        } catch (error) {
          if ((error as NodeJS.ErrnoException).code !== 'ESRCH' && this.config?.debug) {
            engineLogger.warn(`Failed to signal shell process group ${child.pid}: ${String(error)}`);
          }
        }
      };
      const awaitOwnedGroupExit = async (timeoutMs: number): Promise<boolean> => {
        const deadline = Date.now() + timeoutMs;
        while (ownedGroupIsActive() && Date.now() < deadline) {
          // Yield to process-exit delivery without guessing at a fixed reap delay.
          // kill(-pgid, 0) remains the authoritative whole-group liveness check.
          await new Promise<void>((done) => setImmediate(done));
        }
        return !ownedGroupIsActive();
      };
      // The invocation signal observer can request process.exit in this same
      // turn. Keep its finalization behind the existing close/group-settlement
      // boundary, or it can exit before our escalation timer kills descendants.
      let cancellationEscalation: NodeJS.Timeout | undefined;
      let settleOwnedProcess!: () => void;
      const ownedProcessSettlement = new Promise<void>((resolveSettlement) => {
        settleOwnedProcess = () => {
          if (cancellationEscalation) clearTimeout(cancellationEscalation);
          resolveSettlement();
        };
      });
      joinActiveInvocation(ownedProcessSettlement);

      const cancelOwnedGroup = (signal: NodeJS.Signals): void => {
        signalOwnedGroup(signal);
        cancellationEscalation ??= setTimeout(() => signalOwnedGroup('SIGKILL'), 250);
      };
      const onSigint = (): void => cancelOwnedGroup('SIGINT');
      const onSigterm = (): void => cancelOwnedGroup('SIGTERM');
      const onSighup = (): void => cancelOwnedGroup('SIGHUP');
      process.prependListener('SIGINT', onSigint);
      process.prependListener('SIGTERM', onSigterm);
      process.prependListener('SIGHUP', onSighup);
      const removeSignalOwners = (): void => {
        process.removeListener('SIGINT', onSigint);
        process.removeListener('SIGTERM', onSigterm);
        process.removeListener('SIGHUP', onSighup);
      };

      // Handle stdout (JSON streaming or TEXT streaming). Keep only a bounded tail
      // in memory; the structured capture file is the source of truth for final
      // subagent payloads. This avoids Node-version-sensitive heap growth when
      // Pi or other providers emit very large logs.
      child.stdout?.on('data', (chunk: Buffer) => {
        const data = chunk.toString();
        stdoutCapture = appendCappedText(stdoutCapture, data, captureLimitBytes);

        if (this.config!.debug) {
          engineLogger.debug(`Script stdout chunk: ${data.length} bytes`);
        }

        // Try to parse and emit streaming events (handles both JSON and TEXT formats)
        if (this.config!.enableJsonStreaming !== false) {
          try {
            this.parseAndEmitStreamingEvents(
              data,
              (request.metadata?.sessionId as string) || 'unknown',
              toolId,
            );
          } catch (error) {
            if (this.config!.debug) {
              engineLogger.warn(
                `Streaming parse error: ${error instanceof Error ? error.message : String(error)}`,
              );
            }
          }
        }
      });

      // Handle stderr - stream as progress events for user visibility, while also
      // retaining only a bounded tail for the final error payload.
      child.stderr?.on('data', (chunk: Buffer) => {
        const errorData = chunk.toString();
        stderrCapture = appendCappedText(stderrCapture, errorData, captureLimitBytes);

        if (this.config!.debug) {
          engineLogger.debug(`Script stderr: ${errorData}`);
        }

        // Emit stderr lines as progress events so errors are visible during execution
        const lines = errorData.split('\n');
        for (const line of lines) {
          if (!line.trim()) continue;
          this.emitProgressEvent({
            sessionId: (request.metadata?.sessionId as string) || 'unknown',
            timestamp: new Date(),
            backend: 'shell',
            count: ++this.eventCounter,
            type: ProgressEventType.THINKING,
            content: line,
            toolId,
            metadata: {
              format: 'text',
              source: 'stderr',
              raw: true,
            },
          }).catch((error) => {
            if (this.config?.debug) {
              engineLogger.warn(
                `Failed to emit stderr progress event: ${error instanceof Error ? error.message : String(error)}`,
              );
            }
          });
        }
      });

      // Handle process completion
      child.on('close', (exitCode) => {
        void (async () => {
          removeSignalOwners();
          if (ownedGroupIsActive()) {
            signalOwnedGroup('SIGTERM');
            if (!await awaitOwnedGroupExit(500)) {
              signalOwnedGroup('SIGKILL');
              if (!await awaitOwnedGroupExit(5000)) {
                await cleanupPromptFile();
                reject(new Error(`Failed to terminate shell process group ${child.pid ?? 'unknown'}`));
                return;
              }
            }
          }
          const duration = Date.now() - startTime;
          const success = exitCode === 0;

          let subAgentResponse: any;
          if (capturePath) {
            try {
              const captured = await fs.readFile(capturePath, 'utf-8');
              if (captured.trim()) {
                subAgentResponse = JSON.parse(captured);
              }
            } catch (error) {
              // ENOENT is expected when the subagent exits without producing a result event
              const isNotFound = (error as NodeJS.ErrnoException)?.code === 'ENOENT';
              if (!isNotFound && this.config?.debug) {
                engineLogger.warn(
                  `Failed to read subagent capture: ${error instanceof Error ? error.message : String(error)}`,
                );
              }
            } finally {
              if (captureDir) {
                try {
                  await fs.rm(captureDir, { recursive: true, force: true });
                } catch (cleanupError) {
                  if (this.config?.debug) {
                    engineLogger.warn(
                      `Failed to clean capture directory: ${cleanupError instanceof Error ? cleanupError.message : String(cleanupError)}`,
                    );
                  }
                }
              }
            }
          }

          await cleanupPromptFile();

          if (timedOutError) {
            reject(timedOutError);
            return;
          }

          if (this.config!.debug) {
            engineLogger.debug(
              `Script execution completed with exit code: ${exitCode}, duration: ${duration}ms`,
            );
            engineLogger.debug(
              `Stdout captured bytes: ${stdoutCapture.bytes}${stdoutCapture.truncated ? ' (truncated)' : ''}, ` +
                `Stderr captured bytes: ${stderrCapture.bytes}${stderrCapture.truncated ? ' (truncated)' : ''}`,
            );
          }

          const execResult: ScriptExecutionResult = {
            success,
            output: stdoutCapture.text,
            exitCode: exitCode || 0,
            duration,
            metadata: {
              outputTruncated: stdoutCapture.truncated,
              errorTruncated: stderrCapture.truncated,
              captureLimitBytes,
            },
          };
          if (stderrCapture.text) {
            execResult.error = stderrCapture.text;
          }
          if (subAgentResponse) {
            execResult.subAgentResponse = subAgentResponse;
          }
          resolve(execResult);
        })().finally(settleOwnedProcess);
      });

      // Handle process errors
      child.on('error', (error) => {
        void (async () => {
          removeSignalOwners();
          if (isProcessKilled) return; // Timeout settles from the close event.

          await cleanupPromptFile();

          if (this.config!.debug) {
            engineLogger.error(`Script execution error: ${error.message}`);
          }
          reject(new Error(`Failed to execute script: ${error.message}`));
        })().finally(() => {
          // A failed spawn owns no process; otherwise close must settle its group.
          if (!child.pid) settleOwnedProcess();
        });
      });

      // Apply timeout if configured. Pi live sessions are intentionally long-lived
      // interactive flows, so they should not be force-killed by backend timeout.
      const timeout = this.config!.timeout || 43200000; // 12 hours default for long-running operations
      const disableTimeoutForPiLive = isPiLiveMode;
      const timer = disableTimeoutForPiLive
        ? null
        : setTimeout(() => {
            if (isProcessKilled) return;

            isProcessKilled = true;
            if (this.config!.debug) {
              engineLogger.warn(`Script execution timed out after ${timeout}ms, killing process`);
            }

            timedOutError = new Error(`Script execution timed out after ${timeout}ms`);
            signalOwnedGroup('SIGTERM');

            // Do not settle until close proves the complete producer group released
            // its inherited output handles.
            setTimeout(() => signalOwnedGroup('SIGKILL'), 500);
          }, timeout);

      // Clear timeout when process completes
      child.on('close', () => {
        if (timer) clearTimeout(timer);
      });

      child.on('error', () => {
        if (timer) clearTimeout(timer);
      });
    });
  }

  /**
   * Build a structured, JSON-parsable result payload for programmatic capture while
   * preserving the shell backend's existing on-screen streaming behavior.
   */
  private buildStructuredOutput(
    subagentType: string,
    result: ScriptExecutionResult,
  ): { content: string; metadata?: ToolExecutionMetadata } {
    if (subagentType === 'claude') {
      const claudeEvent = result.subAgentResponse ?? this.extractLastJsonEvent(result.output);
      const isError = claudeEvent?.is_error ?? claudeEvent?.subtype === 'error';

      // Check for quota limit error
      const resultText = claudeEvent?.result ?? claudeEvent?.error ?? '';
      const quotaLimitInfo = detectQuotaLimit(resultText);

      const structuredPayload = {
        type: 'result',
        subtype: claudeEvent?.subtype || (isError ? 'error' : 'success'),
        is_error: isError,
        result: claudeEvent?.result ?? claudeEvent?.error ?? claudeEvent?.content ?? result.output,
        error: claudeEvent?.error,
        stderr: result.error,
        datetime: claudeEvent?.datetime,
        counter: claudeEvent?.counter,
        session_id: claudeEvent?.session_id,
        num_turns: claudeEvent?.num_turns,
        duration_ms: claudeEvent?.duration_ms ?? result.duration,
        exit_code: result.exitCode,
        total_cost_usd: claudeEvent?.total_cost_usd,
        usage: claudeEvent?.usage,
        modelUsage: claudeEvent?.modelUsage || claudeEvent?.model_usage || {},
        permission_denials: claudeEvent?.permission_denials || [],
        uuid: claudeEvent?.uuid,
        sub_agent_response: claudeEvent,
        // Add quota limit info if detected
        ...(quotaLimitInfo.detected && { quota_limit: quotaLimitInfo }),
      };

      const metadataObj: Record<string, unknown> = {
        structuredOutput: true,
        contentType: 'application/json',
        rawOutput: result.output,
      };
      if (claudeEvent) {
        metadataObj.subAgentResponse = claudeEvent;
      }
      if (quotaLimitInfo.detected) {
        metadataObj.quotaLimitInfo = quotaLimitInfo;
      }
      const metadata = metadataObj as ToolExecutionMetadata;

      return {
        content: JSON.stringify(structuredPayload),
        metadata,
      };
    }

    // Check for Codex quota limit errors in output
    if (subagentType === 'codex') {
      // Codex streams JSON events; look for error/turn.failed events with quota messages
      const codexQuotaMessage = this.extractCodexQuotaMessage(result.output, result.error);
      if (codexQuotaMessage) {
        const quotaLimitInfo = detectQuotaLimit(codexQuotaMessage);
        if (quotaLimitInfo.detected) {
          const metadata: ToolExecutionMetadata = {
            structuredOutput: true,
            contentType: 'application/json',
            rawOutput: result.output,
            quotaLimitInfo,
          };
          const structuredPayload = {
            type: 'result',
            subtype: 'error',
            is_error: true,
            result: codexQuotaMessage,
            error: codexQuotaMessage,
            exit_code: result.exitCode,
            duration_ms: result.duration,
            quota_limit: quotaLimitInfo,
          };
          return {
            content: JSON.stringify(structuredPayload),
            metadata,
          };
        }
      }
    }

    // For Codex subagent: extract structured result from capture file or last JSON event
    if (subagentType === 'codex') {
      const codexEvent = result.subAgentResponse ?? this.extractLastJsonEvent(result.output);
      if (codexEvent) {
        const isError = codexEvent.is_error ?? !result.success;
        // Extract message text from agent_message event format
        // Codex events can be: {msg: {message: "..."}} (legacy) or {item: {text: "..."}} (item.completed)
        const msgPayload = codexEvent.msg ?? codexEvent;
        const itemPayload =
          typeof codexEvent.item === 'object' && codexEvent.item ? codexEvent.item : undefined;
        const messageText =
          msgPayload.message ??
          msgPayload.text ??
          itemPayload?.text ??
          itemPayload?.message ??
          msgPayload.content ??
          result.output;
        const structuredPayload = {
          type: 'result',
          subtype: codexEvent.subtype || (isError ? 'error' : 'success'),
          is_error: isError,
          result: messageText,
          error: isError ? messageText : undefined,
          stderr: result.error,
          exit_code: result.exitCode,
          duration_ms: result.duration,
          sub_agent_response: codexEvent,
        };
        const metadata: ToolExecutionMetadata = {
          ...(codexEvent ? { subAgentResponse: codexEvent } : undefined),
          structuredOutput: true,
          contentType: 'application/json',
          rawOutput: result.output,
        };
        return {
          content: JSON.stringify(structuredPayload),
          metadata,
        };
      }
    }

    // For Pi subagent: extract structured result from capture file or last JSON event
    if (subagentType === 'pi') {
      const piEvent = result.subAgentResponse ?? this.extractLastJsonEvent(result.output);
      if (piEvent) {
        const piNestedEvent =
          typeof piEvent.sub_agent_response === 'object' && piEvent.sub_agent_response
            ? piEvent.sub_agent_response
            : undefined;
        const piSessionId =
          typeof piEvent.session_id === 'string' && piEvent.session_id
            ? piEvent.session_id
            : typeof piEvent.sessionId === 'string' && piEvent.sessionId
              ? piEvent.sessionId
            : typeof piEvent.id === 'string' && piEvent.type === 'session'
              ? piEvent.id
              : typeof piNestedEvent?.session_id === 'string' && piNestedEvent.session_id
                ? piNestedEvent.session_id
                : typeof piNestedEvent?.sessionId === 'string' && piNestedEvent.sessionId
                  ? piNestedEvent.sessionId
                  : typeof piNestedEvent?.id === 'string' && piNestedEvent.type === 'session'
                    ? piNestedEvent.id
              : typeof piEvent.sub_agent_response?.session_id === 'string' &&
                  piEvent.sub_agent_response.session_id
                ? piEvent.sub_agent_response.session_id
                : undefined;

        // Sanitize piEvent: strip bulky messages array and redundant type from sub_agent_response
        // to reduce token usage in the structured output
        const sanitizedPiEvent = { ...piEvent };
        delete sanitizedPiEvent.messages;
        // Also sanitize nested sub_agent_response if present (from pi.py capture)
        if (
          sanitizedPiEvent.sub_agent_response &&
          typeof sanitizedPiEvent.sub_agent_response === 'object'
        ) {
          const inner = { ...sanitizedPiEvent.sub_agent_response };
          delete inner.messages;
          delete inner.type;
          sanitizedPiEvent.sub_agent_response = inner;
        }

        // Extract result text: prefer .result, fall back to .messages array (agent_end events)
        const hasDirectResultText = typeof piEvent.result === 'string';
        let resultText: string | undefined = hasDirectResultText ? piEvent.result : undefined;
        if (resultText === undefined && Array.isArray(piEvent.messages)) {
          // agent_end event: extract last assistant message text
          for (let i = piEvent.messages.length - 1; i >= 0; i--) {
            const msg = piEvent.messages[i];
            if (msg?.role === 'assistant') {
              // Extract text from content array
              const content = msg.content;
              if (typeof content === 'string') {
                resultText = content;
              } else if (Array.isArray(content)) {
                const texts: string[] = [];
                for (const item of content) {
                  if (typeof item === 'string') texts.push(item);
                  else if (item?.type === 'text' && typeof item.text === 'string')
                    texts.push(item.text);
                }
                resultText = texts.join('\n');
              }
              if (resultText) break;
            }
          }
        }
        if (resultText === undefined && typeof piEvent.error === 'string') {
          resultText = piEvent.error;
        }

        if (resultText !== undefined) {
          const isError = piEvent.is_error ?? !result.success;
          const usage = piEvent.usage;
          const totalCostUsd =
            typeof piEvent.total_cost_usd === 'number'
              ? piEvent.total_cost_usd
              : typeof usage?.cost?.total === 'number'
                ? usage.cost.total
                : undefined;

          const structuredPayload = {
            type: 'result',
            subtype: piEvent.subtype || (isError ? 'error' : 'success'),
            is_error: isError,
            result: resultText,
            error: isError ? piEvent.error ?? result.error ?? resultText : piEvent.error,
            stderr: result.error,
            session_id: piSessionId,
            exit_code: result.exitCode,
            duration_ms: piEvent.duration_ms ?? result.duration,
            total_cost_usd: totalCostUsd,
            usage,
            sub_agent_response: sanitizedPiEvent,
          };
          const metadata: ToolExecutionMetadata = {
            ...(piEvent ? { subAgentResponse: piEvent } : undefined),
            structuredOutput: true,
            contentType: 'application/json',
            rawOutput: result.output,
          };
          return {
            content: JSON.stringify(structuredPayload),
            metadata,
          };
        }

        const isSessionSnapshotOnly = piEvent.type === 'session' || piEvent.subtype === 'session';
        if (isSessionSnapshotOnly) {
          const errorMessage =
            result.error?.trim() ||
            'Pi exited before emitting a terminal result event (session snapshot only).';
          const structuredPayload = {
            type: 'result',
            subtype: 'error',
            is_error: true,
            result: errorMessage,
            error: errorMessage,
            stderr: result.error,
            session_id: piSessionId,
            exit_code: result.exitCode,
            duration_ms: result.duration,
            sub_agent_response: sanitizedPiEvent,
          };
          const metadata: ToolExecutionMetadata = {
            ...(piEvent ? { subAgentResponse: piEvent } : undefined),
            structuredOutput: true,
            contentType: 'application/json',
            rawOutput: result.output,
          };
          return {
            content: JSON.stringify(structuredPayload),
            metadata,
          };
        }

        // Snapshot-only Pi captures (e.g. live session event written before agent_end)
        // still carry resumable session IDs even when no final result text is available.
        if (!result.success) {
          const errorMessage = result.error?.trim() || result.output?.trim() || 'Unknown error';
          const structuredPayload = {
            type: 'result',
            subtype: 'error',
            is_error: true,
            result: errorMessage,
            error: errorMessage,
            stderr: result.error,
            session_id: piSessionId,
            exit_code: result.exitCode,
            duration_ms: result.duration,
            sub_agent_response: sanitizedPiEvent,
          };
          const metadata: ToolExecutionMetadata = {
            ...(piEvent ? { subAgentResponse: piEvent } : undefined),
            structuredOutput: true,
            contentType: 'application/json',
            rawOutput: result.output,
          };
          return {
            content: JSON.stringify(structuredPayload),
            metadata,
          };
        }
      }
    }

    // For generic subagents: build structured error output on failure
    if (!result.success) {
      const errorMessage = result.error?.trim() || result.output?.trim() || 'Unknown error';
      const structuredPayload = {
        type: 'result',
        subtype: 'error',
        is_error: true,
        result: errorMessage,
        error: errorMessage,
        stderr: result.error,
        exit_code: result.exitCode,
        duration_ms: result.duration,
      };
      const metadata: ToolExecutionMetadata = {
        structuredOutput: true,
        contentType: 'application/json',
        rawOutput: result.output,
      };
      return {
        content: JSON.stringify(structuredPayload),
        metadata,
      };
    }

    const returnValue: { content: string; metadata?: ToolExecutionMetadata } = {
      content: result.output,
    };
    if (result.metadata) {
      returnValue.metadata = result.metadata as ToolExecutionMetadata;
    }
    return returnValue;
  }

  /**
   * Extract quota limit message from Codex stream output
   * Codex outputs JSON events like:
   * {"type": "error", "message": "You've hit your usage limit..."}
   * {"type": "turn.failed", "error": {"message": "You've hit your usage limit..."}}
   */
  private extractCodexQuotaMessage(output: string, stderr?: string): string | null {
    const sources = [output, stderr].filter(Boolean);

    for (const source of sources) {
      const lines = source!
        .split('\n')
        .map((l) => l.trim())
        .filter(Boolean);
      for (const line of lines) {
        try {
          const parsed = JSON.parse(line);
          // Check type=error events
          if (parsed?.type === 'error' && parsed?.message) {
            if (/you'?ve hit your usage limit/i.test(parsed.message)) {
              return parsed.message;
            }
          }
          // Check type=turn.failed events
          if (parsed?.type === 'turn.failed' && parsed?.error?.message) {
            if (/you'?ve hit your usage limit/i.test(parsed.error.message)) {
              return parsed.error.message;
            }
          }
        } catch {
          // Not JSON, check as plain text
          if (/you'?ve hit your usage limit/i.test(line)) {
            return line;
          }
        }
      }
    }

    return null;
  }

  /**
   * Parse JSON structured output payload emitted by shell service wrappers.
   */
  private parseStructuredResultPayload(
    content: string,
  ): { is_error?: boolean; subtype?: string; error?: string; result?: string } | null {
    try {
      const parsed = JSON.parse(content) as {
        is_error?: boolean;
        subtype?: string;
        error?: string;
        result?: string;
      };
      if (!parsed || typeof parsed !== 'object') {
        return null;
      }
      return parsed;
    } catch {
      return null;
    }
  }

  /**
   * Extract the last valid JSON object from a script's stdout to use as a structured payload fallback.
   */
  private extractLastJsonEvent(output: string): any | null {
    if (!output) {
      return null;
    }

    const lines = output
      .split('\n')
      .map((line) => line.trim())
      .filter(Boolean);

    for (let i = lines.length - 1; i >= 0; i--) {
      try {
        const parsed = JSON.parse(lines[i]!);
        if (parsed && typeof parsed === 'object') {
          return parsed;
        }
      } catch {
        // Ignore parse failures and continue scanning earlier lines
      }
    }

    return null;
  }

  /**
   * Parse streaming events from script output
   * Handles both JSON format (Claude) and TEXT format (Codex)
   *
   * Strategy:
   * 1. Try to parse each line as JSON first (for Claude)
   * 2. If JSON parsing fails, treat as TEXT streaming (for Codex and other text-based subagents)
   * 3. Emit all text lines (including whitespace-only) as progress events for real-time display
   */
  private parseAndEmitStreamingEvents(data: string, sessionId: string, toolId?: string): void {
    // Handle partial lines by maintaining a buffer
    if (!this.jsonBuffer) {
      this.jsonBuffer = '';
    }

    this.jsonBuffer += data;
    const streamBufferLimitBytes = getPositiveIntegerEnv(
      'JUNO_SHELL_STREAM_BUFFER_LIMIT_BYTES',
      DEFAULT_STREAM_BUFFER_LIMIT_BYTES,
    );
    if (Buffer.byteLength(this.jsonBuffer, 'utf8') > streamBufferLimitBytes) {
      // A provider can emit a very long line without a newline (for example a
      // large UI/log blob). Keep a bounded tail so streaming does not become an
      // accidental unbounded string accumulator under Node 24.
      this.jsonBuffer = appendCappedText(
        { text: '', bytes: 0, truncated: false },
        this.jsonBuffer,
        streamBufferLimitBytes,
      ).text;
    }

    const streamToolId = toolId || `stream_${Date.now()}`;

    // Split by lines, but keep the last potentially incomplete line in buffer
    const lines = this.jsonBuffer.split('\n');
    this.jsonBuffer = lines.pop() || ''; // Keep last incomplete line

    // Process complete lines
    for (const line of lines) {
      const rawLine = line.endsWith('\r') ? line.slice(0, -1) : line;
      if (!rawLine) continue;

      const hasNonWhitespace = rawLine.trim().length > 0;

      // Preserve whitespace-only lines (tabs/spaces) as-is for accurate pretty output rendering
      if (!hasNonWhitespace) {
        this.emitProgressEvent({
          sessionId,
          timestamp: new Date(),
          backend: 'shell',
          count: ++this.eventCounter,
          type: ProgressEventType.THINKING,
          content: rawLine,
          toolId: streamToolId,
          metadata: {
            format: 'text',
            raw: true,
          },
        }).catch((error) => {
          if (this.config?.debug) {
            engineLogger.warn(
              `Failed to emit whitespace-only streaming event: ${error instanceof Error ? error.message : String(error)}`,
            );
          }
        });
        continue;
      }

      const trimmedLine = rawLine.trim();

      // Try JSON parsing first (for Claude and other JSON-outputting subagents)
      let isJsonParsed = false;
      try {
        const jsonEvent = JSON.parse(trimmedLine);

        // Detect format: Claude CLI or generic StreamingEvent
        let progressEvent: ProgressEvent;

        if (this.isClaudeCliEvent(jsonEvent)) {
          // Handle Claude CLI specific format
          // Pass the original trimmedLine for raw JSON output mode
          progressEvent = this.convertClaudeEventToProgress(jsonEvent, sessionId, rawLine, streamToolId);
          isJsonParsed = true;
        } else if (this.isGenericStreamingEvent(jsonEvent)) {
          // Handle generic StreamingEvent format
          progressEvent = {
            sessionId,
            timestamp: jsonEvent.timestamp ? new Date(jsonEvent.timestamp) : new Date(),
            backend: 'shell',
            count: ++this.eventCounter,
            type: jsonEvent.type as ProgressEventType,
            content: jsonEvent.content,
            toolId: streamToolId,
            metadata: jsonEvent.metadata,
          };
          isJsonParsed = true;
        } else {
          // Unknown JSON format, treat as text below
          if (this.config?.debug) {
            engineLogger.debug(`Unknown JSON format, treating as text: ${trimmedLine}`);
          }
        }

        // Emit the progress event if JSON was successfully parsed
        if (isJsonParsed) {
          this.emitProgressEvent(progressEvent!).catch((error) => {
            if (this.config?.debug) {
              engineLogger.warn(
                `Failed to emit progress event: ${error instanceof Error ? error.message : String(error)}`,
              );
            }
          });
        }
      } catch (error) {
        // Not JSON - this is expected for text-based subagents like Codex
        // Treat as TEXT streaming and emit as thinking event
        isJsonParsed = false;
      }

      // If not JSON, handle as TEXT streaming (for Codex and other text-based outputs)
      if (!isJsonParsed && trimmedLine.length > 0) {
        this.emitProgressEvent({
          sessionId,
          timestamp: new Date(),
          backend: 'shell',
          count: ++this.eventCounter,
          type: ProgressEventType.THINKING,
          content: rawLine,
          toolId: streamToolId,
          metadata: {
            format: 'text',
            raw: true,
          },
        }).catch((error) => {
          if (this.config?.debug) {
            engineLogger.warn(
              `Failed to emit text streaming event: ${error instanceof Error ? error.message : String(error)}`,
            );
          }
        });
      }
    }
  }

  /**
   * Check if JSON event is Claude CLI format
   */
  private isClaudeCliEvent(event: any): boolean {
    return (
      event &&
      typeof event === 'object' &&
      event.type &&
      ['system', 'assistant', 'result'].includes(event.type)
    );
  }

  /**
   * Check if JSON event is generic StreamingEvent format
   */
  private isGenericStreamingEvent(event: any): boolean {
    return event && typeof event === 'object' && event.type && event.content !== undefined;
  }

  /**
   * Convert Claude CLI event to ProgressEvent format
   */
  private convertClaudeEventToProgress(
    event: any,
    sessionId: string,
    originalLine?: string,
    toolId?: string,
  ): ProgressEvent {
    let type: ProgressEventType;
    let content: string;
    const metadata: Record<string, any> = {};
    const eventToolId = toolId || `claude_${Date.now()}`;

    // If outputRawJson is enabled, pass the original JSON line for jq-style formatting
    // This allows the progress display to format it with colors and indentation
    if (this.config?.outputRawJson && originalLine) {
      // Determine event type based on Claude CLI format
      switch (event.type) {
        case 'system':
          type = ProgressEventType.TOOL_START;
          break;
        case 'assistant':
          type = ProgressEventType.THINKING;
          break;
        case 'result':
          type = event.is_error || event.subtype === 'error' ? ProgressEventType.ERROR : ProgressEventType.TOOL_RESULT;
          break;
        default:
          type = ProgressEventType.THINKING;
      }

      // Pass the raw JSON for jq-style formatting in the display layer
      content = originalLine;
      metadata.rawJsonOutput = true;
      metadata.originalType = event.type;
      metadata.parsedEvent = event; // Keep parsed version for metadata access

      return {
        sessionId,
        timestamp: new Date(),
        backend: 'shell',
        count: ++this.eventCounter,
        type,
        content,
        toolId: eventToolId,
        metadata,
      };
    }

    // Original simplified format (when outputRawJson is false/undefined)
    switch (event.type) {
      case 'system':
        // System/init event
        type = ProgressEventType.TOOL_START;
        content = `Initializing Claude session`;
        metadata.subtype = event.subtype;
        metadata.sessionId = event.session_id;
        metadata.model = event.model;
        metadata.tools = event.tools;
        metadata.cwd = event.cwd;
        break;

      case 'assistant':
        // Assistant message event
        type = ProgressEventType.THINKING;
        // Check if this is pretty-formatted JSON from claude.py
        if (!event.message && (event.content !== undefined || event.tool_use !== undefined)) {
          // Pretty-formatted: { "type": "assistant", "datetime": "...", "content": "...", "counter": "..." }
          // or with tool_use: { "type": "assistant", "datetime": "...", "tool_use": {...}, "counter": "..." }
          if (event.content && typeof event.content === 'string') {
            content = event.content;
          } else if (event.tool_use) {
            // For tool_use, show the tool name and input
            content = `Tool: ${event.tool_use.name}`;
            metadata.tool_use = event.tool_use; // Preserve tool_use data in metadata
          } else {
            content = ''; // Empty content (content was explicitly set to undefined/empty)
          }
        } else if (event.message?.content && Array.isArray(event.message.content)) {
          // Original format: Extract content from message.content array
          const textContent = event.message.content.find((c: any) => c.type === 'text');
          content = textContent?.text || 'Processing...';
        } else {
          content = 'Processing...';
        }
        metadata.messageId = event.message?.id;
        metadata.model = event.message?.model;
        metadata.usage = event.message?.usage;
        metadata.sessionId = event.session_id;
        metadata.datetime = event.datetime; // Preserve datetime from pretty format
        metadata.counter = event.counter; // Preserve counter from pretty format
        break;

      case 'result':
        // Result event
        if (event.is_error || event.subtype === 'error') {
          type = ProgressEventType.ERROR;
          content = event.result || event.error || 'Execution failed';
        } else {
          type = ProgressEventType.TOOL_RESULT;
          content = event.result || 'Execution completed';
        }
        metadata.subtype = event.subtype;
        metadata.duration = event.duration_ms;
        metadata.cost = event.total_cost_usd;
        metadata.usage = event.usage;
        metadata.sessionId = event.session_id;
        break;

      default:
        // Fallback to thinking
        type = ProgressEventType.THINKING;
        content = JSON.stringify(event);
        metadata.unknownType = event.type;
    }

    return {
      sessionId,
      timestamp: new Date(),
      backend: 'shell',
      count: ++this.eventCounter,
      type,
      content,
      toolId: eventToolId,
      metadata,
    };
  }


  /**
   * Emit progress event to all callbacks
   */
  private async emitProgressEvent(event: ProgressEvent): Promise<void> {
    // Write to log file first
    if (this.logFilePath) {
      const logMessage = `[${event.type}] ${event.content}${event.metadata ? ' | metadata: ' + JSON.stringify(event.metadata) : ''}`;
      await this.writeToLogFile(logMessage);
    }

    // Then emit to callbacks for screen display
    for (const callback of this.progressCallbacks) {
      try {
        await callback(event);
      } catch (error) {
        // Don't break on callback errors
        if (this.config?.debug) {
          engineLogger.warn(
            `Progress callback error: ${error instanceof Error ? error.message : String(error)}`,
          );
        }
      }
    }
  }
}
