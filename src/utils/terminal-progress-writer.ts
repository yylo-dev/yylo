/**
 * Terminal Progress Writer Utility
 *
 * Provides safe progress output to stderr that properly coordinates with active user input on stdin.
 * This prevents visual mixing of progress output with user-typed characters.
 *
 * The problem:
 * - When user is typing on stdin, their characters are echoed to the terminal
 * - If progress output writes to stderr while user is typing, it can visually interrupt the input line
 * - Result: User sees "xxxxx[MCP] Progress event:..." instead of clean separation
 *
 * The solution:
 * - Use ANSI escape codes to clear the current input line before writing progress
 * - Write the progress output
 * - The terminal will automatically restore the input line after our output
 *
 * ANSI Escape Codes used:
 * - \r - Move cursor to beginning of line
 * - \x1b[K - Clear from cursor to end of line
 * - \x1b[2K - Clear entire line
 */

import { EOL } from 'node:os';
import { isFeedbackActive, bufferProgressEvent } from './feedback-state.js';

export interface TerminalProgressOptions {
  /**
   * Whether to use terminal-aware output (with ANSI codes)
   * @default true if stderr.isTTY, false otherwise
   */
  terminalAware?: boolean;

  /**
   * Custom output stream
   * @default process.stderr
   */
  stream?: NodeJS.WriteStream;
}

/**
 * Terminal-aware progress writer that coordinates with active user input
 */
export class TerminalProgressWriter {
  private options: Required<TerminalProgressOptions>;

  constructor(options: TerminalProgressOptions = {}) {
    this.options = {
      terminalAware: options.terminalAware ?? process.stderr.isTTY ?? false,
      stream: options.stream ?? process.stderr,
    };
  }

  /**
   * Write progress output to stderr with proper terminal coordination
   *
   * @param content - The content to write (can be string or object for console.error formatting)
   */
  write(content: any): void {
    // If feedback collection is active, buffer the progress instead of displaying
    // This prevents visual mixing of progress with user input
    if (isFeedbackActive()) {
      bufferProgressEvent(content);
      return;
    }

    if (this.options.terminalAware) {
      // Terminal-aware mode: clear the line, write content, let terminal restore input
      // Note: We use \r\x1b[K to clear the current line before writing
      // This ensures any partial user input is temporarily cleared
      this.options.stream.write('\r\x1b[K'); // Move to start and clear line

      // Write the actual content using console.error for proper formatting
      // console.error writes to stderr and handles object formatting with colors
      if (typeof content === 'string') {
        this.options.stream.write(content);
        if (!content.endsWith('\n') && !content.endsWith(EOL)) {
          this.options.stream.write(EOL);
        }
      } else {
        // Use console.error for object formatting (colors, indentation, etc.)
        console.error(content);
      }

      // Note: We don't need to manually restore the input line
      // The terminal automatically re-displays the user's input buffer after our output
    } else {
      // Non-TTY mode: simple write without ANSI codes
      if (typeof content === 'string') {
        this.options.stream.write(content);
        if (!content.endsWith('\n') && !content.endsWith(EOL)) {
          this.options.stream.write(EOL);
        }
      } else {
        console.error(content);
      }
    }
  }

  /**
   * Write formatted progress message with prefix
   *
   * @param prefix - Prefix to add before the message (e.g., '[MCP]')
   * @param message - The message or object to write
   */
  writeWithPrefix(prefix: string, message: any): void {
    // If feedback collection is active, buffer the progress instead of displaying
    // This prevents visual mixing of progress with user input
    if (isFeedbackActive()) {
      bufferProgressEvent(message, prefix);
      return;
    }

    if (this.options.terminalAware) {
      this.options.stream.write('\r\x1b[K'); // Clear line first
    }

    // Write prefix and message using console.error for formatting
    console.error(prefix, message);
  }

  /**
   * Check if terminal-aware mode is enabled
   */
  isTerminalAware(): boolean {
    return this.options.terminalAware;
  }
}

/**
 * Global singleton instance for consistent terminal progress output
 */
let globalWriter: TerminalProgressWriter | null = null;

/**
 * Get or create the global terminal progress writer
 */
export function getTerminalProgressWriter(): TerminalProgressWriter {
  if (!globalWriter) {
    globalWriter = new TerminalProgressWriter();
  }
  return globalWriter;
}

/**
 * Write progress to stderr with proper terminal coordination
 *
 * This is the recommended way to write progress output that might appear
 * while the user is actively typing input.
 *
 * @param content - Content to write (string or object)
 */
export function writeTerminalProgress(content: any): void {
  getTerminalProgressWriter().write(content);
}

/**
 * Write progress with prefix (e.g., '[MCP] Progress event:', data)
 *
 * @param prefix - Prefix string
 * @param message - Message or object to write
 */
export function writeTerminalProgressWithPrefix(prefix: string, message: any): void {
  getTerminalProgressWriter().writeWithPrefix(prefix, message);
}

/** Invocation-local waiting notices, never a timeout or cancellation owner. */
export async function withWaitingProgress<T>(
  message: string,
  operation: () => Promise<T>,
  enabled = true,
): Promise<T> {
  if (!enabled) return operation();
  const notify = (): void => {
    try { writeTerminalProgress(`YYLO: ${message}`); } catch { /* Preserve the primary operation. */ }
  };
  // Announce before potentially synchronous work, then leave headroom below
  // the five-second progress interval. Nothing is written to stdout.
  notify();
  const timer = setInterval(notify, 4000);
  timer.unref();
  try { return await operation(); } finally { clearInterval(timer); }
}

/**
 * Reset the global writer (useful for testing)
 */
export function resetTerminalProgressWriter(): void {
  globalWriter = null;
}
