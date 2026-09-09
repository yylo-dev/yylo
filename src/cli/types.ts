/**
 * CLI Types Module for yylo
 *
 * TypeScript interfaces and types for the CLI framework,
 * supporting commands, options, error handling, and constants.
 */

import type { SubagentType, SessionStatus, LogLevel, BackendType } from '../types/index';

// ============================================================================
// Command Structure Types
// ============================================================================

/**
 * Interface for usage examples
 */
export interface CommandExample {
  /** Full command example */
  command: string;
  /** Description of what the example does */
  description: string;
}

// ============================================================================
// CLI Options Types
// ============================================================================

/**
 * Global CLI options available to all commands
 */
export interface GlobalCLIOptions {
  /** Verbosity level: 0=quiet, 1=normal+helping texts (default), 2=debug+hooks. Accepts number, boolean, or string */
  verbose?: number;
  /** Quiet mode: suppress agent messages and hook output (alias: --silent) */
  quiet?: boolean;
  /** Emit the versioned public machine execution envelope as the only stdout payload. */
  executionEnvelope?: boolean;
  /** Alias for --quiet */
  silent?: boolean;
  /** Configuration file path (.json, .toml, pyproject.toml) */
  config?: string;
  /** Log file path (auto-generated if not specified) */
  logFile?: string;
  /** Disable colored output */
  noColor?: boolean;
  /** Log level for output */
  logLevel?: LogLevel;
  /** Enable concurrent feedback collection during execution */
  enableFeedback?: boolean;
  /** Behavior when Claude hourly quota limit is reached: "wait" to sleep until reset, "raise" to exit immediately */
  onHourlyLimit?: 'wait' | 'raise';
  /** Skip execution of all lifecycle hooks (`false` for --no-hooks) */
  hooks?: boolean;
  /** Alias state for --no-hook (`false` when passed) */
  hook?: boolean;
}

/** Normalize Commander's singular/plural negated option keys. */
export function areLifecycleHooksDisabled(
  options: Pick<GlobalCLIOptions, 'hooks' | 'hook'>,
): boolean {
  return options.hooks === false || options.hook === false;
}

/**
 * Main execution command options
 */
export interface MainCommandOptions extends GlobalCLIOptions {
  /** Subagent to use (optional; falls back to config/defaults) */
  subagent?: SubagentType;
  /** Prompt input (file path or inline text). Commander sets to `true` when -p flag used without argument (e.g. heredoc) */
  prompt?: string | true;
  /** Prompt file path (alternative to -p "$(cat file)") */
  promptFile?: string;
  /** Working directory */
  cwd?: string;
  /** Maximum iterations (-1 for unlimited) */
  maxIterations?: number;
  /** Model to use (subagent-specific) */
  model?: string;
  /** Agents configuration (forwarded to shell backend --agents flag) */
  agents?: string;
  /** Available tools from built-in set (only works with --print mode, forwarded to shell backend --tools flag) */
  tools?: string[];
  /** Permission-based filtering of specific tool instances (forwarded to shell backend --allowedTools flag) */
  allowedTools?: string[];
  /** Disallowed tools for Claude (forwarded to shell backend --disallowed-tool flag) */
  disallowedTools?: string[];
  /** Append tools to default allowed-tools list (mutually exclusive with --allowed-tools, forwarded to shell backend --appendAllowedTools flag) */
  appendAllowedTools?: string[];
  /** Backend type (shell) */
  backend?: 'shell';
  /** Interactive mode for typing prompts */
  interactive?: boolean;
  /** Run Pi subagent in interactive live TUI mode (pi-only) */
  live?: boolean;
  /** Launch interactive prompt editor */
  interactivePrompt?: boolean;
  /** Resume a conversation by session ID (shell backend only) */
  resume?: string;
  /** Prompt text supplied by the CLI --clone UX; enables cloneSession after validation */
  clone?: string | true;
  /** Clone/fork the resume session through Pi native --fork (Pi only) */
  cloneSession?: boolean;
  /** Explicit Pi session ID/path to fork from when cloning */
  cloneFromSession?: string;
  /** Named branch target for `yylo clone --name <branch>` */
  cloneBranchName?: string;
  /** Named branch source for `yylo clone --from <branch>` */
  cloneBranchFrom?: string;
  /** Continue the most recent conversation (shell backend only) */
  continue?: boolean;
  /** Load last persisted session+settings snapshot and auto-populate resume/options */
  continueFromLatest?: boolean;
  /** Extended thinking level (forwarded to shell backend --thinking flag, pi subagent) */
  thinking?: string;
}

