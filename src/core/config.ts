/**
 * Core configuration module for yylo
 *
 * Provides comprehensive configuration management with multi-source loading,
 * validation, and environment variable support.
 *
 * @module core/config
 */

import { z } from 'zod';
import * as path from 'node:path';
import { randomUUID } from 'node:crypto';
import * as nodeFs from 'node:fs';
import { promises as fsPromises } from 'node:fs';
import * as yaml from 'js-yaml';
import fs from 'fs-extra';
import type { JunoTaskConfig, PromptMacroConfig } from '../types/index';
import { getDefaultHooks } from '../templates/default-hooks.js';
import { SUBAGENT_DEFAULT_MODELS } from './subagent-models.js';
import { migrateLegacyEnvironment } from './identity-migration.js';
import { resolveController } from '../utils/controller-resolver.js';

/**
 * Environment variable mapping for configuration options
 * All config options can be set via YYLO_* environment variables
 * Uses YYLO_* environment variables
 */
export const ENV_VAR_MAPPING = {
  // Core settings
  YYLO_DEFAULT_SUBAGENT: 'defaultSubagent',
  YYLO_DEFAULT_BACKEND: 'defaultBackend',
  YYLO_DEFAULT_MAX_ITERATIONS: 'defaultMaxIterations',
  YYLO_DEFAULT_MODEL: 'defaultModel',

  // Logging settings
  YYLO_LOG_LEVEL: 'logLevel',
  YYLO_LOG_FILE: 'logFile',
  YYLO_VERBOSE: 'verbose',
  YYLO_QUIET: 'quiet',

  // MCP settings
  YYLO_MCP_TIMEOUT: 'mcpTimeout',
  YYLO_MCP_RETRIES: 'mcpRetries',
  YYLO_MCP_SERVER_PATH: 'mcpServerPath',
  YYLO_MCP_SERVER_NAME: 'mcpServerName',

  // Hook settings
  YYLO_HOOK_COMMAND_TIMEOUT: 'hookCommandTimeout',

  // Quota/hourly limit settings
  YYLO_ON_HOURLY_LIMIT: 'onHourlyLimit',

  // TUI settings
  YYLO_INTERACTIVE: 'interactive',
  YYLO_HEADLESS_MODE: 'headlessMode',

  // Paths
  YYLO_WORKING_DIRECTORY: 'workingDirectory',
  YYLO_SESSION_DIRECTORY: 'sessionDirectory',
} as const;

/**
 * Zod schema for validating subagent types
 */
const SubagentTypeSchema = z.enum(['claude', 'cursor', 'codex', 'gemini', 'pi']);

/**
 * Zod schema for validating model shortcuts.
 * Keys must start with ":" (e.g., ":sonnet", ":fav").
 * Values are arbitrary model identifiers.
 */
const ModelShortcutMapSchema = z.record(
  z.string().regex(
    /^:[a-zA-Z0-9_-]+$/,
    'model shortcut must start with ":" and contain only alphanumeric, hyphen, or underscore',
  ),
  z.string().trim().min(1, 'model shortcut target must not be empty'),
);

const ModelShortcutsSchema = z
  .object({
    claude: ModelShortcutMapSchema.optional(),
    cursor: ModelShortcutMapSchema.optional(),
    codex: ModelShortcutMapSchema.optional(),
    gemini: ModelShortcutMapSchema.optional(),
    pi: ModelShortcutMapSchema.optional(),
  })
  .strict();

/**
 * Zod schema for validating backend types
 */
const BackendTypeSchema = z.enum(['shell']);

/**
 * Zod schema for validating log levels
 */
const LogLevelSchema = z.enum(['error', 'warn', 'info', 'debug', 'trace']);

/**
 * Zod schema for validating on-hourly-limit behavior
 */
const OnHourlyLimitSchema = z.enum(['wait', 'raise']);

/**
 * Zod schema for validating hook types
 */
const HookTypeSchema = z.enum([
  'START_RUN',
  'START_ITERATION',
  'END_ITERATION',
  'END_RUN',
  'ON_STALE',
]);

/**
 * Zod schema for validating individual hook configuration
 */
const HookSchema = z.object({
  commands: z.array(z.string()).describe('List of bash commands to execute for this hook'),
});

/**
 * Zod schema for validating hooks configuration
 * Maps hook types to their respective configurations
 */
const HooksSchema = z.record(HookTypeSchema, HookSchema).optional();

const PromptMacroOrderSchema = z.enum([
  'before_command_substitution',
  'after_command_substitution',
]);

const PromptMacroDictionarySchema = z.record(z.string(), z.string());

type RawPromptMacroValue = string | { path?: unknown; text?: unknown };
type RawPromptMacroDictionary = Record<string, RawPromptMacroValue>;

const PromptMacrosSchema = z
  .object({
    enabled: z.boolean().optional(),
    order: PromptMacroOrderSchema.optional(),
    maxDepth: z.number().int().min(1).max(100).optional(),
    global: PromptMacroDictionarySchema.optional(),
    local: PromptMacroDictionarySchema.optional(),
  })
  .strict()
  .optional();

const GitCheckpointAgentSchema = z
  .object({
    enabled: z.boolean().optional(),
    service: z.string().min(1).optional(),
    model: z.string().min(1).optional(),
    timeoutSeconds: z.number().int().min(1).max(600).optional(),
  })
  .strict();

export const DEFAULT_GIT_CHECKPOINT_INCLUDE = [
  '.juno_task/tasks',
  '.juno_task/ledger',
  '.juno_task/wiki',
  '.juno_task/specs',
  '.juno_task/workflows',
  '.juno_task/plan.md',
  '.juno_task/tasks.md',
  '.juno_task/managed-assets.json',
] as const;

export const PROJECT_CONFIG_VERSION = 1;

const GitCheckpointSchema = z
  .object({
    include: z.array(z.string().min(1)).optional(),
    agent: GitCheckpointAgentSchema.optional(),
  })
  .strict()
  .optional();

const GitFlowSchema = z
  .object({
    enabled: z.boolean(),
    policy: z.literal('.juno_task/config/git-flow.json'),
  })
  .strict()
  .optional();

const ControllerWorkspaceSchema = z
  .object({
    mode: z.literal('metadata-only'),
    policy: z.literal('.juno_task/config/metadata-controller.json'),
  })
  .strict()
  .optional();

const SafeRelativeAssetRootSchema = z.string().min(1).refine((value) => {
  if (path.isAbsolute(value)) return false;
  const normalized = path.normalize(value);
  return normalized !== '..' && !normalized.startsWith(`..${path.sep}`);
}, 'must be a relative path contained by the controller');

const RoleHooksSchema = z.object({
  controller: HooksSchema,
  product: HooksSchema,
}).strict().optional();

const EnvironmentBindingSchema = z.object({
  source: z.string().min(1).refine(path.isAbsolute, 'must be an explicit absolute path'),
  authorized: z.literal(true),
}).strict().optional();

const HeadlessUiSchema = z
  .object({
    turnCostDisplayThresholdUsd: z
      .number()
      .finite()
      .nonnegative()
      .default(0.5)
      .describe('Show authoritative per-turn cost above this USD threshold'),
  })
  .strict();

