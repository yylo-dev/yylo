/**
 * Logging system implementation for juno-task-ts
 *
 * File-based logging system matching Python version's format:
 * - Creates logs in .juno_task/logs/ directory
 * - Uses timestamp format: subagent_loop_{subagent}_{timestamp}.log
 * - Format: %(asctime)s - %(levelname)s - %(message)s
 * - Routes MCP debug logs to files instead of console
 */

import fs from 'fs-extra';
import path from 'node:path';
import { fileURLToPath as _fileURLToPath } from 'node:url';

export enum LogLevel {
  DEBUG = 0,
  INFO = 1,
  WARNING = 2,
  ERROR = 3,
  CRITICAL = 4,
}

export interface LoggerOptions {
  logDirectory?: string;
  logLevel?: LogLevel;
  enableConsoleLogging?: boolean;
}

/**
 * File-based logger for juno-task-ts
 * Matches Python version logging format and behavior
 */
export class JunoLogger {
  private logDirectory: string;
  private logLevel: LogLevel;
  private enableConsoleLogging: boolean;
  private logFilePath?: string;

  constructor(options: LoggerOptions = {}) {
    this.logDirectory = options.logDirectory || path.join(process.cwd(), '.juno_task', 'logs');
    this.logLevel = options.logLevel || LogLevel.INFO;
    this.enableConsoleLogging = options.enableConsoleLogging ?? true;
  }

  /**
   * Initialize logger with specific log file
   * Matches Python's generate_log_file function
   */
  async initialize(subagent: string = 'mcp'): Promise<void> {
    // Ensure log directory exists
    await fs.ensureDir(this.logDirectory);

    // Generate timestamp matching Python version
    const timestamp = new Date().toISOString().replace(/[:.]/g, '-').split('T');
    const dateStr = timestamp[0]!;
    const timeStr = timestamp[1]!.split('-')[0]!.substring(0, 6); // HH-MM-SS
    const fullTimestamp = `${dateStr}_${timeStr}`;

    // Generate log file name matching Python version: subagent_loop_{subagent}_{timestamp}.log
    const logFileName = `subagent_loop_${subagent}_${fullTimestamp}.log`;
    this.logFilePath = path.join(this.logDirectory, logFileName);

    // Initialize log file with header
    const header = `# YYLO TypeScript Log - Started ${new Date().toISOString()}\n`;
    await fs.writeFile(this.logFilePath, header);
  }

  /**
   * Format log message matching Python version format
   * Format: %(asctime)s - %(levelname)s - %(message)s
   */
  private formatMessage(level: string, message: string): string {
    const timestamp = new Date().toISOString();
    return `${timestamp} - ${level} - ${message}\n`;
  }

  /**
   * Write message to log file
   */
  private async writeToLog(message: string): Promise<void> {
    if (!this.logFilePath) {
      throw new Error('Logger not initialized. Call initialize() first.');
    }
    await fs.appendFile(this.logFilePath, message);
  }

  /**
   * Log message at specified level
   */
  private async log(
    level: LogLevel,
    levelName: string,
    message: string,
    writeToConsole: boolean = true,
  ): Promise<void> {
    if (level < this.logLevel) {
      return;
    }

    const formattedMessage = this.formatMessage(levelName, message);

    // Always write to log file
    await this.writeToLog(formattedMessage);

    // Optionally write to console (for non-MCP logs) - use stderr for logging
    if (this.enableConsoleLogging && writeToConsole) {
      console.error(formattedMessage.trim());
    }
  }

  /**
   * Debug level logging - typically goes to file only
   */
  async debug(message: string, writeToConsole: boolean = false): Promise<void> {
    await this.log(LogLevel.DEBUG, 'DEBUG', message, writeToConsole);
  }

  /**
   * Info level logging
   */
  async info(message: string, writeToConsole: boolean = true): Promise<void> {
    await this.log(LogLevel.INFO, 'INFO', message, writeToConsole);
  }

  /**
   * Warning level logging
   */
  async warning(message: string, writeToConsole: boolean = true): Promise<void> {
    await this.log(LogLevel.WARNING, 'WARNING', message, writeToConsole);
  }

  /**
   * Error level logging
   */
  async error(message: string, writeToConsole: boolean = true): Promise<void> {
    await this.log(LogLevel.ERROR, 'ERROR', message, writeToConsole);
  }

  /**
   * Critical level logging
   */
  async critical(message: string, writeToConsole: boolean = true): Promise<void> {
    await this.log(LogLevel.CRITICAL, 'CRITICAL', message, writeToConsole);
  }

  /**
   * Get the current log file path
   */
  getLogFilePath(): string | undefined {
    return this.logFilePath;
  }

  /**
   * Set log level
   */
  setLogLevel(level: LogLevel): void {
    this.logLevel = level;
  }

  /**
   * Enable/disable console logging
   */
  setConsoleLogging(enabled: boolean): void {
    this.enableConsoleLogging = enabled;
  }
}

/**
 * MCP-specific logger that routes all [MCP] messages to log files
 * This is the main fix for the MCP logging pollution issue
 */
export class MCPLogger {
  private logger: JunoLogger;
  private initialized: boolean = false;

  constructor(options?: LoggerOptions) {
    this.logger = new JunoLogger(options);
  }

  /**
   * Initialize MCP logger
   */
  async initialize(): Promise<void> {
    if (!this.initialized) {
      await this.logger.initialize('mcp');
      this.initialized = true;
    }
  }

  /**
   * Log MCP-specific debug messages (file only)
   * This replaces console.log('[MCP] ...') calls
   */
  async debug(message: string): Promise<void> {
    await this.ensureInitialized();
    await this.logger.debug(`[MCP] ${message}`, false); // Never write to console
  }

  /**
   * Log MCP info messages (file only)
   */
  async info(message: string): Promise<void> {
    await this.ensureInitialized();
    await this.logger.info(`[MCP] ${message}`, false); // Never write to console
  }

  /**
   * Log MCP warning messages (file only)
   */
  async warning(message: string): Promise<void> {
    await this.ensureInitialized();
    await this.logger.warning(`[MCP] ${message}`, false); // Never write to console
  }

  /**
   * Log MCP error messages (file only by default)
   */
  async error(message: string, writeToConsole: boolean = false): Promise<void> {
    await this.ensureInitialized();
    await this.logger.error(`[MCP] ${message}`, writeToConsole); // Default to file only
  }

  /**
   * Get log file path for debugging
   */
  getLogFilePath(): string | undefined {
    return this.logger.getLogFilePath();
  }

  private async ensureInitialized(): Promise<void> {
    if (!this.initialized) {
      await this.initialize();
    }
  }
}

/**
 * Global MCP logger instance for use throughout the application
 */
let globalMCPLogger: MCPLogger | null = null;

/**
 * Get or create the global MCP logger instance
 */
export function getMCPLogger(options?: LoggerOptions): MCPLogger {
  if (!globalMCPLogger) {
    globalMCPLogger = new MCPLogger(options);
  }
  return globalMCPLogger;
}

/**
 * Initialize the global MCP logger
 */
export async function initializeMCPLogger(options?: LoggerOptions): Promise<MCPLogger> {
  const logger = getMCPLogger(options);
  await logger.initialize();
  return logger;
}