/**
 * Init command options
 */
export interface InitCommandOptions extends GlobalCLIOptions {
  /** Target directory */
  directory?: string;
  /** Force overwrite existing files */
  force?: boolean;
  /** Main task description */
  task?: string;
  /** Preferred subagent */
  subagent?: SubagentType;
  /** Repository URL */
  gitUrl?: string;
  /** Product branch used by the generated task workspace */
  targetBranch?: string;
  /** Force interactive mode for guided setup */
  interactive?: boolean;
  /** Template variant to use */
  template?: string;
  /** Custom template variables */
  variables?: Record<string, string>;
}

/**
 * Start command options
 */
export interface StartCommandOptions extends GlobalCLIOptions {
  /** Subagent to use (optional override of config default) */
  subagent?: SubagentType;
  /** Backend type (shell) */
  backend?: BackendType;
  /** Maximum iterations */
  maxIterations?: number;
  /** Model to use */
  model?: string;
  /** Agents configuration (forwarded to shell backend --agents flag) */
  agents?: string;
  /** Available tools from built-in set (only works with --print mode, forwarded to shell backend --tools flag) */
  tools?: string[];
  /** Permission-based filtering of specific tool instances (forwarded to shell backend --allowedTools flag) */
  allowedTools?: string[];
  /** Disallowed tools for Claude (forwarded to shell backend --disallowed-tool flag) */
  disallowedTools?: string[];
  /** Append tools to default allowed-tools list (mutually exclusive with --allowed-tools, forwarded to shell backend --appendAllowedTools flag) */
  appendAllowedTools?: string[];
  /** Resume a conversation by session ID (shell backend only) */
  resume?: string;
  /** Prompt text supplied by the CLI --clone UX; enables cloneSession after validation */
  clone?: string | true;
  /** Clone/fork the resume session through Pi native --fork (Pi only) */
  cloneSession?: boolean;
  /** Explicit Pi session ID/path to fork from when cloning */
  cloneFromSession?: string;
  /** Continue the most recent conversation (shell backend only) */
  continue?: boolean;
  /** Project directory */
  directory?: string;
  /** Display performance metrics summary after execution */
  showMetrics?: boolean;
  /** Show interactive performance dashboard after execution */
  showDashboard?: boolean;
  /** Display performance trends from historical data */
  showTrends?: boolean;
  /** Save performance metrics to file */
  saveMetrics?: boolean | string;
  /** Custom path for metrics file */
  metricsFile?: string;
  /** Enable concurrent feedback collection during execution */
  enableFeedback?: boolean;
  /** Validate configuration and exit without executing */
  dryRun?: boolean;
}

/**
 * Feedback command options
 */
export interface FeedbackCommandOptions extends GlobalCLIOptions {
  /** Custom USER_FEEDBACK.md file path */
  file?: string;
  /** Interactive multiline input */
  interactive?: boolean;
  /** Issue description */
  issue?: string;
  /** Test criteria or success factors */
  test?: string;
  /** Test criteria alias */
  testCriteria?: string;
}

/**
 * Session list command options
 */
export interface SessionListOptions extends GlobalCLIOptions {
  /** Maximum sessions to show */
  limit?: number;
  /** Filter by subagent */
  subagent?: SubagentType;
  /** Filter by status */
  status?: SessionStatus[];
}

/**
 * Session info command options
 */
export interface SessionInfoOptions extends GlobalCLIOptions {
  /** Show detailed information (inherits verbosity level from GlobalCLIOptions) */
}

/**
 * Session remove command options
 */
export interface SessionRemoveOptions extends GlobalCLIOptions {
  /** Skip confirmation prompt */
  force?: boolean;
}

/**
 * Session clean command options
 */