const AgentProfileSchema = z
  .object({
    version: z.literal(1),
    promptAssetRoot: SafeRelativeAssetRootSchema,
    roleHooks: RoleHooksSchema,
    environmentBinding: EnvironmentBindingSchema,
  })
  .strict()
  .optional();

/** Field ownership used by metadata-controller validation and v2 migration. */
export const METADATA_CONTROLLER_CONFIG_FIELD_OWNERSHIP = {
  controllerSafe: [
    'configVersion', 'agentProfile', 'controllerWorkspace', 'gitCheckpoint',
    'defaultSubagent', 'defaultBackend', 'defaultMaxIterations', 'defaultModel',
    'defaultModels', 'workflowModels', 'mainTask', 'logLevel', 'logFile', 'verbose',
    'quiet', 'mcpTimeout', 'mcpRetries', 'mcpServerPath', 'mcpServerName',
    'hookCommandTimeout', 'onHourlyLimit', 'interactive', 'headlessMode',
    'kanbanRegistry', 'promptMacros', 'modelShortcuts', 'headlessUi',
  ],
  productOnly: ['workingDirectory', 'sessionDirectory', 'gitFlow', 'autoDependencyUpdate', 'hooks', 'skipHooks'],
  secret: ['envFilePath', 'envFileCopied'],
  retired: ['lifecycle'],
} as const;

export function selectAgentProfileHooks(
  profile: JunoTaskConfig['agentProfile'] | undefined,
  role: 'controller' | 'controller-retired' | 'task' | 'integration-owner' | 'unregistered',
): JunoTaskConfig['hooks'] | undefined {
  if (!profile?.roleHooks) return undefined;
  if (role === 'controller') return profile.roleHooks.controller;
  if (role === 'task' || role === 'integration-owner') return profile.roleHooks.product;
  return undefined;
}

function validateMetadataControllerSource(config: Partial<JunoTaskConfig>): void {
  const raw = config as Record<string, unknown>;
  const workspace = raw.controllerWorkspace as Record<string, unknown> | undefined;
  if (workspace?.mode !== 'metadata-only') return;
  for (const field of METADATA_CONTROLLER_CONFIG_FIELD_OWNERSHIP.productOnly) {
    if (Object.prototype.hasOwnProperty.call(raw, field)) {
      throw new Error(`Metadata-controller configuration field ${field} is product-only and cannot be activated in the controller`);
    }
  }
  for (const field of METADATA_CONTROLLER_CONFIG_FIELD_OWNERSHIP.retired) {
    if (Object.prototype.hasOwnProperty.call(raw, field)) {
      throw new Error(`Metadata-controller configuration field ${field} is retired`);
    }
  }
}

const KanbanProjectAliasSchema = z
  .string()
  .regex(/^[a-z0-9][a-z0-9_-]{0,63}$/, 'must be a lowercase project alias');

const KanbanRegistrySchema = z
  .object({
    enabled: z.boolean(),
    allowedProjects: z.array(KanbanProjectAliasSchema).refine(
      (aliases) => new Set(aliases).size === aliases.length,
      'must not contain duplicate project aliases',
    ),
  })
  .strict()
  .optional();

/**
 * Zod schema for validating JunoTaskConfig
 * Provides runtime validation with detailed error messages
 */
export const JunoTaskConfigSchema = z
  .object({
    configVersion: z.number().int().min(1).optional().describe('Persisted project config generation'),

    // Core settings
    defaultSubagent: SubagentTypeSchema.describe('Default subagent to use for task execution'),

    defaultBackend: BackendTypeSchema.describe('Default backend to use for task execution'),

    defaultMaxIterations: z
      .number()
      .int()
      .min(1)
      .max(1000)
      .describe('Default maximum number of iterations for task execution'),

    defaultModel: z.string().optional().describe('Default model to use for the subagent'),

    defaultModels: z
      .record(SubagentTypeSchema, z.string())
      .optional()
      .describe('Optional per-subagent default model overrides'),

    workflowModels: z
      .array(z.string().min(1).refine((value) => value === value.trim(), 'workflow model selectors must be trimmed'))
      .refine((values) => new Set(values).size === values.length, 'workflow model selectors must be unique')
      .optional()
      .describe('Exact provider/model selectors approved for explicit managed workflow use'),

    headlessUi: HeadlessUiSchema.default({ turnCostDisplayThresholdUsd: 0.5 }),

    // Project metadata
    mainTask: z.string().optional().describe('Main task objective for the project'),

    // Logging settings
    logLevel: LogLevelSchema.describe('Logging level for the application'),

    logFile: z.string().optional().describe('Path to log file (optional)'),

    verbose: z.preprocess(
      (val) => {
        if (val === true) return 1;
        if (val === false) return 0;
        if (typeof val === 'string') {
          const lower = val.toLowerCase().trim();
          if (lower === 'true' || lower === 'yes') return 1;
          if (lower === 'false' || lower === 'no') return 0;
        }
        return val;
      },
      z.number().int().min(0).max(2),
    ).describe('Verbosity level: 0=quiet, 1=normal+helping texts (default), 2=debug+hooks'),

    quiet: z.boolean().describe('Enable quiet mode (minimal output)'),

    // MCP settings
    mcpTimeout: z
      .number()
      .int()
      .min(1000)
      // Allow very large timeouts to satisfy real-world workflows and user tests
      // User feedback requires accepting values like 6,000,000 ms (100 minutes)
      .max(86400000) // up to 24 hours
      .describe('MCP server timeout in milliseconds'),

    mcpRetries: z.number().int().min(0).max(10).describe('Number of retries for MCP operations'),

    mcpServerPath: z
      .string()
      .optional()
      .describe('Path to MCP server executable (auto-discovered if not specified)'),

    mcpServerName: z
      .string()
      .optional()
      .describe('Named MCP server to connect to'),

    // Hook settings
    hookCommandTimeout: z
      .number()
      .int()
      .min(1000)
      .max(3600000) // up to 1 hour
      .optional()
      .describe(
        'Timeout for individual hook commands in milliseconds (default: 300000 = 5 minutes)',
      ),

    autoDependencyUpdate: z
      .boolean()
      .optional()
      .describe(
        'Opt-out flag for automatic START_RUN dependency updates. Set false to prevent install_requirements.sh hook migration/injection.',
      ),

    // Quota/hourly limit settings
    onHourlyLimit: OnHourlyLimitSchema.describe(
      'Behavior when Claude hourly quota limit is reached: "wait" to sleep until reset, "raise" to exit immediately',
    ),

    // TUI settings
    interactive: z.boolean().describe('Enable interactive mode'),

    headlessMode: z.boolean().describe('Enable headless mode (no TUI)'),

    // Paths
    workingDirectory: z.string().describe('Working directory for task execution'),

    sessionDirectory: z.string().describe('Directory for storing session data'),

    // Controller-owned Git checkpoint configuration
    gitCheckpoint: GitCheckpointSchema.describe(
      'Allowlisted controller paths and optional read-only commit-planning agent settings',
    ),

    gitFlow: GitFlowSchema.describe(
      'Enablement and canonical policy pointer for the Python-owned Git-flow engine',
    ),

    controllerWorkspace: ControllerWorkspaceSchema.describe(
      'Canonical metadata-only controller ownership and boundary policy pointer',
    ),

    agentProfile: AgentProfileSchema.describe(
      'Versioned metadata-controller agent profile and explicit prompt asset root',
    ),

    kanbanRegistry: KanbanRegistrySchema.describe(
      'Disabled-by-default cross-project Kanban routing and explicit alias allowlist',
    ),

    // Project environment bootstrap
    envFilePath: z
      .string()
      .optional()
      .describe(
        'Path to the project env file loaded before execution (relative to project root or absolute)',
      ),

    envFileCopied: z
      .boolean()
      .optional()
      .describe('Tracks whether configured env file has been initialized from .env.yylo'),

    // Hooks configuration
    hooks: HooksSchema.describe(
      'Hook system configuration for executing commands at specific lifecycle events',
    ),

    // Skip hooks execution
    skipHooks: z.boolean().optional().describe('Skip execution of all lifecycle hooks when true'),

    // Prompt macro dictionary expansion
    promptMacros: PromptMacrosSchema.describe(
      'Prompt macro dictionary expansion config (@@key). Use global/local dictionaries, local overrides global, and maxDepth controls recursive expansion safety.',
    ),

    // Model shortcuts configuration
    modelShortcuts: ModelShortcutsSchema
      .optional()
      .describe('Per-subagent model shortcuts. Project shortcuts merge with CLI defaults and take precedence. Keys start with ":" (e.g., ":fav").'),
  })
  .strict();

