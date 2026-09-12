/**
 * yylo - TypeScript implementation of yylo CLI tool
 *
 * Main entry point for the library exports
 */

// Core exports
export * from './core/config';
export * from './templates/default-hooks';
export * from './core/engine';
export * from './core/execution-envelope';
export {
  MACHINE_RESPONSE_SCHEMA,
  type MachineFormat,
  type MachineOutputRequest,
  type MachineResponse,
} from './cli/machine-output';
export * from './core/session';
export * from './core/tmux-workspace';
// Utility exports (excluding validateConfig to avoid conflicts)
export * from './utils/environment';
export {
  executeHook,
  executeHooks,
  validateHooksConfig,
  type HooksConfig,
  type HookExecutionContext,
  type HookExecutionOptions,
  type HookExecutionResult,
  type CommandExecutionResult,
} from './utils/hooks';
export {
  SubagentSchema,
  LogLevelSchema,
  SessionStatusSchema,
  IterationsSchema,
  ModelSchema,
  ConfigValidationSchema,
  validateSubagent,
  validateModel,
  validateIterations,
  validateLogLevel,
  validateEnvironmentVars,
} from './utils/validation';

// Type exports (consolidated to avoid conflicts)
export * from './types';

// Version information
export { version } from './version';