export interface SessionCleanOptions extends GlobalCLIOptions {
  /** Remove sessions older than N days */
  days?: number;
  /** Remove only empty log files */
  empty?: boolean;
  /** Skip confirmation prompt */
  force?: boolean;
}

/**
 * Setup-git command options
 */
export interface SetupGitOptions extends GlobalCLIOptions {
  /** Show current upstream URL configuration */
  show?: boolean;
  /** Remove upstream URL configuration */
  remove?: boolean;
}

/**
 * Test command options
 */
export interface TestCommandOptions extends GlobalCLIOptions {
  /** Test type to generate/run */
  type?: 'unit' | 'integration' | 'e2e' | 'performance' | 'all';
  /** AI subagent for test generation */
  subagent?: SubagentType;
  /** AI intelligence level */
  intelligence?: 'basic' | 'smart' | 'comprehensive';
  /** Generate tests using AI */
  generate?: boolean;
  /** Execute tests */
  run?: boolean;
  /** Generate coverage report */
  coverage?: boolean | string;
  /** Analyze test quality and coverage */
  analyze?: boolean;
  /** Analysis quality level */
  quality?: 'basic' | 'thorough' | 'exhaustive';
  /** Generate improvement suggestions */
  suggestions?: boolean;
  /** Generate test report */
  report?: boolean | string;
  /** Report format */
  format?: 'json' | 'html' | 'markdown' | 'console';
  /** Test template to use */
  template?: string;
  /** Testing framework */
  framework?: 'vitest' | 'jest' | 'mocha' | 'custom';
  /** Watch mode for continuous testing */
  watch?: boolean;
  /** Test reporters (comma-separated) */
  reporters?: string[];
}

// ============================================================================
// Parsed Arguments Types
// ============================================================================

/**
 * Parsed command line arguments
 */
export interface ParsedArgs {
  /** Command name */
  command: string;
  /** Subcommand name (if applicable) */
  subcommand?: string;
  /** Positional arguments */
  args: string[];
  /** Parsed options */
  options: Record<string, any>;
  /** Unknown options (for validation) */
  unknown: string[];
}

/**
 * Option validation result
 */
export interface OptionValidationResult {
  /** Whether validation passed */
  valid: boolean;
  /** Validation error messages */
  errors: string[];
  /** Validation warnings */
  warnings: string[];
  /** Normalized/coerced option values */
  normalizedOptions: Record<string, any>;
}

// ============================================================================
// Error Types
// ============================================================================

/**
 * Base CLI error class
 */
export abstract class CLIError extends Error {
  /** Error code for programmatic handling */
  abstract code: string;
  /** Whether error should show help */
  showHelp: boolean = false;
  /** Suggested solutions */
  suggestions: string[] = [];

  constructor(message: string, showHelp: boolean = false) {
    super(message);
    this.name = this.constructor.name;
    this.showHelp = showHelp;
  }
}

/**
 * Validation error (user input issues)
 */
export class ValidationError extends CLIError {
  code = 'VALIDATION_ERROR';

  constructor(message: string, suggestions: string[] = []) {
    super(message, true);
    this.suggestions = suggestions;
  }
}

/**
 * Configuration error (config file/setup issues)
 */
export class ConfigurationError extends CLIError {
  code = 'CONFIGURATION_ERROR';

  constructor(message: string, suggestions: string[] = []) {
    super(message, false);
    this.suggestions = suggestions;
  }
}

/**
 * Runtime error (file system operations, process failures, etc.)
 */
export class RuntimeError extends CLIError {
  code = 'RUNTIME_ERROR';

  constructor(message: string, path?: string) {
    super(path ? `${message}: ${path}` : message);
    this.suggestions = [
      'Check file/directory permissions',
      'Verify path exists and is accessible',
      'Use absolute paths to avoid ambiguity',
    ];
  }
}

// ============================================================================
// Environment Variables
// ============================================================================

/**
 * Environment variable mappings for CLI options
 * Uses YYLO_* prefix exclusively (legacy JUNO_TASK_* removed)
 */