/**
 * Default configuration values
 * These are used as fallbacks when no other configuration is provided
 */
const DEFAULT_PROMPT_MACROS: PromptMacroConfig = {
  enabled: true,
  order: 'before_command_substitution',
  maxDepth: 10,
  global: {},
  local: {},
};

/** User-visible defaults persisted into fresh and upgraded project configs. */
export function createPersistedProjectConfigDefaults(baseDir: string): Record<string, unknown> {
  return {
    configVersion: PROJECT_CONFIG_VERSION,
    defaultSubagent: 'claude',
    defaultBackend: 'shell',
    defaultMaxIterations: 1,
    defaultModels: { ...SUBAGENT_DEFAULT_MODELS },
    workflowModels: [],
    headlessUi: { turnCostDisplayThresholdUsd: 0.5 },
    logLevel: 'info',
    verbose: 1,
    quiet: false,
    mcpTimeout: 43200000,
    mcpRetries: 3,
    onHourlyLimit: 'raise',
    interactive: true,
    headlessMode: false,
    workingDirectory: baseDir,
    sessionDirectory: path.join(baseDir, '.juno_task'),
    kanbanRegistry: { enabled: false, allowedProjects: [] },
    gitCheckpoint: { include: [...DEFAULT_GIT_CHECKPOINT_INCLUDE] },
    envFilePath: '.env.yylo',
    envFileCopied: false,
    hooks: getDefaultHooks(),
    autoDependencyUpdate: true,
    promptMacros: { ...DEFAULT_PROMPT_MACROS, global: {}, local: {} },
    modelShortcuts: {},
  };
}

export const DEFAULT_CONFIG = createPersistedProjectConfigDefaults(
  process.cwd(),
) as unknown as JunoTaskConfig;

/**
 * Global configuration file names to search for
 * Searched in order of preference (after project-specific config)
 */
const GLOBAL_CONFIG_FILE_NAMES = [
  'yylo.config.json',
  'yylo.config.js',
  '.yylorc.json',
  '.yylorc.js',
  // Read-only discovery compatibility. Canonical filenames always win; legacy
  // JavaScript entries retain the existing explicit unsupported-format error.
  'juno-code.config.json',
  'juno-code.config.js',
  '.juno-coderc.json',
  '.juno-coderc.js',
  'package.json', // Looks for canonical 'yylo', then legacy 'junoCode'
] as const;

/**
 * Project-specific configuration file (highest precedence for project settings)
 */
const PROJECT_CONFIG_FILE = '.juno_task/config.json';

/**
 * Default project env file created and loaded on startup
 */
const DEFAULT_PROJECT_ENV_FILE = '.env.yylo';
const LEGACY_PROJECT_ENV_FILE = '.env.juno'; // bounded 0.1 RC migration input

/**
 * Supported configuration file formats
 */
type ConfigFileFormat = 'json' | 'yaml' | 'toml' | 'js';

/**
 * Configuration source types for precedence handling
 * Precedence order: cli > env > projectFile > file > defaults
 */
type ConfigSource = 'defaults' | 'file' | 'projectFile' | 'env' | 'cli';

function normalizePromptMacrosConfig(
  value: JunoTaskConfig['promptMacros'] | undefined,
): PromptMacroConfig {
  return {
    enabled: value?.enabled ?? DEFAULT_PROMPT_MACROS.enabled,
    order: value?.order ?? DEFAULT_PROMPT_MACROS.order,
    maxDepth: value?.maxDepth ?? DEFAULT_PROMPT_MACROS.maxDepth,
    global: { ...(value?.global ?? {}) },
    local: { ...(value?.local ?? {}) },
  };
}

function mergePromptMacrosConfig(
  base: JunoTaskConfig['promptMacros'] | undefined,
  override: JunoTaskConfig['promptMacros'] | undefined,
): PromptMacroConfig {
  const baseNormalized = normalizePromptMacrosConfig(base);
  const overrideNormalized = normalizePromptMacrosConfig(override);

  return {
    enabled: override?.enabled ?? baseNormalized.enabled,
    order: override?.order ?? baseNormalized.order,
    maxDepth: override?.maxDepth ?? baseNormalized.maxDepth,
    global: {
      ...baseNormalized.global,
      ...overrideNormalized.global,
    },
    local: {
      ...baseNormalized.local,
      ...overrideNormalized.local,
    },
  };
}

export function getPromptMacroDictionary(config: Pick<JunoTaskConfig, 'promptMacros'>): Record<string, string> {
  const normalized = normalizePromptMacrosConfig(config.promptMacros);
  return {
    ...normalized.global,
    ...normalized.local,
  };
}

/**
 * Utility function to resolve paths (relative to absolute)
 *
 * @param inputPath - The path to resolve
 * @param basePath - Base path for relative resolution (defaults to cwd)
 * @returns Absolute path
 */
function resolvePath(inputPath: string, basePath: string = process.cwd()): string {
  if (path.isAbsolute(inputPath)) {
    return inputPath;
  }
  return path.resolve(basePath, inputPath);
}

/**
 * Utility function to parse environment variables
 * Handles type conversion for boolean and number values
 *
 * @param value - Environment variable value
 * @returns Parsed value with appropriate type
 */
function parseEnvValue(value: string): string | number | boolean {
  // Handle empty string
  if (value === '') return value;

  // Handle boolean values
  if (value.toLowerCase() === 'true') return true;
  if (value.toLowerCase() === 'false') return false;

  // Handle numeric values
  const numValue = Number(value);
  if (!isNaN(numValue) && isFinite(numValue)) {
    return numValue;
  }

  // Return as string
  return value;
}

/**
 * Load configuration from environment variables
 * Maps YYLO_* environment variables to config properties
 *
 * @returns Partial configuration from environment variables
 */