export const ENVIRONMENT_MAPPINGS = {
  // Core options
  YYLO_SUBAGENT: 'subagent',
  YYLO_AGENT: 'backend',
  YYLO_BACKEND: 'backend',
  YYLO_PROMPT: 'prompt',
  YYLO_CWD: 'cwd',
  YYLO_MAX_ITERATIONS: 'maxIterations',
  YYLO_MODEL: 'model',
  YYLO_LOG_FILE: 'logFile',
  YYLO_VERBOSE: 'verbose',
  YYLO_QUIET: 'quiet',
  YYLO_INTERACTIVE: 'interactive',
  YYLO_CONFIG: 'config',

  // MCP options
  YYLO_MCP_SERVER_PATH: 'mcpServerPath',
  YYLO_MCP_TIMEOUT: 'mcpTimeout',
  YYLO_MCP_RETRIES: 'mcpRetries',

  // Session options
  YYLO_SESSION_DIR: 'sessionDir',
  YYLO_LOG_LEVEL: 'logLevel',

  // Template options
  YYLO_TEMPLATE: 'template',
  YYLO_FORCE: 'force',

  // Git options
  YYLO_GIT_URL: 'gitUrl',

  // UI options
  YYLO_NO_COLOR: 'noColor',
  YYLO_HEADLESS: 'headless',

  // Feedback options
  YYLO_ENABLE_FEEDBACK: 'enableFeedback',

  // Special aliases
  JUNO_INTERACTIVE_FEEDBACK_MODE: 'enableFeedback', // Alias for enableFeedback
} as const;

/**
 * Type for environment variable keys
 */
export type EnvironmentVariable = keyof typeof ENVIRONMENT_MAPPINGS;

/**
 * Type for CLI option keys
 */
export type CLIOptionKey = (typeof ENVIRONMENT_MAPPINGS)[EnvironmentVariable];

// ============================================================================
// Configuration Types
// ============================================================================

/**
 * Initialization data for project setup
 */
export interface InitializationData {
  /** Main task description */
  task: string;
  /** Preferred subagent */
  subagent: SubagentType;
  /** Repository URL (optional) */
  gitUrl?: string;
  /** Template variables */
  variables: Record<string, string>;
  /** Template variant */
  template: string;
  /** Additional metadata */
  metadata?: {
    author?: string;
    description?: string;
    tags?: string[];
  };
}

/**
 * Command execution result
 */
export interface CommandExecutionResult {
  /** Whether command succeeded */
  success: boolean;
  /** Exit code */
  exitCode: number;
  /** Execution time in milliseconds */
  duration: number;
  /** Output messages */
  output: string[];
  /** Error messages */
  errors: string[];
}

// ============================================================================
// Type Guards
// ============================================================================

/**
 * Type guard for CLI errors
 */
export function isCLIError(error: unknown): error is CLIError {
  return error instanceof CLIError;
}

/**
 * Type guard for validation errors
 */
export function isValidationError(error: unknown): error is ValidationError {
  return error instanceof ValidationError;
}

/**
 * Type guard for configuration errors
 */
export function isConfigurationError(error: unknown): error is ConfigurationError {
  return error instanceof ConfigurationError;
}

/**
 * Type guard for runtime errors
 */
export function isRuntimeError(error: unknown): error is RuntimeError {
  return error instanceof RuntimeError;
}

// ============================================================================
// Constants
// ============================================================================

/**
 * Supported subagent aliases
 */
export const SUBAGENT_ALIASES: Record<string, SubagentType> = {
  'claude-code': 'claude',
  claude_code: 'claude',
  'gemini-cli': 'gemini',
  'cursor-agent': 'cursor',
  'pi-agent': 'pi',
};

/**
 * Command categories for help organization
 */
export const COMMAND_CATEGORIES = {
  EXECUTION: ['yylo', 'start'],
  PROJECT: ['init', 'setup-git'],
  TESTING: ['test'],
  SESSION: ['session'],
  FEEDBACK: ['feedback'],
} as const;

/**
 * Exit codes for different error types
 */
export const EXIT_CODES = {
  SUCCESS: 0,
  VALIDATION_ERROR: 1,
  CONFIGURATION_ERROR: 2,
  COMMAND_NOT_FOUND: 3,
  RUNTIME_ERROR: 5,
  UNEXPECTED_ERROR: 99,
} as const;

export type ExitCode = (typeof EXIT_CODES)[keyof typeof EXIT_CODES];