function loadConfigFromEnv(): Partial<JunoTaskConfig> {
  migrateLegacyEnvironment();
  const config: Partial<JunoTaskConfig> = {};

  for (const [envVar, configKey] of Object.entries(ENV_VAR_MAPPING) as [string, string][]) {
    const value = process.env[envVar];
    if (value !== undefined) {
      let parsed = parseEnvValue(value);
      // Normalize verbose: convert boolean to numeric level (0-2)
      if (configKey === 'verbose') {
        if (parsed === true) parsed = 1;
        else if (parsed === false) parsed = 0;
      }
      (config as any)[configKey] = parsed;
    }
  }

  return config;
}

/**
 * Load configuration from a JSON file
 *
 * @param filePath - Path to the JSON configuration file
 * @returns Parsed configuration object
 */
async function loadJsonConfig(filePath: string): Promise<Partial<JunoTaskConfig>> {
  try {
    const content = await fsPromises.readFile(filePath, 'utf-8');
    return JSON.parse(content);
  } catch (error) {
    throw new Error(`Failed to load JSON config from ${filePath}: ${error}`);
  }
}

function isPromptMacroObject(value: unknown): value is { path?: unknown; text?: unknown } {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function nonEmptyString(value: unknown): value is string {
  return typeof value === 'string' && value.trim().length > 0;
}

async function resolvePromptMacroValue(
  keyPath: string,
  value: unknown,
  baseDir: string,
  confineToBaseDir = false,
): Promise<string> {
  if (typeof value === 'string') {
    return value;
  }

  if (!isPromptMacroObject(value)) {
    throw new Error(`${keyPath} must be a string or an object with exactly one of { path, text }`);
  }

  const pathValue = value.path;
  const textValue = value.text;
  const hasPath = nonEmptyString(pathValue);
  const hasText = nonEmptyString(textValue);

  if (hasPath === hasText) {
    throw new Error(`${keyPath} must define exactly one non-empty field: path or text`);
  }

  if (hasText) {
    return textValue;
  }

  const macroPath = pathValue as string;
  const resolvedPath = path.isAbsolute(macroPath) ? macroPath : path.resolve(baseDir, macroPath);
  try {
    if (confineToBaseDir) {
      const [realBase, realAsset] = await Promise.all([
        fsPromises.realpath(baseDir),
        fsPromises.realpath(resolvedPath),
      ]);
      if (realAsset !== realBase && !realAsset.startsWith(`${realBase}${path.sep}`)) {
        throw new Error('asset path escapes the configured promptAssetRoot');
      }
    }
    return await fsPromises.readFile(resolvedPath, 'utf-8');
  } catch (error) {
    throw new Error(`${keyPath} failed to read path ${resolvedPath}: ${error}`);
  }
}

async function resolvePromptMacroDictionary(
  dictionary: unknown,
  baseDir: string,
  keyPath: string,
  confineToBaseDir = false,
): Promise<Record<string, string> | undefined> {
  if (dictionary === undefined) {
    return undefined;
  }
  if (!isPromptMacroObject(dictionary)) {
    throw new Error(`${keyPath} must be an object`);
  }

  const resolved: Record<string, string> = {};
  for (const [key, value] of Object.entries(dictionary as RawPromptMacroDictionary)) {
    resolved[key] = await resolvePromptMacroValue(`${keyPath}.${key}`, value, baseDir, confineToBaseDir);
  }
  return resolved;
}

async function resolvePromptMacroFileEntries(
  config: Partial<JunoTaskConfig>,
  baseDir: string,
): Promise<Partial<JunoTaskConfig>> {
  validateMetadataControllerSource(config);
  const profile = config.agentProfile;
  const promptAssetBaseDir = profile
    ? path.resolve(baseDir, profile.promptAssetRoot)
    : baseDir;
  const rawPromptMacros = config.promptMacros as unknown as
    | (Omit<PromptMacroConfig, 'global' | 'local'> & {
      global?: unknown;
      local?: unknown;
    })
    | undefined;

  if (!rawPromptMacros) {
    return config;
  }

  return {
    ...config,
    promptMacros: {
      ...rawPromptMacros,
      global: await resolvePromptMacroDictionary(rawPromptMacros.global, promptAssetBaseDir, 'promptMacros.global', Boolean(profile)),
      local: await resolvePromptMacroDictionary(rawPromptMacros.local, promptAssetBaseDir, 'promptMacros.local', Boolean(profile)),
    } as PromptMacroConfig,
  };
}

/**
 * Load configuration from a YAML file
 *
 * @param filePath - Path to the YAML configuration file
 * @returns Parsed configuration object
 */
async function loadYamlConfig(filePath: string): Promise<Partial<JunoTaskConfig>> {
  try {
    const content = await fsPromises.readFile(filePath, 'utf-8');
    const parsed = yaml.load(content);
    return parsed as Partial<JunoTaskConfig>;
  } catch (error) {
    throw new Error(`Failed to load YAML config from ${filePath}: ${error}`);
  }
}

/**
 * Load configuration from package.json
 * Looks for configuration in the canonical 'yylo' field and then the legacy
 * 'junoCode' compatibility field
 *
 * @param filePath - Path to package.json
 * @returns Parsed configuration object
 */
async function loadPackageJsonConfig(filePath: string): Promise<Partial<JunoTaskConfig>> {
  try {
    const content = await fsPromises.readFile(filePath, 'utf-8');
    const packageJson = JSON.parse(content);
    return packageJson.yylo ?? packageJson.junoCode ?? {};
  } catch (error) {
    throw new Error(`Failed to load package.json config from ${filePath}: ${error}`);
  }
}

/**
 * Determine configuration file format based on file extension
 *
 * @param filePath - Path to the configuration file
 * @returns Configuration file format
 */
function getConfigFileFormat(filePath: string): ConfigFileFormat {
  const ext = path.extname(filePath).toLowerCase();

  switch (ext) {
    case '.json':
      return 'json';
    case '.yaml':
    case '.yml':
      return 'yaml';
    case '.toml':
      return 'toml';
    case '.js':
    case '.mjs':
      return 'js';
    default:
      // For files like .yylorc (no extension), assume JSON
      return 'json';
  }
}

/**
 * Load configuration from a file
 * Automatically detects file format and uses appropriate parser
 *
 * @param filePath - Path to the configuration file
 * @returns Parsed configuration object
 */
async function loadConfigFromFile(
  filePath: string,
  promptMacroPathBaseDir: string = process.cwd(),
): Promise<Partial<JunoTaskConfig>> {
  const format = getConfigFileFormat(filePath);
  const resolvedPath = resolvePath(filePath);
  const macroPathBaseDir = resolvePath(promptMacroPathBaseDir);

  // Check if file exists
  try {
    await fsPromises.access(resolvedPath, nodeFs.constants.R_OK);
  } catch {
    throw new Error(`Configuration file not readable: ${resolvedPath}`);
  }

  switch (format) {
    case 'json':
      if (path.basename(filePath) === 'package.json') {
        return resolvePromptMacroFileEntries(await loadPackageJsonConfig(resolvedPath), macroPathBaseDir);
      }
      return resolvePromptMacroFileEntries(await loadJsonConfig(resolvedPath), macroPathBaseDir);

    case 'yaml':
      return resolvePromptMacroFileEntries(await loadYamlConfig(resolvedPath), macroPathBaseDir);

    case 'toml':
      // TOML support would require additional dependency
      throw new Error('TOML configuration files are not yet supported');

    case 'js':
      // JavaScript config files would require dynamic import
      throw new Error('JavaScript configuration files are not yet supported');

    default:
      throw new Error(`Unsupported configuration file format: ${format}`);
  }
}

/**
 * Find project-specific configuration file
 * Looks for .juno_task/config.json in the specified directory
 *
 * @param searchDir - Directory to search for project configuration file
 * @returns Path to found project config file, or null if none found
 */
async function findProjectConfigFile(searchDir: string = process.cwd()): Promise<string | null> {
  const filePath = path.join(searchDir, PROJECT_CONFIG_FILE);

  try {
    await fsPromises.access(filePath, nodeFs.constants.R_OK);
    return filePath;
  } catch {
    // File doesn't exist or isn't readable
    return null;
  }
}

/**
 * Find global configuration file in the specified directory
 * Searches for global config files in order of preference
 *
 * @param searchDir - Directory to search for global configuration files
 * @returns Path to found global config file, or null if none found
 */
async function findGlobalConfigFile(searchDir: string = process.cwd()): Promise<string | null> {
  for (const fileName of GLOBAL_CONFIG_FILE_NAMES) {
    const filePath = path.join(searchDir, fileName);

    try {
      await fsPromises.access(filePath, nodeFs.constants.R_OK);
      return filePath;
    } catch {
      // File doesn't exist or isn't readable, continue searching
      continue;
    }
  }

  return null;
}

/**
 * ConfigLoader class for multi-source configuration loading
 *
 * Implements configuration precedence: CLI args > Environment Variables > Project Config > Global Config Files > Profile > Defaults
 */
export class ConfigLoader {
  private configSources: Map<ConfigSource, Partial<JunoTaskConfig>> = new Map();
  private projectConfigDir: string;

  /**
   * Create a new ConfigLoader instance
   *
   * @param baseDir - Base directory for relative path resolution
   */
  constructor(private baseDir: string = process.cwd(), projectConfigDir?: string) {
    this.projectConfigDir = projectConfigDir ?? baseDir;
    // Initialize with defaults
    this.configSources.set('defaults', DEFAULT_CONFIG);
  }

  /**
   * Load configuration from environment variables
   *
   * @returns This ConfigLoader instance for method chaining
   */
  fromEnvironment(): this {
    const envConfig = loadConfigFromEnv();
    this.configSources.set('env', envConfig);
    return this;
  }

  /**
   * Load configuration from a specific file
   *
   * @param filePath - Path to configuration file
   * @returns This ConfigLoader instance for method chaining
   */
  async fromFile(filePath: string): Promise<this> {
    try {
      const fileConfig = await loadConfigFromFile(filePath, this.baseDir);
      this.configSources.set('file', fileConfig);
    } catch (error) {
      throw new Error(`Failed to load configuration file: ${error}`);
    }
    return this;
  }

  /**
   * Load configuration from project-specific config file
   * Loads from .juno_task/config.json with highest precedence for project settings
   *
   * @returns This ConfigLoader instance for method chaining
   */
  async fromProjectConfig(): Promise<this> {
    try {
      const projectConfigFile = await findProjectConfigFile(this.projectConfigDir);
      if (projectConfigFile) {
        const fileConfig = await loadConfigFromFile(projectConfigFile, this.projectConfigDir);
        this.configSources.set('projectFile', fileConfig);
      }
    } catch (error) {
      throw new Error(`Failed to load project configuration file: ${error}`);
    }
    return this;
  }

  /**
   * Automatically discover and load configuration files
   * Searches for both project-specific and global config files in the base directory
   * Project-specific config (.juno_task/config.json) takes precedence over global configs
   *
   * @returns This ConfigLoader instance for method chaining
   */
  async autoDiscoverFile(): Promise<this> {
    // First, try to load project-specific config
    const projectConfigFile = await findProjectConfigFile(this.projectConfigDir);
    if (projectConfigFile) {
      const fileConfig = await loadConfigFromFile(projectConfigFile, this.projectConfigDir);
      this.configSources.set('projectFile', fileConfig);
    }

    // Then, try to load global config file
    const globalConfigFile = await findGlobalConfigFile(this.baseDir);
    if (globalConfigFile) {
      const fileConfig = await loadConfigFromFile(globalConfigFile, this.baseDir);
      this.configSources.set('file', fileConfig);
    }

    return this;
  }

  /**
   * Load configuration from CLI arguments
   *
   * @param cliConfig - Configuration object from CLI argument parsing
   * @returns This ConfigLoader instance for method chaining
   */
  fromCli(cliConfig: Partial<JunoTaskConfig>): this {
    this.configSources.set('cli', cliConfig);
    return this;
  }

  /**
   * Merge all configuration sources according to precedence
   * CLI args > Environment Variables > Project Config > Global Config Files > Defaults
   *
   * @returns Merged configuration object
   */
  merge(): JunoTaskConfig {
    // Start with defaults to ensure all required properties are present
    const mergedConfig = { ...DEFAULT_CONFIG };

    // Apply sources in order of precedence (lowest to highest)
    const sourcePrecedence: ConfigSource[] = ['file', 'projectFile', 'env', 'cli'];

    for (const source of sourcePrecedence) {
      const sourceConfig = this.configSources.get(source);
      if (sourceConfig) {
        const nextPromptMacros = mergePromptMacrosConfig(
          mergedConfig.promptMacros,
          sourceConfig.promptMacros,
        );
        Object.assign(mergedConfig, sourceConfig);
        mergedConfig.promptMacros = nextPromptMacros;
      }
    }

    // Resolve paths to absolute paths
    if (mergedConfig.workingDirectory) {
      mergedConfig.workingDirectory = resolvePath(mergedConfig.workingDirectory, this.baseDir);
    }

    if (mergedConfig.sessionDirectory) {
      mergedConfig.sessionDirectory = resolvePath(mergedConfig.sessionDirectory, this.baseDir);
    }

    if (mergedConfig.logFile) {
      mergedConfig.logFile = resolvePath(mergedConfig.logFile, this.baseDir);
    }

    if (mergedConfig.mcpServerPath) {
      mergedConfig.mcpServerPath = resolvePath(mergedConfig.mcpServerPath, this.baseDir);
    }

    return mergedConfig;
  }

  /**
   * Load and merge configuration from all sources
   * Convenience method that performs auto-discovery and returns validated config
   *
   * @param cliConfig - Optional CLI configuration
   * @returns Promise resolving to validated configuration
   */
  async loadAll(cliConfig?: Partial<JunoTaskConfig>): Promise<JunoTaskConfig> {
    // Load from environment
    this.fromEnvironment();

    // Auto-discover configuration file
    await this.autoDiscoverFile();

    // Add CLI config if provided
    if (cliConfig) {
      this.fromCli(cliConfig);
    }

    // Merge and return
    return this.merge();
  }
}

/**
 * Validate configuration object against schema
 *
 * @param config - Configuration object to validate
 * @returns Validated configuration object
 * @throws Error if validation fails
 */
export function validateConfig(config: unknown): JunoTaskConfig {
  try {
    const parsed = JunoTaskConfigSchema.parse(config);
    return parsed as JunoTaskConfig;
  } catch (error) {
    if (error instanceof z.ZodError) {
      const errorMessages = error.errors
        .map((err) => `${err.path.join('.') || '<root>'}: ${err.message}`)
        .join('; ');

      const hasPromptMacroSnakeCaseHint = error.errors.some((err) => {
        const typedErr = err as z.ZodIssue & { keys?: string[] };
        if (typedErr.path.join('.') === 'prompt_macros') return true;
        if (typedErr.code === 'unrecognized_keys' && Array.isArray(typedErr.keys)) {
          return typedErr.keys.some((key) =>
            ['prompt_macros', 'max_depth', 'before_command_substitution'].includes(key),
          );
        }
        return false;
      });

      const configRecord = config && typeof config === 'object' && !Array.isArray(config)
        ? config as Record<string, unknown>
        : undefined;
      const controllerWorkspace = configRecord?.controllerWorkspace;
      const hasRetiredControllerConfig = Object.prototype.hasOwnProperty.call(configRecord ?? {}, 'lifecycle') || (
        controllerWorkspace !== undefined && (
          typeof controllerWorkspace !== 'object' ||
          controllerWorkspace === null ||
          Array.isArray(controllerWorkspace) ||
          (controllerWorkspace as Record<string, unknown>).mode !== 'metadata-only' ||
          (controllerWorkspace as Record<string, unknown>).policy !== '.juno_task/config/metadata-controller.json'
        )
      );

      const hint = hasPromptMacroSnakeCaseHint
        ? ' Hint: use config.promptMacros with keys { enabled, order, maxDepth, global, local }.'
        : hasRetiredControllerConfig
          ? ' Migration required: persisted lifecycle and sparse controllerWorkspace configuration were removed; prepare a metadata-only controller using { mode: "metadata-only", policy: ".juno_task/config/metadata-controller.json" }.'
        : '';

      throw new Error(`Configuration validation failed: ${errorMessages}${hint}`);
    }
    throw error;
  }
}

/**
 * Parse dotenv-style content into key/value pairs.
 * Supports comments (#), `export KEY=VALUE`, and quoted values.
 */
function parseEnvFileContent(content: string): Record<string, string> {
  const envVars: Record<string, string> = {};
  const lines = content.split(/\r?\n/);

  for (const rawLine of lines) {
    const trimmedLine = rawLine.trim();
    if (!trimmedLine || trimmedLine.startsWith('#')) {
      continue;
    }

    const line = trimmedLine.startsWith('export ') ? trimmedLine.slice(7).trim() : trimmedLine;
    const separatorIndex = line.indexOf('=');
    if (separatorIndex === -1) {
      continue;
    }

    const key = line.slice(0, separatorIndex).trim();
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) {
      continue;
    }

    let value = line.slice(separatorIndex + 1).trim();

    // Handle quoted values
    if (
      ((value.startsWith('"') && value.endsWith('"')) ||
        (value.startsWith("'") && value.endsWith("'"))) &&
      value.length >= 2
    ) {
      const quote = value[0];
      value = value.slice(1, -1);
      if (quote === '"') {
        value = value
          .replace(/\\n/g, '\n')
          .replace(/\\r/g, '\r')
          .replace(/\\t/g, '\t')
          .replace(/\\"/g, '"')
          .replace(/\\\\/g, '\\');
      }
    } else {
      // Strip inline comments from unquoted values (`KEY=value # comment`)
      const inlineCommentIndex = value.indexOf(' #');
      if (inlineCommentIndex >= 0) {
        value = value.slice(0, inlineCommentIndex).trimEnd();
      }
    }

    envVars[key] = value;
  }

  return envVars;
}

/**
 * Load environment variables from a dotenv-style file into process.env.
 * Variables from the file override existing process.env values.
 */
async function loadEnvFileIntoProcess(envFilePath: string): Promise<void> {
  try {
    const content = await fsPromises.readFile(envFilePath, 'utf-8');
    const parsed = parseEnvFileContent(content);

    for (const [key, value] of Object.entries(parsed)) {
      process.env[key] = value;
    }
  } catch (error) {
    console.warn(`Warning: Failed to load env file ${envFilePath}: ${error}`);
  }
}

async function loadAuthorizedProfileEnvironment(binding: { source: string; authorized: true }): Promise<void> {
  const source = path.resolve(binding.source);
  let handle: fsPromises.FileHandle | undefined;
  try {
    handle = await fsPromises.open(source, nodeFs.constants.O_RDONLY | nodeFs.constants.O_NOFOLLOW);
    const metadata = await handle.stat();
    if (!metadata.isFile()) {
      throw new Error('source is not a regular file');
    }
    if ((metadata.mode & 0o077) !== 0) {
      throw new Error('source must have mode 0600');
    }
    const parsed = parseEnvFileContent(await handle.readFile('utf8'));
    for (const [key, value] of Object.entries(parsed)) process.env[key] = value;
  } catch (error) {
    throw new Error(`Authorized controller environment source is missing, unsafe, or unreadable: ${source}: ${error}`);
  } finally {
    await handle?.close().catch(() => undefined);
  }
}

async function readMetadataAgentProfile(baseDir: string): Promise<
  { metadata: true; profile: JunoTaskConfig['agentProfile'] } | undefined
> {
  const configPath = path.join(baseDir, PROJECT_CONFIG_FILE);
  if (!(await fs.pathExists(configPath))) return undefined;
  const raw = await fs.readJson(configPath) as Record<string, unknown>;
  const workspace = raw.controllerWorkspace as Record<string, unknown> | undefined;
  if (workspace?.mode !== 'metadata-only') return undefined;
  validateMetadataControllerSource(raw as Partial<JunoTaskConfig>);
  const parsed = AgentProfileSchema.parse(raw.agentProfile);
  return { metadata: true, profile: parsed as JunoTaskConfig['agentProfile'] };
}

/**
 * Ensure project env files exist and load them before config/env precedence is evaluated.
 *
 * Behavior:
 * - Always ensure `.env.yylo` exists in project root.
 * - Read `.juno_task/config.json` for optional `envFilePath` and `envFileCopied`.
 * - If a custom env path is configured and not initialized yet, copy `.env.yylo` once.
 * - Load `.env.yylo`, then custom env file (if different) so custom values can override defaults.
 */
async function ensureAndLoadProjectEnv(
  baseDir: string,
  allowWritesOverride?: boolean,
): Promise<void> {
  const configPath = path.join(baseDir, PROJECT_CONFIG_FILE);
  const defaultEnvPath = resolvePath(DEFAULT_PROJECT_ENV_FILE, baseDir);

  const allowProjectWrites =
    allowWritesOverride ?? process.env.YYLO_PROJECT_BOOTSTRAP_WRITES !== '0';

  // Bounded 0.1 RC migration: preserve the legacy file byte-for-byte while
  // seeding the canonical name. Re-running is idempotent and rollback-safe.
  const legacyEnvPath = resolvePath(LEGACY_PROJECT_ENV_FILE, baseDir);
  if (allowProjectWrites && !(await fs.pathExists(defaultEnvPath)) && await fs.pathExists(legacyEnvPath)) {
    await fsPromises.copyFile(legacyEnvPath, defaultEnvPath);
  }

  // Agent startup in task/candidate worktrees is read-only. Controller startup
  // and direct loadConfig callers retain normal initialization by default.
  if (allowProjectWrites) {
    await fs.ensureFile(defaultEnvPath);
  }

  let existingConfig: Record<string, unknown> | null = null;

  if (await fs.pathExists(configPath)) {
    try {
      existingConfig = await fs.readJson(configPath);
    } catch (error) {
      console.warn(`Warning: Failed to read ${configPath} for env bootstrap: ${error}`);
    }
  }

  const configuredEnvPathRaw =
    existingConfig && typeof existingConfig.envFilePath === 'string' && existingConfig.envFilePath
      ? existingConfig.envFilePath
      : DEFAULT_PROJECT_ENV_FILE;

  const configuredEnvPath = resolvePath(configuredEnvPathRaw, baseDir);

  let envFileCopied =
    existingConfig && typeof existingConfig.envFileCopied === 'boolean'
      ? existingConfig.envFileCopied
      : false;

  let needsConfigUpdate = false;

  if (configuredEnvPath !== defaultEnvPath) {
    const configuredExists = await fs.pathExists(configuredEnvPath);

    if (!configuredExists && allowProjectWrites) {
      await fs.ensureDir(path.dirname(configuredEnvPath));
      if (!envFileCopied) {
        await fsPromises.copyFile(defaultEnvPath, configuredEnvPath);
      } else {
        await fs.ensureFile(configuredEnvPath);
      }
    }

    if (allowProjectWrites && !envFileCopied) {
      envFileCopied = true;
      needsConfigUpdate = true;
    }
  }

  if (
    allowProjectWrites &&
    existingConfig &&
    (needsConfigUpdate ||
      typeof existingConfig.envFilePath !== 'string' ||
      typeof existingConfig.envFileCopied !== 'boolean')
  ) {
    const lockPath = path.join(path.dirname(configPath), '.config.json.migration.lock');
    const lock = await acquireProjectConfigMigrationLock(lockPath);
    if (lock) {
      try {
        const originalConfigBytes = await fs.readFile(configPath);
        const currentConfig = JSON.parse(originalConfigBytes.toString('utf8')) as Record<string, unknown>;
        const updatedConfig = {
          ...currentConfig,
          envFilePath: configuredEnvPathRaw,
          envFileCopied,
        };
        await writeProjectConfigAtomic(configPath, updatedConfig, fs.rename, originalConfigBytes);
      } finally {
        await lock.close().catch(() => undefined);
        await fs.remove(lockPath).catch(() => undefined);
      }
    }
  }

  // Load existing env files in both modes; read-only startup merely refuses to
  // manufacture or migrate them. A legacy-only project without an explicit
  // custom path remains readable, while the canonical file always wins.
  const defaultEnvExists = await fs.pathExists(defaultEnvPath);
  let loadedPrimaryPath: string | null = null;
  if (defaultEnvExists) {
    await loadEnvFileIntoProcess(defaultEnvPath);
    loadedPrimaryPath = defaultEnvPath;
  } else if (configuredEnvPathRaw === DEFAULT_PROJECT_ENV_FILE && await fs.pathExists(legacyEnvPath)) {
    await loadEnvFileIntoProcess(legacyEnvPath);
    loadedPrimaryPath = legacyEnvPath;
  }
  if (configuredEnvPath !== loadedPrimaryPath && (await fs.pathExists(configuredEnvPath))) {
    await loadEnvFileIntoProcess(configuredEnvPath);
  }
}

/**
 * Ensure hooks configuration exists in project config file
 *
 * This function handles auto-migration for the hooks configuration:
 * - If .juno_task/config.json doesn't exist: create it with default config including empty hooks section
 * - If it exists but has no "hooks" field: add hooks: {} to the file
 * - Preserve all existing configuration
 *
 * @param baseDir - Base directory where .juno_task directory should be located
 * @returns Promise that resolves when migration is complete
 */
function isPlainObject(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function cloneJsonValue<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

/** Add only absent persisted defaults; scalar and array values remain user-owned. */
export function mergePersistedProjectDefaults(
  existing: Record<string, unknown>,
  defaults: Record<string, unknown>,
): boolean {
  let changed = false;
  for (const [key, defaultValue] of Object.entries(defaults)) {
    if (!Object.prototype.hasOwnProperty.call(existing, key)) {
      existing[key] = cloneJsonValue(defaultValue);
      changed = true;
      continue;
    }
    const currentValue = existing[key];
    if (
      key !== 'hooks' &&
      key !== 'defaultModels' &&
      isPlainObject(currentValue) &&
      isPlainObject(defaultValue)
    ) {
      changed = mergePersistedProjectDefaults(currentValue, defaultValue) || changed;
    }
  }
  return changed;
}

export async function writeProjectConfigAtomic(
  configPath: string,
  payload: Record<string, unknown>,
  replace: (source: string, destination: string) => Promise<void> = fs.rename,
  expectedOriginal?: Buffer,
): Promise<void> {
  const mode = (await fs.stat(configPath)).mode & 0o777;
  const tempPath = path.join(
    path.dirname(configPath),
    `.${path.basename(configPath)}.${process.pid}.${randomUUID()}.tmp`,
  );
  try {
    await fs.writeFile(tempPath, `${JSON.stringify(payload, null, 2)}\n`, { mode });
    if (expectedOriginal && !(await fs.readFile(configPath)).equals(expectedOriginal)) {
      throw new Error('project config changed during migration');
    }
    await replace(tempPath, configPath);
  } finally {
    await fs.remove(tempPath).catch(() => undefined);
  }
}

async function validateProjectConfigBeforeWrites(
  configPath: string,
  baseDir: string,
): Promise<void> {
  if (!(await fs.pathExists(configPath))) return;
  const projectConfig = await loadConfigFromFile(configPath, baseDir);
  validateConfig({ ...DEFAULT_CONFIG, ...projectConfig });
}

async function acquireProjectConfigMigrationLock(
  lockPath: string,
): Promise<fsPromises.FileHandle | undefined> {
  try {
    const handle = await fsPromises.open(lockPath, 'wx', 0o600);
    await handle.writeFile(`${JSON.stringify({ pid: process.pid, createdAt: Date.now() })}\n`);
    return handle;
  } catch (error: any) {
    if (error?.code === 'EEXIST') return undefined;
    throw error;
  }
}

async function ensureHooksConfig(baseDir: string): Promise<void> {
  const configDir = path.join(baseDir, '.juno_task');
  const configPath = path.join(configDir, 'config.json');
  const lockPath = path.join(configDir, '.config.json.migration.lock');
  let lock: fsPromises.FileHandle | undefined;
  try {
    await fs.ensureDir(configDir);
    lock = await acquireProjectConfigMigrationLock(lockPath);
    if (!lock) return;

    await validateProjectConfigBeforeWrites(configPath, baseDir);

    // Check if config file exists
    const configExists = await fs.pathExists(configPath);

    // Use default hooks template with file size monitoring commands
    const allHookTypes = getDefaultHooks();

    if (!configExists) {
      // Create a complete project config from the same persisted defaults used by migration.
      const defaultConfig = createPersistedProjectConfigDefaults(baseDir);
      await fs.writeJson(configPath, defaultConfig, { spaces: 2 });
    } else {
      // Read existing config and add only newly introduced persisted defaults.
      const originalConfigBytes = await fs.readFile(configPath);
      const existingConfig = JSON.parse(originalConfigBytes.toString('utf8')) as Record<string, any>;
      const persistedDefaults = createPersistedProjectConfigDefaults(baseDir);
      // A legacy single-model choice is user intent; seed the new map with it before additive merge.
      if (
        !isPlainObject(existingConfig.defaultModels) &&
        typeof existingConfig.defaultModel === 'string'
      ) {
        const selected =
          typeof existingConfig.defaultSubagent === 'string' ? existingConfig.defaultSubagent : 'claude';
        (persistedDefaults.defaultModels as Record<string, string>)[selected] =
          existingConfig.defaultModel;
      }
      let needsUpdate = mergePersistedProjectDefaults(existingConfig, persistedDefaults);
      if (
        typeof existingConfig.configVersion === 'number' &&
        existingConfig.configVersion < PROJECT_CONFIG_VERSION
      ) {
        existingConfig.configVersion = PROJECT_CONFIG_VERSION;
        needsUpdate = true;
      }

      // Hooks are user-owned and opaque. Only a wholly absent hooks section receives defaults.
      if (!existingConfig.hooks) {
        existingConfig.hooks = allHookTypes;
        needsUpdate = true;
      }

      // Migration: Add defaultModel if missing (for configs created before this feature)
      if (!Object.prototype.hasOwnProperty.call(existingConfig, 'defaultModel')) {
        const subagent = existingConfig.defaultSubagent || 'claude';
        existingConfig.defaultModel =
          SUBAGENT_DEFAULT_MODELS[subagent as keyof typeof SUBAGENT_DEFAULT_MODELS] ||
          SUBAGENT_DEFAULT_MODELS.claude;
        needsUpdate = true;
      }

      // Migration: add per-subagent default model map when absent
      if (
        !existingConfig.defaultModels ||
        typeof existingConfig.defaultModels !== 'object' ||
        Array.isArray(existingConfig.defaultModels)
      ) {
        const baseDefaults = { ...SUBAGENT_DEFAULT_MODELS } as Record<string, string>;
        const subagent = existingConfig.defaultSubagent || 'claude';
        if (typeof existingConfig.defaultModel === 'string') {
          baseDefaults[subagent] = existingConfig.defaultModel;
        }
        existingConfig.defaultModels = baseDefaults;
        needsUpdate = true;
      }

      // Existing model and iteration scalars are explicit project values and are never rewritten.

      // Ensure env bootstrap keys exist in project config
      if (!Object.prototype.hasOwnProperty.call(existingConfig, 'envFilePath')) {
        existingConfig.envFilePath = DEFAULT_PROJECT_ENV_FILE;
        needsUpdate = true;
      }

      if (typeof existingConfig.envFileCopied !== 'boolean') {
        existingConfig.envFileCopied = false;
        needsUpdate = true;
      }

      if (needsUpdate) {
        await writeProjectConfigAtomic(configPath, existingConfig, fs.rename, originalConfigBytes);
      }
    }
  } catch (error) {
    // Invalid config or migration failures remain visible; the original file is not replaced.
    console.warn(`Warning: Failed to ensure project configuration: ${error}`);
    throw error;
  } finally {
    if (lock) {
      await lock.close().catch(() => undefined);
      await fs.remove(lockPath).catch(() => undefined);
    }
  }
}

/**
 * Load and validate configuration from all sources
 *
 * This is the main entry point for configuration loading.
 * It performs auto-discovery, merging, and validation.
 *
 * @param options - Configuration loading options
 * @param options.baseDir - Base directory for relative path resolution
 * @param options.configFile - Specific configuration file to load
 * @param options.cliConfig - CLI configuration override
 * @returns Promise resolving to validated configuration
 *
 * @example
 * ```typescript
 * // Load with auto-discovery
 * const config = await loadConfig();
 *
 * // Load with specific file
 * const config = await loadConfig({
 *   configFile: './my-config.json'
 * });
 *
 * // Load with CLI overrides
 * const config = await loadConfig({
 *   cliConfig: { verbose: true, logLevel: 'debug' }
 * });
 * ```
 */
export async function loadConfig(
  options: {
    baseDir?: string;
    configFile?: string;
    cliConfig?: Partial<JunoTaskConfig>;
  } = {},
): Promise<JunoTaskConfig> {
  const { baseDir = process.cwd(), configFile, cliConfig } = options;
  const invocationDir = path.resolve(baseDir);
  let profileDir = invocationDir;
  let invocationRole: 'controller' | 'controller-retired' | 'task' | 'integration-owner' | 'unregistered' = 'unregistered';
  if (!configFile) {
    try {
      const resolution = resolveController(invocationDir, 'diagnostic');
      profileDir = path.resolve(resolution.path);
      invocationRole = resolution.role;
    } catch {
      // Unmanaged and legacy projects continue to use their local project config.
    }
  }
  const metadataSource = configFile ? undefined : await readMetadataAgentProfile(profileDir);
  const metadataAgentProfile = metadataSource?.profile;

  const allowProjectWrites = process.env.YYLO_PROJECT_BOOTSTRAP_WRITES !== '0'
    && profileDir === invocationDir && metadataSource === undefined;

  const resolveConfig = async (): Promise<JunoTaskConfig> => {
    const loader = new ConfigLoader(invocationDir, profileDir);
    loader.fromEnvironment();
    if (configFile) {
      await loader.fromFile(configFile);
    } else {
      await loader.autoDiscoverFile();
    }
    if (cliConfig) loader.fromCli(cliConfig);
    const merged = loader.merge();
    // A canonical controller profile supplies preferences, never the invoking
    // task/integration workspace identity.
    if (profileDir !== invocationDir) {
      merged.workingDirectory = invocationDir;
      merged.sessionDirectory = path.join(invocationDir, '.juno_task');
    }
    if (metadataSource) {
      const selectedHooks = selectAgentProfileHooks(metadataAgentProfile, invocationRole);
      if (selectedHooks) merged.hooks = selectedHooks;
      else delete merged.hooks;
    }
    return validateConfig(merged);
  };

  // Metadata controllers never load an ambient/default env file. Only a
  // reviewed explicit 0600 regular-file binding participates in precedence.
  if (metadataAgentProfile?.environmentBinding) {
    await loadAuthorizedProfileEnvironment(metadataAgentProfile.environmentBinding);
  } else if (!metadataSource) {
    await ensureAndLoadProjectEnv(profileDir, false);
  }
  let resolved = await resolveConfig();

  if (allowProjectWrites) {
    await ensureHooksConfig(profileDir);
    await ensureAndLoadProjectEnv(profileDir, true);
    resolved = await resolveConfig();
  }

  return resolved;
}

/**
 * Type export for configuration loading options
 */
export type ConfigLoadOptions = Parameters<typeof loadConfig>[0];

/**
 * Type export for environment variable mapping
 */
export type EnvVarMapping = typeof ENV_VAR_MAPPING;
